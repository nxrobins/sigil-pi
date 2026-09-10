//! Trusted stdio embedding for the fixed, scoped transaction producer. Not HTTP.
use serde::Deserialize;
use serde_json::{Value, json};
use sigil_application_host::policy::Input;
use sigil_application_host::transaction::{Config as TransactionConfig, Transaction};
use sigil_durable_store::store::{Access, Limits, OpenMode, Store};
use sigil_worker_bridge::{Config as WorkerConfig, strict};
use std::collections::BTreeMap;
use std::fs::File;
use std::io::{self, BufRead, Read, Write};
use std::path::PathBuf;

type Result<T> = std::result::Result<T, &'static str>;
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    version: u32,
    state_root: PathBuf,
    limits: Limits,
    worker: WorkerConfig,
    marker: String,
    values: usize,
    inputs: Vec<Input>,
    grants: BTreeMap<String, Access>,
}
#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
enum Request {
    Apply { values: Vec<String> },
}

fn read(input: &mut impl BufRead) -> Result<Option<Result<Request>>> {
    let mut raw = Vec::new();
    let n = input
        .take(65537)
        .read_until(b'\n', &mut raw)
        .map_err(|_| "transport")?;
    if n == 0 {
        return Ok(None);
    }
    if n > 65536 || raw.last() != Some(&b'\n') {
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
    let c: Config =
        serde_json::from_value(strict::parse(&raw).map_err(|_| "config")?).map_err(|_| "config")?;
    if c.version != 1 || !c.state_root.is_absolute() {
        return Err("config");
    }
    let mut store =
        Store::open(&c.state_root, OpenMode::Existing, c.limits).map_err(|_| "storage")?;
    let mut transaction = Transaction::new(
        TransactionConfig {
            worker: c.worker,
            marker: c.marker,
            values: c.values,
            inputs: c.inputs,
            grants: c.grants,
        },
        &store,
    )?;
    let mut clock = 0;
    let mut input = io::stdin().lock();
    let mut output = io::stdout().lock();
    writeln!(
        output,
        "{}",
        json!({"status":"ready","protocol":"sigil-transaction/v1"})
    )
    .and_then(|_| output.flush())
    .map_err(|_| "transport")?;
    while let Some(request) = read(&mut input)? {
        let result = match request {
            Ok(Request::Apply { values }) => transaction
                .apply(&mut store, &mut clock, &values)
                .map(|v| json!({"applied":v})),
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
