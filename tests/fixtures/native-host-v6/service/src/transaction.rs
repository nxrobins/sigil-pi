//! Fixed pure transaction producer over actual scoped snapshots. No application
//! phase, account, tenant, operation or recovery decision is interpreted here.
use crate::action::{admit_time, commit_batch};
use crate::policy::{Input, Key};
use crate::{Function, Result, fields, frame, now};
use serde::{Deserialize, Serialize};
use sigil_durable_store::store::{Access, Batch, Receipt, Scope, Store};
use sigil_worker_bridge::Config as WorkerConfig;
use std::collections::{BTreeMap, BTreeSet};
use std::time::{Duration, Instant};

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub worker: WorkerConfig,
    pub marker: String,
    pub values: usize,
    pub inputs: Vec<Input>,
    pub grants: BTreeMap<String, Access>,
}
pub struct Transaction {
    function: Function,
    marker: String,
    count: usize,
    inputs: Vec<Input>,
    scope: Scope,
}
#[derive(Serialize)]
pub struct Applied {
    /// Opaque SIGIL-owned data, never an execution or storage capability.
    pub context: [String; 2],
    /// Present only for this invocation's actual acknowledged commit.
    pub receipt: Option<Receipt>,
}
type Reads = BTreeMap<(String, String), u64>;

impl Transaction {
    /// Admit bound templates before application storage is initialized.
    pub(crate) fn validate_config(config: &Config) -> Result<()> {
        if config.marker.len() != 4
            || !config.marker.as_bytes()[..3]
                .iter()
                .all(u8::is_ascii_alphanumeric)
            || !config.marker.ends_with('\n')
            || config.values > 16
            || config.inputs.is_empty()
            || config.inputs.len() > 32
        {
            return Err("config");
        }
        Store::validate_grants(&config.grants).map_err(|_| "config")?;
        for input in &config.inputs {
            match input {
                Input::WorkerFacts | Input::CredentialFacts | Input::Bundle => {
                    return Err("config");
                }
                Input::Value { index } if *index >= config.values => return Err("config"),
                Input::Literal { value } if value.len() > 16384 => return Err("config"),
                Input::Read { namespace, key }
                    if !matches!(
                        config.grants.get(namespace),
                        Some(Access::Read | Access::ReadWrite)
                    ) || matches!(key, Key::Value { index } if *index >= config.values)
                        || matches!(key, Key::Literal { value } if !crate::policy::literal_key(value)) =>
                {
                    return Err("config");
                }
                _ => {}
            }
        }
        Ok(())
    }

    pub fn new(config: Config, store: &Store) -> Result<Self> {
        Self::validate_config(&config)?;
        Ok(Self {
            function: Function::new(config.worker)?,
            marker: config.marker,
            count: config.values,
            inputs: config.inputs,
            scope: store.scope(config.grants).map_err(|_| "config")?,
        })
    }

    /// Hold the Store exclusively across reads, pure evaluation and exact commit.
    /// Callers supply bounded lookup values, never snapshots, proposals or receipts.
    pub fn apply(
        &mut self,
        store: &mut Store,
        last_clock: &mut u64,
        values: &[String],
    ) -> Result<Applied> {
        if values.len() != self.count || values.iter().any(|v| v.len() > 512) {
            return Err("protocol");
        }
        let deadline = Instant::now() + Duration::from_millis(self.function.timeout_ms);
        admit_time(None, last_clock, deadline, now)?;
        let at = last_clock.to_string();
        let mut parts = Vec::new();
        let mut reads = BTreeMap::new();
        let mut bytes = 4usize;
        for input in &self.inputs {
            let value = match input {
                Input::Literal { value } => value.clone(),
                Input::Value { index } => values[*index].clone(),
                Input::Clock => at.clone(),
                Input::Read { namespace, key } => {
                    let key = match key {
                        Key::Literal { value } => value,
                        Key::Value { index } => &values[*index],
                    };
                    let seen = store
                        .get(&self.scope, namespace, key)
                        .map_err(|_| "storage")?;
                    reads.insert((namespace.clone(), key.clone()), seen.revision);
                    frame(
                        "SR1\n",
                        &[
                            "ok",
                            &seen.revision.to_string(),
                            seen.value.as_deref().unwrap_or(""),
                        ],
                    )?
                }
                Input::WorkerFacts | Input::CredentialFacts | Input::Bundle => {
                    return Err("config");
                }
            };
            bytes = bytes.checked_add(8 + value.len()).ok_or("limit")?;
            if bytes > 4 * 1024 * 1024 {
                return Err("limit");
            }
            parts.push(value);
        }
        let input = frame(
            &self.marker,
            &parts.iter().map(String::as_str).collect::<Vec<_>>(),
        )?;
        let output = self.function.invoke(input, deadline)?;
        let parts = fields(&output, "TX1\n", 3)?;
        if parts[0].len() > 65536 || parts[1].len() > 65536 {
            return Err("limit");
        }
        let context = [parts[0].to_owned(), parts[1].to_owned()];
        admit_time(None, last_clock, deadline, now)?;
        let receipt = if parts[2].is_empty() {
            None
        } else {
            let batch = commit_batch(parts[2])?;
            validate(&batch, &reads)?;
            admit_time(None, last_clock, deadline, now)?;
            Some(store.commit(&self.scope, &batch).map_err(|_| "storage")?)
        };
        Ok(Applied { context, receipt })
    }
}

/// Every mutation/precondition names an actual read and its exact revision, and
/// every actual read remains a precondition. No unobserved or unchecked coordinate.
fn validate(batch: &Batch, reads: &Reads) -> Result<()> {
    if batch.writes.is_empty() {
        return Err("binding");
    }
    let mut covered = BTreeSet::new();
    for (namespace, key, revision) in batch
        .checks
        .iter()
        .map(|c| (&c.namespace, &c.key, c.revision))
        .chain(
            batch
                .writes
                .iter()
                .map(|w| (&w.namespace, &w.key, w.revision)),
        )
    {
        let coordinate = (namespace.clone(), key.clone());
        if reads.get(&coordinate) != Some(&revision) || !covered.insert(coordinate) {
            return Err("binding");
        }
    }
    if covered.len() != reads.len() {
        return Err("binding");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use sigil_durable_store::store::{Check, Mutation};

    fn batch() -> Batch {
        Batch {
            checks: vec![Check {
                namespace: "state".into(),
                key: "conversation".into(),
                revision: 7,
            }],
            writes: vec![Mutation {
                namespace: "output".into(),
                key: "record".into(),
                revision: 0,
                value: Some("opaque".into()),
            }],
        }
    }
    fn reads() -> Reads {
        BTreeMap::from([
            (("state".into(), "conversation".into()), 7),
            (("output".into(), "record".into()), 0),
        ])
    }
    #[test]
    fn exact_observed_coordinates_and_revisions_are_required() {
        assert_eq!(validate(&batch(), &reads()), Ok(()));
        for change in 0..6 {
            let mut b = batch();
            match change {
                0 => b.checks.clear(),
                1 => b.writes.clear(),
                2 => b.writes[0].namespace = "unread".into(),
                3 => b.writes[0].revision = 1,
                4 => b.checks[0].revision = 8,
                _ => b.checks.push(b.checks[0].clone()),
            }
            assert_eq!(validate(&b, &reads()), Err("binding"));
        }
    }
    #[test]
    fn extra_reads_and_duplicate_check_write_coordinates_are_refused() {
        let mut observed = reads();
        observed.insert(("uncovered".into(), "key".into()), 3);
        assert_eq!(validate(&batch(), &observed), Err("binding"));
        let mut b = batch();
        b.writes[0].namespace = "state".into();
        b.writes[0].key = "conversation".into();
        b.writes[0].revision = 7;
        assert_eq!(validate(&b, &reads()), Err("binding"));
    }
}
