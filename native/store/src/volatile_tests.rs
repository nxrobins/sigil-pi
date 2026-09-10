use super::*;
use crate::store::{Access, Batch, Limits, OpenMode};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::sync::{Arc, Barrier, Mutex};
use tempfile::TempDir;

#[test]
fn exhausting_one_namespace_cannot_take_anothers_reserved_records_or_bytes() {
    let (_dir, store, scope, mut cells) = fresh(VolatileLimits {
        value_bytes: 4,
        records: 1,
        live_bytes: 4,
    });
    let other = grant(&store, "other", Access::ReadWrite);
    cells
        .compare_set(&store, &scope, &write("app", "one", 0, Some("full")))
        .unwrap();
    let before = snapshot(&cells);
    assert_eq!(
        cells.compare_set(&store, &scope, &write("app", "two", 0, Some("x"))),
        Err(Error::Limit)
    );
    assert_eq!(snapshot(&cells), before);
    cells
        .compare_set(&store, &other, &write("other", "one", 0, Some("full")))
        .unwrap();
    assert_eq!(
        cells
            .get(&store, &other, "other", "one")
            .unwrap()
            .value
            .as_deref(),
        Some("full")
    );
    assert_eq!(cells.live_bytes, 8);
    snapshot(&cells);
}

#[test]
fn a_valid_held_grant_does_not_add_an_undeclared_temporary_namespace() {
    let (_dir, store, _scope, mut cells) = fresh(limits());
    let undeclared = grant(&store, "undeclared", Access::ReadWrite);
    let before = snapshot(&cells);
    assert_eq!(
        cells.get(&store, &undeclared, "undeclared", "key"),
        Err(Error::Denied)
    );
    assert_eq!(
        cells.compare_set(
            &store,
            &undeclared,
            &write("undeclared", "key", 0, Some("value"))
        ),
        Err(Error::Denied)
    );
    assert_eq!(snapshot(&cells), before);
}

#[test]
fn namespace_catalog_and_total_reservations_are_validated_at_construction() {
    let (_dir, store, _scope) = owner(Limits::default());
    for names in [vec![], vec!["app", "app"], vec![""], vec!["bad/name"]] {
        assert!(matches!(
            Volatile::new(&store, limits(), &names),
            Err(Error::Invalid)
        ));
    }
    let too_many: Vec<String> = (0..65).map(|n| format!("ns{n}")).collect();
    let names: Vec<&str> = too_many.iter().map(String::as_str).collect();
    assert!(matches!(
        Volatile::new(&store, limits(), &names),
        Err(Error::Invalid)
    ));
    assert!(matches!(
        Volatile::new(
            &store,
            VolatileLimits {
                records: 33,
                ..limits()
            },
            &["app", "other"]
        ),
        Err(Error::Invalid)
    ));
    assert!(matches!(
        Volatile::new(
            &store,
            VolatileLimits {
                live_bytes: LIVE_BYTES,
                ..limits()
            },
            &["app", "other"]
        ),
        Err(Error::Invalid)
    ));
    let exact = VolatileLimits {
        value_bytes: VALUE_BYTES,
        records: 32,
        live_bytes: LIVE_BYTES / 2,
    };
    assert!(Volatile::new(&store, exact, &["app", "other"]).is_ok());
    let (_small_dir, small, _scope) = owner(Limits {
        records: 1,
        live_bytes: 32,
        value_bytes: 32,
        ..Limits::default()
    });
    assert!(matches!(
        Volatile::new(
            &small,
            VolatileLimits {
                value_bytes: 8,
                records: 1,
                live_bytes: 16
            },
            &["app", "other"]
        ),
        Err(Error::Invalid)
    ));
    let (_small_dir, small, _scope) = owner(Limits {
        records: 2,
        live_bytes: 32,
        value_bytes: 32,
        ..Limits::default()
    });
    assert!(matches!(
        Volatile::new(
            &small,
            VolatileLimits {
                value_bytes: 8,
                records: 1,
                live_bytes: 17
            },
            &["app", "other"]
        ),
        Err(Error::Invalid)
    ));
}

fn limits() -> VolatileLimits {
    VolatileLimits {
        value_bytes: 32,
        records: 3,
        live_bytes: 48,
    }
}

fn grant(store: &Store, namespace: &str, access: Access) -> Scope {
    store.scope([(namespace.into(), access)].into()).unwrap()
}

fn owner(limits: Limits) -> (TempDir, Store, Scope) {
    let dir = tempfile::tempdir().unwrap();
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let store = Store::open(dir.path(), OpenMode::CreateNew, limits).unwrap();
    let scope = grant(&store, "app", Access::ReadWrite);
    (dir, store, scope)
}

fn fresh(limits: VolatileLimits) -> (TempDir, Store, Scope, Volatile) {
    let (dir, store, scope) = owner(Limits::default());
    let cells = Volatile::new(&store, limits, &["app", "other"]).unwrap();
    (dir, store, scope, cells)
}

fn write(namespace: &str, key: &str, revision: u64, value: Option<&str>) -> Mutation {
    Mutation {
        namespace: namespace.into(),
        key: key.into(),
        revision,
        value: value.map(str::to_owned),
    }
}

fn snapshot(cells: &Volatile) -> (u64, usize, BTreeMap<(String, String), Record>) {
    let mut expected: BTreeMap<String, Usage> = cells
        .namespaces
        .keys()
        .map(|name| (name.clone(), Usage::default()))
        .collect();
    for ((namespace, _), record) in &cells.cells {
        let usage = expected
            .get_mut(namespace)
            .expect("undeclared namespace retained");
        usage.records += 1;
        usage.live_bytes += record.value.as_ref().map_or(0, String::len);
    }
    assert_eq!(cells.namespaces, expected);
    assert_eq!(
        cells.live_bytes,
        expected
            .values()
            .map(|usage| usage.live_bytes)
            .sum::<usize>()
    );
    (cells.revision, cells.live_bytes, cells.cells.clone())
}

#[test]
fn missing_empty_and_tombstone_are_distinct_and_deletion_cannot_reset_revision() {
    let (_dir, store, scope, mut cells) = fresh(limits());
    assert_eq!(
        cells.get(&store, &scope, "app", "key").unwrap(),
        Record {
            revision: 0,
            value: None
        }
    );
    assert_eq!(snapshot(&cells), (0, 0, BTreeMap::new()));
    assert_eq!(
        cells
            .compare_set(&store, &scope, &write("app", "key", 0, Some("")))
            .unwrap()
            .revision,
        1
    );
    assert_eq!(
        cells.get(&store, &scope, "app", "key").unwrap(),
        Record {
            revision: 1,
            value: Some("".into())
        }
    );
    assert_eq!(
        cells
            .compare_set(&store, &scope, &write("app", "key", 1, None))
            .unwrap()
            .revision,
        2
    );
    assert_eq!(
        cells.get(&store, &scope, "app", "key").unwrap(),
        Record {
            revision: 2,
            value: None
        }
    );
    let before = snapshot(&cells);
    assert_eq!(
        cells.compare_set(&store, &scope, &write("app", "key", 0, Some("recreate"))),
        Err(Error::Conflict)
    );
    assert_eq!(snapshot(&cells), before);
}

#[test]
fn values_are_opaque_utf8_bytes_and_successful_noop_still_advances_revision() {
    let (_dir, store, scope, mut cells) = fresh(limits());
    let value = "é😀\0\n";
    assert_eq!(
        cells
            .compare_set(&store, &scope, &write("app", "key", 0, Some(value)))
            .unwrap()
            .revision,
        1
    );
    assert_eq!(
        cells
            .get(&store, &scope, "app", "key")
            .unwrap()
            .value
            .as_deref(),
        Some(value)
    );
    assert_eq!(cells.live_bytes, value.len());
    assert_eq!(
        cells
            .compare_set(&store, &scope, &write("app", "key", 1, Some(value)))
            .unwrap()
            .revision,
        2
    );
    assert_eq!(cells.live_bytes, value.len());
}

#[test]
fn stale_and_future_expected_revisions_never_change_any_cell_or_accounting() {
    let (_dir, store, scope, mut cells) = fresh(limits());
    cells
        .compare_set(&store, &scope, &write("app", "key", 0, Some("first")))
        .unwrap();
    for expected in [0, 2, i64::MAX as u64] {
        let before = snapshot(&cells);
        assert_eq!(
            cells.compare_set(
                &store,
                &scope,
                &write("app", "key", expected, Some("later"))
            ),
            Err(Error::Conflict)
        );
        assert_eq!(snapshot(&cells), before);
    }
}

#[test]
fn disjoint_namespaces_keep_identical_keys_separate_and_denials_do_not_reveal_values() {
    let (_dir, store, scope, mut cells) = fresh(limits());
    let other = grant(&store, "other", Access::ReadWrite);
    cells
        .compare_set(&store, &scope, &write("app", "same", 0, Some("private-a")))
        .unwrap();
    cells
        .compare_set(
            &store,
            &other,
            &write("other", "same", 0, Some("private-b")),
        )
        .unwrap();
    assert_eq!(
        cells
            .get(&store, &scope, "app", "same")
            .unwrap()
            .value
            .as_deref(),
        Some("private-a")
    );
    assert_eq!(
        cells
            .get(&store, &other, "other", "same")
            .unwrap()
            .value
            .as_deref(),
        Some("private-b")
    );
    let before = snapshot(&cells);
    assert_eq!(
        cells.get(&store, &scope, "other", "same"),
        Err(Error::Denied)
    );
    assert_eq!(
        cells.compare_set(&store, &other, &write("app", "same", 1, Some("forged"))),
        Err(Error::Denied)
    );
    assert_eq!(snapshot(&cells), before);
}

#[test]
fn existing_read_and_create_only_grant_rules_are_preserved() {
    let (_dir, store, scope, mut cells) = fresh(limits());
    let creator = grant(&store, "app", Access::CreateOnly);
    let reader = grant(&store, "app", Access::Read);
    cells
        .compare_set(&store, &creator, &write("app", "key", 0, Some("created")))
        .unwrap();
    assert_eq!(
        cells
            .get(&store, &reader, "app", "key")
            .unwrap()
            .value
            .as_deref(),
        Some("created")
    );
    let before = snapshot(&cells);
    assert_eq!(
        cells.get(&store, &creator, "app", "key"),
        Err(Error::Denied)
    );
    assert_eq!(
        cells.compare_set(&store, &creator, &write("app", "key", 1, Some("overwrite"))),
        Err(Error::Denied)
    );
    assert_eq!(
        cells.compare_set(&store, &creator, &write("app", "fresh", 0, None)),
        Err(Error::Denied)
    );
    assert_eq!(
        cells.compare_set(&store, &reader, &write("app", "key", 1, None)),
        Err(Error::Denied)
    );
    assert_eq!(snapshot(&cells), before);
    cells
        .compare_set(&store, &scope, &write("app", "key", 1, None))
        .unwrap();
    let deleted = snapshot(&cells);
    assert_eq!(
        cells.compare_set(&store, &creator, &write("app", "key", 0, Some("ABA"))),
        Err(Error::Conflict)
    );
    assert_eq!(snapshot(&cells), deleted);
}

#[test]
fn scopes_and_owner_objects_from_another_store_cannot_address_cells() {
    let (_dir, store, scope, mut cells) = fresh(limits());
    let (_other_dir, other_store, other_scope) = owner(Limits::default());
    cells
        .compare_set(&store, &scope, &write("app", "key", 0, Some("private")))
        .unwrap();
    let before = snapshot(&cells);
    for (owner, presented) in [
        (&store, &other_scope),
        (&other_store, &scope),
        (&other_store, &other_scope),
    ] {
        assert_eq!(
            cells.get(owner, presented, "app", "key"),
            Err(Error::Denied)
        );
        assert_eq!(
            cells.compare_set(owner, presented, &write("app", "key", 1, None)),
            Err(Error::Denied)
        );
    }
    assert_eq!(snapshot(&cells), before);
}

#[test]
fn cells_do_not_read_write_or_shadow_actual_durable_records() {
    let (_dir, mut store, scope, mut cells) = fresh(limits());
    store
        .commit(
            &scope,
            &Batch {
                checks: vec![],
                writes: vec![write("app", "same", 0, Some("durable"))],
            },
        )
        .unwrap();
    let before: i64 = store
        .connection
        .query_row("SELECT revision FROM meta WHERE id=1", [], |row| row.get(0))
        .unwrap();
    assert_eq!(
        cells.get(&store, &scope, "app", "same").unwrap().revision,
        0
    );
    cells
        .compare_set(&store, &scope, &write("app", "same", 0, Some("temporary")))
        .unwrap();
    assert_eq!(
        store.get(&scope, "app", "same").unwrap().value.as_deref(),
        Some("durable")
    );
    assert_eq!(
        cells
            .get(&store, &scope, "app", "same")
            .unwrap()
            .value
            .as_deref(),
        Some("temporary")
    );
    let after: i64 = store
        .connection
        .query_row("SELECT revision FROM meta WHERE id=1", [], |row| row.get(0))
        .unwrap();
    assert_eq!(after, before);
}

#[test]
fn preexisting_cells_keep_held_authority_during_storage_poison_but_no_new_scope_is_minted() {
    let (_dir, mut store, scope, mut cells) = fresh(limits());
    cells
        .compare_set(&store, &scope, &write("app", "key", 0, Some("first")))
        .unwrap();
    store.poisoned = true;
    assert_eq!(store.get(&scope, "app", "key"), Err(Error::ReopenRequired));
    assert!(matches!(
        store.scope([("app".into(), Access::ReadWrite)].into()),
        Err(Error::ReopenRequired)
    ));
    assert!(matches!(
        Volatile::new(&store, limits(), &["app"]),
        Err(Error::ReopenRequired)
    ));
    assert_eq!(
        cells
            .get(&store, &scope, "app", "key")
            .unwrap()
            .value
            .as_deref(),
        Some("first")
    );
    cells
        .compare_set(&store, &scope, &write("app", "key", 1, Some("second")))
        .unwrap();
    store.poisoned = false;
    assert_eq!(store.get(&scope, "app", "key").unwrap().revision, 0);
    assert_eq!(
        cells
            .get(&store, &scope, "app", "key")
            .unwrap()
            .value
            .as_deref(),
        Some("second")
    );
}

#[test]
fn reopening_storage_invalidates_old_owner_binding_and_new_cells_start_empty() {
    let (dir, store, scope, mut cells) = fresh(limits());
    cells
        .compare_set(&store, &scope, &write("app", "key", 0, Some("temporary")))
        .unwrap();
    drop(store);
    let reopened = Store::open(dir.path(), OpenMode::Existing, Limits::default()).unwrap();
    let new_scope = grant(&reopened, "app", Access::ReadWrite);
    assert_eq!(
        cells.get(&reopened, &scope, "app", "key"),
        Err(Error::Denied)
    );
    assert_eq!(
        cells.get(&reopened, &new_scope, "app", "key"),
        Err(Error::Denied)
    );
    assert_eq!(
        cells.compare_set(&reopened, &new_scope, &write("app", "key", 1, None)),
        Err(Error::Denied)
    );
    let fresh = Volatile::new(&reopened, limits(), &["app"]).unwrap();
    assert_eq!(
        fresh.get(&reopened, &new_scope, "app", "key").unwrap(),
        Record {
            revision: 0,
            value: None
        }
    );
}

#[test]
fn limits_count_utf8_bytes_before_any_revision_or_state_change() {
    let (_dir, store, scope, mut cells) = fresh(VolatileLimits {
        value_bytes: 4,
        records: 3,
        live_bytes: 8,
    });
    cells
        .compare_set(&store, &scope, &write("app", "key", 0, Some("éé")))
        .unwrap();
    let before = snapshot(&cells);
    assert_eq!(
        cells.compare_set(&store, &scope, &write("app", "key", 1, Some("ééx"))),
        Err(Error::Limit)
    );
    assert_eq!(snapshot(&cells), before);
}

#[test]
fn total_live_byte_bound_is_atomic_and_shrinking_a_value_releases_only_its_bytes() {
    let (_dir, store, scope, mut cells) = fresh(VolatileLimits {
        value_bytes: 4,
        records: 3,
        live_bytes: 5,
    });
    cells
        .compare_set(&store, &scope, &write("app", "a", 0, Some("1234")))
        .unwrap();
    let before = snapshot(&cells);
    assert_eq!(
        cells.compare_set(&store, &scope, &write("app", "b", 0, Some("12"))),
        Err(Error::Limit)
    );
    assert_eq!(snapshot(&cells), before);
    cells
        .compare_set(&store, &scope, &write("app", "a", 1, Some("1")))
        .unwrap();
    cells
        .compare_set(&store, &scope, &write("app", "b", 0, Some("1234")))
        .unwrap();
    assert_eq!(cells.live_bytes, 5);
    assert_eq!(cells.revision, 3);
}

#[test]
fn tombstones_hold_capacity_without_eviction_and_can_be_reused_only_at_their_revision() {
    let (_dir, store, scope, mut cells) = fresh(VolatileLimits {
        records: 1,
        ..limits()
    });
    cells
        .compare_set(&store, &scope, &write("app", "a", 0, Some("first")))
        .unwrap();
    cells
        .compare_set(&store, &scope, &write("app", "a", 1, None))
        .unwrap();
    assert_eq!(cells.live_bytes, 0);
    let before = snapshot(&cells);
    assert_eq!(
        cells.compare_set(&store, &scope, &write("app", "b", 0, Some("new"))),
        Err(Error::Limit)
    );
    assert_eq!(snapshot(&cells), before);
    cells
        .compare_set(&store, &scope, &write("app", "a", 2, Some("return")))
        .unwrap();
    assert_eq!(cells.cells.len(), 1);
    assert_eq!(cells.revision, 3);
}

#[test]
fn invalid_addresses_and_out_of_range_revisions_do_not_change_state() {
    let (_dir, store, scope, mut cells) = fresh(limits());
    for (namespace, key) in [
        ("", "key"),
        ("app/escape", "key"),
        ("app", ""),
        ("app", "../escape"),
        (&"x".repeat(129), "key"),
        ("app", &"x".repeat(257)),
    ] {
        let before = snapshot(&cells);
        assert_eq!(
            cells.get(&store, &scope, namespace, key),
            Err(Error::Invalid)
        );
        assert_eq!(
            cells.compare_set(&store, &scope, &write(namespace, key, 0, Some("value"))),
            Err(Error::Invalid)
        );
        assert_eq!(snapshot(&cells), before);
    }
    assert_eq!(
        cells.compare_set(&store, &scope, &write("app", "key", u64::MAX, None)),
        Err(Error::Invalid)
    );
    assert_eq!(snapshot(&cells), (0, 0, BTreeMap::new()));
}

#[test]
fn exhausted_revision_cannot_wrap_or_reset_the_cell_store() {
    let (_dir, store, scope, mut cells) = fresh(limits());
    cells.revision = i64::MAX as u64;
    let before = snapshot(&cells);
    assert_eq!(
        cells.compare_set(&store, &scope, &write("app", "key", 0, Some("value"))),
        Err(Error::Limit)
    );
    assert_eq!(snapshot(&cells), before);
}

#[test]
fn stale_process_binding_is_refused_before_read_or_mutation() {
    let (_dir, store, scope, mut cells) = fresh(limits());
    cells.process = cells.process.wrapping_add(1);
    let before = snapshot(&cells);
    assert_eq!(cells.get(&store, &scope, "app", "key"), Err(Error::Denied));
    assert_eq!(
        cells.compare_set(&store, &scope, &write("app", "key", 0, Some("value"))),
        Err(Error::Denied)
    );
    assert_eq!(snapshot(&cells), before);
}

#[test]
fn invalid_independent_limits_are_rejected() {
    let (_dir, store, _scope) = owner(Limits::default());
    for invalid in [
        VolatileLimits {
            value_bytes: 0,
            ..limits()
        },
        VolatileLimits {
            value_bytes: VALUE_BYTES + 1,
            live_bytes: LIVE_BYTES,
            ..limits()
        },
        VolatileLimits {
            records: 0,
            ..limits()
        },
        VolatileLimits {
            records: RECORDS + 1,
            ..limits()
        },
        VolatileLimits {
            live_bytes: 31,
            ..limits()
        },
        VolatileLimits {
            live_bytes: LIVE_BYTES + 1,
            ..limits()
        },
    ] {
        assert!(matches!(
            Volatile::new(&store, invalid, &["app"]),
            Err(Error::Invalid)
        ));
    }
}

#[test]
fn temporary_limits_cannot_exceed_already_admitted_durable_store_ceilings() {
    let (_dir, store, _scope) = owner(Limits {
        value_bytes: 4,
        batch_bytes: 4,
        batch_items: 1,
        records: 1,
        live_bytes: 4,
        ..Limits::default()
    });
    let valid = VolatileLimits {
        value_bytes: 4,
        records: 1,
        live_bytes: 4,
    };
    assert!(Volatile::new(&store, valid, &["app"]).is_ok());
    for invalid in [
        VolatileLimits {
            value_bytes: 5,
            live_bytes: 5,
            ..valid
        },
        VolatileLimits {
            records: 2,
            ..valid
        },
        VolatileLimits {
            live_bytes: 5,
            ..valid
        },
    ] {
        assert!(matches!(
            Volatile::new(&store, invalid, &["app"]),
            Err(Error::Invalid)
        ));
    }
}

#[test]
fn two_actual_competing_writers_from_the_same_observation_have_one_winner() {
    let (_dir, store, scope, cells) = fresh(limits());
    let shared = Arc::new(Mutex::new((store, cells, scope)));
    let barrier = Arc::new(Barrier::new(2));
    let threads: Vec<_> = ["first", "second"]
        .into_iter()
        .map(|value| {
            let shared = shared.clone();
            let barrier = barrier.clone();
            std::thread::spawn(move || {
                let observed = {
                    let held = shared.lock().unwrap();
                    held.1.get(&held.0, &held.2, "app", "key").unwrap().revision
                };
                barrier.wait();
                let mut held = shared.lock().unwrap();
                let (owner, cells, scope) = &mut *held;
                cells.compare_set(owner, scope, &write("app", "key", observed, Some(value)))
            })
        })
        .collect();
    let results: Vec<_> = threads
        .into_iter()
        .map(|thread| thread.join().unwrap())
        .collect();
    assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
    assert_eq!(
        results
            .iter()
            .filter(|result| **result == Err(Error::Conflict))
            .count(),
        1
    );
    let held = shared.lock().unwrap();
    assert_eq!(held.1.revision, 1);
    assert_eq!(held.1.cells.len(), 1);
}
