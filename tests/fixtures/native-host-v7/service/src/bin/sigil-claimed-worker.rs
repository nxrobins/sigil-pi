//! Trusted local embedding/conformance surface. Not a product API or dispatcher.
//! One bootstrap store scope and one fixed effect worker; no caller grants.
use serde::Deserialize;
use serde_json::{Value, json};
use sigil_application_host::claimed::{
    Attempt, Binding, Execution, OwnedWorker, Recorder, RecorderConfig,
};
use sigil_application_host::policy::{Config as PolicyConfig, Policy};
use sigil_durable_store::store::{Access, Batch, Check, Limits, Mutation, OpenMode, Scope, Store};
use sigil_worker_bridge::{Bridge, Config as WorkerConfig, strict};
use std::collections::BTreeMap;
use std::fs::File;
use std::io::{self, BufRead, Read, Write};
use std::path::PathBuf;
use std::sync::atomic::AtomicBool;

type Result<T> = std::result::Result<T, &'static str>;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    version: u32,
    worker: WorkerConfig,
    state_root: PathBuf,
    limits: Limits,
    grants: BTreeMap<String, Access>,
    #[serde(default)]
    policy: Option<PolicyConfig>,
    #[serde(default)]
    recorder: Option<RecorderConfig>,
}
#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
enum Request {
    Poll {
        generation: String,
    },
    Recover {
        intent_namespace: String,
        key: String,
    },
    Run {
        ticket: String,
    },
    Authorize {
        values: Vec<String>,
    },
    Prepare {
        intent_namespace: String,
        claim_namespace: String,
        key: String,
        input: String,
        fuel: u64,
        timeout_ms: u64,
        time_guard: String,
    },
    Claim {
        batch: String,
    },
    Execute {
        ticket: String,
    },
    Cancel {
        ticket: String,
    },
    Get {
        namespace: String,
        key: String,
    },
    Commit {
        checks: Vec<Check>,
        writes: Vec<Mutation>,
    },
}

// Version 4 is a trusted local conformance adapter for the owned library, not an
// automatic application driver or an authenticated product cancellation API.
fn serve_owned(
    input: &mut impl BufRead,
    output: &mut impl Write,
    store: &mut Store,
    read_scope: &Scope,
    policy: &mut Policy,
    mut lane: OwnedWorker,
    last_clock: &mut u64,
) -> Result<()> {
    while let Some(request) = read(input)? {
        let result = match request {
            Ok(Request::Authorize { values }) => lane
                .start(policy, store, last_clock, &values)
                .map(|started| json!({"started":started})),
            Ok(Request::Poll { generation }) if lane.generation() == Some(generation.as_str()) => {
                lane.poll(store, last_clock)
                    .map(|completion| json!({"completion":completion}))
            }
            Ok(Request::Poll { .. }) => Err("ticket"),
            Ok(Request::Cancel { ticket }) if lane.generation() == Some(ticket.as_str()) => {
                Ok(json!({"cancellation_requested":lane.request_cancel()}))
            }
            Ok(Request::Cancel { .. }) => Err("ticket"),
            Ok(Request::Get { namespace, key }) => store
                .get(read_scope, &namespace, &key)
                .map(|r| json!({"record":r}))
                .map_err(|_| "storage"),
            _ => Err("protocol"),
        };
        write(output, result)?;
    }
    // Drop requests best-effort cancellation, not an acknowledged stop/delivery.
    Ok(())
}
fn read(input: &mut impl BufRead) -> Result<Option<Result<Request>>> {
    let mut raw = Vec::new();
    let n = input
        .take(16 * 1024 * 1024 + 1)
        .read_until(b'\n', &mut raw)
        .map_err(|_| "transport")?;
    if n == 0 {
        return Ok(None);
    }
    if n > 16 * 1024 * 1024 || raw.last() != Some(&b'\n') {
        return Err("limit");
    }
    Ok(Some(strict::parse(&raw).map_err(|_| "protocol").and_then(
        |v| serde_json::from_value(v).map_err(|_| "protocol"),
    )))
}
fn write(out: &mut impl Write, result: Result<Value>) -> Result<()> {
    let value = match result {
        Ok(mut value) => {
            value["status"] = json!("ok");
            value
        }
        Err(code) => json!({"status":"error","code":code}),
    };
    writeln!(out, "{value}")
        .and_then(|_| out.flush())
        .map_err(|_| "transport")
}
fn serve_attempt(
    input: &mut impl BufRead,
    output: &mut impl Write,
    mut attempt: Attempt<'_>,
    context: String,
    mut recorder: Option<&mut Recorder>,
) -> Result<()> {
    write(
        output,
        Ok(json!({"prepared":attempt.prepared(),"intent":attempt.intent(),"context":context})),
    )?;
    while let Some(request) = read(input)? {
        let (result, finish) = match request {
            Ok(Request::Run { ticket }) => match recorder.as_mut() {
                Some(recorder) => (
                    attempt
                        .run_recorded(&ticket, recorder, &AtomicBool::new(false))
                        .map(|r| json!({"completion":r})),
                    true,
                ),
                None => (Err("protocol"), false),
            },
            Ok(Request::Claim { batch }) if recorder.is_none() => {
                let result = attempt.claim(&batch).map(|r| json!({"receipt":r}));
                let finish = result.is_err();
                (result, finish)
            }
            Ok(Request::Execute { ticket }) if recorder.is_none() => (
                attempt
                    .execute(&ticket, &AtomicBool::new(false))
                    .map(|o| json!({"observation":o})),
                true,
            ),
            Ok(Request::Cancel { ticket }) if ticket == attempt.prepared().ticket => {
                (Ok(json!({"cancelled_before_start":true})), true)
            }
            _ => (Err("protocol"), false),
        };
        write(output, result)?;
        if finish {
            break;
        }
    }
    // Drop retires pending execution before storage or another attempt is exposed.
    Ok(())
}
fn run() -> Result<()> {
    let args = std::env::args().collect::<Vec<_>>();
    if args.len() != 2 {
        return Err("config");
    }
    let mut raw = Vec::new();
    File::open(&args[1])
        .map_err(|_| "config")?
        .take(65537)
        .read_to_end(&mut raw)
        .map_err(|_| "config")?;
    if raw.len() > 65536 {
        return Err("limit");
    }
    let config: Config =
        serde_json::from_value(strict::parse(&raw).map_err(|_| "config")?).map_err(|_| "config")?;
    if !matches!(config.version, 1..=4)
        || !config.state_root.is_absolute()
        || (config.version >= 2) != config.policy.is_some()
        || (config.version >= 3) != config.recorder.is_some()
    {
        return Err("config");
    }
    Store::validate_grants(&config.grants).map_err(|_| "config")?;
    if let Some(recorder) = &config.recorder {
        let claim = &config.policy.as_ref().ok_or("config")?.claim_namespace;
        let delivery = &recorder.delivery_namespace;
        if claim == delivery
            || config.grants.get(claim) != Some(&Access::ReadWrite)
            || config.grants.get(delivery) != Some(&Access::CreateOnly)
            || config
                .grants
                .iter()
                .any(|(ns, access)| ns != claim && ns != delivery && *access != Access::Read)
        {
            return Err("config");
        }
    }
    let claim_namespace = config.policy.as_ref().map(|p| p.claim_namespace.clone());
    let mut recorder = config.recorder.map(Recorder::new).transpose()?;
    let mut worker = Bridge::new(config.worker).map_err(|_| "config")?;
    let mut store = Store::open(&config.state_root, OpenMode::Existing, config.limits)
        .map_err(|_| "storage")?;
    let scope = store.scope(config.grants.clone()).map_err(|_| "config")?;
    let mut policy = config.policy.map(|p| Policy::new(p, &store)).transpose()?;
    let mut last_clock = 0;
    let mut input = io::stdin().lock();
    let mut output = io::stdout().lock();
    writeln!(
        output,
        "{}",
        json!({"status":"ready","protocol":format!("sigil-claimed-worker/v{}", config.version)})
    )
    .and_then(|_| output.flush())
    .map_err(|_| "transport")?;
    if config.version == 4 {
        // Identical bootstrap grants: create-only delivery authority does not
        // become read authority on the conformance get surface.
        let effect_scope = store.scope(config.grants).map_err(|_| "config")?;
        return serve_owned(
            &mut input,
            &mut output,
            &mut store,
            &scope,
            policy.as_mut().ok_or("config")?,
            OwnedWorker::new(worker, recorder.take().ok_or("config")?, effect_scope),
            &mut last_clock,
        );
    }
    while let Some(request) = read(&mut input)? {
        let result = match request {
            Ok(Request::Recover {
                intent_namespace,
                key,
            }) => match &mut recorder {
                Some(recorder) => recorder
                    .recover(
                        &mut store,
                        &scope,
                        Binding {
                            intent_namespace,
                            claim_namespace: claim_namespace.clone().ok_or("config")?,
                            key,
                        },
                    )
                    .map(|r| json!({"recovery": r})),
                None => Err("protocol"),
            },
            Ok(Request::Authorize { values }) => {
                let attempted = match &mut policy {
                    Some(p) => p.begin(&mut store, &scope, &mut worker, &mut last_clock, &values),
                    None => Err("protocol"),
                };
                match attempted {
                    Ok((attempt, context)) => {
                        serve_attempt(
                            &mut input,
                            &mut output,
                            attempt,
                            context,
                            recorder.as_mut(),
                        )?;
                        continue;
                    }
                    Err(code) => Err(code),
                }
            }
            Ok(Request::Prepare {
                intent_namespace,
                claim_namespace,
                key,
                input: payload,
                fuel,
                timeout_ms,
                time_guard,
            }) if policy.is_none() => {
                match Attempt::begin(
                    &mut store,
                    &scope,
                    &mut worker,
                    &mut last_clock,
                    Binding {
                        intent_namespace,
                        claim_namespace,
                        key,
                    },
                    Execution {
                        input: payload,
                        fuel,
                        timeout_ms,
                        time_guard,
                    },
                ) {
                    Ok(attempt) => {
                        serve_attempt(&mut input, &mut output, attempt, String::new(), None)?;
                        continue;
                    }
                    Err(code) => Err(code),
                }
            }
            Ok(Request::Get { namespace, key }) => store
                .get(&scope, &namespace, &key)
                .map(|r| json!({"record":r}))
                .map_err(|_| "storage"),
            Ok(Request::Commit { checks, writes }) if recorder.is_none() => store
                .commit(&scope, &Batch { checks, writes })
                .map(|r| json!({"receipt":r}))
                .map_err(|_| "storage"),
            Ok(
                Request::Execute { .. }
                | Request::Claim { .. }
                | Request::Cancel { .. }
                | Request::Run { .. },
            ) => Err("ticket"),
            Ok(Request::Prepare { .. } | Request::Commit { .. } | Request::Poll { .. }) => {
                Err("protocol")
            }
            Err(code) => Err(code),
        };
        write(&mut output, result)?;
    }
    Ok(())
}
fn main() {
    if let Err(code) = run() {
        eprintln!("{}", json!({"status":"error","code":code}));
        std::process::exit(2);
    }
}
