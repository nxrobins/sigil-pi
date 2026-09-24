//! A bounded action codec over actual native storage inspection. The result is
//! content-free and carries neither readiness policy nor future write authority.
use crate::{Result, frame};
use sigil_durable_store::store::inspection::{self, Failure};
use sigil_durable_store::store::{FailureOrigin, Scope, Store};
use sigil_worker_bridge::strict;
use std::time::Instant;

pub const QUERY_BYTES: usize = 2048;

pub(crate) struct Query {
    namespaces: Vec<String>,
}

pub(crate) struct Observation {
    pub(crate) wire: String,
    pub(crate) read_scope_checked: bool,
}

impl Query {
    pub(crate) fn parse(raw: &str) -> Result<Self> {
        if raw.len() > QUERY_BYTES {
            return Err("limit");
        }
        let parsed = strict::parse(raw.as_bytes()).map_err(|_| "protocol")?;
        let namespaces = serde_json::from_value(parsed).map_err(|_| "protocol")?;
        Ok(Self { namespaces })
    }

    pub(crate) fn observe(
        &self,
        store: &mut Store,
        scope: &Scope,
        deadline: Instant,
    ) -> Result<String> {
        Ok(self.inspect(store, scope, deadline)?.wire)
    }

    pub(crate) fn inspect(
        &self,
        store: &mut Store,
        scope: &Scope,
        deadline: Instant,
    ) -> Result<Observation> {
        let seen = store.inspect_namespaces(scope, &self.namespaces, deadline);
        let read_scope_checked = !matches!(seen,
            Err(Failure::Observed(failure)) if failure.origin == FailureOrigin::Precheck);
        let (status, label, origin) = match seen {
            Ok(()) => ("ok", "", ""),
            Err(Failure::Observed(failure)) => (
                "error",
                crate::storage_observation::error_label(failure.error),
                crate::storage_observation::origin_label(failure.origin),
            ),
            Err(Failure::Deadline) => ("error", "deadline", "execution"),
            Err(Failure::WorkLimit) => ("error", "work_limit", "execution"),
        };
        Ok(Observation {
            wire: frame("SI1\n", &[status, label, origin])?,
            read_scope_checked,
        })
    }
}

pub(crate) fn manifest() -> serde_json::Value {
    serde_json::json!({
        "contract":"sigil-storage-inspection/v1", "command":"inspect_storage",
        "observation":"SI1", "fields":["status","error","origin"],
        "query_bytes":QUERY_BYTES, "namespaces":inspection::NAMESPACES,
        "maximum_ms":inspection::TIMEOUT_MS,
        "sqlite_progress_steps":inspection::SQLITE_STEPS,
        "deadline":"cooperative-no-late-success-not-kernel-interruption",
        "snapshot":"single-read-transaction-all-or-nothing",
        "scope":"held-native-read-scope-all-requested-namespaces",
        "physical_checks":["private-files","sqlite-identity","exact-schema","quick-check","frozen-limits","capacity"],
        "scoped_checks":["bounded-values","names","revisions","utf8","record-digests"],
        "origins":["precheck","storage","execution"],
        "returns_contents":false, "writes":false, "readiness_decision":false,
        "write_reservation":false, "audit_or_retention_verification":false
    })
}
