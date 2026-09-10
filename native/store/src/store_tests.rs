use super::*;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::{PermissionsExt, symlink};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use tempfile::TempDir;

fn temp() -> TempDir {
    let dir = tempfile::tempdir().unwrap();
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
    dir
}

fn scope(store: &Store, grants: &[(&str, Access)]) -> Scope {
    store
        .scope(
            grants
                .iter()
                .map(|(name, access)| ((*name).to_owned(), *access))
                .collect(),
        )
        .unwrap()
}

fn fresh() -> (TempDir, Store, Scope) {
    let dir = temp();
    let store = Store::open(dir.path(), OpenMode::CreateNew, Limits::default()).unwrap();
    let grant = scope(
        &store,
        &[("app", Access::ReadWrite), ("intents", Access::ReadWrite)],
    );
    (dir, store, grant)
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

#[test]
fn metadata_preserves_native_presence_and_utf8_size_without_payload_or_writes() {
    let (dir, mut store, app) = fresh();
    let payload = "private-canary-é";
    store
        .commit(
            &app,
            &batch(vec![
                write("app", "a", 0, Some(payload)),
                write("app", "b", 0, Some("")),
                write("app", "c", 0, Some("deleted-canary")),
                write("intents", "a", 0, Some("other-namespace-canary")),
            ]),
        )
        .unwrap();
    store
        .commit(&app, &batch(vec![write("app", "c", 1, None)]))
        .unwrap();
    let reader = scope(&store, &[("app", Access::Read)]);
    let before: i64 = store
        .connection
        .query_row("SELECT revision FROM meta", [], |r| r.get(0))
        .unwrap();
    let page = store.metadata(&reader, "app", None, 3).unwrap();
    assert_eq!(
        serde_json::to_value(&page).unwrap(),
        serde_json::json!({
            "entries": [
                {"key":"a","revision":"1","present":true,"value_bytes":payload.len()},
                {"key":"b","revision":"1","present":true,"value_bytes":0},
                {"key":"c","revision":"2","present":false,"value_bytes":0}
            ], "next":"c"
        })
    );
    let encoded = serde_json::to_string(&page).unwrap();
    assert!(!encoded.contains("canary"));
    assert_eq!(
        store.keys(&reader, "app", None, 3).unwrap().keys,
        ["a", "b", "c"]
    );
    assert_eq!(
        store.get(&reader, "app", "a").unwrap().value.as_deref(),
        Some(payload)
    );
    assert_eq!(
        store
            .connection
            .query_row("SELECT revision FROM meta", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        before
    );
    assert!(
        store
            .metadata(&reader, "app", page.next.as_deref(), 3)
            .unwrap()
            .entries
            .is_empty()
    );
    drop(store);
    let reopened = Store::open(dir.path(), OpenMode::Existing, Limits::default()).unwrap();
    assert_eq!(
        reopened.metadata(&reader, "app", None, 3),
        Err(Error::Denied)
    );
    let current = scope(&reopened, &[("app", Access::Read)]);
    assert_eq!(reopened.metadata(&current, "app", None, 3).unwrap(), page);
}

#[test]
fn metadata_reuses_scope_cursor_limit_and_poison_checks() {
    let (_dir, mut store, app) = fresh();
    let creator = scope(&store, &[("app", Access::CreateOnly)]);
    let reader = scope(&store, &[("app", Access::Read)]);
    let (_other_dir, _other, foreign) = fresh();
    assert_eq!(store.metadata(&creator, "app", None, 1), Err(Error::Denied));
    assert_eq!(
        store.metadata(&reader, "intents", None, 1),
        Err(Error::Denied)
    );
    assert_eq!(store.metadata(&foreign, "app", None, 1), Err(Error::Denied));
    for limit in [0, 129, usize::MAX] {
        assert_eq!(
            store.metadata(&app, "app", None, limit),
            Err(Error::Invalid)
        );
    }
    for cursor in ["", "bad/key", "é", "x; SELECT", &"x".repeat(257)] {
        assert_eq!(
            store.metadata(&app, "app", Some(cursor), 1),
            Err(Error::Invalid)
        );
    }
    assert_eq!(
        store.metadata(&app, "bad/name", None, 1),
        Err(Error::Invalid)
    );
    store.poisoned = true;
    assert_eq!(
        store.metadata(&app, "app", None, 1),
        Err(Error::ReopenRequired)
    );
}

#[test]
fn metadata_serializes_the_exact_signed64_revision_range_as_strings() {
    for revision in [9_007_199_254_740_993_u64, i64::MAX as u64] {
        let (_dir, mut store, app) = fresh();
        store
            .commit(&app, &batch(vec![write("app", "a", 0, Some("value"))]))
            .unwrap();
        // Controlled native codec fixture, not a fabricated product admission or
        // a claim that this many commits occurred. Retain an actually valid digest.
        store
            .connection
            .execute(
                "UPDATE records SET revision=?1,digest=?2 WHERE namespace='app' AND key='a'",
                params![
                    revision as i64,
                    record_digest("app", "a", revision, Some("value"))
                ],
            )
            .unwrap();
        let page = store.metadata(&app, "app", None, 1).unwrap();
        assert_eq!(page.entries[0].revision, revision);
        assert_eq!(
            serde_json::to_value(page).unwrap()["entries"][0]["revision"],
            revision.to_string()
        );
    }
}

#[test]
fn metadata_maximum_page_keys_and_value_stay_bounded() {
    let (_dir, mut store, app) = fresh();
    let keys: Vec<_> = (0..129)
        .map(|i| format!("{i:03}{}", "x".repeat(253)))
        .collect();
    for chunk in keys.chunks(64) {
        store
            .commit(
                &app,
                &batch(
                    chunk
                        .iter()
                        .map(|key| write("app", key, 0, Some("value")))
                        .collect(),
                ),
            )
            .unwrap();
    }
    let maximum = "x".repeat(2 * 1024 * 1024);
    let updated = store
        .commit(
            &app,
            &batch(vec![write("app", &keys[0], 1, Some(&maximum))]),
        )
        .unwrap();
    let page = store.metadata(&app, "app", None, 128).unwrap();
    assert_eq!(page.entries.len(), 128);
    assert_eq!(page.entries[0].value_bytes, maximum.len());
    // Three earlier batches made the global receipt 4; the updated record's
    // own CAS revision is 2. Metadata must never substitute the global counter.
    assert_eq!(updated.revision, 4);
    assert_eq!(page.entries[0].revision, 2);
    assert!(page.entries[1..].iter().all(|entry| entry.revision == 1));
    assert_eq!(
        page.entries
            .iter()
            .map(|row| row.key.as_str())
            .collect::<Vec<_>>(),
        keys[..128].iter().map(String::as_str).collect::<Vec<_>>()
    );
    assert_eq!(page.next.as_ref(), Some(&keys[127]));
    assert!(serde_json::to_vec(&page).unwrap().len() < 65_536);
    let rest = store
        .metadata(&app, "app", page.next.as_deref(), 128)
        .unwrap();
    assert_eq!(rest.entries.len(), 1);
    assert_eq!(rest.entries[0].key, keys[128]);
    assert_eq!(rest.next, None);
}

#[test]
fn metadata_does_not_return_a_partial_page_after_corruption() {
    for sql in [
        "UPDATE records SET digest=zeroblob(32) WHERE key='b'",
        "UPDATE records SET revision=2 WHERE key='b'",
        "UPDATE records SET key='bad/key' WHERE key='b'",
        "UPDATE records SET key=hex(zeroblob(129)) WHERE key='b'",
    ] {
        let (_dir, mut store, app) = fresh();
        store
            .commit(
                &app,
                &batch(vec![
                    write("app", "a", 0, Some("first-canary")),
                    write("app", "b", 0, Some("second-canary")),
                ]),
            )
            .unwrap();
        store.connection.execute(sql, []).unwrap();
        assert_eq!(store.metadata(&app, "app", None, 128), Err(Error::Corrupt));
        assert_eq!(store.keys(&app, "app", None, 128), Err(Error::Corrupt));
    }
}

#[test]
fn metadata_is_a_live_scan_and_rescan_finds_insertions_before_the_cursor() {
    let (_dir, mut store, app) = fresh();
    store
        .commit(&app, &batch(vec![write("app", "b", 0, Some("initial"))]))
        .unwrap();
    let first = store.metadata(&app, "app", None, 1).unwrap();
    store
        .commit(
            &app,
            &batch(vec![
                write("app", "a", 0, Some("inserted")),
                write("app", "c", 0, Some("after")),
            ]),
        )
        .unwrap();
    let rest = store
        .metadata(&app, "app", first.next.as_deref(), 128)
        .unwrap();
    assert_eq!(
        rest.entries
            .iter()
            .map(|row| row.key.as_str())
            .collect::<Vec<_>>(),
        ["c"]
    );
    let refreshed = store.metadata(&app, "app", None, 128).unwrap();
    assert_eq!(
        refreshed
            .entries
            .iter()
            .map(|row| row.key.as_str())
            .collect::<Vec<_>>(),
        ["a", "b", "c"]
    );
}

#[test]
fn key_pages_are_lexical_scoped_bounded_and_include_tombstones() {
    let (_dir, mut store, app) = fresh();
    store
        .commit(
            &app,
            &batch(vec![
                write("app", "c", 0, Some("private c")),
                write("app", "a", 0, Some("private a")),
                write("app", "b", 0, Some("private b")),
                write("intents", "other", 0, Some("other namespace")),
            ]),
        )
        .unwrap();
    store
        .commit(&app, &batch(vec![write("app", "b", 1, None)]))
        .unwrap();
    let reader = scope(&store, &[("app", Access::Read)]);
    let first = store.keys(&reader, "app", None, 2).unwrap();
    assert_eq!(first.keys, ["a", "b"]);
    assert_eq!(first.next.as_deref(), Some("b"));
    let second = store
        .keys(&reader, "app", first.next.as_deref(), 2)
        .unwrap();
    assert_eq!(second.keys, ["c"]);
    assert_eq!(second.next, None);
    let whole = store.keys(&reader, "app", None, 3).unwrap();
    assert_eq!(whole.next.as_deref(), Some("c"));
    assert_eq!(
        store
            .keys(&reader, "app", whole.next.as_deref(), 3)
            .unwrap(),
        KeyPage {
            keys: vec![],
            next: None
        }
    );
    assert_eq!(store.get(&reader, "app", "b").unwrap().revision, 2);
    assert_eq!(
        store
            .connection
            .query_row("SELECT revision FROM meta", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        2
    );
}

#[test]
fn key_discovery_requires_read_authority_from_this_store_boot() {
    let (dir, store, app) = fresh();
    let creator = scope(&store, &[("app", Access::CreateOnly)]);
    let reader = scope(&store, &[("app", Access::Read)]);
    let (_other_dir, _other, foreign) = fresh();
    assert_eq!(store.keys(&creator, "app", None, 1), Err(Error::Denied));
    assert_eq!(store.keys(&reader, "intents", None, 1), Err(Error::Denied));
    assert_eq!(store.keys(&foreign, "app", None, 1), Err(Error::Denied));
    drop(store);
    let reopened = Store::open(dir.path(), OpenMode::Existing, Limits::default()).unwrap();
    assert_eq!(reopened.keys(&app, "app", None, 1), Err(Error::Denied));
}

#[test]
fn key_discovery_refuses_invalid_names_cursors_and_page_limits() {
    let (_dir, store, app) = fresh();
    for limit in [0, 129, usize::MAX] {
        assert_eq!(store.keys(&app, "app", None, limit), Err(Error::Invalid));
    }
    for cursor in ["", "bad/key", "é", "x; SELECT", &"x".repeat(257)] {
        assert_eq!(
            store.keys(&app, "app", Some(cursor), 1),
            Err(Error::Invalid)
        );
    }
    assert_eq!(store.keys(&app, "bad/name", None, 1), Err(Error::Invalid));
}

#[test]
fn key_discovery_refuses_corrupt_records_and_poisoned_handles() {
    let (_dir, mut store, app) = fresh();
    store
        .commit(&app, &batch(vec![write("app", "a", 0, Some("value"))]))
        .unwrap();
    store
        .connection
        .execute("UPDATE records SET digest=zeroblob(32)", [])
        .unwrap();
    assert_eq!(store.keys(&app, "app", None, 128), Err(Error::Corrupt));
    store.poisoned = true;
    assert_eq!(store.keys(&app, "app", None, 1), Err(Error::ReopenRequired));
}

#[test]
fn key_discovery_checks_key_size_before_exposing_corrupt_storage() {
    for key in ["bad/key".to_owned(), "x".repeat(257), "é".repeat(256)] {
        let (_dir, mut store, app) = fresh();
        store
            .commit(&app, &batch(vec![write("app", "a", 0, Some("value"))]))
            .unwrap();
        store
            .connection
            .execute("UPDATE records SET key=?1", params![key])
            .unwrap();
        assert_eq!(store.keys(&app, "app", None, 1), Err(Error::Corrupt));
    }
}

#[test]
fn key_cursor_is_not_a_snapshot_and_rescan_finds_new_earlier_work_after_restart() {
    let (dir, mut store, app) = fresh();
    store
        .commit(&app, &batch(vec![write("app", "b", 0, Some("old"))]))
        .unwrap();
    let first = store.keys(&app, "app", None, 1).unwrap();
    store
        .commit(&app, &batch(vec![write("app", "a", 0, Some("new"))]))
        .unwrap();
    assert!(
        store
            .keys(&app, "app", first.next.as_deref(), 1)
            .unwrap()
            .keys
            .is_empty()
    );
    drop(store);
    let store = Store::open(dir.path(), OpenMode::Existing, Limits::default()).unwrap();
    let reader = scope(&store, &[("app", Access::Read)]);
    assert_eq!(
        store.keys(&reader, "app", None, 128).unwrap().keys,
        ["a", "b"]
    );
}

#[test]
fn key_discovery_enforces_the_maximum_page_and_preserves_maximum_length_keys() {
    let (_dir, mut store, app) = fresh();
    for start in [0, 64, 128] {
        let writes = (start..start + 64)
            .map(|i| write("app", &format!("k{i:03}"), 0, Some("value")))
            .collect();
        store.commit(&app, &batch(writes)).unwrap();
    }
    let long_key = "z".repeat(256);
    store
        .commit(
            &app,
            &batch(vec![write("app", &long_key, 0, Some("value"))]),
        )
        .unwrap();
    let first = store.keys(&app, "app", None, 128).unwrap();
    assert_eq!(first.keys.len(), 128);
    assert_eq!(first.keys.first().unwrap(), "k000");
    assert_eq!(first.next.as_deref(), Some("k127"));
    let second = store.keys(&app, "app", first.next.as_deref(), 128).unwrap();
    assert_eq!(second.keys.len(), 65);
    assert_eq!(second.keys.last().unwrap(), &long_key);
    assert!(second.next.is_none());
}

#[test]
fn create_slot_probe_is_scoped_nonmutating_and_does_not_expose_records() {
    let (_dir, mut store, app) = fresh();
    let creator = scope(&store, &[("app", Access::CreateOnly)]);
    let reader = scope(&store, &[("app", Access::Read)]);
    assert_eq!(store.unused_create_slot(&creator, "app", "new"), Ok(true));
    assert_eq!(store.get(&creator, "app", "new"), Err(Error::Denied));
    assert_eq!(store.get(&app, "app", "new").unwrap().revision, 0);
    assert_eq!(
        store.unused_create_slot(&creator, "intents", "new"),
        Err(Error::Denied)
    );
    assert_eq!(
        store.unused_create_slot(&reader, "app", "new"),
        Err(Error::Denied)
    );
    assert_eq!(
        store.unused_create_slot(&creator, "app", "bad/key"),
        Err(Error::Invalid)
    );
    store
        .commit(
            &app,
            &batch(vec![write("app", "new", 0, Some("private bytes"))]),
        )
        .unwrap();
    assert_eq!(store.unused_create_slot(&creator, "app", "new"), Ok(false));
    assert_eq!(store.get(&creator, "app", "new"), Err(Error::Denied));
    store
        .commit(&app, &batch(vec![write("app", "new", 1, None)]))
        .unwrap();
    assert_eq!(store.unused_create_slot(&creator, "app", "new"), Ok(false));
}

#[test]
fn create_slot_probe_refuses_foreign_scope_and_corruption() {
    let (_dir, mut store, app) = fresh();
    let (_foreign_dir, _foreign, foreign_scope) = fresh();
    assert_eq!(
        store.unused_create_slot(&foreign_scope, "app", "new"),
        Err(Error::Denied)
    );
    store
        .commit(&app, &batch(vec![write("app", "new", 0, Some("value"))]))
        .unwrap();
    store
        .connection
        .execute(
            "UPDATE records SET digest=zeroblob(32) WHERE namespace='app'",
            [],
        )
        .unwrap();
    assert_eq!(
        store.unused_create_slot(&app, "app", "new"),
        Err(Error::Corrupt)
    );
}

#[test]
fn atomic_opaque_records_survive_reopen() {
    let (dir, mut store, app) = fresh();
    let data = "\0not SQL: '); DROP TABLE records; -- 😀";
    assert_eq!(
        store.commit(
            &app,
            &batch(vec![
                write("app", "same", 0, Some(data)),
                write("intents", "op:1", 0, Some("effect"))
            ])
        ),
        Ok(Receipt { revision: 1 })
    );
    drop(store);
    let reopened = Store::open(dir.path(), OpenMode::Existing, Limits::default()).unwrap();
    let reader = scope(
        &reopened,
        &[("app", Access::Read), ("intents", Access::Read)],
    );
    assert_eq!(
        reopened
            .get(&reader, "app", "same")
            .unwrap()
            .value
            .as_deref(),
        Some(data)
    );
    assert_eq!(
        reopened
            .get(&reader, "intents", "op:1")
            .unwrap()
            .value
            .as_deref(),
        Some("effect")
    );
}

#[test]
fn stale_batch_changes_no_record_and_does_not_poison_store() {
    let (_dir, mut store, app) = fresh();
    store
        .commit(&app, &batch(vec![write("app", "same", 0, Some("old"))]))
        .unwrap();
    assert_eq!(
        store.commit(
            &app,
            &batch(vec![
                write("intents", "op:1", 0, Some("new")),
                write("app", "same", 0, Some("wrong"))
            ])
        ),
        Err(Error::Conflict)
    );
    assert_eq!(store.get(&app, "intents", "op:1").unwrap().revision, 0);
    assert_eq!(
        store.get(&app, "app", "same").unwrap().value.as_deref(),
        Some("old")
    );
    store
        .commit(&app, &batch(vec![write("app", "same", 1, Some("next"))]))
        .unwrap();
}

#[test]
fn read_precondition_is_atomic_with_writes() {
    let (_dir, mut store, app) = fresh();
    let mut proposed = batch(vec![write("intents", "op:1", 0, Some("intent"))]);
    proposed.checks.push(Check {
        namespace: "app".into(),
        key: "same".into(),
        revision: 0,
    });
    store
        .commit(&app, &batch(vec![write("app", "same", 0, Some("changed"))]))
        .unwrap();
    assert_eq!(store.commit(&app, &proposed), Err(Error::Conflict));
    assert_eq!(store.get(&app, "intents", "op:1").unwrap().revision, 0);
}

#[test]
fn executor_scope_cannot_read_or_write_application_state() {
    let (_dir, mut store, app) = fresh();
    store
        .commit(&app, &batch(vec![write("app", "same", 0, Some("private"))]))
        .unwrap();
    let worker = scope(
        &store,
        &[
            ("intents", Access::Read),
            ("deliveries", Access::CreateOnly),
        ],
    );
    assert_eq!(store.get(&worker, "app", "same"), Err(Error::Denied));
    assert_eq!(
        store.commit(
            &worker,
            &batch(vec![write("app", "same", 1, Some("overwrite"))])
        ),
        Err(Error::Denied)
    );
    store
        .commit(
            &worker,
            &batch(vec![write("deliveries", "op:1", 0, Some("observed"))]),
        )
        .unwrap();
    assert_eq!(store.get(&worker, "deliveries", "op:1"), Err(Error::Denied));
}

#[test]
fn tenants_with_identical_keys_do_not_share_records() {
    let (_dir, mut store, _) = fresh();
    let a = scope(&store, &[("tenant-a", Access::ReadWrite)]);
    let b = scope(&store, &[("tenant-b", Access::ReadWrite)]);
    store
        .commit(&a, &batch(vec![write("tenant-a", "same", 0, Some("A"))]))
        .unwrap();
    store
        .commit(&b, &batch(vec![write("tenant-b", "same", 0, Some("B"))]))
        .unwrap();
    assert_eq!(store.get(&a, "tenant-b", "same"), Err(Error::Denied));
    assert_eq!(store.get(&b, "tenant-a", "same"), Err(Error::Denied));
    assert_eq!(
        store.get(&a, "tenant-a", "same").unwrap().value.as_deref(),
        Some("A")
    );
    assert_eq!(
        store.get(&b, "tenant-b", "same").unwrap().value.as_deref(),
        Some("B")
    );
}

#[test]
fn read_and_create_only_rights_cannot_be_widened_by_a_batch() {
    let (_dir, mut store, _) = fresh();
    let reader = scope(&store, &[("app", Access::Read)]);
    assert_eq!(
        store.commit(&reader, &batch(vec![write("app", "same", 0, Some("x"))])),
        Err(Error::Denied)
    );
    let creator = scope(&store, &[("app", Access::CreateOnly)]);
    store
        .commit(
            &creator,
            &batch(vec![write("app", "same", 0, Some("immutable"))]),
        )
        .unwrap();
    assert_eq!(
        store.commit(
            &creator,
            &batch(vec![write("app", "same", 0, Some("replay"))])
        ),
        Err(Error::Conflict)
    );
    assert_eq!(
        store.commit(
            &creator,
            &batch(vec![write("app", "same", 1, Some("change"))])
        ),
        Err(Error::Denied)
    );
    assert_eq!(
        store.commit(&creator, &batch(vec![write("app", "same", 0, None)])),
        Err(Error::Denied)
    );
}

#[test]
fn tombstone_retains_revision_and_prevents_aba() {
    let (_dir, mut store, app) = fresh();
    store
        .commit(&app, &batch(vec![write("app", "same", 0, Some("first"))]))
        .unwrap();
    store
        .commit(&app, &batch(vec![write("app", "same", 1, None)]))
        .unwrap();
    assert_eq!(
        store.get(&app, "app", "same").unwrap(),
        Record {
            revision: 2,
            value: None
        }
    );
    assert_eq!(
        store.commit(&app, &batch(vec![write("app", "same", 0, Some("new"))])),
        Err(Error::Conflict)
    );
    store
        .commit(
            &app,
            &batch(vec![write("app", "same", 2, Some("explicit"))]),
        )
        .unwrap();
    assert_eq!(store.get(&app, "app", "same").unwrap().revision, 3);
}

#[test]
fn scope_is_bound_to_boot_and_store_not_just_grant_names() {
    let (dir, store, old) = fresh();
    drop(store);
    let reopened = Store::open(dir.path(), OpenMode::Existing, Limits::default()).unwrap();
    assert_eq!(reopened.get(&old, "app", "same"), Err(Error::Denied));
    let (_other, other, _) = fresh();
    assert_eq!(other.get(&old, "app", "same"), Err(Error::Denied));
}

#[test]
fn second_owner_is_rejected_including_a_path_alias() {
    let (dir, _store, _) = fresh();
    assert_eq!(
        Store::open(dir.path(), OpenMode::Existing, Limits::default()).err(),
        Some(Error::Busy)
    );
    assert_eq!(
        Store::open(&dir.path().join("."), OpenMode::Existing, Limits::default()).err(),
        Some(Error::Busy)
    );
}

#[test]
fn closing_store_unlocks_even_with_a_transient_inherited_descriptor() {
    let (dir, store, _) = fresh();
    // Models the descriptor copy between fork and exec in another thread.
    let inherited = store.owner.try_clone().unwrap();
    drop(store);
    let reopened = Store::open(dir.path(), OpenMode::Existing, Limits::default()).unwrap();
    // Closing the stale copy must not release the replacement owner's lock.
    drop(inherited);
    assert_eq!(
        Store::open(dir.path(), OpenMode::Existing, Limits::default()).err(),
        Some(Error::Busy)
    );
    drop(reopened);
}

#[test]
fn non_owner_process_copy_cannot_unlock_parent_or_use_store() {
    let (dir, mut store, app) = fresh();
    let owner_process = store.owner.process;
    let foreign_process = owner_process.wrapping_add(1);
    let copied = OwnedLock {
        file: store.owner.try_clone().unwrap(),
        process: foreign_process,
    };
    drop(copied);
    assert_eq!(
        Store::open(dir.path(), OpenMode::Existing, Limits::default()).err(),
        Some(Error::Busy)
    );
    store.owner.process = foreign_process;
    assert_eq!(store.get(&app, "app", "same"), Err(Error::Denied));
    assert!(matches!(
        store.scope(BTreeMap::from([("app".into(), Access::Read)])),
        Err(Error::Denied)
    ));
    store.owner.process = owner_process;
}

#[test]
fn missing_state_is_not_implicitly_initialized() {
    let dir = temp();
    assert_eq!(
        Store::open(dir.path(), OpenMode::Existing, Limits::default()).err(),
        Some(Error::Missing)
    );
    assert!(!dir.path().join("records.sqlite").exists());
}

#[test]
fn repeated_initialization_cannot_overwrite_records() {
    let (dir, mut store, app) = fresh();
    store
        .commit(
            &app,
            &batch(vec![write("app", "same", 0, Some("preserved"))]),
        )
        .unwrap();
    drop(store);
    assert_eq!(
        Store::open(dir.path(), OpenMode::CreateNew, Limits::default()).err(),
        Some(Error::AlreadyExists)
    );
    let reopened = Store::open(dir.path(), OpenMode::Existing, Limits::default()).unwrap();
    assert_eq!(
        reopened
            .get(&scope(&reopened, &[("app", Access::Read)]), "app", "same")
            .unwrap()
            .value
            .as_deref(),
        Some("preserved")
    );
}

#[test]
fn unsafe_roots_and_symlinks_are_refused() {
    let dir = temp();
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o755)).unwrap();
    assert_eq!(
        Store::open(dir.path(), OpenMode::CreateNew, Limits::default()).err(),
        Some(Error::Denied)
    );
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let links = temp();
    symlink(dir.path(), links.path().join("alias")).unwrap();
    assert_eq!(
        Store::open(
            &links.path().join("alias"),
            OpenMode::CreateNew,
            Limits::default()
        )
        .err(),
        Some(Error::Denied)
    );
    symlink(links.path().join("missing"), dir.path().join("owner.lock")).unwrap();
    assert_eq!(
        Store::open(dir.path(), OpenMode::CreateNew, Limits::default()).err(),
        Some(Error::Denied)
    );
}

#[test]
fn hardlinked_database_is_refused() {
    let (dir, store, _) = fresh();
    drop(store);
    fs::hard_link(
        dir.path().join("records.sqlite"),
        dir.path().join("alias.sqlite"),
    )
    .unwrap();
    assert_eq!(
        Store::open(dir.path(), OpenMode::Existing, Limits::default()).err(),
        Some(Error::Denied)
    );
}

#[test]
fn replacing_owner_lock_invalidates_live_handle() {
    let (dir, store, app) = fresh();
    fs::rename(dir.path().join("owner.lock"), dir.path().join("old.lock")).unwrap();
    OpenOptions::new()
        .create_new(true)
        .write(true)
        .mode(0o600)
        .open(dir.path().join("owner.lock"))
        .unwrap();
    assert_eq!(store.get(&app, "app", "same"), Err(Error::Denied));
}

#[test]
fn corrupt_record_and_unknown_schema_fail_closed() {
    let (dir, mut store, app) = fresh();
    store
        .commit(&app, &batch(vec![write("app", "same", 0, Some("good"))]))
        .unwrap();
    store
        .connection
        .execute("UPDATE records SET value=x'626164'", [])
        .unwrap();
    assert_eq!(store.get(&app, "app", "same"), Err(Error::Corrupt));
    drop(store);
    assert_eq!(
        Store::open(dir.path(), OpenMode::Existing, Limits::default()).err(),
        Some(Error::Corrupt)
    );
    let (other, store, _) = fresh();
    store
        .connection
        .pragma_update(None, "user_version", 99)
        .unwrap();
    drop(store);
    assert_eq!(
        Store::open(other.path(), OpenMode::Existing, Limits::default()).err(),
        Some(Error::Corrupt)
    );
}

#[test]
fn bootstrap_limits_cannot_silently_change_on_restart() {
    let (dir, store, _) = fresh();
    drop(store);
    let changed = Limits {
        records: 10,
        ..Limits::default()
    };
    assert_eq!(
        Store::open(dir.path(), OpenMode::Existing, changed).err(),
        Some(Error::Invalid)
    );
}

#[test]
fn record_digest_binds_namespace_key_and_revision() {
    for sql in [
        "UPDATE records SET namespace='intents'",
        "UPDATE records SET key='other'",
        "UPDATE records SET revision=2",
    ] {
        let (dir, mut store, app) = fresh();
        store
            .commit(&app, &batch(vec![write("app", "same", 0, Some("private"))]))
            .unwrap();
        store.connection.execute(sql, []).unwrap();
        drop(store);
        assert_eq!(
            Store::open(dir.path(), OpenMode::Existing, Limits::default()).err(),
            Some(Error::Corrupt)
        );
    }
}

#[test]
fn wrong_application_header_is_refused_without_rewriting_database() {
    let (dir, store, _) = fresh();
    store
        .connection
        .pragma_update(None, "application_id", 99)
        .unwrap();
    drop(store);
    let path = dir.path().join("records.sqlite");
    let before = fs::read(&path).unwrap();
    assert_eq!(
        Store::open(dir.path(), OpenMode::Existing, Limits::default()).err(),
        Some(Error::Corrupt)
    );
    assert_eq!(fs::read(&path).unwrap(), before);
}

#[test]
fn invalid_names_and_duplicate_read_write_targets_are_refused() {
    let (_dir, mut store, app) = fresh();
    assert_eq!(store.get(&app, "../app", "same"), Err(Error::Invalid));
    assert_eq!(store.get(&app, "app", "x\0y"), Err(Error::Invalid));
    let mut proposed = batch(vec![write("app", "same", 0, Some("x"))]);
    proposed.checks.push(Check {
        namespace: "app".into(),
        key: "same".into(),
        revision: 0,
    });
    assert_eq!(store.commit(&app, &proposed), Err(Error::Invalid));
    assert_eq!(store.get(&app, "app", "same").unwrap().revision, 0);
}

#[test]
fn oversized_value_and_duplicate_keys_cannot_partially_commit() {
    let (_dir, mut store, app) = fresh();
    let huge = "x".repeat(store.limits.value_bytes + 1);
    assert_eq!(
        store.commit(
            &app,
            &batch(vec![
                write("intents", "op:1", 0, Some("first")),
                write("app", "same", 0, Some(&huge))
            ])
        ),
        Err(Error::Limit)
    );
    assert_eq!(store.get(&app, "intents", "op:1").unwrap().revision, 0);
    assert_eq!(
        store.commit(
            &app,
            &batch(vec![
                write("app", "same", 0, Some("a")),
                write("app", "same", 0, Some("b"))
            ])
        ),
        Err(Error::Invalid)
    );
}

#[test]
fn record_count_ceiling_rolls_back_whole_batch() {
    let dir = temp();
    let mut store = Store::open(
        dir.path(),
        OpenMode::CreateNew,
        Limits {
            records: 1,
            ..Limits::default()
        },
    )
    .unwrap();
    let app = scope(&store, &[("app", Access::ReadWrite)]);
    assert_eq!(
        store.commit(
            &app,
            &batch(vec![
                write("app", "a", 0, Some("a")),
                write("app", "b", 0, Some("b"))
            ])
        ),
        Err(Error::Limit)
    );
    assert_eq!(store.get(&app, "app", "a").unwrap().revision, 0);
    store
        .commit(&app, &batch(vec![write("app", "a", 0, Some("valid"))]))
        .unwrap();
}

#[test]
fn sqlite_full_requires_reopen_and_does_not_publish_partial_records() {
    let dir = temp();
    let limits = Limits {
        database_pages: 16,
        ..Limits::default()
    };
    let mut store = Store::open(dir.path(), OpenMode::CreateNew, limits.clone()).unwrap();
    let app = scope(&store, &[("app", Access::ReadWrite)]);
    let payload = "x".repeat(256 * 1024);
    assert_eq!(
        store.commit(
            &app,
            &batch(vec![
                write("app", "a", 0, Some("first")),
                write("app", "b", 0, Some(&payload))
            ])
        ),
        Err(Error::Storage)
    );
    assert_eq!(store.get(&app, "app", "a"), Err(Error::ReopenRequired));
    drop(store);
    let reopened = Store::open(dir.path(), OpenMode::Existing, limits).unwrap();
    assert_eq!(
        reopened
            .get(&scope(&reopened, &[("app", Access::Read)]), "app", "a")
            .unwrap()
            .revision,
        0
    );
}

struct ChildOwner(Child);
impl Drop for ChildOwner {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn kill_at_boundary(root: &Path, phase: &str) {
    let mut child = ChildOwner(
        Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "store::tests::process_child_entrypoint",
                "--nocapture",
            ])
            .env("SIGIL_STORE_TEST_ROOT", root)
            .env("SIGIL_STORE_TEST_PHASE", phase)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap(),
    );
    let output = child.0.stdout.take().unwrap();
    let (sender, receiver) = mpsc::channel();
    let reader = std::thread::spawn(move || {
        for line in BufReader::new(output)
            .lines()
            .map_while(std::result::Result::ok)
        {
            if line.contains("FAULT_BOUNDARY_REACHED") {
                let _ = sender.send(());
                break;
            }
        }
    });
    receiver
        .recv_timeout(Duration::from_secs(10))
        .expect("child did not reach the actual transaction boundary");
    child.0.kill().unwrap();
    assert!(!child.0.wait().unwrap().success());
    reader.join().unwrap();
}

#[test]
fn process_child_entrypoint() {
    // Child-process fixture. It performs no work in the ordinary test invocation.
    let Ok(root) = std::env::var("SIGIL_STORE_TEST_ROOT") else {
        return;
    };
    let phase = std::env::var("SIGIL_STORE_TEST_PHASE").unwrap();
    let mut store = Store::open(Path::new(&root), OpenMode::Existing, Limits::default()).unwrap();
    let app = scope(
        &store,
        &[("app", Access::ReadWrite), ("intents", Access::ReadWrite)],
    );
    let proposed = batch(vec![
        write("app", "same", 1, Some("new")),
        write("intents", "op:1", 0, Some("intent")),
    ]);
    let park = || {
        println!("FAULT_BOUNDARY_REACHED");
        std::io::stdout().flush().unwrap();
        loop {
            std::thread::park();
        }
    };
    if phase == "before_commit" {
        store.commit_inner(&app, &proposed, park).unwrap();
    } else if phase == "after_commit" {
        store.commit(&app, &proposed).unwrap();
        park();
    } else {
        panic!("unknown test phase");
    }
}

#[test]
fn kill_before_commit_recovers_old_state_and_no_intent() {
    let (dir, mut store, app) = fresh();
    store
        .commit(&app, &batch(vec![write("app", "same", 0, Some("old"))]))
        .unwrap();
    drop(store);
    kill_at_boundary(dir.path(), "before_commit");
    let reopened = Store::open(dir.path(), OpenMode::Existing, Limits::default()).unwrap();
    let app = scope(
        &reopened,
        &[("app", Access::Read), ("intents", Access::Read)],
    );
    assert_eq!(
        reopened.get(&app, "app", "same").unwrap().value.as_deref(),
        Some("old")
    );
    assert_eq!(reopened.get(&app, "intents", "op:1").unwrap().revision, 0);
}

#[test]
fn kill_after_commit_before_reply_recovers_entire_batch() {
    let (dir, mut store, app) = fresh();
    store
        .commit(&app, &batch(vec![write("app", "same", 0, Some("old"))]))
        .unwrap();
    drop(store);
    kill_at_boundary(dir.path(), "after_commit");
    let reopened = Store::open(dir.path(), OpenMode::Existing, Limits::default()).unwrap();
    let app = scope(
        &reopened,
        &[("app", Access::Read), ("intents", Access::Read)],
    );
    assert_eq!(
        reopened.get(&app, "app", "same").unwrap().value.as_deref(),
        Some("new")
    );
    assert_eq!(
        reopened
            .get(&app, "intents", "op:1")
            .unwrap()
            .value
            .as_deref(),
        Some("intent")
    );
}
