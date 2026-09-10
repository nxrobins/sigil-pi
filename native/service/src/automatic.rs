//! Bounded interpreter for a fixed SIGIL coordinator and scoped mechanism aliases.
//! No application records, phases, model/tool names or scheduling policy are parsed.
use crate::audited_transaction::{self, AuditedTransaction};
use crate::claimed::{Binding, OwnedWorker, Recorder, RecorderConfig};
use crate::effect_audit;
use crate::policy::{self, Policy};
use crate::transaction::{self, Transaction};
use crate::{CredentialConfig, Function, Result, fields, frame, now};
use serde::{Deserialize, Serialize};
use sigil_durable_store::store::{Access, Limits, Scope, Store};
use sigil_worker_bridge::{Bridge, Config as WorkerConfig, strict};
use std::collections::{BTreeMap, BTreeSet};
use std::time::{Duration, Instant};

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub participants: Vec<ParticipantConfig>,
}
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ParticipantConfig {
    pub credential_sha256: String,
    pub worker: WorkerConfig,
    pub binding: String,
    pub claim_namespace: String,
    pub delivery_namespace: String,
    pub read_grants: BTreeMap<String, Access>,
    pub effects: BTreeMap<String, EffectConfig>,
    pub transactions: BTreeMap<String, transaction::Config>,
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "explicit_transaction_audit"
    )]
    pub transaction_audit: Option<audited_transaction::Config>,
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "explicit_transaction_audit"
    )]
    pub effect_audit: Option<audited_transaction::Config>,
}
fn explicit_transaction_audit<'de, D: serde::Deserializer<'de>>(
    input: D,
) -> std::result::Result<Option<audited_transaction::Config>, D::Error> {
    audited_transaction::Config::deserialize(input).map(Some)
}
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EffectConfig {
    pub worker: WorkerConfig,
    pub policy: policy::Config,
    pub recorder: RecorderConfig,
    pub grants: BTreeMap<String, Access>,
}
pub(crate) struct Admitted {
    rows: Vec<AdmittedRow>,
}
struct AdmittedRow {
    config: ParticipantConfig,
    function: Function,
    facts: String,
    transactions: BTreeMap<String, AdmittedTransaction>,
    effects: BTreeMap<String, AdmittedEffect>,
}
struct AdmittedEffect {
    worker: Bridge,
    recorder: Recorder,
    policy: policy::Config,
    grants: BTreeMap<String, Access>,
    audit: Option<effect_audit::Admitted>,
}
enum AdmittedTransaction {
    Plain(Box<transaction::Admitted>),
    Audited(Box<audited_transaction::Admitted>),
}
enum TransactionMechanism {
    Plain(Box<Transaction>),
    Audited(Box<AuditedTransaction>),
}
impl AdmittedTransaction {
    fn open(self, store: &Store) -> Result<TransactionMechanism> {
        match self {
            Self::Plain(transaction) => Ok(TransactionMechanism::Plain(Box::new(
                transaction.open(store)?,
            ))),
            Self::Audited(transaction) => Ok(TransactionMechanism::Audited(Box::new(
                transaction.open(store)?,
            ))),
        }
    }
}
impl TransactionMechanism {
    fn inspect_owner(&self) -> Result<()> {
        match self {
            Self::Plain(transaction) => transaction.inspect_owner(),
            Self::Audited(transaction) => transaction.inspect_owner(),
        }
    }
    fn apply(
        &mut self,
        store: &mut Store,
        clock: &mut u64,
        values: &[String],
    ) -> Result<transaction::Applied> {
        match self {
            Self::Plain(transaction) => transaction.apply(store, clock, values),
            Self::Audited(transaction) => {
                let applied = transaction.apply(store, clock, values)?;
                // Preserve CL1's existing context/receipt observation. This is
                // not an independent checkpoint-retention or verification API.
                Ok(transaction::Applied {
                    context: applied.context,
                    receipt: applied.receipt,
                })
            }
        }
    }
}
struct Effect {
    lane: OwnedWorker,
    policy: Policy,
}
struct Participant {
    function: Function,
    facts: String,
    binding: String,
    claim_namespace: String,
    delivery_namespace: String,
    scope: Scope,
    reads: String,
    effects: BTreeMap<String, Effect>,
    transactions: BTreeMap<String, TransactionMechanism>,
    transaction_names: String,
    continuation: String,
    stage: String,
    observation: String,
    due: Instant,
}
pub(crate) struct Automatic {
    rows: Vec<Participant>,
    bundle: String,
    cursor: usize,
}

fn names<T>(map: &BTreeMap<String, T>) -> Result<String> {
    serde_json::to_string(&map.keys().collect::<Vec<_>>()).map_err(|_| "config")
}
fn alias(v: &str) -> bool {
    !v.is_empty() && v.len() <= 64 && v.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
}
fn subset(grants: &BTreeMap<String, Access>, allowed: &BTreeMap<String, Access>) -> bool {
    grants.iter().all(|(ns, access)| {
        matches!(
            (access, allowed.get(ns)),
            (Access::Read, Some(Access::Read | Access::ReadWrite))
                | (Access::ReadWrite, Some(Access::ReadWrite))
                | (
                    Access::CreateOnly,
                    Some(Access::CreateOnly | Access::ReadWrite)
                )
        )
    })
}
fn envelope(facts: &str, bundle: &str, config: &ParticipantConfig, stage: &str) -> Result<String> {
    frame(
        "CL1\n",
        &[
            facts,
            bundle,
            &config.binding,
            &now()?.to_string(),
            stage,
            "",
            "",
            &serde_json::to_string(
                &config
                    .effects
                    .keys()
                    .map(|k| frame("AF1\n", &[k, "", ""]))
                    .collect::<Result<Vec<_>>>()?,
            )
            .map_err(|_| "config")?,
            &names(&config.transactions)?,
            &names(&config.read_grants)?,
            &config.claim_namespace,
            &config.delivery_namespace,
        ],
    )
}

impl Admitted {
    /// Scope delegation and grantless coordinator admission happen before opening
    /// application storage. The full serialized configuration is bundle-bound.
    pub(crate) fn new(
        config: Config,
        credentials: &[CredentialConfig],
        bundle: &str,
        limits: &Limits,
    ) -> Result<Self> {
        if config.participants.is_empty()
            || config.participants.len() != credentials.len()
            || config
                .participants
                .iter()
                .map(|p| p.effects.len())
                .sum::<usize>()
                > 16
        {
            return Err("config");
        }
        let all_domain: BTreeSet<_> = credentials.iter().flat_map(|r| r.grants.keys()).collect();
        let mut owners = BTreeSet::new();
        let mut reserved = BTreeSet::new();
        let mut rows = Vec::new();
        for mut config in config.participants {
            let credential = credentials
                .iter()
                .find(|c| c.sha256 == config.credential_sha256)
                .ok_or("config")?;
            if !owners.insert(config.credential_sha256.clone())
                || config.binding.len() > 16384
                || config.effects.is_empty()
                || config.transactions.is_empty()
                || config.transactions.len() > 8
                || config
                    .effects
                    .keys()
                    .chain(config.transactions.keys())
                    .any(|k| !alias(k))
                || config.read_grants.values().any(|v| *v != Access::Read)
            {
                return Err("config");
            }
            for ns in [&config.claim_namespace, &config.delivery_namespace] {
                if all_domain.contains(ns) || !reserved.insert(ns.clone()) {
                    return Err("config");
                }
            }
            for audit in [&config.transaction_audit, &config.effect_audit]
                .into_iter()
                .flatten()
            {
                audit.limits.validate(limits).map_err(|_| "config")?;
                for ns in [&audit.heads, &audit.records] {
                    if all_domain.contains(ns) || !reserved.insert(ns.clone()) {
                        return Err("config");
                    }
                }
            }
            let mut permitted_reads = credential.grants.clone();
            permitted_reads.insert(config.claim_namespace.clone(), Access::Read);
            permitted_reads.insert(config.delivery_namespace.clone(), Access::Read);
            Store::validate_grants(&permitted_reads).map_err(|_| "config")?;
            if !subset(&config.read_grants, &permitted_reads) {
                return Err("config");
            }
            for (name, effect) in &mut config.effects {
                if effect.policy.alias != *name
                    || effect.policy.claim_namespace != config.claim_namespace
                    || effect.recorder.delivery_namespace != config.delivery_namespace
                    || effect.grants.get(&config.claim_namespace) != Some(&Access::ReadWrite)
                    || effect.grants.get(&config.delivery_namespace) != Some(&Access::CreateOnly)
                    || !subset(&effect.policy.read_grants, &credential.grants)
                {
                    return Err("config");
                }
                // The service must supply current authority and actual worker
                // facts, not accept literal snapshots baked into a deployment.
                if effect
                    .policy
                    .inputs
                    .iter()
                    .filter(|v| matches!(v, policy::Input::CredentialFacts))
                    .count()
                    != 1
                    || effect
                        .policy
                        .inputs
                        .iter()
                        .filter(|v| matches!(v, policy::Input::Bundle))
                        .count()
                        != 1
                    || effect
                        .policy
                        .inputs
                        .iter()
                        .filter(|v| matches!(v, policy::Input::WorkerFacts))
                        .count()
                        != 1
                {
                    return Err("config");
                }
                policy::bind_context(&mut effect.policy.inputs, &credential.facts, bundle);
                Policy::validate_config(&effect.policy)?;
                Store::validate_grants(&effect.grants).map_err(|_| "config")?;
                for (ns, access) in &effect.grants {
                    if ns != &config.claim_namespace
                        && ns != &config.delivery_namespace
                        && (*access != Access::Read || !permitted_reads.contains_key(ns))
                    {
                        return Err("config");
                    }
                }
                Function::new(effect.policy.worker.clone())?;
            }
            for transaction in config.transactions.values_mut() {
                let mut allowed = credential.grants.clone();
                allowed.insert(config.claim_namespace.clone(), Access::Read);
                allowed.insert(config.delivery_namespace.clone(), Access::Read);
                if !subset(&transaction.grants, &allowed) {
                    return Err("config");
                }
                policy::bind_context(&mut transaction.inputs, &credential.facts, bundle);
                Transaction::validate_config(transaction)?;
            }
            let mut function = Function::cached(config.worker.clone())?;
            let deadline = Instant::now() + Duration::from_millis(function.timeout_ms);
            let output = function.invoke(
                envelope(&credential.facts, bundle, &config, "boot")?,
                deadline,
            )?;
            if fields(&output, "LC1\n", 6)? != ["ready", "", "", "", "", "0"] {
                return Err("config");
            }
            let effects = std::mem::take(&mut config.effects)
                .into_iter()
                .map(|(name, effect)| {
                    let worker = Bridge::new(effect.worker).map_err(|_| "config")?;
                    let audit = config
                        .effect_audit
                        .as_ref()
                        .map(|audit| {
                            effect_audit::Admitted::new(
                                audit.clone(),
                                &effect.recorder,
                                &worker,
                                &effect.grants,
                                &credential.facts,
                            )
                        })
                        .transpose()?;
                    Ok((
                        name,
                        AdmittedEffect {
                            worker,
                            recorder: Recorder::new(effect.recorder)?,
                            policy: effect.policy,
                            grants: effect.grants,
                            audit,
                        },
                    ))
                })
                .collect::<Result<BTreeMap<_, _>>>()?;
            let transactions = std::mem::take(&mut config.transactions)
                .into_iter()
                .map(|(name, transaction)| {
                    let admitted = match &config.transaction_audit {
                        Some(audit) => {
                            let mut admitted =
                                audited_transaction::Admitted::new(transaction, audit.clone())?;
                            admitted.admit_policy()?;
                            AdmittedTransaction::Audited(Box::new(admitted))
                        }
                        None => {
                            AdmittedTransaction::Plain(Box::new(Transaction::admit(transaction)?))
                        }
                    };
                    Ok((name, admitted))
                })
                .collect::<Result<BTreeMap<_, _>>>()?;
            rows.push(AdmittedRow {
                config,
                function,
                facts: credential.facts.clone(),
                transactions,
                effects,
            });
        }
        Ok(Self { rows })
    }
    pub(crate) fn open(self, store: &mut Store, bundle: String) -> Result<Automatic> {
        let mut rows = Vec::new();
        for admitted in self.rows {
            let config = admitted.config;
            let reads = names(&config.read_grants)?;
            let transaction_names = names(&admitted.transactions)?;
            let mut effects = BTreeMap::new();
            let mut inspected_effect_audit = false;
            for (name, mut effect) in admitted.effects {
                if let Some(audit) = effect.audit {
                    let audit = audit.open(store)?;
                    if !inspected_effect_audit {
                        audit.inspect_existing(store)?;
                        inspected_effect_audit = true;
                    }
                    effect.recorder.attach_audit(audit);
                }
                let lane = OwnedWorker::new(
                    effect.worker,
                    effect.recorder,
                    store.scope(effect.grants).map_err(|_| "config")?,
                );
                effects.insert(
                    name,
                    Effect {
                        lane,
                        policy: Policy::new(effect.policy, store)?,
                    },
                );
            }
            let mut transactions = BTreeMap::new();
            let mut inspected_audit = false;
            for (name, admitted) in admitted.transactions {
                let transaction = admitted.open(store)?;
                if !inspected_audit && let TransactionMechanism::Audited(audit) = &transaction {
                    // All this participant's transactions bind the same admitted
                    // chain/key/namespaces/limits. Inspect it once, before any
                    // coordinator tick or effect dispatch can run.
                    audit.inspect_existing(store)?;
                    inspected_audit = true;
                }
                transactions.insert(name, transaction);
            }
            rows.push(Participant {
                function: admitted.function,
                facts: admitted.facts,
                binding: config.binding,
                claim_namespace: config.claim_namespace,
                delivery_namespace: config.delivery_namespace,
                scope: store.scope(config.read_grants).map_err(|_| "config")?,
                reads,
                effects,
                transactions,
                transaction_names,
                continuation: String::new(),
                stage: "init".into(),
                observation: String::new(),
                due: Instant::now(),
            });
        }
        Ok(Automatic {
            rows,
            bundle,
            cursor: 0,
        })
    }
}

impl Automatic {
    pub(crate) fn inspect_owners(&self) -> Result<usize> {
        let mut in_flight = 0;
        for row in &self.rows {
            row.function.inspect_owner()?;
            for effect in row.effects.values() {
                effect.policy.inspect_owner()?;
                in_flight += effect.lane.inspect_owners()?;
            }
            for transaction in row.transactions.values() {
                transaction.inspect_owner()?;
            }
        }
        Ok(in_flight)
    }

    /// One bounded coordinator instruction per host turn. Rotation services
    /// eligible continuations; SIGIL selects all work, lookups, effects and waits.
    pub(crate) fn tick(&mut self, store: &mut Store, clock: &mut u64) -> Result<()> {
        for _ in 0..self.rows.len() {
            let index = self.cursor;
            self.cursor = (self.cursor + 1) % self.rows.len();
            let row = &mut self.rows[index];
            if Instant::now() < row.due {
                continue;
            }
            return row.step(store, clock, &self.bundle);
        }
        Ok(())
    }
}
impl Participant {
    fn step(&mut self, store: &mut Store, clock: &mut u64, bundle: &str) -> Result<()> {
        let result = self.instruction(store, clock, bundle);
        if let Err(code) = result {
            self.stage = "error".into();
            self.observation = code.into();
            // Independent host backoff after a failed mechanism, not retry of an
            // effect. The next SIGIL invocation decides how to interpret the error.
            self.due = Instant::now() + Duration::from_millis(100);
        }
        result
    }
    fn instruction(&mut self, store: &mut Store, clock: &mut u64, bundle: &str) -> Result<()> {
        let deadline = Instant::now() + Duration::from_millis(self.function.timeout_ms);
        crate::action::admit_time(None, clock, deadline, now)?;
        let active = self
            .effects
            .iter()
            .map(|(alias, e)| {
                frame(
                    "AF1\n",
                    &[
                        alias,
                        e.lane.generation().unwrap_or(""),
                        e.lane.context().unwrap_or(""),
                    ],
                )
            })
            .collect::<Result<Vec<_>>>()?;
        let input = frame(
            "CL1\n",
            &[
                &self.facts,
                bundle,
                &self.binding,
                &clock.to_string(),
                &self.stage,
                &self.observation,
                &self.continuation,
                &serde_json::to_string(&active).map_err(|_| "protocol")?,
                &self.transaction_names,
                &self.reads,
                &self.claim_namespace,
                &self.delivery_namespace,
            ],
        )?;
        let output = self.function.invoke(input, deadline)?;
        let c = fields(&output, "LC1\n", 6)?;
        if c[4].len() > 65536
            || c[5].is_empty()
            || c[5].len() > 6
            || !c[5].bytes().all(|b| b.is_ascii_digit())
        {
            return Err("protocol");
        }
        let delay = c[5].parse::<u64>().map_err(|_| "protocol")?;
        if delay > 300000 {
            return Err("limit");
        }
        self.continuation = c[4].into();
        crate::action::admit_time(None, clock, deadline, now)?;
        let value = match c[0] {
            "yield" if c[1].is_empty() && c[2].is_empty() && c[3].is_empty() => String::new(),
            "read" if c[3].is_empty() => {
                let r = store.get(&self.scope, c[1], c[2]).map_err(|_| "storage")?;
                frame(
                    "SR1\n",
                    &[
                        "ok",
                        &r.revision.to_string(),
                        r.value.as_deref().unwrap_or(""),
                    ],
                )?
            }
            "keys" => {
                let limit = c[3].parse::<usize>().map_err(|_| "protocol")?;
                let page = store
                    .keys(
                        &self.scope,
                        c[1],
                        if c[2].is_empty() { None } else { Some(c[2]) },
                        limit,
                    )
                    .map_err(|_| "storage")?;
                frame(
                    "KP1\n",
                    &[
                        &serde_json::to_string(&page.keys).map_err(|_| "protocol")?,
                        page.next.as_deref().unwrap_or(""),
                    ],
                )?
            }
            "start" if c[3].is_empty() => {
                let values = lookup_values(c[2])?;
                let e = self.effects.get_mut(c[1]).ok_or("capability")?;
                serde_json::to_string(&e.lane.start(&mut e.policy, store, clock, &values)?)
                    .map_err(|_| "protocol")?
            }
            "poll" if c[3].is_empty() => {
                let e = self.effects.get_mut(c[1]).ok_or("capability")?;
                if e.lane.generation() != Some(c[2]) {
                    return Err("ticket");
                }
                serde_json::to_string(&e.lane.poll(store, clock)?).map_err(|_| "protocol")?
            }
            "cancel" if c[3].is_empty() => {
                let e = self.effects.get(c[1]).ok_or("capability")?;
                if e.lane.generation() != Some(c[2]) {
                    return Err("ticket");
                }
                serde_json::to_string(&e.lane.request_cancel()).map_err(|_| "protocol")?
            }
            "recover" if c[3].is_empty() => {
                let values = lookup_values(c[2])?;
                if values.len() != 2 {
                    return Err("protocol");
                }
                if self
                    .effects
                    .values()
                    .any(|e| e.lane.active_coordinate() == Some((&values[0], &values[1])))
                {
                    return Err("busy");
                }
                let e = self.effects.get_mut(c[1]).ok_or("capability")?;
                serde_json::to_string(&e.lane.recover(
                    store,
                    Binding {
                        intent_namespace: values[0].clone(),
                        claim_namespace: self.claim_namespace.clone(),
                        key: values[1].clone(),
                    },
                )?)
                .map_err(|_| "protocol")?
            }
            "transaction" if c[3].is_empty() => {
                let values = lookup_values(c[2])?;
                serde_json::to_string(
                    &self
                        .transactions
                        .get_mut(c[1])
                        .ok_or("capability")?
                        .apply(store, clock, &values)?,
                )
                .map_err(|_| "protocol")?
            }
            _ => return Err("protocol"),
        };
        self.stage = c[0].into();
        self.observation = value;
        self.due = Instant::now() + Duration::from_millis(delay);
        Ok(())
    }
}
fn lookup_values(raw: &str) -> Result<Vec<String>> {
    if raw.len() > 65536 {
        return Err("limit");
    }
    let values: Vec<String> =
        serde_json::from_value(strict::parse(raw.as_bytes()).map_err(|_| "protocol")?)
            .map_err(|_| "protocol")?;
    if values.len() > 16 || values.iter().any(|v| v.len() > 512) {
        return Err("limit");
    }
    Ok(values)
}
