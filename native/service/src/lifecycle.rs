//! Explicit process-wide operator-signal observation, not application policy.
//!
//! SIGUSR1 only latches a fact. It does not stop admission, cancel, wait, exit,
//! mark an effect delivered, or promise quiescence. The opting-in embedding owns
//! this signal disposition for the rest of its process lifetime. No reset API.
use crate::{Result, frame};
use serde::{Deserialize, Serialize};
use std::sync::OnceLock;
use std::sync::atomic::{AtomicBool, Ordering};

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub source: Source,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub enum Source {
    #[serde(rename = "posix_sigusr1")]
    PosixSigusr1,
}

static REQUESTED: AtomicBool = AtomicBool::new(false);
static INSTALLED: OnceLock<Result<u32>> = OnceLock::new();

// This handler performs only a lock-free AtomicBool store: no allocation,
// locking, formatting, logging, I/O, guest execution, or resource cleanup.
extern "C" fn requested(_: libc::c_int) {
    REQUESTED.store(true, Ordering::Release);
}

fn disposition() -> Result<libc::sigaction> {
    // SAFETY: sigaction initializes the provided valid, writable structure;
    // a null new-action pointer is the read-only query form.
    let mut current: libc::sigaction = unsafe { std::mem::zeroed() };
    if unsafe { libc::sigaction(libc::SIGUSR1, std::ptr::null(), &mut current) } != 0 {
        return Err("signal_unavailable");
    }
    Ok(current)
}

fn install() -> Result<u32> {
    // An embedding with a pre-existing handler/ignore disposition must keep it.
    // Concurrent replacement by another signal owner is unsupported and is also
    // checked at observation. Opt-in is required before this code can run.
    if disposition()?.sa_sigaction != libc::SIG_DFL {
        return Err("signal_in_use");
    }
    // SAFETY: all fields are initialized before the OS reads this structure;
    // the function pointer is a static extern-C handler for the process lifetime.
    let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
    action.sa_sigaction = requested as *const () as usize;
    action.sa_flags = libc::SA_RESTART;
    if unsafe { libc::sigemptyset(&mut action.sa_mask) } != 0 {
        return Err("signal_unavailable");
    }
    let mut previous: libc::sigaction = unsafe { std::mem::zeroed() };
    if unsafe { libc::sigaction(libc::SIGUSR1, &action, &mut previous) } != 0 {
        return Err("signal_unavailable");
    }
    if previous.sa_sigaction != libc::SIG_DFL {
        // Do not keep a disposition accidentally taken from a concurrent owner.
        if unsafe { libc::sigaction(libc::SIGUSR1, &previous, std::ptr::null_mut()) } != 0 {
            return Err("signal_restore_unconfirmed");
        }
        return Err("signal_in_use");
    }
    Ok(std::process::id())
}

pub(crate) struct Latch {
    pid: u32,
}

impl Latch {
    pub(crate) fn open(config: &Config) -> Result<Self> {
        match config.source {
            Source::PosixSigusr1 => {}
        }
        let pid = (*INSTALLED.get_or_init(install))?;
        if pid != std::process::id() {
            return Err("signal_wrong_process");
        }
        let latch = Self { pid };
        latch.sample()?;
        Ok(latch)
    }

    fn sample(&self) -> Result<bool> {
        if self.pid != std::process::id() {
            return Err("signal_wrong_process");
        }
        let installed = disposition()?;
        if installed.sa_sigaction != requested as *const () as usize
            || installed.sa_flags & (libc::SA_SIGINFO | libc::SA_RESETHAND) != 0
        {
            return Err("signal_owner_changed");
        }
        Ok(REQUESTED.load(Ordering::Acquire))
    }

    pub(crate) fn observe(&self) -> Result<String> {
        match self.sample() {
            Ok(value) => frame("LF1\n", &["ok", "", if value { "1" } else { "0" }]),
            Err(error) => frame("LF1\n", &["error", error, ""]),
        }
    }
}

pub(crate) fn manifest() -> serde_json::Value {
    serde_json::json!({
        "contract":"sigil-lifecycle-observation/v1", "source":"posix_sigusr1",
        "observation":"LF1", "fields":["status","error","operator-request-observed"],
        "lifetime":"one-process-signal-disposition", "reset":false,
        "sampling":"each-entry-evaluation-no-atomicity-with-a-later-effect",
        "scope":"process-wide-not-tenant-owned",
        "readiness_or_admission_decision":false, "automatic_exit":false,
        "cancellation":false, "quiescence_or_delivery_guarantee":false,
        "external_signal_owner":"embedding-exclusively-owns-SIGUSR1-until-process-exit"
    })
}

#[cfg(test)]
mod tests;
