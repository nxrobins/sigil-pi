//! Generic request/fact binding and scoped read/commit continuation execution.
//! SIGIL owns all route, credential-validity and application-state decisions.
use serde::Deserialize;
use sha2::{Digest, Sha256};
use sigil_durable_store::store::{Access, Limits, OpenMode, Scope, Store};
use sigil_worker_bridge::{Bridge, Config as WorkerConfig, PureBridge};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{DirBuilder, File};
use std::io::Read;
use std::os::unix::fs::DirBuilderExt;
use std::path::PathBuf;
use std::sync::atomic::AtomicBool;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use subtle::{ConditionallySelectable, ConstantTimeEq};

mod action;
pub mod automatic;
pub mod claimed;
pub mod policy;
pub mod transaction;
use action::{Step, perform};

pub type Result<T> = std::result::Result<T, &'static str>;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CredentialConfig {
    pub sha256: String,
    pub facts: String,
    pub grants: BTreeMap<String, Access>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub version: u32,
    pub worker: WorkerConfig,
    pub functions: BTreeMap<String, WorkerConfig>,
    pub state_root: PathBuf,
    pub limits: Limits,
    pub credentials: Vec<CredentialConfig>,
    #[serde(default)]
    pub automatic: Option<automatic::Config>,
}
struct Credential {
    digest: [u8; 32],
    facts: String,
    scope: Scope,
}
pub struct Request {
    pub method: String,
    pub path: String,
    pub authorization: Option<String>,
    pub body: String,
}
#[derive(Debug)]
pub struct Reply {
    pub status: u16,
    pub body: String,
}
pub struct Engine {
    machine: Machine,
    store: Store,
    credentials: Vec<Credential>,
    last_clock: u64,
    automatic: Option<automatic::Automatic>,
}
struct Function {
    worker: PureExecution,
    fuel: u64,
    timeout_ms: u64,
}
enum PureExecution {
    Fresh(Bridge),
    Cached(PureBridge),
}
struct Machine {
    entry: Function,
    functions: BTreeMap<String, Function>,
    bundle: String,
    function_names: String,
}
struct Context {
    request: Request,
    facts: String,
    id: String,
    mode: &'static str,
}
struct Finished {
    reply: Reply,
    guard: Option<action::Guard>,
    deadline: Instant,
}
pub fn frame(marker: &str, fields: &[&str]) -> Result<String> {
    if marker.len() != 4 {
        return Err("protocol");
    }
    let mut out = marker.to_owned();
    for v in fields {
        if v.len() > 4 * 1024 * 1024 {
            return Err("limit");
        }
        out.push_str(&format!("{:08}", v.len()));
        out.push_str(v);
    }
    if out.len() > 4 * 1024 * 1024 {
        return Err("limit");
    }
    Ok(out)
}
fn fields<'a>(value: &'a str, marker: &str, count: usize) -> Result<Vec<&'a str>> {
    if !value.starts_with(marker) || value.len() > 2 * 1024 * 1024 {
        return Err("protocol");
    }
    let mut at = 4;
    let mut out = Vec::new();
    for _ in 0..count {
        let raw = value.get(at..at + 8).ok_or("protocol")?;
        if !raw.bytes().all(|b| b.is_ascii_digit()) {
            return Err("protocol");
        }
        let n: usize = raw.parse().map_err(|_| "protocol")?;
        at += 8;
        out.push(value.get(at..at + n).ok_or("protocol")?);
        at += n;
    }
    if at != value.len() {
        return Err("protocol");
    }
    Ok(out)
}
fn digest_bytes(value: &str) -> Result<[u8; 32]> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err("config");
    }
    let mut out = [0; 32];
    for (i, byte) in out.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&value[i * 2..i * 2 + 2], 16).map_err(|_| "config")?;
    }
    Ok(out)
}
fn identity() -> Result<String> {
    let mut bytes = [0u8; 32];
    File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .map_err(|_| "entropy")?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}
fn now() -> Result<u64> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|v| v.as_secs())
        .map_err(|_| "clock")
}
fn invoke(worker: &mut Bridge, input: String, fuel: u64, timeout: u64) -> Result<String> {
    let prepared = worker.prepare(input, fuel, timeout).map_err(|_| "worker")?;
    let seen = worker
        .execute(&prepared.ticket, &AtomicBool::new(false))
        .map_err(|_| "worker")?;
    if seen.fault.is_some() || !seen.worker_reaped {
        return Err("worker");
    }
    let result = seen.result.ok_or("worker")?;
    if result["status"] != "ok" {
        return Err("application");
    }
    result["data"]["output_text"]
        .as_str()
        .map(str::to_owned)
        .ok_or("protocol")
}

impl Function {
    fn manifest(config: &WorkerConfig) -> Result<serde_json::Value> {
        if !config.net.is_empty() || !config.fs.is_empty() || !config.secret_env.is_empty() {
            return Err("config");
        }
        Ok(
            serde_json::json!({"source":config.source_sha256,"runtime":config.runtime_sha256,
            "fuel":config.max_fuel,"timeout_ms":config.max_timeout_ms}),
        )
    }
    fn new(config: WorkerConfig) -> Result<Self> {
        Self::manifest(&config)?;
        Ok(Self {
            fuel: config.max_fuel,
            timeout_ms: config.max_timeout_ms.min(30_000),
            worker: PureExecution::Fresh(Bridge::new(config).map_err(|_| "config")?),
        })
    }
    fn cached(config: WorkerConfig) -> Result<Self> {
        Self::manifest(&config)?;
        Ok(Self {
            fuel: config.max_fuel,
            timeout_ms: config.max_timeout_ms.min(30000),
            worker: PureExecution::Cached(PureBridge::new(config).map_err(|_| "config")?),
        })
    }
    fn invoke(&mut self, input: String, deadline: Instant) -> Result<String> {
        let remaining = deadline
            .saturating_duration_since(Instant::now())
            .as_millis() as u64;
        if remaining == 0 {
            return Err("deadline");
        }
        let timeout = remaining.min(self.timeout_ms);
        match &mut self.worker {
            PureExecution::Fresh(worker) => invoke(worker, input, self.fuel, timeout),
            PureExecution::Cached(worker) => {
                let result = worker
                    .invoke(input, self.fuel, timeout)
                    .map_err(|_| "worker")?;
                if result["status"] != "ok" {
                    return Err("application");
                }
                result["data"]["output_text"]
                    .as_str()
                    .map(str::to_owned)
                    .ok_or("protocol")
            }
        }
    }
}
impl Machine {
    fn new(
        entry: WorkerConfig,
        configs: BTreeMap<String, WorkerConfig>,
        automatic: Option<&automatic::Config>,
    ) -> Result<Self> {
        if configs.len() > 8 {
            return Err("config");
        }
        let mut manifest = BTreeMap::new();
        for (name, config) in &configs {
            if name.is_empty()
                || name.len() > 64
                || !name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
            {
                return Err("config");
            }
            manifest.insert(name.clone(), Function::manifest(config)?);
        }
        let mut identity = serde_json::json!({"contract":"sigil-application-host/v3", "steps":8,
            "entry":Function::manifest(&entry)?, "functions":manifest});
        if let Some(config) = automatic {
            identity["contract"] = serde_json::json!("sigil-application-host/v4");
            identity["automatic"] = serde_json::to_value(config).map_err(|_| "config")?;
            identity["pure_execution"] = serde_json::json!({"contract":"fixed-grantless-process/v1", "max_calls":4096, "fresh_guest":true});
        }
        let bundle = Sha256::digest(serde_json::to_vec(&identity).map_err(|_| "config")?)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        let function_names =
            serde_json::to_string(&configs.keys().collect::<Vec<_>>()).map_err(|_| "config")?;
        let mut functions = BTreeMap::new();
        let construct = if automatic.is_some() {
            Function::cached
        } else {
            Function::new
        };
        for (name, config) in configs {
            functions.insert(name, construct(config)?);
        }
        Ok(Self {
            entry: construct(entry)?,
            functions,
            bundle,
            function_names,
        })
    }
    fn run(
        &mut self,
        context: Context,
        mut store: Option<&mut Store>,
        scope: Option<&Scope>,
        last_clock: &mut u64,
    ) -> Result<Finished> {
        let mut stage = if context.mode == "boot" {
            "boot"
        } else {
            "init"
        };
        let mut observation = String::new();
        let mut continuation = String::new();
        let deadline = Instant::now() + Duration::from_millis(self.entry.timeout_ms);
        for _ in 0..8 {
            let clock = now()?;
            if clock < *last_clock {
                return Err("clock");
            }
            *last_clock = clock;
            let input = frame(
                "AH3\n",
                &[
                    &context.request.method,
                    &context.request.path,
                    &context.request.body,
                    &context.facts,
                    &clock.to_string(),
                    &context.id,
                    stage,
                    &observation,
                    &continuation,
                    &self.bundle,
                    &self.function_names,
                    context.mode,
                ],
            )?;
            let raw = self.entry.invoke(input, deadline)?;
            match perform(store.as_deref_mut(), scope, last_clock, deadline, &raw, now)? {
                Step::Reply { reply, guard } => {
                    return Ok(Finished {
                        reply,
                        guard,
                        deadline,
                    });
                }
                Step::Continue {
                    stage: next,
                    observation: seen,
                    continuation: held,
                } => {
                    stage = next;
                    observation = seen;
                    continuation = held;
                }
                Step::Call {
                    target,
                    input,
                    continuation: held,
                } => {
                    // A function gets ONLY the selected bytes, with no storage,
                    // network or secret grants. Its result is data, never a host
                    // command. Only the entry application chooses the next action.
                    let function = self.functions.get_mut(&target).ok_or("capability")?;
                    observation = function.invoke(input, deadline)?;
                    stage = "call";
                    continuation = held;
                }
            }
        }
        Err("limit")
    }
}
impl Engine {
    pub fn open(config: Config, mode: OpenMode) -> Result<Self> {
        if !matches!(config.version, 3 | 4)
            || (config.version == 4) != config.automatic.is_some()
            || config.credentials.is_empty()
            || config.credentials.len() > 64
            || !config.worker.net.is_empty()
            || !config.worker.fs.is_empty()
            || !config.worker.secret_env.is_empty()
        {
            return Err("config");
        }
        let mut machine = Machine::new(config.worker, config.functions, config.automatic.as_ref())?;
        let mut registry = Vec::new();
        let mut seen = BTreeSet::new();
        for row in &config.credentials {
            digest_bytes(&row.sha256)?;
            if !seen.insert(row.sha256.clone()) {
                return Err("config");
            }
            Store::validate_grants(&row.grants).map_err(|_| "config")?;
            let mut grants = Vec::new();
            for (ns, access) in &row.grants {
                let access = serde_json::to_value(access).map_err(|_| "config")?;
                grants.push(frame("CG1\n", &[ns, access.as_str().ok_or("config")?])?);
            }
            registry.push(frame(
                "CB1\n",
                &[
                    &row.facts,
                    &serde_json::to_string(&grants).map_err(|_| "config")?,
                ],
            )?);
        }
        let mut clock = now()?;
        let boot = machine.run(
            Context {
                request: Request {
                    method: String::new(),
                    path: String::new(),
                    authorization: None,
                    body: serde_json::to_string(&registry).map_err(|_| "config")?,
                },
                facts: String::new(),
                id: String::new(),
                mode: "boot",
            },
            None,
            None,
            &mut clock,
        )?;
        if boot.reply.status != 204 || !boot.reply.body.is_empty() {
            return Err("config");
        }
        let guard = boot.guard.ok_or("protocol")?;
        let admitted = config
            .automatic
            .map(|auto| automatic::Admitted::new(auto, &config.credentials, &machine.bundle))
            .transpose()?;
        // No state is opened or initialized until the SIGIL bootstrap decision.
        if !config.state_root.is_absolute() {
            return Err("config");
        }
        action::admit_time(Some(&guard), &mut clock, boot.deadline, now)?;
        if matches!(mode, OpenMode::CreateNew) {
            // Create exactly the configured new directory, never repair or
            // overwrite existing state. Store independently checks its metadata.
            DirBuilder::new()
                .mode(0o700)
                .create(&config.state_root)
                .map_err(|_| "storage")?;
            File::open(config.state_root.parent().ok_or("config")?)
                .and_then(|f| f.sync_all())
                .map_err(|_| "storage")?;
        }
        let store = Store::open(&config.state_root, mode, config.limits).map_err(|_| "storage")?;
        let automatic = admitted
            .map(|a| a.open(&store, machine.bundle.clone()))
            .transpose()?;
        let mut credentials = Vec::new();
        for row in config.credentials {
            credentials.push(Credential {
                digest: digest_bytes(&row.sha256)?,
                facts: row.facts,
                scope: store.scope(row.grants).map_err(|_| "config")?,
            });
        }
        Ok(Self {
            machine,
            store,
            credentials,
            last_clock: clock,
            automatic,
        })
    }
    fn credential(&self, authorization: Option<&str>) -> Option<usize> {
        let token = authorization
            .and_then(|h| h.strip_prefix("Bearer "))
            .unwrap_or("");
        if token.is_empty() || token.len() > 4096 {
            return None;
        }
        let candidate: [u8; 32] = Sha256::digest(token.as_bytes()).into();
        let mut selected = u64::MAX;
        for (i, row) in self.credentials.iter().enumerate() {
            selected =
                u64::conditional_select(&selected, &(i as u64), candidate.ct_eq(&row.digest));
        }
        (selected != u64::MAX).then_some(selected as usize)
    }
    pub fn request(&mut self, mut request: Request) -> Result<Reply> {
        if request.body.len() > 1024 * 1024
            || request.path.len() > 4096
            || request.method.len() > 32
        {
            return Err("limit");
        }
        let matched = self.credential(request.authorization.as_deref());
        // Raw bearer material never enters SIGIL, storage, logs or a continuation.
        request.authorization = None;
        let facts = matched
            .map(|i| self.credentials[i].facts.clone())
            .unwrap_or_default();
        let context = Context {
            request,
            facts,
            id: identity()?,
            mode: "request",
        };
        self.machine
            .run(
                context,
                Some(&mut self.store),
                matched.map(|i| &self.credentials[i].scope),
                &mut self.last_clock,
            )
            .map(|done| done.reply)
    }

    pub fn tick(&mut self) -> Result<()> {
        if let Some(automatic) = &mut self.automatic {
            automatic.tick(&mut self.store, &mut self.last_clock)?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frame_round_trip_uses_bytes_and_preserves_values() {
        let values = ["é😀", "a\0b\n", ""];
        let encoded = frame("TS1\n", &values).unwrap();
        assert!(encoded.starts_with("TS1\n00000006"));
        assert_eq!(fields(&encoded, "TS1\n", 3).unwrap(), values);
    }

    #[test]
    fn malformed_or_trailing_frames_are_rejected() {
        for bad in [
            "TS1\n0000000x",
            "TS1\n00000004abc",
            "TS1\n00000001é",
            "TS1\n00000000x",
            "TS2\n00000000",
        ] {
            assert!(fields(bad, "TS1\n", 1).is_err());
        }
    }

    #[test]
    fn credential_digest_grammar_is_exact() {
        assert_eq!(digest_bytes(&"ab".repeat(32)).unwrap(), [0xab; 32]);
        for bad in [
            "AB".repeat(32),
            "x".repeat(64),
            "a".repeat(63),
            "a".repeat(65),
        ] {
            assert!(digest_bytes(&bad).is_err());
        }
    }
}
