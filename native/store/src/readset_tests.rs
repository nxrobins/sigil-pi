use super::*;
use std::os::unix::fs::PermissionsExt;
use tempfile::TempDir;

fn fresh() -> (TempDir, Store, Scope) {
    let dir = tempfile::tempdir().unwrap();
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let store = Store::open(dir.path(), OpenMode::CreateNew, Limits::default()).unwrap();
    let scope = grant(
        &store,
        &[("app", Access::ReadWrite), ("other", Access::ReadWrite)],
    );
    (dir, store, scope)
}

fn grant(store: &Store, names: &[(&str, Access)]) -> Scope {
    store
        .scope(names.iter().map(|(n, a)| ((*n).into(), *a)).collect())
        .unwrap()
}

fn key(namespace: &str, key: &str) -> ReadKey {
    ReadKey {
        namespace: namespace.into(),
        key: key.into(),
    }
}

fn write(namespace: &str, key: &str, revision: u64, value: Option<&str>) -> Mutation {
    Mutation {
        namespace: namespace.into(),
        key: key.into(),
        revision,
        value: value.map(str::to_owned),
    }
}

fn batch(writes: Vec<Mutation>) -> Batch {
    Batch {
        checks: vec![],
        writes,
    }
}

fn revision(store: &Store) -> i64 {
    store
        .connection
        .query_row("SELECT revision FROM meta WHERE id=1", [], |r| r.get(0))
        .unwrap()
}

#[test]
fn missing_empty_and_tombstone_records_remain_distinct_without_a_write() {
    let (_dir, mut store, scope) = fresh();
    store
        .commit(
            &scope,
            &batch(vec![
                write("app", "empty", 0, Some("")),
                write("app", "deleted", 0, Some("old")),
            ]),
        )
        .unwrap();
    store
        .commit(&scope, &batch(vec![write("app", "deleted", 1, None)]))
        .unwrap();
    let before = revision(&store);
    let reader = grant(&store, &[("app", Access::Read)]);
    assert_eq!(
        store
            .get_many(
                &reader,
                &[
                    key("app", "absent"),
                    key("app", "empty"),
                    key("app", "deleted")
                ]
            )
            .unwrap(),
        [
            Record {
                revision: 0,
                value: None
            },
            Record {
                revision: 1,
                value: Some("".into())
            },
            Record {
                revision: 2,
                value: None
            }
        ]
    );
    assert_eq!(revision(&store), before);
}

#[test]
fn caller_order_and_distinct_namespaces_are_preserved() {
    let (_dir, mut store, scope) = fresh();
    store
        .commit(
            &scope,
            &batch(vec![
                write("app", "same", 0, Some("A é😀")),
                write("other", "same", 0, Some("B")),
            ]),
        )
        .unwrap();
    let rows = store
        .get_many(&scope, &[key("other", "same"), key("app", "same")])
        .unwrap();
    assert_eq!(rows[0], store.get(&scope, "other", "same").unwrap());
    assert_eq!(rows[1], store.get(&scope, "app", "same").unwrap());
}

#[test]
fn every_address_is_authorized_before_reading_a_healthy_prefix() {
    let (_dir, mut store, scope) = fresh();
    store
        .commit(
            &scope,
            &batch(vec![write("app", "one", 0, Some("healthy-prefix-canary"))]),
        )
        .unwrap();
    let reader = grant(&store, &[("app", Access::Read)]);
    assert_eq!(
        store
            .get_many_inner(
                &reader,
                &[key("app", "one"), key("other", "one")],
                || panic!("read prefix before refusing other scope")
            )
            .unwrap_err(),
        Error::Denied
    );
    let create_only = grant(&store, &[("app", Access::CreateOnly)]);
    assert_eq!(
        store
            .get_many(&create_only, &[key("app", "one")])
            .unwrap_err(),
        Error::Denied
    );
    let (_foreign_dir, _foreign_store, foreign) = fresh();
    assert_eq!(
        store.get_many(&foreign, &[key("app", "one")]).unwrap_err(),
        Error::Denied
    );
}

#[test]
fn malformed_empty_duplicate_and_overlong_sets_are_not_queries() {
    let (_dir, mut store, scope) = fresh();
    for keys in [
        vec![],
        vec![key("app", "one"); 4],
        vec![key("app", "one"); 2],
        vec![key("app", "")],
        vec![key("../app", "one")],
        vec![key("app", &"a".repeat(257))],
    ] {
        assert_eq!(
            store
                .get_many_inner(&scope, &keys, || panic!("invalid set reached data"))
                .unwrap_err(),
            Error::Invalid
        );
    }
    let reader = grant(&store, &[("app", Access::Read)]);
    assert_eq!(
        store
            .get_many(
                &reader,
                &[key("app", "one"), key("app", "two"), key("app", "three")]
            )
            .unwrap()
            .len(),
        READ_SET_ITEMS
    );
    assert_eq!(revision(&store), 0);
}

#[test]
fn combined_value_bound_counts_utf8_bytes_before_allocating_any_payload() {
    let (_dir, mut store, scope) = fresh();
    let half = "é".repeat(READ_SET_VALUE_BYTES / 4);
    assert_eq!(half.len() * 2, READ_SET_VALUE_BYTES);
    store
        .commit(
            &scope,
            &batch(vec![
                write("app", "one", 0, Some(&half)),
                write("app", "two", 0, Some(&half)),
                write("app", "extra", 0, Some("x")),
            ]),
        )
        .unwrap();
    let before = revision(&store);
    let exact = store
        .get_many(&scope, &[key("app", "one"), key("app", "two")])
        .unwrap();
    assert_eq!(
        exact
            .iter()
            .map(|r| r.value.as_ref().unwrap().len())
            .sum::<usize>(),
        READ_SET_VALUE_BYTES
    );
    assert_eq!(
        store
            .get_many_inner(
                &scope,
                &[key("app", "one"), key("app", "two"), key("app", "extra")],
                || panic!("over-limit prefix was allocated")
            )
            .unwrap_err(),
        Error::Limit
    );
    assert_eq!(revision(&store), before);
}

#[test]
fn corrupt_later_record_returns_no_partial_result_and_releases_the_read_transaction() {
    let (_dir, mut store, scope) = fresh();
    store
        .commit(
            &scope,
            &batch(vec![
                write("app", "one", 0, Some("healthy")),
                write("app", "two", 0, Some("corrupt")),
            ]),
        )
        .unwrap();
    store
        .connection
        .execute(
            "UPDATE records SET digest=zeroblob(32) WHERE namespace='app' AND key='two'",
            [],
        )
        .unwrap();
    assert_eq!(
        store
            .get_many(&scope, &[key("app", "one"), key("app", "two")])
            .unwrap_err(),
        Error::Corrupt
    );
    assert_eq!(
        store.get_many(&scope, &[key("app", "one")]).unwrap()[0]
            .value
            .as_deref(),
        Some("healthy")
    );
    assert_eq!(revision(&store), 1);
}

#[test]
fn poisoned_owner_remains_unusable_through_the_new_read_entrypoint() {
    let (_dir, mut store, scope) = fresh();
    store.poisoned = true;
    assert_eq!(
        store
            .get_many_inner(&scope, &[key("app", "one")], || panic!(
                "poisoned owner read data"
            ))
            .unwrap_err(),
        Error::ReopenRequired
    );
}

#[test]
fn coherent_snapshot_blocks_a_competing_commit_until_all_records_are_read() {
    let (dir, mut store, scope) = fresh();
    store
        .commit(
            &scope,
            &batch(vec![
                write("app", "one", 0, Some("old-one")),
                write("app", "two", 0, Some("old-two")),
            ]),
        )
        .unwrap();
    // This privileged raw connection is test scaffolding, not a second admitted
    // store owner. The existing DELETE journal must refuse this competing commit
    // until the read transaction ends. Do not switch journaling to make it pass.
    let observed = store.get_many_inner(&scope, &[key("app", "one"), key("app", "two")], || {
        let mut writer = Connection::open(dir.path().join("records.sqlite")).unwrap();
        writer.busy_timeout(Duration::ZERO).unwrap();
        let tx = writer.transaction_with_behavior(TransactionBehavior::Immediate).unwrap();
        for (name, value) in [("one", "new-one"), ("two", "new-two")] {
            let digest = record_digest("app", name, 2, Some(value));
            tx.execute("UPDATE records SET revision=2,value=?1,digest=?2 WHERE namespace='app' AND key=?3", params![value.as_bytes(), digest.as_slice(), name]).unwrap();
        }
        tx.execute("UPDATE meta SET revision=2 WHERE id=1", []).unwrap();
        assert_eq!(tx.commit().unwrap_err().sqlite_error_code(), Some(rusqlite::ErrorCode::DatabaseBusy));
    }).unwrap();
    assert_eq!(
        observed,
        [
            Record {
                revision: 1,
                value: Some("old-one".into())
            },
            Record {
                revision: 1,
                value: Some("old-two".into())
            }
        ]
    );
    assert_eq!(revision(&store), 1);
    store
        .commit(
            &scope,
            &batch(vec![
                write("app", "one", 1, Some("new-one")),
                write("app", "two", 1, Some("new-two")),
            ]),
        )
        .unwrap();
    let later = store
        .get_many(&scope, &[key("app", "one"), key("app", "two")])
        .unwrap();
    assert_eq!(
        later,
        [
            Record {
                revision: 2,
                value: Some("new-one".into())
            },
            Record {
                revision: 2,
                value: Some("new-two".into())
            }
        ]
    );
    assert_eq!(revision(&store), 2);
}
