//! Private facts from an actual grantless evaluation. This is not an audit
//! publisher, authority verdict or effect observation supplied by a caller.
use crate::{AtomicBool, Function, PureExecution, Result};
use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use sigil_worker_bridge::Fault;
use std::time::Instant;

const INPUT_LIMIT: usize = 4 * 1024 * 1024;

#[derive(Serialize)]
struct Content {
    sha256: String,
    bytes: usize,
    representation: &'static str,
}
impl Content {
    fn new(bytes: &[u8], representation: &'static str) -> Self {
        Self {
            sha256: format!("{:x}", Sha256::digest(bytes)),
            bytes: bytes.len(),
            representation,
        }
    }
}

// Fields and constructors stay inside this native module. In particular this
// does not implement Deserialize, accept a reported result, or retain raw data.
#[derive(Serialize)]
pub(crate) struct Facts {
    mode: &'static str,
    admitted_source_sha256: String,
    admitted_runtime_sha256: String,
    input_bytes: usize,
    input: Option<Content>,
    selected_fuel: u64,
    selected_timeout_ms: Option<u64>,
    elapsed_ms: u128,
    boundary: &'static str,
    error: Option<&'static str>,
    fault: Option<Fault>,
    request_may_have_run: Option<bool>,
    worker_reaped: Option<bool>,
    runtime_status: Option<String>,
    parsed_result: Option<Content>,
    output_kind: &'static str,
    output: Option<Content>,
}

impl Facts {
    fn new(mode: &'static str, input: &str, function: &Function) -> Self {
        Self {
            mode,
            // Identity accepted by the actual native owner. On refusal this
            // does not assert that those bytes were executed, nor is it compiler
            // verification provenance or a signature over the observation.
            admitted_source_sha256: function.source_sha256.clone(),
            admitted_runtime_sha256: function.runtime_sha256.clone(),
            input_bytes: input.len(),
            // The original bridge will refuse larger inputs. Observation must
            // not add an unbounded hashing operation before that refusal.
            input: (input.len() <= INPUT_LIMIT).then(|| Content::new(input.as_bytes(), "utf8")),
            selected_fuel: function.fuel,
            selected_timeout_ms: None,
            elapsed_ms: 0,
            boundary: "before_invoke",
            error: None,
            fault: None,
            request_may_have_run: None,
            worker_reaped: None,
            runtime_status: None,
            parsed_result: None,
            output_kind: "not_observed",
            output: None,
        }
    }

    fn observe_result(&mut self, result: &Value) {
        self.runtime_status = result["status"].as_str().map(str::to_owned);
        // This identifies the parsed value's explicit serde representation,
        // NOT original wire bytes or guest output. No result body is retained.
        self.parsed_result = serde_json::to_vec(result)
            .ok()
            .map(|bytes| Content::new(&bytes, "serde-json-value/v1"));
        match result.get("data").and_then(|v| v.get("output_text")) {
            Some(Value::String(output)) => {
                self.output_kind = "string";
                self.output = Some(Content::new(output.as_bytes(), "utf8"));
            }
            Some(_) => self.output_kind = "non_string",
            None => self.output_kind = "missing",
        }
    }

    pub(crate) fn input_digest(&self) -> Result<&str> {
        self.input
            .as_ref()
            .map(|v| v.sha256.as_str())
            .ok_or("limit")
    }

    pub(crate) fn input_bytes(&self) -> usize {
        self.input_bytes
    }

    pub(crate) fn output_digest(&self) -> Result<&str> {
        self.output
            .as_ref()
            .map(|v| v.sha256.as_str())
            .ok_or("protocol")
    }
}

pub(crate) struct Evaluation {
    pub(crate) result: Result<(String, u64)>,
    pub(crate) facts: Facts,
}

impl Function {
    /// The caller may inspect failure facts before reducing the old result to
    /// its public error code. No retry, new grant or publication happens here.
    pub(crate) fn observe(&mut self, input: String, deadline: Instant) -> Evaluation {
        let started = Instant::now();
        let mode = match self.worker {
            PureExecution::Fresh(_) => "fresh",
            PureExecution::Cached(_) => "cached",
        };
        let mut facts = Facts::new(mode, &input, self);
        let result = (|| {
            let remaining = deadline
                .saturating_duration_since(Instant::now())
                .as_millis() as u64;
            if remaining == 0 {
                return Err("deadline");
            }
            let timeout = remaining.min(self.timeout_ms);
            facts.selected_timeout_ms = Some(timeout);
            let result = match &mut self.worker {
                PureExecution::Fresh(worker) => {
                    facts.boundary = "prepare";
                    let prepared = worker.prepare(input, self.fuel, timeout).map_err(|fault| {
                        facts.fault = Some(fault);
                        "worker"
                    })?;
                    facts.boundary = "execute";
                    let seen = worker
                        .execute(&prepared.ticket, &AtomicBool::new(false))
                        .map_err(|fault| {
                            facts.fault = Some(fault);
                            "worker"
                        })?;
                    facts.boundary = "observation";
                    facts.fault = seen.fault;
                    facts.request_may_have_run = Some(seen.request_may_have_run);
                    facts.worker_reaped = Some(seen.worker_reaped);
                    if let Some(result) = &seen.result {
                        facts.observe_result(result);
                    }
                    if seen.fault.is_some() || !seen.worker_reaped {
                        return Err("worker");
                    }
                    seen.result.ok_or("worker")?
                }
                PureExecution::Cached(worker) => {
                    facts.boundary = "cached_invoke";
                    let result = worker.invoke(input, self.fuel, timeout).map_err(|fault| {
                        facts.fault = Some(fault);
                        "worker"
                    })?;
                    facts.observe_result(&result);
                    // PureBridge does not expose the fresh bridge's send/reap
                    // observation. None stays unknown, including on success.
                    result
                }
            };
            facts.boundary = "runtime_status";
            if result["status"] != "ok" {
                return Err("application");
            }
            facts.boundary = "output";
            let output = result["data"]["output_text"]
                .as_str()
                .map(str::to_owned)
                .ok_or("protocol")?;
            facts.boundary = "complete";
            Ok((output, timeout))
        })();
        facts.error = result.as_ref().err().copied();
        facts.elapsed_ms = started.elapsed().as_millis();
        Evaluation { result, facts }
    }
}

#[cfg(test)]
mod tests;
