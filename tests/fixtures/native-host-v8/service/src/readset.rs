//! Bounded opaque read-set codec; exposed only by the explicit v8 host profile.
//! No application traversal, tenant allowance, route or recovery decisions.
use crate::Result;
use sigil_durable_store::store::{READ_SET_ITEMS, ReadKey, Record, Scope, Store};
use sigil_worker_bridge::strict;
use std::collections::BTreeSet;
use std::fmt::Write;

pub const QUERY_BYTES: usize = 4096;
pub const RESPONSE_BYTES: usize = 2 * 1024 * 1024;

#[derive(Debug)]
pub struct ReadSet {
    keys: Vec<ReadKey>,
}

fn valid_name(value: &str, max: usize) -> bool {
    !value.is_empty()
        && value.len() <= max
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
}

fn field(out: &mut String, value: &str) -> Result<()> {
    write!(out, "{:08}", value.len()).map_err(|_| "protocol")?;
    out.push_str(value);
    Ok(())
}

impl ReadSet {
    /// Exact array of namespace/key pairs. Structural validity is not authority;
    /// the held native scope is still checked for every address during execution.
    pub fn parse(raw: &str) -> Result<Self> {
        if raw.len() > QUERY_BYTES {
            return Err("limit");
        }
        let value = strict::parse(raw.as_bytes()).map_err(|_| "protocol")?;
        let keys: Vec<ReadKey> = serde_json::from_value(value).map_err(|_| "protocol")?;
        if keys.is_empty() || keys.len() > READ_SET_ITEMS {
            return Err("protocol");
        }
        let mut seen = BTreeSet::new();
        for key in &keys {
            if !valid_name(&key.namespace, 128)
                || !valid_name(&key.key, 256)
                || !seen.insert((&key.namespace, &key.key))
            {
                return Err("protocol");
            }
        }
        Ok(Self { keys })
    }

    /// One actual scoped read transaction. Errors reveal neither a partial set
    /// nor which member failed. The embedding action must enforce its time guard
    /// BEFORE calling this function; parsing a query does not grant that action.
    pub fn observe(&self, store: &mut Store, scope: &Scope) -> Result<String> {
        match store.get_many(scope, &self.keys) {
            Ok(records) => self.encode(&records),
            Err(_) => Ok(Self::failure()),
        }
    }

    pub fn failure() -> String {
        // RM1 has three fixed fields: status, count, RB1 batch (empty on error).
        "RM1\n00000005error00000001000000000".to_owned()
    }

    fn encode(&self, records: &[Record]) -> Result<String> {
        if records.len() != self.keys.len() {
            return Err("protocol");
        }
        let mut lengths = Vec::with_capacity(records.len());
        let mut revisions = Vec::with_capacity(records.len());
        for (key, record) in self.keys.iter().zip(records) {
            if record.revision > i64::MAX as u64 || (record.revision == 0 && record.value.is_some())
            {
                return Err("protocol");
            }
            let revision = record.revision.to_string();
            // RR1: namespace, key, decimal revision, presence 0/1, opaque value.
            let length = 4usize + 5 * 8 + key.namespace.len() + key.key.len() + revision.len() + 1;
            lengths.push(
                length
                    .checked_add(record.value.as_ref().map_or(0, String::len))
                    .ok_or("limit")?,
            );
            revisions.push(revision);
        }
        // RB1 always contains three fields, padding unused slots with empty data.
        // Count is 1..=3, hence one digit. Bound the FULL response before creating
        // it; the store's independent aggregate-value bound excludes wire framing.
        let batch_bytes = lengths
            .iter()
            .try_fold(4usize + READ_SET_ITEMS * 8, |size, row| {
                size.checked_add(*row)
            })
            .ok_or("limit")?;
        let total = (4usize + 3 * 8 + 2 + 1)
            .checked_add(batch_bytes)
            .ok_or("limit")?;
        if total > RESPONSE_BYTES {
            return Err("limit");
        }
        let mut out = String::with_capacity(total);
        out.push_str("RM1\n");
        field(&mut out, "ok")?;
        field(&mut out, &records.len().to_string())?;
        write!(&mut out, "{batch_bytes:08}").map_err(|_| "protocol")?;
        out.push_str("RB1\n");
        for (i, (key, record)) in self.keys.iter().zip(records).enumerate() {
            write!(&mut out, "{:08}", lengths[i]).map_err(|_| "protocol")?;
            out.push_str("RR1\n");
            field(&mut out, &key.namespace)?;
            field(&mut out, &key.key)?;
            field(&mut out, &revisions[i])?;
            field(&mut out, if record.value.is_some() { "1" } else { "0" })?;
            field(&mut out, record.value.as_deref().unwrap_or(""))?;
        }
        for _ in records.len()..READ_SET_ITEMS {
            field(&mut out, "")?;
        }
        if out.len() != total {
            return Err("protocol");
        }
        Ok(out)
    }
}

#[cfg(test)]
#[path = "readset_tests.rs"]
mod tests;
