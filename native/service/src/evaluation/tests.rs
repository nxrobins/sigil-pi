//! Actual native transports with controlled replies, not compiled SIGIL evidence.
use super::*;
use crate::WorkerConfig;
use serde_json::json;
use std::collections::BTreeMap;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;
use std::sync::{Mutex, MutexGuard};
use std::time::Duration;

// These new mechanism fixtures each create several controlled runtime processes,
// not an offered-load workload. Limit only their mutual concurrency, before any
// worker or clock exists. Original tests, runner settings and deadlines stay put.
static EVALUATION_FIXTURE: Mutex<()> = Mutex::new(());

fn fixture_guard() -> MutexGuard<'static, ()> {
    EVALUATION_FIXTURE.lock().unwrap_or_else(|e| e.into_inner())
}

struct Fixture {
    directory: tempfile::TempDir,
    runtime: PathBuf,
    function: Function,
}
impl Fixture {
    fn new(cached: bool, inner: Value) -> Self {
        Self::wire(
            cached,
            json!({"content":[{"type":"text","text":inner.to_string()}]}),
        )
    }

    fn wire(cached: bool, result: Value) -> Self {
        let directory = tempfile::tempdir().unwrap();
        let runtime = directory.path().join("runtime");
        let reply = json!({"jsonrpc":"2.0","id":2,"result":result})
            .to_string()
            .replace("\"id\":2", "\"id\":'\"$n\"'");
        let body = format!(
            "#!/bin/sh\nread -r line\nprintf '%s\\n' '{{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{{}}}}'\nn=2\nwhile read -r line; do\nprintf '%s\\n' \"$line\" >> '{}'\nprintf '%s\\n' '{reply}'\nn=$((n + 1))\ndone\n",
            directory.path().join("calls").display()
        );
        fs::write(&runtime, &body).unwrap();
        fs::set_permissions(&runtime, fs::Permissions::from_mode(0o700)).unwrap();
        let source = directory.path().join("source");
        let text = "controlled native evaluator fixture, not SIGIL";
        fs::write(&source, text).unwrap();
        let config = WorkerConfig {
            version: 1,
            runtime: runtime.clone(),
            source,
            runtime_sha256: Content::new(body.as_bytes(), "utf8").sha256,
            source_sha256: Content::new(text.as_bytes(), "utf8").sha256,
            max_fuel: 1000,
            max_timeout_ms: 5000,
            net: vec![],
            fs: vec![],
            secret_env: BTreeMap::new(),
        };
        let function = if cached {
            Function::cached(config)
        } else {
            Function::new(config)
        }
        .unwrap();
        Self {
            directory,
            runtime,
            function,
        }
    }

    fn call(&mut self, input: &str) -> Evaluation {
        self.function
            .observe(input.into(), Instant::now() + Duration::from_secs(5))
    }

    fn calls(&self) -> Vec<Value> {
        fs::read_to_string(self.directory.path().join("calls"))
            .unwrap_or_default()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect()
    }
}

fn request_observations(facts: &Facts, cached: bool, sent: bool) {
    assert_eq!(facts.mode, if cached { "cached" } else { "fresh" });
    assert_eq!(
        facts.request_may_have_run,
        if cached { None } else { Some(sent) }
    );
    assert_eq!(facts.worker_reaped, if cached { None } else { Some(true) });
}

fn private_facts(facts: &Facts) -> String {
    let raw = serde_json::to_string(facts).unwrap();
    for secret in ["private-input", "private-output", "private-diagnostic"] {
        assert!(!raw.contains(secret));
    }
    assert!(raw.len() < 2048);
    raw
}

#[test]
fn actual_input_output_result_and_selected_limits_are_retained_for_both_modes() {
    let _guard = fixture_guard();
    for cached in [false, true] {
        let inner = json!({"status":"ok","data":{"output_text":"private-output"}});
        let mut f = Fixture::new(cached, inner.clone());
        let seen = f.call("private-input");
        let (output, timeout) = seen.result.unwrap();
        assert_eq!(output, "private-output");
        assert!(timeout > 0 && timeout <= 5000);
        assert_eq!(seen.facts.selected_timeout_ms, Some(timeout));
        assert_eq!(seen.facts.selected_fuel, 1000);
        assert_eq!(seen.facts.admitted_source_sha256, f.function.source_sha256);
        assert_eq!(
            seen.facts.admitted_runtime_sha256,
            f.function.runtime_sha256
        );
        assert_eq!(seen.facts.boundary, "complete");
        assert_eq!(seen.facts.error, None);
        assert_eq!(seen.facts.fault, None);
        let calls = f.calls();
        assert_eq!(calls.len(), 1);
        let actual_input = calls[0]["params"]["arguments"]["input"].as_str().unwrap();
        assert_eq!(actual_input, "private-input");
        assert_eq!(
            seen.facts.input_digest().unwrap(),
            Content::new(actual_input.as_bytes(), "utf8").sha256
        );
        assert_eq!(seen.facts.input_bytes(), actual_input.len());
        assert_eq!(
            seen.facts.output_digest().unwrap(),
            Content::new(output.as_bytes(), "utf8").sha256
        );
        let parsed = seen.facts.parsed_result.as_ref().unwrap();
        assert_eq!(parsed.representation, "serde-json-value/v1");
        assert_eq!(
            parsed.sha256,
            Content::new(&serde_json::to_vec(&inner).unwrap(), "serde-json-value/v1").sha256
        );
        request_observations(&seen.facts, cached, true);
        private_facts(&seen.facts);
    }
}

#[test]
fn runtime_error_keeps_actual_result_digest_without_inventing_denial_or_output() {
    let _guard = fixture_guard();
    for cached in [false, true] {
        let mut f = Fixture::new(
            cached,
            json!({"status":"error","error":{"message":"private-diagnostic","code":-403}}),
        );
        let seen = f.call("private-input");
        assert_eq!(
            seen.result,
            Err("application"),
            "{}",
            private_facts(&seen.facts)
        );
        assert_eq!(seen.facts.error, Some("application"));
        assert_eq!(seen.facts.boundary, "runtime_status");
        assert_eq!(seen.facts.runtime_status.as_deref(), Some("error"));
        assert!(seen.facts.parsed_result.is_some());
        assert!(seen.facts.selected_timeout_ms.unwrap() > 0);
        assert_eq!(seen.facts.output_kind, "missing");
        assert!(seen.facts.output.is_none());
        request_observations(&seen.facts, cached, true);
        let raw = private_facts(&seen.facts);
        for invented in ["authorized", "denied", "business_success", "claim_receipt"] {
            assert!(!raw.contains(invented));
        }
    }
}

#[test]
fn actual_empty_missing_and_non_string_outputs_remain_distinct() {
    let _guard = fixture_guard();
    for cached in [false, true] {
        for (data, kind, successful) in [
            (json!({"output_text":""}), "string", true),
            (json!({}), "missing", false),
            (json!({"output_text":null}), "non_string", false),
        ] {
            let mut f = Fixture::new(cached, json!({"status":"ok","data":data}));
            let seen = f.call("input");
            assert_eq!(seen.facts.output_kind, kind);
            if successful {
                assert_eq!(seen.result.unwrap().0, "");
                assert_eq!(
                    seen.facts.output_digest().unwrap(),
                    Content::new(b"", "utf8").sha256
                );
                assert_eq!(seen.facts.output.as_ref().unwrap().bytes, 0);
            } else {
                assert_eq!(seen.result, Err("protocol"));
                assert!(seen.facts.output.is_none());
                assert_eq!(seen.facts.boundary, "output");
            }
            assert!(seen.facts.parsed_result.is_some());
        }
    }
}

#[test]
fn runtime_error_with_an_actual_output_retains_its_digest_but_stays_an_error() {
    let _guard = fixture_guard();
    for cached in [false, true] {
        let mut f = Fixture::new(
            cached,
            json!({"status":"error","data":{"output_text":"private-output"}}),
        );
        let seen = f.call("private-input");
        assert_eq!(
            seen.result,
            Err("application"),
            "{}",
            private_facts(&seen.facts)
        );
        assert_eq!(seen.facts.output_kind, "string");
        assert_eq!(
            seen.facts.output_digest().unwrap(),
            Content::new(b"private-output", "utf8").sha256
        );
        private_facts(&seen.facts);
    }
}

#[test]
fn native_protocol_fault_retains_only_the_observations_the_bridge_supplies() {
    let _guard = fixture_guard();
    for cached in [false, true] {
        let mut f = Fixture::wire(cached, json!({"isError":true,"content":[]}));
        let seen = f.call("private-input");
        assert_eq!(seen.result, Err("worker"));
        assert_eq!(seen.facts.fault, Some(Fault::Protocol));
        assert_eq!(seen.facts.output_kind, "not_observed");
        assert!(seen.facts.output.is_none());
        assert!(seen.facts.parsed_result.is_none());
        assert!(seen.facts.runtime_status.is_none());
        request_observations(&seen.facts, cached, true);
        assert_eq!(f.calls().len(), 1);
        private_facts(&seen.facts);
    }
}

#[test]
fn expired_preinvoke_deadline_never_selects_a_timeout_or_invokes_the_runtime() {
    let _guard = fixture_guard();
    for cached in [false, true] {
        let mut f = Fixture::new(cached, json!({"status":"ok","data":{"output_text":"ok"}}));
        let seen = f.function.observe(
            "private-input".into(),
            Instant::now() - Duration::from_secs(1),
        );
        assert_eq!(seen.result, Err("deadline"));
        assert_eq!(seen.facts.boundary, "before_invoke");
        assert!(seen.facts.selected_timeout_ms.is_none());
        assert!(seen.facts.fault.is_none());
        assert!(seen.facts.request_may_have_run.is_none());
        assert!(seen.facts.worker_reaped.is_none());
        assert!(seen.facts.parsed_result.is_none());
        assert!(f.calls().is_empty());
        assert_eq!(f.call("later-input").result.unwrap().0, "ok");
        assert_eq!(f.calls().len(), 1);
    }
}

#[test]
fn oversized_input_observation_is_bounded_and_preserves_the_original_bridge_limit() {
    let _guard = fixture_guard();
    for cached in [false, true] {
        let mut f = Fixture::new(cached, json!({"status":"ok","data":{"output_text":"ok"}}));
        let seen = f.call(&"x".repeat(INPUT_LIMIT + 1));
        assert_eq!(seen.result, Err("worker"));
        assert_eq!(seen.facts.fault, Some(Fault::Limit));
        assert_eq!(seen.facts.input_bytes(), INPUT_LIMIT + 1);
        assert!(seen.facts.input.is_none());
        assert!(seen.facts.output.is_none());
        assert!(seen.facts.parsed_result.is_none());
        assert!(f.calls().is_empty());
        private_facts(&seen.facts);
    }
}

#[test]
fn changed_runtime_is_refused_and_no_successful_result_is_manufactured() {
    let _guard = fixture_guard();
    for cached in [false, true] {
        let mut f = Fixture::new(cached, json!({"status":"ok","data":{"output_text":"ok"}}));
        fs::write(&f.runtime, "replaced private fixture only").unwrap();
        let seen = f.call("private-input");
        assert_eq!(seen.result, Err("worker"));
        assert_eq!(seen.facts.fault, Some(Fault::Invalid));
        assert_eq!(
            seen.facts.admitted_runtime_sha256,
            f.function.runtime_sha256
        );
        assert_ne!(
            seen.facts.admitted_runtime_sha256,
            Content::new(b"replaced private fixture only", "utf8").sha256
        );
        assert!(seen.facts.output.is_none());
        assert!(seen.facts.parsed_result.is_none());
        request_observations(&seen.facts, cached, false);
        assert!(f.calls().is_empty());
    }
}

#[test]
fn legacy_wrappers_preserve_success_error_and_selected_timeout_contracts() {
    let _guard = fixture_guard();
    for cached in [false, true] {
        let mut f = Fixture::new(cached, json!({"status":"ok","data":{"output_text":"ok"}}));
        assert_eq!(
            f.function
                .invoke("a".into(), Instant::now() + Duration::from_secs(5)),
            Ok("ok".into())
        );
        let second = f
            .function
            .invoke_observed("b".into(), Instant::now() + Duration::from_secs(5))
            .unwrap();
        assert_eq!(second.0, "ok");
        assert!(second.1 > 0 && second.1 <= 5000);
        let calls = f.calls();
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[1]["id"], if cached { 3 } else { 2 });
        let mut failed = Fixture::new(cached, json!({"status":"error"}));
        assert_eq!(
            failed
                .function
                .invoke("a".into(), Instant::now() + Duration::from_secs(5)),
            Err("application")
        );
        assert_eq!(
            failed
                .function
                .invoke_observed("b".into(), Instant::now() + Duration::from_secs(5)),
            Err("application")
        );
    }
}
