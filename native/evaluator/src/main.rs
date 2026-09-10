//! One verified, grantless SIGIL artifact per supervised process. No product policy.
//! The existing native bridge holds the source/hash, input and independent limits.
//! Only immutable freshly compiled Wasm is reused; every invocation gets a new
//! Wasmtime Store/Instance, memory and fuel through the exact pinned runtime.
#![forbid(unsafe_code)]

use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sigil_compiler::{CompileLimits, CompilerContext, compile_tool_with_limits_and_context};
use sigil_runtime::{IoGrants, execute_ephemeral};
use sigil_worker_bridge::strict;
use std::io::{self, BufRead, Read, Write};

const SIGIL_PIN: &str = "8277a1d92d599df89e6b4391fc70fd0fa534d696";
const FRAME_MAX: usize = 16 * 1024 * 1024;
const SOURCE_MAX: usize = 65536;
const INPUT_MAX: usize = 4 * 1024 * 1024;
const OUTPUT_MAX: usize = 4 * 1024 * 1024;
const CALL_MAX: u64 = 4096;
const FUEL_MAX: u64 = 1_000_000_000;

type Result<T> = std::result::Result<T, &'static str>;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    jsonrpc: String,
    id: u64,
    method: String,
    params: Value,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Call {
    name: String,
    arguments: Arguments,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Arguments {
    source: String,
    input: String,
    fuel: u64,
    host_profile: String,
    grants: Value,
}
struct Artifact {
    source: String,
    sha256: String,
    wasm: Vec<u8>,
}
#[derive(Default)]
struct Evaluator {
    initialized: bool,
    calls: u64,
    artifact: Option<Artifact>,
}

impl Evaluator {
    fn request(&mut self, raw: &[u8]) -> Result<Value> {
        if raw.len() > FRAME_MAX {
            return Err("frame_limit");
        }
        let value = strict::parse(raw).map_err(|_| "protocol")?;
        let request: Request = serde_json::from_value(value).map_err(|_| "protocol")?;
        if request.jsonrpc != "2.0" {
            return Err("protocol");
        }
        let result = if !self.initialized {
            if request.id != 1 || request.method != "initialize" || request.params != json!({}) {
                return Err("protocol");
            }
            self.initialized = true;
            json!({"protocolVersion":"2024-11-05", "capabilities":{"tools":{}},
                "serverInfo":{"name":"sigil-fixed-evaluator", "version":"0.1.0"}})
        } else {
            if self.calls >= CALL_MAX
                || request.id != self.calls + 2
                || request.method != "tools/call"
            {
                return Err("protocol");
            }
            let call: Call = serde_json::from_value(request.params).map_err(|_| "protocol")?;
            if call.name != "sigil_forge" {
                return Err("protocol");
            }
            self.evaluate(call.arguments)?
        };
        Ok(json!({"jsonrpc":"2.0", "id":request.id, "result":result}))
    }

    fn evaluate(&mut self, arguments: Arguments) -> Result<Value> {
        // This executable cannot be repurposed as an effect worker. No env flag,
        // certificate supplied by the caller, grant or alternate host is accepted.
        if arguments.source.is_empty()
            || arguments.source.len() > SOURCE_MAX
            || arguments.input.len() > INPUT_MAX
            || !(1..=FUEL_MAX).contains(&arguments.fuel)
            || arguments.host_profile != "ephemeral"
            || arguments.grants != json!({"net":[], "fs":[], "secret":[]})
        {
            return Err("admission");
        }
        match &self.artifact {
            Some(artifact) if artifact.source != arguments.source => return Err("source_changed"),
            Some(_) => {}
            None => {
                let context = CompilerContext::with_host_profile(
                    sigil_runtime::host_profile_by_name("ephemeral").ok_or("host_contract")?,
                );
                let compiled = compile_tool_with_limits_and_context(
                    &arguments.source,
                    &CompileLimits::default(),
                    &context,
                )
                .map_err(|_| "verification")?;
                // Require the verdict produced by THIS compilation, never an
                // asserted/cached external certificate or an environment override.
                if !compiled.solver_verified
                    || compiled.wasm.is_empty()
                    || compiled.wasm.len() > FRAME_MAX
                {
                    return Err("verification");
                }
                self.artifact = Some(Artifact {
                    sha256: format!("{:x}", Sha256::digest(arguments.source.as_bytes())),
                    source: arguments.source,
                    wasm: compiled.wasm,
                });
            }
        }
        let artifact = self.artifact.as_ref().ok_or("admission")?;
        self.calls += 1;
        // The pinned runtime retains its 16 MiB guest ceiling, Wasmtime fuel
        // backstop and fresh instance boundary. There is no deserialization of
        // externally supplied precompiled bytes, shared guest state or result cache.
        let outcome = execute_ephemeral(
            &artifact.wasm,
            arguments.input.as_bytes(),
            arguments.fuel,
            &IoGrants::default(),
        );
        let mut result = match outcome {
            Ok(value) if value.output.len() <= OUTPUT_MAX => {
                let text = String::from_utf8(value.output).map_err(|_| "output")?;
                json!({"schema_version":2, "status":"ok", "command":"forge",
                    "data":{"output_text":text, "fuel_consumed":value.fuel_consumed}})
            }
            Ok(_) => return Err("output_limit"),
            Err(_) => json!({"schema_version":2, "status":"error", "command":"forge",
                             "data":{"output_text":null}}),
        };
        result["data"]["fixed_evaluator"] = json!({"sigil_pin":SIGIL_PIN,
            "source_sha256":artifact.sha256, "solver_verified":true,
            "compilations":1, "evaluations":self.calls});
        Ok(json!({"content":[{"type":"text", "text":result.to_string()}]}))
    }
}

fn run(input: impl BufRead, mut output: impl Write) -> Result<()> {
    let mut input = input;
    let mut evaluator = Evaluator::default();
    loop {
        let mut raw = Vec::new();
        let size = input
            .by_ref()
            .take((FRAME_MAX + 1) as u64)
            .read_until(b'\n', &mut raw)
            .map_err(|_| "input")?;
        if size == 0 {
            return Ok(());
        }
        if size > FRAME_MAX || raw.last() != Some(&b'\n') {
            return Err("frame_limit");
        }
        let response = evaluator.request(&raw)?.to_string();
        if response.len() + 1 > FRAME_MAX {
            return Err("output_limit");
        }
        output
            .write_all(response.as_bytes())
            .map_err(|_| "output")?;
        output.write_all(b"\n").map_err(|_| "output")?;
        output.flush().map_err(|_| "output")?;
        if evaluator.calls == CALL_MAX {
            return Ok(());
        }
    }
}

fn main() {
    if std::env::args_os().len() != 1 {
        eprintln!("fixed_evaluator_refused: arguments");
        std::process::exit(1);
    }
    if let Err(code) = run(io::stdin().lock(), io::stdout().lock()) {
        // Static categories only; do not print source, inputs or compiler errors.
        eprintln!("fixed_evaluator_refused: {code}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests;
