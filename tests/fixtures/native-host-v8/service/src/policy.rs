//! Bind a fixed pure SIGIL dispatch policy to actual host facts, scoped reads,
//! and one fixed effect worker. No application record or authority is parsed here.
//! Store ownership stays exclusive until the returned durable attempt is dropped.
use crate::action::admit_time;
use crate::claimed::{Attempt, Binding, Execution};
use crate::{Function, Result, fields, frame, now};
use serde::{Deserialize, Serialize};
use sigil_durable_store::store::{Access, Scope, Store};
use sigil_worker_bridge::{Bridge, Config as WorkerConfig};
use std::collections::BTreeMap;
use std::time::{Duration, Instant};

#[derive(Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Key {
    Literal { value: String },
    Value { index: usize },
}
#[derive(Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Input {
    Literal { value: String },
    Value { index: usize },
    Clock,
    Read { namespace: String, key: Key },
    WorkerFacts,
    CredentialFacts,
    Bundle,
}
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub worker: WorkerConfig,
    pub marker: String,
    pub values: usize,
    pub inputs: Vec<Input>,
    pub read_grants: BTreeMap<String, Access>,
    pub alias: String,
    pub binding: String,
    pub claim_namespace: String,
}
pub struct Policy {
    function: Function,
    marker: String,
    count: usize,
    inputs: Vec<Input>,
    scope: Scope,
    alias: String,
    binding: String,
    claim_namespace: String,
}
impl Policy {
    /// Validate the complete bound template without opening application storage.
    pub(crate) fn validate_config(config: &Config) -> Result<()> {
        if config.marker.len() != 4
            || !config.marker.as_bytes()[..3]
                .iter()
                .all(u8::is_ascii_alphanumeric)
            || !config.marker.ends_with('\n')
            || config.values > 16
            || config.inputs.is_empty()
            || config.inputs.len() > 32
            || config.alias.is_empty()
            || config.alias.len() > 64
            || !config
                .alias
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'_')
            || config.binding.len() > 16384
            || config.read_grants.values().any(|v| *v != Access::Read)
        {
            return Err("config");
        }
        Store::validate_grants(&BTreeMap::from([(
            config.claim_namespace.clone(),
            Access::ReadWrite,
        )]))
        .map_err(|_| "config")?;
        Store::validate_grants(&config.read_grants).map_err(|_| "config")?;
        for input in &config.inputs {
            match input {
                Input::CredentialFacts | Input::Bundle => return Err("config"),
                Input::Value { index } if *index >= config.values => return Err("config"),
                Input::Read { namespace, key } => {
                    if config.read_grants.get(namespace) != Some(&Access::Read)
                        || matches!(key, Key::Value { index } if *index >= config.values)
                        || matches!(key, Key::Literal { value } if !literal_key(value))
                    {
                        return Err("config");
                    }
                }
                Input::Literal { value } if value.len() > 16384 => return Err("config"),
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
            scope: store.scope(config.read_grants).map_err(|_| "config")?,
            alias: config.alias,
            binding: config.binding,
            claim_namespace: config.claim_namespace,
        })
    }

    /// Callers may supply bounded lookup identities only. The bootstrap template
    /// decides where those values go; it cannot be replaced by a request.
    pub fn begin<'a>(
        &mut self,
        store: &'a mut Store,
        effect_scope: &'a Scope,
        effect: &'a mut Bridge,
        last_clock: &'a mut u64,
        values: &[String],
    ) -> Result<(Attempt<'a>, String)> {
        if values.len() != self.count || values.iter().any(|v| v.len() > 512) {
            return Err("protocol");
        }
        let deadline = Instant::now() + Duration::from_millis(self.function.timeout_ms);
        admit_time(None, last_clock, deadline, now)?;
        let at = last_clock.to_string();
        let facts = effect.facts().map_err(|_| "worker")?;
        let fuel = facts.max_fuel;
        let timeout_ms = facts.max_timeout_ms;
        let net = serde_json::to_string(facts.net).map_err(|_| "protocol")?;
        let fs = serde_json::to_string(facts.fs).map_err(|_| "protocol")?;
        let secrets = serde_json::to_string(&facts.secret_names).map_err(|_| "protocol")?;
        let worker_facts = frame(
            "EF1\n",
            &[
                &self.alias,
                &self.binding,
                facts.source_sha256,
                facts.runtime_sha256,
                &net,
                &fs,
                &secrets,
            ],
        )?;
        let mut inputs = Vec::new();
        let mut reads = Vec::new();
        let mut framed_bytes = 4usize;
        for input in &self.inputs {
            let value = match input {
                Input::CredentialFacts | Input::Bundle => return Err("config"),
                Input::Literal { value } => value.clone(),
                Input::Value { index } => values[*index].clone(),
                Input::Clock => at.clone(),
                Input::WorkerFacts => worker_facts.clone(),
                Input::Read { namespace, key } => {
                    let key = match key {
                        Key::Literal { value } => value,
                        Key::Value { index } => &values[*index],
                    };
                    let actual = store
                        .get(&self.scope, namespace, key)
                        .map_err(|_| "storage")?;
                    let framed = frame(
                        "SR1\n",
                        &[
                            "ok",
                            &actual.revision.to_string(),
                            actual.value.as_deref().unwrap_or(""),
                        ],
                    )?;
                    reads.push((namespace.clone(), key.clone(), actual));
                    framed
                }
            };
            framed_bytes = framed_bytes.checked_add(8 + value.len()).ok_or("limit")?;
            if framed_bytes > 4 * 1024 * 1024 {
                return Err("limit");
            }
            inputs.push(value);
        }
        let raw = frame(
            &self.marker,
            &inputs.iter().map(String::as_str).collect::<Vec<_>>(),
        )?;
        let proposal = self.function.invoke(raw, deadline)?;
        let parts = fields(&proposal, "DW1\n", 7)?;
        // DW1 is a shared mechanical envelope. The last field is opaque to the
        // host; application-specific correlation remains SIGIL's responsibility.
        if parts[0] != self.alias
            || !reads.iter().any(|(ns, key, r)| {
                ns == parts[2]
                    && key == parts[3]
                    && r.revision > 0
                    && r.revision.to_string() == parts[4]
                    && r.value.as_ref().is_some_and(|v| !v.is_empty())
            })
        {
            return Err("binding");
        }
        admit_time(None, last_clock, deadline, now)?;
        let attempt = Attempt::begin(
            store,
            effect_scope,
            effect,
            last_clock,
            Binding {
                intent_namespace: parts[2].into(),
                claim_namespace: self.claim_namespace.clone(),
                key: parts[3].into(),
            },
            Execution {
                input: parts[1].into(),
                fuel,
                timeout_ms,
                time_guard: parts[5].into(),
            },
        )?;
        Ok((attempt, parts[6].into()))
    }
}

pub(crate) fn literal_key(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 256
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
}

// Resolve only bootstrap placeholders against the actual service-owned facts.
// Public requests cannot provide these values or replace a template.
pub(crate) fn bind_context(inputs: &mut [Input], facts: &str, bundle: &str) {
    for input in inputs {
        match input {
            Input::CredentialFacts => {
                *input = Input::Literal {
                    value: facts.into(),
                }
            }
            Input::Bundle => {
                *input = Input::Literal {
                    value: bundle.into(),
                }
            }
            _ => {}
        }
    }
}
