use super::*;
use crate::fields;
use sigil_durable_store::store::volatile::VolatileLimits;
use sigil_durable_store::store::{Access, Limits, OpenMode};
use std::collections::BTreeSet;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use tempfile::TempDir;

// Retain every earlier value/presence/receipt assertion while extending the
// durable frames. These codec-only synthetic failures explicitly use Storage;
// origin_tests separately asserts the entire frame for both origins and actual
// denied scopes, unsafe layouts, missing paths and conditional writes.
fn synthetic_failure(error: Error) -> sigil_durable_store::store::ObservedError {
    sigil_durable_store::store::ObservedError {
        origin: FailureOrigin::Storage,
        error,
    }
}

fn read_result(marker: &str, seen: sigil_durable_store::store::Result<Record>) -> Result<String> {
    match marker {
        "DR2\n" => super::durable_read_result(seen.map_err(synthetic_failure)),
        "VR1\n" => super::temporary_read_result(seen),
        _ => panic!("unknown test codec"),
    }
}

fn durable_receipt(seen: sigil_durable_store::store::Result<Receipt>) -> Result<String> {
    super::durable_receipt(seen.map_err(synthetic_failure))
}

const ERRORS: [(Error, &str); 11] = [
    (Error::Invalid, "invalid"),
    (Error::Denied, "denied"),
    (Error::Conflict, "conflict"),
    (Error::AlreadyExists, "already_exists"),
    (Error::Missing, "missing"),
    (Error::Busy, "busy"),
    (Error::Corrupt, "corrupt"),
    (Error::Storage, "storage"),
    (Error::Limit, "limit"),
    (Error::CommitUncertain, "commit_uncertain"),
    (Error::ReopenRequired, "reopen_required"),
];

fn limits() -> VolatileLimits {
    VolatileLimits {
        value_bytes: VALUE_BYTES,
        records: 2,
        live_bytes: 2 * VALUE_BYTES,
    }
}

fn fixture() -> (TempDir, Store, Scope, Volatile) {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().join("store");
    fs::create_dir(&root).unwrap();
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    let store = Store::open(&root, OpenMode::CreateNew, Limits::default()).unwrap();
    let scope = store
        .scope([("app".into(), Access::ReadWrite)].into())
        .unwrap();
    let cells = Volatile::new(&store, limits(), &["app", "other"]).unwrap();
    (dir, store, scope, cells)
}

fn mutation(namespace: &str, key: &str, revision: u64, value: Option<&str>) -> Mutation {
    Mutation {
        namespace: namespace.into(),
        key: key.into(),
        revision,
        value: value.map(str::to_owned),
    }
}

fn batch(key: &str, revision: u64, value: Option<&str>) -> Batch {
    Batch {
        checks: vec![],
        writes: vec![mutation("app", key, revision, value)],
    }
}

fn temporary(namespace: &str, key: &str, revision: u64, value: Option<&str>) -> TemporaryWrite {
    TemporaryWrite::parse(
        &serde_json::to_string(&mutation(namespace, key, revision, value)).unwrap(),
    )
    .unwrap()
}

fn check_read(raw: &str, marker: &str, expected: &[&str; 5]) {
    let values = fields(raw, marker, if marker == "DR2\n" { 6 } else { 5 }).unwrap();
    assert_eq!(&values[..5], expected);
    if marker == "DR2\n" {
        assert_eq!(values[5].is_empty(), expected[0] == "ok");
        assert!(matches!(values[5], "" | "precheck" | "storage"));
    }
    assert!(raw.len() <= RESPONSE_BYTES);
}

fn check_receipt(raw: &str, marker: &str, expected: &[&str; 3]) {
    let values = fields(raw, marker, if marker == "DC2\n" { 4 } else { 3 }).unwrap();
    assert_eq!(&values[..3], expected);
    if marker == "DC2\n" {
        assert_eq!(values[3].is_empty(), expected[0] == "ok");
        assert!(matches!(values[3], "" | "precheck" | "storage"));
    }
    assert!(raw.len() < 128);
}

#[test]
fn native_error_labels_are_exact_distinct_bounded_facts_not_policy() {
    let mut labels = BTreeSet::new();
    for (error, label) in ERRORS {
        assert_eq!(error_label(error), label);
        assert!(labels.insert(label));
        assert!(label.len() <= 16);
        assert!(
            label
                .bytes()
                .all(|byte| byte.is_ascii_lowercase() || byte == b'_')
        );
    }
    assert_eq!(labels.len(), 11);
}

#[test]
fn every_read_error_has_no_revision_presence_payload_or_diagnostic() {
    for marker in ["DR2\n", "VR1\n"] {
        for (error, label) in ERRORS {
            let raw = read_result(marker, Err(error)).unwrap();
            check_read(&raw, marker, &["error", "0", "0", "", label]);
            assert!(raw.len() < 128);
        }
    }
}

#[test]
fn every_receipt_error_including_uncertain_commit_remains_a_failure() {
    for (error, label) in ERRORS {
        check_receipt(
            &durable_receipt(Err(error)).unwrap(),
            "DC2\n",
            &["error", "0", label],
        );
        check_receipt(
            &temporary_receipt(Err(error)).unwrap(),
            "VC1\n",
            &["error", "0", label],
        );
    }
}

#[test]
fn observations_do_not_alias_durable_or_legacy_frames() {
    let durable = durable_receipt(Ok(Receipt { revision: 1 })).unwrap();
    let temporary = temporary_receipt(Ok(VolatileReceipt { revision: 1 })).unwrap();
    check_receipt(&durable, "DC2\n", &["ok", "1", ""]);
    check_receipt(&temporary, "VC1\n", &["ok", "1", ""]);
    assert!(fields(&temporary, "DC2\n", 3).is_err());
    assert!(fields(&durable, "VC1\n", 3).is_err());
    for raw in [&durable, &temporary] {
        assert!(fields(raw, "SC1\n", 2).is_err());
    }
    for (marker, other) in [("DR2\n", "VR1\n"), ("VR1\n", "DR2\n")] {
        let raw = read_result(
            marker,
            Ok(Record {
                revision: 0,
                value: None,
            }),
        )
        .unwrap();
        assert!(fields(&raw, other, 5).is_err());
        assert!(fields(&raw, "SR1\n", 3).is_err());
    }
}

#[test]
fn missing_empty_and_tombstoned_reads_have_distinct_canonical_observations() {
    for marker in ["DR2\n", "VR1\n"] {
        for (record, expected) in [
            (
                Record {
                    revision: 0,
                    value: None,
                },
                ["ok", "0", "0", "", ""],
            ),
            (
                Record {
                    revision: 1,
                    value: Some(String::new()),
                },
                ["ok", "1", "1", "", ""],
            ),
            (
                Record {
                    revision: 2,
                    value: None,
                },
                ["ok", "2", "0", "", ""],
            ),
        ] {
            check_read(&read_result(marker, Ok(record)).unwrap(), marker, &expected);
        }
    }
}

#[test]
fn reads_preserve_exact_utf8_control_bytes_and_embedded_frame_markers() {
    let value = "é😀\0\r\nVR1\n00000002ok\"\\";
    for marker in ["DR2\n", "VR1\n"] {
        let raw = read_result(
            marker,
            Ok(Record {
                revision: 1,
                value: Some(value.into()),
            }),
        )
        .unwrap();
        check_read(&raw, marker, &["ok", "1", "1", value, ""]);
        assert_eq!(
            raw.len(),
            (if marker == "DR2\n" { 56 } else { 48 }) + value.len()
        );
    }
}

#[test]
fn impossible_internal_records_are_not_reclassified_as_storage_outages() {
    for marker in ["DR2\n", "VR1\n"] {
        for record in [
            Record {
                revision: 0,
                value: Some(String::new()),
            },
            Record {
                revision: 0,
                value: Some("impossible".into()),
            },
            Record {
                revision: i64::MAX as u64 + 1,
                value: None,
            },
        ] {
            assert_eq!(read_result(marker, Ok(record)), Err("protocol"));
        }
    }
}

#[test]
fn successful_receipts_require_positive_nonwrapping_actual_revisions() {
    for revision in [0, i64::MAX as u64 + 1, u64::MAX] {
        assert_eq!(durable_receipt(Ok(Receipt { revision })), Err("protocol"));
        assert_eq!(
            temporary_receipt(Ok(VolatileReceipt { revision })),
            Err("protocol")
        );
    }
    let maximum = (i64::MAX as u64).to_string();
    check_receipt(
        &durable_receipt(Ok(Receipt {
            revision: i64::MAX as u64,
        }))
        .unwrap(),
        "DC2\n",
        &["ok", &maximum, ""],
    );
}

#[test]
fn the_entire_read_frame_is_bounded_and_oversize_is_not_a_native_storage_error() {
    let value = "a".repeat(RESPONSE_BYTES - 56);
    let raw = read_result(
        "DR2\n",
        Ok(Record {
            revision: 1,
            value: Some(value.clone()),
        }),
    )
    .unwrap();
    assert_eq!(raw.len(), RESPONSE_BYTES);
    check_read(&raw, "DR2\n", &["ok", "1", "1", &value, ""]);
    assert_eq!(
        read_result(
            "DR2\n",
            Ok(Record {
                revision: 1,
                value: Some(value.clone() + "a")
            })
        ),
        Err("limit")
    );
    assert_eq!(
        read_result(
            "DR2\n",
            Ok(Record {
                revision: 10,
                value: Some(value)
            })
        ),
        Err("limit")
    );
}

#[test]
fn actual_durable_and_temporary_operations_are_separate_at_identical_coordinates() {
    let (_dir, mut store, scope, mut cells) = fixture();
    check_read(
        &durable_read(&store, &scope, "app", "same").unwrap(),
        "DR2\n",
        &["ok", "0", "0", "", ""],
    );
    check_receipt(
        &durable_commit(&mut store, &scope, &batch("same", 0, Some("durable"))).unwrap(),
        "DC2\n",
        &["ok", "1", ""],
    );
    check_read(
        &temporary_read(&cells, &store, &scope, "app", "same").unwrap(),
        "VR1\n",
        &["ok", "0", "0", "", ""],
    );
    check_receipt(
        &temporary("app", "same", 0, Some("temporary"))
            .perform(&mut cells, &store, &scope)
            .unwrap(),
        "VC1\n",
        &["ok", "1", ""],
    );
    check_read(
        &durable_read(&store, &scope, "app", "same").unwrap(),
        "DR2\n",
        &["ok", "1", "1", "durable", ""],
    );
    check_read(
        &temporary_read(&cells, &store, &scope, "app", "same").unwrap(),
        "VR1\n",
        &["ok", "1", "1", "temporary", ""],
    );
    // A second actual durable commit proves that temporary work did not advance
    // the store's global commit receipt or add a hidden durable write.
    check_receipt(
        &durable_commit(&mut store, &scope, &batch("second", 0, Some("other"))).unwrap(),
        "DC2\n",
        &["ok", "2", ""],
    );
}

#[test]
fn parsed_temporary_writes_round_trip_actual_opaque_values_and_tombstones() {
    let (_dir, store, scope, mut cells) = fixture();
    let value = "\0é😀\nDR2\n00000002ok";
    check_receipt(
        &temporary("app", "key", 0, Some(value))
            .perform(&mut cells, &store, &scope)
            .unwrap(),
        "VC1\n",
        &["ok", "1", ""],
    );
    check_read(
        &temporary_read(&cells, &store, &scope, "app", "key").unwrap(),
        "VR1\n",
        &["ok", "1", "1", value, ""],
    );
    check_receipt(
        &temporary("app", "key", 1, None)
            .perform(&mut cells, &store, &scope)
            .unwrap(),
        "VC1\n",
        &["ok", "2", ""],
    );
    check_read(
        &temporary_read(&cells, &store, &scope, "app", "key").unwrap(),
        "VR1\n",
        &["ok", "2", "0", "", ""],
    );
}

#[test]
fn temporary_query_is_one_unambiguous_mutation_with_required_nullable_value() {
    for raw in [
        "",
        "null",
        "[]",
        "{}",
        r#"{"namespace":"app","key":"key","revision":0}"#,
        r#"{"namespace":"app","key":"key","revision":0,"value":1}"#,
        r#"{"namespace":"app","key":"key","revision":-1,"value":null}"#,
        r#"{"namespace":"app","key":"key","revision":0.0,"value":null}"#,
        r#"{"namespace":"app","key":"key","revision":"0","value":null}"#,
        r#"{"namespace":"app","key":"key","revision":0,"value":null,"scope":"all"}"#,
        r#"{"namespace":"app","key":"key","revision":0,"value":null,"key":"other"}"#,
        r#"{"namespace":"app","key":"key","revision":0,"value":null,"\u0076alue":"other"}"#,
        r#"{"namespace":"app","key":"key","revision":0,"value":null}null"#,
    ] {
        assert_eq!(TemporaryWrite::parse(raw).unwrap_err(), "protocol", "{raw}");
    }
}

#[test]
fn temporary_query_byte_ceiling_is_exact_and_independent_of_cell_value_limit() {
    let raw = r#"{"namespace":"app","key":"key","revision":0,"value":null}"#;
    let padded = raw.to_owned() + &" ".repeat(TEMPORARY_QUERY_BYTES - raw.len());
    assert!(TemporaryWrite::parse(&padded).is_ok());
    assert_eq!(TemporaryWrite::parse(&(padded + " ")).unwrap_err(), "limit");
}

#[test]
fn worst_case_json_escapes_fit_but_decoded_bytes_still_hit_the_cell_ceiling() {
    let (_dir, store, scope, mut cells) = fixture();
    let value = "\0".repeat(VALUE_BYTES);
    let raw = serde_json::to_string(&mutation("app", "key", 0, Some(&value))).unwrap();
    assert!(raw.len() > VALUE_BYTES * 6);
    check_receipt(
        &TemporaryWrite::parse(&raw)
            .unwrap()
            .perform(&mut cells, &store, &scope)
            .unwrap(),
        "VC1\n",
        &["ok", "1", ""],
    );
    let too_large = value.clone() + "\0";
    check_receipt(
        &temporary("app", "key", 1, Some(&too_large))
            .perform(&mut cells, &store, &scope)
            .unwrap(),
        "VC1\n",
        &["error", "0", "limit"],
    );
    check_read(
        &temporary_read(&cells, &store, &scope, "app", "key").unwrap(),
        "VR1\n",
        &["ok", "1", "1", &value, ""],
    );
}

#[test]
fn actual_conflict_invalid_and_denied_writes_remain_distinct_and_do_not_mutate() {
    let (_dir, mut store, scope, mut cells) = fixture();
    durable_commit(&mut store, &scope, &batch("key", 0, Some("durable"))).unwrap();
    temporary("app", "key", 0, Some("temporary"))
        .perform(&mut cells, &store, &scope)
        .unwrap();
    for (namespace, key, revision, label) in [
        ("app", "key", 0, "conflict"),
        ("app", "bad/key", 0, "invalid"),
        ("app", "key", i64::MAX as u64 + 1, "invalid"),
        ("other", "key", 1, "denied"),
    ] {
        let write = mutation(namespace, key, revision, Some("unaccepted-canary"));
        check_receipt(
            &durable_commit(
                &mut store,
                &scope,
                &Batch {
                    checks: vec![],
                    writes: vec![write],
                },
            )
            .unwrap(),
            "DC2\n",
            &["error", "0", label],
        );
        check_receipt(
            &temporary(namespace, key, revision, Some("unaccepted-canary"))
                .perform(&mut cells, &store, &scope)
                .unwrap(),
            "VC1\n",
            &["error", "0", label],
        );
    }
    check_read(
        &durable_read(&store, &scope, "app", "key").unwrap(),
        "DR2\n",
        &["ok", "1", "1", "durable", ""],
    );
    check_read(
        &temporary_read(&cells, &store, &scope, "app", "key").unwrap(),
        "VR1\n",
        &["ok", "1", "1", "temporary", ""],
    );
}

#[test]
fn held_read_and_create_only_scopes_do_not_gain_new_access_through_the_codec() {
    let (_dir, store, _scope, mut cells) = fixture();
    let creator = store
        .scope([("app".into(), Access::CreateOnly)].into())
        .unwrap();
    let reader = store.scope([("app".into(), Access::Read)].into()).unwrap();
    temporary("app", "key", 0, Some("private-canary"))
        .perform(&mut cells, &store, &creator)
        .unwrap();
    check_read(
        &temporary_read(&cells, &store, &creator, "app", "key").unwrap(),
        "VR1\n",
        &["error", "0", "0", "", "denied"],
    );
    check_receipt(
        &temporary("app", "key", 1, None)
            .perform(&mut cells, &store, &reader)
            .unwrap(),
        "VC1\n",
        &["error", "0", "denied"],
    );
    check_read(
        &temporary_read(&cells, &store, &reader, "app", "key").unwrap(),
        "VR1\n",
        &["ok", "1", "1", "private-canary", ""],
    );
}

#[test]
fn actual_storage_path_failure_is_reported_without_preventing_held_temporary_access() {
    let (dir, mut store, scope, mut cells) = fixture();
    durable_commit(&mut store, &scope, &batch("key", 0, Some("durable"))).unwrap();
    temporary("app", "key", 0, Some("temporary"))
        .perform(&mut cells, &store, &scope)
        .unwrap();
    let root = dir.path().join("store");
    let parked = dir.path().join("temporarily-unavailable");
    fs::rename(&root, &parked).unwrap();
    check_read(
        &durable_read(&store, &scope, "app", "key").unwrap(),
        "DR2\n",
        &["error", "0", "0", "", "storage"],
    );
    check_receipt(
        &durable_commit(&mut store, &scope, &batch("key", 1, Some("unaccepted"))).unwrap(),
        "DC2\n",
        &["error", "0", "storage"],
    );
    assert!(matches!(
        store.scope([("app".into(), Access::ReadWrite)].into()),
        Err(Error::Storage)
    ));
    assert!(matches!(
        Volatile::new(&store, limits(), &["app"]),
        Err(Error::Storage)
    ));
    check_receipt(
        &temporary("app", "key", 1, Some("still-temporary"))
            .perform(&mut cells, &store, &scope)
            .unwrap(),
        "VC1\n",
        &["ok", "2", ""],
    );
    check_read(
        &temporary_read(&cells, &store, &scope, "app", "key").unwrap(),
        "VR1\n",
        &["ok", "2", "1", "still-temporary", ""],
    );
    fs::rename(&parked, &root).unwrap();
    check_read(
        &durable_read(&store, &scope, "app", "key").unwrap(),
        "DR2\n",
        &["ok", "1", "1", "durable", ""],
    );
    check_receipt(
        &durable_commit(&mut store, &scope, &batch("key", 1, Some("recovered"))).unwrap(),
        "DC2\n",
        &["ok", "2", ""],
    );
}

#[test]
fn wrong_store_scopes_cannot_expose_or_mutate_either_storage_kind() {
    let (_dir, mut store, scope, mut cells) = fixture();
    let (_foreign_dir, foreign, foreign_scope, _foreign_cells) = fixture();
    durable_commit(&mut store, &scope, &batch("key", 0, Some("private"))).unwrap();
    temporary("app", "key", 0, Some("private"))
        .perform(&mut cells, &store, &scope)
        .unwrap();
    check_read(
        &durable_read(&store, &foreign_scope, "app", "key").unwrap(),
        "DR2\n",
        &["error", "0", "0", "", "denied"],
    );
    check_receipt(
        &durable_commit(&mut store, &foreign_scope, &batch("key", 1, None)).unwrap(),
        "DC2\n",
        &["error", "0", "denied"],
    );
    for (owner, held) in [
        (&store, &foreign_scope),
        (&foreign, &scope),
        (&foreign, &foreign_scope),
    ] {
        check_read(
            &temporary_read(&cells, owner, held, "app", "key").unwrap(),
            "VR1\n",
            &["error", "0", "0", "", "denied"],
        );
        check_receipt(
            &temporary("app", "key", 1, None)
                .perform(&mut cells, owner, held)
                .unwrap(),
            "VC1\n",
            &["error", "0", "denied"],
        );
    }
}

#[test]
fn actual_reopen_keeps_only_durable_data_and_invalidates_old_temporary_bindings() {
    let (dir, mut store, scope, mut cells) = fixture();
    durable_commit(&mut store, &scope, &batch("key", 0, Some("durable"))).unwrap();
    temporary("app", "key", 0, Some("temporary"))
        .perform(&mut cells, &store, &scope)
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
    for held in [&scope, &current] {
        check_read(
            &temporary_read(&cells, &reopened, held, "app", "key").unwrap(),
            "VR1\n",
            &["error", "0", "0", "", "denied"],
        );
    }
    check_read(
        &durable_read(&reopened, &current, "app", "key").unwrap(),
        "DR2\n",
        &["ok", "1", "1", "durable", ""],
    );
    let fresh = Volatile::new(&reopened, limits(), &["app"]).unwrap();
    check_read(
        &temporary_read(&fresh, &reopened, &current, "app", "key").unwrap(),
        "VR1\n",
        &["ok", "0", "0", "", ""],
    );
}

#[path = "storage_origin_tests.rs"]
mod origin_tests;
