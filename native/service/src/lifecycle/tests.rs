use super::*;
use std::process::Command;

fn config() -> Config {
    Config {
        source: Source::PosixSigusr1,
    }
}

// A process-wide signal must not affect the rest of the Rust test runner.
// Every OS-disposition case executes in its own actual subprocess.
fn child(case: &str) {
    let result = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "lifecycle::tests::signal_child", "--nocapture"])
        .env("PI_LIFECYCLE_TEST_CASE", case)
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}\n{}",
        String::from_utf8_lossy(&result.stdout),
        String::from_utf8_lossy(&result.stderr)
    );
    assert!(String::from_utf8_lossy(&result.stdout).contains("signal-case-complete"));
}

#[test]
fn actual_signal_latches_once_and_new_handles_cannot_reset_it() {
    child("latch");
}

#[test]
fn preexisting_signal_owner_is_not_overwritten() {
    child("preexisting");
}

#[test]
fn changed_signal_owner_is_an_error_not_a_healthy_false() {
    child("changed");
}

#[test]
fn process_identity_is_checked_before_signal_queries() {
    child("pid");
}

#[test]
fn configuration_rejects_unknown_sources_nulls_and_extra_fields() {
    assert!(
        serde_json::from_value::<Config>(serde_json::json!({"source":"posix_sigusr1"})).is_ok()
    );
    for value in [
        serde_json::json!(null),
        serde_json::json!({}),
        serde_json::json!({"source":"SIGTERM"}),
        serde_json::json!({"source":true}),
        serde_json::json!({"source":"posix_sigusr1","requested":false}),
    ] {
        assert!(serde_json::from_value::<Config>(value).is_err());
    }
}

#[test]
fn manifest_does_not_claim_readiness_cancellation_or_quiescence() {
    let value = manifest();
    assert_eq!(value["observation"], "LF1");
    for key in [
        "reset",
        "readiness_or_admission_decision",
        "automatic_exit",
        "cancellation",
        "quiescence_or_delivery_guarantee",
    ] {
        assert_eq!(value[key], false);
    }
}

#[test]
fn signal_child() {
    let Ok(case) = std::env::var("PI_LIFECYCLE_TEST_CASE") else {
        return;
    };
    if case == "preexisting" {
        // SAFETY: isolated child owns its SIGUSR1 disposition; SIG_IGN is an OS constant.
        assert_ne!(
            unsafe { libc::signal(libc::SIGUSR1, libc::SIG_IGN) },
            libc::SIG_ERR
        );
        assert!(matches!(Latch::open(&config()), Err("signal_in_use")));
        assert_eq!(disposition().unwrap().sa_sigaction, libc::SIG_IGN);
    } else {
        let mut latch = Latch::open(&config()).unwrap();
        assert_eq!(
            crate::fields(&latch.observe().unwrap(), "LF1\n", 3).unwrap(),
            ["ok", "", "0"]
        );
        match case.as_str() {
            "latch" => {
                // SAFETY: the isolated child installed the static handler above.
                assert_eq!(unsafe { libc::raise(libc::SIGUSR1) }, 0);
                assert_eq!(latch.sample(), Ok(true));
                assert_eq!(Latch::open(&config()).unwrap().sample(), Ok(true));
                assert_eq!(unsafe { libc::raise(libc::SIGUSR1) }, 0);
                assert_eq!(
                    crate::fields(&latch.observe().unwrap(), "LF1\n", 3).unwrap(),
                    ["ok", "", "1"]
                );
            }
            "changed" => {
                assert_ne!(
                    unsafe { libc::signal(libc::SIGUSR1, libc::SIG_IGN) },
                    libc::SIG_ERR
                );
                assert_eq!(
                    crate::fields(&latch.observe().unwrap(), "LF1\n", 3).unwrap(),
                    ["error", "signal_owner_changed", ""]
                );
                assert!(matches!(
                    Latch::open(&config()),
                    Err("signal_owner_changed")
                ));
            }
            "pid" => {
                latch.pid = latch.pid.checked_add(1).unwrap();
                assert_eq!(
                    crate::fields(&latch.observe().unwrap(), "LF1\n", 3).unwrap(),
                    ["error", "signal_wrong_process", ""]
                );
            }
            _ => panic!("unknown child case"),
        }
    }
    println!("signal-case-complete");
}
