use super::*;
use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::Path;
use tempfile::TempDir;

fn config(root: &Path, name: &str) -> WorkerConfig {
    let runtime = root.join(name);
    let body = format!("#!/bin/sh\n# {name}\nexit 0\n");
    fs::write(&runtime, &body).unwrap();
    fs::set_permissions(&runtime, fs::Permissions::from_mode(0o700)).unwrap();
    WorkerConfig {
        version: 1,
        runtime,
        runtime_sha256: format!("{:x}", Sha256::digest(body)),
        source: root.join("unused-source"),
        source_sha256: "0".repeat(64),
        max_fuel: 1,
        max_timeout_ms: 1,
        net: vec![],
        fs: vec![],
        secret_env: BTreeMap::new(),
    }
}

fn fixture() -> (TempDir, WorkerConfig, Registry) {
    let dir = tempfile::tempdir().unwrap();
    let entry = config(dir.path(), "entry");
    let registry = Registry::new(&entry, &BTreeMap::new(), None).unwrap();
    (dir, entry, registry)
}

fn future() -> Instant {
    Instant::now() + Duration::from_secs(10)
}

#[test]
fn inspection_rehashes_admitted_files_without_running_them_or_returning_names() {
    let (dir, entry, registry) = fixture();
    let before = fs::read(&entry.runtime).unwrap();
    let mut calls = 0;
    assert_eq!(
        registry.inspect(future(), || {
            calls += 1;
            Ok(0)
        }),
        Ok(0)
    );
    assert_eq!(calls, 1);
    assert_eq!(fs::read(&entry.runtime).unwrap(), before);
    assert_eq!(fs::read_dir(dir.path()).unwrap().count(), 1);
    let wire = registry.observe(future(), || Ok(2)).unwrap();
    assert_eq!(crate::fields(&wire, "RI1\n", 3).unwrap(), ["ok", "", "2"]);
    assert!(!wire.contains(dir.path().to_str().unwrap()));
}

#[test]
fn removed_changed_and_restored_runtime_are_observed_on_each_request() {
    let (dir, entry, registry) = fixture();
    let parked = dir.path().join("parked");
    fs::rename(&entry.runtime, &parked).unwrap();
    assert_eq!(
        registry.inspect(future(), || Ok(0)),
        Err("artifact_invalid")
    );
    fs::rename(&parked, &entry.runtime).unwrap();
    assert_eq!(registry.inspect(future(), || Ok(0)), Ok(0));
    let bytes = fs::read(&entry.runtime).unwrap();
    fs::write(&entry.runtime, b"#!/bin/sh\nexit 1\n").unwrap();
    assert_eq!(
        registry.inspect(future(), || Ok(0)),
        Err("artifact_invalid")
    );
    fs::write(&entry.runtime, bytes).unwrap();
    assert_eq!(registry.inspect(future(), || Ok(0)), Ok(0));
}

#[test]
fn symlink_directory_nonexecutable_and_writable_modes_are_refused() {
    for mode in [0o600, 0o722, 0o720, 0o702] {
        let (_dir, entry, registry) = fixture();
        fs::set_permissions(&entry.runtime, fs::Permissions::from_mode(mode)).unwrap();
        assert_eq!(fs::metadata(&entry.runtime).unwrap().mode() & 0o7777, mode);
        assert_eq!(
            registry.inspect(future(), || Ok(0)),
            Err("artifact_invalid"),
            "requested mode {mode:o}, actual {:o}",
            fs::metadata(&entry.runtime).unwrap().mode()
        );
    }
    let (dir, entry, registry) = fixture();
    let parked = dir.path().join("parked");
    fs::rename(&entry.runtime, &parked).unwrap();
    symlink(&parked, &entry.runtime).unwrap();
    assert_eq!(
        registry.inspect(future(), || Ok(0)),
        Err("artifact_invalid")
    );
    fs::remove_file(&entry.runtime).unwrap();
    fs::create_dir(&entry.runtime).unwrap();
    assert_eq!(
        registry.inspect(future(), || Ok(0)),
        Err("artifact_invalid")
    );
}

#[test]
fn mode_predicate_refuses_privileged_bits_even_when_the_os_strips_them_on_chmod() {
    // macOS stripped 04700 to 0700 on the real fixture. These are explicit
    // numeric predicate checks, not claims that privileged files were created.
    for mode in [0o4700, 0o2700, 0o6700, 0o600, 0o722] {
        assert!(!executable_file_mode(mode));
    }
    for mode in [0o700, 0o500, 0o755] {
        assert!(executable_file_mode(mode));
    }
}

#[test]
fn fifo_is_rejected_without_waiting_for_a_writer() {
    use std::ffi::CString;
    let (_dir, entry, registry) = fixture();
    fs::remove_file(&entry.runtime).unwrap();
    let path = CString::new(entry.runtime.to_str().unwrap()).unwrap();
    // SAFETY: valid NUL-terminated path in an owned test directory.
    assert_eq!(unsafe { libc::mkfifo(path.as_ptr(), 0o700) }, 0);
    assert_eq!(
        registry.inspect(future(), || Ok(0)),
        Err("artifact_invalid")
    );
}

#[test]
fn expired_deadline_and_failed_owner_precede_filesystem_inspection() {
    let (dir, entry, registry) = fixture();
    fs::rename(&entry.runtime, dir.path().join("parked")).unwrap();
    assert_eq!(
        registry.inspect(Instant::now(), || panic!(
            "expired inspection reached owner"
        )),
        Err("inspection_deadline")
    );
    assert_eq!(
        registry.inspect(future(), || Err("owner_cleanup_unconfirmed")),
        Err("owner_cleanup_unconfirmed")
    );
    assert_eq!(
        registry.inspect(future(), || Ok(17)),
        Err("inspection_limit")
    );
}

#[test]
fn file_and_aggregate_byte_budgets_are_checked_against_actual_files() {
    let (dir, entry, _) = fixture();
    let second = config(dir.path(), "second");
    let length = fs::metadata(&entry.runtime).unwrap().len();
    let registry = Registry::new(&entry, &[("second".into(), second)].into(), None).unwrap();
    assert_eq!(
        registry.inspect_bounded(future(), || Ok(0), length - 1, TOTAL_BYTES),
        Err("artifact_invalid")
    );
    // Scaled private test budget exercises real two-file accounting. Public
    // inspect/observe always use the unchanged constants tested below.
    assert_eq!(
        registry.inspect_bounded(future(), || Ok(0), FILE_BYTES, length),
        Err("inspection_limit")
    );
    assert_eq!(registry.inspect(future(), || Ok(0)), Ok(0));
    OpenOptions::new()
        .write(true)
        .open(&entry.runtime)
        .unwrap()
        .set_len(FILE_BYTES + 1)
        .unwrap();
    assert_eq!(
        registry.inspect(future(), || Ok(0)),
        Err("artifact_invalid")
    );
}

#[test]
fn only_exact_duplicate_admitted_bindings_are_coalesced() {
    let (_dir, entry, _) = fixture();
    let registry = Registry::new(&entry, &[("again".into(), entry.clone())].into(), None).unwrap();
    assert_eq!(registry.files.len(), 1);
    let mut changed = entry.clone();
    changed.runtime_sha256 = "f".repeat(64);
    assert!(matches!(
        Registry::new(&entry, &[("changed".into(), changed)].into(), None),
        Err("config")
    ));
    for hash in ["", "F", &"F".repeat(64)] {
        let mut bad = entry.clone();
        bad.runtime_sha256 = hash.into();
        assert!(matches!(
            Registry::new(&bad, &BTreeMap::new(), None),
            Err("config")
        ));
    }
    let mut relative = entry.clone();
    relative.runtime = PathBuf::from("relative");
    assert!(matches!(
        Registry::new(&relative, &BTreeMap::new(), None),
        Err("config")
    ));
}

#[test]
fn distinct_artifact_inventory_is_independently_bounded() {
    let (dir, entry, _) = fixture();
    let mut functions = BTreeMap::new();
    for n in 0..FILES - 1 {
        let mut row = entry.clone();
        row.runtime = dir.path().join(format!("runtime-{n}"));
        functions.insert(n.to_string(), row);
    }
    assert_eq!(
        Registry::new(&entry, &functions, None).unwrap().files.len(),
        FILES
    );
    let mut extra = entry.clone();
    extra.runtime = dir.path().join("too-many");
    functions.insert("extra".into(), extra);
    assert!(matches!(
        Registry::new(&entry, &functions, None),
        Err("config")
    ));
}

#[test]
fn every_registered_execution_role_contributes_its_actual_runtime_binding() {
    let (dir, entry, _) = fixture();
    let row = |name| serde_json::to_value(config(dir.path(), name)).unwrap();
    let automatic: automatic::Config = serde_json::from_value(serde_json::json!({"participants":[{
        "credential_sha256":"fixture", "worker":row("coordinator"), "binding":"fixture",
        "claim_namespace":"claim", "delivery_namespace":"delivery", "read_grants":{},
        "effects":{"effect":{"worker":row("effect"), "grants":{},
            "policy":{"worker":row("policy"),"marker":"DF1\n","values":0,"inputs":[],"read_grants":{},"alias":"effect","binding":"","claim_namespace":"claim"},
            "recorder":{"worker":row("recorder"),"delivery_namespace":"delivery"}}},
        "transactions":{"transaction":{"worker":row("transaction"),"marker":"TT1\n","values":0,"inputs":[],"grants":{}}}
    }]})).unwrap();
    // Registry traversal only; this deliberately incomplete application config
    // is not used to bypass real Engine admission in service/HTTP tests.
    let functions = [("function".into(), config(dir.path(), "function"))].into();
    let registry = Registry::new(&entry, &functions, Some(&automatic)).unwrap();
    assert_eq!(registry.files.len(), 7);
    for name in [
        "entry",
        "function",
        "coordinator",
        "effect",
        "policy",
        "recorder",
        "transaction",
    ] {
        let path = dir.path().join(name);
        let parked = dir.path().join("parked");
        fs::rename(&path, &parked).unwrap();
        assert_eq!(
            registry.inspect(future(), || Ok(0)),
            Err("artifact_invalid"),
            "{name}"
        );
        fs::rename(&parked, &path).unwrap();
    }
    assert_eq!(registry.inspect(future(), || Ok(0)), Ok(0));
}

#[test]
fn manifest_binds_mechanism_ceilings_without_a_readiness_or_dispatch_verdict() {
    let bound = manifest();
    assert_eq!(bound["runtime_files"], 64);
    assert_eq!(bound["runtime_file_bytes"], 268435456);
    assert_eq!(bound["runtime_total_bytes"], 536870912);
    assert_eq!(bound["runtime_maximum_ms"], 1000);
    assert_eq!(bound["runtime_buffer_bytes"], 16384);
    assert_eq!(bound["worker_dispatch"], false);
    assert_eq!(bound["readiness_decision"], false);
    assert_eq!(bound["retry"], false);
    assert_eq!(bound["spawnability_or_effect_success_guarantee"], false);
}
