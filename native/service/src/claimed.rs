//! A durable create-once slot gates a frozen one-use worker ticket.
//!
//! SIGIL supplies the claim bytes and time window. This mechanism checks the
//! SD1 claim binding but does not choose transitions, authority, or retries.
//! The same actual store, scope and worker are exclusively borrowed throughout
//! preparation, commit and execution. Caller-supplied receipts cannot arm it.
use crate::action::{Guard, admit_time, commit_batch};
use crate::{Result, fields, frame, now};
use sigil_durable_store::store::{Check, Receipt, Record, Scope, Store};
use sigil_worker_bridge::{Bridge, Observation, Prepared};
use std::sync::atomic::AtomicBool;
use std::time::{Duration, Instant};

mod owned;
mod recorded;
pub use owned::{OwnedCompletion, OwnedWorker, Started};
pub use recorded::{Recorded, Recorder, RecorderConfig, Recovered};

pub struct Binding {
    pub intent_namespace: String,
    pub claim_namespace: String,
    pub key: String,
}
pub struct Execution {
    pub input: String,
    pub fuel: u64,
    pub timeout_ms: u64,
    pub time_guard: String,
}

enum Phase {
    Prepared,
    Claimed(String),
    Closed,
}

pub struct Attempt<'a> {
    store: &'a mut Store,
    scope: &'a Scope,
    worker: &'a mut Bridge,
    last_clock: &'a mut u64,
    intent: Check,
    snapshot: Record,
    claim_namespace: String,
    prepared: Prepared,
    guard: Guard,
    deadline: Instant,
    phase: Phase,
    retire_pending: bool,
    // Actual prepare arguments, never supplied by a result/publication request.
    // Contains only an input hash/length, selected ceilings and the checked TG1.
    prepared_audit: String,
}

impl<'a> Attempt<'a> {
    pub fn begin(
        store: &'a mut Store,
        scope: &'a Scope,
        worker: &'a mut Bridge,
        last_clock: &'a mut u64,
        binding: Binding,
        execution: Execution,
    ) -> Result<Self> {
        if binding.intent_namespace == binding.claim_namespace
            || execution.timeout_ms == 0
            || execution.timeout_ms > 300_000
        {
            return Err("protocol");
        }
        let deadline = Instant::now() + Duration::from_millis(execution.timeout_ms);
        let guard = Guard::parse(&execution.time_guard)?;
        admit_time(Some(&guard), last_clock, deadline, now)?;
        // Store validates identifiers, ownership and scope, and authenticates
        // retained bytes. A caller cannot substitute a claimed snapshot.
        let snapshot = store
            .get(scope, &binding.intent_namespace, &binding.key)
            .map_err(|_| "storage")?;
        let slot = store
            .get(scope, &binding.claim_namespace, &binding.key)
            .map_err(|_| "storage")?;
        if snapshot.revision == 0 || snapshot.value.as_ref().is_none_or(String::is_empty) {
            return Err("intent");
        }
        // Tombstones are not empty slots. A retained claim, terminal or not,
        // cannot be used to mint a new ticket after any restart.
        if slot.revision != 0 || slot.value.is_some() {
            return Err("claimed");
        }
        let input_bytes = execution.input.len();
        let prepared = worker
            .prepare(execution.input, execution.fuel, execution.timeout_ms)
            .map_err(|_| "worker")?;
        let prepared_audit = frame(
            "WP1\n",
            &[
                &prepared.input_sha256,
                &input_bytes.to_string(),
                &execution.fuel.to_string(),
                &execution.timeout_ms.to_string(),
                &execution.time_guard,
            ],
        )?;
        Ok(Self {
            store,
            scope,
            worker,
            last_clock,
            intent: Check {
                namespace: binding.intent_namespace,
                key: binding.key,
                revision: snapshot.revision,
            },
            snapshot,
            claim_namespace: binding.claim_namespace,
            prepared,
            guard,
            deadline,
            phase: Phase::Prepared,
            retire_pending: true,
            prepared_audit,
        })
    }

    pub fn prepared(&self) -> &Prepared {
        &self.prepared
    }
    pub fn intent(&self) -> &Record {
        &self.snapshot
    }

    pub(crate) fn bind_dispatch(
        &mut self,
        evidence: crate::policy::DispatchEvidence,
    ) -> Result<()> {
        self.prepared_audit = evidence.bind_prepared(&self.prepared_audit)?;
        Ok(())
    }

    /// Accept exactly one create-only claim and a check of the bound intent.
    /// Claim bytes remain SIGIL-owned. The receipt is generated here, never
    /// accepted from the caller; committing elsewhere cannot arm this attempt.
    pub fn claim(&mut self, raw: &str) -> Result<Receipt> {
        self.claim_inner(raw, None)
    }

    // Only the private native recorder can couple a claim to its actual audit
    // proposal. This is not a public callback or caller-provided receipt path.
    fn claim_inner(
        &mut self,
        raw: &str,
        recorded: Option<(&mut Recorder, &recorded::Proposal)>,
    ) -> Result<Receipt> {
        if !matches!(self.phase, Phase::Prepared) {
            return Err("ticket");
        }
        // A failed or uncertain claim cannot subsequently dispatch. Drop also
        // retires the underlying ticket; durable uncertainty is left for SIGIL.
        self.phase = Phase::Closed;
        let batch = commit_batch(raw)?;
        if batch.checks != [self.intent.clone()] || batch.writes.len() != 1 {
            return Err("claim");
        }
        let write = &batch.writes[0];
        if write.namespace != self.claim_namespace
            || write.key != self.intent.key
            || write.revision != 0
            || write.value.as_ref().is_none_or(String::is_empty)
        {
            return Err("claim");
        }
        let value = write.value.as_deref().unwrap();
        let record = fields(value, "SD1\n", 5)?;
        if record
            != [
                self.intent.namespace.as_str(),
                self.intent.key.as_str(),
                self.intent.revision.to_string().as_str(),
                self.prepared.generation.as_str(),
                "1",
            ]
        {
            return Err("claim");
        }
        admit_time(Some(&self.guard), self.last_clock, self.deadline, now)?;
        let receipt = match recorded {
            Some((recorder, proposal)) => recorder.commit(
                self.store,
                self.scope,
                proposal,
                &batch,
                self.deadline,
                Some((&self.guard, self.last_clock)),
            )?,
            None => self
                .store
                .commit(self.scope, &batch)
                .map_err(|_| "storage")?,
        };
        self.phase = Phase::Claimed(write.value.clone().unwrap());
        Ok(receipt)
    }

    /// Initiate this frozen worker at most once after our own successful commit.
    /// Every Err is before invoking the worker for this attempt. After invocation,
    /// the bridge returns its actual may-have-run/reaping/result observation.
    pub fn execute(&mut self, ticket: &str, cancel: &AtomicBool) -> Result<Observation> {
        if ticket != self.prepared.ticket {
            return Err("ticket");
        }
        self.admit_execution()?;
        self.worker.execute(ticket, cancel).map_err(|_| "worker")
    }

    // Shared by synchronous execution and the owned handoff. An owned handoff
    // also checks the time guard in its execution thread before invoking Bridge.
    fn admit_execution(&mut self) -> Result<()> {
        let phase = std::mem::replace(&mut self.phase, Phase::Closed);
        let Phase::Claimed(value) = phase else {
            return Err("unclaimed");
        };
        admit_time(Some(&self.guard), self.last_clock, self.deadline, now)?;
        let intent = self
            .store
            .get(self.scope, &self.intent.namespace, &self.intent.key)
            .map_err(|_| "storage")?;
        let claim = self
            .store
            .get(self.scope, &self.claim_namespace, &self.intent.key)
            .map_err(|_| "storage")?;
        if intent != self.snapshot || claim.revision != 1 || claim.value.as_deref() != Some(&value)
        {
            return Err("claim");
        }
        admit_time(Some(&self.guard), self.last_clock, self.deadline, now)?;
        Ok(())
    }
}

impl Drop for Attempt<'_> {
    fn drop(&mut self) {
        // No other caller can obtain our borrowed worker before this retirement.
        // An already consumed ticket legitimately produces Ticket here.
        if self.retire_pending {
            let _ = self.worker.cancel_pending(&self.prepared.ticket);
        }
    }
}

#[cfg(test)]
mod tests;

#[cfg(test)]
mod audit_tests;
