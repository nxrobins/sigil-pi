//! One fixed, receipt-bound effect lane. The storage owner never moves into the
//! worker thread, and no public method accepts a caller-authored observation.
use super::recorded::{BoundRecords, empty_facts, validate_delivery, worker_facts};
use super::*;
use crate::frame;
use crate::policy::Policy;
use serde::Serialize;
use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::thread::{self, JoinHandle};

/// Bootstrap-owned lane with an independent ceiling of one in-flight effect.
/// The embedding host must also bound its registry of lanes. Neither construction
/// nor cancellation is public-client authority; SIGIL chooses/authorizes both.
pub struct OwnedWorker {
    worker: Option<Bridge>,
    recorder: Recorder,
    scope: Scope,
    flight: Option<Flight>,
}

#[derive(Serialize)]
pub struct Started {
    pub claim_receipt: Receipt,
    pub generation: String,
    /// Opaque SIGIL correlation, not a native scheduling decision.
    pub context: String,
}

#[derive(Serialize)]
pub struct OwnedCompletion {
    pub completion: Recorded,
    pub context: String,
    /// Loss of the execution owner is NOT a definitely-unsent refusal.
    pub owner_lost: bool,
}

// Only the actual local claim path can construct a Handoff. It is not Clone,
// Serialize or Deserialize, and carries no replaceable caller storage scope.
struct Handoff {
    intent: Check,
    snapshot: Record,
    claim_namespace: String,
    claim_value: String,
    receipt: Receipt,
    generation: String,
    ticket: String,
    guard: Guard,
    deadline: Instant,
    clock: u64,
    refused: Option<&'static str>,
}
struct Flight {
    bound: Handoff,
    context: String,
    cancel: Arc<AtomicBool>,
    thread: Option<JoinHandle<WorkerReport>>,
    immediate: Option<WorkerReport>,
}
struct WorkerReport {
    worker: Option<Bridge>,
    observation: Option<Observation>,
    refused: Option<&'static str>,
    clock: u64,
}

impl Attempt<'_> {
    fn handoff(mut self, recorder: &mut Recorder) -> Result<Handoff> {
        let receipt = self.claim_recorded(recorder)?;
        let Phase::Claimed(claim_value) = &self.phase else {
            return Err("claim");
        };
        let claim_value = claim_value.clone();
        let refused = self.admit_execution().err();
        let bound = Handoff {
            intent: self.intent.clone(),
            snapshot: self.snapshot.clone(),
            claim_namespace: self.claim_namespace.clone(),
            claim_value,
            receipt,
            generation: self.prepared.generation.clone(),
            ticket: self.prepared.ticket.clone(),
            guard: self.guard.clone(),
            deadline: self.deadline,
            clock: *self.last_clock,
            refused,
        };
        // Only a successful gate transfers the still-pending ticket. A refusal
        // keeps normal Drop retirement and is later recorded as a no-invocation
        // fact. Either path has the actual claim receipt, never a supplied one.
        self.retire_pending = refused.is_some();
        Ok(bound)
    }
}

impl OwnedWorker {
    /// Actual active correlation only; knowing it is not cancellation authority.
    pub fn generation(&self) -> Option<&str> {
        self.flight.as_ref().map(|f| f.bound.generation.as_str())
    }

    pub fn context(&self) -> Option<&str> {
        self.flight.as_ref().map(|f| f.context.as_str())
    }

    pub fn active_coordinate(&self) -> Option<(&str, &str)> {
        self.flight.as_ref().map(|f| {
            (
                f.bound.intent.namespace.as_str(),
                f.bound.intent.key.as_str(),
            )
        })
    }

    /// Trusted embedding only. The containing registry must also exclude active
    /// handles in its OTHER lanes before choosing abandoned-claim recovery.
    pub fn recover(&mut self, store: &mut Store, binding: Binding) -> Result<Recovered> {
        if self.flight.is_some() {
            return Err("busy");
        }
        self.recorder.recover(store, &self.scope, binding)
    }

    pub fn new(worker: Bridge, recorder: Recorder, scope: Scope) -> Self {
        Self {
            worker: Some(worker),
            recorder,
            scope,
            flight: None,
        }
    }

    /// Prepare/claim using fixed SIGIL policy and actual host facts. Only lookup
    /// identities cross this API. The borrow ends before the effect is polled.
    /// A successful return acknowledges the claim, NOT effect delivery.
    pub fn start(
        &mut self,
        policy: &mut Policy,
        store: &mut Store,
        last_clock: &mut u64,
        values: &[String],
    ) -> Result<Started> {
        if self.flight.is_some() {
            return Err("busy");
        }
        let worker = self.worker.as_mut().ok_or("worker_unavailable")?;
        let (attempt, context) = policy.begin(store, &self.scope, worker, last_clock, values)?;
        let bound = attempt.handoff(&mut self.recorder)?;
        Ok(self.launch(bound, context))
    }

    fn launch(&mut self, bound: Handoff, context: String) -> Started {
        let mut worker = self.worker.take().expect("exclusive prepared worker");
        let started = Started {
            claim_receipt: bound.receipt.clone(),
            generation: bound.generation.clone(),
            context: context.clone(),
        };
        let cancel = Arc::new(AtomicBool::new(false));
        let mut flight = Flight {
            bound,
            context,
            cancel: Arc::clone(&cancel),
            thread: None,
            immediate: None,
        };
        if let Some(code) = flight.bound.refused {
            flight.immediate = Some(WorkerReport {
                worker: Some(worker),
                observation: None,
                refused: Some(code),
                clock: flight.bound.clock,
            });
        } else {
            let ticket = flight.bound.ticket.clone();
            let guard = flight.bound.guard.clone();
            let deadline = flight.bound.deadline;
            let mut clock = flight.bound.clock;
            // JoinHandle is the sole result channel. It is private and cannot be
            // substituted by an HTTP/stdio observation or a serialized receipt.
            match thread::Builder::new()
                .name("sigil-effect".into())
                .spawn(move || {
                    let gate = admit_time(Some(&guard), &mut clock, deadline, now);
                    let (observation, refused) = match gate {
                        Ok(()) => match worker.execute(&ticket, &cancel) {
                            Ok(observed) => (Some(observed), None),
                            Err(_) => (None, Some("worker")),
                        },
                        Err(code) => {
                            let _ = worker.cancel_pending(&ticket);
                            (None, Some(code))
                        }
                    };
                    WorkerReport {
                        worker: Some(worker),
                        observation,
                        refused,
                        clock,
                    }
                }) {
                Ok(handle) => flight.thread = Some(handle),
                Err(_) => {
                    // The closure was never invoked. Its worker was dropped and
                    // this lane stays unavailable; no replacement is manufactured.
                    flight.immediate = Some(WorkerReport {
                        worker: None,
                        observation: None,
                        refused: Some("spawn"),
                        clock: flight.bound.clock,
                    });
                }
            }
        }
        self.flight = Some(flight);
        started
    }

    /// Nonblocking signal for the actually owned worker. SIGIL must authorize
    /// this call and any durable cancellation transition. True means only that a
    /// signal was issued to a held flight, not that local or remote work stopped.
    pub fn request_cancel(&self) -> bool {
        if let Some(flight) = &self.flight {
            flight.cancel.store(true, Ordering::Release);
            true
        } else {
            false
        }
    }

    /// Pending checks never join a running thread or invoke result policy. Once
    /// finished, commit using the original scope and re-read exact immutable
    /// bindings; result recording has its own bounded interval after expiry.
    pub fn poll(
        &mut self,
        store: &mut Store,
        last_clock: &mut u64,
    ) -> Result<Option<OwnedCompletion>> {
        let flight = self.flight.as_ref().ok_or("idle")?;
        if flight.thread.as_ref().is_some_and(|t| !t.is_finished()) {
            return Ok(None);
        }
        let mut flight = self.flight.take().unwrap();
        let report = match flight.thread.take() {
            Some(handle) => handle.join().ok(),
            None => flight.immediate.take(),
        };
        let owner_lost = report.is_none();
        let (observation, refused) = if let Some(report) = report {
            *last_clock = (*last_clock).max(report.clock);
            self.worker = report.worker;
            (report.observation, report.refused)
        } else {
            // No Bridge is recovered from a panicked/lost controller. The lane
            // cannot dispatch again, even if SIGIL records uncertainty below.
            (None, None)
        };
        let terminal = self.record(
            store,
            &flight.bound,
            observation.as_ref(),
            refused,
            owner_lost,
        );
        let (phase, delivery_receipt, recording_error) = match terminal {
            Ok((phase, receipt)) => (Some(phase), Some(receipt), None),
            Err(code) => (None, None, Some(code)),
        };
        Ok(Some(OwnedCompletion {
            completion: Recorded {
                claim_receipt: flight.bound.receipt,
                delivery_receipt,
                phase,
                observation,
                refused,
                recording_error,
            },
            context: flight.context,
            owner_lost,
        }))
    }

    fn record(
        &mut self,
        store: &mut Store,
        bound: &Handoff,
        observation: Option<&Observation>,
        refused: Option<&'static str>,
        owner_lost: bool,
    ) -> Result<(String, Receipt)> {
        let deadline = self.recorder.deadline();
        let intent = store
            .get(&self.scope, &bound.intent.namespace, &bound.intent.key)
            .map_err(|_| "storage")?;
        let claim = store
            .get(&self.scope, &bound.claim_namespace, &bound.intent.key)
            .map_err(|_| "storage")?;
        if intent != bound.snapshot {
            return Err("intent");
        }
        if claim.revision != 1 || claim.value.as_deref() != Some(&bound.claim_value) {
            return Err("claim");
        }
        if !store
            .unused_create_slot(
                &self.scope,
                self.recorder.delivery_namespace(),
                &bound.intent.key,
            )
            .map_err(|_| "storage")?
        {
            return Err("delivery");
        }
        let facts = if owner_lost {
            frame(
                "WF1\n",
                &[
                    "abandoned",
                    &bound.generation,
                    "1",
                    "0",
                    "owner_unavailable",
                    "",
                    "missing",
                    "0",
                    "",
                ],
            )?
        } else if let Some(observed) = observation {
            if observed.generation != bound.generation {
                return Err("generation");
            }
            worker_facts(observed)?
        } else {
            empty_facts("refused", &bound.generation, refused.ok_or("protocol")?)?
        };
        let (phase, hint, raw) = self.recorder.propose_bound(
            BoundRecords {
                intent: &bound.intent,
                claim_namespace: &bound.claim_namespace,
                intent_value: intent.value.as_deref().ok_or("intent")?,
                claim: &claim,
                generation: &bound.generation,
            },
            &facts,
            deadline,
        )?;
        if hint != "record_after_commit" {
            return Err("protocol");
        }
        let batch = commit_batch(&raw)?;
        validate_delivery(
            &batch,
            &bound.intent,
            &bound.claim_namespace,
            self.recorder.delivery_namespace(),
            &bound.generation,
            &phase,
        )?;
        if Instant::now() >= deadline {
            return Err("deadline");
        }
        let receipt = store.commit(&self.scope, &batch).map_err(|_| "storage")?;
        Ok((phase, receipt))
    }
}

impl Drop for OwnedWorker {
    fn drop(&mut self) {
        // Dropping a JoinHandle detaches it; signaling is not confirmed cleanup.
        // Durable claims remain consumed and restart must recover, never replay.
        self.request_cancel();
    }
}

#[cfg(test)]
mod tests;
