//! Bounded process-local compare-and-set cells under held native store scopes.
//!
//! These cells are NOT durable records, admission receipts, time windows or a
//! fallback policy. They are never persisted, evicted or automatically reset.
//! An embedding application must keep one instance for the admitted process
//! lifetime, enforce its action/time guards, and interpret opaque values in SIGIL.
//! Dropping/recreating the instance loses its data; this must not be hidden by
//! an API claiming durable state or exactly-once external effects.

use super::{Error, Mutation, Record, Result, Scope, Store, valid_name};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub const VALUE_BYTES: usize = 65_536;
pub const RECORDS: usize = 64;
pub const LIVE_BYTES: usize = 4 * 1024 * 1024;

/// Deliberately not the durable Store's Receipt type. A successful temporary
/// mutation must never be framed or interpreted as a durable commit receipt.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VolatileReceipt {
    pub revision: u64,
}

/// Explicit per-declared-namespace ceilings, not tenant allowances or defaults.
/// The SUM of reserved ceilings must fit the independent process ceiling and the
/// already admitted store ceiling. One namespace cannot consume another's share.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct VolatileLimits {
    pub value_bytes: usize,
    pub records: usize,
    pub live_bytes: usize,
}

impl VolatileLimits {
    /// Configuration-only admission, also repeated against the actual Store.
    pub fn validate_for(self, limits: &super::Limits, namespaces: usize) -> Result<()> {
        if self.value_bytes == 0
            || self.value_bytes > VALUE_BYTES
            || self.value_bytes > limits.value_bytes
            || self.records == 0
            || self.records > RECORDS
            || self.records as u64 > limits.records
            || self.live_bytes < self.value_bytes
            || self.live_bytes > LIVE_BYTES
            || self.live_bytes as u64 > limits.live_bytes
            || namespaces == 0
            || namespaces > RECORDS
            || self
                .records
                .checked_mul(namespaces)
                .is_none_or(|n| n > RECORDS || n as u64 > limits.records)
            || self
                .live_bytes
                .checked_mul(namespaces)
                .is_none_or(|n| n > LIVE_BYTES || n as u64 > limits.live_bytes)
        {
            return Err(Error::Invalid);
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
struct Usage {
    records: usize,
    live_bytes: usize,
}

/// No deserialization or grant-minting API. The caller must hold a real Scope
/// from the same admitted Store. Its existing pure grant check is reused without
/// depending on current database availability, which is a separate observation.
pub struct Volatile {
    boot: [u8; 32],
    process: u32,
    limits: VolatileLimits,
    namespaces: BTreeMap<String, Usage>,
    cells: BTreeMap<(String, String), Record>,
    revision: u64,
    live_bytes: usize,
}

impl Volatile {
    /// Trusted process initialization only. Do not expose construction or reset
    /// to a guest or call it anew for every request. Existing scope minting still
    /// requires valid admitted storage; a storage failure cannot mint authority.
    pub fn new(store: &Store, limits: VolatileLimits, namespaces: &[&str]) -> Result<Self> {
        store.verify_files()?;
        limits.validate_for(&store.limits, namespaces.len())?;
        let mut declared = BTreeMap::new();
        for name in namespaces {
            if !valid_name(name, 128)
                || declared
                    .insert((*name).to_owned(), Usage::default())
                    .is_some()
            {
                return Err(Error::Invalid);
            }
        }
        Ok(Self {
            boot: store.boot,
            process: std::process::id(),
            limits,
            namespaces: declared,
            cells: BTreeMap::new(),
            revision: 0,
            live_bytes: 0,
        })
    }

    fn owner(&self, store: &Store) -> Result<()> {
        if self.process != std::process::id()
            || store.owner.process != std::process::id()
            || self.boot != store.boot
        {
            return Err(Error::Denied);
        }
        Ok(())
    }

    pub fn get(&self, store: &Store, scope: &Scope, namespace: &str, key: &str) -> Result<Record> {
        self.owner(store)?;
        store.authorize(scope, namespace, key, false, 0, false)?;
        if !self.namespaces.contains_key(namespace) {
            return Err(Error::Denied);
        }
        Ok(self
            .cells
            .get(&(namespace.to_owned(), key.to_owned()))
            .cloned()
            .unwrap_or(Record {
                revision: 0,
                value: None,
            }))
    }

    /// One atomic cell update under Rust's exclusive mutable access. All checks
    /// precede mutation; failure changes neither data, revision nor byte usage.
    /// A tombstone keeps its slot and revision, preventing delete/recreate ABA.
    /// No cell is evicted to admit another tenant or caller-selected key.
    pub fn compare_set(
        &mut self,
        store: &Store,
        scope: &Scope,
        write: &Mutation,
    ) -> Result<VolatileReceipt> {
        self.owner(store)?;
        store.authorize(
            scope,
            &write.namespace,
            &write.key,
            true,
            write.revision,
            write.value.is_none(),
        )?;
        let namespace = self.namespaces.get(&write.namespace).ok_or(Error::Denied)?;
        let address = (write.namespace.clone(), write.key.clone());
        let previous = self.cells.get(&address);
        if previous.map_or(0, |record| record.revision) != write.revision {
            return Err(Error::Conflict);
        }
        let length = write.value.as_ref().map_or(0, String::len);
        if length > self.limits.value_bytes
            || (previous.is_none() && namespace.records >= self.limits.records)
        {
            return Err(Error::Limit);
        }
        let old_length = previous
            .and_then(|record| record.value.as_ref())
            .map_or(0, String::len);
        let namespace_bytes = namespace
            .live_bytes
            .checked_sub(old_length)
            .and_then(|value| value.checked_add(length))
            .filter(|value| *value <= self.limits.live_bytes)
            .ok_or(Error::Limit)?;
        let live_bytes = self
            .live_bytes
            .checked_sub(old_length)
            .and_then(|n| n.checked_add(length))
            .ok_or(Error::Limit)?;
        let records = namespace.records + usize::from(previous.is_none());
        let revision = self
            .revision
            .checked_add(1)
            .filter(|revision| *revision <= i64::MAX as u64)
            .ok_or(Error::Limit)?;
        self.cells.insert(
            address,
            Record {
                revision,
                value: write.value.clone(),
            },
        );
        self.namespaces.insert(
            write.namespace.clone(),
            Usage {
                records,
                live_bytes: namespace_bytes,
            },
        );
        self.live_bytes = live_bytes;
        self.revision = revision;
        Ok(VolatileReceipt { revision })
    }
}

#[cfg(test)]
#[path = "volatile_tests.rs"]
mod tests;
