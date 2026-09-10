//! Opt-in atomic coupling of a fixed pure SIGIL transaction and fixed pure SIGIL
//! audit projection. This records state-transition executions, NOT model/tool
//! effects or every guest invocation. v2 adds failure journaling. No arbitrary signing.
use crate::action::admit_time;
use crate::evaluation_audit::{self, Projector, Target};
use crate::transaction::{self, Transaction};
use crate::{Function, Result, fields, frame, identity, now};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sigil_durable_store::store::authenticated_log::{AuthenticatedLog, ChainLimits, Checkpoint};
use sigil_durable_store::store::{Access, Error, Receipt, Scope, Store};
use sigil_worker_bridge::Config as WorkerConfig;
use std::collections::BTreeMap;
use std::os::unix::ffi::OsStringExt;
use std::time::{Duration, Instant};
use zeroize::Zeroizing;

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub version: u32,
    pub worker: WorkerConfig,
    #[serde(
        default,
        deserialize_with = "explicit_evaluation",
        skip_serializing_if = "Option::is_none"
    )]
    pub evaluation: Option<WorkerConfig>,
    pub key_env: String,
    pub chain: String,
    pub heads: String,
    pub records: String,
    pub limits: ChainLimits,
}

fn explicit_evaluation<'de, D>(input: D) -> std::result::Result<Option<WorkerConfig>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    WorkerConfig::deserialize(input).map(Some)
}

pub struct AuditedTransaction {
    transaction: Transaction,
    evaluation: Option<Projector>,
    formatter: Function,
    log: AuthenticatedLog,
    scope: Scope,
    chain: String,
    heads: String,
    // Only admitted, non-secret metadata. No paths, key bytes, input values,
    // stored records, execution output, or caller-supplied observation objects.
    producer: String,
    projector: String,
    grants: String,
}

/// Native-only bootstrap state; source bytes and key are retained, never
/// serialized or reloaded from mutable deployment inputs after state creation.
pub(crate) struct Admitted {
    transaction: transaction::Admitted,
    evaluation: Option<Projector>,
    formatter: Function,
    key: Zeroizing<Vec<u8>>,
    scope: BTreeMap<String, Access>,
    chain: String,
    heads: String,
    records: String,
    limits: ChainLimits,
    producer: String,
    projector: String,
    grants: String,
}

#[derive(Serialize)]
pub struct Applied {
    pub context: [String; 2],
    pub receipt: Option<Receipt>,
    pub checkpoint: Option<Value>,
}

fn checkpoint(c: &Checkpoint) -> Value {
    json!({"chain":c.chain,"head_revision":c.head_revision,"count":c.count,
        "bytes":c.bytes,"tip":c.tip})
}

fn storage(error: Error) -> &'static str {
    match error {
        Error::CommitUncertain => "commit_uncertain",
        _ => "storage",
    }
}

pub(crate) fn environment_key(name: &str) -> Result<Zeroizing<Vec<u8>>> {
    if name.is_empty()
        || name.len() > 128
        || !name
            .bytes()
            .enumerate()
            .all(|(i, b)| b == b'_' || b.is_ascii_alphabetic() || (i > 0 && b.is_ascii_digit()))
    {
        return Err("config");
    }
    let key = Zeroizing::new(std::env::var_os(name).ok_or("config")?.into_vec());
    if !(32..=1024).contains(&key.len()) {
        return Err("config");
    }
    Ok(key)
}

pub(crate) fn combined_grants(
    domain: &BTreeMap<String, Access>,
    config: &Config,
) -> Result<BTreeMap<String, Access>> {
    if !matches!(
        (config.version, config.evaluation.is_some()),
        (1, false) | (2, true)
    ) || config.heads == config.records
        || domain.contains_key(&config.heads)
        || domain.contains_key(&config.records)
    {
        return Err("config");
    }
    crate::digest_bytes(&config.chain)?;
    let mut grants = domain.clone();
    // These privileges stay in this native coupling, never the producer's scope.
    // append itself permits only create-only entries plus authenticated head CAS.
    grants.insert(config.heads.clone(), Access::ReadWrite);
    grants.insert(config.records.clone(), Access::ReadWrite);
    Store::validate_grants(&grants).map_err(|_| "config")?;
    Ok(grants)
}

pub(crate) fn manifest(config: &WorkerConfig) -> Result<String> {
    Function::manifest(config)?;
    frame(
        "TM1\n",
        &[
            &config.source_sha256,
            &config.runtime_sha256,
            &config.max_fuel.to_string(),
            &config.max_timeout_ms.min(30_000).to_string(),
        ],
    )
}

impl Admitted {
    pub(crate) fn new(transaction: transaction::Config, config: Config) -> Result<Self> {
        Transaction::validate_config(&transaction)?;
        let grants = serde_json::to_string(&transaction.grants).map_err(|_| "config")?;
        let scope = combined_grants(&transaction.grants, &config)?;
        let producer = manifest(&transaction.worker)?;
        let projector = manifest(&config.worker)?;
        let evaluation = config
            .evaluation
            .map(|worker| Projector::admit(worker, &config.chain, &grants))
            .transpose()?;
        let transaction = Transaction::admit(transaction)?;
        let formatter = Function::cached(config.worker)?;
        let key = environment_key(&config.key_env)?;
        Ok(Self {
            transaction,
            evaluation,
            formatter,
            key,
            scope,
            chain: config.chain,
            heads: config.heads,
            records: config.records,
            limits: config.limits,
            producer,
            projector,
            grants,
        })
    }

    /// The automatic embedding verifies its fixed SIGIL admission policy before
    /// opening application state. AB1 contains real configured metadata, not a
    /// fabricated TA1 execution, signature request, or mutable state snapshot.
    pub(crate) fn admit_policy(&mut self) -> Result<()> {
        let deadline = Instant::now() + Duration::from_millis(self.formatter.timeout_ms);
        let input = frame(
            "AB1\n",
            &[&self.chain, &self.producer, &self.projector, &self.grants],
        )?;
        if self.formatter.invoke(input, deadline)? != "transaction_audit_admitted" {
            return Err("config");
        }
        Ok(())
    }

    pub(crate) fn open(self, store: &Store) -> Result<AuditedTransaction> {
        let log = AuthenticatedLog::new(store, &self.heads, &self.records, self.limits, &self.key)
            .map_err(|_| "config")?;
        Ok(AuditedTransaction {
            transaction: self.transaction.open(store)?,
            evaluation: self.evaluation,
            formatter: self.formatter,
            log,
            scope: store.scope(self.scope).map_err(|_| "config")?,
            chain: self.chain,
            heads: self.heads,
            producer: self.producer,
            projector: self.projector,
            grants: self.grants,
        })
    }
}

impl AuditedTransaction {
    pub fn new(transaction: transaction::Config, config: Config, store: &Store) -> Result<Self> {
        Admitted::new(transaction, config)?.open(store)
    }

    pub(crate) fn inspect_owner(&self) -> Result<()> {
        self.transaction.inspect_owner()?;
        if let Some(evaluation) = &self.evaluation {
            evaluation.inspect_owner()?;
        }
        self.formatter.inspect_owner()
    }

    /// Native startup integrity check, not an application readiness decision or
    /// independent freshness assertion. Clean absence stays uninitialized.
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

    /// One exclusive Store borrow spans actual reads, producer execution, SIGIL
    /// audit projection, and one original atomic Store commit. No fallback to an
    /// unaudited commit, retry, replacement chain, or fabricated receipt.
    pub fn apply(
        &mut self,
        store: &mut Store,
        clock: &mut u64,
        values: &[String],
    ) -> Result<Applied> {
        let id = identity()?;
        let plan = match self.transaction.prepare(store, clock, values) {
            Ok(plan) => plan,
            Err(code) => {
                if let Some(failure) = self.transaction.take_failure(code) {
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
                    )?;
                }
                return Err(code);
            }
        };
        let mut facts = vec![
            id,
            self.chain.clone(),
            self.producer.clone(),
            self.projector.clone(),
            self.grants.clone(),
        ];
        facts.extend(plan.facts);
        let request = frame(
            "TA1\n",
            &facts.iter().map(String::as_str).collect::<Vec<_>>(),
        )?;
        let output = self.formatter.invoke(request, plan.deadline)?;
        let fields = fields(&output, "AR1\n", 2)?;
        admit_time(None, clock, plan.deadline, now)?;
        match plan.batch {
            None if fields == ["none", ""] => Ok(Applied {
                context: plan.context,
                receipt: None,
                checkpoint: None,
            }),
            Some(batch) if fields[0] == "publish" && !fields[1].is_empty() => {
                let head = store
                    .get(&self.scope, &self.heads, &self.chain)
                    .map_err(storage)?;
                admit_time(None, clock, plan.deadline, now)?;
                let published = self
                    .log
                    .append(
                        store,
                        &self.scope,
                        &self.chain,
                        head.revision,
                        fields[1],
                        &batch,
                    )
                    .map_err(storage)?;
                Ok(Applied {
                    context: plan.context,
                    receipt: Some(published.receipt),
                    checkpoint: Some(checkpoint(&published.checkpoint)),
                })
            }
            _ => Err("binding"),
        }
    }

    /// Full-chain integrity, not completeness of the application's audit inventory
    /// or freshness. Trusted embeddings can retain an independent latest anchor.
    pub fn verify(&self, store: &mut Store, expected: Option<&Checkpoint>) -> Result<Value> {
        self.log
            .verify(
                store,
                &self.scope,
                &self.chain,
                expected,
                Instant::now() + Duration::from_secs(1),
            )
            .map(|c| checkpoint(&c))
            .map_err(|_| "audit_verification")
    }
}
