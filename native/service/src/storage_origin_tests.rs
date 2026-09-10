use super::*;
use sigil_durable_store::store::ObservedError;

fn failure(origin: FailureOrigin, error: Error) -> ObservedError {
    ObservedError { origin, error }
}

#[test]
fn every_origin_and_error_is_encoded_exactly_without_diagnostics_or_payload() {
    for (origin, origin_text) in [
        (FailureOrigin::Precheck, "precheck"),
        (FailureOrigin::Storage, "storage"),
    ] {
        assert_eq!(origin_label(origin), origin_text);
        for (error, label) in ERRORS {
            let read = super::super::durable_read_result(Err(failure(origin, error))).unwrap();
            assert_eq!(
                fields(&read, "DR2\n", 6).unwrap(),
                ["error", "0", "0", "", label, origin_text]
            );
            let commit = super::super::durable_receipt(Err(failure(origin, error))).unwrap();
            assert_eq!(
                fields(&commit, "DC2\n", 4).unwrap(),
                ["error", "0", label, origin_text]
            );
            assert!(read.len() < 128 && commit.len() < 128);
        }
    }
}

#[test]
fn successful_new_observations_have_no_failure_origin_and_reject_old_markers() {
    let (_dir, mut store, scope, _cells) = fixture();
    let commit = durable_commit(&mut store, &scope, &batch("key", 0, Some("value"))).unwrap();
    assert_eq!(fields(&commit, "DC2\n", 4).unwrap(), ["ok", "1", "", ""]);
    assert!(fields(&commit, "DC1\n", 3).is_err());
    assert!(fields(&commit, "SC1\n", 2).is_err());
    let read = durable_read(&store, &scope, "app", "key").unwrap();
    assert_eq!(
        fields(&read, "DR2\n", 6).unwrap(),
        ["ok", "1", "1", "value", "", ""]
    );
    assert!(fields(&read, "DR1\n", 5).is_err());
    assert!(fields(&read, "SR1\n", 3).is_err());
}

#[test]
fn actual_denied_scope_and_unsafe_private_layout_have_different_origins() {
    let (dir, mut store, scope, cells) = fixture();
    let denied = store
        .scope([("other".into(), Access::ReadWrite)].into())
        .unwrap();
    let root = dir.path().join("store");
    fs::set_permissions(&root, fs::Permissions::from_mode(0o755)).unwrap();
    for (held, origin) in [(&denied, "precheck"), (&scope, "storage")] {
        let read = durable_read(&store, held, "app", "key").unwrap();
        assert_eq!(
            fields(&read, "DR2\n", 6).unwrap(),
            ["error", "0", "0", "", "denied", origin]
        );
        let commit =
            durable_commit(&mut store, held, &batch("key", 0, Some("not-written"))).unwrap();
        assert_eq!(
            fields(&commit, "DC2\n", 4).unwrap(),
            ["error", "0", "denied", origin]
        );
    }
    // No new scope is minted on failure; an existing distinct temporary cell
    // remains readable under its already-held scope, without implying fallback.
    assert!(matches!(
        store.scope([("app".into(), Access::ReadWrite)].into()),
        Err(Error::Denied)
    ));
    assert_eq!(
        fields(
            &temporary_read(&cells, &store, &scope, "app", "key").unwrap(),
            "VR1\n",
            5
        )
        .unwrap(),
        ["ok", "0", "0", "", ""]
    );
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    assert_eq!(store.get(&scope, "app", "key").unwrap().revision, 0);
}

#[test]
fn actual_missing_path_does_not_override_scope_failure_precedence() {
    let (dir, mut store, scope, _cells) = fixture();
    let denied = store
        .scope([("other".into(), Access::ReadWrite)].into())
        .unwrap();
    let root = dir.path().join("store");
    let parked = dir.path().join("parked");
    fs::rename(&root, &parked).unwrap();
    for (held, label, origin) in [
        (&denied, "denied", "precheck"),
        (&scope, "storage", "storage"),
    ] {
        assert_eq!(
            fields(
                &durable_read(&store, held, "app", "key").unwrap(),
                "DR2\n",
                6
            )
            .unwrap(),
            ["error", "0", "0", "", label, origin]
        );
        assert_eq!(
            fields(
                &durable_commit(&mut store, held, &batch("key", 0, Some("blocked"))).unwrap(),
                "DC2\n",
                4
            )
            .unwrap(),
            ["error", "0", label, origin]
        );
    }
    fs::rename(&parked, &root).unwrap();
    assert_eq!(store.get(&scope, "app", "key").unwrap().revision, 0);
}

#[test]
fn conditional_conflicts_are_storage_observations_without_a_success_receipt() {
    let (_dir, mut store, scope, _cells) = fixture();
    durable_commit(&mut store, &scope, &batch("key", 0, Some("retained"))).unwrap();
    let conflict = durable_commit(&mut store, &scope, &batch("key", 0, Some("stale"))).unwrap();
    assert_eq!(
        fields(&conflict, "DC2\n", 4).unwrap(),
        ["error", "0", "conflict", "storage"]
    );
    assert_eq!(
        fields(
            &durable_read(&store, &scope, "app", "key").unwrap(),
            "DR2\n",
            6
        )
        .unwrap(),
        ["ok", "1", "1", "retained", "", ""]
    );
}

#[test]
fn actual_input_and_database_capacity_limits_have_distinct_origins() {
    let dir = tempfile::tempdir().unwrap();
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let mut store = Store::open(
        dir.path(),
        OpenMode::CreateNew,
        Limits {
            value_bytes: 4,
            records: 1,
            ..Limits::default()
        },
    )
    .unwrap();
    let scope = store
        .scope([("app".into(), Access::ReadWrite)].into())
        .unwrap();
    let rejected = durable_commit(&mut store, &scope, &batch("key", 0, Some("oversize"))).unwrap();
    assert_eq!(
        fields(&rejected, "DC2\n", 4).unwrap(),
        ["error", "0", "limit", "precheck"]
    );
    durable_commit(&mut store, &scope, &batch("key", 0, Some("kept"))).unwrap();
    let full = durable_commit(&mut store, &scope, &batch("other", 0, Some("no"))).unwrap();
    assert_eq!(
        fields(&full, "DC2\n", 4).unwrap(),
        ["error", "0", "limit", "storage"]
    );
    assert_eq!(store.get(&scope, "app", "other").unwrap().revision, 0);
}

#[test]
fn an_actual_replaced_database_identity_is_a_storage_refusal_without_repair() {
    let (dir, mut store, scope, _cells) = fixture();
    durable_commit(&mut store, &scope, &batch("key", 0, Some("retained"))).unwrap();
    let path = dir.path().join("store/records.sqlite");
    let original = dir.path().join("store/original.sqlite");
    fs::rename(&path, &original).unwrap();
    fs::copy(&original, &path).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    let read = durable_read(&store, &scope, "app", "key").unwrap();
    assert_eq!(
        fields(&read, "DR2\n", 6).unwrap(),
        ["error", "0", "0", "", "denied", "storage"]
    );
    let commit = durable_commit(&mut store, &scope, &batch("key", 1, Some("blocked"))).unwrap();
    assert_eq!(
        fields(&commit, "DC2\n", 4).unwrap(),
        ["error", "0", "denied", "storage"]
    );
    fs::remove_file(&path).unwrap();
    fs::rename(&original, &path).unwrap();
    assert_eq!(
        store.get(&scope, "app", "key").unwrap().value.as_deref(),
        Some("retained")
    );
}
