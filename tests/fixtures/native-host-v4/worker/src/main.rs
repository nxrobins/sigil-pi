//! Trusted stdio embedding surface, not an HTTP/API entry point.
use serde::Deserialize;
use serde_json::json;
use sigil_worker_bridge::{Bridge, Config, Fault, strict};
use std::fs::File;
use std::io::{self, BufRead, Read, Write};
use std::sync::atomic::AtomicBool;

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
enum Request {
    Prepare {
        input: String,
        fuel: u64,
        timeout_ms: u64,
    },
    Execute {
        ticket: String,
    },
    Cancel {
        ticket: String,
    },
}
fn run() -> Result<(), Fault> {
    let args = std::env::args().collect::<Vec<_>>();
    if args.len() != 2 {
        return Err(Fault::Invalid);
    }
    let mut raw = Vec::new();
    File::open(&args[1])
        .map_err(|_| Fault::Invalid)?
        .take(65537)
        .read_to_end(&mut raw)
        .map_err(|_| Fault::Invalid)?;
    if raw.len() > 65536 {
        return Err(Fault::Limit);
    }
    let config: Config = serde_json::from_value(strict::parse(&raw).map_err(|_| Fault::Invalid)?)
        .map_err(|_| Fault::Invalid)?;
    let mut bridge = Bridge::new(config)?;
    let mut out = io::stdout().lock();
    writeln!(out,"{}",json!({"status":"ready","protocol":"sigil-worker/v1","linux_parent_death_signal":cfg!(target_os="linux")})).map_err(|_|Fault::Transport)?;
    out.flush().map_err(|_| Fault::Transport)?;
    let mut input = io::stdin().lock();
    loop {
        let mut raw = Vec::new();
        let n = (&mut input)
            .take(16 * 1024 * 1024 + 1)
            .read_until(b'\n', &mut raw)
            .map_err(|_| Fault::Transport)?;
        if n == 0 {
            return Ok(());
        }
        if n > 16 * 1024 * 1024 || raw.last() != Some(&b'\n') {
            return Err(Fault::Limit);
        }
        let request = strict::parse(&raw)
            .map_err(|_| Fault::Invalid)
            .and_then(|v| serde_json::from_value::<Request>(v).map_err(|_| Fault::Invalid));
        let result = request.and_then(|r| match r {
            Request::Prepare {
                input,
                fuel,
                timeout_ms,
            } => bridge
                .prepare(input, fuel, timeout_ms)
                .map(|p| json!({"prepared":p})),
            Request::Execute { ticket } => bridge
                .execute(&ticket, &AtomicBool::new(false))
                .map(|o| json!({"observation":o})),
            Request::Cancel { ticket } => bridge
                .cancel_pending(&ticket)
                .map(|_| json!({"cancelled_before_start":true})),
        });
        let response = match result {
            Ok(mut v) => {
                v["status"] = json!("ok");
                v
            }
            Err(e) => json!({"status":"error","code":e}),
        };
        writeln!(out, "{response}").map_err(|_| Fault::Transport)?;
        out.flush().map_err(|_| Fault::Transport)?;
    }
}
fn main() {
    if let Err(e) = run() {
        eprintln!("{}", json!({"status":"error","code":e}));
        std::process::exit(2);
    }
}
