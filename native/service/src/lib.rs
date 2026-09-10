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
use std::time::{Duration, Instant};
use subtle::{ConditionallySelectable, ConstantTimeEq};

mod action;
pub mod audited_transaction;
pub mod automatic;
pub mod claimed;
mod effect_audit;
mod evaluation;
mod evaluation_audit;
pub mod http_exchange;
pub mod lifecycle;
pub mod monotonic_facts;
pub mod policy;
pub mod process_facts;
pub mod readset;
mod request_facts;
mod runtime_inspection;
mod storage_inspection;
pub mod storage_observation;
pub mod transaction;
use action::{EntryContract, Step, perform_with_http_contract};
use request_facts::{ClockSample, Presentation};

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
pub struct HttpConfig {
    pub response_headers: Vec<String>,
}
fn explicit_http_config<'de, D>(
    deserializer: D,
) -> std::result::Result<Option<HttpConfig>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    // Missing is allowed for old profiles; explicit null is not a silent opt-in.
    HttpConfig::deserialize(deserializer).map(Some)
}
fn explicit_process_config<'de, D>(
    deserializer: D,
) -> std::result::Result<Option<process_facts::Config>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    process_facts::Config::deserialize(deserializer).map(Some)
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
    #[serde(default, deserialize_with = "explicit_http_config")]
    pub http: Option<HttpConfig>,
    #[serde(default, deserialize_with = "explicit_process_config")]
    pub process_facts: Option<process_facts::Config>,
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
    pub request_id_hints: Vec<Vec<u8>>,
}
#[derive(Debug)]
pub struct Reply {
    pub status: u16,
    pub body: String,
    pub content_type: String,
    pub headers: Vec<(String, String)>,
}
pub struct Engine {
    machine: Machine,
    store: Store,
    credentials: Vec<Credential>,
    last_clock: u64,
    automatic: Option<automatic::Automatic>,
    process_facts: Option<process_facts::ProcessFacts>,
}
struct Function {
    worker: PureExecution,
    fuel: u64,
    timeout_ms: u64,
    source_sha256: String,
    runtime_sha256: String,
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
    entry_contract: EntryContract,
    http: Option<http_exchange::HeaderPolicy>,
    header_inventory: String,
    runtime_registry: Option<runtime_inspection::Registry>,
}
struct Context {
    request: Request,
    facts: String,
    id: String,
    mode: &'static str,
    http_request_facts: String,
    presentation: Presentation,
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
    Ok(ClockSample::observe()?.seconds)
}
fn entry_profile(version: u32, automatic: bool, http: bool) -> Result<EntryContract> {
    match (version, automatic, http) {
        (3, false, false) | (4, true, false) => Ok(EntryContract::Legacy),
        (5, false, false) | (6, true, false) => Ok(EntryContract::Metadata),
        (7, true, true) => Ok(EntryContract::HttpExchange),
        (8, true, true) => Ok(EntryContract::RequestAdmission),
        (9, true, true) => Ok(EntryContract::ProcessFacts),
        _ => Err("config"),
    }
}
fn http_manifest(policy: &http_exchange::HeaderPolicy) -> serde_json::Value {
    serde_json::json!({
        "contract":"sigil-http-exchange/v1", "envelope":"AH5", "command":"HC5",
        "request_facts":"RF1", "request_headers":["x-request-id"],
        "request_header_bytes":http_exchange::REQUEST_HEADER_BYTES,
        "request_header_count":http_exchange::REQUEST_HEADER_COUNT,
        "fresh_request_identity_bits":http_exchange::FRESH_ID_BYTES * 8,
        "response":"HR1", "headers":"HH1", "response_headers":policy.names().collect::<Vec<_>>(),
        "response_types":http_exchange::MEDIA_TYPES, "response_frame_bytes":http_exchange::FRAME_BYTES,
        "header_bytes":http_exchange::HEADER_BYTES, "header_count":http_exchange::HEADER_COUNT,
        "header_value_bytes":http_exchange::HEADER_VALUE_BYTES
    })
}
fn bind_http_manifest(
    identity: &mut serde_json::Value,
    contract: EntryContract,
    policy: &http_exchange::HeaderPolicy,
) {
    identity["contract"] = serde_json::json!("sigil-application-host/v7");
    identity["http"] = http_manifest(policy);
    if contract.uses_request_facts() {
        identity["contract"] = serde_json::json!("sigil-application-host/v8");
        let http = &mut identity["http"];
        http["contract"] = serde_json::json!("sigil-http-exchange/v2");
        http["envelope"] = serde_json::json!("AH6");
        http["command"] = serde_json::json!("HC6");
        http["envelope_fields"] = serde_json::json!(17);
        http["presentation"] = serde_json::json!({
            "index":15, "source":"authorization-exact-Bearer-space-prefix",
            "exact_prefix":"1", "other":"0", "boot":"", "authorization_verdict":false
        });
        http["clock_fraction"] = serde_json::json!({
            "index":16, "same_sample_as_index":4, "units":"nonzero-subsecond-nanoseconds",
            "present":"1", "absent":"0"
        });
        identity["read_set"] = serde_json::json!({
            "contract":"sigil-read-set/v1", "command":"read_many", "observation":"RM1",
            "batch":"RB1", "record":"RR1", "query_bytes":readset::QUERY_BYTES,
            "items":sigil_durable_store::store::READ_SET_ITEMS,
            "value_bytes":sigil_durable_store::store::READ_SET_VALUE_BYTES,
            "response_bytes":readset::RESPONSE_BYTES, "snapshot":"single-read-transaction",
            "scope":"held-native-scope-all-addresses", "errors":"all-or-nothing"
        });
    }
    if contract == EntryContract::ProcessFacts {
        identity["execution_inspection"] = runtime_inspection::manifest();
        identity["storage_inspection"] = storage_inspection::manifest();
        identity["contract"] = serde_json::json!("sigil-application-host/v9");
        identity["http"]["contract"] = serde_json::json!("sigil-http-exchange/v3");
        identity["http"]["envelope"] = serde_json::json!("AH7");
        identity["http"]["command"] = serde_json::json!("HC7");
        identity["http"]["envelope_fields"] = serde_json::json!(18);
        identity["http"]["monotonic_clock"] = serde_json::json!({
            "index":17, "observation":"MC1", "domain_bits":256,
            "units":"nanoseconds-since-process-origin", "maximum":i64::MAX,
            "boot":"", "application_policy":false
        });
        identity["storage_observations"] = serde_json::json!({
            "durable_read":"DR2", "durable_commit":"DC2",
            "temporary_read":"VR1", "temporary_write":"VC1",
            "failure_origins":["precheck","storage"],
            "response_bytes":storage_observation::RESPONSE_BYTES,
            "temporary_query_bytes":storage_observation::TEMPORARY_QUERY_BYTES,
            "durable_and_temporary_receipts_distinct":true
        });
    }
}
impl Function {
    fn inspect_owner(&self) -> Result<()> {
        match &self.worker {
            PureExecution::Fresh(worker) => worker.facts().map(|_| ()),
            PureExecution::Cached(worker) => worker.inspect_owner(),
        }
        .map_err(runtime_inspection::owner_fault)
    }
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
            source_sha256: config.source_sha256.clone(),
            runtime_sha256: config.runtime_sha256.clone(),
            worker: PureExecution::Fresh(Bridge::new(config).map_err(|_| "config")?),
        })
    }
    fn cached(config: WorkerConfig) -> Result<Self> {
        Self::manifest(&config)?;
        Ok(Self {
            fuel: config.max_fuel,
            timeout_ms: config.max_timeout_ms.min(30000),
            source_sha256: config.source_sha256.clone(),
            runtime_sha256: config.runtime_sha256.clone(),
            worker: PureExecution::Cached(PureBridge::new(config).map_err(|_| "config")?),
        })
    }
    fn invoke(&mut self, input: String, deadline: Instant) -> Result<String> {
        self.invoke_observed(input, deadline)
            .map(|(output, _)| output)
    }
    // The selected timeout is an observation of the actual invocation, not its
    // configured ceiling or a caller-supplied claim about elapsed execution.
    fn invoke_observed(&mut self, input: String, deadline: Instant) -> Result<(String, u64)> {
        self.observe(input, deadline).result
    }
}
impl Machine {
    fn new(
        entry: WorkerConfig,
        configs: BTreeMap<String, WorkerConfig>,
        automatic: Option<&automatic::Config>,
        entry_contract: EntryContract,
        http: Option<&HttpConfig>,
        process: Option<&process_facts::Config>,
    ) -> Result<Self> {
        process_facts::require_profile(entry_contract, process)?;
        let runtime_registry = if entry_contract == EntryContract::ProcessFacts {
            Some(runtime_inspection::Registry::new(
                &entry, &configs, automatic,
            )?)
        } else {
            None
        };
        if configs.len() > 8 || entry_contract.uses_http() != http.is_some() {
            return Err("config");
        }
        let http = http
            .map(|config| {
                http_exchange::HeaderPolicy::new(
                    &config
                        .response_headers
                        .iter()
                        .map(String::as_str)
                        .collect::<Vec<_>>(),
                )
            })
            .transpose()?;
        let header_inventory = http
            .as_ref()
            .map(|policy| {
                serde_json::to_string(&policy.names().collect::<Vec<_>>()).map_err(|_| "config")
            })
            .transpose()?
            .unwrap_or_default();
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
        if let Some(commands) = entry_contract.command_inventory() {
            identity["contract"] = serde_json::json!(if automatic.is_some() {
                "sigil-application-host/v6"
            } else {
                "sigil-application-host/v5"
            });
            identity["commands"] = serde_json::from_str(commands).map_err(|_| "config")?;
        }
        if let Some(policy) = &http {
            bind_http_manifest(&mut identity, entry_contract, policy);
        }
        if let Some(config) = process {
            process_facts::bind_manifest(&mut identity, config);
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
            entry_contract,
            http,
            header_inventory,
            runtime_registry,
        })
    }
    fn run(
        &mut self,
        context: Context,
        mut store: Option<&mut Store>,
        scope: Option<&Scope>,
        last_clock: &mut u64,
        mut process: Option<&mut process_facts::ProcessFacts>,
        automatic: Option<&automatic::Automatic>,
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
            let sample = ClockSample::observe()?;
            let clock = sample.seconds;
            if clock < *last_clock {
                return Err("clock");
            }
            *last_clock = clock;
            let clock_text = clock.to_string();
            let monotonic =
                if self.entry_contract == EntryContract::ProcessFacts && context.mode != "boot" {
                    process.as_deref_mut().ok_or("config")?.observe()?
                } else {
                    String::new()
                };
            let mut values: Vec<&str> = vec![
                &context.request.method,
                &context.request.path,
                &context.request.body,
                &context.facts,
                &clock_text,
                &context.id,
                stage,
                &observation,
                &continuation,
                &self.bundle,
                &self.function_names,
                context.mode,
            ];
            if let Some(commands) = self.entry_contract.command_inventory() {
                values.push(commands);
            }
            if self.http.is_some() {
                values.push(&context.http_request_facts);
                values.push(&self.header_inventory);
            }
            if self.entry_contract.uses_request_facts() {
                values.push(if context.mode == "boot" {
                    ""
                } else {
                    context.presentation.wire()
                });
                // This fraction belongs to AH6 field 4, not a second clock read.
                // Credential/time-guard seconds keep their existing units.
                values.push(sample.fraction_wire());
            }
            if self.entry_contract == EntryContract::ProcessFacts {
                values.push(&monotonic);
            }
            let input = frame(self.entry_contract.envelope_marker(), &values)?;
            let raw = self.entry.invoke(input, deadline)?;
            let step = if self.entry_contract == EntryContract::ProcessFacts {
                action::perform_with_process_facts(
                    (
                        self.entry_contract,
                        self.http.as_ref(),
                        process.as_deref_mut(),
                    ),
                    store.as_deref_mut(),
                    scope,
                    last_clock,
                    deadline,
                    &raw,
                    now,
                )?
            } else {
                perform_with_http_contract(
                    (self.entry_contract, self.http.as_ref()),
                    store.as_deref_mut(),
                    scope,
                    last_clock,
                    deadline,
                    &raw,
                    now,
                )?
            };
            match step {
                Step::InspectExecution {
                    storage,
                    read_scope_checked,
                    continuation: held,
                } => {
                    let runtime = if read_scope_checked {
                        self.runtime_registry
                            .as_ref()
                            .ok_or("capability")?
                            .observe(deadline, || {
                                self.entry.inspect_owner()?;
                                for function in self.functions.values() {
                                    function.inspect_owner()?;
                                }
                                automatic.ok_or("owner_unavailable")?.inspect_owners()
                            })?
                    } else {
                        frame("RI1\n", &["error", "scope_not_checked", ""])?
                    };
                    observation = frame("EI1\n", &[&storage, &runtime])?;
                    stage = "inspect_execution";
                    continuation = held;
                }
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
        let entry_contract = entry_profile(
            config.version,
            config.automatic.is_some(),
            config.http.is_some(),
        )?;
        process_facts::require_profile(entry_contract, config.process_facts.as_ref())?;
        if let Some(process) = &config.process_facts {
            process.validate(&config.limits, &config.credentials)?;
        }
        if config.credentials.is_empty()
            || config.credentials.len() > 64
            || !config.worker.net.is_empty()
            || !config.worker.fs.is_empty()
            || !config.worker.secret_env.is_empty()
        {
            return Err("config");
        }
        let mut machine = Machine::new(
            config.worker,
            config.functions,
            config.automatic.as_ref(),
            entry_contract,
            config.http.as_ref(),
            config.process_facts.as_ref(),
        )?;
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
                    request_id_hints: Vec::new(),
                },
                facts: String::new(),
                id: String::new(),
                mode: "boot",
                http_request_facts: String::new(),
                presentation: Presentation::Other,
            },
            None,
            None,
            &mut clock,
            None,
            None,
        )?;
        if boot.reply.status != 204
            || !boot.reply.body.is_empty()
            || !boot.reply.headers.is_empty()
            || boot.reply.content_type != "application/json"
        {
            return Err("config");
        }
        let guard = boot.guard.ok_or("protocol")?;
        let admitted = config
            .automatic
            .map(|auto| {
                automatic::Admitted::new(auto, &config.credentials, &machine.bundle, &config.limits)
            })
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
        let mut store =
            Store::open(&config.state_root, mode, config.limits).map_err(|_| "storage")?;
        let process_facts = config
            .process_facts
            .map(|config| process_facts::ProcessFacts::open(&store, config))
            .transpose()?;
        let automatic = admitted
            .map(|a| a.open(&mut store, machine.bundle.clone()))
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
            process_facts,
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
        let presentation = Presentation::observe(request.authorization.as_deref());
        let matched = self.credential(request.authorization.as_deref());
        // Raw bearer material never enters SIGIL, storage, logs or a continuation.
        request.authorization = None;
        let http_request_facts = if self.machine.http.is_some() {
            // Separate entropy acquisition: an HTTP correlation label does not
            // expose or derive from the durable operation identity below.
            let fresh = identity()?;
            http_exchange::request_facts(
                &fresh[..http_exchange::FRESH_ID_BYTES * 2],
                &request
                    .request_id_hints
                    .iter()
                    .map(Vec::as_slice)
                    .collect::<Vec<_>>(),
            )?
        } else {
            if !request.request_id_hints.is_empty() {
                return Err("protocol");
            }
            String::new()
        };
        request.request_id_hints.clear();
        let facts = matched
            .map(|i| self.credentials[i].facts.clone())
            .unwrap_or_default();
        let context = Context {
            request,
            facts,
            id: identity()?,
            mode: "request",
            http_request_facts,
            presentation,
        };
        self.machine
            .run(
                context,
                Some(&mut self.store),
                matched.map(|i| &self.credentials[i].scope),
                &mut self.last_clock,
                self.process_facts.as_mut(),
                self.automatic.as_ref(),
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

#[cfg(test)]
mod http_tests;
