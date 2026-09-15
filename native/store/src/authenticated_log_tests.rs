use super::*;
use std::os::unix::fs::PermissionsExt;
use tempfile::TempDir;

const KEY: &[u8] = b"native-only-test-key-never-persisted-0123456789";

fn limits() -> ChainLimits {
    ChainLimits {
        payload_bytes: 1024,
        records: 1000,
        bytes: 2 * 1024 * 1024,
    }
}

fn grant(store: &Store, access: Access) -> Scope {
    store
        .scope(BTreeMap::from([
            ("heads".into(), access),
            ("entries".into(), access),
            ("domain".into(), access),
        ]))
        .unwrap()
}

fn fresh() -> (TempDir, Store, Scope, AuthenticatedLog) {
    let dir = tempfile::tempdir().unwrap();
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let store = Store::open(dir.path(), OpenMode::CreateNew, Limits::default()).unwrap();
    let scope = grant(&store, Access::ReadWrite);
    let log = AuthenticatedLog::new(&store, "heads", "entries", limits(), KEY).unwrap();
    (dir, store, scope, log)
}

fn empty() -> Batch {
    Batch {
        checks: vec![],
        writes: vec![],
    }
}

fn mutation(namespace: &str, key: &str, revision: u64, value: Option<&str>) -> Mutation {
    Mutation {
        namespace: namespace.into(),
        key: key.into(),
        revision,
        value: value.map(str::to_owned),
    }
}

fn write(store: &mut Store, scope: &Scope, namespace: &str, key: &str, value: Option<&str>) {
    let revision = store.get(scope, namespace, key).unwrap().revision;
    store
        .commit(
            scope,
            &Batch {
                checks: vec![],
                writes: vec![mutation(namespace, key, revision, value)],
            },
        )
        .unwrap();
}

fn snapshot(store: &Store) -> (i64, String) {
    let revision = store
        .connection
        .query_row("SELECT revision FROM meta", [], |r| r.get(0))
        .unwrap();
    let rows = store.connection.prepare("SELECT namespace,key,revision,hex(value),hex(digest) FROM records ORDER BY namespace,key").unwrap()
        .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, i64>(2)?,
            r.get::<_, String>(3)?, r.get::<_, String>(4)?))).unwrap()
        .collect::<std::result::Result<Vec<_>, _>>().unwrap();
    (revision, serde_json::to_string(&rows).unwrap())
}

fn verify(
    log: &AuthenticatedLog,
    store: &mut Store,
    scope: &Scope,
    chain: &str,
    expected: Option<&Checkpoint>,
) -> std::result::Result<Checkpoint, Failure> {
    log.verify(
        store,
        scope,
        chain,
        expected,
        Instant::now() + Duration::from_secs(5),
    )
}

fn corrupted(result: std::result::Result<Checkpoint, Failure>) {
    assert_eq!(
        result,
        Err(Failure::Observed(ObservedError::storage(Error::Corrupt)))
    );
}

#[test]
fn optional_inspection_distinguishes_absence_without_creating_verified_history() {
    let (_dir, mut store, scope, log) = fresh();
    let chain = "a".repeat(64);
    let other = "b".repeat(64);
    let published = log
        .append(&mut store, &scope, &other, 0, "other chain", &empty())
        .unwrap();
    let before = snapshot(&store);
    assert_eq!(
        log.inspect_existing(
            &mut store,
            &scope,
            &chain,
            None,
            Instant::now() + Duration::from_secs(5)
        )
        .unwrap(),
        None
    );
    assert!(verify(&log, &mut store, &scope, &chain, None).is_err());
    assert!(
        log.inspect_existing(
            &mut store,
            &scope,
            &chain,
            Some(&published.checkpoint),
            Instant::now() + Duration::from_secs(5)
        )
        .is_err()
    );
    assert_eq!(
        log.inspect_existing(
            &mut store,
            &scope,
            &other,
            Some(&published.checkpoint),
            Instant::now() + Duration::from_secs(5)
        )
        .unwrap(),
        Some(published.checkpoint)
    );
    assert_eq!(snapshot(&store), before);
}

#[test]
fn optional_inspection_refuses_tombstones_orphans_and_historical_corruption() {
    for kind in [
        "head_tombstone",
        "entry_tombstone",
        "orphan",
        "orphan_tombstone",
        "historical_corruption",
    ] {
        let (_dir, mut store, scope, log) = fresh();
        let chain = "a".repeat(64);
        if !kind.starts_with("orphan") {
            let first = log
                .append(&mut store, &scope, &chain, 0, "first", &empty())
                .unwrap();
            log.append(
                &mut store,
                &scope,
                &chain,
                first.checkpoint.head_revision,
                "second",
                &empty(),
            )
            .unwrap();
        }
        match kind {
            "head_tombstone" => write(&mut store, &scope, "heads", &chain, None),
            "entry_tombstone" | "orphan_tombstone" => {
                write(&mut store, &scope, "entries", &entry_key(&chain, 0), None)
            }
            _ => write(
                &mut store,
                &scope,
                "entries",
                &entry_key(&chain, 0),
                Some("not an authenticated entry"),
            ),
        }
        let before = snapshot(&store);
        assert!(
            log.inspect_existing(
                &mut store,
                &scope,
                &chain,
                None,
                Instant::now() + Duration::from_secs(5)
            )
            .is_err(),
            "{kind}"
        );
        assert_eq!(snapshot(&store), before);
    }
}

#[test]
fn optional_inspection_retains_key_scope_and_deadline_boundaries() {
    let (_dir, mut store, scope, log) = fresh();
    let chain = "a".repeat(64);
    log.append(&mut store, &scope, &chain, 0, "event", &empty())
        .unwrap();
    let wrong = AuthenticatedLog::new(&store, "heads", "entries", limits(), &[7; 32]).unwrap();
    let denied = store
        .scope(BTreeMap::from([("heads".into(), Access::Read)]))
        .unwrap();
    let before = snapshot(&store);
    assert!(
        wrong
            .inspect_existing(
                &mut store,
                &scope,
                &chain,
                None,
                Instant::now() + Duration::from_secs(5)
            )
            .is_err()
    );
    assert!(
        log.inspect_existing(
            &mut store,
            &denied,
            &chain,
            None,
            Instant::now() + Duration::from_secs(5)
        )
        .is_err()
    );
    assert_eq!(
        log.inspect_existing(
            &mut store,
            &scope,
            &chain,
            None,
            Instant::now() - Duration::from_secs(1)
        ),
        Err(Failure::Deadline)
    );
    assert_eq!(snapshot(&store), before);
}

#[test]
fn atomically_publishes_related_state_head_and_authenticated_record_then_reopens() {
    let (dir, mut store, scope, log) = fresh();
    let chain = "a".repeat(64);
    let first = log
        .append(
            &mut store,
            &scope,
            &chain,
            0,
            "opaque event é",
            &Batch {
                checks: vec![],
                writes: vec![mutation("domain", "intent", 0, Some("committed intent"))],
            },
        )
        .unwrap();
    assert_eq!(first.receipt.revision, first.checkpoint.head_revision);
    assert_eq!(first.checkpoint.count, 1);
    for (namespace, key) in [
        ("heads", chain.clone()),
        ("entries", entry_key(&chain, 0)),
        ("domain", "intent".into()),
    ] {
        assert_eq!(
            store.get(&scope, namespace, &key).unwrap().revision,
            first.receipt.revision
        );
    }
    let second = log
        .append(
            &mut store,
            &scope,
            &chain,
            first.checkpoint.head_revision,
            "next observation",
            &empty(),
        )
        .unwrap();
    let before = snapshot(&store);
    assert_eq!(
        verify(&log, &mut store, &scope, &chain, Some(&second.checkpoint)).unwrap(),
        second.checkpoint
    );
    assert_eq!(snapshot(&store), before);
    assert!(!before.1.contains(std::str::from_utf8(KEY).unwrap()));
    let text: String = store
        .connection
        .query_row(
            "SELECT group_concat(CAST(value AS TEXT),'') FROM records",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(!text.contains(std::str::from_utf8(KEY).unwrap()));
    drop(store);
    let mut store = Store::open(dir.path(), OpenMode::Existing, Limits::default()).unwrap();
    let scope = grant(&store, Access::ReadWrite);
    assert_eq!(
        verify(&log, &mut store, &scope, &chain, None),
        Err(Failure::Observed(ObservedError::precheck(Error::Denied)))
    );
    let log = AuthenticatedLog::new(&store, "heads", "entries", limits(), KEY).unwrap();
    assert_eq!(
        verify(&log, &mut store, &scope, &chain, Some(&second.checkpoint)).unwrap(),
        second.checkpoint
    );
    let third = log
        .append(
            &mut store,
            &scope,
            &chain,
            second.checkpoint.head_revision,
            "after restart",
            &empty(),
        )
        .unwrap();
    assert_eq!(third.checkpoint.count, 3);
    assert!(verify(&log, &mut store, &scope, &chain, Some(&third.checkpoint)).is_ok());
}

#[test]
fn every_stale_or_denied_related_coordinate_prevents_partial_audit_publication() {
    for kind in ["head", "domain", "check", "denied", "reserved_namespace"] {
        let (_dir, mut store, scope, log) = fresh();
        let chain = "a".repeat(64);
        let first = log
            .append(&mut store, &scope, &chain, 0, "first", &empty())
            .unwrap();
        let mut related = Batch {
            checks: vec![],
            writes: vec![mutation("domain", "new", 0, Some("must stay absent"))],
        };
        let mut expected = first.checkpoint.head_revision;
        match kind {
            "head" => expected = 0,
            "domain" => related.writes[0].revision = 1,
            "check" => related.checks.push(Check {
                namespace: "domain".into(),
                key: "other".into(),
                revision: 1,
            }),
            "denied" => related
                .writes
                .push(mutation("other_tenant", "x", 0, Some("denied"))),
            "reserved_namespace" => {
                related
                    .writes
                    .push(mutation("entries", "other", 0, Some("not signed")))
            }
            _ => unreachable!(),
        }
        let before = snapshot(&store);
        assert!(
            log.append(
                &mut store,
                &scope,
                &chain,
                expected,
                "cannot commit",
                &related
            )
            .is_err(),
            "{kind}"
        );
        assert_eq!(snapshot(&store), before, "{kind}");
        assert!(verify(&log, &mut store, &scope, &chain, Some(&first.checkpoint)).is_ok());
    }
}

#[test]
fn valid_storage_checksums_do_not_hide_modified_or_unsigned_audit_content() {
    for kind in [
        "payload",
        "previous",
        "sequence",
        "chain",
        "version",
        "tag",
        "unsigned",
        "whitespace",
        "duplicate",
    ] {
        let (_dir, mut store, scope, log) = fresh();
        let chain = "a".repeat(64);
        log.append(&mut store, &scope, &chain, 0, "original", &empty())
            .unwrap();
        let key = entry_key(&chain, 0);
        let original = store.get(&scope, "entries", &key).unwrap().value.unwrap();
        let mut raw: serde_json::Value = serde_json::from_str(&original).unwrap();
        match kind {
            "payload" => raw["body"]["payload"] = "changed".into(),
            "previous" => raw["body"]["previous"] = "f".repeat(64).into(),
            "sequence" => raw["body"]["sequence"] = 1.into(),
            "chain" => raw["body"]["chain"] = "b".repeat(64).into(),
            "version" => raw["body"]["version"] = 2.into(),
            "tag" => raw["tag"] = "0".repeat(64).into(),
            "unsigned" => {
                raw.as_object_mut().unwrap().remove("tag");
            }
            _ => {}
        }
        // Keep original field ordering so mutation tests reach the MAC check,
        // not merely a different JSON representation.
        let changed = match kind {
            "whitespace" => format!(" {original}"),
            "duplicate" => original.replacen("{", "{\"tag\":\"duplicate\",", 1),
            "unsigned" => serde_json::to_string(&raw).unwrap(),
            _ => {
                let parsed: Signed<RecordBody> = serde_json::from_value(raw).unwrap();
                encoded(&parsed).unwrap()
            }
        };
        // Ordinary Store commits repair the unkeyed storage digest. Authentication
        // must still fail against a coherently modified storage record.
        write(&mut store, &scope, "entries", &key, Some(&changed));
        corrupted(verify(&log, &mut store, &scope, &chain, None));
        let before = snapshot(&store);
        let revision = store.get(&scope, "heads", &chain).unwrap().revision;
        assert_eq!(
            log.append(
                &mut store,
                &scope,
                &chain,
                revision,
                "do not bury corruption",
                &empty()
            )
            .unwrap_err(),
            Error::Corrupt
        );
        assert_eq!(snapshot(&store), before);
    }
}

#[test]
fn inner_corruption_is_found_by_complete_verification_even_when_tail_is_valid() {
    let (_dir, mut store, scope, log) = fresh();
    let chain = "a".repeat(64);
    let mut revision = 0;
    for i in 0..3 {
        revision = log
            .append(
                &mut store,
                &scope,
                &chain,
                revision,
                &format!("event {i}"),
                &empty(),
            )
            .unwrap()
            .checkpoint
            .head_revision;
    }
    write(
        &mut store,
        &scope,
        "entries",
        &entry_key(&chain, 1),
        Some("coherently checksummed garbage"),
    );
    let next = log
        .append(
            &mut store,
            &scope,
            &chain,
            revision,
            "tail append is NOT full verification",
            &empty(),
        )
        .unwrap();
    assert_eq!(next.checkpoint.count, 4);
    corrupted(verify(&log, &mut store, &scope, &chain, None));
}

#[test]
fn absent_deleted_or_orphaned_state_never_becomes_a_verified_empty_chain() {
    for kind in [
        "missing",
        "head_tombstone",
        "entry_tombstone",
        "orphan",
        "extra_entry",
    ] {
        let (_dir, mut store, scope, log) = fresh();
        let chain = "a".repeat(64);
        if kind != "missing" && kind != "orphan" {
            log.append(&mut store, &scope, &chain, 0, "event", &empty())
                .unwrap();
        }
        match kind {
            "head_tombstone" => write(&mut store, &scope, "heads", &chain, None),
            "entry_tombstone" => write(&mut store, &scope, "entries", &entry_key(&chain, 0), None),
            "orphan" | "extra_entry" => write(
                &mut store,
                &scope,
                "entries",
                &entry_key(&chain, 7),
                Some("orphan"),
            ),
            _ => {}
        }
        assert!(
            verify(&log, &mut store, &scope, &chain, None).is_err(),
            "{kind}"
        );
        if kind == "head_tombstone" || kind == "orphan" {
            let before = snapshot(&store);
            let revision = store.get(&scope, "heads", &chain).unwrap().revision;
            assert_eq!(
                log.append(
                    &mut store,
                    &scope,
                    &chain,
                    revision,
                    "not a reset",
                    &empty()
                )
                .unwrap_err(),
                Error::Corrupt
            );
            assert_eq!(snapshot(&store), before);
        }
    }
}

#[test]
fn signing_key_scope_process_and_storage_boot_are_independent_boundaries() {
    let (_dir, mut store, scope, mut log) = fresh();
    let chain = "a".repeat(64);
    let first = log
        .append(&mut store, &scope, &chain, 0, "event", &empty())
        .unwrap();
    let wrong = AuthenticatedLog::new(
        &store,
        "heads",
        "entries",
        limits(),
        b"different-native-key-that-is-long-enough",
    )
    .unwrap();
    corrupted(verify(&wrong, &mut store, &scope, &chain, None));
    let reader = grant(&store, Access::Read);
    assert!(verify(&log, &mut store, &reader, &chain, None).is_ok());
    assert_eq!(
        log.append(
            &mut store,
            &reader,
            &chain,
            first.checkpoint.head_revision,
            "denied",
            &empty()
        )
        .unwrap_err(),
        Error::Denied
    );
    let (_other, other_store, other_scope, _) = fresh();
    assert_eq!(
        verify(&log, &mut store, &other_scope, &chain, None),
        Err(Failure::Observed(ObservedError::precheck(Error::Denied)))
    );
    assert_ne!(store.boot, other_store.boot);
    log.process = log.process.wrapping_add(1);
    assert_eq!(
        verify(&log, &mut store, &scope, &chain, None),
        Err(Failure::Observed(ObservedError::precheck(Error::Denied)))
    );
}

#[test]
fn signatures_bind_both_namespaces_and_chain_identity() {
    let (_dir, mut store, scope, log) = fresh();
    let chain = "a".repeat(64);
    log.append(&mut store, &scope, &chain, 0, "event", &empty())
        .unwrap();
    let other = "b".repeat(64);
    let head = store.get(&scope, "heads", &chain).unwrap().value.unwrap();
    let entry = store
        .get(&scope, "entries", &entry_key(&chain, 0))
        .unwrap()
        .value
        .unwrap();
    write(&mut store, &scope, "heads", &other, Some(&head));
    write(
        &mut store,
        &scope,
        "entries",
        &entry_key(&other, 0),
        Some(&entry),
    );
    corrupted(verify(&log, &mut store, &scope, &other, None));
    let other_scope = store
        .scope(BTreeMap::from([
            ("other_heads".into(), Access::ReadWrite),
            ("other_entries".into(), Access::ReadWrite),
        ]))
        .unwrap();
    write(&mut store, &other_scope, "other_heads", &chain, Some(&head));
    write(
        &mut store,
        &other_scope,
        "other_entries",
        &entry_key(&chain, 0),
        Some(&entry),
    );
    let other_log =
        AuthenticatedLog::new(&store, "other_heads", "other_entries", limits(), KEY).unwrap();
    corrupted(verify(&other_log, &mut store, &other_scope, &chain, None));
}

#[test]
fn independent_checkpoints_detect_coherent_rollback_without_claiming_an_external_notary() {
    let (_dir, mut store, scope, log) = fresh();
    let chain = "a".repeat(64);
    let first = log
        .append(&mut store, &scope, &chain, 0, "first", &empty())
        .unwrap();
    let old_head = store.get(&scope, "heads", &chain).unwrap().value.unwrap();
    let second = log
        .append(
            &mut store,
            &scope,
            &chain,
            first.checkpoint.head_revision,
            "second",
            &empty(),
        )
        .unwrap();
    // Direct storage attacker restores an older internally authentic snapshot,
    // including the unkeyed record checksum/revision. No key is used here.
    store
        .connection
        .execute(
            "DELETE FROM records WHERE namespace='entries' AND key=?1",
            [entry_key(&chain, 1)],
        )
        .unwrap();
    store
        .connection
        .execute(
            "UPDATE records SET value=?1,digest=?2,revision=?3 WHERE namespace='heads' AND key=?4",
            params![
                old_head.as_bytes(),
                record_digest(
                    "heads",
                    &chain,
                    first.checkpoint.head_revision,
                    Some(&old_head)
                ),
                first.checkpoint.head_revision as i64,
                chain
            ],
        )
        .unwrap();
    assert!(verify(&log, &mut store, &scope, &chain, None).is_ok());
    assert_eq!(
        verify(&log, &mut store, &scope, &chain, Some(&second.checkpoint)),
        Err(Failure::Observed(ObservedError::storage(Error::Conflict)))
    );
}

#[test]
fn payload_record_byte_and_native_batch_ceilings_are_enforced_without_partial_writes() {
    let (_dir, mut store, scope, _) = fresh();
    let log = AuthenticatedLog::new(
        &store,
        "heads",
        "entries",
        ChainLimits {
            records: 1,
            ..limits()
        },
        KEY,
    )
    .unwrap();
    let chain = "a".repeat(64);
    let before = snapshot(&store);
    assert_eq!(
        log.append(&mut store, &scope, &chain, 0, &"x".repeat(1025), &empty())
            .unwrap_err(),
        Error::Limit
    );
    assert_eq!(snapshot(&store), before);
    let first = log
        .append(
            &mut store,
            &scope,
            &chain,
            0,
            &"\u{0000}".repeat(1024),
            &empty(),
        )
        .unwrap();
    assert!(first.checkpoint.bytes > 6144);
    let before = snapshot(&store);
    assert_eq!(
        log.append(
            &mut store,
            &scope,
            &chain,
            first.checkpoint.head_revision,
            "full",
            &empty()
        )
        .unwrap_err(),
        Error::Limit
    );
    assert_eq!(snapshot(&store), before);
    assert!(verify(&log, &mut store, &scope, &chain, Some(&first.checkpoint)).is_ok());
    let other = "b".repeat(64);
    let too_many = Batch {
        checks: vec![],
        writes: (0..63)
            .map(|i| mutation("domain", &format!("k{i}"), 0, Some("x")))
            .collect(),
    };
    assert_eq!(
        log.append(&mut store, &scope, &other, 0, "full batch", &too_many)
            .unwrap_err(),
        Error::Invalid
    );
    assert_eq!(snapshot(&store), before);
    let small = AuthenticatedLog::new(
        &store,
        "heads",
        "entries",
        ChainLimits {
            bytes: 7168,
            ..limits()
        },
        KEY,
    )
    .unwrap();
    let first = small
        .append(
            &mut store,
            &scope,
            &other,
            0,
            &"\u{0000}".repeat(1024),
            &empty(),
        )
        .unwrap();
    let before = snapshot(&store);
    assert_eq!(
        small
            .append(
                &mut store,
                &scope,
                &other,
                first.checkpoint.head_revision,
                &"\u{0000}".repeat(1024),
                &empty()
            )
            .unwrap_err(),
        Error::Limit
    );
    assert_eq!(snapshot(&store), before);
}

#[test]
fn config_changes_cannot_silently_reset_keys_or_widen_persisted_chain_limits() {
    let (_dir, mut store, scope, log) = fresh();
    let chain = "a".repeat(64);
    let first = log
        .append(&mut store, &scope, &chain, 0, "event", &empty())
        .unwrap();
    let changed = AuthenticatedLog::new(
        &store,
        "heads",
        "entries",
        ChainLimits {
            records: 999,
            ..limits()
        },
        KEY,
    )
    .unwrap();
    corrupted(verify(&changed, &mut store, &scope, &chain, None));
    let before = snapshot(&store);
    assert_eq!(
        changed
            .append(
                &mut store,
                &scope,
                &chain,
                first.checkpoint.head_revision,
                "not reconfigured",
                &empty()
            )
            .unwrap_err(),
        Error::Corrupt
    );
    assert_eq!(snapshot(&store), before);
    for key in [vec![], vec![1; 31], vec![1; 1025]] {
        assert!(AuthenticatedLog::new(&store, "heads", "entries", limits(), &key).is_err());
    }
    for bad in [
        ChainLimits {
            payload_bytes: 0,
            ..limits()
        },
        ChainLimits {
            payload_bytes: PAYLOAD_BYTES + 1,
            ..limits()
        },
        ChainLimits {
            records: 0,
            ..limits()
        },
        ChainLimits {
            records: CHAIN_RECORDS + 1,
            ..limits()
        },
        ChainLimits {
            bytes: 1,
            ..limits()
        },
        ChainLimits {
            bytes: CHAIN_BYTES + 1,
            ..limits()
        },
    ] {
        assert!(AuthenticatedLog::new(&store, "heads", "entries", bad, KEY).is_err());
    }
    assert!(AuthenticatedLog::new(&store, "heads", "heads", limits(), KEY).is_err());
}

#[test]
fn verification_deadlines_and_sqlite_work_limits_never_leave_handlers_or_mutations() {
    let (_dir, mut store, scope, log) = fresh();
    let chain = "a".repeat(64);
    let mut revision = 0;
    // Exercise enough work in the count query to cross the existing SQLite
    // progress-callback granularity (1000 steps); 50 rows did not do so.
    for _ in 0..300 {
        revision = log
            .append(&mut store, &scope, &chain, revision, "event", &empty())
            .unwrap()
            .checkpoint
            .head_revision;
    }
    let before = snapshot(&store);
    assert_eq!(
        log.verify(
            &mut store,
            &scope,
            &chain,
            None,
            Instant::now() - Duration::from_secs(1)
        ),
        Err(Failure::Deadline)
    );
    assert_eq!(
        log.verify_bounded(
            &mut store,
            &scope,
            &chain,
            None,
            Instant::now() + Duration::from_secs(5),
            1000
        ),
        Err(Failure::WorkLimit)
    );
    assert_eq!(snapshot(&store), before);
    assert!(verify(&log, &mut store, &scope, &chain, None).is_ok());
    assert!(
        log.append(
            &mut store,
            &scope,
            &chain,
            revision,
            "handler was removed",
            &empty()
        )
        .is_ok()
    );
}

#[test]
fn hmac_sha256_matches_rfc4231_and_rejects_changed_tag() {
    // RFC 4231 section 4.2. This short conformance-vector key is NOT accepted by
    // the product constructor, whose independent minimum remains 32 bytes.
    let mut mac = HmacSha256::new_from_slice(&[0x0b; 20]).unwrap();
    mac.update(b"Hi There");
    let tag =
        hash_bytes("b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7").unwrap();
    assert!(mac.clone().verify_slice(&tag).is_ok());
    let mut changed = tag;
    changed[0] ^= 1;
    assert!(mac.verify_slice(&changed).is_err());
}

#[test]
fn versioned_framing_matches_an_independent_python_stdlib_hmac_fixture() {
    // Independently computed from ordered UTF-8 JSON and big-endian u64 byte
    // lengths using Python's json/hashlib/hmac, not this sign/decode pair.
    let (_dir, mut store, scope, log) = fresh();
    let chain = "a".repeat(64);
    let published = log
        .append(&mut store, &scope, &chain, 0, "opaque event é", &empty())
        .unwrap();
    let raw = store
        .get(&scope, "entries", &entry_key(&chain, 0))
        .unwrap()
        .value
        .unwrap();
    let signed: Signed<RecordBody> = serde_json::from_str(&raw).unwrap();
    assert_eq!(
        signed.tag,
        "4a0ba50b5193cc9b55fa840a20ff341d1030b52a095bb62c2b603a189fa61d34"
    );
    assert_eq!(
        published.checkpoint.tip,
        "847abd11bb86ec07ac8eb9726daac71fd6eaaf134fdb4811caf3580e715f3a9d"
    );
    assert_eq!(published.checkpoint.bytes, 289);
}

#[test]
fn head_cas_revisions_are_not_confused_with_whole_store_commit_receipts() {
    let (_dir, mut store, scope, log) = fresh();
    write(&mut store, &scope, "domain", "unrelated", Some("one"));
    write(&mut store, &scope, "domain", "unrelated", Some("two"));
    let a = "a".repeat(64);
    let b = "b".repeat(64);
    let first = log
        .append(&mut store, &scope, &a, 0, "first", &empty())
        .unwrap();
    assert_eq!(first.receipt.revision, 3);
    assert_eq!(first.checkpoint.head_revision, 1);
    let other = log
        .append(&mut store, &scope, &b, 0, "other chain", &empty())
        .unwrap();
    assert_eq!(other.receipt.revision, 4);
    assert_eq!(other.checkpoint.head_revision, 1);
    let next = log
        .append(
            &mut store,
            &scope,
            &a,
            first.checkpoint.head_revision,
            "second",
            &empty(),
        )
        .unwrap();
    assert_eq!(next.receipt.revision, 5);
    assert_eq!(next.checkpoint.head_revision, 2);
    assert!(verify(&log, &mut store, &scope, &a, Some(&next.checkpoint)).is_ok());
    assert!(verify(&log, &mut store, &scope, &b, Some(&other.checkpoint)).is_ok());
}

#[test]
fn complete_thousand_record_verification_is_read_only_and_fits_the_native_budget() {
    let (_dir, mut store, scope, log) = fresh();
    let chain = "a".repeat(64);
    let mut head_revision = 0;
    let mut last = None;
    for _ in 0..1000 {
        let next = log
            .append(
                &mut store,
                &scope,
                &chain,
                head_revision,
                &"x".repeat(1024),
                &empty(),
            )
            .unwrap();
        head_revision = next.checkpoint.head_revision;
        last = Some(next.checkpoint);
    }
    let before = snapshot(&store);
    let verified = verify(&log, &mut store, &scope, &chain, last.as_ref()).unwrap();
    assert_eq!(verified.count, 1000);
    assert!(verified.bytes > 1024 * 1000);
    assert_eq!(snapshot(&store), before);
}

#[test]
fn authenticated_log_process_child() {
    use std::io::Write;
    let Ok(root) = std::env::var("SIGIL_AUTHENTICATED_LOG_TEST_ROOT") else {
        return;
    };
    let phase = std::env::var("SIGIL_AUTHENTICATED_LOG_TEST_PHASE").unwrap();
    let mut store = Store::open(Path::new(&root), OpenMode::Existing, Limits::default()).unwrap();
    let scope = grant(&store, Access::ReadWrite);
    let log = AuthenticatedLog::new(&store, "heads", "entries", limits(), KEY).unwrap();
    let related = Batch {
        checks: vec![],
        writes: vec![mutation("domain", "intent", 1, Some("new"))],
    };
    let park = || {
        println!("AUTHENTICATED_LOG_BOUNDARY_REACHED");
        std::io::stdout().flush().unwrap();
        loop {
            std::thread::park();
        }
    };
    if phase == "before_commit" {
        log.append_inner(
            &mut store,
            &scope,
            &"a".repeat(64),
            1,
            "second",
            &related,
            park,
        )
        .unwrap();
    } else if phase == "after_commit" {
        log.append(&mut store, &scope, &"a".repeat(64), 1, "second", &related)
            .unwrap();
        park();
    } else {
        panic!("unknown child phase");
    }
}

#[test]
fn actual_process_kills_preserve_atomic_audit_and_related_state_at_both_commit_boundaries() {
    use std::io::{BufRead, BufReader};
    use std::process::{Child, Command, Stdio};
    use std::sync::mpsc;
    struct ChildOwner(Child);
    impl Drop for ChildOwner {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    for phase in ["before_commit", "after_commit"] {
        let (dir, mut store, scope, log) = fresh();
        let chain = "a".repeat(64);
        log.append(
            &mut store,
            &scope,
            &chain,
            0,
            "first",
            &Batch {
                checks: vec![],
                writes: vec![mutation("domain", "intent", 0, Some("old"))],
            },
        )
        .unwrap();
        let before = snapshot(&store);
        drop(store);
        let mut child = ChildOwner(
            Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "store::authenticated_log::tests::authenticated_log_process_child",
                    "--nocapture",
                ])
                .env("SIGIL_AUTHENTICATED_LOG_TEST_ROOT", dir.path())
                .env("SIGIL_AUTHENTICATED_LOG_TEST_PHASE", phase)
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
                if line.contains("AUTHENTICATED_LOG_BOUNDARY_REACHED") {
                    let _ = sender.send(());
                    break;
                }
            }
        });
        receiver
            .recv_timeout(Duration::from_secs(15))
            .expect("real authenticated transaction boundary not reached");
        child.0.kill().unwrap();
        assert!(!child.0.wait().unwrap().success());
        reader.join().unwrap();
        let mut reopened = Store::open(dir.path(), OpenMode::Existing, Limits::default()).unwrap();
        let scope = grant(&reopened, Access::ReadWrite);
        let log = AuthenticatedLog::new(&reopened, "heads", "entries", limits(), KEY).unwrap();
        let checked = verify(&log, &mut reopened, &scope, &chain, None).unwrap();
        let expected = if phase == "before_commit" {
            "old"
        } else {
            "new"
        };
        assert_eq!(
            reopened
                .get(&scope, "domain", "intent")
                .unwrap()
                .value
                .as_deref(),
            Some(expected)
        );
        assert_eq!(checked.count, if phase == "before_commit" { 1 } else { 2 });
        if phase == "before_commit" {
            assert_eq!(snapshot(&reopened), before);
        }
        let tail = reopened
            .get(&scope, "entries", &entry_key(&chain, 1))
            .unwrap();
        assert_eq!(tail.value.is_some(), phase == "after_commit");
    }
}
