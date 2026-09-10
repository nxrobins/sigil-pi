//! Actual receipt/scope/atomicity mechanics; controlled protocol workers only.
//! The HTTP suite separately executes the real compiled SIGIL audit policy.
use super::tests::{Fixture, binding, execution, fixture_recorder, recorded_scope};
use super::*;
use crate::effect_audit::EffectAudit;
use sigil_durable_store::store::Access;
use sigil_worker_bridge::Config;
use std::collections::BTreeMap;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex, MutexGuard};

// These component fixtures each launch multiple controlled processes. They
// test precise invocation/commit boundaries, not offered-load performance. Do
// not make their independent wall-clock guards race one another in cargo's
// parallel unit runner; acquire this before creating workers or starting clocks.
// Existing tests, runner concurrency, and every execution deadline stay intact.
static AUDIT_FIXTURE: Mutex<()> = Mutex::new(());

fn fixture_guard() -> MutexGuard<'static, ()> {
    // No mutable state is shared. Keep the original failed test visible without
    // converting every independent subsequent fixture into a poison failure.
    AUDIT_FIXTURE.lock().unwrap_or_else(|e| e.into_inner())
}

fn formatter(directory: &Path, delay: u64) -> Config {
    use sha2::{Digest, Sha256};
    let runtime = directory.join("audit-runtime");
    let output = frame("AR1\n", &["publish", "native publication fixture"]).unwrap();
    let reply = serde_json::json!({"jsonrpc":"2.0","id":2,"result":{"content":[{
        "type":"text","text":serde_json::json!({"status":"ok","data":{"output_text":output}}).to_string()}]}});
    let reply = reply.to_string().replace("\"id\":2", "\"id\":'\"$n\"'");
    let boot = serde_json::json!({"jsonrpc":"2.0","id":2,"result":{"content":[{
        "type":"text","text":serde_json::json!({"status":"ok","data":{"output_text":"native fixture admitted"}}).to_string()}]}});
    let boot = boot.to_string().replace("\"id\":2", "\"id\":'\"$n\"'");
    let captured = directory.join("audit-inputs");
    let body = format!(
        "#!/bin/sh\nprintf '%s\\n' \"$$\" >> '{}'\nread -r line\nprintf '%s\\n' '{{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{{}}}}'\nn=2\nwhile read -r line; do\ncase \"$line\" in\n*native-audit-fixture-boot*) printf '%s\\n' '{boot}' ;;\n*) printf '%s\\n' \"$line\" >> '{}'\n/bin/sleep {delay}\nprintf '%s\\n' '{reply}' ;;\nesac\nn=$((n + 1))\ndone\n",
        directory.join("audit-starts").display(),
        captured.display()
    );
    fs::write(&runtime, &body).unwrap();
    fs::set_permissions(&runtime, fs::Permissions::from_mode(0o700)).unwrap();
    let source = directory.join("audit-source");
    fs::write(&source, "native fixture, not SIGIL").unwrap();
    Config {
        version: 1,
        runtime,
        source,
        runtime_sha256: format!("{:x}", Sha256::digest(body)),
        source_sha256: format!("{:x}", Sha256::digest("native fixture, not SIGIL")),
        max_fuel: 1000,
        max_timeout_ms: 5000,
        net: vec![],
        fs: vec![],
        secret_env: BTreeMap::new(),
    }
}

fn admitted_recorder(directory: &Path, phase: &str) -> Recorder {
    use sha2::{Digest, Sha256};
    // Build/admit before Attempt::begin, just like the actual automatic service.
    // The old static helper needs a generation and therefore forces fixture
    // filesystem/admission work into the already-running execution window.
    // This controlled responder instead copies the actual WF1 generation. The
    // production native claim/delivery checks still require its exact binding.
    let placeholder = "e".repeat(64);
    drop(fixture_recorder(directory, &placeholder, phase, 0));
    let runtime = directory.join("recorder-runtime");
    let original = fs::read_to_string(&runtime).unwrap();
    let anchor = "read -r line\nprintf '%s\\n' \"$line\" >";
    assert_eq!(original.matches(anchor).count(), 1);
    let dynamic = "read -r line\ngeneration=$(printf '%s\\n' \"$line\" | /usr/bin/sed -nE 's/.*WF1\\\\n0000000[789](prepared|observed|refused|abandoned)00000064([0-9a-f]{64}).*/\\2/p')\n[ ${#generation} -eq 64 ] || exit 65\nprintf '%s\\n' \"$line\" >";
    let body = original
        .replace(anchor, dynamic)
        .replace(&placeholder, "'\"$generation\"'");
    fs::write(&runtime, &body).unwrap();
    let source = directory.join("recorder-source");
    Recorder::new(RecorderConfig {
        delivery_namespace: "delivery".into(),
        worker: Config {
            version: 1,
            runtime,
            runtime_sha256: format!("{:x}", Sha256::digest(body)),
            source_sha256: format!("{:x}", Sha256::digest(fs::read(&source).unwrap())),
            source,
            max_fuel: 1000,
            max_timeout_ms: 5000,
            net: vec![],
            fs: vec![],
            secret_env: BTreeMap::new(),
        },
    })
    .unwrap()
}

fn grant_map() -> BTreeMap<String, Access> {
    BTreeMap::from([
        ("intent".into(), Access::Read),
        ("claim".into(), Access::ReadWrite),
        ("delivery".into(), Access::CreateOnly),
    ])
}

fn recording_timing(directory: &Path) -> Vec<Vec<String>> {
    fs::read_to_string(directory.join("audit-inputs"))
        .unwrap_or_default()
        .lines()
        .take(4)
        .filter_map(|line| {
            let request: serde_json::Value = serde_json::from_str(line).ok()?;
            let input = request["params"]["arguments"]["input"].as_str()?;
            let facts = fields(input, "WA1\n", 28).ok()?;
            Some(facts[22..27].iter().map(|v| (*v).to_owned()).collect())
        })
        .collect()
}

fn state(f: &Fixture) -> (u64, u64, u64) {
    let scope = f
        .store
        .scope(BTreeMap::from([
            ("claim".into(), Access::Read),
            ("delivery".into(), Access::Read),
            ("audit.heads".into(), Access::Read),
        ]))
        .unwrap();
    let head = f.store.get(&scope, "audit.heads", &"a".repeat(64)).unwrap();
    let count = head
        .value
        .map(|raw| {
            serde_json::from_str::<serde_json::Value>(&raw).unwrap()["body"]["count"]
                .as_u64()
                .unwrap()
        })
        .unwrap_or(0);
    (
        f.store
            .get(&scope, "claim", "operation:1")
            .unwrap()
            .revision,
        f.store
            .get(&scope, "delivery", "operation:1")
            .unwrap()
            .revision,
        count,
    )
}

fn policy_fixture(f: &Fixture, output: &str) -> (crate::policy::Policy, String, String) {
    use crate::effect_audit::digest;
    use crate::policy::{Config as PolicyConfig, Input, Key, Policy};
    let runtime = f.directory.path().join("policy-runtime");
    let reply = serde_json::json!({"jsonrpc":"2.0","id":2,"result":{"content":[{
        "type":"text","text":serde_json::json!({"status":"ok","data":{"output_text":output}}).to_string()}]}});
    let body = format!(
        "#!/bin/sh\nread -r line\nprintf '%s\\n' '{{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{{}}}}'\nread -r line\nprintf '%s\\n' \"$line\" > '{}'\nprintf '%s\\n' '{reply}'\nwhile :; do /bin/sleep 1; done\n",
        f.directory.path().join("policy-input").display()
    );
    fs::write(&runtime, &body).unwrap();
    fs::set_permissions(&runtime, fs::Permissions::from_mode(0o700)).unwrap();
    let source = f.directory.path().join("policy-source");
    fs::write(&source, "controlled native dispatch policy").unwrap();
    let config = PolicyConfig {
        worker: Config {
            version: 1,
            runtime,
            runtime_sha256: digest(&body),
            source,
            source_sha256: digest("controlled native dispatch policy"),
            max_fuel: 1000,
            max_timeout_ms: 5000,
            net: vec![],
            fs: vec![],
            secret_env: BTreeMap::new(),
        },
        marker: "PF1\n".into(),
        values: 1,
        inputs: vec![
            Input::Value { index: 0 },
            Input::Clock,
            Input::Literal {
                value: "private credential/context fixture".into(),
            },
            Input::Read {
                namespace: "intent".into(),
                key: Key::Value { index: 0 },
            },
            Input::Read {
                namespace: "intent".into(),
                key: Key::Literal {
                    value: "absent".into(),
                },
            },
            Input::WorkerFacts,
        ],
        read_grants: BTreeMap::from([("intent".into(), Access::Read)]),
        alias: "fixture".into(),
        binding: "private binding fixture".into(),
        claim_namespace: "claim".into(),
    };
    let hash = digest(&serde_json::to_string(&config).unwrap());
    let manifest = crate::audited_transaction::manifest(&config.worker).unwrap();
    (Policy::new(config, &f.store).unwrap(), hash, manifest)
}

#[test]
fn actual_policy_evidence_is_frozen_into_claim_and_completion_without_raw_inputs() {
    use crate::effect_audit::digest;
    let _fixture = fixture_guard();
    let mut f = Fixture::new();
    recorded_scope(&mut f, Access::CreateOnly);
    let audit = EffectAudit::fixture(
        &mut f.store,
        formatter(f.directory.path(), 0),
        grant_map(),
        10,
        false,
    );
    let mut recorder = admitted_recorder(f.directory.path(), "2");
    recorder.attach_audit(audit);
    let output = frame(
        "DW1\n",
        &[
            "fixture",
            "exact frozen input",
            "intent",
            "operation:1",
            "1",
            &execution().time_guard,
            "private returned context",
        ],
    )
    .unwrap();
    let (mut policy, config_hash, manifest) = policy_fixture(&f, &output);
    let (mut attempt, context) = policy
        .begin(
            &mut f.store,
            &f.scope,
            &mut f.worker,
            &mut f.clock,
            &["operation:1".into()],
        )
        .unwrap();
    assert_eq!(context, "private returned context");
    let prepared = attempt.prepared_audit.clone();
    let wp = fields(&prepared, "WP2\n", 6).unwrap();
    let dp = fields(wp[5], "DP1\n", 16).unwrap();
    let request: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(f.directory.path().join("policy-input")).unwrap())
            .unwrap();
    let input = request["params"]["arguments"]["input"].as_str().unwrap();
    let actual = fields(input, "PF1\n", 6).unwrap();
    assert_eq!(dp[0], manifest);
    assert_eq!(dp[1], config_hash);
    assert_eq!(dp[2], digest(input));
    assert_eq!(dp[3], input.len().to_string());
    assert_eq!(dp[4], digest(&output));
    assert_eq!(dp[5], output.len().to_string());
    let present = frame(
        "DQ1\n",
        &[
            "intent",
            &digest("operation:1"),
            "1",
            "some",
            &digest("opaque intent"),
        ],
    )
    .unwrap();
    let absent = frame(
        "DQ1\n",
        &["intent", &digest("absent"), "0", "none", &digest("")],
    )
    .unwrap();
    assert_eq!(
        dp[6],
        digest(&frame("DS1\n", &[&present, &absent]).unwrap())
    );
    assert_eq!(dp[7], "2");
    assert_eq!(dp[8], actual[1]);
    assert!(dp[9].parse::<u64>().unwrap() >= dp[8].parse::<u64>().unwrap());
    assert!(dp[10].parse::<u64>().unwrap() < 5000);
    assert!((1..=5000).contains(&dp[11].parse::<u64>().unwrap()));
    assert_eq!(
        &dp[12..],
        [
            "fixture",
            &digest("exact frozen input"),
            &execution().time_guard,
            &digest(&context)
        ]
    );
    let ticket = attempt.prepared.ticket.clone();
    let result = attempt
        .run_recorded(&ticket, &mut recorder, &AtomicBool::new(false))
        .unwrap();
    assert_eq!(result.claim_receipt.revision, 2);
    assert_eq!(result.delivery_receipt.unwrap().revision, 3);
    assert!(result.observation.unwrap().request_may_have_run);
    drop(attempt);
    assert_eq!(state(&f), (2, 1, 2));
    let captured = fs::read_to_string(f.directory.path().join("audit-inputs")).unwrap();
    assert_eq!(captured.lines().count(), 2);
    assert_eq!(
        fs::read_to_string(f.directory.path().join("audit-starts"))
            .unwrap()
            .lines()
            .count(),
        1
    );
    for line in captured.lines() {
        let request: serde_json::Value = serde_json::from_str(line).unwrap();
        let input = request["params"]["arguments"]["input"].as_str().unwrap();
        assert_eq!(fields(input, "WA1\n", 28).unwrap()[15], prepared);
        for secret in [
            "private credential",
            "private binding",
            "private returned context",
            "opaque intent",
            "exact frozen input",
        ] {
            assert!(!input.contains(secret));
        }
    }
}

#[test]
fn invalid_policy_output_cannot_publish_a_claim_or_dispatch_provenance() {
    let _fixture = fixture_guard();
    for output in [
        "invalid reply".to_owned(),
        frame(
            "DW1\n",
            &[
                "different_alias",
                "input",
                "intent",
                "operation:1",
                "1",
                &execution().time_guard,
                "context",
            ],
        )
        .unwrap(),
    ] {
        let mut f = Fixture::new();
        recorded_scope(&mut f, Access::CreateOnly);
        let (mut policy, _, _) = policy_fixture(&f, &output);
        assert!(
            policy
                .begin(
                    &mut f.store,
                    &f.scope,
                    &mut f.worker,
                    &mut f.clock,
                    &["operation:1".into()]
                )
                .is_err()
        );
        assert_eq!(state(&f), (0, 0, 0));
        assert!(!f.directory.path().join("started").exists());
        assert!(!f.directory.path().join("audit-inputs").exists());
    }
}

#[test]
fn claim_and_observed_delivery_each_publish_audit_in_their_one_actual_commit() {
    let _fixture = fixture_guard();
    let mut f = Fixture::new();
    recorded_scope(&mut f, Access::CreateOnly);
    let config = formatter(f.directory.path(), 0);
    let audit = EffectAudit::fixture(&mut f.store, config, grant_map(), 10, false);
    let mut recorder = admitted_recorder(f.directory.path(), "2");
    recorder.attach_audit(audit);
    let mut attempt = Attempt::begin(
        &mut f.store,
        &f.scope,
        &mut f.worker,
        &mut f.clock,
        binding(),
        execution(),
    )
    .unwrap();
    let ticket = attempt.prepared.ticket.clone();
    let result = attempt
        .run_recorded(&ticket, &mut recorder, &AtomicBool::new(false))
        .unwrap();
    // Intent was commit 1. Separate state/audit commits would increase these.
    assert_eq!(result.claim_receipt.revision, 2);
    assert_eq!(result.delivery_receipt.unwrap().revision, 3);
    assert_eq!(result.phase.as_deref(), Some("2"));
    assert!(result.observation.unwrap().request_may_have_run);
    drop(attempt);
    assert_eq!(state(&f), (2, 1, 2));
    let captured = fs::read_to_string(f.directory.path().join("audit-inputs")).unwrap();
    let requests: Vec<serde_json::Value> = captured
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(requests.len(), 2);
    for (index, request) in requests.iter().enumerate() {
        let values = fields(
            request["params"]["arguments"]["input"].as_str().unwrap(),
            "WA1\n",
            28,
        )
        .unwrap();
        let prepared = fields(values[15], "WP1\n", 5).unwrap();
        assert_eq!(
            prepared,
            [
                crate::effect_audit::digest("exact frozen input"),
                "18".into(),
                "100".into(),
                "5000".into(),
                execution().time_guard
            ]
        );
        let observed = fields(values[14], "WO1\n", 9).unwrap();
        assert_eq!(values[12], values[13]);
        assert_eq!(
            observed[0],
            if index == 0 { "prepared" } else { "observed" }
        );
        assert_eq!(
            observed[8],
            if index == 0 {
                String::new()
            } else {
                crate::effect_audit::digest("observed")
            }
        );
        assert!(!request.to_string().contains("exact frozen input"));
    }
    let last_input: serde_json::Value = serde_json::from_str(
        &fs::read_to_string(f.directory.path().join("recorder-input")).unwrap(),
    )
    .unwrap();
    let last = fields(
        requests[1]["params"]["arguments"]["input"]
            .as_str()
            .unwrap(),
        "WA1\n",
        28,
    )
    .unwrap();
    let actual = last_input["params"]["arguments"]["input"].as_str().unwrap();
    assert_eq!(last[16], crate::effect_audit::digest(actual));
    assert_eq!(last[17], actual.len().to_string());
}

#[test]
fn private_audit_scope_cannot_widen_the_original_claim_permission() {
    let _fixture = fixture_guard();
    let mut f = Fixture::new();
    let config = formatter(f.directory.path(), 0);
    let audit = EffectAudit::fixture(&mut f.store, config, grant_map(), 10, false);
    let mut original = grant_map();
    original.insert("claim".into(), Access::Read);
    f.scope = f.store.scope(original).unwrap();
    let mut recorder = admitted_recorder(f.directory.path(), "2");
    recorder.attach_audit(audit);
    let mut attempt = Attempt::begin(
        &mut f.store,
        &f.scope,
        &mut f.worker,
        &mut f.clock,
        binding(),
        execution(),
    )
    .unwrap();
    let ticket = attempt.prepared.ticket.clone();
    assert_eq!(
        attempt
            .run_recorded(&ticket, &mut recorder, &AtomicBool::new(false))
            .err(),
        Some("storage")
    );
    drop(attempt);
    assert_eq!(state(&f), (0, 0, 0));
    assert!(!f.directory.path().join("started").exists());
}

#[test]
fn actual_hard_cancellation_is_projected_without_inventing_a_runtime_result() {
    let _fixture = fixture_guard();
    use sha2::{Digest, Sha256};
    use sigil_worker_bridge::Fault;
    let mut f = Fixture::new();
    recorded_scope(&mut f, Access::CreateOnly);
    let runtime = f.directory.path().join("held-effect-runtime");
    let invoked = f.directory.path().join("effect-invoked");
    let trace = f.directory.path().join("effect-startup-trace");
    // A controlled native protocol worker, not a model/provider/compiled SIGIL
    // fixture. It acknowledges initialization, receives the actual invocation,
    // and remains live until the bridge's actual cancellation kills it.
    let body = format!(
        "#!/bin/sh\nprintf '%s\\n' spawned > '{}'\nread -r line\nprintf '%s\\n' init_read >> '{}'\nprintf '%s\\n' '{{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{{}}}}'\nprintf '%s\\n' init_replied >> '{}'\nread -r line\n: > '{}'\nwhile :; do /bin/sleep 1; done\n",
        trace.display(),
        trace.display(),
        trace.display(),
        invoked.display()
    );
    fs::write(&runtime, &body).unwrap();
    fs::set_permissions(&runtime, fs::Permissions::from_mode(0o700)).unwrap();
    f.worker = Bridge::new(Config {
        version: 1,
        runtime,
        runtime_sha256: format!("{:x}", Sha256::digest(body)),
        source: f.directory.path().join("source"),
        source_sha256: format!("{:x}", Sha256::digest("opaque source")),
        max_fuel: 1000,
        max_timeout_ms: 5000,
        net: vec![],
        fs: vec![],
        secret_env: BTreeMap::new(),
    })
    .unwrap();
    let config = formatter(f.directory.path(), 0);
    let audit = EffectAudit::fixture(&mut f.store, config, grant_map(), 10, false);
    let mut recorder = admitted_recorder(f.directory.path(), "4");
    recorder.attach_audit(audit);
    let began = Instant::now();
    let mut attempt = Attempt::begin(
        &mut f.store,
        &f.scope,
        &mut f.worker,
        &mut f.clock,
        binding(),
        execution(),
    )
    .unwrap();
    let stop = Arc::new(AtomicBool::new(false));
    let cancel = Arc::clone(&stop);
    let notifier = std::thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(5);
        while !invoked.is_file() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(2));
        }
        let did_invoke = invoked.is_file();
        cancel.store(true, Ordering::Release);
        did_invoke
    });
    let ticket = attempt.prepared.ticket.clone();
    let result = attempt.run_recorded(&ticket, &mut recorder, &stop);
    assert!(
        notifier.join().unwrap(),
        "worker did not reach invocation after {:?}: {}; startup trace: {:?}; recorder phase/clock/elapsed/timeout: {:?}",
        began.elapsed(),
        serde_json::to_string(&result).unwrap(),
        fs::read_to_string(&trace),
        recording_timing(f.directory.path()),
    );
    let result = result.unwrap();
    assert_eq!(result.claim_receipt.revision, 2);
    assert_eq!(result.delivery_receipt.unwrap().revision, 3);
    assert_eq!(result.phase.as_deref(), Some("4"));
    assert!(result.recording_error.is_none());
    let observed = result.observation.unwrap();
    assert!(observed.request_may_have_run && observed.worker_reaped);
    assert_eq!(observed.fault, Some(Fault::Cancelled));
    assert!(observed.result.is_none());
    drop(attempt);
    assert_eq!(state(&f), (2, 1, 2));
    let captured = fs::read_to_string(f.directory.path().join("audit-inputs")).unwrap();
    let rows: Vec<serde_json::Value> = captured
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(rows.len(), 2);
    let actual = fields(
        rows[1]["params"]["arguments"]["input"].as_str().unwrap(),
        "WA1\n",
        28,
    )
    .unwrap();
    let facts = fields(actual[14], "WO1\n", 9).unwrap();
    assert_eq!(
        facts,
        [
            "observed",
            &observed.generation,
            "1",
            "1",
            "cancelled",
            "",
            "missing",
            "0",
            ""
        ]
    );
    assert_eq!(actual[22], "4");
}

#[test]
fn full_audit_chain_prevents_claim_and_cannot_arm_the_prepared_worker() {
    let _fixture = fixture_guard();
    let mut f = Fixture::new();
    recorded_scope(&mut f, Access::CreateOnly);
    let config = formatter(f.directory.path(), 0);
    let audit = EffectAudit::fixture(&mut f.store, config, grant_map(), 1, true);
    let mut recorder = admitted_recorder(f.directory.path(), "2");
    recorder.attach_audit(audit);
    let mut attempt = Attempt::begin(
        &mut f.store,
        &f.scope,
        &mut f.worker,
        &mut f.clock,
        binding(),
        execution(),
    )
    .unwrap();
    let ticket = attempt.prepared.ticket.clone();
    assert_eq!(
        attempt
            .run_recorded(&ticket, &mut recorder, &AtomicBool::new(false))
            .err(),
        Some("storage")
    );
    assert!(attempt.execute(&ticket, &AtomicBool::new(false)).is_err());
    drop(attempt);
    assert_eq!(state(&f), (0, 0, 1));
    assert!(!f.directory.path().join("started").exists());
}

#[test]
fn delivery_audit_exhaustion_preserves_observation_without_an_invented_receipt() {
    let _fixture = fixture_guard();
    let mut f = Fixture::new();
    recorded_scope(&mut f, Access::CreateOnly);
    let config = formatter(f.directory.path(), 0);
    let audit = EffectAudit::fixture(&mut f.store, config, grant_map(), 1, false);
    let mut recorder = admitted_recorder(f.directory.path(), "2");
    recorder.attach_audit(audit);
    let mut attempt = Attempt::begin(
        &mut f.store,
        &f.scope,
        &mut f.worker,
        &mut f.clock,
        binding(),
        execution(),
    )
    .unwrap();
    let ticket = attempt.prepared.ticket.clone();
    let result = attempt
        .run_recorded(&ticket, &mut recorder, &AtomicBool::new(false))
        .unwrap();
    assert_eq!(result.claim_receipt.revision, 2);
    assert!(result.delivery_receipt.is_none() && result.phase.is_none());
    assert_eq!(result.recording_error, Some("storage"));
    let seen = result.observation.as_ref().unwrap();
    assert!(
        seen.request_may_have_run,
        "expected actual invocation: {}",
        serde_json::to_string(seen).unwrap()
    );
    drop(attempt);
    assert_eq!(state(&f), (1, 0, 1));
}

#[test]
fn audit_projection_does_not_reset_expired_initiation_authority() {
    let _fixture = fixture_guard();
    let mut f = Fixture::new();
    recorded_scope(&mut f, Access::CreateOnly);
    let config = formatter(f.directory.path(), 1);
    let audit = EffectAudit::fixture(&mut f.store, config, grant_map(), 10, false);
    let mut recorder = admitted_recorder(f.directory.path(), "2");
    recorder.attach_audit(audit);
    let mut attempt = Attempt::begin(
        &mut f.store,
        &f.scope,
        &mut f.worker,
        &mut f.clock,
        binding(),
        execution(),
    )
    .unwrap();
    attempt.deadline = Instant::now() + Duration::from_millis(200);
    let ticket = attempt.prepared.ticket.clone();
    assert_eq!(
        attempt
            .run_recorded(&ticket, &mut recorder, &AtomicBool::new(false))
            .err(),
        Some("deadline")
    );
    drop(attempt);
    assert_eq!(state(&f), (0, 0, 0));
    assert!(!f.directory.path().join("started").exists());
}
