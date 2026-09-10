//! Execute an application-selected command with a fresh, generic time check.
//! Credential validity and operation deadlines are SIGIL policy, not parsed here.
use crate::{Reply, Result, fields, frame};
use serde::Deserialize;
use sigil_durable_store::store::{Batch, Check, Mutation, Scope, Store};
use sigil_worker_bridge::strict;
use std::time::Instant;

#[derive(Debug)]
pub(crate) enum Step {
    Reply {
        reply: Reply,
        guard: Option<Guard>,
    },
    Call {
        target: String,
        input: String,
        continuation: String,
    },
    Continue {
        stage: &'static str,
        observation: String,
        continuation: String,
    },
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CommitCommand {
    op: String,
    checks: Vec<Check>,
    writes: Vec<Mutation>,
}

pub(crate) fn commit_batch(raw: &str) -> Result<Batch> {
    if raw.len() > 2 * 1024 * 1024 {
        return Err("limit");
    }
    let parsed = strict::parse(raw.as_bytes()).map_err(|_| "protocol")?;
    let batch: CommitCommand = serde_json::from_value(parsed).map_err(|_| "protocol")?;
    if batch.op != "commit" {
        return Err("protocol");
    }
    Ok(Batch {
        checks: batch.checks,
        writes: batch.writes,
    })
}

#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Guard {
    before: u64,
    until: u64,
}
impl Guard {
    pub(crate) fn parse(raw: &str) -> Result<Self> {
        fn second(v: &str) -> Result<u64> {
            if v.is_empty()
                || v.len() > 16
                || !v.bytes().all(|b| b.is_ascii_digit())
                || (v.len() > 1 && v.starts_with('0'))
            {
                return Err("protocol");
            }
            let n: u64 = v.parse().map_err(|_| "protocol")?;
            if n > 9_007_199_254_740_991 {
                return Err("protocol");
            }
            Ok(n)
        }
        let values = fields(raw, "TG1\n", 2)?;
        let before = second(values[0])?;
        let until = second(values[1])?;
        if until <= before {
            return Err("protocol");
        }
        Ok(Self { before, until })
    }
    fn check(&self, clock: u64) -> Result<()> {
        if clock < self.before || clock >= self.until {
            return Err("time_guard");
        }
        Ok(())
    }
}

enum Command<'a> {
    Reply(u16, &'a str),
    Read(&'a str, &'a str),
    Commit(Batch),
    Call(&'a str, &'a str),
}

pub(crate) fn admit_time(
    guard: Option<&Guard>,
    last_clock: &mut u64,
    deadline: Instant,
    clock: impl FnOnce() -> Result<u64>,
) -> Result<()> {
    if Instant::now() >= deadline {
        return Err("deadline");
    }
    let at = clock()?;
    if at < *last_clock {
        return Err("clock");
    }
    *last_clock = at;
    if let Some(guard) = guard {
        guard.check(at)?;
    }
    if Instant::now() >= deadline {
        return Err("deadline");
    }
    Ok(())
}

pub(crate) fn perform(
    store: Option<&mut Store>,
    scope: Option<&Scope>,
    last_clock: &mut u64,
    deadline: Instant,
    raw: &str,
    clock: impl FnOnce() -> Result<u64>,
) -> Result<Step> {
    let action = fields(raw, "HC3\n", 5)?;
    let guard = if action[4].is_empty() {
        // Unguarded replies permit non-sensitive refusal responses. Bootstrap has
        // its own mandatory guard because it precedes opening native storage.
        // The application selects which replies require its authority time window.
        if action[0] != "reply" {
            return Err("protocol");
        }
        None
    } else {
        Some(Guard::parse(action[4])?)
    };
    let command = match action[0] {
        "reply" => {
            if action[1].len() != 3 || !action[1].bytes().all(|b| b.is_ascii_digit()) {
                return Err("protocol");
            }
            let status: u16 = action[1].parse().map_err(|_| "protocol")?;
            if !(200..=599).contains(&status) || !action[3].is_empty() {
                return Err("protocol");
            }
            Command::Reply(status, action[2])
        }
        "read" => Command::Read(action[1], action[2]),
        "call" => Command::Call(action[1], action[2]),
        "commit" => {
            if !action[2].is_empty() {
                return Err("protocol");
            }
            Command::Commit(commit_batch(action[1])?)
        }
        _ => return Err("protocol"),
    };

    // All worker execution and command parsing have finished. Check both clocks
    // just before initiating the native action. This does not promise completion
    // of a filesystem operation or delivery of an HTTP response before expiry.
    admit_time(guard.as_ref(), last_clock, deadline, clock)?;
    let (stage, observation) = match command {
        Command::Reply(status, body) => {
            return Ok(Step::Reply {
                reply: Reply {
                    status,
                    body: body.to_owned(),
                },
                guard,
            });
        }
        Command::Call(target, input) => {
            return Ok(Step::Call {
                target: target.to_owned(),
                input: input.to_owned(),
                continuation: action[3].to_owned(),
            });
        }
        Command::Read(namespace, key) => {
            let scope = scope.ok_or("capability")?;
            let store = store.ok_or("capability")?;
            let seen = match store.get(scope, namespace, key) {
                Ok(r) => frame(
                    "SR1\n",
                    &[
                        "ok",
                        &r.revision.to_string(),
                        r.value.as_deref().unwrap_or(""),
                    ],
                )?,
                Err(_) => frame("SR1\n", &["error", "0", ""])?,
            };
            ("read", seen)
        }
        Command::Commit(batch) => {
            let scope = scope.ok_or("capability")?;
            let store = store.ok_or("capability")?;
            let seen = match store.commit(scope, &batch) {
                Ok(r) => frame("SC1\n", &["ok", &r.revision.to_string()])?,
                Err(_) => frame("SC1\n", &["error", "0"])?,
            };
            ("commit", seen)
        }
    };
    Ok(Step::Continue {
        stage,
        observation,
        continuation: action[3].to_owned(),
    })
}

#[cfg(test)]
mod tests;
