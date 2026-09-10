use super::*;
use crate::http_exchange::HeaderPolicy;
use crate::readset::ReadSet;
use sigil_durable_store::store::{Access, Limits, OpenMode};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::time::Duration;
use tempfile::TempDir;

const QUERY: &str = r#"[{"namespace":"app","key":"one"},{"namespace":"app","key":"absent"}]"#;

fn fixture() -> (TempDir, Store, Scope) {
    let dir = tempfile::tempdir().unwrap();
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let store = Store::open(dir.path(), OpenMode::CreateNew, Limits::default()).unwrap();
    let scope = store
        .scope([("app".into(), Access::ReadWrite)].into())
        .unwrap();
    (dir, store, scope)
}

fn command(query: &str, extra: &str, guard: &str) -> String {
    frame(
        "HC6\n",
        &["read_many", query, extra, "held-only-by-entry", guard],
    )
    .unwrap()
}

fn future() -> Instant {
    Instant::now() + Duration::from_secs(30)
}

#[test]
fn explicit_inventory_and_marker_do_not_alias_older_http_contract() {
    assert_eq!(EntryContract::RequestAdmission.envelope_marker(), "AH6\n");
    assert_eq!(EntryContract::RequestAdmission.command_marker(), "HC6\n");
    assert_eq!(
        EntryContract::RequestAdmission.command_inventory(),
        Some(r#"["call","commit","metadata","read","read_many","reply"]"#)
    );
    assert!(EntryContract::RequestAdmission.uses_http());
    assert!(EntryContract::HttpExchange.uses_http());
    assert!(!EntryContract::Legacy.uses_http());
    assert!(!EntryContract::Metadata.uses_http());
}

#[test]
fn actual_grouped_read_preserves_native_scope_revision_and_entry_continuation() {
    let (dir, mut store, scope) = fixture();
    store
        .commit(
            &scope,
            &Batch {
                checks: vec![],
                writes: vec![Mutation {
                    namespace: "app".into(),
                    key: "one".into(),
                    revision: 0,
                    value: Some("opaque-value-é".into()),
                }],
            },
        )
        .unwrap();
    let reader = store.scope([("app".into(), Access::Read)].into()).unwrap();
    let policy = HeaderPolicy::new(&["x-request-id"]).unwrap();
    let raw = command(QUERY, "", &frame("TG1\n", &["100", "200"]).unwrap());
    let mut high = 100;
    for _ in 0..2 {
        let Step::Continue {
            stage,
            observation,
            continuation,
        } = perform_with_http_contract(
            (EntryContract::RequestAdmission, Some(&policy)),
            Some(&mut store),
            Some(&reader),
            &mut high,
            future(),
            &raw,
            || Ok(150),
        )
        .unwrap()
        else {
            panic!("grouped read was not returned as data")
        };
        assert_eq!(high, 150);
        assert_eq!(stage, "read_many");
        assert_eq!(continuation, "held-only-by-entry");
        let outer = fields(&observation, "RM1\n", 3).unwrap();
        assert_eq!(&outer[..2], ["ok", "2"]);
        let rows = fields(outer[2], "RB1\n", 3).unwrap();
        assert_eq!(
            fields(rows[0], "RR1\n", 5).unwrap(),
            ["app", "one", "1", "1", "opaque-value-é"]
        );
        assert_eq!(
            fields(rows[1], "RR1\n", 5).unwrap(),
            ["app", "absent", "0", "0", ""]
        );
        assert_eq!(rows[2], "");
    }
    drop(store);
    let store = Store::open(dir.path(), OpenMode::Existing, Limits::default()).unwrap();
    let reader = store.scope([("app".into(), Access::Read)].into()).unwrap();
    assert_eq!(store.get(&reader, "app", "one").unwrap().revision, 1);
    assert_eq!(store.get(&reader, "app", "absent").unwrap().revision, 0);
}

#[test]
fn malformed_or_unguarded_read_sets_and_wrong_protocol_fail_before_clock_or_store() {
    let policy = HeaderPolicy::new(&["x-request-id"]).unwrap();
    let guard = frame("TG1\n", &["100", "200"]).unwrap();
    let valid = command(QUERY, "", &guard);
    let mut bad = vec![
        command(QUERY, "extra", &guard),
        command(QUERY, "", ""),
        command(QUERY, "", &frame("TG1\n", &["0100", "200"]).unwrap()),
        valid.clone() + "trailing",
        valid.replacen("HC6\n", "HC5\n", 1),
    ];
    for query in [
        "[]",
        "null",
        r#"[{"namespace":"app","key":"one","scope":"all"}]"#,
        r#"[{"namespace":"app","key":"one","key":"two"}]"#,
    ] {
        bad.push(command(query, "", &guard));
    }
    for raw in bad {
        let mut high = 100;
        assert_eq!(
            perform_with_http_contract(
                (EntryContract::RequestAdmission, Some(&policy)),
                None,
                None,
                &mut high,
                future(),
                &raw,
                || panic!("malformed query reached native action")
            )
            .unwrap_err(),
            "protocol"
        );
        assert_eq!(high, 100);
    }
    assert_eq!(
        perform_with_http_contract(
            (EntryContract::RequestAdmission, None),
            None,
            None,
            &mut 100,
            future(),
            &valid,
            || panic!("missing HTTP contract reached native action")
        )
        .unwrap_err(),
        "protocol"
    );
    let over = command(
        &(QUERY.to_owned() + &" ".repeat(crate::readset::QUERY_BYTES)),
        "",
        &guard,
    );
    assert_eq!(
        perform_with_http_contract(
            (EntryContract::RequestAdmission, Some(&policy)),
            None,
            None,
            &mut 100,
            future(),
            &over,
            || panic!("oversize query reached native action")
        )
        .unwrap_err(),
        "limit"
    );
}

#[test]
fn time_expiry_rollback_and_deadline_refuse_before_a_store_is_required() {
    let policy = HeaderPolicy::new(&["x-request-id"]).unwrap();
    let raw = command(QUERY, "", &frame("TG1\n", &["100", "200"]).unwrap());
    for (mut last, clock, expected) in [
        (90, 99, "time_guard"),
        (100, 200, "time_guard"),
        (150, 149, "clock"),
    ] {
        assert_eq!(
            perform_with_http_contract(
                (EntryContract::RequestAdmission, Some(&policy)),
                None,
                None,
                &mut last,
                future(),
                &raw,
                || Ok(clock)
            )
            .unwrap_err(),
            expected
        );
    }
    assert_eq!(
        perform_with_http_contract(
            (EntryContract::RequestAdmission, Some(&policy)),
            None,
            None,
            &mut 100,
            Instant::now() - Duration::from_millis(1),
            &raw,
            || panic!("deadline already elapsed")
        )
        .unwrap_err(),
        "deadline"
    );
}

#[test]
fn refused_native_scope_returns_no_partial_values_or_grants() {
    let (_dir, mut store, scope) = fixture();
    store
        .commit(
            &scope,
            &Batch {
                checks: vec![],
                writes: vec![Mutation {
                    namespace: "app".into(),
                    key: "one".into(),
                    revision: 0,
                    value: Some("private-canary".into()),
                }],
            },
        )
        .unwrap();
    let creator = store
        .scope([("app".into(), Access::CreateOnly)].into())
        .unwrap();
    let (_foreign_dir, _foreign_store, foreign) = fixture();
    let policy = HeaderPolicy::new(&["x-request-id"]).unwrap();
    let guard = frame("TG1\n", &["100", "200"]).unwrap();
    let mixed = r#"[{"namespace":"app","key":"one"},{"namespace":"other","key":"one"}]"#;
    for (scope, query) in [(&scope, mixed), (&creator, QUERY), (&foreign, QUERY)] {
        let Step::Continue { observation, .. } = perform_with_http_contract(
            (EntryContract::RequestAdmission, Some(&policy)),
            Some(&mut store),
            Some(scope),
            &mut 100,
            future(),
            &command(query, "", &guard),
            || Ok(150),
        )
        .unwrap() else {
            panic!("refusal was not returned as data")
        };
        assert_eq!(observation, ReadSet::failure());
        assert!(!observation.contains("private-canary"));
    }
    for (store, scope) in [(Some(&mut store), None), (None, Some(&scope))] {
        assert_eq!(
            perform_with_http_contract(
                (EntryContract::RequestAdmission, Some(&policy)),
                store,
                scope,
                &mut 100,
                future(),
                &command(QUERY, "", &guard),
                || Ok(150)
            )
            .unwrap_err(),
            "capability"
        );
    }
}
