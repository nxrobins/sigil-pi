use super::*;
use std::os::unix::fs::PermissionsExt;
use std::sync::{Mutex, MutexGuard};

// These fixtures deliberately saturate pipes or hold a child until a short
// deadline. Run one at a time so another fixture's flood/spawn workload cannot
// prevent this test from reaching the phase whose behavior it is asserting.
// Concurrent cancellation remains explicitly exercised within its own fixture.
static PROCESS_FIXTURE: Mutex<()> = Mutex::new(());
pub(super) struct Fixture {
    directory: tempfile::TempDir,
    _serial: MutexGuard<'static, ()>,
}
impl std::ops::Deref for Fixture {
    type Target = tempfile::TempDir;
    fn deref(&self) -> &Self::Target {
        &self.directory
    }
}
pub(super) fn fixture(script: &str) -> (Fixture, Bridge) {
    let serial = PROCESS_FIXTURE.lock().unwrap_or_else(|e| e.into_inner());
    let dir = tempfile::tempdir().unwrap();
    let runtime = dir.path().join("runtime");
    let body = format!("#!/bin/sh\n{script}\n")
        .replace("@MARKER@", dir.path().join("seen").to_str().unwrap());
    std::fs::write(&runtime, &body).unwrap();
    std::fs::set_permissions(&runtime, std::fs::Permissions::from_mode(0o700)).unwrap();
    let source = dir.path().join("program.sigil");
    std::fs::write(&source, "module fixture;").unwrap();
    let bridge = Bridge::new(Config {
        version: 1,
        runtime,
        runtime_sha256: digest(body.as_bytes()),
        source,
        source_sha256: digest(b"module fixture;"),
        max_fuel: 300_000_000,
        max_timeout_ms: 5000,
        net: vec![],
        fs: vec![],
        secret_env: BTreeMap::new(),
    })
    .unwrap();
    (
        Fixture {
            directory: dir,
            _serial: serial,
        },
        bridge,
    )
}
const HANDSHAKE: &str =
    "read -r line\nprintf '%s\\n' '{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}'\nread -r line\n";
const OK: &str = "printf '%s\\n' '{\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{\"content\":[{\"type\":\"text\",\"text\":\"{\\\"status\\\":\\\"ok\\\",\\\"data\\\":{\\\"output_text\\\":\\\"ok\\\"}}\"}]}}'\nwhile :; do /bin/sleep 1; done";

#[test]
fn duplicate_keys_at_any_depth_are_rejected() {
    for bad in [
        r#"{"x":1,"x":2}"#,
        r#"{"x":{"a":1,"\u0061":2}}"#,
        r#"[{"a":1,"a":2}]"#,
    ] {
        assert!(strict::parse(bad.as_bytes()).is_err());
    }
}
#[test]
fn worker_facts_are_an_explicit_non_secret_projection() {
    let (_dir, mut b) = fixture("exit 99");
    b.config.net = vec!["example.test".into()];
    b.config.fs = vec!["/workspace".into()];
    b.config
        .secret_env
        .insert("provider".into(), "PRIVATE_ENV_NAME".into());
    b.grants = json!({"secret":["provider=private-value-canary"]});
    let actual = serde_json::to_value(b.facts().unwrap()).unwrap();
    assert_eq!(
        actual,
        json!({
            "source_sha256": b.config.source_sha256,
            "runtime_sha256": b.config.runtime_sha256,
            "net":["example.test"], "fs":["/workspace"], "secret_names":["provider"],
            "max_fuel":300_000_000, "max_timeout_ms":5000,
        })
    );
    assert!(!actual.to_string().contains("PRIVATE_ENV_NAME"));
    assert!(!actual.to_string().contains("private-value-canary"));
    b.poisoned = true;
    assert!(matches!(b.facts(), Err(Fault::CleanupUnconfirmed)));
    b.poisoned = false;
    b.owner = b.owner.wrapping_add(1);
    assert!(matches!(b.facts(), Err(Fault::WrongProcess)));
}
#[test]
fn ticket_is_consumed_once_and_next_worker_is_fresh() {
    let (_dir, mut b) = fixture(&format!("{HANDSHAKE}{OK}"));
    let p = b.prepare("input".into(), 100, 2000).unwrap();
    assert_eq!(
        b.prepare("other".into(), 100, 2000).err(),
        Some(Fault::Busy)
    );
    assert_eq!(
        b.execute("wrong-ticket", &AtomicBool::new(false)).err(),
        Some(Fault::Ticket)
    );
    let o = b.execute(&p.ticket, &AtomicBool::new(false)).unwrap();
    assert_eq!(o.fault, None);
    assert!(o.worker_reaped && o.request_may_have_run);
    assert_eq!(o.result.unwrap()["data"]["output_text"], "ok");
    assert_eq!(
        b.execute(&p.ticket, &AtomicBool::new(false)).err(),
        Some(Fault::Ticket)
    );
    assert_ne!(
        b.prepare("next".into(), 100, 2000).unwrap().ticket,
        p.ticket
    );
}
#[test]
fn cancellation_before_start_does_not_spawn() {
    let (dir, mut b) = fixture(": > '@MARKER@'");
    let p = b.prepare("input".into(), 100, 1000).unwrap();
    let o = b.execute(&p.ticket, &AtomicBool::new(true)).unwrap();
    assert_eq!(o.fault, Some(Fault::Cancelled));
    assert!(!o.request_may_have_run);
    assert!(!dir.path().join("seen").exists());
}
#[test]
fn expired_ticket_never_sends_and_cannot_replay() {
    let (_dir, mut b) = fixture("exit 99");
    let p = b.prepare("input".into(), 100, 1).unwrap();
    std::thread::sleep(Duration::from_millis(5));
    let o = b.execute(&p.ticket, &AtomicBool::new(false)).unwrap();
    assert_eq!(o.fault, Some(Fault::Deadline));
    assert!(!o.request_may_have_run && o.worker_reaped);
    assert_eq!(
        b.execute(&p.ticket, &AtomicBool::new(false)).err(),
        Some(Fault::Ticket)
    );
}
#[test]
fn partial_response_cannot_bypass_deadline() {
    let (dir, mut b) = fixture(&format!(
        "{HANDSHAKE}: > '@MARKER@'; printf '{{'; /bin/sleep 20"
    ));
    let p = b.prepare("input".into(), 100, 1000).unwrap();
    let start = Instant::now();
    let o = b.execute(&p.ticket, &AtomicBool::new(false)).unwrap();
    assert_eq!(o.fault, Some(Fault::Deadline));
    assert!(o.request_may_have_run && o.worker_reaped);
    assert!(dir.path().join("seen").exists());
    assert!(start.elapsed() < Duration::from_secs(4));
}
#[test]
fn partial_handshake_is_definitely_before_forge() {
    let (_dir, mut b) = fixture("printf '{'; /bin/sleep 20");
    let p = b.prepare("input".into(), 100, 100).unwrap();
    let o = b.execute(&p.ticket, &AtomicBool::new(false)).unwrap();
    assert_eq!(o.fault, Some(Fault::Deadline));
    assert!(!o.request_may_have_run && o.worker_reaped);
}
#[test]
fn blocked_request_write_is_deadline_bounded() {
    let (_dir, mut b) = fixture(
        "read -r line; printf '%s\\n' '{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}'; /bin/sleep 20",
    );
    let p = b.prepare("x".repeat(1024 * 1024), 100, 1000).unwrap();
    let start = Instant::now();
    let o = b.execute(&p.ticket, &AtomicBool::new(false)).unwrap();
    assert_eq!(o.fault, Some(Fault::Deadline));
    // A non-reading pipe may accept a prefix or refuse the entire first write;
    // neither behavior may block the deadline. Do not invent successful bytes.
    assert!(o.worker_reaped);
    assert!(start.elapsed() < Duration::from_secs(4));
}
#[test]
fn cancellation_interrupts_a_running_partial_reply() {
    let (dir, mut b) = fixture(&format!(
        "{HANDSHAKE}: > '@MARKER@'; printf '{{'; /bin/sleep 20"
    ));
    let p = b.prepare("input".into(), 100, 3000).unwrap();
    let cancel = std::sync::Arc::new(AtomicBool::new(false));
    let signal = cancel.clone();
    let marker = dir.path().join("seen");
    let thread = std::thread::spawn(move || {
        let until = Instant::now() + Duration::from_secs(2);
        while !marker.exists() && Instant::now() < until {
            std::thread::sleep(Duration::from_millis(2));
        }
        assert!(
            marker.exists(),
            "worker did not reach the cancellation boundary"
        );
        signal.store(true, Ordering::Release);
    });
    let o = b.execute(&p.ticket, &cancel).unwrap();
    thread.join().unwrap();
    assert_eq!(o.fault, Some(Fault::Cancelled));
    assert!(o.worker_reaped && o.request_may_have_run);
}
#[test]
fn wrong_id_duplicates_and_multiple_frames_are_retired() {
    for reply in [
        r#"{"jsonrpc":"2.0","id":3,"result":{}}"#,
        r#"{"jsonrpc":"2.0","id":2,"id":2,"result":{}}"#,
        "{\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{}}\n{}",
    ] {
        let (dir, mut b) = fixture(&format!(
            "{HANDSHAKE}: > '@MARKER@'; printf '%s\\n' '{reply}'; /bin/sleep 20"
        ));
        let p = b.prepare("input".into(), 100, 1000).unwrap();
        let o = b.execute(&p.ticket, &AtomicBool::new(false)).unwrap();
        assert_eq!(
            o.fault,
            Some(Fault::Protocol),
            "reply={reply}; received={}; sent={}",
            dir.path().join("seen").exists(),
            o.request_may_have_run
        );
        assert!(o.worker_reaped);
    }
}
#[test]
fn stderr_flood_is_bounded_and_not_returned() {
    let (_dir, mut b) = fixture(&format!(
        "{HANDSHAKE}while :; do printf 'private-stderr-canary-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\\n' >&2; done"
    ));
    let p = b.prepare("input".into(), 100, 2000).unwrap();
    let o = b.execute(&p.ticket, &AtomicBool::new(false)).unwrap();
    assert_eq!(o.fault, Some(Fault::StderrLimit));
    assert!(o.worker_reaped);
    assert!(
        !serde_json::to_string(&o)
            .unwrap()
            .contains("private-stderr-canary")
    );
}
#[test]
fn oversized_stdout_without_a_frame_is_retired() {
    let (_dir, mut b) = fixture(&format!(
        "{HANDSHAKE}/usr/bin/head -c 16777217 /dev/zero; /bin/sleep 20"
    ));
    let p = b.prepare("input".into(), 100, 3000).unwrap();
    let o = b.execute(&p.ticket, &AtomicBool::new(false)).unwrap();
    assert_eq!(o.fault, Some(Fault::Limit));
    assert!(o.worker_reaped && o.request_may_have_run);
    assert!(o.result.is_none());
}
#[test]
fn changed_runtime_hash_prevents_start() {
    let (_dir, mut b) = fixture("exit 99");
    let p = b.prepare("input".into(), 100, 1000).unwrap();
    std::fs::write(&b.config.runtime, "changed").unwrap();
    let o = b.execute(&p.ticket, &AtomicBool::new(false)).unwrap();
    assert_eq!(o.fault, Some(Fault::Invalid));
    assert!(!o.request_may_have_run);
}
#[test]
fn unverified_override_and_parent_secret_environment_are_not_inherited() {
    // The environment is empty regardless of ambient variables; test a generic
    // variable without mutating process-global environment in concurrent tests.
    let (_dir, mut b) = fixture(&format!(
        "{HANDSHAKE}if [ -n \"$HOME$SIGIL_ALLOW_UNVERIFIED_CERT\" ]; then exit 9; fi\n{OK}"
    ));
    let p = b.prepare("input".into(), 100, 1000).unwrap();
    let o = b.execute(&p.ticket, &AtomicBool::new(false)).unwrap();
    assert_eq!(o.fault, None);
}
