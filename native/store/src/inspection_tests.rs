use super::*;
use std::os::unix::fs::PermissionsExt;
use tempfile::TempDir;

fn fixture() -> (TempDir, Store, Scope) {
    fixture_with_limits(Limits::default())
}

fn fixture_with_limits(limits: Limits) -> (TempDir, Store, Scope) {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().join("store");
    fs::create_dir(&root).unwrap();
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    let store = Store::open(&root, OpenMode::CreateNew, limits).unwrap();
    let scope = store
        .scope(
            [
                ("a".into(), Access::ReadWrite),
                ("b".into(), Access::ReadWrite),
            ]
            .into(),
        )
        .unwrap();
    (dir, store, scope)
}

fn inspect(store: &mut Store, scope: &Scope, names: &[&str]) -> Inspected<()> {
    store.inspect_namespaces(
        scope,
        &names.iter().map(|name| (*name).into()).collect::<Vec<_>>(),
        Instant::now() + Duration::from_secs(10),
    )
}

fn write(store: &mut Store, scope: &Scope, namespace: &str, value: Option<&str>) {
    let revision = store.get(scope, namespace, "key").unwrap().revision;
    store
        .commit(
            scope,
            &Batch {
                checks: vec![],
                writes: vec![Mutation {
                    namespace: namespace.into(),
                    key: "key".into(),
                    revision,
                    value: value.map(str::to_owned),
                }],
            },
        )
        .unwrap();
}

fn precheck(error: Error) -> Failure {
    Failure::Observed(ObservedError::precheck(error))
}

#[test]
fn pinned_sqlite_integrity_query_has_a_content_free_result() {
    let (_dir, store, _scope) = fixture();
    let ok: bool = store
        .connection
        .query_row("PRAGMA quick_check(1)", [], |row| {
            Ok(matches!(
                row.get_ref(0)?,
                rusqlite::types::ValueRef::Text(b"ok")
            ))
        })
        .unwrap();
    assert!(ok);
}

#[test]
fn empty_live_empty_value_and_tombstone_are_inspected_without_mutation() {
    let (_dir, mut store, scope) = fixture();
    assert_eq!(inspect(&mut store, &scope, &["a", "b"]), Ok(()));
    for value in [Some("private payload"), Some(""), None] {
        write(&mut store, &scope, "a", value);
        let before = fs::read(store.root.join("records.sqlite")).unwrap();
        let record = store.get(&scope, "a", "key").unwrap();
        assert_eq!(inspect(&mut store, &scope, &["a", "b"]), Ok(()));
        assert_eq!(store.get(&scope, "a", "key").unwrap(), record);
        assert_eq!(fs::read(store.root.join("records.sqlite")).unwrap(), before);
        assert!(!store.root.join("records.sqlite-journal").exists());
        assert!(store.connection.is_autocommit());
    }
}

#[test]
fn reader_can_inspect_but_creator_and_foreign_scopes_cannot() {
    let (_dir, mut store, scope) = fixture();
    write(&mut store, &scope, "a", Some("retained"));
    let reader = store.scope([("a".into(), Access::Read)].into()).unwrap();
    let creator = store
        .scope([("a".into(), Access::CreateOnly)].into())
        .unwrap();
    let (_other_dir, _other_store, foreign) = fixture();
    assert_eq!(inspect(&mut store, &reader, &["a"]), Ok(()));
    for (held, names) in [
        (&reader, vec!["b"]),
        (&creator, vec!["a"]),
        (&foreign, vec!["a"]),
    ] {
        assert_eq!(
            inspect(&mut store, held, &names),
            Err(precheck(Error::Denied))
        );
    }
}

#[test]
fn invalid_or_ungranted_namespace_precedes_missing_storage() {
    let (dir, mut store, scope) = fixture();
    fs::rename(dir.path().join("store"), dir.path().join("parked")).unwrap();
    for (names, expected) in [
        (vec![], Error::Limit),
        (vec!["a"; NAMESPACES + 1], Error::Limit),
        (vec!["a", "a"], Error::Invalid),
        (vec!["a", "bad/path"], Error::Invalid),
        (vec!["a", "other"], Error::Denied),
    ] {
        assert_eq!(inspect(&mut store, &scope, &names), Err(precheck(expected)));
    }
    assert_eq!(
        inspect(&mut store, &scope, &["a"]),
        Err(storage(Error::Storage))
    );
}

#[test]
fn held_scope_survives_actual_path_outage_without_reopening_or_resetting() {
    let (dir, mut store, scope) = fixture();
    write(&mut store, &scope, "a", Some("retained"));
    let before = store.get(&scope, "a", "key").unwrap();
    fs::rename(dir.path().join("store"), dir.path().join("parked")).unwrap();
    assert_eq!(
        inspect(&mut store, &scope, &["a"]),
        Err(storage(Error::Storage))
    );
    fs::rename(dir.path().join("parked"), dir.path().join("store")).unwrap();
    assert_eq!(inspect(&mut store, &scope, &["a"]), Ok(()));
    assert_eq!(store.get(&scope, "a", "key").unwrap(), before);
}

#[test]
fn unsafe_layout_and_replaced_file_are_storage_facts() {
    let (_dir, mut store, scope) = fixture();
    fs::set_permissions(&store.root, fs::Permissions::from_mode(0o755)).unwrap();
    assert_eq!(
        inspect(&mut store, &scope, &["a"]),
        Err(storage(Error::Denied))
    );
    fs::set_permissions(&store.root, fs::Permissions::from_mode(0o700)).unwrap();
    let path = store.root.join("records.sqlite");
    fs::rename(&path, store.root.join("parked.sqlite")).unwrap();
    fs::copy(store.root.join("parked.sqlite"), &path).unwrap();
    assert_eq!(
        inspect(&mut store, &scope, &["a"]),
        Err(storage(Error::Denied))
    );
}

#[test]
fn wrong_owner_and_poison_are_not_successful_inspections() {
    let (_dir, mut store, scope) = fixture();
    let owner = store.owner.process;
    store.owner.process = owner.wrapping_add(1);
    assert_eq!(
        inspect(&mut store, &scope, &["a"]),
        Err(precheck(Error::Denied))
    );
    store.owner.process = owner;
    store.poisoned = true;
    assert_eq!(
        inspect(&mut store, &scope, &["a"]),
        Err(storage(Error::ReopenRequired))
    );
}

#[test]
fn deadline_does_not_become_storage_corruption_or_leak_a_handler() {
    let (_dir, mut store, scope) = fixture();
    assert_eq!(
        store.inspect_namespaces(&scope, &["a".into()], Instant::now()),
        Err(Failure::Deadline)
    );
    let budget = Budget::install(&store.connection, Instant::now(), SQLITE_STEPS).unwrap();
    assert_eq!(budget.check(), Err(Failure::Deadline));
    drop(budget);
    write(&mut store, &scope, "a", Some("still writable"));
    assert_eq!(inspect(&mut store, &scope, &["a"]), Ok(()));
}

#[test]
fn real_sqlite_progress_interrupt_is_bounded_and_handler_is_removed_on_error() {
    let (_dir, mut store, scope) = fixture();
    for group in 0..16 {
        let writes = (0..64)
            .map(|n| Mutation {
                namespace: "a".into(),
                key: format!("key-{group:02}-{n:02}"),
                revision: 0,
                value: Some("opaque".repeat(10)),
            })
            .collect();
        store
            .commit(
                &scope,
                &Batch {
                    checks: vec![],
                    writes,
                },
            )
            .unwrap();
    }
    assert_eq!(
        store.inspect_bounded(
            &scope,
            &["a".into()],
            Instant::now() + Duration::from_secs(10),
            PROGRESS_STEPS
        ),
        Err(Failure::WorkLimit)
    );
    assert!(store.connection.is_autocommit());
    write(&mut store, &scope, "a", Some("not interrupted"));
    assert_eq!(inspect(&mut store, &scope, &["a"]), Ok(()));
}

#[test]
fn deadline_interrupts_actual_sqlite_work_and_is_not_a_clock_double() {
    let (_dir, store, _scope) = fixture();
    let budget = Budget::install(
        &store.connection,
        Instant::now() + Duration::from_millis(5),
        u64::MAX,
    )
    .unwrap();
    let result: rusqlite::Result<i64> = store.connection.query_row(
        "WITH RECURSIVE numbers(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM numbers WHERE x < 1000000000) SELECT sum(x) FROM numbers",
        [], |row| row.get(0),
    );
    assert_eq!(
        result.unwrap_err().sqlite_error_code(),
        Some(rusqlite::ErrorCode::OperationInterrupted)
    );
    assert_eq!(budget.check(), Err(Failure::Deadline));
    drop(budget);
    let value: i64 = store
        .connection
        .query_row("SELECT 1", [], |r| r.get(0))
        .unwrap();
    assert_eq!(value, 1);
}

#[test]
fn actual_exclusive_database_lock_is_busy_then_recovers() {
    let (_dir, mut store, scope) = fixture();
    let other = Connection::open(store.root.join("records.sqlite")).unwrap();
    other.execute_batch("BEGIN EXCLUSIVE").unwrap();
    assert_eq!(
        inspect(&mut store, &scope, &["a"]),
        Err(storage(Error::Busy))
    );
    other.execute_batch("ROLLBACK").unwrap();
    assert_eq!(inspect(&mut store, &scope, &["a"]), Ok(()));
}

#[test]
fn global_identity_schema_and_frozen_limits_are_rechecked() {
    for corrupt in [
        "PRAGMA application_id=0",
        "PRAGMA user_version=2",
        "CREATE TABLE extra(value)",
        "CREATE TRIGGER extra AFTER INSERT ON records BEGIN SELECT 1; END",
        "UPDATE meta SET limits='{}'",
        "DELETE FROM meta",
        "PRAGMA ignore_check_constraints=ON; UPDATE meta SET revision=-1",
    ] {
        let (_dir, mut store, scope) = fixture();
        store.connection.execute_batch(corrupt).unwrap();
        assert_eq!(
            inspect(&mut store, &scope, &["a"]),
            Err(storage(Error::Corrupt)),
            "{corrupt}"
        );
        assert!(store.connection.is_autocommit());
    }
}

#[test]
fn frozen_limits_keep_existing_open_semantics_for_json_order_and_whitespace() {
    let (_dir, mut store, scope) = fixture();
    let alternate =
        serde_json::to_string_pretty(&serde_json::to_value(&store.limits).unwrap()).unwrap();
    assert_ne!(alternate, serde_json::to_string(&store.limits).unwrap());
    store
        .connection
        .execute("UPDATE meta SET limits=?1", [alternate])
        .unwrap();
    assert_eq!(inspect(&mut store, &scope, &["a"]), Ok(()));
    let root = store.root.clone();
    drop(store);
    let mut reopened = Store::open(&root, OpenMode::Existing, Limits::default()).unwrap();
    let scope = reopened.scope([("a".into(), Access::Read)].into()).unwrap();
    assert_eq!(inspect(&mut reopened, &scope, &["a"]), Ok(()));
}

#[test]
fn record_digests_are_checked_only_inside_the_complete_requested_scope() {
    let (_dir, mut store, scope) = fixture();
    write(&mut store, &scope, "a", Some("a secret"));
    write(&mut store, &scope, "b", Some("b secret"));
    store
        .connection
        .execute(
            "UPDATE records SET digest=zeroblob(32) WHERE namespace='b'",
            [],
        )
        .unwrap();
    let reader = store.scope([("a".into(), Access::Read)].into()).unwrap();
    assert_eq!(inspect(&mut store, &reader, &["a"]), Ok(()));
    assert_eq!(
        inspect(&mut store, &reader, &["a", "b"]),
        Err(precheck(Error::Denied))
    );
    assert_eq!(
        inspect(&mut store, &scope, &["a", "b"]),
        Err(storage(Error::Corrupt))
    );
}

#[test]
fn malformed_scoped_records_are_not_hidden_by_sqlite_structural_success() {
    for corrupt in [
        "UPDATE records SET value=x'ff'",
        "UPDATE records SET value=zeroblob(2097153)",
        "UPDATE records SET key='bad/path'",
        "UPDATE records SET key=''",
        "UPDATE records SET digest=zeroblob(31)",
        "UPDATE records SET revision=999",
        "UPDATE records SET value=NULL",
    ] {
        let (_dir, mut store, scope) = fixture();
        write(&mut store, &scope, "a", Some("original"));
        store.connection.execute_batch(corrupt).unwrap();
        assert_eq!(
            inspect(&mut store, &scope, &["a"]),
            Err(storage(Error::Corrupt)),
            "{corrupt}"
        );
    }
}

#[test]
fn physical_capacity_breach_is_not_a_good_read_inspection() {
    let (_dir, mut store, scope) = fixture_with_limits(Limits {
        value_bytes: 256,
        live_bytes: 1024,
        ..Limits::default()
    });
    write(&mut store, &scope, "a", Some("original"));
    // A real oversized stored value breaches the frozen shared capacity before
    // any scoped payload allocation. These are explicit test-fixture limits.
    store
        .connection
        .execute(
            "UPDATE records SET value=zeroblob(?1)",
            [store.limits.live_bytes as i64 + 1],
        )
        .unwrap();
    assert_eq!(
        inspect(&mut store, &scope, &["a"]),
        Err(storage(Error::Limit))
    );
}
