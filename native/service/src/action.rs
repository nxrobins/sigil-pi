//! Execute an application-selected command with a fresh, generic time check.
//! Credential validity and operation deadlines are SIGIL policy, not parsed here.
use crate::{Reply, Result, fields, frame};
use serde::Deserialize;
use sigil_durable_store::store::{Batch, Check, Mutation, Scope, Store};
use sigil_worker_bridge::strict;
use std::time::Instant;

/// Explicit entry ABI. Legacy deployments retain AH3/HC3 and cannot request
/// metadata. The newer profiles advertise the implemented command inventory.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum EntryContract {
    Legacy,
    Metadata,
    HttpExchange,
    RequestAdmission,
    ProcessFacts,
}

impl EntryContract {
    pub(crate) fn envelope_marker(self) -> &'static str {
        match self {
            Self::Legacy => "AH3\n",
            Self::Metadata => "AH4\n",
            Self::HttpExchange => "AH5\n",
            Self::RequestAdmission => "AH6\n",
            Self::ProcessFacts => "AH7\n",
        }
    }

    fn command_marker(self) -> &'static str {
        match self {
            Self::Legacy => "HC3\n",
            Self::Metadata => "HC4\n",
            Self::HttpExchange => "HC5\n",
            Self::RequestAdmission => "HC6\n",
            Self::ProcessFacts => "HC7\n",
        }
    }

    pub(crate) fn command_inventory(self) -> Option<&'static str> {
        match self {
            Self::Legacy => None,
            Self::Metadata | Self::HttpExchange => {
                Some(r#"["call","commit","metadata","read","reply"]"#)
            }
            Self::RequestAdmission => {
                Some(r#"["call","commit","metadata","read","read_many","reply"]"#)
            }
            Self::ProcessFacts => Some(
                r#"["call","commit","commit_observed","inspect_execution","inspect_storage","metadata","read","read_many","read_observed","reply","temporary_read","temporary_write"]"#,
            ),
        }
    }

    pub(crate) fn uses_http(self) -> bool {
        matches!(
            self,
            Self::HttpExchange | Self::RequestAdmission | Self::ProcessFacts
        )
    }

    pub(crate) fn uses_request_facts(self) -> bool {
        matches!(self, Self::RequestAdmission | Self::ProcessFacts)
    }
}

#[derive(Debug)]
pub(crate) enum Step {
    InspectExecution {
        storage: String,
        read_scope_checked: bool,
        continuation: String,
    },
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

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct MetadataCommand {
    after: Option<String>,
    limit: usize,
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
    Reply(
        u16,
        &'a str,
        Option<crate::http_exchange::ResponseMetadata<'a>>,
    ),
    Read(&'a str, &'a str),
    Commit(Batch),
    Call(&'a str, &'a str),
    Metadata(&'a str, MetadataCommand),
    ReadSet(crate::readset::ReadSet),
    ReadObserved(&'a str, &'a str),
    CommitObserved(Batch),
    TemporaryRead(&'a str, &'a str),
    TemporaryWrite(crate::storage_observation::TemporaryWrite),
    InspectStorage(crate::storage_inspection::Query),
    InspectExecution(crate::storage_inspection::Query),
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

#[cfg(test)]
fn perform(
    store: Option<&mut Store>,
    scope: Option<&Scope>,
    last_clock: &mut u64,
    deadline: Instant,
    raw: &str,
    clock: impl FnOnce() -> Result<u64>,
) -> Result<Step> {
    perform_with_contract(
        EntryContract::Legacy,
        store,
        scope,
        last_clock,
        deadline,
        raw,
        clock,
    )
}

#[cfg(test)]
pub(crate) fn perform_with_contract(
    contract: EntryContract,
    store: Option<&mut Store>,
    scope: Option<&Scope>,
    last_clock: &mut u64,
    deadline: Instant,
    raw: &str,
    clock: impl FnOnce() -> Result<u64>,
) -> Result<Step> {
    perform_with_http_contract(
        (contract, None),
        store,
        scope,
        last_clock,
        deadline,
        raw,
        clock,
    )
}

pub(crate) fn perform_with_http_contract(
    profile: (EntryContract, Option<&crate::http_exchange::HeaderPolicy>),
    store: Option<&mut Store>,
    scope: Option<&Scope>,
    last_clock: &mut u64,
    deadline: Instant,
    raw: &str,
    clock: impl FnOnce() -> Result<u64>,
) -> Result<Step> {
    perform_with_process_facts(
        (profile.0, profile.1, None),
        store,
        scope,
        last_clock,
        deadline,
        raw,
        clock,
    )
}

pub(crate) fn perform_with_process_facts(
    profile: (
        EntryContract,
        Option<&crate::http_exchange::HeaderPolicy>,
        Option<&mut crate::process_facts::ProcessFacts>,
    ),
    store: Option<&mut Store>,
    scope: Option<&Scope>,
    last_clock: &mut u64,
    deadline: Instant,
    raw: &str,
    clock: impl FnOnce() -> Result<u64>,
) -> Result<Step> {
    let (contract, http, process) = profile;
    if contract.uses_http() != http.is_some() {
        return Err("protocol");
    }
    let action = fields(raw, contract.command_marker(), 5)?;
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
            let metadata = http.map(|policy| policy.parse(action[2])).transpose()?;
            Command::Reply(status, action[2], metadata)
        }
        "read" => Command::Read(action[1], action[2]),
        "call" => Command::Call(action[1], action[2]),
        "metadata"
            if matches!(
                contract,
                EntryContract::Metadata
                    | EntryContract::HttpExchange
                    | EntryContract::RequestAdmission
                    | EntryContract::ProcessFacts
            ) =>
        {
            let parsed = strict::parse(action[2].as_bytes()).map_err(|_| "protocol")?;
            let query = serde_json::from_value(parsed).map_err(|_| "protocol")?;
            Command::Metadata(action[1], query)
        }
        "read_many" if contract.uses_request_facts() => {
            if !action[2].is_empty() {
                return Err("protocol");
            }
            Command::ReadSet(crate::readset::ReadSet::parse(action[1])?)
        }
        "commit" => {
            if !action[2].is_empty() {
                return Err("protocol");
            }
            Command::Commit(commit_batch(action[1])?)
        }
        "read_observed" if contract == EntryContract::ProcessFacts => {
            Command::ReadObserved(action[1], action[2])
        }
        "commit_observed" if contract == EntryContract::ProcessFacts => {
            if !action[2].is_empty() {
                return Err("protocol");
            }
            Command::CommitObserved(commit_batch(action[1])?)
        }
        "temporary_read" if contract == EntryContract::ProcessFacts => {
            Command::TemporaryRead(action[1], action[2])
        }
        "temporary_write" if contract == EntryContract::ProcessFacts => {
            if !action[2].is_empty() {
                return Err("protocol");
            }
            Command::TemporaryWrite(crate::storage_observation::TemporaryWrite::parse(
                action[1],
            )?)
        }
        "inspect_storage" if contract == EntryContract::ProcessFacts => {
            if !action[2].is_empty() {
                return Err("protocol");
            }
            Command::InspectStorage(crate::storage_inspection::Query::parse(action[1])?)
        }
        "inspect_execution" if contract == EntryContract::ProcessFacts => {
            if !action[2].is_empty() {
                return Err("protocol");
            }
            Command::InspectExecution(crate::storage_inspection::Query::parse(action[1])?)
        }
        _ => return Err("protocol"),
    };

    // All worker execution and command parsing have finished. Check both clocks
    // just before initiating the native action. This does not promise completion
    // of a filesystem operation or delivery of an HTTP response before expiry.
    admit_time(guard.as_ref(), last_clock, deadline, clock)?;
    let (stage, observation) = match command {
        Command::InspectExecution(query) => {
            let scope = scope.ok_or("capability")?;
            let store = store.ok_or("capability")?;
            let inspected = query.inspect(store, scope, deadline)?;
            return Ok(Step::InspectExecution {
                storage: inspected.wire,
                read_scope_checked: inspected.read_scope_checked,
                continuation: action[3].to_owned(),
            });
        }
        Command::InspectStorage(query) => {
            let scope = scope.ok_or("capability")?;
            let store = store.ok_or("capability")?;
            ("inspect_storage", query.observe(store, scope, deadline)?)
        }
        Command::Reply(status, body, metadata) => {
            let (content_type, headers, body) = match metadata {
                Some(value) => (value.content_type, value.headers, value.body),
                None => ("application/json", Vec::new(), body),
            };
            return Ok(Step::Reply {
                reply: Reply {
                    status,
                    body: body.to_owned(),
                    content_type: content_type.to_owned(),
                    headers: headers
                        .into_iter()
                        .map(|(name, value)| (name.to_owned(), value.to_owned()))
                        .collect(),
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
        Command::ReadObserved(namespace, key) => {
            let scope = scope.ok_or("capability")?;
            let store = store.ok_or("capability")?;
            (
                "read_observed",
                crate::storage_observation::durable_read(store, scope, namespace, key)?,
            )
        }
        Command::CommitObserved(batch) => {
            let scope = scope.ok_or("capability")?;
            let store = store.ok_or("capability")?;
            (
                "commit_observed",
                crate::storage_observation::durable_commit(store, scope, &batch)?,
            )
        }
        Command::TemporaryRead(namespace, key) => {
            let scope = scope.ok_or("capability")?;
            let store = store.ok_or("capability")?;
            let process = process.ok_or("capability")?;
            (
                "temporary_read",
                crate::storage_observation::temporary_read(
                    &process.cells,
                    store,
                    scope,
                    namespace,
                    key,
                )?,
            )
        }
        Command::TemporaryWrite(write) => {
            let scope = scope.ok_or("capability")?;
            let store = store.ok_or("capability")?;
            let process = process.ok_or("capability")?;
            (
                "temporary_write",
                write.perform(&mut process.cells, store, scope)?,
            )
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
        Command::ReadSet(query) => {
            let scope = scope.ok_or("capability")?;
            let store = store.ok_or("capability")?;
            ("read_many", query.observe(store, scope)?)
        }
        Command::Metadata(namespace, query) => {
            let scope = scope.ok_or("capability")?;
            let store = store.ok_or("capability")?;
            let seen = match store.metadata(scope, namespace, query.after.as_deref(), query.limit) {
                Ok(page) => frame(
                    "KM1\n",
                    &[
                        "ok",
                        &serde_json::to_string(&page.entries).map_err(|_| "protocol")?,
                        page.next.as_deref().unwrap_or(""),
                    ],
                )?,
                Err(_) => frame("KM1\n", &["error", "[]", ""])?,
            };
            ("metadata", seen)
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

#[cfg(test)]
mod http_tests;

#[cfg(test)]
mod readset_tests;

#[cfg(test)]
mod process_tests;
