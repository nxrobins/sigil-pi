//! Explicit process-lifetime mechanisms, not a readiness or allowance policy.
//! Configuration is bundle-bound; native scopes still govern every cell action.
use crate::{CredentialConfig, Result, lifecycle, monotonic_facts::MonotonicFacts};
use serde::{Deserialize, Serialize};
use sigil_durable_store::store::volatile::{Volatile, VolatileLimits};
use sigil_durable_store::store::{Access, Limits, Store};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub namespaces: Vec<String>,
    pub per_namespace: VolatileLimits,
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "explicit_lifecycle"
    )]
    pub lifecycle: Option<lifecycle::Config>,
}

fn explicit_lifecycle<'de, D>(
    deserializer: D,
) -> std::result::Result<Option<lifecycle::Config>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    lifecycle::Config::deserialize(deserializer).map(Some)
}

pub(crate) fn require_profile(
    contract: crate::action::EntryContract,
    config: Option<&Config>,
) -> Result<()> {
    if (contract == crate::action::EntryContract::ProcessFacts) != config.is_some() {
        return Err("config");
    }
    Ok(())
}

pub(crate) fn manifest(config: &Config) -> serde_json::Value {
    let mut manifest = serde_json::json!({
        "contract":"sigil-process-facts/v1", "lifetime":"one-admitted-process",
        "configuration":config,
        "temporary_value_bytes":sigil_durable_store::store::volatile::VALUE_BYTES,
        "temporary_records":sigil_durable_store::store::volatile::RECORDS,
        "temporary_live_bytes":sigil_durable_store::store::volatile::LIVE_BYTES,
        "reset_command":false, "grant_minting":false, "durable":false
    });
    if config.lifecycle.is_some() {
        manifest["contract"] = serde_json::json!("sigil-process-facts/v2");
        manifest["observation"] = serde_json::json!("PF1");
        manifest["fields"] =
            serde_json::json!(["MC1-monotonic-clock", "LF1-lifecycle-observation"]);
        manifest["lifecycle"] = lifecycle::manifest();
    }
    manifest
}

pub(crate) fn bind_manifest(identity: &mut serde_json::Value, config: &Config) {
    identity["process_facts"] = manifest(config);
    if config.lifecycle.is_some() {
        identity["http"]["contract"] = serde_json::json!("sigil-http-exchange/v4");
        identity["http"]["process_observation"] = serde_json::json!({
            "index":17, "observation":"PF1", "fields":["MC1","LF1"], "boot":""
        });
        identity["http"]["monotonic_clock"]["within"] =
            serde_json::json!({"observation":"PF1","field":0});
    }
}

impl Config {
    pub(crate) fn validate(&self, limits: &Limits, credentials: &[CredentialConfig]) -> Result<()> {
        let declared: BTreeSet<_> = self.namespaces.iter().collect();
        if declared.len() != self.namespaces.len() {
            return Err("config");
        }
        self.per_namespace
            .validate_for(limits, declared.len())
            .map_err(|_| "config")?;
        let names: BTreeMap<_, _> = self
            .namespaces
            .iter()
            .map(|name| (name.clone(), Access::ReadWrite))
            .collect();
        Store::validate_grants(&names).map_err(|_| "config")?;
        // Declaring capacity does not mint a capability. Require an existing
        // configured writer, and still use the matched credential's real Scope
        // at dispatch. Tenant ownership/allowance decisions remain in SIGIL.
        if self.namespaces.iter().any(|name| {
            !credentials
                .iter()
                .any(|row| row.grants.get(name) == Some(&Access::ReadWrite))
        }) {
            return Err("config");
        }
        Ok(())
    }
}

pub(crate) struct ProcessFacts {
    pub(crate) cells: Volatile,
    clock: MonotonicFacts,
    lifecycle: Option<lifecycle::Latch>,
}

impl ProcessFacts {
    /// Called once after SIGIL bootstrap and Store admission, never per request.
    pub(crate) fn open(store: &Store, config: Config) -> Result<Self> {
        Ok(Self {
            cells: Volatile::new(
                store,
                config.per_namespace,
                &config
                    .namespaces
                    .iter()
                    .map(String::as_str)
                    .collect::<Vec<_>>(),
            )
            .map_err(|_| "config")?,
            clock: MonotonicFacts::new()?,
            lifecycle: config
                .lifecycle
                .as_ref()
                .map(lifecycle::Latch::open)
                .transpose()?,
        })
    }

    pub(crate) fn observe(&mut self) -> Result<String> {
        let clock = self.clock.observe()?;
        match &self.lifecycle {
            Some(lifecycle) => crate::frame("PF1\n", &[&clock, &lifecycle.observe()?]),
            None => Ok(clock),
        }
    }
}
