use super::*;
use std::os::unix::fs::PermissionsExt;
use tempfile::TempDir;

fn fixture(limits: Limits) -> (TempDir, Store, Scope) {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().join("store");
    fs::create_dir(&root).unwrap();
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    let store = Store::open(&root, OpenMode::CreateNew, limits).unwrap();
    let scope = store
        .scope([("app".into(), Access::ReadWrite)].into())
        .unwrap();
    (dir, store, scope)
}

fn batch(namespace: &str, key: &str, revision: u64, value: &str) -> Batch {
    Batch {
        checks: vec![],
        writes: vec![Mutation {
            namespace: namespace.into(),
            key: key.into(),
            revision,
            value: Some(value.into()),
        }],
    }
}

fn failed<T: std::fmt::Debug>(result: ObservedResult<T>, origin: FailureOrigin, error: Error) {
    assert_eq!(result.unwrap_err(), ObservedError { origin, error });
}

fn revision(store: &Store) -> i64 {
    store
        .connection
        .query_row("SELECT revision FROM meta WHERE id=1", [], |row| row.get(0))
        .unwrap()
}

#[test]
fn batch_preflight_preserves_original_scope_and_never_mutates_or_reserves() {
    let (_dir, mut store, scope) = fixture(Limits::default());
    let read = store
        .scope(BTreeMap::from([("app".into(), Access::Read)]))
        .unwrap();
    assert_eq!(
        store.check_batch(&read, &batch("app", "key", 0, "data")),
        Err(Error::Denied)
    );
    assert!(
        store
            .check_batch(&scope, &batch("app", "key", 0, "data"))
            .is_ok()
    );
    assert_eq!(revision(&store), 0);
    assert_eq!(store.get(&scope, "app", "key").unwrap().revision, 0);
    store
        .commit(&scope, &batch("app", "key", 0, "different committed data"))
        .unwrap();
    assert!(
        store
            .check_batch(&scope, &batch("app", "key", 0, "data"))
            .is_ok()
    );
    assert_eq!(
        store.commit(&scope, &batch("app", "key", 0, "data")),
        Err(Error::Conflict)
    );
    assert_eq!(revision(&store), 1);
}

#[test]
fn batch_preflight_rejects_foreign_boot_and_original_shape_and_value_ceilings() {
    let (_dir, store, scope) = fixture(Limits::default());
    let (_other, other, _) = fixture(Limits::default());
    assert_eq!(
        other.check_batch(&scope, &batch("app", "key", 0, "data")),
        Err(Error::Denied)
    );
    assert_eq!(
        store.check_batch(
            &scope,
            &Batch {
                checks: vec![],
                writes: vec![]
            }
        ),
        Err(Error::Invalid)
    );
    assert_eq!(
        store.check_batch(
            &scope,
            &batch(
                "app",
                "key",
                0,
                &"x".repeat(Limits::default().value_bytes + 1)
            )
        ),
        Err(Error::Limit)
    );
    assert_eq!(revision(&store), 0);
}

#[test]
fn observed_success_returns_the_actual_existing_record_and_commit_receipt() {
    let (_dir, mut store, scope) = fixture(Limits::default());
    assert_eq!(
        store.get_observed(&scope, "app", "key").unwrap(),
        Record {
            revision: 0,
            value: None
        }
    );
    assert_eq!(
        store.commit_observed(&scope, &batch("app", "key", 0, "first")),
        Ok(Receipt { revision: 1 })
    );
    assert_eq!(
        store.get_observed(&scope, "app", "key").unwrap(),
        store.get(&scope, "app", "key").unwrap()
    );
    assert_eq!(
        store.commit(&scope, &batch("app", "key", 1, "second")),
        Ok(Receipt { revision: 2 })
    );
    assert_eq!(
        store.get_observed(&scope, "app", "key").unwrap(),
        Record {
            revision: 2,
            value: Some("second".into())
        }
    );
    assert_eq!(revision(&store), 2);
}

#[test]
fn scope_and_address_prechecks_take_precedence_over_unavailable_storage() {
    let (dir, mut store, scope) = fixture(Limits::default());
    let creator = store
        .scope([("app".into(), Access::CreateOnly)].into())
        .unwrap();
    let reader = store.scope([("app".into(), Access::Read)].into()).unwrap();
    fs::rename(dir.path().join("store"), dir.path().join("parked")).unwrap();
    failed(
        store.get_observed(&creator, "app", "key"),
        FailureOrigin::Precheck,
        Error::Denied,
    );
    failed(
        store.get_observed(&scope, "other", "key"),
        FailureOrigin::Precheck,
        Error::Denied,
    );
    failed(
        store.get_observed(&scope, "app", "bad/key"),
        FailureOrigin::Precheck,
        Error::Invalid,
    );
    failed(
        store.commit_observed(&reader, &batch("app", "key", 0, "denied")),
        FailureOrigin::Precheck,
        Error::Denied,
    );
    failed(
        store.commit_observed(&scope, &batch("app", "key", i64::MAX as u64 + 1, "invalid")),
        FailureOrigin::Precheck,
        Error::Invalid,
    );
    assert_eq!(revision(&store), 0);
    fs::rename(dir.path().join("parked"), dir.path().join("store")).unwrap();
    assert_eq!(store.get(&scope, "app", "key").unwrap().revision, 0);
}

#[test]
fn actual_missing_storage_path_has_storage_origin_after_scope_checks() {
    let (dir, mut store, scope) = fixture(Limits::default());
    store
        .commit(&scope, &batch("app", "key", 0, "retained"))
        .unwrap();
    fs::rename(dir.path().join("store"), dir.path().join("parked")).unwrap();
    failed(
        store.get_observed(&scope, "app", "key"),
        FailureOrigin::Storage,
        Error::Storage,
    );
    failed(
        store.commit_observed(&scope, &batch("app", "key", 1, "not-committed")),
        FailureOrigin::Storage,
        Error::Storage,
    );
    assert_eq!(store.get(&scope, "app", "key"), Err(Error::Storage));
    assert_eq!(
        store.commit(&scope, &batch("app", "key", 1, "not-committed")),
        Err(Error::Storage)
    );
    assert_eq!(revision(&store), 1);
    fs::rename(dir.path().join("parked"), dir.path().join("store")).unwrap();
    assert_eq!(
        store.get(&scope, "app", "key").unwrap().value.as_deref(),
        Some("retained")
    );
}

#[test]
fn unsafe_private_layout_is_not_misreported_as_a_denied_scope() {
    let (dir, mut store, scope) = fixture(Limits::default());
    let root = dir.path().join("store");
    fs::set_permissions(&root, fs::Permissions::from_mode(0o755)).unwrap();
    failed(
        store.get_observed(&scope, "app", "key"),
        FailureOrigin::Storage,
        Error::Denied,
    );
    failed(
        store.commit_observed(&scope, &batch("app", "key", 0, "blocked")),
        FailureOrigin::Storage,
        Error::Denied,
    );
    assert!(matches!(
        store.scope([("app".into(), Access::ReadWrite)].into()),
        Err(Error::Denied)
    ));
    assert_eq!(store.get(&scope, "app", "key"), Err(Error::Denied));
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    assert_eq!(revision(&store), 0);
    assert_eq!(store.get(&scope, "app", "key").unwrap().revision, 0);
}

#[test]
fn wrong_process_is_precheck_failure_even_when_storage_layout_is_also_invalid() {
    let (dir, mut store, scope) = fixture(Limits::default());
    let owner = store.owner.process;
    store.owner.process = owner.wrapping_add(1);
    fs::set_permissions(dir.path().join("store"), fs::Permissions::from_mode(0o755)).unwrap();
    failed(
        store.get_observed(&scope, "app", "key"),
        FailureOrigin::Precheck,
        Error::Denied,
    );
    failed(
        store.commit_observed(&scope, &batch("app", "key", 0, "blocked")),
        FailureOrigin::Precheck,
        Error::Denied,
    );
    assert_eq!(store.get(&scope, "app", "key"), Err(Error::Denied));
    store.owner.process = owner;
    fs::set_permissions(dir.path().join("store"), fs::Permissions::from_mode(0o700)).unwrap();
    assert_eq!(revision(&store), 0);
}

#[test]
fn foreign_store_scopes_fail_precheck_without_storage_access() {
    let (_dir, mut store, scope) = fixture(Limits::default());
    let (_other_dir, _other, foreign) = fixture(Limits::default());
    store.poisoned = true;
    failed(
        store.get_observed(&foreign, "app", "key"),
        FailureOrigin::Precheck,
        Error::Denied,
    );
    failed(
        store.commit_observed(&foreign, &batch("app", "key", 0, "blocked")),
        FailureOrigin::Precheck,
        Error::Denied,
    );
    failed(
        store.get_observed(&scope, "app", "key"),
        FailureOrigin::Storage,
        Error::ReopenRequired,
    );
    failed(
        store.commit_observed(&scope, &batch("app", "key", 0, "blocked")),
        FailureOrigin::Storage,
        Error::ReopenRequired,
    );
    store.poisoned = false;
    assert_eq!(revision(&store), 0);
}

#[test]
fn malformed_and_oversized_batches_remain_prechecks_without_touching_storage() {
    let (_dir, mut store, scope) = fixture(Limits {
        value_bytes: 4,
        batch_bytes: 8,
        ..Limits::default()
    });
    store.poisoned = true;
    failed(
        store.commit_observed(
            &scope,
            &Batch {
                checks: vec![],
                writes: vec![],
            },
        ),
        FailureOrigin::Precheck,
        Error::Invalid,
    );
    failed(
        store.commit_observed(&scope, &batch("app", "key", 0, "large")),
        FailureOrigin::Precheck,
        Error::Limit,
    );
    let write = batch("app", "key", 0, "one").writes.pop().unwrap();
    failed(
        store.commit_observed(
            &scope,
            &Batch {
                checks: vec![],
                writes: vec![write.clone(), write],
            },
        ),
        FailureOrigin::Precheck,
        Error::Invalid,
    );
    store.poisoned = false;
    assert_eq!(revision(&store), 0);
}

#[test]
fn actual_conditional_conflict_is_storage_origin_but_never_a_successful_receipt() {
    let (_dir, mut store, scope) = fixture(Limits::default());
    store
        .commit(&scope, &batch("app", "key", 0, "retained"))
        .unwrap();
    failed(
        store.commit_observed(&scope, &batch("app", "key", 0, "stale")),
        FailureOrigin::Storage,
        Error::Conflict,
    );
    assert_eq!(revision(&store), 1);
    assert_eq!(
        store.get(&scope, "app", "key").unwrap().value.as_deref(),
        Some("retained")
    );
    assert!(!store.poisoned);
}

#[test]
fn actual_database_capacity_failure_is_distinct_from_an_oversized_input() {
    let (_dir, mut store, scope) = fixture(Limits {
        records: 1,
        ..Limits::default()
    });
    store
        .commit(&scope, &batch("app", "first", 0, "retained"))
        .unwrap();
    failed(
        store.commit_observed(&scope, &batch("app", "second", 0, "too-many-records")),
        FailureOrigin::Storage,
        Error::Limit,
    );
    assert_eq!(revision(&store), 1);
    assert_eq!(store.get(&scope, "app", "second").unwrap().revision, 0);
    assert!(!store.poisoned);
}

#[test]
fn actual_corrupt_record_does_not_leak_a_payload_and_cannot_be_overwritten() {
    let (_dir, mut store, scope) = fixture(Limits::default());
    store
        .commit(&scope, &batch("app", "key", 0, "private-value"))
        .unwrap();
    store
        .connection
        .execute(
            "UPDATE records SET digest=zeroblob(32) WHERE namespace='app' AND key='key'",
            [],
        )
        .unwrap();
    failed(
        store.get_observed(&scope, "app", "key"),
        FailureOrigin::Storage,
        Error::Corrupt,
    );
    failed(
        store.commit_observed(&scope, &batch("app", "key", 1, "repair-attempt")),
        FailureOrigin::Storage,
        Error::Corrupt,
    );
    assert_eq!(store.get(&scope, "app", "key"), Err(Error::Corrupt));
    assert_eq!(revision(&store), 1);
}

#[test]
fn actual_sqlite_write_failure_preserves_poison_and_reopen_requirement() {
    let (dir, mut store, scope) = fixture(Limits::default());
    // Test-only trigger forces the actual INSERT to fail after BEGIN IMMEDIATE.
    // There is no public SQL/fault-injection API in the native mechanism.
    store.connection.execute_batch("CREATE TRIGGER write_fault BEFORE INSERT ON records BEGIN SELECT RAISE(ABORT, 'injected write failure'); END;").unwrap();
    failed(
        store.commit_observed(&scope, &batch("app", "key", 0, "not-committed")),
        FailureOrigin::Storage,
        Error::Storage,
    );
    assert!(store.poisoned);
    failed(
        store.get_observed(&scope, "app", "key"),
        FailureOrigin::Storage,
        Error::ReopenRequired,
    );
    store
        .connection
        .execute_batch("DROP TRIGGER write_fault;")
        .unwrap();
    drop(store);
    let reopened = Store::open(
        &dir.path().join("store"),
        OpenMode::Existing,
        Limits::default(),
    )
    .unwrap();
    let current = reopened
        .scope([("app".into(), Access::ReadWrite)].into())
        .unwrap();
    failed(
        reopened.get_observed(&scope, "app", "key"),
        FailureOrigin::Precheck,
        Error::Denied,
    );
    assert_eq!(
        reopened
            .get_observed(&current, "app", "key")
            .unwrap()
            .revision,
        0
    );
    assert_eq!(revision(&reopened), 0);
}

#[test]
fn original_commit_hook_runs_once_only_after_validation_and_before_the_same_commit() {
    let (_dir, mut store, scope) = fixture(Limits::default());
    let calls = std::cell::Cell::new(0);
    assert_eq!(
        store.commit_inner(&scope, &batch("app", "key", 0, "retained"), || calls
            .set(calls.get() + 1)),
        Ok(Receipt { revision: 1 })
    );
    assert_eq!(calls.get(), 1);
    failed(
        store.commit_observed_inner(&scope, &batch("app", "key", 0, "stale"), || {
            panic!("conflict reached commit hook")
        }),
        FailureOrigin::Storage,
        Error::Conflict,
    );
    assert_eq!(revision(&store), 1);
}
