use super::*;
use sigil_durable_store::store::{Access, Limits, OpenMode};
use std::collections::BTreeMap;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::time::Duration;
use tempfile::TempDir;

fn fixture() -> (TempDir, Store, Scope) {
    let directory = tempfile::tempdir().unwrap();
    fs::set_permissions(directory.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let store = Store::open(directory.path(), OpenMode::CreateNew, Limits::default()).unwrap();
    let scope = store
        .scope(BTreeMap::from([("scope".to_owned(), Access::ReadWrite)]))
        .unwrap();
    (directory, store, scope)
}
fn window(before: &str, until: &str) -> String {
    frame("TG1\n", &[before, until]).unwrap()
}
fn command(kind: &str, a: &str, b: &str, continuation: &str, guard: &str) -> String {
    frame("HC3\n", &[kind, a, b, continuation, guard]).unwrap()
}
fn commit(guard: &str) -> String {
    command(
        "commit",
        r#"{"op":"commit","checks":[],"writes":[{"namespace":"scope","key":"a","revision":0,"value":"first"},{"namespace":"scope","key":"b","revision":0,"value":"second"}]}"#,
        "",
        "held",
        guard,
    )
}
fn future() -> Instant {
    Instant::now() + Duration::from_secs(30)
}

fn metadata(namespace: &str, query: &str, guard: &str) -> String {
    frame("HC4\n", &["metadata", namespace, query, "list-held", guard]).unwrap()
}

#[test]
fn metadata_contract_is_explicit_and_cannot_silently_upgrade_old_commands() {
    assert_eq!(EntryContract::Legacy.envelope_marker(), "AH3\n");
    assert_eq!(EntryContract::Legacy.command_inventory(), None);
    assert_eq!(EntryContract::Metadata.envelope_marker(), "AH4\n");
    assert_eq!(
        EntryContract::Metadata.command_inventory(),
        Some(r#"["call","commit","metadata","read","reply"]"#)
    );
    let query = r#"{"after":null,"limit":2}"#;
    let guard = window("100", "200");
    for (contract, raw) in [
        (EntryContract::Legacy, metadata("scope", query, &guard)),
        (
            EntryContract::Legacy,
            command("metadata", "scope", query, "held", &guard),
        ),
        (
            EntryContract::Metadata,
            command("read", "scope", "a", "held", &guard),
        ),
        (
            EntryContract::Metadata,
            frame("HC4\n", &["metadata", "scope", query, "held"]).unwrap(),
        ),
        (
            EntryContract::Metadata,
            metadata("scope", query, &guard) + "trailing",
        ),
    ] {
        let mut high = 100;
        assert_eq!(
            perform_with_contract(contract, None, None, &mut high, future(), &raw, || panic!(
                "incompatible frame reached action boundary"
            ))
            .unwrap_err(),
            "protocol"
        );
        assert_eq!(high, 100);
    }
}

#[test]
fn metadata_observation_comes_from_scoped_records_without_exposing_payloads() {
    let (dir, mut store, scope) = fixture();
    let mut high = 100;
    perform(
        Some(&mut store),
        Some(&scope),
        &mut high,
        future(),
        &commit(&window("100", "200")),
        || Ok(100),
    )
    .unwrap();
    let read_only = store
        .scope(BTreeMap::from([("scope".to_owned(), Access::Read)]))
        .unwrap();
    for (query, expected_key, next) in [
        (r#"{"after":null,"limit":1}"#, "a", "a"),
        (r#"{"after":"a","limit":2}"#, "b", ""),
    ] {
        let Step::Continue {
            stage,
            observation,
            continuation,
        } = perform_with_contract(
            EntryContract::Metadata,
            Some(&mut store),
            Some(&read_only),
            &mut high,
            future(),
            &metadata("scope", query, &window("100", "200")),
            || Ok(150),
        )
        .unwrap()
        else {
            panic!("metadata did not return an observation");
        };
        assert_eq!(stage, "metadata");
        assert_eq!(continuation, "list-held");
        let seen = fields(&observation, "KM1\n", 3).unwrap();
        assert_eq!(seen[0], "ok");
        assert_eq!(seen[2], next);
        let entries: serde_json::Value = serde_json::from_str(seen[1]).unwrap();
        assert_eq!(
            entries,
            serde_json::json!([{
                "key":expected_key, "revision":"1", "present":true,
                "value_bytes":if expected_key == "a" { 5 } else { 6 }
            }])
        );
        assert!(!observation.contains("first") && !observation.contains("second"));
    }
    assert_eq!(store.get(&scope, "scope", "a").unwrap().revision, 1);
    assert_eq!(store.get(&scope, "scope", "b").unwrap().revision, 1);
    drop(store);
    let store = Store::open(dir.path(), OpenMode::Existing, Limits::default()).unwrap();
    let scope = store
        .scope(BTreeMap::from([("scope".to_owned(), Access::Read)]))
        .unwrap();
    assert_eq!(
        store.get(&scope, "scope", "a").unwrap().value.as_deref(),
        Some("first")
    );
    assert_eq!(store.get(&scope, "scope", "b").unwrap().revision, 1);
}

#[test]
fn metadata_requires_actual_read_scope_and_cannot_run_during_bootstrap() {
    let (_dir, mut store, scope) = fixture();
    let create_only = store
        .scope(BTreeMap::from([("scope".to_owned(), Access::CreateOnly)]))
        .unwrap();
    let other = store
        .scope(BTreeMap::from([("other".to_owned(), Access::Read)]))
        .unwrap();
    let query = r#"{"limit":1}"#;
    let raw = metadata("scope", query, &window("100", "200"));
    let mut high = 100;
    for (handle, grant) in [(None, None), (Some(&mut store), None)] {
        assert_eq!(
            perform_with_contract(
                EntryContract::Metadata,
                handle,
                grant,
                &mut high,
                future(),
                &raw,
                || Ok(150)
            )
            .unwrap_err(),
            "capability"
        );
    }
    assert_eq!(
        perform_with_contract(
            EntryContract::Metadata,
            None,
            Some(&scope),
            &mut high,
            future(),
            &raw,
            || Ok(150)
        )
        .unwrap_err(),
        "capability"
    );
    for (grant, namespace, args) in [
        (&create_only, "scope", query),
        (&other, "scope", query),
        (&scope, "other", query),
        (&scope, "../scope", query),
        (&scope, "scope", r#"{"limit":0}"#),
        (&scope, "scope", r#"{"limit":129}"#),
        (&scope, "scope", r#"{"after":"../forged","limit":1}"#),
    ] {
        let Step::Continue { observation, .. } = perform_with_contract(
            EntryContract::Metadata,
            Some(&mut store),
            Some(grant),
            &mut high,
            future(),
            &metadata(namespace, args, &window("100", "200")),
            || Ok(150),
        )
        .unwrap() else {
            panic!("expected scoped refusal observation");
        };
        assert_eq!(
            fields(&observation, "KM1\n", 3).unwrap(),
            ["error", "[]", ""]
        );
    }
}

#[test]
fn metadata_arguments_and_guards_are_strict_before_any_clock_or_storage_action() {
    let (_dir, mut store, scope) = fixture();
    let guard = window("100", "200");
    let mut malformed = Vec::new();
    for query in [
        "",
        "[]",
        "{}",
        r#"{"limit":1,"limit":2}"#,
        r#"{"after":null,"after":"a","limit":1}"#,
        r#"{"limit":"1"}"#,
        r#"{"limit":true}"#,
        r#"{"limit":-1}"#,
        r#"{"limit":1.0}"#,
        r#"{"limit":1,"after":1}"#,
        r#"{"limit":1,"namespace":"forged"}"#,
        r#"{"limit":1,"observation":"forged"}"#,
        r#"{"limit":18446744073709551616}"#,
    ] {
        malformed.push(metadata("scope", query, &guard));
    }
    malformed.push(metadata("scope", r#"{"limit":1}"#, ""));
    malformed.push(metadata("scope", r#"{"limit":1}"#, "forged"));
    for raw in malformed {
        let mut high = 100;
        assert_eq!(
            perform_with_contract(
                EntryContract::Metadata,
                Some(&mut store),
                Some(&scope),
                &mut high,
                future(),
                &raw,
                || panic!("malformed metadata reached action boundary")
            )
            .unwrap_err(),
            "protocol"
        );
        assert_eq!(high, 100);
    }
}

#[test]
fn metadata_uses_fresh_expiry_deadline_and_monotonic_clock_checks() {
    let (_dir, mut store, scope) = fixture();
    let raw = metadata("scope", r#"{"limit":1}"#, &window("100", "200"));
    for at in [99, 200, 201] {
        let mut high = 0;
        assert_eq!(
            perform_with_contract(
                EntryContract::Metadata,
                Some(&mut store),
                Some(&scope),
                &mut high,
                future(),
                &raw,
                || Ok(at)
            )
            .unwrap_err(),
            "time_guard"
        );
        assert_eq!(high, at);
    }
    for clock in [Ok(149), Err("clock")] {
        let mut high = 150;
        assert_eq!(
            perform_with_contract(
                EntryContract::Metadata,
                Some(&mut store),
                Some(&scope),
                &mut high,
                future(),
                &raw,
                || clock
            )
            .unwrap_err(),
            "clock"
        );
        assert_eq!(high, 150);
    }
    let mut high = 100;
    assert_eq!(
        perform_with_contract(
            EntryContract::Metadata,
            Some(&mut store),
            Some(&scope),
            &mut high,
            Instant::now() - Duration::from_millis(1),
            &raw,
            || panic!("expired host ceiling reached clock")
        )
        .unwrap_err(),
        "deadline"
    );
    let deadline = Instant::now() + Duration::from_millis(2);
    assert_eq!(
        perform_with_contract(
            EntryContract::Metadata,
            Some(&mut store),
            Some(&scope),
            &mut high,
            deadline,
            &raw,
            || {
                std::thread::sleep(Duration::from_millis(5));
                Ok(150)
            }
        )
        .unwrap_err(),
        "deadline"
    );
    for at in [100, 199] {
        let mut high = 100;
        assert!(
            perform_with_contract(
                EntryContract::Metadata,
                Some(&mut store),
                Some(&scope),
                &mut high,
                future(),
                &raw,
                || Ok(at)
            )
            .is_ok()
        );
    }
}

#[test]
fn metadata_profile_preserves_existing_actions_with_the_new_explicit_marker() {
    let (_dir, mut store, scope) = fixture();
    let guard = window("100", "200");
    let mut high = 100;
    let raw = commit(&guard).replacen("HC3\n", "HC4\n", 1);
    let Step::Continue { observation, .. } = perform_with_contract(
        EntryContract::Metadata,
        Some(&mut store),
        Some(&scope),
        &mut high,
        future(),
        &raw,
        || Ok(150),
    )
    .unwrap() else {
        panic!("expected commit");
    };
    assert_eq!(fields(&observation, "SC1\n", 2).unwrap(), ["ok", "1"]);
    let raw = frame("HC4\n", &["read", "scope", "a", "held", &guard]).unwrap();
    let Step::Continue { observation, .. } = perform_with_contract(
        EntryContract::Metadata,
        Some(&mut store),
        Some(&scope),
        &mut high,
        future(),
        &raw,
        || Ok(150),
    )
    .unwrap() else {
        panic!("expected read");
    };
    assert_eq!(
        fields(&observation, "SR1\n", 3).unwrap(),
        ["ok", "1", "first"]
    );
    let raw = frame(
        "HC4\n",
        &["call", "listing", "selected-data", "held", &guard],
    )
    .unwrap();
    let Step::Call {
        target,
        input,
        continuation,
    } = perform_with_contract(
        EntryContract::Metadata,
        None,
        None,
        &mut high,
        future(),
        &raw,
        || Ok(150),
    )
    .unwrap()
    else {
        panic!("expected pure function call");
    };
    assert_eq!(
        (target.as_str(), input.as_str(), continuation.as_str()),
        ("listing", "selected-data", "held")
    );
    let raw = frame("HC4\n", &["reply", "401", "refused", "", ""]).unwrap();
    let Step::Reply { reply, guard } = perform_with_contract(
        EntryContract::Metadata,
        None,
        None,
        &mut high,
        future(),
        &raw,
        || Ok(150),
    )
    .unwrap() else {
        panic!("expected refusal reply");
    };
    assert_eq!(reply.status, 401);
    assert_eq!(reply.body, "refused");
    assert!(guard.is_none());
}

#[test]
fn guard_codec_is_canonical_bounded_and_nonempty() {
    assert_eq!(
        Guard::parse(&window("0", "9007199254740991")).unwrap(),
        Guard {
            before: 0,
            until: 9_007_199_254_740_991
        }
    );
    for (before, until) in [
        ("", "200"),
        ("01", "200"),
        ("+1", "200"),
        ("-1", "200"),
        (" 1", "200"),
        ("1 ", "200"),
        ("١", "200"),
        ("1.0", "200"),
        ("1e1", "200"),
        ("1", "0200"),
        ("1", "9007199254740992"),
        ("1", "18446744073709551616"),
        ("2", "2"),
        ("2", "1"),
        ("0", "0"),
    ] {
        assert!(
            Guard::parse(&window(before, until)).is_err(),
            "{before:?} {until:?}"
        );
    }
    for raw in ["", "TG2\n000000011000000012", "TG1\n00000001é000000012"] {
        assert!(Guard::parse(raw).is_err());
    }
    assert!(Guard::parse(&(window("1", "2") + "x")).is_err());
    assert!(Guard::parse(&frame("TG1\n", &["1", "2", "3"]).unwrap()).is_err());
}

#[test]
fn guard_has_inclusive_start_and_exclusive_end() {
    let guard = Guard::parse(&window("100", "200")).unwrap();
    assert_eq!(guard.check(99), Err("time_guard"));
    assert_eq!(guard.check(100), Ok(()));
    assert_eq!(guard.check(199), Ok(()));
    assert_eq!(guard.check(200), Err("time_guard"));
    assert_eq!(guard.check(u64::MAX), Err("time_guard"));
}

#[test]
fn expired_or_future_commands_never_touch_the_actual_store() {
    let (_dir, mut store, scope) = fixture();
    for at in [99, 200, 201] {
        let mut high = 0;
        let error = perform(
            Some(&mut store),
            Some(&scope),
            &mut high,
            future(),
            &commit(&window("100", "200")),
            || Ok(at),
        )
        .unwrap_err();
        assert_eq!(error, "time_guard");
        for key in ["a", "b"] {
            let r = store.get(&scope, "scope", key).unwrap();
            assert_eq!(r.revision, 0);
            assert!(r.value.is_none());
        }
        // Rejected time bounds do not roll back the observed clock high-water.
        assert_eq!(high, at);
    }
}

#[test]
fn valid_guard_preserves_atomic_cas_and_the_real_commit_observation() {
    let (_dir, mut store, scope) = fixture();
    let raw = commit(&window("100", "200"));
    let mut high = 100;
    for (at, expected) in [(100, vec!["ok", "1"]), (199, vec!["error", "0"])] {
        let Step::Continue {
            stage,
            observation,
            continuation,
        } = perform(
            Some(&mut store),
            Some(&scope),
            &mut high,
            future(),
            &raw,
            || Ok(at),
        )
        .unwrap()
        else {
            panic!("commit returned a reply");
        };
        assert_eq!(stage, "commit");
        assert_eq!(continuation, "held");
        assert_eq!(fields(&observation, "SC1\n", 2).unwrap(), expected);
    }
    for (key, value) in [("a", "first"), ("b", "second")] {
        let r = store.get(&scope, "scope", key).unwrap();
        assert_eq!(r.revision, 1);
        assert_eq!(r.value.as_deref(), Some(value));
    }
}

#[test]
fn unguarded_io_and_legacy_commands_are_rejected_before_execution() {
    let (_dir, mut store, scope) = fixture();
    let mut high = 100;
    for raw in [
        commit(""),
        command("read", "scope", "a", "held", ""),
        frame("HC1\n", &["commit", "{}", "", "held"]).unwrap(),
        frame("HC3\n", &["read", "scope", "a", "held"]).unwrap(),
        command("commit", "{}", "", "held", &window("100", "100")),
        command("read", "scope", "a", "held", "forged"),
    ] {
        assert_eq!(
            perform(
                Some(&mut store),
                Some(&scope),
                &mut high,
                future(),
                &raw,
                || panic!("malformed command reached action boundary")
            )
            .unwrap_err(),
            "protocol"
        );
    }
    assert_eq!(store.get(&scope, "scope", "a").unwrap().revision, 0);
}

#[test]
fn time_guard_cannot_mint_or_widen_a_storage_scope() {
    let (_dir, mut store, scope) = fixture();
    let mut high = 100;
    assert_eq!(
        perform(
            Some(&mut store),
            None,
            &mut high,
            future(),
            &commit(&window("100", "200")),
            || Ok(150)
        )
        .unwrap_err(),
        "capability"
    );
    let read_only = store
        .scope(BTreeMap::from([("scope".to_owned(), Access::Read)]))
        .unwrap();
    let Step::Continue { observation, .. } = perform(
        Some(&mut store),
        Some(&read_only),
        &mut high,
        future(),
        &commit(&window("100", "200")),
        || Ok(150),
    )
    .unwrap() else {
        panic!("expected actual scope refusal");
    };
    assert_eq!(fields(&observation, "SC1\n", 2).unwrap(), ["error", "0"]);
    assert_eq!(store.get(&scope, "scope", "a").unwrap().revision, 0);
}

#[test]
fn elapsed_host_deadline_after_worker_result_prevents_action() {
    let (_dir, mut store, scope) = fixture();
    let mut high = 100;
    let expired = Instant::now() - Duration::from_millis(1);
    assert_eq!(
        perform(
            Some(&mut store),
            Some(&scope),
            &mut high,
            expired,
            &commit(&window("100", "200")),
            || panic!("expired host ceiling should fail before clock read")
        )
        .unwrap_err(),
        "deadline"
    );
    assert_eq!(store.get(&scope, "scope", "a").unwrap().revision, 0);
}

#[test]
fn elapsed_host_deadline_during_clock_read_prevents_action() {
    let (_dir, mut store, scope) = fixture();
    let mut high = 100;
    let deadline = Instant::now() + Duration::from_millis(2);
    assert_eq!(
        perform(
            Some(&mut store),
            Some(&scope),
            &mut high,
            deadline,
            &commit(&window("100", "200")),
            || {
                std::thread::sleep(Duration::from_millis(5));
                Ok(150)
            }
        )
        .unwrap_err(),
        "deadline"
    );
    assert_eq!(store.get(&scope, "scope", "a").unwrap().revision, 0);
}

#[test]
fn clock_failure_or_rollback_never_starts_a_write() {
    let (_dir, mut store, scope) = fixture();
    let mut high = 150;
    let raw = commit(&window("100", "200"));
    for clock in [Ok(149), Err("clock")] {
        assert_eq!(
            perform(
                Some(&mut store),
                Some(&scope),
                &mut high,
                future(),
                &raw,
                || { clock }
            )
            .unwrap_err(),
            "clock"
        );
        assert_eq!(high, 150);
        assert_eq!(store.get(&scope, "scope", "a").unwrap().revision, 0);
    }
}

#[test]
fn replies_and_reads_obey_the_selected_guard_too() {
    let (_dir, mut store, scope) = fixture();
    let mut high = 150;
    for raw in [
        command("read", "scope", "a", "held", &window("100", "200")),
        command("reply", "200", "protected", "", &window("100", "200")),
    ] {
        assert!(
            perform(
                Some(&mut store),
                Some(&scope),
                &mut high,
                future(),
                &raw,
                || Ok(199)
            )
            .is_ok()
        );
    }
    for raw in [
        command("read", "scope", "a", "held", &window("100", "200")),
        command("reply", "200", "protected", "", &window("100", "200")),
    ] {
        assert_eq!(
            perform(
                Some(&mut store),
                Some(&scope),
                &mut high,
                future(),
                &raw,
                || Ok(200)
            )
            .unwrap_err(),
            "time_guard"
        );
    }
    // The host interprets neither response bodies nor product status semantics.
    let raw = command("reply", "401", "refused", "", "");
    let Step::Reply { reply, .. } =
        perform(Some(&mut store), None, &mut high, future(), &raw, || {
            Ok(200)
        })
        .unwrap()
    else {
        panic!("expected refusal reply");
    };
    assert_eq!(reply.status, 401);
    assert_eq!(reply.body, "refused");
}

#[test]
fn expiry_before_reply_does_not_undo_an_already_committed_decision() {
    let (dir, mut store, scope) = fixture();
    let mut high = 150;
    assert!(
        perform(
            Some(&mut store),
            Some(&scope),
            &mut high,
            future(),
            &commit(&window("100", "200")),
            || Ok(199)
        )
        .is_ok()
    );
    let raw = command("reply", "202", "accepted", "", &window("100", "200"));
    assert_eq!(
        perform(
            Some(&mut store),
            Some(&scope),
            &mut high,
            future(),
            &raw,
            || Ok(200)
        )
        .unwrap_err(),
        "time_guard"
    );
    drop(scope);
    drop(store);
    let store = Store::open(dir.path(), OpenMode::Existing, Limits::default()).unwrap();
    let scope = store
        .scope(BTreeMap::from([("scope".to_owned(), Access::Read)]))
        .unwrap();
    assert_eq!(
        store.get(&scope, "scope", "a").unwrap().value.as_deref(),
        Some("first")
    );
    assert_eq!(store.get(&scope, "scope", "b").unwrap().revision, 1);
}

#[test]
fn strict_command_validation_still_precedes_native_action() {
    let (_dir, mut store, scope) = fixture();
    let mut high = 100;
    let guard = window("100", "200");
    for raw in [
        command("reply", "+200", "", "", ""),
        command("reply", "0200", "", "", ""),
        command("reply", "199", "", "", ""),
        command("reply", "600", "", "", ""),
        command("reply", "200", "", "forged", ""),
        command("unknown", "", "", "", &guard),
        command("commit", "{}", "extra", "held", &guard),
        command(
            "commit",
            r#"{"op":"commit","checks":[],"checks":[],"writes":[]}"#,
            "",
            "held",
            &guard,
        ),
        command(
            "commit",
            r#"{"op":"read","checks":[],"writes":[]}"#,
            "",
            "held",
            &guard,
        ),
        command(
            "commit",
            r#"{"op":"commit","checks":[],"writes":[],"scope":"forged"}"#,
            "",
            "held",
            &guard,
        ),
        command(
            "commit",
            r#"{"op":"commit","checks":[],"writes":[{"namespace":"scope","key":"a","revision":0}]}"#,
            "",
            "held",
            &guard,
        ),
    ] {
        assert_eq!(
            perform(
                Some(&mut store),
                Some(&scope),
                &mut high,
                future(),
                &raw,
                || panic!("malformed command reached action boundary")
            )
            .unwrap_err(),
            "protocol"
        );
    }
    assert_eq!(store.get(&scope, "scope", "a").unwrap().revision, 0);
}

#[test]
fn bootstrap_can_return_a_guarded_call_but_has_no_storage_handle() {
    let (_dir, mut store, scope) = fixture();
    let mut high = 100;
    let raw = command(
        "call",
        "function",
        "opaque input",
        "held",
        &window("100", "200"),
    );
    let Step::Call {
        target,
        input,
        continuation,
    } = perform(None, None, &mut high, future(), &raw, || Ok(150)).unwrap()
    else {
        panic!("expected pure call request");
    };
    assert_eq!(
        (target.as_str(), input.as_str(), continuation.as_str()),
        ("function", "opaque input", "held")
    );
    assert_eq!(
        perform(
            None,
            Some(&scope),
            &mut high,
            future(),
            &commit(&window("100", "200")),
            || Ok(150)
        )
        .unwrap_err(),
        "capability"
    );
    assert_eq!(store.get(&scope, "scope", "a").unwrap().revision, 0);
    let raw = command("call", "function", "opaque input", "held", "");
    assert_eq!(
        perform(
            Some(&mut store),
            Some(&scope),
            &mut high,
            future(),
            &raw,
            || Ok(150)
        )
        .unwrap_err(),
        "protocol"
    );
}

#[test]
fn guarded_version_two_commands_are_not_silently_upgraded() {
    let (_dir, mut store, scope) = fixture();
    let mut high = 100;
    let raw = frame(
        "HC2\n",
        &["read", "scope", "a", "held", &window("100", "200")],
    )
    .unwrap();
    assert_eq!(
        perform(
            Some(&mut store),
            Some(&scope),
            &mut high,
            future(),
            &raw,
            || panic!("legacy protocol reached native action")
        )
        .unwrap_err(),
        "protocol"
    );
}
