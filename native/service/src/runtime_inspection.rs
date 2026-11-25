//! Read-only revalidation of admitted runtime artifacts and owner observations.
//! No model/tool is dispatched. No source, path, grant, secret or diagnostic is
//! returned. This does not choose application readiness or promise future work.
use crate::{Result, WorkerConfig, automatic, frame};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::OpenOptions;
use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::PathBuf;
use std::time::{Duration, Instant};

pub(crate) const FILES: usize = 64;
pub(crate) const FILE_BYTES: u64 = 256 * 1024 * 1024;
pub(crate) const TOTAL_BYTES: u64 = 512 * 1024 * 1024;
pub(crate) const TIMEOUT_MS: u64 = 1000;

pub(crate) struct Registry {
    files: BTreeMap<PathBuf, String>,
}

impl Registry {
    pub(crate) fn new(
        entry: &WorkerConfig,
        functions: &BTreeMap<String, WorkerConfig>,
        automatic: Option<&automatic::Config>,
    ) -> Result<Self> {
        let mut rows = vec![entry];
        rows.extend(functions.values());
        if let Some(config) = automatic {
            for participant in &config.participants {
                rows.push(&participant.worker);
                for effect in participant.effects.values() {
                    rows.extend([
                        &effect.worker,
                        &effect.policy.worker,
                        &effect.recorder.worker,
                    ]);
                }
                rows.extend(participant.transactions.values().map(|row| &row.worker));
                if let Some(audit) = &participant.transaction_audit {
                    rows.push(&audit.worker);
                    rows.extend(audit.evaluation.as_ref());
                }
                if let Some(audit) = &participant.effect_audit {
                    rows.push(&audit.worker);
                    rows.extend(audit.evaluation.as_ref());
                }
            }
        }
        let mut files = BTreeMap::new();
        for config in rows {
            if !config.runtime.is_absolute()
                || config.runtime_sha256.len() != 64
                || !config
                    .runtime_sha256
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                || files
                    .get(&config.runtime)
                    .is_some_and(|hash| hash != &config.runtime_sha256)
            {
                return Err("config");
            }
            files.insert(config.runtime.clone(), config.runtime_sha256.clone());
            if files.len() > FILES {
                return Err("config");
            }
        }
        Ok(Self { files })
    }

    fn inspect(&self, deadline: Instant, owners: impl FnOnce() -> Result<usize>) -> Result<usize> {
        self.inspect_bounded(deadline, owners, FILE_BYTES, TOTAL_BYTES)
    }

    fn inspect_bounded(
        &self,
        deadline: Instant,
        owners: impl FnOnce() -> Result<usize>,
        file_bytes: u64,
        total_bytes: u64,
    ) -> Result<usize> {
        let deadline = deadline.min(Instant::now() + Duration::from_millis(TIMEOUT_MS));
        check_time(deadline)?;
        let in_flight = owners()?;
        if in_flight > 16 {
            return Err("inspection_limit");
        }
        let mut total = 0;
        for (path, expected) in &self.files {
            check_time(deadline)?;
            let mut file = OpenOptions::new()
                .read(true)
                .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK)
                .open(path)
                .map_err(|_| "artifact_invalid")?;
            let before = file.metadata().map_err(|_| "artifact_invalid")?;
            if !before.is_file()
                || !executable_file_mode(before.mode())
                || before.len() > file_bytes
            {
                return Err("artifact_invalid");
            }
            if before.len() > total_bytes - total {
                return Err("inspection_limit");
            }
            let mut digest = Sha256::new();
            let mut read = 0;
            let mut buffer = [0u8; 16384];
            loop {
                check_time(deadline)?;
                let n = file.read(&mut buffer).map_err(|_| "artifact_invalid")?;
                if n == 0 {
                    break;
                }
                read += n as u64;
                total += n as u64;
                if read > file_bytes || total > total_bytes || read > before.len() {
                    return Err("inspection_limit");
                }
                digest.update(&buffer[..n]);
            }
            let after = file.metadata().map_err(|_| "artifact_invalid")?;
            if read != before.len()
                || after.len() != before.len()
                || after.mode() != before.mode()
                || after.mtime() != before.mtime()
                || after.mtime_nsec() != before.mtime_nsec()
                || format!("{:x}", digest.finalize()) != *expected
            {
                return Err("artifact_invalid");
            }
            // Observe pathname replacement as well as mutation of the open file.
            let named = std::fs::symlink_metadata(path).map_err(|_| "artifact_invalid")?;
            if !named.is_file() || named.dev() != after.dev() || named.ino() != after.ino() {
                return Err("artifact_invalid");
            }
            check_time(deadline)?;
        }
        check_time(deadline)?;
        Ok(in_flight)
    }

    pub(crate) fn observe(
        &self,
        deadline: Instant,
        owners: impl FnOnce() -> Result<usize>,
    ) -> Result<String> {
        match self.inspect(deadline, owners) {
            Ok(in_flight) => frame("RI1\n", &["ok", "", &in_flight.to_string()]),
            Err(error) => frame("RI1\n", &["error", error, ""]),
        }
    }
}

fn check_time(deadline: Instant) -> Result<()> {
    if Instant::now() >= deadline {
        Err("inspection_deadline")
    } else {
        Ok(())
    }
}

fn executable_file_mode(mode: u32) -> bool {
    mode & 0o6022 == 0 && mode & 0o111 != 0
}

pub(crate) fn owner_fault(fault: sigil_worker_bridge::Fault) -> &'static str {
    match fault {
        sigil_worker_bridge::Fault::WrongProcess => "owner_wrong_process",
        sigil_worker_bridge::Fault::CleanupUnconfirmed => "owner_cleanup_unconfirmed",
        _ => "owner_unavailable",
    }
}

pub(crate) fn manifest() -> serde_json::Value {
    serde_json::json!({
        "contract":"sigil-execution-inspection/v1", "command":"inspect_execution",
        "observation":"EI1", "fields":["SI1-storage-inspection","RI1-runtime-inspection"],
        "runtime_fields":["status","error","in-flight-or-uncollected-effects"],
        "runtime_files":FILES,"runtime_file_bytes":FILE_BYTES,"runtime_total_bytes":TOTAL_BYTES,
        "runtime_maximum_ms":TIMEOUT_MS,"runtime_buffer_bytes":16384,
        "runtime_scope":"all-admitted-entry-functions-coordinators-effects-policies-recorders-transactions",
        "deduplication":"exact-admitted-path-and-hash-within-one-inspection-no-cross-request-cache",
        "runtime_checks":["owner-prechecks","file-type-mode-size-identity","current-pinned-runtime-digests"],
        "deadline":"cooperative-no-late-success-not-kernel-interruption",
        "storage_precheck_failure":"runtime-not-observed",
        "readiness_decision":false,"worker_dispatch":false,"retry":false,
        "spawnability_or_effect_success_guarantee":false
    })
}

#[cfg(test)]
mod tests;
