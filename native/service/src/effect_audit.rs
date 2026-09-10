//! Native-only coupling of actual claim/delivery production to authenticated
//! publication. SIGIL selects event semantics/redaction; no guest signing API.
use crate::action::{Guard, admit_time};
use crate::audited_transaction::{Config, combined_grants, environment_key, manifest};
use crate::evaluation_audit::{self, Failure, Pending, Projector, Subject, Target};
use crate::{Function, Result, fields, frame, identity, now};
use sha2::{Digest, Sha256};
use sigil_durable_store::store::authenticated_log::AuthenticatedLog;
use sigil_durable_store::store::{Access, Batch, Check, Receipt, Scope, Store};
use sigil_worker_bridge::Bridge;
use std::collections::BTreeMap;
use std::time::{Duration, Instant};
use zeroize::Zeroizing;

pub(crate) struct Admitted {
    config: Config,
    evaluation: Option<Projector>,
    recorder_subject: Option<Subject>,
    key: Zeroizing<Vec<u8>>,
    formatter: Function,
    scope: BTreeMap<String, Access>,
    grants: String,
    producer: String,
    projector: String,
    effect: String,
    context: String,
}

pub(crate) struct EffectAudit {
    formatter: Function,
    evaluation: Option<Projector>,
    recorder_subject: Option<Subject>,
    log: AuthenticatedLog,
    scope: Scope,
    heads: String,
    chain: String,
    grants: String,
    producer: String,
    projector: String,
    effect: String,
    context: String,
}

// Not deserializable or public. These references come only from the actual
// recorder invocation, held native binding and actual prepared/observed facts.
pub(crate) struct Recording<'a> {
    pub intent: &'a Check,
    pub claim_namespace: &'a str,
    pub delivery_namespace: &'a str,
    pub claim_revision: u64,
    pub claim_generation: &'a str,
    pub worker_facts: &'a str,
    pub prepared: Option<&'a str>,
    pub input_sha256: &'a str,
    pub input_bytes: usize,
    pub output: &'a str,
    pub batch: &'a str,
    pub phase: &'a str,
    pub before: u64,
    pub after: u64,
    pub elapsed_ms: u128,
    pub timeout_ms: u64,
}

pub(crate) fn digest(value: &str) -> String {
    format!("{:x}", Sha256::digest(value.as_bytes()))
}

impl Admitted {
    pub(crate) fn new(
        config: Config,
        producer: &crate::claimed::RecorderConfig,
        worker: &Bridge,
        domain: &BTreeMap<String, Access>,
        credential_facts: &str,
    ) -> Result<Self> {
        let scope = combined_grants(domain, &config)?;
        let grants = serde_json::to_string(domain).map_err(|_| "config")?;
        let evaluation = config
            .evaluation
            .clone()
            .map(|worker| Projector::admit(worker, &config.chain, &grants))
            .transpose()?;
        let recorder_subject = evaluation
            .as_ref()
            .map(|_| Subject::new(producer, &producer.worker, domain))
            .transpose()?;
        let producer = manifest(&producer.worker)?;
        let projector = manifest(&config.worker)?;
        let facts = worker.facts().map_err(|_| "config")?;
        let effect = frame(
            "EM1\n",
            &[
                facts.source_sha256,
                facts.runtime_sha256,
                &facts.max_fuel.to_string(),
                &facts.max_timeout_ms.to_string(),
                &serde_json::to_string(facts.net).map_err(|_| "config")?,
                &serde_json::to_string(facts.fs).map_err(|_| "config")?,
                &serde_json::to_string(&facts.secret_names).map_err(|_| "config")?,
            ],
        )?;
        let mut formatter = Function::cached(config.worker.clone())?;
        let key = environment_key(&config.key_env)?;
        let deadline = Instant::now() + Duration::from_millis(formatter.timeout_ms);
        let boot = frame(
            "WB1\n",
            &[
                &config.chain,
                &producer,
                &projector,
                &grants,
                &effect,
                credential_facts,
                &digest(credential_facts),
            ],
        )?;
        let decision = formatter.invoke(boot, deadline)?;
        let decision = fields(&decision, "WB2\n", 2)?;
        if decision[0] != "effect_audit_admitted"
            || decision[1].is_empty()
            || decision[1].len() > 1024
        {
            return Err("config");
        }
        Ok(Self {
            config,
            evaluation,
            recorder_subject,
            key,
            formatter,
            scope,
            grants,
            producer,
            projector,
            effect,
            context: decision[1].into(),
        })
    }

    pub(crate) fn open(self, store: &Store) -> Result<EffectAudit> {
        let log = AuthenticatedLog::new(
            store,
            &self.config.heads,
            &self.config.records,
            self.config.limits,
            &self.key,
        )
        .map_err(|_| "config")?;
        Ok(EffectAudit {
            formatter: self.formatter,
            evaluation: self.evaluation,
            recorder_subject: self.recorder_subject,
            log,
            scope: store.scope(self.scope).map_err(|_| "config")?,
            heads: self.config.heads,
            chain: self.config.chain,
            grants: self.grants,
            producer: self.producer,
            projector: self.projector,
            effect: self.effect,
            context: self.context,
        })
    }
}

impl EffectAudit {
    #[cfg(test)]
    pub(crate) fn fixture(
        store: &mut Store,
        formatter: sigil_worker_bridge::Config,
        mut grants: BTreeMap<String, Access>,
        records: u64,
        fill: bool,
    ) -> Self {
        // Controlled native protocol fixture only, not compiler/event evidence.
        // Match actual Admitted::new: a cached formatter is boot-invoked before
        // an Attempt starts. A cold Fresh formatter here introduces per-event
        // process launch/reaping into a boundary production does not use.
        let mut formatter = Function::cached(formatter).unwrap();
        assert_eq!(
            formatter
                .invoke(
                    "native-audit-fixture-boot".into(),
                    Instant::now() + Duration::from_secs(5),
                )
                .unwrap(),
            "native fixture admitted",
        );
        let encoded = serde_json::to_string(&grants).unwrap();
        grants.insert("audit.heads".into(), Access::ReadWrite);
        grants.insert("audit.entries".into(), Access::ReadWrite);
        let scope = store.scope(grants).unwrap();
        let log = AuthenticatedLog::new(
            store,
            "audit.heads",
            "audit.entries",
            sigil_durable_store::store::authenticated_log::ChainLimits {
                payload_bytes: 1024,
                records,
                bytes: 1_000_000,
            },
            b"native-effect-audit-test-key-0123456789",
        )
        .unwrap();
        let chain = "a".repeat(64);
        if fill {
            log.append(
                store,
                &scope,
                &chain,
                0,
                "capacity fixture",
                &Batch {
                    checks: vec![],
                    writes: vec![],
                },
            )
            .unwrap();
        }
        Self {
            formatter,
            evaluation: None,
            recorder_subject: None,
            log,
            scope,
            heads: "audit.heads".into(),
            chain,
            grants: encoded,
            producer: "native fixture".into(),
            projector: "native fixture".into(),
            effect: "native fixture".into(),
            context: "native fixture".into(),
        }
    }

    pub(crate) fn inspect_owner(&self) -> Result<()> {
        if let Some(evaluation) = &self.evaluation {
            evaluation.inspect_owner()?;
        }
        self.formatter.inspect_owner()
    }

    pub(crate) fn record_evaluation_failure(
        &mut self,
        store: &mut Store,
        failure: Failure,
    ) -> Result<()> {
        evaluation_audit::publish(
            self.evaluation.as_mut(),
            Target {
                log: &self.log,
                scope: &self.scope,
                chain: &self.chain,
                heads: &self.heads,
            },
            store,
            failure,
        )
    }

    pub(crate) fn observes_recorder_evaluations(&self) -> bool {
        self.evaluation.is_some()
    }

    pub(crate) fn record_recorder_failure(
        &mut self,
        store: &mut Store,
        pending: Pending,
        code: &'static str,
    ) -> Result<()> {
        if self.evaluation.is_none() {
            return Ok(());
        }
        let subject = self.recorder_subject.as_ref().ok_or("audit_recording")?;
        let failure = pending.refuse(subject, code);
        self.record_evaluation_failure(store, failure)
    }

    pub(crate) fn inspect_existing(&self, store: &mut Store) -> Result<()> {
        self.log
            .inspect_existing(
                store,
                &self.scope,
                &self.chain,
                None,
                Instant::now() + Duration::from_secs(1),
            )
            .map(|_| ())
            .map_err(|_| "audit_verification")
    }

    pub(crate) fn propose(&mut self, r: Recording<'_>, deadline: Instant) -> Result<String> {
        let wf = fields(r.worker_facts, "WF1\n", 9)?;
        // Only a complete retained output is hashed. Oversized/non-string/missing
        // output remains explicitly unavailable, never the hash of an empty body.
        let output_digest = if wf[6] == "string" {
            digest(wf[8])
        } else {
            String::new()
        };
        let mut observed = wf[..8].to_vec();
        observed.push(&output_digest);
        let observed = frame("WO1\n", &observed)?;
        // Abandoned recovery does not establish the old worker's artifact,
        // selected input, grants or execution budgets from the current profile.
        let (effect, prepared) = if wf[0] == "abandoned" {
            ("", "")
        } else {
            (self.effect.as_str(), r.prepared.ok_or("binding")?)
        };
        let input = frame(
            "WA1\n",
            &[
                &identity()?,
                &self.chain,
                &self.producer,
                &self.projector,
                effect,
                &self.grants,
                &r.intent.namespace,
                r.claim_namespace,
                r.delivery_namespace,
                &r.intent.key,
                &r.intent.revision.to_string(),
                &r.claim_revision.to_string(),
                r.claim_generation,
                wf[1],
                &observed,
                prepared,
                r.input_sha256,
                &r.input_bytes.to_string(),
                &digest(r.output),
                &r.output.len().to_string(),
                &digest(r.batch),
                &r.batch.len().to_string(),
                r.phase,
                &r.before.to_string(),
                &r.after.to_string(),
                &r.elapsed_ms.to_string(),
                &r.timeout_ms.to_string(),
                &self.context,
            ],
        )?;
        let output = self.formatter.invoke(input, deadline)?;
        let parts = fields(&output, "AR1\n", 2)?;
        if parts[0] != "publish" || parts[1].is_empty() {
            return Err("binding");
        }
        Ok(parts[1].into())
    }

    pub(crate) fn commit(
        &self,
        store: &mut Store,
        batch: &Batch,
        payload: &str,
        deadline: Instant,
        guard: Option<(&Guard, &mut u64)>,
    ) -> Result<Receipt> {
        let head = store
            .get(&self.scope, &self.heads, &self.chain)
            .map_err(|_| "storage")?;
        if let Some((guard, clock)) = guard {
            admit_time(Some(guard), clock, deadline, now)?;
        } else if Instant::now() >= deadline {
            return Err("deadline");
        }
        self.log
            .append(
                store,
                &self.scope,
                &self.chain,
                head.revision,
                payload,
                batch,
            )
            .map(|p| p.receipt)
            .map_err(|_| "storage")
    }
}
