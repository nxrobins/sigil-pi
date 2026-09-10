//! Fixed-source, fixed-grant one-use worker tickets. No application policy.
mod pure;
pub mod strict;
pub use pure::PureBridge;
mod transport;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};
use transport::Worker;

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Fault {
    Invalid,
    Limit,
    Busy,
    Ticket,
    Deadline,
    Cancelled,
    Spawn,
    Transport,
    Protocol,
    RemoteProtocol,
    StderrLimit,
    CleanupUnconfirmed,
    WrongProcess,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub version: u32,
    pub runtime: PathBuf,
    pub runtime_sha256: String,
    pub source: PathBuf,
    pub source_sha256: String,
    pub max_fuel: u64,
    pub max_timeout_ms: u64,
    pub net: Vec<String>,
    pub fs: Vec<String>,
    pub secret_env: BTreeMap<String, String>,
}

fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn pinned_file(path: &Path, expected: &str, max: usize) -> Result<Vec<u8>, Fault> {
    if !path.is_absolute()
        || expected.len() != 64
        || !expected
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(Fault::Invalid);
    }
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path)
        .map_err(|_| Fault::Invalid)?;
    let meta = file.metadata().map_err(|_| Fault::Invalid)?;
    if !meta.is_file() || meta.mode() & 0o6022 != 0 || meta.len() > max as u64 {
        return Err(Fault::Invalid);
    }
    let mut bytes = Vec::new();
    file.take(max as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| Fault::Invalid)?;
    if bytes.len() > max || digest(&bytes) != expected {
        return Err(Fault::Invalid);
    }
    Ok(bytes)
}

struct Pending {
    ticket: String,
    input: String,
    fuel: u64,
    deadline: Instant,
}
#[derive(Serialize)]
pub struct Prepared {
    pub ticket: String,
    pub generation: String,
    pub input_sha256: String,
}
#[derive(Serialize)]
pub struct Observation {
    pub generation: String,
    pub request_may_have_run: bool,
    pub worker_reaped: bool,
    pub fault: Option<Fault>,
    pub result: Option<Value>,
}
/// Immutable non-secret facts about the admitted worker. Never contains secret
/// values, environment-variable names, source text, or mutable caller grants.
#[derive(Serialize)]
pub struct Facts<'a> {
    pub source_sha256: &'a str,
    pub runtime_sha256: &'a str,
    pub net: &'a [String],
    pub fs: &'a [String],
    pub secret_names: Vec<&'a str>,
    pub max_fuel: u64,
    pub max_timeout_ms: u64,
}
pub struct Bridge {
    config: Config,
    source: String,
    grants: Value,
    pending: Option<Pending>,
    poisoned: bool,
    owner: u32,
}
impl Bridge {
    pub fn facts(&self) -> Result<Facts<'_>, Fault> {
        self.live()?;
        Ok(Facts {
            source_sha256: &self.config.source_sha256,
            runtime_sha256: &self.config.runtime_sha256,
            net: &self.config.net,
            fs: &self.config.fs,
            secret_names: self.config.secret_env.keys().map(String::as_str).collect(),
            max_fuel: self.config.max_fuel,
            max_timeout_ms: self.config.max_timeout_ms,
        })
    }
    pub fn new(config: Config) -> Result<Self, Fault> {
        if config.version != 1
            || !(1..=1_000_000_000).contains(&config.max_fuel)
            || !(1..=300_000).contains(&config.max_timeout_ms)
            || config.net.len() > 64
            || config.fs.len() > 64
            || config.secret_env.len() > 32
        {
            return Err(Fault::Invalid);
        }
        pinned_file(&config.runtime, &config.runtime_sha256, 256 * 1024 * 1024)?;
        let source = String::from_utf8(pinned_file(&config.source, &config.source_sha256, 65536)?)
            .map_err(|_| Fault::Invalid)?;
        if source.is_empty() {
            return Err(Fault::Invalid);
        }
        let mut secrets = Vec::new();
        for (name, var) in &config.secret_env {
            if name.is_empty()
                || name.len() > 128
                || !name
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
            {
                return Err(Fault::Invalid);
            }
            let value = std::env::var(var).map_err(|_| Fault::Invalid)?;
            if value.is_empty() || value.len() > 16384 || value.contains(['\r', '\n', '\0']) {
                return Err(Fault::Invalid);
            }
            secrets.push(format!("{name}={value}"));
        }
        let grants = json!({"net":config.net,"fs":config.fs,"secret":secrets});
        Ok(Self {
            config,
            source,
            grants,
            pending: None,
            poisoned: false,
            owner: std::process::id(),
        })
    }
    fn live(&self) -> Result<(), Fault> {
        if self.owner != std::process::id() {
            return Err(Fault::WrongProcess);
        }
        if self.poisoned {
            return Err(Fault::CleanupUnconfirmed);
        }
        Ok(())
    }
    pub fn prepare(
        &mut self,
        input: String,
        fuel: u64,
        timeout_ms: u64,
    ) -> Result<Prepared, Fault> {
        self.live()?;
        if input.len() > 4 * 1024 * 1024
            || fuel == 0
            || fuel > self.config.max_fuel
            || timeout_ms == 0
            || timeout_ms > self.config.max_timeout_ms
        {
            return Err(Fault::Limit);
        }
        if self
            .pending
            .as_ref()
            .is_some_and(|p| p.deadline > Instant::now())
        {
            return Err(Fault::Busy);
        }
        let mut entropy = [0u8; 32];
        File::open("/dev/urandom")
            .and_then(|mut f| f.read_exact(&mut entropy))
            .map_err(|_| Fault::Invalid)?;
        let ticket = entropy
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>();
        let ready = Prepared {
            ticket: ticket.clone(),
            generation: ticket.clone(),
            input_sha256: digest(input.as_bytes()),
        };
        self.pending = Some(Pending {
            ticket,
            input,
            fuel,
            deadline: Instant::now() + Duration::from_millis(timeout_ms),
        });
        Ok(ready)
    }
    pub fn cancel_pending(&mut self, ticket: &str) -> Result<(), Fault> {
        self.live()?;
        if self.pending.as_ref().is_none_or(|p| p.ticket != ticket) {
            return Err(Fault::Ticket);
        }
        self.pending.take();
        Ok(())
    }
    pub fn execute(&mut self, ticket: &str, cancel: &AtomicBool) -> Result<Observation, Fault> {
        self.live()?;
        if self.pending.as_ref().is_none_or(|p| p.ticket != ticket) {
            return Err(Fault::Ticket);
        }
        // Consume BEFORE every possible spawn/send/failure; it can never replay.
        let pending = self.pending.take().unwrap();
        let mut seen = Observation {
            generation: pending.ticket,
            request_may_have_run: false,
            worker_reaped: true,
            fault: None,
            result: None,
        };
        if cancel.load(Ordering::Acquire) {
            seen.fault = Some(Fault::Cancelled);
            return Ok(seen);
        }
        if Instant::now() >= pending.deadline {
            seen.fault = Some(Fault::Deadline);
            return Ok(seen);
        }
        // Detect runtime replacement before launch. Trusted local package paths
        // remain a bootstrap assumption; this is not descriptor-based admission.
        if pinned_file(
            &self.config.runtime,
            &self.config.runtime_sha256,
            256 * 1024 * 1024,
        )
        .is_err()
        {
            seen.fault = Some(Fault::Invalid);
            return Ok(seen);
        }
        if Instant::now() >= pending.deadline {
            seen.fault = Some(Fault::Deadline);
            return Ok(seen);
        }
        let mut command = Command::new(&self.config.runtime);
        command.env_clear().env("LANG", "C.UTF-8").current_dir("/");
        let mut worker = match Worker::spawn(&mut command) {
            Ok(w) => w,
            Err(e) => {
                seen.fault = Some(e);
                if e == Fault::CleanupUnconfirmed {
                    self.poisoned = true;
                    seen.worker_reaped = false;
                }
                return Ok(seen);
            }
        };
        let mut handshake_sent = false;
        let run = (|| {
            worker.exchange(
                &json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}),
                1,
                pending
                    .deadline
                    .min(Instant::now() + Duration::from_secs(5)),
                cancel,
                &mut handshake_sent,
            )?;
            let result=worker.exchange(&json!({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{
                "name":"sigil_forge","arguments":{"source":self.source,"input":pending.input,"fuel":pending.fuel,
                    "host_profile":"ephemeral","grants":self.grants}}}),2,pending.deadline,cancel,&mut seen.request_may_have_run)?;
            let content = result["content"].as_array().ok_or(Fault::Protocol)?;
            if content.len() != 1 || content[0]["type"] != "text" || result["isError"] == true {
                return Err(Fault::Protocol);
            }
            let inner = strict::parse(
                content[0]["text"]
                    .as_str()
                    .ok_or(Fault::Protocol)?
                    .as_bytes(),
            )
            .map_err(|_| Fault::Protocol)?;
            if !matches!(inner["status"].as_str(), Some("ok" | "error")) {
                return Err(Fault::Protocol);
            }
            if cancel.load(Ordering::Acquire) {
                return Err(Fault::Cancelled);
            }
            if Instant::now() >= pending.deadline {
                return Err(Fault::Deadline);
            }
            Ok(inner)
        })();
        seen.worker_reaped = worker.stop();
        if !seen.worker_reaped {
            self.poisoned = true;
            seen.fault = Some(Fault::CleanupUnconfirmed);
        } else {
            match run {
                Ok(v) => seen.result = Some(v),
                Err(e) => seen.fault = Some(e),
            }
        }
        Ok(seen)
    }
}

#[cfg(test)]
mod tests;
