use super::*;
use crate::process_facts::{self, Config, ProcessFacts};
use sigil_durable_store::store::volatile::VolatileLimits;
use sigil_durable_store::store::{Access, Limits, OpenMode};
use std::cell::Cell;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::time::Duration;
use tempfile::TempDir;

fn config() -> Config {
    Config {
        namespaces: vec!["a".into(), "b".into()],
        lifecycle: None,
        per_namespace: VolatileLimits {
            value_bytes: 1024,
            records: 2,
            live_bytes: 2048,
        },
    }
}

fn fixture() -> (TempDir, Store, Scope, ProcessFacts) {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().join("store");
    fs::create_dir(&root).unwrap();
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    let store = Store::open(&root, OpenMode::CreateNew, Limits::default()).unwrap();
    let scope = store
        .scope([("a".into(), Access::ReadWrite)].into())
        .unwrap();
    let process = ProcessFacts::open(&store, config()).unwrap();
    (dir, store, scope, process)
}

fn header_policy() -> crate::http_exchange::HeaderPolicy {
    crate::http_exchange::HeaderPolicy::new(&["x-request-id", "retry-after"]).unwrap()
}

fn guard() -> String {
    frame("TG1\n", &["100", "200"]).unwrap()
}

fn command(name: &str, a: &str, b: &str, guard: &str) -> String {
    frame("HC7\n", &[name, a, b, "held", guard]).unwrap()
}

fn mutation(revision: u64, value: &str) -> String {
    serde_json::json!({"namespace":"a", "key":"key", "revision":revision, "value":value})
        .to_string()
}

fn batch(revision: u64, value: &str) -> String {
    format!(
        r#"{{"op":"commit","checks":[],"writes":[{}]}}"#,
        mutation(revision, value)
    )
}

fn execute(
    store: &mut Store,
    scope: &Scope,
    process: &mut ProcessFacts,
    raw: &str,
) -> Result<Step> {
    perform_with_process_facts(
        (
            EntryContract::ProcessFacts,
            Some(&header_policy()),
            Some(process),
        ),
        Some(store),
        Some(scope),
        &mut 100,
        Instant::now() + Duration::from_secs(15),
        raw,
        || Ok(150),
    )
}

fn observed(step: Step, expected_stage: &str, marker: &str, count: usize) -> Vec<String> {
    let Step::Continue {
        stage,
        observation,
        continuation,
    } = step
    else {
        panic!("not an observation")
    };
    assert_eq!(stage, expected_stage);
    assert_eq!(continuation, "held");
    fields(&observation, marker, count)
        .unwrap()
        .into_iter()
        .map(str::to_owned)
        .collect()
}

#[test]
fn process_configuration_requires_the_explicit_profile_and_cannot_upgrade_v8() {
    for old in [
        EntryContract::Legacy,
        EntryContract::Metadata,
        EntryContract::HttpExchange,
        EntryContract::RequestAdmission,
    ] {
        assert_eq!(process_facts::require_profile(old, None), Ok(()));
        assert_eq!(
            process_facts::require_profile(old, Some(&config())),
            Err("config")
        );
    }
    assert_eq!(
        process_facts::require_profile(EntryContract::ProcessFacts, None),
        Err("config")
    );
    assert_eq!(
        process_facts::require_profile(EntryContract::ProcessFacts, Some(&config())),
        Ok(())
    );
    assert_eq!(EntryContract::ProcessFacts.envelope_marker(), "AH7\n");
    assert_eq!(EntryContract::ProcessFacts.command_marker(), "HC7\n");
    assert_eq!(EntryContract::RequestAdmission.command_marker(), "HC6\n");
}

#[test]
fn process_configuration_rejects_null_unknown_fields_missing_limits_and_duplicate_names() {
    #[derive(serde::Deserialize)]
    struct Envelope {
        #[serde(default, deserialize_with = "crate::explicit_process_config")]
        process_facts: Option<Config>,
    }
    assert!(
        serde_json::from_str::<Envelope>("{}")
            .unwrap()
            .process_facts
            .is_none()
    );
    for value in [
        serde_json::Value::Null,
        serde_json::json!({}),
        serde_json::json!({"namespaces":["a"],"per_namespace":{"value_bytes":1,"records":1}}),
        serde_json::json!({"namespaces":["a"],"per_namespace":{"value_bytes":1,"records":1,"live_bytes":1,"reset":true}}),
        serde_json::json!({"namespaces":["a"],"per_namespace":{"value_bytes":1,"records":1,"live_bytes":1},"reset":true}),
    ] {
        assert!(
            serde_json::from_value::<Envelope>(serde_json::json!({"process_facts":value})).is_err()
        );
    }
    let good =
        serde_json::from_value::<Envelope>(serde_json::json!({"process_facts":config()})).unwrap();
    assert_eq!(good.process_facts.unwrap().namespaces, ["a", "b"]);
}

fn writers() -> Vec<crate::CredentialConfig> {
    vec![crate::CredentialConfig {
        sha256: "a".repeat(64),
        facts: "SIGIL validates identity policy".into(),
        grants: [
            ("a".into(), Access::ReadWrite),
            ("b".into(), Access::ReadWrite),
        ]
        .into(),
    }]
}

#[test]
fn declaring_temporary_capacity_requires_existing_writer_authority_and_independent_bounds() {
    assert_eq!(config().validate(&Limits::default(), &writers()), Ok(()));
    for names in [
        vec![],
        vec!["a".into(), "a".into()],
        vec!["ungranted".into()],
        vec!["bad/name".into()],
    ] {
        assert_eq!(
            Config {
                namespaces: names,
                ..config()
            }
            .validate(&Limits::default(), &writers()),
            Err("config")
        );
    }
    for access in [Access::Read, Access::CreateOnly] {
        let mut rows = writers();
        rows[0].grants.insert("a".into(), access);
        assert_eq!(config().validate(&Limits::default(), &rows), Err("config"));
    }
    for value_bytes in [0, 65537, usize::MAX] {
        let mut cfg = config();
        cfg.per_namespace.value_bytes = value_bytes;
        assert_eq!(cfg.validate(&Limits::default(), &writers()), Err("config"));
    }
    for records in [0, 33, usize::MAX] {
        let mut cfg = config();
        cfg.per_namespace.records = records;
        assert_eq!(cfg.validate(&Limits::default(), &writers()), Err("config"));
    }
    assert_eq!(
        config().validate(
            &Limits {
                live_bytes: 1024,
                ..Limits::default()
            },
            &writers()
        ),
        Err("config")
    );
}

#[test]
fn bundle_manifest_binds_process_lifetime_limits_and_distinct_storage_markers() {
    let before = process_facts::manifest(&config());
    assert_eq!(before["temporary_value_bytes"], 65536);
    assert_eq!(before["temporary_records"], 64);
    assert_eq!(before["temporary_live_bytes"], 4194304);
    assert_eq!(before["durable"], false);
    assert_eq!(before["reset_command"], false);
    let mut changed = config();
    changed.per_namespace.records = 1;
    assert_ne!(before, process_facts::manifest(&changed));
    changed = config();
    changed.namespaces.pop();
    assert_ne!(before, process_facts::manifest(&changed));
    let mut manifest = serde_json::json!({"steps":8});
    crate::bind_http_manifest(&mut manifest, EntryContract::ProcessFacts, &header_policy());
    assert_eq!(manifest["steps"], 8);
    assert_eq!(manifest["http"]["envelope_fields"], 18);
    assert_eq!(manifest["http"]["monotonic_clock"]["index"], 17);
    assert_eq!(manifest["http"]["monotonic_clock"]["maximum"], i64::MAX);
    assert_eq!(manifest["storage_observations"]["durable_read"], "DR2");
    assert_eq!(manifest["storage_observations"]["temporary_read"], "VR1");
    assert_eq!(manifest["storage_observations"]["durable_commit"], "DC2");
    assert_eq!(manifest["storage_observations"]["temporary_write"], "VC1");
}

#[test]
fn lifecycle_is_explicit_and_null_or_unrecognized_sources_cannot_enable_it() {
    let legacy = serde_json::to_value(config()).unwrap();
    assert!(legacy.get("lifecycle").is_none());
    for value in [
        serde_json::json!(null),
        serde_json::json!(true),
        serde_json::json!({}),
        serde_json::json!({"source":"posix_sigusr1","draining":false}),
        serde_json::json!({"source":"SIGTERM"}),
    ] {
        let mut raw = legacy.clone();
        raw["lifecycle"] = value;
        assert!(serde_json::from_value::<Config>(raw).is_err());
    }
    let mut raw = legacy;
    raw["lifecycle"] = serde_json::json!({"source":"posix_sigusr1"});
    assert!(
        serde_json::from_value::<Config>(raw)
            .unwrap()
            .lifecycle
            .is_some()
    );
}

#[test]
fn lifecycle_fact_container_is_bundle_bound_without_changing_legacy_clock_or_step_limits() {
    let mut old = serde_json::json!({"steps":8});
    crate::bind_http_manifest(&mut old, EntryContract::ProcessFacts, &header_policy());
    process_facts::bind_manifest(&mut old, &config());
    assert_eq!(old["process_facts"]["contract"], "sigil-process-facts/v1");
    assert!(old["http"].get("process_observation").is_none());
    assert!(old["http"]["monotonic_clock"].get("within").is_none());
    let mut changed = config();
    changed.lifecycle = Some(crate::lifecycle::Config {
        source: crate::lifecycle::Source::PosixSigusr1,
    });
    let mut new = old.clone();
    process_facts::bind_manifest(&mut new, &changed);
    assert_eq!(new["steps"], 8);
    assert_eq!(new["http"]["envelope_fields"], 18);
    assert_eq!(new["http"]["contract"], "sigil-http-exchange/v4");
    assert_eq!(new["http"]["process_observation"]["index"], 17);
    assert_eq!(new["http"]["process_observation"]["observation"], "PF1");
    assert_eq!(new["process_facts"]["contract"], "sigil-process-facts/v2");
    assert_eq!(new["process_facts"]["lifecycle"]["observation"], "LF1");
    assert_ne!(old, new);
}

#[test]
fn actual_durable_and_temporary_actions_produce_distinct_receipts_and_keep_old_commands() {
    let (_dir, mut store, scope, mut process) = fixture();
    let raw = command("read_observed", "a", "key", &guard());
    assert_eq!(
        observed(
            execute(&mut store, &scope, &mut process, &raw).unwrap(),
            "read_observed",
            "DR2\n",
            6
        ),
        ["ok", "0", "0", "", "", ""]
    );
    let raw = command("commit_observed", &batch(0, "durable"), "", &guard());
    assert_eq!(
        observed(
            execute(&mut store, &scope, &mut process, &raw).unwrap(),
            "commit_observed",
            "DC2\n",
            4
        ),
        ["ok", "1", "", ""]
    );
    let raw = command("temporary_write", &mutation(0, "temporary"), "", &guard());
    assert_eq!(
        observed(
            execute(&mut store, &scope, &mut process, &raw).unwrap(),
            "temporary_write",
            "VC1\n",
            3
        ),
        ["ok", "1", ""]
    );
    let raw = command("temporary_read", "a", "key", &guard());
    assert_eq!(
        observed(
            execute(&mut store, &scope, &mut process, &raw).unwrap(),
            "temporary_read",
            "VR1\n",
            5
        ),
        ["ok", "1", "1", "temporary", ""]
    );
    let raw = command("read", "a", "key", &guard());
    assert_eq!(
        observed(
            execute(&mut store, &scope, &mut process, &raw).unwrap(),
            "read",
            "SR1\n",
            3
        ),
        ["ok", "1", "durable"]
    );
}

#[test]
fn actual_storage_outage_keeps_held_temporary_authority_without_reset_or_new_scope() {
    let (dir, mut store, scope, mut process) = fixture();
    execute(
        &mut store,
        &scope,
        &mut process,
        &command("temporary_write", &mutation(0, "kept"), "", &guard()),
    )
    .unwrap();
    fs::rename(dir.path().join("store"), dir.path().join("parked")).unwrap();
    let read = execute(
        &mut store,
        &scope,
        &mut process,
        &command("read_observed", "a", "key", &guard()),
    )
    .unwrap();
    assert_eq!(
        observed(read, "read_observed", "DR2\n", 6),
        ["error", "0", "0", "", "storage", "storage"]
    );
    let write = execute(
        &mut store,
        &scope,
        &mut process,
        &command("commit_observed", &batch(0, "no"), "", &guard()),
    )
    .unwrap();
    assert_eq!(
        observed(write, "commit_observed", "DC2\n", 4),
        ["error", "0", "storage", "storage"]
    );
    assert!(
        store
            .scope([("a".into(), Access::ReadWrite)].into())
            .is_err()
    );
    let read = execute(
        &mut store,
        &scope,
        &mut process,
        &command("temporary_read", "a", "key", &guard()),
    )
    .unwrap();
    assert_eq!(
        observed(read, "temporary_read", "VR1\n", 5),
        ["ok", "1", "1", "kept", ""]
    );
    let write = execute(
        &mut store,
        &scope,
        &mut process,
        &command("temporary_write", &mutation(1, "updated"), "", &guard()),
    )
    .unwrap();
    assert_eq!(
        observed(write, "temporary_write", "VC1\n", 3),
        ["ok", "2", ""]
    );
    fs::rename(dir.path().join("parked"), dir.path().join("store")).unwrap();
    assert_eq!(store.get(&scope, "a", "key").unwrap().revision, 0);
    assert_eq!(
        process
            .cells
            .get(&store, &scope, "a", "key")
            .unwrap()
            .revision,
        2
    );
}

fn actions() -> Vec<(&'static str, String, &'static str)> {
    vec![
        ("read_observed", "a".into(), "key"),
        ("commit_observed", batch(0, "blocked"), ""),
        ("temporary_read", "a".into(), "key"),
        ("temporary_write", mutation(0, "blocked"), ""),
        ("inspect_storage", r#"["a"]"#.into(), ""),
        ("inspect_execution", r#"["a"]"#.into(), ""),
    ]
}

#[test]
fn storage_inspection_action_observes_real_scoped_storage_without_returning_contents() {
    let (_dir, mut store, scope, mut process) = fixture();
    execute(
        &mut store,
        &scope,
        &mut process,
        &command(
            "commit_observed",
            &batch(0, "must remain private"),
            "",
            &guard(),
        ),
    )
    .unwrap();
    let before = store.get(&scope, "a", "key").unwrap();
    let raw = command("inspect_storage", r#"["a"]"#, "", &guard());
    let step = execute(&mut store, &scope, &mut process, &raw).unwrap();
    assert_eq!(
        observed(step, "inspect_storage", "SI1\n", 3),
        ["ok", "", ""]
    );
    assert_eq!(store.get(&scope, "a", "key").unwrap(), before);
    let reader = store.scope([("a".into(), Access::Read)].into()).unwrap();
    let step = execute(&mut store, &reader, &mut process, &raw).unwrap();
    assert_eq!(
        observed(step, "inspect_storage", "SI1\n", 3),
        ["ok", "", ""]
    );
}

#[test]
fn execution_inspection_carries_native_scope_provenance_and_real_storage_outcomes() {
    let (dir, mut store, scope, mut process) = fixture();
    for missing in [false, true] {
        if missing {
            fs::rename(dir.path().join("store"), dir.path().join("parked")).unwrap();
        }
        for (names, checked, status, label, origin) in [
            (
                r#"["a"]"#,
                true,
                if missing { "error" } else { "ok" },
                if missing { "storage" } else { "" },
                if missing { "storage" } else { "" },
            ),
            (r#"["a","b"]"#, false, "error", "denied", "precheck"),
            ("[]", false, "error", "limit", "precheck"),
        ] {
            let step = execute(
                &mut store,
                &scope,
                &mut process,
                &command("inspect_execution", names, "", &guard()),
            )
            .unwrap();
            let Step::InspectExecution {
                storage,
                read_scope_checked,
                continuation,
            } = step
            else {
                panic!("wrong step")
            };
            assert_eq!(read_scope_checked, checked);
            assert_eq!(continuation, "held");
            assert_eq!(
                fields(&storage, "SI1\n", 3).unwrap(),
                [status, label, origin]
            );
        }
        if missing {
            fs::rename(dir.path().join("parked"), dir.path().join("store")).unwrap();
        }
    }
}

#[test]
fn storage_inspection_action_reports_actual_outage_and_recovers_with_the_held_scope() {
    let (dir, mut store, scope, mut process) = fixture();
    let raw = command("inspect_storage", r#"["a"]"#, "", &guard());
    fs::rename(dir.path().join("store"), dir.path().join("parked")).unwrap();
    let step = execute(&mut store, &scope, &mut process, &raw).unwrap();
    assert_eq!(
        observed(step, "inspect_storage", "SI1\n", 3),
        ["error", "storage", "storage"]
    );
    let denied = command("inspect_storage", r#"["a","b"]"#, "", &guard());
    let step = execute(&mut store, &scope, &mut process, &denied).unwrap();
    assert_eq!(
        observed(step, "inspect_storage", "SI1\n", 3),
        ["error", "denied", "precheck"]
    );
    fs::rename(dir.path().join("parked"), dir.path().join("store")).unwrap();
    let step = execute(&mut store, &scope, &mut process, &raw).unwrap();
    assert_eq!(
        observed(step, "inspect_storage", "SI1\n", 3),
        ["ok", "", ""]
    );
}

#[test]
fn storage_inspection_query_and_native_namespace_bounds_fail_closed() {
    let (_dir, mut store, scope, mut process) = fixture();
    for (names, error) in [
        ("[]".to_owned(), "limit"),
        (r#"["a","a"]"#.to_owned(), "invalid"),
        (r#"["bad/path"]"#.to_owned(), "invalid"),
        (serde_json::to_string(&["a"; 9]).unwrap(), "limit"),
    ] {
        let raw = command("inspect_storage", &names, "", &guard());
        let step = execute(&mut store, &scope, &mut process, &raw).unwrap();
        assert_eq!(
            observed(step, "inspect_storage", "SI1\n", 3),
            ["error", error, "precheck"]
        );
    }
    let oversized = " ".repeat(crate::storage_inspection::QUERY_BYTES + 1);
    let result = perform_with_process_facts(
        (
            EntryContract::ProcessFacts,
            Some(&header_policy()),
            Some(&mut process),
        ),
        Some(&mut store),
        Some(&scope),
        &mut 100,
        Instant::now() + Duration::from_secs(15),
        &command("inspect_storage", &oversized, "", &guard()),
        || panic!("oversized query reached the clock"),
    );
    assert!(matches!(result, Err("limit")));
}

#[test]
fn storage_inspection_codec_preserves_deadline_as_an_execution_fact() {
    let (_dir, mut store, scope, _process) = fixture();
    // Direct codec boundary, not an expired HTTP action: the dispatcher itself
    // rejects expired requests before invoking this operation.
    let output = crate::storage_inspection::Query::parse(r#"["a"]"#)
        .unwrap()
        .observe(&mut store, &scope, Instant::now())
        .unwrap();
    assert_eq!(
        fields(&output, "SI1\n", 3).unwrap(),
        ["error", "deadline", "execution"]
    );
}

#[test]
fn storage_inspection_is_bundle_bound_without_changing_old_profiles() {
    let mut identity = serde_json::json!({"steps":8});
    crate::bind_http_manifest(&mut identity, EntryContract::ProcessFacts, &header_policy());
    let inspection = &identity["storage_inspection"];
    assert_eq!(inspection["observation"], "SI1");
    assert_eq!(inspection["query_bytes"], 2048);
    assert_eq!(inspection["namespaces"], 8);
    assert_eq!(inspection["maximum_ms"], 1000);
    assert_eq!(inspection["sqlite_progress_steps"], 8000000);
    assert_eq!(inspection["writes"], false);
    assert_eq!(inspection["returns_contents"], false);
    assert_eq!(inspection["readiness_decision"], false);
    assert_eq!(inspection["write_reservation"], false);
    assert_eq!(identity["steps"], 8);
    assert_eq!(identity["execution_inspection"]["observation"], "EI1");
    assert_eq!(identity["execution_inspection"]["worker_dispatch"], false);
    assert_eq!(
        identity["execution_inspection"]["readiness_decision"],
        false
    );
    let inventory: Vec<String> =
        serde_json::from_str(EntryContract::ProcessFacts.command_inventory().unwrap()).unwrap();
    assert!(inventory.contains(&"inspect_storage".into()));
    for old in [EntryContract::HttpExchange, EntryContract::RequestAdmission] {
        let mut before = serde_json::json!({});
        crate::bind_http_manifest(&mut before, old, &header_policy());
        assert!(before.get("storage_inspection").is_none());
        assert!(before.get("execution_inspection").is_none());
        assert!(!old.command_inventory().unwrap().contains("inspect_storage"));
    }
}

#[test]
fn every_new_action_requires_fresh_time_and_cannot_return_an_outage_for_expiry() {
    let (_dir, mut store, scope, mut process) = fixture();
    for (name, a, b) in actions() {
        let calls = Cell::new(0);
        let raw = command(name, &a, b, &frame("TG1\n", &["100", "150"]).unwrap());
        let result = perform_with_process_facts(
            (
                EntryContract::ProcessFacts,
                Some(&header_policy()),
                Some(&mut process),
            ),
            Some(&mut store),
            Some(&scope),
            &mut 100,
            Instant::now() + Duration::from_secs(15),
            &raw,
            || {
                calls.set(calls.get() + 1);
                Ok(150)
            },
        );
        assert!(matches!(result, Err("time_guard")));
        assert_eq!(calls.get(), 1);
        let raw = command(name, &a, b, &guard());
        let result = perform_with_process_facts(
            (
                EntryContract::ProcessFacts,
                Some(&header_policy()),
                Some(&mut process),
            ),
            Some(&mut store),
            Some(&scope),
            &mut 200,
            Instant::now() + Duration::from_secs(15),
            &raw,
            || Ok(150),
        );
        assert!(matches!(result, Err("clock")));
        let result = perform_with_process_facts(
            (
                EntryContract::ProcessFacts,
                Some(&header_policy()),
                Some(&mut process),
            ),
            Some(&mut store),
            Some(&scope),
            &mut 100,
            Instant::now(),
            &raw,
            || panic!("expired deadline sampled clock"),
        );
        assert!(matches!(result, Err("deadline")));
        assert_eq!(store.get(&scope, "a", "key").unwrap().revision, 0);
        assert_eq!(
            process
                .cells
                .get(&store, &scope, "a", "key")
                .unwrap()
                .revision,
            0
        );
    }
}

#[test]
fn old_profiles_refuse_every_new_action_before_clock_or_storage_access() {
    let (_dir, mut store, scope, mut process) = fixture();
    for contract in [
        EntryContract::Legacy,
        EntryContract::Metadata,
        EntryContract::HttpExchange,
        EntryContract::RequestAdmission,
    ] {
        for (name, a, b) in actions() {
            let raw = frame(contract.command_marker(), &[name, &a, b, "held", &guard()]).unwrap();
            let policy = header_policy();
            let result = perform_with_process_facts(
                (
                    contract,
                    contract.uses_http().then_some(&policy),
                    Some(&mut process),
                ),
                Some(&mut store),
                Some(&scope),
                &mut 100,
                Instant::now() + Duration::from_secs(15),
                &raw,
                || panic!("old profile sampled clock for a new action"),
            );
            assert!(matches!(result, Err("protocol")));
        }
    }
    assert_eq!(store.get(&scope, "a", "key").unwrap().revision, 0);
    assert_eq!(
        process
            .cells
            .get(&store, &scope, "a", "key")
            .unwrap()
            .revision,
        0
    );
}

#[test]
fn every_new_action_requires_a_held_scope_and_temporary_actions_require_process_state() {
    let (_dir, mut store, scope, mut process) = fixture();
    for (name, a, b) in actions() {
        let raw = command(name, &a, b, &guard());
        let result = perform_with_process_facts(
            (
                EntryContract::ProcessFacts,
                Some(&header_policy()),
                Some(&mut process),
            ),
            Some(&mut store),
            None,
            &mut 100,
            Instant::now() + Duration::from_secs(15),
            &raw,
            || Ok(150),
        );
        assert!(matches!(result, Err("capability")));
        if name.starts_with("temporary_") {
            let result = perform_with_http_contract(
                (EntryContract::ProcessFacts, Some(&header_policy())),
                Some(&mut store),
                Some(&scope),
                &mut 100,
                Instant::now() + Duration::from_secs(15),
                &raw,
                || Ok(150),
            );
            assert!(matches!(result, Err("capability")));
        }
    }
}

#[test]
fn native_scope_denial_remains_distinct_from_storage_failure_at_dispatch() {
    let (dir, mut store, scope, mut process) = fixture();
    let other = store
        .scope([("b".into(), Access::ReadWrite)].into())
        .unwrap();
    fs::set_permissions(dir.path().join("store"), fs::Permissions::from_mode(0o755)).unwrap();
    for (held, origin) in [(&other, "precheck"), (&scope, "storage")] {
        let read = execute(
            &mut store,
            held,
            &mut process,
            &command("read_observed", "a", "key", &guard()),
        )
        .unwrap();
        assert_eq!(
            observed(read, "read_observed", "DR2\n", 6),
            ["error", "0", "0", "", "denied", origin]
        );
    }
    let denied = execute(
        &mut store,
        &other,
        &mut process,
        &command("temporary_write", &mutation(0, "no"), "", &guard()),
    )
    .unwrap();
    assert_eq!(
        observed(denied, "temporary_write", "VC1\n", 3),
        ["error", "0", "denied"]
    );
    fs::set_permissions(dir.path().join("store"), fs::Permissions::from_mode(0o700)).unwrap();
    assert_eq!(
        process
            .cells
            .get(&store, &scope, "a", "key")
            .unwrap()
            .revision,
        0
    );
}

#[test]
fn stale_temporary_write_cannot_acknowledge_new_data_or_advance_revision() {
    let (_dir, mut store, scope, mut process) = fixture();
    execute(
        &mut store,
        &scope,
        &mut process,
        &command("temporary_write", &mutation(0, "kept"), "", &guard()),
    )
    .unwrap();
    let stale = execute(
        &mut store,
        &scope,
        &mut process,
        &command("temporary_write", &mutation(0, "stale"), "", &guard()),
    )
    .unwrap();
    assert_eq!(
        observed(stale, "temporary_write", "VC1\n", 3),
        ["error", "0", "conflict"]
    );
    assert_eq!(
        process
            .cells
            .get(&store, &scope, "a", "key")
            .unwrap()
            .value
            .as_deref(),
        Some("kept")
    );
    assert_eq!(
        process
            .cells
            .get(&store, &scope, "a", "key")
            .unwrap()
            .revision,
        1
    );
}

#[test]
fn malformed_new_actions_never_reach_clock_or_storage() {
    let (_dir, mut store, scope, mut process) = fixture();
    for raw in [
        command("inspect_execution", r#"["a"]"#, "extra", &guard()),
        command("inspect_execution", "[false]", "", &guard()),
        command("inspect_storage", r#"["a"]"#, "extra", &guard()),
        command("inspect_storage", r#"{"namespaces":["a"]}"#, "", &guard()),
        command("inspect_storage", "[1]", "", &guard()),
        command("inspect_storage", r#"["a"] []"#, "", &guard()),
        command("temporary_read", "a", "key", ""),
        command("temporary_write", &mutation(0, "no"), "extra", &guard()),
        command("commit_observed", &batch(0, "no"), "extra", &guard()),
        command(
            "temporary_write",
            r#"{"namespace":"a","key":"key","revision":0}"#,
            "",
            &guard(),
        ),
        command(
            "temporary_write",
            r#"{"namespace":"a","key":"key","key":"other","revision":0,"value":"no"}"#,
            "",
            &guard(),
        ),
    ] {
        let result = perform_with_process_facts(
            (
                EntryContract::ProcessFacts,
                Some(&header_policy()),
                Some(&mut process),
            ),
            Some(&mut store),
            Some(&scope),
            &mut 100,
            Instant::now() + Duration::from_secs(15),
            &raw,
            || panic!("malformed command reached the clock"),
        );
        assert!(matches!(result, Err("protocol")));
    }
    assert_eq!(store.get(&scope, "a", "key").unwrap().revision, 0);
    assert_eq!(
        process
            .cells
            .get(&store, &scope, "a", "key")
            .unwrap()
            .revision,
        0
    );
}

#[test]
fn process_clock_and_cells_share_a_stable_lifetime_but_not_durable_state() {
    let (_dir, mut store, scope, mut process) = fixture();
    let first = process.observe().unwrap();
    execute(
        &mut store,
        &scope,
        &mut process,
        &command("temporary_write", &mutation(0, "kept"), "", &guard()),
    )
    .unwrap();
    let second = process.observe().unwrap();
    let a = fields(&first, "MC1\n", 2).unwrap();
    let b = fields(&second, "MC1\n", 2).unwrap();
    assert_eq!(a[0], b[0]);
    assert!(a[1].parse::<u64>().unwrap() <= b[1].parse::<u64>().unwrap());
    assert_eq!(
        process
            .cells
            .get(&store, &scope, "a", "key")
            .unwrap()
            .revision,
        1
    );
    assert_eq!(store.get(&scope, "a", "key").unwrap().revision, 0);
    // This explicit trusted construction models a fresh embedding lifetime;
    // it is not an exposed reset action or an actual process crash test.
    let mut fresh = ProcessFacts::open(&store, config()).unwrap();
    assert_ne!(
        fields(&fresh.observe().unwrap(), "MC1\n", 2).unwrap()[0],
        a[0]
    );
    assert_eq!(
        fresh
            .cells
            .get(&store, &scope, "a", "key")
            .unwrap()
            .revision,
        0
    );
}
