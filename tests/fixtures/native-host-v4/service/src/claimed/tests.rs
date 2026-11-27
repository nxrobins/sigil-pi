use super::*;
use sigil_durable_store::store::{Access, Batch, Limits, Mutation, OpenMode};
use sigil_worker_bridge::{Config, Fault};
use std::collections::BTreeMap;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use tempfile::TempDir;

pub(super) struct Fixture {
    pub(super) directory: TempDir,
    pub(super) store: Store,
    pub(super) scope: Scope,
    pub(super) worker: Bridge,
    pub(super) clock: u64,
}
impl Fixture {
    pub(super) fn new() -> Self {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path().join("store");
        fs::create_dir(&root).unwrap();
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
        let mut store = Store::open(&root, OpenMode::CreateNew, Limits::default()).unwrap();
        let owner = store
            .scope(BTreeMap::from([("intent".into(), Access::ReadWrite)]))
            .unwrap();
        store
            .commit(
                &owner,
                &Batch {
                    checks: vec![],
                    writes: vec![Mutation {
                        namespace: "intent".into(),
                        key: "operation:1".into(),
                        revision: 0,
                        value: Some("opaque intent".into()),
                    }],
                },
            )
            .unwrap();
        let scope = store
            .scope(BTreeMap::from([
                ("intent".into(), Access::Read),
                ("claim".into(), Access::ReadWrite),
            ]))
            .unwrap();
        let runtime = directory.path().join("runtime");
        // A deterministic native protocol fixture, not compiler verification.
        let body = format!(
            "#!/bin/sh\n: > '{}'\nread -r line\nprintf '%s\\n' '{{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{{}}}}'\nread -r line\nprintf '%s\\n' '{{\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{{\"content\":[{{\"type\":\"text\",\"text\":\"{{\\\"status\\\":\\\"ok\\\",\\\"data\\\":{{\\\"output_text\\\":\\\"observed\\\"}}}}\"}}]}}}}'\nwhile :; do /bin/sleep 1; done\n",
            directory.path().join("started").display()
        );
        fs::write(&runtime, &body).unwrap();
        fs::set_permissions(&runtime, fs::Permissions::from_mode(0o700)).unwrap();
        let source = directory.path().join("source");
        fs::write(&source, "opaque source").unwrap();
        use sha2::{Digest, Sha256};
        let worker = Bridge::new(Config {
            version: 1,
            runtime,
            runtime_sha256: format!("{:x}", Sha256::digest(body)),
            source,
            source_sha256: format!("{:x}", Sha256::digest("opaque source")),
            max_fuel: 1000,
            max_timeout_ms: 5000,
            net: vec![],
            fs: vec![],
            secret_env: BTreeMap::new(),
        })
        .unwrap();
        Self {
            directory,
            store,
            scope,
            worker,
            clock: 0,
        }
    }
    fn begin(&mut self) -> Attempt<'_> {
        Attempt::begin(
            &mut self.store,
            &self.scope,
            &mut self.worker,
            &mut self.clock,
            binding(),
            execution(),
        )
        .unwrap()
    }
    fn did_not_start(&self) {
        assert!(!self.directory.path().join("started").exists());
    }
    fn claim_record(&self) -> Record {
        self.store.get(&self.scope, "claim", "operation:1").unwrap()
    }
}
pub(super) fn binding() -> Binding {
    Binding {
        intent_namespace: "intent".into(),
        claim_namespace: "claim".into(),
        key: "operation:1".into(),
    }
}
pub(super) fn execution() -> Execution {
    Execution {
        input: "exact frozen input".into(),
        fuel: 100,
        timeout_ms: 5000,
        time_guard: crate::frame("TG1\n", &["0", "9007199254740991"]).unwrap(),
    }
}
fn claim(a: &Attempt<'_>) -> serde_json::Value {
    let value = crate::frame(
        "SD1\n",
        &["intent", "operation:1", "1", &a.prepared().generation, "1"],
    )
    .unwrap();
    serde_json::json!({"op":"commit","checks":[{"namespace":"intent","key":"operation:1","revision":1}],
        "writes":[{"namespace":"claim","key":"operation:1","revision":0,"value":value}]})
}

// Native-only protocol fixture. Real compiler/policy evidence is exercised by
// pytest; this controlled responder isolates receipt and clock mechanics.
pub(super) fn fixture_recorder(
    directory: &std::path::Path,
    generation: &str,
    phase: &str,
    delay: u64,
) -> Recorder {
    fn reply(output: &str) -> String {
        serde_json::json!({"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text",
            "text":serde_json::json!({"status":"ok","data":{"output_text":output}}).to_string()}]}})
        .to_string()
    }
    let sd = |phase: &str| {
        crate::frame("SD1\n", &["intent", "operation:1", "1", generation, phase]).unwrap()
    };
    let checks = serde_json::json!([{"namespace":"intent","key":"operation:1","revision":1}]);
    let claim = serde_json::json!({"op":"commit","checks":checks,"writes":[{
        "namespace":"claim","key":"operation:1","revision":0,"value":sd("1")}]});
    let payload = if phase == "2" { "observed" } else { "" };
    let outcome = if phase == "2" { "returned" } else { "" };
    let dr = crate::frame(
        "DR1\n",
        &[
            "intent",
            "operation:1",
            "1",
            generation,
            phase,
            outcome,
            payload,
        ],
    )
    .unwrap();
    let terminal = serde_json::json!({"op":"commit","checks":checks,"writes":[
        {"namespace":"claim","key":"operation:1","revision":1,"value":sd(phase)},
        {"namespace":"delivery","key":"operation:1","revision":0,"value":dr}]});
    let before =
        crate::frame("ER1\n", &["1", "dispatch_after_commit", &claim.to_string()]).unwrap();
    let after = crate::frame(
        "ER1\n",
        &[phase, "record_after_commit", &terminal.to_string()],
    )
    .unwrap();
    let runtime = directory.join("recorder-runtime");
    let body = format!(
        "#!/bin/sh\nread -r line\nprintf '%s\\n' '{{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{{}}}}'\nread -r line\nprintf '%s\\n' \"$line\" > '{}'\ncase \"$line\" in\n*prepared*) printf '%s\\n' '{}' ;;\n*) /bin/sleep {}; printf '%s\\n' '{}' ;;\nesac\nwhile :; do /bin/sleep 1; done\n",
        directory.join("recorder-input").display(),
        reply(&before),
        delay,
        reply(&after)
    );
    fs::write(&runtime, &body).unwrap();
    fs::set_permissions(&runtime, fs::Permissions::from_mode(0o700)).unwrap();
    let source = directory.join("recorder-source");
    fs::write(&source, "native protocol fixture, not SIGIL").unwrap();
    use sha2::{Digest, Sha256};
    Recorder::new(RecorderConfig {
        delivery_namespace: "delivery".into(),
        worker: Config {
            version: 1,
            runtime,
            runtime_sha256: format!("{:x}", Sha256::digest(body)),
            source,
            source_sha256: format!("{:x}", Sha256::digest("native protocol fixture, not SIGIL")),
            max_fuel: 1000,
            max_timeout_ms: 5000,
            net: vec![],
            fs: vec![],
            secret_env: BTreeMap::new(),
        },
    })
    .unwrap()
}

pub(super) fn recorded_scope(f: &mut Fixture, delivery: Access) {
    f.scope = f
        .store
        .scope(BTreeMap::from([
            ("intent".into(), Access::Read),
            ("claim".into(), Access::ReadWrite),
            ("delivery".into(), delivery),
        ]))
        .unwrap();
}

#[test]
fn recorded_cancellation_binds_the_actual_no_send_observation_and_commits_once() {
    let mut f = Fixture::new();
    recorded_scope(&mut f, Access::CreateOnly);
    let directory = f.directory.path().to_owned();
    {
        let mut a = f.begin();
        let ticket = a.prepared.ticket.clone();
        let mut recorder = fixture_recorder(&directory, &a.prepared.generation, "3", 0);
        let result = a
            .run_recorded(&ticket, &mut recorder, &AtomicBool::new(true))
            .unwrap();
        assert_eq!(result.phase.as_deref(), Some("3"));
        assert!(result.delivery_receipt.is_some() && result.recording_error.is_none());
        let seen = result.observation.unwrap();
        assert!(!seen.request_may_have_run && seen.worker_reaped);
        assert_eq!(seen.fault, Some(Fault::Cancelled));
        assert_eq!(
            a.run_recorded(&ticket, &mut recorder, &AtomicBool::new(false))
                .err(),
            Some("ticket")
        );
    }
    f.did_not_start();
    let raw = fs::read_to_string(directory.join("recorder-input")).unwrap();
    let actual: serde_json::Value = serde_json::from_str(&raw).unwrap();
    let incoming = crate::fields(
        actual["params"]["arguments"]["input"].as_str().unwrap(),
        "WR1\n",
        10,
    )
    .unwrap();
    let facts = crate::fields(incoming[9], "WF1\n", 9).unwrap();
    assert_eq!(&facts[2..], ["0", "1", "cancelled", "", "missing", "0", ""]);
    assert_eq!(f.claim_record().revision, 2);
}

#[test]
fn recorded_results_can_be_committed_after_the_dispatch_authority_expires() {
    let mut f = Fixture::new();
    recorded_scope(&mut f, Access::CreateOnly);
    let directory = f.directory.path().to_owned();
    {
        let mut a = f.begin();
        let ticket = a.prepared.ticket.clone();
        let mut recorder = fixture_recorder(&directory, &a.prepared.generation, "2", 0);
        a.claim(&claim(&a).to_string()).unwrap();
        let observed = a.execute(&ticket, &AtomicBool::new(false)).unwrap();
        assert!(observed.request_may_have_run && observed.worker_reaped);
        // Advance the dispatch boundary only AFTER the actual observation. This
        // tests the same internal continuation used by run_recorded without a
        // wall-clock race between a three-second guard and fixture startup.
        a.guard = Guard::parse(&crate::frame("TG1\n", &["0", "1"]).unwrap()).unwrap();
        assert_eq!(
            admit_time(
                Some(&a.guard),
                a.last_clock,
                Instant::now() + Duration::from_secs(1),
                now
            ),
            Err("time_guard")
        );
        a.deadline = Instant::now();
        assert!(Instant::now() >= a.deadline);
        let (phase, receipt) = a
            .persist_observation(&mut recorder, Some(&observed), None)
            .unwrap();
        assert_eq!(phase, "2");
        assert_eq!(receipt.revision, 3);
    }
    assert_eq!(f.claim_record().revision, 2);
}

#[test]
fn recorded_run_checks_actual_delivery_authority_before_claim_or_effect() {
    let mut f = Fixture::new();
    recorded_scope(&mut f, Access::Read);
    let directory = f.directory.path().to_owned();
    {
        let mut a = f.begin();
        let ticket = a.prepared.ticket.clone();
        let mut recorder = fixture_recorder(&directory, &a.prepared.generation, "2", 0);
        assert_eq!(
            a.run_recorded(&ticket, &mut recorder, &AtomicBool::new(false))
                .err(),
            Some("storage")
        );
    }
    assert_eq!(f.claim_record().revision, 0);
    f.did_not_start();
    assert!(!directory.join("recorder-input").exists());
}

#[test]
fn only_our_actual_commit_arms_exactly_one_execution() {
    let mut f = Fixture::new();
    {
        let mut a = f.begin();
        assert_eq!(a.intent().value.as_deref(), Some("opaque intent"));
        let ticket = a.prepared().ticket.clone();
        assert_eq!(a.claim(&claim(&a).to_string()).unwrap().revision, 2);
        // A global receipt of 2 does not make the fresh record revision 2.
        let o = a.execute(&ticket, &AtomicBool::new(false)).unwrap();
        assert!(o.request_may_have_run && o.worker_reaped);
        assert_eq!(o.fault, None);
        assert_eq!(o.result.unwrap()["data"]["output_text"], "observed");
        assert!(a.execute(&ticket, &AtomicBool::new(false)).is_err());
    }
    assert!(f.directory.path().join("started").exists());
    assert_eq!(f.claim_record().revision, 1);
}

#[test]
fn execute_before_claim_retires_the_attempt_without_spawning() {
    let mut f = Fixture::new();
    let ticket;
    {
        let mut a = f.begin();
        ticket = a.prepared().ticket.clone();
        assert_eq!(
            a.execute(&ticket, &AtomicBool::new(false)).err(),
            Some("unclaimed")
        );
        assert_eq!(a.claim(&claim(&a).to_string()).err(), Some("ticket"));
    }
    assert_eq!(
        f.worker.execute(&ticket, &AtomicBool::new(false)).err(),
        Some(Fault::Ticket)
    );
    assert_eq!(f.claim_record().revision, 0);
    f.did_not_start();
}

#[test]
fn wrong_or_expanded_claim_batches_cannot_write_or_arm() {
    for variant in 0..12 {
        let mut f = Fixture::new();
        let mut a = f.begin();
        let mut batch = claim(&a);
        match variant {
            0 => batch["checks"] = serde_json::json!([]),
            1 => batch["checks"][0]["namespace"] = "other".into(),
            2 => batch["checks"][0]["key"] = "another".into(),
            3 => batch["checks"][0]["revision"] = 0.into(),
            4 => batch["writes"][0]["namespace"] = "other".into(),
            5 => batch["writes"][0]["key"] = "another".into(),
            6 => batch["writes"][0]["revision"] = 1.into(),
            7 => batch["writes"][0]["value"] = serde_json::Value::Null,
            8 => batch["writes"][0]["value"] = "".into(),
            9 => batch["writes"].as_array_mut().unwrap().push(
                serde_json::json!({"namespace":"claim","key":"extra","revision":0,"value":"extra"}),
            ),
            10 => batch["receipt"] = serde_json::json!({"revision":2}),
            _ => batch["op"] = "reply".into(),
        }
        let ticket = a.prepared().ticket.clone();
        assert!(a.claim(&batch.to_string()).is_err(), "variant {variant}");
        assert!(a.execute(&ticket, &AtomicBool::new(false)).is_err());
        drop(a);
        assert_eq!(f.claim_record().revision, 0);
        f.did_not_start();
    }
}

#[test]
fn claim_record_must_bind_exact_intent_generation_and_claim_phase() {
    for field in 0..7 {
        let mut f = Fixture::new();
        let mut a = f.begin();
        let mut values = [
            "intent".to_owned(),
            "operation:1".into(),
            "1".into(),
            a.prepared().generation.clone(),
            "1".into(),
        ];
        if field < 5 {
            values[field] = "other".into();
        }
        if field == 5 {
            values[4] = "4".into();
        }
        let mut raw = crate::frame(
            "SD1\n",
            &values.iter().map(String::as_str).collect::<Vec<_>>(),
        )
        .unwrap();
        if field == 6 {
            raw.push('x');
        }
        let mut batch = claim(&a);
        batch["writes"][0]["value"] = raw.into();
        assert!(a.claim(&batch.to_string()).is_err());
        let ticket = a.prepared().ticket.clone();
        assert!(a.execute(&ticket, &AtomicBool::new(false)).is_err());
        drop(a);
        assert_eq!(f.claim_record().revision, 0);
        f.did_not_start();
    }
}

#[test]
fn failed_store_commit_cannot_arm_a_ticket() {
    let mut f = Fixture::new();
    f.scope = f
        .store
        .scope(BTreeMap::from([
            ("intent".into(), Access::Read),
            ("claim".into(), Access::Read),
        ]))
        .unwrap();
    {
        let mut a = f.begin();
        let ticket = a.prepared().ticket.clone();
        assert_eq!(a.claim(&claim(&a).to_string()).err(), Some("storage"));
        assert!(a.execute(&ticket, &AtomicBool::new(false)).is_err());
    }
    assert_eq!(f.claim_record().revision, 0);
    f.did_not_start();
}

#[test]
fn abandoned_unclaimed_ticket_is_retired_and_next_ticket_is_fresh() {
    let mut f = Fixture::new();
    let old = f.begin().prepared().ticket.clone();
    assert_eq!(
        f.worker.execute(&old, &AtomicBool::new(false)).err(),
        Some(Fault::Ticket)
    );
    let mut a = f.begin();
    assert_ne!(a.prepared().ticket, old);
    a.claim(&claim(&a).to_string()).unwrap();
    assert_eq!(
        a.execute(&old, &AtomicBool::new(false)).err(),
        Some("ticket")
    );
    drop(a);
    f.did_not_start();
}

#[test]
fn retained_claim_or_tombstone_blocks_new_preparation_across_reopen() {
    for tombstone in [false, true] {
        let mut f = Fixture::new();
        {
            let mut a = f.begin();
            a.claim(&claim(&a).to_string()).unwrap();
        }
        if tombstone {
            f.store
                .commit(
                    &f.scope,
                    &Batch {
                        checks: vec![],
                        writes: vec![Mutation {
                            namespace: "claim".into(),
                            key: "operation:1".into(),
                            revision: 1,
                            value: None,
                        }],
                    },
                )
                .unwrap();
        }
        let root = f.directory.path().join("store");
        drop(f.store);
        f.store = Store::open(&root, OpenMode::Existing, Limits::default()).unwrap();
        f.scope = f
            .store
            .scope(BTreeMap::from([
                ("intent".into(), Access::Read),
                ("claim".into(), Access::ReadWrite),
            ]))
            .unwrap();
        assert_eq!(
            Attempt::begin(
                &mut f.store,
                &f.scope,
                &mut f.worker,
                &mut f.clock,
                binding(),
                execution()
            )
            .err(),
            Some("claimed")
        );
        f.did_not_start();
    }
}

#[test]
fn changed_claim_or_intent_prevents_execution_even_with_a_real_receipt() {
    for target in ["intent", "claim"] {
        let mut f = Fixture::new();
        let owner = f
            .store
            .scope(BTreeMap::from([(target.into(), Access::ReadWrite)]))
            .unwrap();
        let mut a = f.begin();
        let ticket = a.prepared().ticket.clone();
        a.claim(&claim(&a).to_string()).unwrap();
        // Private test access simulates a future embedding regression. Public
        // callers cannot mutate this exclusively borrowed Store during an attempt.
        a.store
            .commit(
                &owner,
                &Batch {
                    checks: vec![],
                    writes: vec![Mutation {
                        namespace: target.into(),
                        key: "operation:1".into(),
                        revision: 1,
                        value: Some("changed".into()),
                    }],
                },
            )
            .unwrap();
        assert_eq!(
            a.execute(&ticket, &AtomicBool::new(false)).err(),
            Some("claim")
        );
        drop(a);
        f.did_not_start();
    }
}

#[test]
fn fresh_time_checks_apply_at_claim_and_execution() {
    for after_claim in [false, true] {
        let mut f = Fixture::new();
        let mut a = f.begin();
        let ticket = a.prepared().ticket.clone();
        if after_claim {
            a.claim(&claim(&a).to_string()).unwrap();
        }
        a.guard = Guard::parse(&crate::frame("TG1\n", &["0", "1"]).unwrap()).unwrap();
        if after_claim {
            assert_eq!(
                a.execute(&ticket, &AtomicBool::new(false)).err(),
                Some("time_guard")
            );
        } else {
            assert_eq!(a.claim(&claim(&a).to_string()).err(), Some("time_guard"));
        }
        drop(a);
        assert_eq!(f.claim_record().revision, u64::from(after_claim));
        f.did_not_start();
    }
}

#[test]
fn monotonic_overrun_and_clock_regression_do_not_dispatch() {
    for regression in [false, true] {
        let mut f = Fixture::new();
        let mut a = f.begin();
        let ticket = a.prepared().ticket.clone();
        a.claim(&claim(&a).to_string()).unwrap();
        if regression {
            *a.last_clock = u64::MAX;
        } else {
            a.deadline = Instant::now();
        }
        assert_eq!(
            a.execute(&ticket, &AtomicBool::new(false)).err(),
            Some(if regression { "clock" } else { "deadline" })
        );
        drop(a);
        f.did_not_start();
    }
}

#[test]
fn cancellation_keeps_the_claim_and_reports_definitely_unsent() {
    let mut f = Fixture::new();
    let mut a = f.begin();
    let ticket = a.prepared().ticket.clone();
    a.claim(&claim(&a).to_string()).unwrap();
    let o = a.execute(&ticket, &AtomicBool::new(true)).unwrap();
    assert_eq!(o.fault, Some(Fault::Cancelled));
    assert!(!o.request_may_have_run && o.worker_reaped);
    drop(a);
    assert_eq!(f.claim_record().revision, 1);
    f.did_not_start();
}

#[test]
fn foreign_scope_and_missing_intent_do_not_prepare() {
    let mut f = Fixture::new();
    let other = Fixture::new();
    assert_eq!(
        Attempt::begin(
            &mut f.store,
            &other.scope,
            &mut f.worker,
            &mut f.clock,
            binding(),
            execution()
        )
        .err(),
        Some("storage")
    );
    let mut missing = binding();
    missing.key = "missing".into();
    assert_eq!(
        Attempt::begin(
            &mut f.store,
            &f.scope,
            &mut f.worker,
            &mut f.clock,
            missing,
            execution()
        )
        .err(),
        Some("intent")
    );
    f.did_not_start();
}
