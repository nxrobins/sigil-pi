//! Native-owned inputs/receipts for the fixed SIGIL claim/result producer.
//! SIGIL classifies observations and constructs transactions; this module binds
//! actual facts, checks exact storage coordinates and commits its returned bytes.
use super::*;
use crate::{Function, frame, identity};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sigil_durable_store::store::{Access, Batch};
use sigil_worker_bridge::Config as WorkerConfig;
use std::collections::BTreeMap;

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RecorderConfig {
    pub worker: WorkerConfig,
    pub delivery_namespace: String,
}
pub struct Recorder {
    function: Function,
    delivery_namespace: String,
}
#[derive(Serialize)]
pub struct Recorded {
    pub claim_receipt: Receipt,
    pub delivery_receipt: Option<Receipt>,
    /// SIGIL's committed phase only; absent if recording is not confirmed.
    pub phase: Option<String>,
    pub observation: Option<Observation>,
    /// A gate refusal before calling the worker, not a remote-effect verdict.
    pub refused: Option<&'static str>,
    pub recording_error: Option<&'static str>,
}
#[derive(Serialize)]
pub struct Recovered {
    pub delivery_receipt: Receipt,
    pub phase: String,
    pub claim_generation: String,
    pub recovery_generation: String,
}
pub(super) struct BoundRecords<'a> {
    pub(super) intent: &'a Check,
    pub(super) claim_namespace: &'a str,
    pub(super) intent_value: &'a str,
    pub(super) claim: &'a Record,
    pub(super) generation: &'a str,
}
impl Recorder {
    pub(super) fn delivery_namespace(&self) -> &str {
        &self.delivery_namespace
    }

    pub fn new(config: RecorderConfig) -> Result<Self> {
        Store::validate_grants(&BTreeMap::from([(
            config.delivery_namespace.clone(),
            Access::CreateOnly,
        )]))
        .map_err(|_| "config")?;
        Ok(Self {
            function: Function::new(config.worker)?,
            delivery_namespace: config.delivery_namespace,
        })
    }
    fn propose(
        &mut self,
        attempt: &Attempt<'_>,
        facts: &str,
        deadline: Instant,
    ) -> Result<(String, String, String)> {
        let intent = attempt
            .store
            .get(
                attempt.scope,
                &attempt.intent.namespace,
                &attempt.intent.key,
            )
            .map_err(|_| "storage")?;
        let claim = attempt
            .store
            .get(attempt.scope, &attempt.claim_namespace, &attempt.intent.key)
            .map_err(|_| "storage")?;
        if intent != attempt.snapshot {
            return Err("intent");
        }
        self.propose_bound(
            BoundRecords {
                intent: &attempt.intent,
                claim_namespace: &attempt.claim_namespace,
                intent_value: intent.value.as_deref().unwrap_or(""),
                claim: &claim,
                generation: &attempt.prepared.generation,
            },
            facts,
            deadline,
        )
    }
    pub(super) fn propose_bound(
        &mut self,
        bound: BoundRecords<'_>,
        facts: &str,
        deadline: Instant,
    ) -> Result<(String, String, String)> {
        if self.delivery_namespace == bound.intent.namespace
            || self.delivery_namespace == bound.claim_namespace
            || bound.intent.namespace == bound.claim_namespace
        {
            return Err("config");
        }
        let input = frame(
            "WR1\n",
            &[
                &bound.intent.namespace,
                bound.claim_namespace,
                &self.delivery_namespace,
                &bound.intent.key,
                &bound.intent.revision.to_string(),
                bound.intent_value,
                &bound.claim.revision.to_string(),
                bound.claim.value.as_deref().unwrap_or(""),
                bound.generation,
                facts,
            ],
        )?;
        let output = self.function.invoke(input, deadline)?;
        let parts = fields(&output, "ER1\n", 3)?;
        Ok((parts[0].into(), parts[1].into(), parts[2].into()))
    }
    pub(super) fn deadline(&self) -> Instant {
        Instant::now() + Duration::from_millis(self.function.timeout_ms)
    }
    /// Record a retained claim for which no local Attempt is held. The exclusive
    /// Store borrow excludes a synchronous Attempt, NOT an owned in-flight
    /// worker, surviving orphan or remote effect. The embedding coordinator must
    /// exclude its active handles before choosing recovery. Recovery never
    /// prepares or invokes an effect worker; a late completion cannot replace it.
    /// Coordinates are trusted embedding lookups, not public recovery authority.
    pub fn recover(
        &mut self,
        store: &mut Store,
        scope: &Scope,
        binding: Binding,
    ) -> Result<Recovered> {
        let deadline = self.deadline();
        let snapshot = store
            .get(scope, &binding.intent_namespace, &binding.key)
            .map_err(|_| "storage")?;
        let claim = store
            .get(scope, &binding.claim_namespace, &binding.key)
            .map_err(|_| "storage")?;
        if snapshot.revision == 0 || snapshot.value.as_ref().is_none_or(String::is_empty) {
            return Err("intent");
        }
        if claim.revision != 1 {
            return Err("claim");
        }
        let sd = fields(claim.value.as_deref().ok_or("claim")?, "SD1\n", 5)?;
        let revision = snapshot.revision.to_string();
        if sd[..3]
            != [
                binding.intent_namespace.as_str(),
                binding.key.as_str(),
                &revision,
            ]
            || sd[4] != "1"
        {
            return Err("claim");
        }
        if !store
            .unused_create_slot(scope, &self.delivery_namespace, &binding.key)
            .map_err(|_| "storage")?
        {
            return Err("delivery");
        }
        let claim_generation = sd[3].to_owned();
        let recovery_generation = identity()?;
        let facts = frame(
            "WF1\n",
            &[
                "abandoned",
                &recovery_generation,
                "1",
                "0",
                "owner_unavailable",
                "",
                "missing",
                "0",
                "",
            ],
        )?;
        let intent = Check {
            namespace: binding.intent_namespace,
            key: binding.key,
            revision: snapshot.revision,
        };
        let (phase, hint, raw) = self.propose_bound(
            BoundRecords {
                intent: &intent,
                claim_namespace: &binding.claim_namespace,
                intent_value: snapshot.value.as_deref().ok_or("intent")?,
                claim: &claim,
                generation: &recovery_generation,
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
            &intent,
            &binding.claim_namespace,
            &self.delivery_namespace,
            &claim_generation,
            &phase,
        )?;
        if Instant::now() >= deadline {
            return Err("deadline");
        }
        let delivery_receipt = store.commit(scope, &batch).map_err(|_| "storage")?;
        Ok(Recovered {
            delivery_receipt,
            phase,
            claim_generation,
            recovery_generation,
        })
    }
}

/// Mechanical projection of the runtime protocol, not application interpretation.
/// Large/non-string/missing output stays explicitly such, never a truncated success.
pub(super) fn worker_facts(observed: &Observation) -> Result<String> {
    let fault = observed
        .fault
        .map(|f| serde_json::to_value(f).map_err(|_| "protocol"))
        .transpose()?
        .and_then(|v| v.as_str().map(str::to_owned))
        .unwrap_or_default();
    let status = observed
        .result
        .as_ref()
        .and_then(|v| v.get("status"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let output = observed
        .result
        .as_ref()
        .and_then(|v| v.get("data"))
        .and_then(|v| v.get("output_text"));
    let (kind, length, payload) = match output {
        Some(Value::String(s)) if s.len() <= 1048576 => ("string", s.len(), s.as_str()),
        Some(Value::String(s)) => ("oversized", s.len(), ""),
        Some(_) => ("other", 0, ""),
        None => ("missing", 0, ""),
    };
    frame(
        "WF1\n",
        &[
            "observed",
            &observed.generation,
            if observed.request_may_have_run {
                "1"
            } else {
                "0"
            },
            if observed.worker_reaped { "1" } else { "0" },
            &fault,
            status,
            kind,
            &length.to_string(),
            payload,
        ],
    )
}
pub(super) fn empty_facts(kind: &str, generation: &str, fault: &str) -> Result<String> {
    frame(
        "WF1\n",
        &[kind, generation, "0", "1", fault, "", "missing", "0", ""],
    )
}

impl Attempt<'_> {
    // Shared claim path; only the actual successful commit returns a receipt.
    pub(super) fn claim_recorded(&mut self, recorder: &mut Recorder) -> Result<Receipt> {
        if !matches!(self.phase, Phase::Prepared) {
            return Err("ticket");
        }
        let start = (|| {
            if !self
                .store
                .unused_create_slot(self.scope, &recorder.delivery_namespace, &self.intent.key)
                .map_err(|_| "storage")?
            {
                return Err("delivery");
            }
            let facts = empty_facts("prepared", &self.prepared.generation, "")?;
            let proposal = recorder.propose(self, &facts, recorder.deadline())?;
            if proposal.0 != "1" || proposal.1 != "dispatch_after_commit" {
                return Err("protocol");
            }
            self.claim(&proposal.2)
        })();
        if start.is_err() {
            self.phase = Phase::Closed;
        }
        start
    }

    /// Own the whole claim -> invoke -> result-commit sequence. No caller-provided
    /// receipt, result, event, phase or transaction is accepted by this path.
    pub fn run_recorded(
        &mut self,
        ticket: &str,
        recorder: &mut Recorder,
        cancel: &AtomicBool,
    ) -> Result<Recorded> {
        if ticket != self.prepared.ticket || !matches!(self.phase, Phase::Prepared) {
            return Err("ticket");
        }
        let claim_receipt = self.claim_recorded(recorder)?;
        let (observation, refused) = match self.execute(ticket, cancel) {
            Ok(seen) => (Some(seen), None),
            Err(code) => (None, Some(code)),
        };
        let terminal = self.persist_observation(recorder, observation.as_ref(), refused);
        let (phase, delivery_receipt, recording_error) = match terminal {
            Ok((phase, receipt)) => (Some(phase), Some(receipt), None),
            Err(code) => (None, None, Some(code)),
        };
        Ok(Recorded {
            claim_receipt,
            delivery_receipt,
            phase,
            observation,
            refused,
            recording_error,
        })
    }

    // Private continuation of run_recorded, not a caller-supplied result API.
    // Result persistence has its own bounded interval. Expired dispatch
    // authority must not erase an observation of work already initiated.
    pub(super) fn persist_observation(
        &mut self,
        recorder: &mut Recorder,
        observation: Option<&Observation>,
        refused: Option<&'static str>,
    ) -> Result<(String, Receipt)> {
        let facts = match observation {
            Some(seen) => worker_facts(seen)?,
            None => empty_facts(
                "refused",
                &self.prepared.generation,
                refused.ok_or("protocol")?,
            )?,
        };
        let deadline = recorder.deadline();
        let (phase, hint, raw) = recorder.propose(self, &facts, deadline)?;
        if hint != "record_after_commit" {
            return Err("protocol");
        }
        let batch = commit_batch(&raw)?;
        validate_delivery(
            &batch,
            &self.intent,
            &self.claim_namespace,
            &recorder.delivery_namespace,
            &self.prepared.generation,
            &phase,
        )?;
        if Instant::now() >= deadline {
            return Err("deadline");
        }
        let receipt = self
            .store
            .commit(self.scope, &batch)
            .map_err(|_| "storage")?;
        Ok((phase, receipt))
    }
}
pub(super) fn validate_delivery(
    batch: &Batch,
    intent: &Check,
    claim_namespace: &str,
    namespace: &str,
    generation: &str,
    phase: &str,
) -> Result<()> {
    if !matches!(phase, "2" | "3" | "4" | "5" | "6")
        || batch.checks != [intent.clone()]
        || batch.writes.len() != 2
    {
        return Err("recording");
    }
    let claim = &batch.writes[0];
    let delivery = &batch.writes[1];
    if claim.namespace != claim_namespace
        || claim.key != intent.key
        || claim.revision != 1
        || delivery.namespace != namespace
        || delivery.key != intent.key
        || delivery.revision != 0
    {
        return Err("recording");
    }
    let sd = fields(claim.value.as_deref().ok_or("recording")?, "SD1\n", 5)?;
    let dr = fields(delivery.value.as_deref().ok_or("recording")?, "DR1\n", 7)?;
    let revision = intent.revision.to_string();
    if sd != [&intent.namespace, &intent.key, &revision, generation, phase] || dr[..5] != sd {
        return Err("recording");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use sigil_worker_bridge::Fault;

    #[test]
    fn bounded_projection_preserves_actual_generation_status_and_utf8_length() {
        let observed = Observation {
            generation: "actual-generation".into(),
            request_may_have_run: true,
            worker_reaped: true,
            fault: None,
            result: Some(json!({"status":"ok","data":{"output_text":"a😀"}})),
        };
        let projected = worker_facts(&observed).unwrap();
        assert_eq!(
            fields(&projected, "WF1\n", 9).unwrap(),
            [
                "observed",
                "actual-generation",
                "1",
                "1",
                "",
                "ok",
                "string",
                "5",
                "a😀"
            ]
        );
    }

    #[test]
    fn projection_does_not_invent_output_or_truncate_oversized_strings() {
        for (result, kind, length) in [
            (json!({"status":"ok"}), "missing", "0"),
            (
                json!({"status":"ok","data":{"output_text":7}}),
                "other",
                "0",
            ),
            (
                json!({"status":"ok","data":{"output_text":"x".repeat(1048577)}}),
                "oversized",
                "1048577",
            ),
        ] {
            let observed = Observation {
                generation: "worker".into(),
                request_may_have_run: true,
                worker_reaped: true,
                fault: None,
                result: Some(result),
            };
            let projected = worker_facts(&observed).unwrap();
            assert_eq!(
                &fields(&projected, "WF1\n", 9).unwrap()[6..],
                [kind, length, ""]
            );
        }
    }

    #[test]
    fn projection_preserves_unconfirmed_cleanup_and_no_result_as_facts_not_policy() {
        let observed = Observation {
            generation: "worker".into(),
            request_may_have_run: true,
            worker_reaped: false,
            fault: Some(Fault::CleanupUnconfirmed),
            result: None,
        };
        let projected = worker_facts(&observed).unwrap();
        assert_eq!(
            &fields(&projected, "WF1\n", 9).unwrap()[2..],
            ["1", "0", "cleanup_unconfirmed", "", "missing", "0", ""]
        );
    }
}
