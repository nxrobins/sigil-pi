//! Scoped authenticated opaque records, atomically published with related writes.
//!
//! This is a storage/secret mechanism, not audit event selection, redaction,
//! retention, tenant quota policy, execution attestation or product readiness.
//! SIGIL must choose those policies and supply only approved content. Keys are
//! native-only; do not expose construction or arbitrary signing to a guest.
//! No automatic retry, truncation, reset, rotation or repair is provided.
//!
//! HMAC protects against modification without the key, not a compromised signer.
//! A complete older valid snapshot is indistinguishable without a trusted external
//! checkpoint. Missing chains fail verification; a caller-held checkpoint can
//! reject rollback. This new format does NOT read or migrate legacy JSONL logs.

use super::inspection::{Budget, Failure, SQLITE_STEPS, TIMEOUT_MS};
use super::*;
use hmac::{Hmac, Mac};
use serde::de::DeserializeOwned;
use std::time::Instant;
use zeroize::Zeroizing;

type HmacSha256 = Hmac<Sha256>;
const HEAD_BYTES: usize = 4096;
const RECORD_OVERHEAD: usize = 1024;
const GENESIS: &str = "0000000000000000000000000000000000000000000000000000000000000000";
pub const PAYLOAD_BYTES: usize = 16_384;
pub const CHAIN_RECORDS: u64 = 10_000;
pub const CHAIN_BYTES: u64 = 16 * 1024 * 1024;

/// Explicit native per-chain ceilings, not default/approved product allowances.
/// All encoded entries count, including escaping and authentication metadata.
/// The separately bounded head also counts against the original Store ceilings.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChainLimits {
    pub payload_bytes: usize,
    pub records: u64,
    pub bytes: u64,
}

impl ChainLimits {
    /// Read-only preflight for trusted host admission before creating state.
    /// Opening a log still checks the actual Store, process, scope and key.
    pub fn validate(self, store: &Limits) -> Result<()> {
        store.validate()?;
        if self.payload_bytes == 0
            || self.payload_bytes > PAYLOAD_BYTES
            || self.payload_bytes * 6 + RECORD_OVERHEAD > store.value_bytes
            || store.value_bytes < HEAD_BYTES
            || self.records == 0
            || self.records > CHAIN_RECORDS
            || self.records >= store.records
            || self.bytes < (self.payload_bytes * 6 + RECORD_OVERHEAD) as u64
            || self.bytes > CHAIN_BYTES
            || self.bytes > store.live_bytes
        {
            return Err(Error::Invalid);
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct RecordBody {
    version: u8,
    chain: String,
    sequence: u64,
    previous: String,
    payload: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct HeadBody {
    version: u8,
    chain: String,
    limits: ChainLimits,
    count: u64,
    bytes: u64,
    tip: String,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Signed<T> {
    body: T,
    tag: String,
}

/// An observation/receipt identity, never a capability or secret. Keeping an
/// independent latest checkpoint is the caller's responsibility, not this store's.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Checkpoint {
    pub chain: String,
    pub head_revision: u64,
    pub count: u64,
    pub bytes: u64,
    pub tip: String,
}

#[derive(Debug)]
pub struct Publication {
    pub receipt: Receipt,
    pub checkpoint: Checkpoint,
}

/// Not Clone/Debug/serializable: retained key bytes must not enter guest facts,
/// config manifests, logs, durable records or public errors. Zeroizing erases this
/// retained buffer on drop; it does not promise erasure of all crypto temporaries.
pub struct AuthenticatedLog {
    boot: [u8; 32],
    process: u32,
    heads: String,
    records: String,
    limits: ChainLimits,
    key: Zeroizing<Vec<u8>>,
}

fn encoded<T: Serialize>(value: &T) -> Result<String> {
    serde_json::to_string(value).map_err(|_| Error::Invalid)
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn digest(raw: &str) -> String {
    hex(&Sha256::digest(raw.as_bytes()))
}

fn hash_bytes(raw: &str) -> Result<[u8; 32]> {
    if raw.len() != 64
        || !raw
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(Error::Corrupt);
    }
    let mut out = [0; 32];
    for (index, byte) in out.iter_mut().enumerate() {
        *byte =
            u8::from_str_radix(&raw[index * 2..index * 2 + 2], 16).map_err(|_| Error::Corrupt)?;
    }
    Ok(out)
}

fn entry_key(chain: &str, sequence: u64) -> String {
    format!("{chain}.{sequence:016x}")
}

impl AuthenticatedLog {
    /// Trusted native construction only. Existing store admission remains intact;
    /// this instance cannot mint Scope objects and is bound to this process/boot.
    pub fn new(
        store: &Store,
        heads: &str,
        records: &str,
        limits: ChainLimits,
        key: &[u8],
    ) -> Result<Self> {
        store.verify_files()?;
        limits.validate(&store.limits)?;
        if heads == records
            || !valid_name(heads, 128)
            || !valid_name(records, 128)
            || !(32..=1024).contains(&key.len())
        {
            return Err(Error::Invalid);
        }
        Ok(Self {
            boot: store.boot,
            process: std::process::id(),
            heads: heads.into(),
            records: records.into(),
            limits,
            key: Zeroizing::new(key.to_vec()),
        })
    }

    fn authorize(
        &self,
        store: &Store,
        scope: &Scope,
        chain: &str,
        write: bool,
        revision: u64,
    ) -> Result<()> {
        if self.boot != store.boot || self.process != std::process::id() {
            return Err(Error::Denied);
        }
        store.verify_process()?;
        if hash_bytes(chain).is_err() {
            return Err(Error::Invalid);
        }
        store.authorize(scope, &self.heads, chain, false, 0, false)?;
        store.authorize(scope, &self.records, &entry_key(chain, 0), false, 0, false)?;
        if write {
            store.authorize(scope, &self.heads, chain, true, revision, false)?;
            store.authorize(scope, &self.records, &entry_key(chain, 0), true, 0, false)?;
        }
        Ok(())
    }

    fn mac<T: Serialize>(&self, domain: &[u8], body: &T) -> Result<HmacSha256> {
        let mut mac = HmacSha256::new_from_slice(&self.key).map_err(|_| Error::Invalid)?;
        // Domain plus length-delimited namespace/body context prevents cross-kind,
        // cross-namespace and ambiguous-concatenation signature reuse.
        mac.update(domain);
        let body = encoded(body)?;
        for part in [
            self.heads.as_bytes(),
            self.records.as_bytes(),
            body.as_bytes(),
        ] {
            mac.update(&(part.len() as u64).to_be_bytes());
            mac.update(part);
        }
        Ok(mac)
    }

    fn sign<T: Serialize>(&self, domain: &[u8], body: T) -> Result<String> {
        let tag = hex(&self.mac(domain, &body)?.finalize().into_bytes());
        encoded(&Signed { body, tag })
    }

    fn decode<T: Serialize + DeserializeOwned>(
        &self,
        raw: &str,
        domain: &[u8],
        limit: usize,
    ) -> Result<T> {
        if raw.len() > limit {
            return Err(Error::Corrupt);
        }
        let signed: Signed<T> = serde_json::from_str(raw).map_err(|_| Error::Corrupt)?;
        if encoded(&signed)? != raw {
            return Err(Error::Corrupt);
        }
        self.mac(domain, &signed.body)?
            .verify_slice(&hash_bytes(&signed.tag)?)
            .map_err(|_| Error::Corrupt)?;
        Ok(signed.body)
    }

    fn head(&self, raw: &str, chain: &str) -> Result<HeadBody> {
        let head: HeadBody = self.decode(raw, b"sigil-authenticated-log/head/v1\0", HEAD_BYTES)?;
        if head.version != 1
            || head.chain != chain
            || head.limits != self.limits
            || head.count == 0
            || head.count > self.limits.records
            || head.bytes == 0
            || head.bytes > self.limits.bytes
            || hash_bytes(&head.tip).is_err()
        {
            return Err(Error::Corrupt);
        }
        Ok(head)
    }

    fn record(&self, raw: &str, chain: &str, sequence: u64) -> Result<RecordBody> {
        let record: RecordBody = self.decode(
            raw,
            b"sigil-authenticated-log/record/v1\0",
            self.limits.payload_bytes * 6 + RECORD_OVERHEAD,
        )?;
        if record.version != 1
            || record.chain != chain
            || record.sequence != sequence
            || record.payload.len() > self.limits.payload_bytes
            || hash_bytes(&record.previous).is_err()
            || (sequence == 0 && record.previous != GENESIS)
        {
            return Err(Error::Corrupt);
        }
        Ok(record)
    }

    fn checkpoint(head: &HeadBody, revision: u64) -> Checkpoint {
        Checkpoint {
            chain: head.chain.clone(),
            head_revision: revision,
            count: head.count,
            bytes: head.bytes,
            tip: head.tip.clone(),
        }
    }

    fn count(connection: &Connection, namespace: &str, chain: &str, limit: u64) -> Result<u64> {
        let count: i64 = connection.query_row(
            "SELECT count(*) FROM (SELECT 1 FROM records WHERE namespace=?1 AND key>=?2 AND key<?3 LIMIT ?4)",
            params![namespace, format!("{chain}."), format!("{chain}/"), i64::try_from(limit).map_err(|_| Error::Invalid)?], |row| row.get(0),
        ).map_err(|_| Error::Corrupt)?;
        u64::try_from(count).map_err(|_| Error::Corrupt)
    }

    /// Publish one immutable authenticated record, its authenticated head and all
    /// supplied related mutations in the SAME original Store transaction. Any
    /// stale coordinate, denied namespace or capacity failure publishes nothing.
    /// CommitUncertain stays uncertain; a signature/proposal is not a receipt.
    /// Append checks the authenticated tail, not the complete historical chain.
    pub fn append(
        &self,
        store: &mut Store,
        scope: &Scope,
        chain: &str,
        expected_revision: u64,
        payload: &str,
        related: &Batch,
    ) -> Result<Publication> {
        self.append_inner(
            store,
            scope,
            chain,
            expected_revision,
            payload,
            related,
            || {},
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn append_inner(
        &self,
        store: &mut Store,
        scope: &Scope,
        chain: &str,
        expected_revision: u64,
        payload: &str,
        related: &Batch,
        before_commit: impl FnOnce(),
    ) -> Result<Publication> {
        self.authorize(store, scope, chain, true, expected_revision)?;
        if payload.len() > self.limits.payload_bytes {
            return Err(Error::Limit);
        }
        if related.checks.len() + related.writes.len() + 2 > store.limits.batch_items
            || related
                .checks
                .iter()
                .any(|v| v.namespace == self.heads || v.namespace == self.records)
            || related
                .writes
                .iter()
                .any(|v| v.namespace == self.heads || v.namespace == self.records)
        {
            return Err(Error::Invalid);
        }
        // Validate borrowed related data BEFORE cloning it. Reuse the existing
        // batch validator when there are writes; check-only batches are bounded
        // here and get the full duplicate/CAS check in the combined transaction.
        if related.writes.is_empty() {
            for check in &related.checks {
                store.authorize(
                    scope,
                    &check.namespace,
                    &check.key,
                    false,
                    check.revision,
                    false,
                )?;
            }
        } else {
            store.validate_batch(scope, related)?;
        }
        store.verify_files()?;
        let old = Store::read_record(&store.connection, &self.heads, chain, HEAD_BYTES)?;
        if old.revision != expected_revision {
            return Err(Error::Conflict);
        }
        let mut head = match old.value {
            Some(raw) => {
                let head = self.head(&raw, chain)?;
                let tail = Store::read_record(
                    &store.connection,
                    &self.records,
                    &entry_key(chain, head.count - 1),
                    self.limits.payload_bytes * 6 + RECORD_OVERHEAD,
                )?;
                let raw = tail.value.ok_or(Error::Corrupt)?;
                self.record(&raw, chain, head.count - 1)?;
                if digest(&raw) != head.tip || head.bytes < raw.len() as u64 {
                    return Err(Error::Corrupt);
                }
                head
            }
            None => {
                // A tombstone or orphaned entry is never a new/empty chain.
                if old.revision != 0
                    || Self::count(&store.connection, &self.records, chain, 1)? != 0
                {
                    return Err(Error::Corrupt);
                }
                HeadBody {
                    version: 1,
                    chain: chain.into(),
                    limits: self.limits,
                    count: 0,
                    bytes: 0,
                    tip: GENESIS.into(),
                }
            }
        };
        if head.count >= self.limits.records {
            return Err(Error::Limit);
        }
        let record = self.sign(
            b"sigil-authenticated-log/record/v1\0",
            RecordBody {
                version: 1,
                chain: chain.into(),
                sequence: head.count,
                previous: head.tip.clone(),
                payload: payload.into(),
            },
        )?;
        let entry = Mutation {
            namespace: self.records.clone(),
            key: entry_key(chain, head.count),
            revision: 0,
            value: Some(record.clone()),
        };
        head.count += 1;
        head.bytes = head
            .bytes
            .checked_add(record.len() as u64)
            .ok_or(Error::Limit)?;
        if head.bytes > self.limits.bytes {
            return Err(Error::Limit);
        }
        head.tip = digest(&record);
        let raw_head = self.sign(b"sigil-authenticated-log/head/v1\0", head.clone())?;
        if raw_head.len() > HEAD_BYTES {
            return Err(Error::Limit);
        }
        let mut batch = related.clone();
        batch.writes.push(entry);
        batch.writes.push(Mutation {
            namespace: self.heads.clone(),
            key: chain.into(),
            revision: expected_revision,
            value: Some(raw_head),
        });
        // Keep the same native transaction, kill-test hook, limits and uncertain
        // commit classification. No compensating write or automatic retry exists.
        let receipt = store.commit_inner(scope, &batch, before_commit)?;
        // Receipt revision identifies the whole-store commit. The head's CAS
        // revision is independent and advances only when this head is updated.
        let checkpoint = Self::checkpoint(&head, expected_revision + 1);
        Ok(Publication {
            receipt,
            checkpoint,
        })
    }

    /// Verify a complete chain in ONE SQLite read snapshot, with bounded
    /// per-record memory, one-second/SQLite-work ceilings and no late success.
    /// Missing, malformed, unsigned, orphaned and tombstoned records all fail.
    /// Optional external checkpoint comparison rejects a coherent older snapshot.
    /// This observation is not a future write reservation or a readiness verdict.
    pub fn verify(
        &self,
        store: &mut Store,
        scope: &Scope,
        chain: &str,
        expected: Option<&Checkpoint>,
        deadline: Instant,
    ) -> std::result::Result<Checkpoint, Failure> {
        self.verify_bounded(store, scope, chain, expected, deadline, SQLITE_STEPS)
    }

    /// Inspect an optional chain without initializing or repairing it. None
    /// means no head (revision zero) and no entries in this exact chain prefix,
    /// NOT a verified empty history. Existing chains receive full verification
    /// in the same bounded read snapshot. An expected checkpoint forbids absence.
    /// Without an independent inventory/checkpoint, complete erasure or rollback
    /// to a pre-initialization snapshot cannot be distinguished from first use.
    pub fn inspect_existing(
        &self,
        store: &mut Store,
        scope: &Scope,
        chain: &str,
        expected: Option<&Checkpoint>,
        deadline: Instant,
    ) -> std::result::Result<Option<Checkpoint>, Failure> {
        self.inspect_bounded(store, scope, chain, expected, deadline, SQLITE_STEPS)
    }

    fn verify_bounded(
        &self,
        store: &mut Store,
        scope: &Scope,
        chain: &str,
        expected: Option<&Checkpoint>,
        deadline: Instant,
        steps: u64,
    ) -> std::result::Result<Checkpoint, Failure> {
        self.inspect_bounded(store, scope, chain, expected, deadline, steps)?
            .ok_or(Failure::Observed(ObservedError::storage(Error::Missing)))
    }

    fn inspect_bounded(
        &self,
        store: &mut Store,
        scope: &Scope,
        chain: &str,
        expected: Option<&Checkpoint>,
        deadline: Instant,
        steps: u64,
    ) -> std::result::Result<Option<Checkpoint>, Failure> {
        self.authorize(store, scope, chain, false, 0)
            .map_err(|error| Failure::Observed(ObservedError::precheck(error)))?;
        let deadline = deadline.min(Instant::now() + Duration::from_millis(TIMEOUT_MS));
        if Instant::now() >= deadline {
            return Err(Failure::Deadline);
        }
        let storage = |error| Failure::Observed(ObservedError::storage(error));
        store.verify_storage_files().map_err(storage)?;
        let budget = Budget::install(&store.connection, deadline, steps)?;
        budget.check()?;
        let result = (|| {
            let tx = store
                .connection
                .unchecked_transaction()
                .map_err(|_| Error::Storage)?;
            let head_row = Store::read_record(&tx, &self.heads, chain, HEAD_BYTES)?;
            if head_row.value.is_none() {
                if head_row.revision != 0 || Self::count(&tx, &self.records, chain, 1)? != 0 {
                    return Err(Error::Corrupt);
                }
                if expected.is_some() {
                    return Err(Error::Conflict);
                }
                tx.commit().map_err(|_| Error::Storage)?;
                return Ok(None);
            }
            let head = self.head(head_row.value.as_deref().ok_or(Error::Missing)?, chain)?;
            let checkpoint = Self::checkpoint(&head, head_row.revision);
            if expected.is_some_and(|value| value != &checkpoint) {
                return Err(Error::Conflict);
            }
            if Self::count(&tx, &self.records, chain, self.limits.records + 1)? != head.count {
                return Err(Error::Corrupt);
            }
            let mut previous = GENESIS.to_owned();
            let mut bytes = 0u64;
            for sequence in 0..head.count {
                // Budget error is preserved below even if an interrupted SQLite
                // operation otherwise maps to a storage error.
                if budget.check().is_err() {
                    return Err(Error::Limit);
                }
                let row = Store::read_record(
                    &tx,
                    &self.records,
                    &entry_key(chain, sequence),
                    self.limits.payload_bytes * 6 + RECORD_OVERHEAD,
                )?;
                let raw = row.value.ok_or(Error::Corrupt)?;
                let record = self.record(&raw, chain, sequence)?;
                if record.previous != previous {
                    return Err(Error::Corrupt);
                }
                bytes = bytes.checked_add(raw.len() as u64).ok_or(Error::Limit)?;
                if bytes > self.limits.bytes {
                    return Err(Error::Limit);
                }
                previous = digest(&raw);
            }
            if bytes != head.bytes || previous != head.tip {
                return Err(Error::Corrupt);
            }
            tx.commit().map_err(|_| Error::Storage)?;
            Ok(Some(checkpoint))
        })();
        budget.check()?;
        let checkpoint = result.map_err(storage)?;
        store.verify_storage_files().map_err(storage)?;
        budget.check()?;
        Ok(checkpoint)
    }
}

#[cfg(test)]
#[path = "authenticated_log_tests.rs"]
mod tests;
