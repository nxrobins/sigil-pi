use super::*;
use crate::{fields, frame};
use sigil_durable_store::store::{Access, Batch, Limits, Mutation, OpenMode};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use tempfile::TempDir;

fn fresh() -> (TempDir, Store, Scope) {
    let dir = tempfile::tempdir().unwrap();
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let store = Store::open(dir.path(), OpenMode::CreateNew, Limits::default()).unwrap();
    let scope = store
        .scope(
            [
                ("app".into(), Access::ReadWrite),
                ("other".into(), Access::ReadWrite),
            ]
            .into(),
        )
        .unwrap();
    (dir, store, scope)
}

fn put(store: &mut Store, scope: &Scope, key: &str, revision: u64, value: Option<&str>) {
    store
        .commit(
            scope,
            &Batch {
                checks: vec![],
                writes: vec![Mutation {
                    namespace: "app".into(),
                    key: key.into(),
                    revision,
                    value: value.map(str::to_owned),
                }],
            },
        )
        .unwrap();
}

#[test]
fn strict_query_refuses_ambiguous_or_unbounded_addresses() {
    for raw in [
        "",
        "null",
        "{}",
        "[]",
        "[null]",
        "[{}]",
        r#"[{"namespace":"app","key":"one","grant":"read_write"}]"#,
        r#"[{"namespace":"app","namespace":"other","key":"one"}]"#,
        r#"[{"namespace":"app","key":1}]"#,
        r#"[{"namespace":"app","key":""}]"#,
        r#"[{"namespace":"../app","key":"one"}]"#,
        r#"[{"namespace":"app","key":"one"},{"namespace":"app","key":"one"}]"#,
        r#"[{"namespace":"app","key":"1"},{"namespace":"app","key":"2"},{"namespace":"app","key":"3"},{"namespace":"app","key":"4"}]"#,
    ] {
        assert!(ReadSet::parse(raw).is_err(), "{raw}");
    }
    for (namespace, key) in [
        ("a".repeat(129), "key".into()),
        ("app".into(), "a".repeat(257)),
        ("app".into(), "é".into()),
    ] {
        let raw = serde_json::to_string(&vec![ReadKey { namespace, key }]).unwrap();
        assert!(ReadSet::parse(&raw).is_err());
    }
}

#[test]
fn query_byte_limit_is_checked_independently_of_record_count() {
    let query = r#"[{"namespace":"app","key":"one"}]"#;
    let padded = format!("{}{}", query, " ".repeat(QUERY_BYTES - query.len()));
    assert_eq!(padded.len(), QUERY_BYTES);
    assert!(ReadSet::parse(&padded).is_ok());
    assert_eq!(ReadSet::parse(&(padded + " ")).unwrap_err(), "limit");
}

#[test]
fn actual_read_returns_bound_coordinates_presence_and_exact_record_revisions() {
    let (_dir, mut store, scope) = fresh();
    put(&mut store, &scope, "empty", 0, Some(""));
    put(&mut store, &scope, "deleted", 0, Some("gone"));
    put(&mut store, &scope, "deleted", 1, None);
    let query = ReadSet::parse(r#"[{"namespace":"app","key":"deleted"},{"namespace":"app","key":"absent"},{"namespace":"app","key":"empty"}]"#).unwrap();
    let raw = query.observe(&mut store, &scope).unwrap();
    let outer = fields(&raw, "RM1\n", 3).unwrap();
    assert_eq!(&outer[..2], ["ok", "3"]);
    let rows = fields(outer[2], "RB1\n", 3).unwrap();
    assert_eq!(
        fields(rows[0], "RR1\n", 5).unwrap(),
        ["app", "deleted", "2", "0", ""]
    );
    assert_eq!(
        fields(rows[1], "RR1\n", 5).unwrap(),
        ["app", "absent", "0", "0", ""]
    );
    assert_eq!(
        fields(rows[2], "RR1\n", 5).unwrap(),
        ["app", "empty", "1", "1", ""]
    );
    assert_eq!(store.get(&scope, "app", "deleted").unwrap().revision, 2);
}

#[test]
fn utf8_is_byte_framed_and_unused_batch_slots_are_empty() {
    let (_dir, mut store, scope) = fresh();
    let value = "first\nRR1\n00000004data é😀\0";
    put(&mut store, &scope, "one", 0, Some(value));
    let raw = ReadSet::parse(r#"[{"namespace":"app","key":"one"}]"#)
        .unwrap()
        .observe(&mut store, &scope)
        .unwrap();
    let outer = fields(&raw, "RM1\n", 3).unwrap();
    assert_eq!(&outer[..2], ["ok", "1"]);
    let rows = fields(outer[2], "RB1\n", 3).unwrap();
    assert_eq!(&rows[1..], ["", ""]);
    assert_eq!(
        fields(rows[0], "RR1\n", 5).unwrap(),
        ["app", "one", "1", "1", value]
    );
}

#[test]
fn denied_members_do_not_expose_a_successful_prefix_or_mint_scope() {
    let (_dir, mut store, scope) = fresh();
    put(&mut store, &scope, "one", 0, Some("retained-canary"));
    let reader = store.scope([("app".into(), Access::Read)].into()).unwrap();
    let query =
        ReadSet::parse(r#"[{"namespace":"app","key":"one"},{"namespace":"other","key":"one"}]"#)
            .unwrap();
    let raw = query.observe(&mut store, &reader).unwrap();
    assert_eq!(raw, frame("RM1\n", &["error", "0", ""]).unwrap());
    assert!(!raw.contains("retained-canary") && !raw.contains("other"));
    let creator = store
        .scope([("app".into(), Access::CreateOnly)].into())
        .unwrap();
    let one = ReadSet::parse(r#"[{"namespace":"app","key":"one"}]"#).unwrap();
    assert_eq!(
        one.observe(&mut store, &creator).unwrap(),
        ReadSet::failure()
    );
    let (_other_dir, _other_store, foreign) = fresh();
    assert_eq!(
        one.observe(&mut store, &foreign).unwrap(),
        ReadSet::failure()
    );
}

#[test]
fn full_reply_limit_includes_all_framing_and_no_output_is_truncated() {
    let query = ReadSet::parse(r#"[{"namespace":"app","key":"one"}]"#).unwrap();
    let mut record = Record {
        revision: i64::MAX as u64,
        value: Some(String::new()),
    };
    let overhead = query.encode(&[record.clone()]).unwrap().len();
    let payload = "a".repeat(RESPONSE_BYTES - overhead);
    record.value = Some(payload);
    let exact = query.encode(&[record.clone()]).unwrap();
    assert_eq!(exact.len(), RESPONSE_BYTES);
    let outer = fields(&exact, "RM1\n", 3).unwrap();
    let batch = fields(outer[2], "RB1\n", 3).unwrap();
    assert_eq!(
        fields(batch[0], "RR1\n", 5).unwrap()[2],
        "9223372036854775807"
    );
    record.value.as_mut().unwrap().push('x');
    assert_eq!(query.encode(&[record]).unwrap_err(), "limit");
}

#[test]
fn impossible_record_shapes_are_not_serialized_as_observed_facts() {
    let query = ReadSet::parse(r#"[{"namespace":"app","key":"one"}]"#).unwrap();
    for rows in [
        vec![],
        vec![Record {
            revision: 0,
            value: Some("forged".into()),
        }],
        vec![Record {
            revision: i64::MAX as u64 + 1,
            value: None,
        }],
    ] {
        assert_eq!(query.encode(&rows).unwrap_err(), "protocol");
    }
}

#[test]
fn existing_host_profiles_do_not_expose_the_read_set_command() {
    use crate::action::{EntryContract, perform_with_http_contract};
    use std::time::{Duration, Instant};
    let policy = crate::http_exchange::HeaderPolicy::new(&["x-request-id"]).unwrap();
    for (contract, marker, http) in [
        (EntryContract::Legacy, "HC3\n", None),
        (EntryContract::Metadata, "HC4\n", None),
        (EntryContract::HttpExchange, "HC5\n", Some(&policy)),
    ] {
        let raw = frame(
            marker,
            &[
                "read_many",
                r#"[{"namespace":"app","key":"one"}]"#,
                "",
                "held",
                &frame("TG1\n", &["100", "200"]).unwrap(),
            ],
        )
        .unwrap();
        assert_eq!(
            perform_with_http_contract(
                (contract, http),
                None,
                None,
                &mut 100,
                Instant::now() + Duration::from_secs(30),
                &raw,
                || panic!("old profile dispatched a new command")
            )
            .unwrap_err(),
            "protocol"
        );
    }
}
