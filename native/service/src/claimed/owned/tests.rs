//! Native process/receipt tests, not compiler or real-service qualification.
use super::*;
use crate::claimed::tests::{Fixture, binding, execution, fixture_recorder, recorded_scope};
use sigil_durable_store::store::{Access, Batch, Mutation};
use sigil_worker_bridge::{Config, Fault};
use std::collections::BTreeMap;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::sync::{Mutex, MutexGuard};
use tempfile::TempDir;

static PROCESS_TESTS: Mutex<()> = Mutex::new(());
fn serial() -> MutexGuard<'static, ()> {
    PROCESS_TESTS.lock().unwrap_or_else(|e| e.into_inner())
}

fn component(directory: &Path, name: &str, body: &str) -> Config {
    use sha2::{Digest, Sha256};
    let runtime = directory.join(format!("{name}-runtime"));
    fs::write(&runtime, body).unwrap();
    fs::set_permissions(&runtime, fs::Permissions::from_mode(0o700)).unwrap();
    let source = directory.join(format!("{name}-source"));
    let code = "native protocol fixture, not SIGIL";
    fs::write(&source, code).unwrap();
    Config {
        version: 1,
        runtime,
        runtime_sha256: format!("{:x}", Sha256::digest(body)),
        source,
        source_sha256: format!("{:x}", Sha256::digest(code)),
        max_fuel: 1000,
        max_timeout_ms: 5000,
        net: vec![],
        fs: vec![],
        secret_env: BTreeMap::new(),
    }
}

fn response(output: &str) -> String {
    serde_json::json!({"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text",
        "text":serde_json::json!({"status":"ok","data":{"output_text":output}}).to_string()}]}})
    .to_string()
}

fn held_worker(directory: &Path) -> Bridge {
    let body = format!(
        "#!/bin/sh\nread -r line\nprintf '%s\\n' '{{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{{}}}}'\nread -r line\n: > '{}'\nwhile [ ! -f '{}' ]; do /bin/sleep 0.01; done\nprintf '%s\\n' '{}'\nwhile :; do /bin/sleep 1; done\n",
        directory.join("effect-received").display(),
        directory.join("release").display(),
        response("observed"),
    );
    Bridge::new(component(directory, "held", &body)).unwrap()
}

struct Running {
    directory: TempDir,
    store: Store,
    owner: Scope,
    lane: OwnedWorker,
    started: Started,
    clock: u64,
}
impl Running {
    fn new(phase: &str, expire: bool) -> Self {
        let mut f = Fixture::new();
        recorded_scope(&mut f, Access::CreateOnly);
        let Fixture {
            directory,
            mut store,
            scope,
            mut worker,
            mut clock,
        } = f;
        // Replace only this test's admitted effect fixture, before preparation.
        if !expire {
            worker = held_worker(directory.path());
        }
        let attempt = Attempt::begin(
            &mut store,
            &scope,
            &mut worker,
            &mut clock,
            binding(),
            execution(),
        )
        .unwrap();
        let mut recorder =
            fixture_recorder(directory.path(), &attempt.prepared.generation, phase, 0);
        let mut bound = attempt.handoff(&mut recorder).unwrap();
        if expire {
            // Deterministic fault at the handoff/thread-start boundary, after an
            // actual claim. No wall-clock scheduling race or timeout relaxation.
            bound.guard = Guard::parse(&frame("TG1\n", &["0", "1"]).unwrap()).unwrap();
        }
        let owner = store
            .scope(BTreeMap::from([
                ("intent".into(), Access::ReadWrite),
                ("claim".into(), Access::ReadWrite),
                ("delivery".into(), Access::ReadWrite),
                ("api".into(), Access::ReadWrite),
            ]))
            .unwrap();
        let mut lane = OwnedWorker::new(worker, recorder, scope);
        let started = lane.launch(bound, "opaque SIGIL context".into());
        Self {
            directory,
            store,
            owner,
            lane,
            started,
            clock,
        }
    }
    fn wait_received(&self) {
        wait_until(|| self.directory.path().join("effect-received").exists());
    }
    fn release(&self) {
        fs::write(self.directory.path().join("release"), b"").unwrap();
    }
    fn finish(&mut self) -> OwnedCompletion {
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            if let Some(result) = self.lane.poll(&mut self.store, &mut self.clock).unwrap() {
                return result;
            }
            assert!(Instant::now() < deadline, "effect did not finish");
            thread::sleep(Duration::from_millis(5));
        }
    }
    fn get(&self, namespace: &str) -> Record {
        self.store
            .get(&self.owner, namespace, "operation:1")
            .unwrap()
    }
    fn write(&mut self, namespace: &str, revision: u64, value: &str) {
        self.store
            .commit(
                &self.owner,
                &Batch {
                    checks: vec![],
                    writes: vec![Mutation {
                        namespace: namespace.into(),
                        key: "operation:1".into(),
                        revision,
                        value: Some(value.into()),
                    }],
                },
            )
            .unwrap();
    }
    fn received_facts(&self) -> Vec<String> {
        let raw = fs::read_to_string(self.directory.path().join("recorder-input")).unwrap();
        let actual: serde_json::Value = serde_json::from_str(&raw).unwrap();
        let input = actual["params"]["arguments"]["input"].as_str().unwrap();
        let wrapped = crate::fields(input, "WR1\n", 10).unwrap();
        crate::fields(wrapped[9], "WF1\n", 9)
            .unwrap()
            .into_iter()
            .map(str::to_owned)
            .collect()
    }
}

fn wait_until(mut predicate: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(4);
    while !predicate() {
        assert!(
            Instant::now() < deadline,
            "fixture did not reach held boundary"
        );
        thread::sleep(Duration::from_millis(5));
    }
}

#[test]
fn pending_effect_releases_store_and_records_actual_result_once() {
    let _serial = serial();
    let mut r = Running::new("2", false);
    r.wait_received();
    assert_eq!(r.started.claim_receipt.revision, 2);
    assert_eq!(r.get("claim").revision, 1);
    let recorder_input = fs::read(r.directory.path().join("recorder-input")).unwrap();
    for _ in 0..10 {
        assert!(r.lane.poll(&mut r.store, &mut r.clock).unwrap().is_none());
    }
    assert_eq!(
        fs::read(r.directory.path().join("recorder-input")).unwrap(),
        recorder_input
    );
    // A real commit on the SAME store succeeds while the worker is still held.
    r.write("api", 0, "another request committed");
    assert_eq!(
        r.get("api").value.as_deref(),
        Some("another request committed")
    );
    r.release();
    let finished = r.finish();
    assert!(!finished.owner_lost);
    assert_eq!(finished.context, "opaque SIGIL context");
    assert_eq!(finished.completion.claim_receipt.revision, 2);
    assert_eq!(finished.completion.delivery_receipt.unwrap().revision, 4);
    assert_eq!(finished.completion.phase.as_deref(), Some("2"));
    let seen = finished.completion.observation.unwrap();
    assert!(seen.request_may_have_run && seen.worker_reaped);
    assert_eq!(seen.generation, r.started.generation);
    assert_eq!(seen.result.unwrap()["data"]["output_text"], "observed");
    assert_eq!(r.lane.poll(&mut r.store, &mut r.clock).err(), Some("idle"));
    assert!(!r.lane.request_cancel());
    assert_eq!(r.get("claim").revision, 2);
    assert_eq!(r.get("delivery").revision, 1);
}

#[test]
fn owner_inspection_observes_in_flight_work_without_join_cancel_or_dispatch() {
    let _serial = serial();
    let mut r = Running::new("2", false);
    r.wait_received();
    let claim = r.get("claim");
    let before = fs::read(r.directory.path().join("recorder-input")).unwrap();
    assert_eq!(r.lane.inspect_owners(), Ok(1));
    assert_eq!(r.get("claim"), claim);
    assert_eq!(
        fs::read(r.directory.path().join("recorder-input")).unwrap(),
        before
    );
    assert!(
        !r.lane
            .flight
            .as_ref()
            .unwrap()
            .cancel
            .load(Ordering::Acquire)
    );
    r.release();
    assert!(!r.finish().owner_lost);
    assert_eq!(r.lane.inspect_owners(), Ok(0));
    let worker = r.lane.worker.take().unwrap();
    assert_eq!(r.lane.inspect_owners(), Err("owner_unavailable"));
    r.lane.worker = Some(worker);
    assert_eq!(r.lane.inspect_owners(), Ok(0));
}

#[test]
fn cancellation_signals_actual_held_worker_and_preserves_possible_delivery() {
    let _serial = serial();
    let mut r = Running::new("4", false);
    r.wait_received();
    assert!(r.lane.request_cancel());
    let finished = r.finish();
    let seen = finished.completion.observation.unwrap();
    assert_eq!(seen.fault, Some(Fault::Cancelled));
    assert!(seen.request_may_have_run && seen.worker_reaped);
    assert_eq!(finished.completion.phase.as_deref(), Some("4"));
    let facts = r.received_facts();
    assert_eq!(facts[0], "observed");
    assert_eq!(&facts[2..5], ["1", "1", "cancelled"]);
    assert!(!r.directory.path().join("release").exists());
}

#[test]
fn authority_is_checked_again_on_execution_thread_before_worker_start() {
    let _serial = serial();
    let mut r = Running::new("3", true);
    let result = r.finish();
    assert_eq!(result.completion.refused, Some("time_guard"));
    assert!(result.completion.observation.is_none());
    assert_eq!(result.completion.phase.as_deref(), Some("3"));
    assert!(!r.directory.path().join("started").exists());
    assert_eq!(
        &r.received_facts()[0..5],
        ["refused", &r.started.generation, "0", "1", "time_guard"]
    );
}

#[test]
fn newer_claim_cannot_be_overwritten_by_late_completion() {
    let _serial = serial();
    let mut r = Running::new("2", false);
    r.wait_received();
    r.write("claim", 1, "newer recovery state");
    r.release();
    let result = r.finish();
    assert_eq!(result.completion.recording_error, Some("claim"));
    assert!(result.completion.delivery_receipt.is_none());
    assert_eq!(
        r.get("claim").value.as_deref(),
        Some("newer recovery state")
    );
    assert_eq!(r.get("delivery").revision, 0);
}

#[test]
fn changed_intent_cannot_receive_an_old_observation() {
    let _serial = serial();
    let mut r = Running::new("2", false);
    r.wait_received();
    r.write("intent", 1, "changed immutable intent");
    r.release();
    let result = r.finish();
    assert_eq!(result.completion.recording_error, Some("intent"));
    assert_eq!(r.get("claim").revision, 1);
    assert_eq!(r.get("delivery").revision, 0);
}

#[test]
fn occupied_delivery_slot_is_not_overwritten() {
    let _serial = serial();
    let mut r = Running::new("2", false);
    r.wait_received();
    r.write("delivery", 0, "already retained delivery");
    r.release();
    let result = r.finish();
    assert_eq!(result.completion.recording_error, Some("delivery"));
    assert_eq!(
        r.get("delivery").value.as_deref(),
        Some("already retained delivery")
    );
    assert_eq!(r.get("claim").revision, 1);
}

#[test]
fn completion_cannot_use_the_same_namespace_names_in_a_different_store() {
    let _serial = serial();
    let mut r = Running::new("2", false);
    let mut other = Fixture::new();
    r.wait_received();
    r.release();
    wait_until(|| {
        r.lane
            .flight
            .as_ref()
            .unwrap()
            .thread
            .as_ref()
            .unwrap()
            .is_finished()
    });
    let result = r
        .lane
        .poll(&mut other.store, &mut r.clock)
        .unwrap()
        .unwrap();
    assert_eq!(result.completion.recording_error, Some("storage"));
    assert!(result.completion.delivery_receipt.is_none());
    assert_eq!(r.get("claim").revision, 1);
    assert_eq!(
        other
            .store
            .get(&other.scope, "claim", "operation:1")
            .unwrap()
            .revision,
        0
    );
}

#[test]
fn dropping_a_lane_requests_cleanup_but_leaves_claim_consumed() {
    let _serial = serial();
    let mut r = Running::new("4", false);
    r.wait_received();
    // Retain the real join handle only to verify cleanup in this test. Production
    // callers cannot take it and Drop itself does not claim a cleanup receipt.
    let handle = r.lane.flight.as_mut().unwrap().thread.take().unwrap();
    let cancel = Arc::clone(&r.lane.flight.as_ref().unwrap().cancel);
    drop(r.lane);
    assert!(cancel.load(Ordering::Acquire));
    let report = handle.join().unwrap();
    let observation = report.observation.unwrap();
    assert_eq!(observation.fault, Some(Fault::Cancelled));
    assert!(observation.worker_reaped && observation.request_may_have_run);
    assert_eq!(
        r.store
            .get(&r.owner, "claim", "operation:1")
            .unwrap()
            .revision,
        1
    );
    assert_eq!(
        r.store
            .get(&r.owner, "delivery", "operation:1")
            .unwrap()
            .revision,
        0
    );
}

#[test]
fn lost_execution_owner_records_uncertainty_and_cannot_supply_a_replacement_worker() {
    let _serial = serial();
    let mut r = Running::new("4", false);
    r.wait_received();
    r.lane.request_cancel();
    let original = r.lane.flight.as_mut().unwrap().thread.take().unwrap();
    // Fault injection at the private ownership channel, after verifying fixture
    // cleanup. No production method accepts this handle or a supplied result.
    let observed = original.join().unwrap();
    assert!(observed.observation.unwrap().worker_reaped);
    let failed = thread::spawn(|| -> WorkerReport { panic!("injected controller loss") });
    wait_until(|| failed.is_finished());
    r.lane.flight.as_mut().unwrap().thread = Some(failed);
    let result = r.finish();
    assert!(result.owner_lost);
    assert!(result.completion.refused.is_none() && result.completion.observation.is_none());
    assert_eq!(result.completion.phase.as_deref(), Some("4"));
    assert!(r.lane.worker.is_none());
    let facts = r.received_facts();
    assert_eq!(facts[0], "abandoned");
    assert_eq!(&facts[2..5], ["1", "0", "owner_unavailable"]);
}
