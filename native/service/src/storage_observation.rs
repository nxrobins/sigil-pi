//! Proposed storage-result codec, not enabled by any existing entry profile.
//!
//! The native host reports a finite mechanism outcome, never a fallback or retry
//! decision. Temporary cells have distinct read and receipt markers and cannot
//! become durable commit acknowledgements. The future versioned action binding
//! must enforce held authority, its fresh time guard, and process-lifetime state.

use crate::{Result, frame};
use sigil_durable_store::store::volatile::{VALUE_BYTES, Volatile, VolatileReceipt};
use sigil_durable_store::store::{
    Batch, Error, FailureOrigin, Mutation, ObservedResult, Receipt, Record, Scope, Store,
};
use sigil_worker_bridge::strict;

pub const RESPONSE_BYTES: usize = crate::readset::RESPONSE_BYTES;
// JSON can represent every byte of an ASCII control-string with six bytes. This
// bounds input framing, not the smaller retained value or admitted grant limits.
pub const TEMPORARY_QUERY_BYTES: usize = 6 * VALUE_BYTES + 1024;

/// Exhaustive native facts only. No diagnostics, paths, SQL, credentials or
/// application state. An added store outcome requires an explicit codec review.
pub const fn error_label(error: Error) -> &'static str {
    match error {
        Error::Invalid => "invalid",
        Error::Denied => "denied",
        Error::Conflict => "conflict",
        Error::AlreadyExists => "already_exists",
        Error::Missing => "missing",
        Error::Busy => "busy",
        Error::Corrupt => "corrupt",
        Error::Storage => "storage",
        Error::Limit => "limit",
        Error::CommitUncertain => "commit_uncertain",
        Error::ReopenRequired => "reopen_required",
    }
}

fn bounded(marker: &str, values: &[&str]) -> Result<String> {
    let size = values.iter().try_fold(4usize, |size, value| {
        size.checked_add(8)?.checked_add(value.len())
    });
    if size.is_none_or(|size| size > RESPONSE_BYTES) {
        return Err("limit");
    }
    frame(marker, values)
}

pub const fn origin_label(origin: FailureOrigin) -> &'static str {
    match origin {
        FailureOrigin::Precheck => "precheck",
        FailureOrigin::Storage => "storage",
    }
}

fn read_success(marker: &str, record: Record, with_origin: bool) -> Result<String> {
    if record.revision > i64::MAX as u64 || (record.revision == 0 && record.value.is_some()) {
        // Impossible internal records stay protocol failures, never fabricated
        // storage outages that could enable application fallback policy.
        return Err("protocol");
    }
    let revision = record.revision.to_string();
    let mut values = vec![
        "ok",
        &revision,
        if record.value.is_some() { "1" } else { "0" },
        record.value.as_deref().unwrap_or(""),
        "",
    ];
    if with_origin {
        values.push("");
    }
    bounded(marker, &values)
}

// DR2: status, revision, presence, value, error label, error origin. A native
// origin fact is included ONLY on failure; it does not become authority for a
// later action. Temporary VR1 stays distinct and has no durable failure origin.
fn durable_read_result(seen: ObservedResult<Record>) -> Result<String> {
    match seen {
        Ok(record) => read_success("DR2\n", record, true),
        Err(failure) => bounded(
            "DR2\n",
            &[
                "error",
                "0",
                "0",
                "",
                error_label(failure.error),
                origin_label(failure.origin),
            ],
        ),
    }
}

fn temporary_read_result(seen: sigil_durable_store::store::Result<Record>) -> Result<String> {
    match seen {
        Ok(record) => read_success("VR1\n", record, false),
        Err(error) => bounded("VR1\n", &["error", "0", "0", "", error_label(error)]),
    }
}

fn positive_revision(revision: u64) -> Result<String> {
    if revision == 0 || revision > i64::MAX as u64 {
        return Err("protocol");
    }
    Ok(revision.to_string())
}

// DC2: status, revision, error label, error origin. No observed failure becomes
// a success receipt, including uncertain commit or a storage-capacity failure.
fn durable_receipt(seen: ObservedResult<Receipt>) -> Result<String> {
    match seen {
        Ok(receipt) => bounded(
            "DC2\n",
            &["ok", &positive_revision(receipt.revision)?, "", ""],
        ),
        Err(failure) => bounded(
            "DC2\n",
            &[
                "error",
                "0",
                error_label(failure.error),
                origin_label(failure.origin),
            ],
        ),
    }
}

fn temporary_receipt(seen: sigil_durable_store::store::Result<VolatileReceipt>) -> Result<String> {
    match seen {
        Ok(receipt) => bounded("VC1\n", &["ok", &positive_revision(receipt.revision)?, ""]),
        Err(error) => bounded("VC1\n", &["error", "0", error_label(error)]),
    }
}

/// Actual native reads/commits, not caller-declared outcomes. These functions do
/// not grant access or decide application permission. They require held native
/// scopes; the embedding action must check its clock/deadline immediately first.
pub fn durable_read(store: &Store, scope: &Scope, namespace: &str, key: &str) -> Result<String> {
    durable_read_result(store.get_observed(scope, namespace, key))
}

pub fn durable_commit(store: &mut Store, scope: &Scope, batch: &Batch) -> Result<String> {
    durable_receipt(store.commit_observed(scope, batch))
}

pub fn temporary_read(
    cells: &Volatile,
    store: &Store,
    scope: &Scope,
    namespace: &str,
    key: &str,
) -> Result<String> {
    temporary_read_result(cells.get(store, scope, namespace, key))
}

/// One explicit nullable mutation, not a batch, transaction, reset or grant.
/// Strict JSON rejects duplicate keys before typed deserialization. Authority,
/// revision and retained-value bounds are checked by the real cell mechanism.
#[derive(Debug)]
pub struct TemporaryWrite {
    mutation: Mutation,
}

impl TemporaryWrite {
    pub fn parse(raw: &str) -> Result<Self> {
        if raw.len() > TEMPORARY_QUERY_BYTES {
            return Err("limit");
        }
        let value = strict::parse(raw.as_bytes()).map_err(|_| "protocol")?;
        let mutation = serde_json::from_value(value).map_err(|_| "protocol")?;
        Ok(Self { mutation })
    }

    pub fn perform(&self, cells: &mut Volatile, store: &Store, scope: &Scope) -> Result<String> {
        temporary_receipt(cells.compare_set(store, scope, &self.mutation))
    }
}

#[cfg(test)]
#[path = "storage_observation_tests.rs"]
mod tests;
