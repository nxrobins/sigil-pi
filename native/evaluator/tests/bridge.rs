//! Actual supervised parent/child boundary, not an in-process evaluator mock.
use sha2::{Digest, Sha256};
use sigil_worker_bridge::{Config, Fault, PureBridge};
use std::collections::BTreeMap;
use std::path::PathBuf;

const ECHO: &str =
    "module echo; pub fn tool_main(p: i64, n: i64) -> i64 @Internal { return (p << 32) | n; }";
const SLOW: &str = r#"module bounded;
pub fn tool_main(p: i64, n: i64) -> i64 @Internal {
    if n == 4 { while true { } }
    return (p << 32) | n;
}"#;

fn config(source: &str) -> (tempfile::TempDir, Config) {
    let directory = tempfile::tempdir().unwrap();
    let source_path = directory.path().join("fixed.sigil");
    std::fs::write(&source_path, source).unwrap();
    let runtime = PathBuf::from(env!("CARGO_BIN_EXE_sigil-fixed-evaluator"));
    let config = Config {
        version: 1,
        runtime_sha256: format!("{:x}", Sha256::digest(std::fs::read(&runtime).unwrap())),
        runtime,
        source: source_path,
        source_sha256: format!("{:x}", Sha256::digest(source.as_bytes())),
        max_fuel: 1_000_000_000,
        max_timeout_ms: 5000,
        net: vec![],
        fs: vec![],
        secret_env: BTreeMap::new(),
    };
    (directory, config)
}

#[test]
fn parent_reuses_the_verified_artifact_but_not_results() {
    let (_directory, config) = config(ECHO);
    let mut bridge = PureBridge::new(config).unwrap();
    for (count, input) in [(1, "first"), (2, "different"), (3, "first")] {
        let result = bridge.invoke(input.into(), 1_000_000, 2000).unwrap();
        assert_eq!(result["data"]["output_text"], input);
        assert_eq!(result["data"]["fixed_evaluator"]["compilations"], 1);
        assert_eq!(result["data"]["fixed_evaluator"]["evaluations"], count);
    }
}

#[test]
fn real_timeout_reaps_and_next_input_uses_a_fresh_process() {
    let (_directory, config) = config(SLOW);
    let mut bridge = PureBridge::new(config).unwrap();
    assert_eq!(
        bridge.invoke("ok".into(), 1_000_000_000, 2000).unwrap()["data"]["output_text"],
        "ok"
    );
    assert_eq!(
        bridge.invoke("hang".into(), 1_000_000_000, 50),
        Err(Fault::Deadline)
    );
    let result = bridge.invoke("new".into(), 1_000_000_000, 2000).unwrap();
    assert_eq!(result["data"]["output_text"], "new");
    assert_eq!(result["data"]["fixed_evaluator"]["evaluations"], 1);
}

#[test]
fn full_actual_lifetime_recycles_before_the_next_evaluation() {
    let (_directory, config) = config(ECHO);
    let mut bridge = PureBridge::new(config).unwrap();
    for count in 1..=4096 {
        let input = count.to_string();
        let result = bridge.invoke(input.clone(), 1_000_000, 2000).unwrap();
        assert_eq!(result["data"]["output_text"], input);
        assert_eq!(result["data"]["fixed_evaluator"]["evaluations"], count);
    }
    let result = bridge
        .invoke("after recycling".into(), 1_000_000, 2000)
        .unwrap();
    assert_eq!(result["data"]["output_text"], "after recycling");
    assert_eq!(result["data"]["fixed_evaluator"]["evaluations"], 1);
    assert_eq!(result["data"]["fixed_evaluator"]["compilations"], 1);
}

#[test]
fn changed_executable_invalidates_even_a_held_process() {
    let (directory, mut config) = config(ECHO);
    let copy = directory.path().join("evaluator-copy");
    std::fs::copy(&config.runtime, &copy).unwrap();
    config.runtime = copy.clone();
    let mut bridge = PureBridge::new(config).unwrap();
    assert_eq!(
        bridge.invoke("before".into(), 1_000_000, 2000).unwrap()["status"],
        "ok"
    );
    // Replacing the path also works on Linux, where writing an executing inode
    // can be refused with ETXTBSY. The held child still owns the original inode.
    let replacement = directory.path().join("replacement");
    std::fs::write(&replacement, b"no longer the admitted executable").unwrap();
    std::fs::rename(replacement, &copy).unwrap();
    assert_eq!(
        bridge.invoke("after".into(), 1_000_000, 2000),
        Err(Fault::Invalid)
    );
}
