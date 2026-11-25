//! Actual native observation/publication with controlled protocol responders.
//! These responders are not SIGIL classifiers; compiled integration is separate.
use super::*;
use crate::policy::{Input, Key, Policy};
use crate::transaction::Transaction;
use serde_json::{Value, json};
use sigil_durable_store::store::authenticated_log::ChainLimits;
use sigil_durable_store::store::{Limits, Mutation, OpenMode};
use sigil_worker_bridge::Bridge;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::sync::Mutex;

// Bound only these new multi-process fixtures, before creating any clocks.
static FIXTURE: Mutex<()> = Mutex::new(());

fn worker(root: &Path, name: &str, boot: &str, result: Value) -> WorkerConfig {
    let response = |inner: Value| {
        json!({"jsonrpc":"2.0","id":2,"result":{"content":[{
            "type":"text","text":inner.to_string()}]}})
        .to_string()
        .replace("\"id\":2", "\"id\":'\"$n\"'")
    };
    let boot = response(json!({"status":"ok","data":{"output_text":boot}}));
    let result = response(result);
    let runtime = root.join(format!("{name}-runtime"));
    let source = root.join(format!("{name}-source"));
    let text = "controlled native fixture, not SIGIL";
    let body = format!(
        "#!/bin/sh\nread -r line\nprintf '%s\\n' '{{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{{}}}}'\nn=2\nwhile read -r line; do\ncase \"$line\" in\n*EB1*) printf '%s\\n' '{boot}' ;;\n*) printf '%s\\n' \"$line\" >> '{}'\nprintf '%s\\n' '{result}' ;;\nesac\nn=$((n + 1))\ndone\n",
        root.join(format!("{name}-calls")).display(),
    );
    fs::write(&runtime, &body).unwrap();
    fs::set_permissions(&runtime, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(&source, text).unwrap();
    WorkerConfig {
        version: 1,
        runtime,
        source,
        runtime_sha256: digest(&body),
        source_sha256: digest(text),
        max_fuel: 1000,
        max_timeout_ms: 5000,
        net: vec![],
        fs: vec![],
        secret_env: BTreeMap::new(),
    }
}
fn ok(output: &str) -> Value {
    json!({"status":"ok","data":{"output_text":output}})
}
fn denied() -> Value {
    json!({"status":"error","error":{"message":"private-diagnostic"}})
}

struct Fixture {
    root: tempfile::TempDir,
    store: Store,
    scope: Scope,
    log: AuthenticatedLog,
    projector: Projector,
    chain: String,
}
impl Fixture {
    fn new(output: &str, records: u64) -> Self {
        let root = tempfile::tempdir().unwrap();
        let state = root.path().join("state");
        fs::create_dir(&state).unwrap();
        fs::set_permissions(&state, fs::Permissions::from_mode(0o700)).unwrap();
        let store = Store::open(&state, OpenMode::CreateNew, Limits::default()).unwrap();
        let grants = BTreeMap::from([
            ("audit.heads".into(), Access::ReadWrite),
            ("audit.entries".into(), Access::ReadWrite),
            ("domain".into(), Access::ReadWrite),
        ]);
        let scope = store.scope(grants).unwrap();
        let log = AuthenticatedLog::new(
            &store,
            "audit.heads",
            "audit.entries",
            ChainLimits {
                payload_bytes: 16384,
                records,
                bytes: 1_000_000,
            },
            b"native-evaluation-journal-fixture-key-0123456789",
        )
        .unwrap();
        let chain = "a".repeat(64);
        let config = worker(
            root.path(),
            "projector",
            "evaluation_audit_admitted",
            ok(output),
        );
        let projector = Projector::admit(config, &chain, "{}").unwrap();
        Self {
            root,
            store,
            scope,
            log,
            projector,
            chain,
        }
    }

    fn failure(&self, cached: bool) -> Failure {
        let config = worker(self.root.path(), "subject", "unused", denied());
        let subject = Subject::new(&config, &config, &BTreeMap::new()).unwrap();
        let mut function = if cached {
            Function::cached(config)
        } else {
            Function::new(config)
        }
        .unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        let lookup = Lookup::new(&["private-lookup".into()]).unwrap();
        let observed = function.observe("private-input".into(), deadline);
        assert_eq!(
            observed.result,
            Err("application"),
            "{}",
            serde_json::to_string(&observed.facts).unwrap()
        );
        Pending::new(observed.facts, lookup, deadline).refuse(&subject, "application")
    }

    fn publish(&mut self, failure: Failure) -> Result<()> {
        publish(
            Some(&mut self.projector),
            Target {
                log: &self.log,
                scope: &self.scope,
                chain: &self.chain,
                heads: "audit.heads",
            },
            &mut self.store,
            failure,
        )
    }

    fn count(&self) -> u64 {
        self.store
            .get(&self.scope, "audit.heads", &self.chain)
            .unwrap()
            .value
            .map(|raw| {
                serde_json::from_str::<Value>(&raw).unwrap()["body"]["count"]
                    .as_u64()
                    .unwrap()
            })
            .unwrap_or(0)
    }

    fn calls(&self) -> Vec<Value> {
        fs::read_to_string(self.root.path().join("projector-calls"))
            .unwrap_or_default()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect()
    }
}

#[test]
fn actual_observation_is_projected_and_only_audit_state_is_committed() {
    let _guard = FIXTURE.lock().unwrap_or_else(|e| e.into_inner());
    for cached in [false, true] {
        let mut f = Fixture::new(
            &frame("AR1\n", &["publish", "controlled redacted payload"]).unwrap(),
            10,
        );
        let failure = f.failure(cached);
        let expected = serde_json::to_value(&failure.pending.facts).unwrap();
        let config_digest = failure.subject.config.clone();
        f.publish(failure).unwrap();
        assert_eq!(f.count(), 1);
        let calls = f.calls();
        assert_eq!(calls.len(), 1);
        let input = calls[0]["params"]["arguments"]["input"].as_str().unwrap();
        let parts = fields(input, "EA1\n", 10).unwrap();
        assert_eq!(parts[1], f.chain);
        assert_eq!(parts[2], config_digest);
        assert_eq!(parts[5], "{}");
        assert_eq!(
            parts[6],
            digest(&frame("EL1\n", &["private-lookup"]).unwrap())
        );
        assert_eq!(parts[7..9], ["1", "application"]);
        assert_eq!(serde_json::from_str::<Value>(parts[9]).unwrap(), expected);
        for raw in ["private-input", "private-lookup", "private-diagnostic"] {
            assert!(!input.contains(raw));
        }
        assert_eq!(
            f.store
                .get(&f.scope, "domain", "operation")
                .unwrap()
                .revision,
            0
        );
        let checkpoint = f
            .log
            .verify(
                &mut f.store,
                &f.scope,
                &f.chain,
                None,
                Instant::now() + Duration::from_secs(1),
            )
            .unwrap();
        assert_eq!(checkpoint.count, 1);
    }
}

#[test]
fn legacy_absence_does_not_invoke_projector_or_append() {
    let _guard = FIXTURE.lock().unwrap_or_else(|e| e.into_inner());
    let mut f = Fixture::new("must not run", 10);
    let failure = f.failure(false);
    publish(
        None,
        Target {
            log: &f.log,
            scope: &f.scope,
            chain: &f.chain,
            heads: "audit.heads",
        },
        &mut f.store,
        failure,
    )
    .unwrap();
    assert_eq!(f.count(), 0);
    assert!(f.calls().is_empty());
}

#[test]
fn expired_subject_deadline_never_gets_a_new_reporting_window() {
    let _guard = FIXTURE.lock().unwrap_or_else(|e| e.into_inner());
    let mut f = Fixture::new("must not run", 10);
    let mut failure = f.failure(false);
    failure.pending.deadline = Instant::now();
    assert_eq!(f.publish(failure), Err("audit_recording"));
    assert_eq!(f.count(), 0);
    assert!(f.calls().is_empty());
}

#[test]
fn malformed_empty_and_skipped_projection_cannot_produce_a_receipt() {
    let _guard = FIXTURE.lock().unwrap_or_else(|e| e.into_inner());
    for output in [
        "arbitrary".into(),
        frame("AR1\n", &["none", ""]).unwrap(),
        frame("AR1\n", &["publish", ""]).unwrap(),
    ] {
        let mut f = Fixture::new(&output, 10);
        let failure = f.failure(false);
        assert_eq!(f.publish(failure), Err("audit_recording"));
        assert_eq!(f.count(), 0);
        assert_eq!(f.calls().len(), 1);
    }
}

#[test]
fn full_chain_reports_failure_without_mutating_the_existing_record() {
    let _guard = FIXTURE.lock().unwrap_or_else(|e| e.into_inner());
    let mut f = Fixture::new(
        &frame("AR1\n", &["publish", "controlled payload"]).unwrap(),
        1,
    );
    let first = f.failure(false);
    f.publish(first).unwrap();
    let head = f.store.get(&f.scope, "audit.heads", &f.chain).unwrap();
    let second = f.failure(false);
    assert_eq!(f.publish(second), Err("audit_recording"));
    assert_eq!(
        f.store.get(&f.scope, "audit.heads", &f.chain).unwrap(),
        head
    );
    assert_eq!(f.count(), 1);
}

#[test]
fn denied_audit_append_scope_cannot_borrow_wider_store_authority() {
    let _guard = FIXTURE.lock().unwrap_or_else(|e| e.into_inner());
    let mut f = Fixture::new(
        &frame("AR1\n", &["publish", "controlled payload"]).unwrap(),
        10,
    );
    let read_only = f
        .store
        .scope(BTreeMap::from([
            ("audit.heads".into(), Access::Read),
            ("audit.entries".into(), Access::Read),
        ]))
        .unwrap();
    let failure = f.failure(false);
    assert_eq!(
        publish(
            Some(&mut f.projector),
            Target {
                log: &f.log,
                scope: &read_only,
                chain: &f.chain,
                heads: "audit.heads",
            },
            &mut f.store,
            failure
        ),
        Err("audit_recording")
    );
    assert_eq!(f.calls().len(), 1); // A valid proposal still is not a receipt.
    assert_eq!(f.count(), 0);
    assert_eq!(
        f.store
            .get(
                &f.scope,
                "audit.entries",
                &format!("{}.0000000000000000", f.chain)
            )
            .unwrap()
            .revision,
        0
    );
    assert_eq!(
        f.store
            .get(&f.scope, "domain", "operation")
            .unwrap()
            .revision,
        0
    );
}

#[test]
fn unavailable_store_cannot_turn_a_projection_into_a_publication() {
    let _guard = FIXTURE.lock().unwrap_or_else(|e| e.into_inner());
    let mut f = Fixture::new(
        &frame("AR1\n", &["publish", "controlled payload"]).unwrap(),
        10,
    );
    let failure = f.failure(false);
    let original = f.root.path().join("state");
    let parked = f.root.path().join("parked-state");
    fs::rename(&original, &parked).unwrap();
    let result = f.publish(failure);
    fs::rename(&parked, &original).unwrap();
    assert_eq!(result, Err("audit_recording"));
    assert_eq!(f.calls().len(), 1);
    assert_eq!(f.count(), 0);
    assert_eq!(
        f.store
            .get(&f.scope, "domain", "operation")
            .unwrap()
            .revision,
        0
    );
}

#[test]
fn corrupt_retained_tail_refuses_failure_append_without_repairing_or_advancing_head() {
    let _guard = FIXTURE.lock().unwrap_or_else(|e| e.into_inner());
    let mut f = Fixture::new(
        &frame("AR1\n", &["publish", "controlled payload"]).unwrap(),
        10,
    );
    let failure = f.failure(false);
    f.publish(failure).unwrap();
    let head = f.store.get(&f.scope, "audit.heads", &f.chain).unwrap();
    let key = format!("{}.0000000000000000", f.chain);
    f.store
        .commit(
            &f.scope,
            &Batch {
                checks: vec![],
                writes: vec![Mutation {
                    namespace: "audit.entries".into(),
                    key: key.clone(),
                    revision: 1,
                    value: Some("deliberately corrupt native fixture".into()),
                }],
            },
        )
        .unwrap();
    let corrupt = f.store.get(&f.scope, "audit.entries", &key).unwrap();
    let failure = f.failure(false);
    assert_eq!(f.publish(failure), Err("audit_recording"));
    assert_eq!(
        f.store.get(&f.scope, "audit.heads", &f.chain).unwrap(),
        head
    );
    assert_eq!(
        f.store.get(&f.scope, "audit.entries", &key).unwrap(),
        corrupt
    );
    assert_eq!(
        f.store
            .get(
                &f.scope,
                "audit.entries",
                &format!("{}.0000000000000001", f.chain)
            )
            .unwrap()
            .revision,
        0
    );
    assert!(
        f.log
            .verify(
                &mut f.store,
                &f.scope,
                &f.chain,
                None,
                Instant::now() + Duration::from_secs(1)
            )
            .is_err()
    );
    assert_eq!(
        f.store
            .get(&f.scope, "domain", "operation")
            .unwrap()
            .revision,
        0
    );
}

#[test]
fn replaced_projector_owner_fails_without_using_replacement_bytes() {
    let _guard = FIXTURE.lock().unwrap_or_else(|e| e.into_inner());
    let mut f = Fixture::new(
        &frame("AR1\n", &["publish", "controlled payload"]).unwrap(),
        10,
    );
    fs::write(f.root.path().join("projector-runtime"), "replaced").unwrap();
    let failure = f.failure(false);
    assert_eq!(f.publish(failure), Err("audit_recording"));
    assert_eq!(f.count(), 0);
    assert!(f.calls().is_empty());
    // This API checks native ownership/poisoning, not current artifact identity.
    // Invocation above independently rejected replacement bytes. Do not turn
    // this limited precheck into a stronger health/admission claim.
    assert_eq!(f.projector.inspect_owner(), Ok(()));
}

#[test]
fn projector_admission_requires_fixed_grantless_matching_boot() {
    let _guard = FIXTURE.lock().unwrap_or_else(|e| e.into_inner());
    let root = tempfile::tempdir().unwrap();
    let mut config = worker(root.path(), "projector", "wrong-boot", ok("unused"));
    assert!(matches!(
        Projector::admit(config.clone(), &"a".repeat(64), "{}"),
        Err("config")
    ));
    config.net.push("https://example.invalid".into());
    assert!(matches!(
        Projector::admit(config.clone(), &"a".repeat(64), "{}"),
        Err("config")
    ));
    config.net.clear();
    config.source_sha256 = "0".repeat(64);
    assert!(Projector::admit(config, &"a".repeat(64), "{}").is_err());
    assert!(!root.path().join("state").exists());
}

fn transaction_config(root: &Path, output: Value) -> crate::transaction::Config {
    crate::transaction::Config {
        worker: worker(root, "transaction", "unused", output),
        marker: "TE1\n".into(),
        values: 1,
        inputs: vec![Input::Value { index: 0 }],
        grants: BTreeMap::from([("domain".into(), Access::Read)]),
    }
}

#[test]
fn actual_transaction_failure_is_consumed_once_and_not_reused_by_invalid_input() {
    let _guard = FIXTURE.lock().unwrap_or_else(|e| e.into_inner());
    for (output, expected) in [
        (denied(), "application"),
        (ok("invalid envelope"), "protocol"),
        (
            ok(&frame(
                "TX1\n",
                &["", "", "{\"op\":\"commit\",\"checks\":[],\"writes\":[]}"],
            )
            .unwrap()),
            "binding",
        ),
    ] {
        let mut f = Fixture::new(
            &frame("AR1\n", &["publish", "controlled payload"]).unwrap(),
            10,
        );
        let mut tx = Transaction::new(transaction_config(f.root.path(), output), &f.store).unwrap();
        assert!(
            matches!(tx.prepare(&mut f.store, &mut 0, &["private-lookup".into()]), Err(code) if code == expected)
        );
        let failure = tx.take_failure(expected).unwrap();
        assert!(tx.take_failure(expected).is_none());
        f.publish(failure).unwrap();
        assert_eq!(f.count(), 1);
        assert!(matches!(
            tx.prepare(&mut f.store, &mut 0, &[]),
            Err("protocol")
        ));
        assert!(tx.take_failure("protocol").is_none());
    }
}

#[test]
fn successful_transaction_discards_facts_and_policy_refusal_is_consumed_once() {
    let _guard = FIXTURE.lock().unwrap_or_else(|e| e.into_inner());
    let mut f = Fixture::new("unused", 10);
    let mut tx = Transaction::new(
        transaction_config(f.root.path(), ok(&frame("TX1\n", &["ok", "", ""]).unwrap())),
        &f.store,
    )
    .unwrap();
    tx.prepare(&mut f.store, &mut 0, &["private-lookup".into()])
        .unwrap();
    assert!(tx.take_failure("storage").is_none());
    // A policy with no matching actual read refuses a syntactically valid DW1.
    let output = frame(
        "DW1\n",
        &[
            "fixture",
            "private-output",
            "domain",
            "operation",
            "1",
            "",
            "",
        ],
    )
    .unwrap();
    let config = crate::policy::Config {
        worker: worker(f.root.path(), "policy", "unused", ok(&output)),
        marker: "PE1\n".into(),
        values: 1,
        inputs: vec![Input::Read {
            namespace: "domain".into(),
            key: Key::Value { index: 0 },
        }],
        read_grants: BTreeMap::from([("domain".into(), Access::Read)]),
        alias: "fixture".into(),
        binding: "opaque".into(),
        claim_namespace: "claim".into(),
    };
    let mut policy = Policy::new(config, &f.store).unwrap();
    let mut effect = Bridge::new(worker(
        f.root.path(),
        "effect",
        "unused",
        ok("must-not-run"),
    ))
    .unwrap();
    assert!(matches!(
        policy.begin(
            &mut f.store,
            &f.scope,
            &mut effect,
            &mut 0,
            &["operation".into()]
        ),
        Err("binding")
    ));
    assert!(policy.take_failure("binding").is_some());
    assert!(policy.take_failure("binding").is_none());
    assert!(matches!(
        policy.begin(&mut f.store, &f.scope, &mut effect, &mut 0, &[]),
        Err("protocol")
    ));
    assert!(policy.take_failure("protocol").is_none());
    assert!(!f.root.path().join("effect-calls").exists());
}
