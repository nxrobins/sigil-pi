//! Private coupling of actual failed evaluations to a fixed SIGIL projector.
//! No caller-authored observations, signing API, domain writes or effect tickets.
use crate::audited_transaction::manifest;
use crate::effect_audit::digest;
use crate::evaluation::Facts;
use crate::{Function, Result, fields, frame, identity};
use serde::Serialize;
use sigil_durable_store::store::authenticated_log::AuthenticatedLog;
use sigil_durable_store::store::{Access, Batch, Error, Scope, Store};
use sigil_worker_bridge::Config as WorkerConfig;
use std::collections::BTreeMap;
use std::time::{Duration, Instant};

#[derive(Clone)]
pub(crate) struct Subject {
    config: String,
    manifest: String,
    grants: String,
}
impl Subject {
    pub(crate) fn new(
        config: &impl Serialize,
        worker: &WorkerConfig,
        grants: &BTreeMap<String, Access>,
    ) -> Result<Self> {
        Ok(Self {
            config: digest(&serde_json::to_string(config).map_err(|_| "config")?),
            manifest: manifest(worker)?,
            grants: serde_json::to_string(grants).map_err(|_| "config")?,
        })
    }
}

// Created only after bounded lookup validation, before the actual invocation.
// Stores no lookup values, inputs, output or diagnostic bodies.
pub(crate) struct Lookup {
    digest: String,
    count: usize,
}
impl Lookup {
    pub(crate) fn new(values: &[String]) -> Result<Self> {
        if values.len() > 16 || values.iter().any(|v| v.len() > 512) {
            return Err("protocol");
        }
        Ok(Self {
            digest: digest(&frame(
                "EL1\n",
                &values.iter().map(String::as_str).collect::<Vec<_>>(),
            )?),
            count: values.len(),
        })
    }
}
pub(crate) struct Pending {
    pub(crate) facts: Facts,
    lookup: Lookup,
    deadline: Instant,
}
impl Pending {
    pub(crate) fn new(facts: Facts, lookup: Lookup, deadline: Instant) -> Self {
        Self {
            facts,
            lookup,
            deadline,
        }
    }

    pub(crate) fn refuse(self, subject: &Subject, code: &'static str) -> Failure {
        Failure {
            subject: subject.clone(),
            pending: self,
            code,
        }
    }
}

// Neither Clone, Deserialize nor public: consuming this retires this observation.
pub(crate) struct Failure {
    subject: Subject,
    pending: Pending,
    code: &'static str,
}
pub(crate) struct Projector {
    function: Function,
    manifest: String,
}
impl Projector {
    pub(crate) fn admit(config: WorkerConfig, chain: &str, grants: &str) -> Result<Self> {
        let manifest = manifest(&config)?;
        let mut function = Function::cached(config)?;
        let input = frame("EB1\n", &[chain, &manifest, grants])?;
        let deadline = Instant::now() + Duration::from_millis(function.timeout_ms);
        if function.invoke(input, deadline)? != "evaluation_audit_admitted" {
            return Err("config");
        }
        Ok(Self { function, manifest })
    }

    pub(crate) fn inspect_owner(&self) -> Result<()> {
        self.function.inspect_owner()
    }

    fn propose(&mut self, chain: &str, failure: &Failure) -> Result<String> {
        let input = frame(
            "EA1\n",
            &[
                &identity()?,
                chain,
                &failure.subject.config,
                &failure.subject.manifest,
                &self.manifest,
                &failure.subject.grants,
                &failure.pending.lookup.digest,
                &failure.pending.lookup.count.to_string(),
                failure.code,
                &serde_json::to_string(&failure.pending.facts).map_err(|_| "protocol")?,
            ],
        )?;
        let output = self.function.invoke(input, failure.pending.deadline)?;
        let parts = fields(&output, "AR1\n", 2)?;
        if parts[0] != "publish" || parts[1].is_empty() {
            return Err("binding");
        }
        Ok(parts[1].into())
    }
}

pub(crate) struct Target<'a> {
    pub log: &'a AuthenticatedLog,
    pub scope: &'a Scope,
    pub chain: &'a str,
    pub heads: &'a str,
}

/// v1 has no failure publisher. v2 never extends the original action deadline or
/// falls back to a domain commit. An uncertain append is not a success receipt,
/// nor proof that no record exists; callers must not retry it automatically.
pub(crate) fn publish(
    projector: Option<&mut Projector>,
    target: Target<'_>,
    store: &mut Store,
    failure: Failure,
) -> Result<()> {
    let Some(projector) = projector else {
        return Ok(());
    };
    let deadline = failure.pending.deadline;
    if Instant::now() >= deadline {
        return Err("audit_recording");
    }
    let payload = projector
        .propose(target.chain, &failure)
        .map_err(|_| "audit_recording")?;
    let head = store
        .get(target.scope, target.heads, target.chain)
        .map_err(|_| "audit_recording")?;
    if Instant::now() >= deadline {
        return Err("audit_recording");
    }
    target
        .log
        .append(
            store,
            target.scope,
            target.chain,
            head.revision,
            &payload,
            &Batch {
                checks: vec![],
                writes: vec![],
            },
        )
        .map(|_| ())
        .map_err(|error| match error {
            Error::CommitUncertain => "audit_commit_uncertain",
            _ => "audit_recording",
        })
}

#[cfg(test)]
mod tests;
