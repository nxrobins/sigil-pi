//! Content-free, read-only inspection of the admitted store and scoped records.
//! This is not readiness, a write reservation, an audit check, or a durability
//! promise. SQLite structure/capacity are shared physical-store facts; record
//! payloads are inspected only in namespaces covered by the held read scope.
use super::*;
use std::sync::Arc;
use std::sync::atomic::{AtomicU8, Ordering};
use std::time::Instant;

pub const NAMESPACES: usize = 8;
pub const TIMEOUT_MS: u64 = 1000;
pub const SQLITE_STEPS: u64 = 8_000_000;
const PROGRESS_STEPS: u64 = 1000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Failure {
    Observed(ObservedError),
    Deadline,
    WorkLimit,
}

type Inspected<T> = std::result::Result<T, Failure>;

fn storage(error: Error) -> Failure {
    Failure::Observed(ObservedError::storage(error))
}

fn sql(error: rusqlite::Error) -> Failure {
    use rusqlite::ErrorCode;
    storage(match error.sqlite_error_code() {
        Some(ErrorCode::DatabaseBusy | ErrorCode::DatabaseLocked) => Error::Busy,
        Some(ErrorCode::SystemIoFailure | ErrorCode::CannotOpen | ErrorCode::DiskFull) => {
            Error::Storage
        }
        _ => Error::Corrupt,
    })
}

pub(super) struct Budget<'a> {
    connection: &'a Connection,
    deadline: Instant,
    stopped: Arc<AtomicU8>,
}

impl<'a> Budget<'a> {
    pub(super) fn install(
        connection: &'a Connection,
        deadline: Instant,
        steps: u64,
    ) -> Inspected<Self> {
        let stopped = Arc::new(AtomicU8::new(0));
        let observed = Arc::clone(&stopped);
        let mut spent = 0;
        connection
            .progress_handler(
                PROGRESS_STEPS as i32,
                Some(move || {
                    spent += PROGRESS_STEPS;
                    let reason = if Instant::now() >= deadline {
                        1
                    } else if spent >= steps {
                        2
                    } else {
                        0
                    };
                    observed.store(reason, Ordering::Relaxed);
                    reason != 0
                }),
            )
            .map_err(sql)?;
        Ok(Self {
            connection,
            deadline,
            stopped,
        })
    }

    pub(super) fn check(&self) -> Inspected<()> {
        if Instant::now() >= self.deadline || self.stopped.load(Ordering::Relaxed) == 1 {
            Err(Failure::Deadline)
        } else if self.stopped.load(Ordering::Relaxed) == 2 {
            Err(Failure::WorkLimit)
        } else {
            Ok(())
        }
    }
}

impl Drop for Budget<'_> {
    fn drop(&mut self) {
        // This private handler has no caller callbacks. It must not affect the
        // next read/commit, including after a failed inspection or unwinding.
        let _ = self.connection.progress_handler(0, None::<fn() -> bool>);
    }
}

impl Store {
    /// One read transaction, all requested namespaces or no success. Scope and
    /// process checks precede all filesystem/SQLite work. No connection, scope,
    /// namespace, record, revision, or receipt is created by this operation.
    ///
    /// The independent one-second/SQLite-work ceilings may shorten the caller's
    /// deadline, never extend it. Checks bound query work cooperatively; they do
    /// not promise to interrupt a kernel filesystem call or a single bounded
    /// digest computation at the deadline. An overrun cannot return success.
    pub fn inspect_namespaces(
        &mut self,
        scope: &Scope,
        namespaces: &[String],
        deadline: Instant,
    ) -> Inspected<()> {
        self.inspect_bounded(scope, namespaces, deadline, SQLITE_STEPS)
    }

    fn inspect_bounded(
        &mut self,
        scope: &Scope,
        namespaces: &[String],
        deadline: Instant,
        steps: u64,
    ) -> Inspected<()> {
        if namespaces.is_empty() || namespaces.len() > NAMESPACES {
            return Err(Failure::Observed(ObservedError::precheck(Error::Limit)));
        }
        let mut unique = BTreeSet::new();
        for namespace in namespaces {
            self.authorize(scope, namespace, "_inspection", false, 0, false)
                .map_err(|error| Failure::Observed(ObservedError::precheck(error)))?;
            if !unique.insert(namespace) {
                return Err(Failure::Observed(ObservedError::precheck(Error::Invalid)));
            }
        }
        self.verify_process()
            .map_err(|error| Failure::Observed(ObservedError::precheck(error)))?;
        let deadline = deadline.min(Instant::now() + Duration::from_millis(TIMEOUT_MS));
        if Instant::now() >= deadline {
            return Err(Failure::Deadline);
        }
        self.verify_storage_files().map_err(storage)?;
        let budget = Budget::install(&self.connection, deadline, steps)?;
        budget.check()?;
        let result = self.inspect_snapshot(namespaces, &budget);
        // Preserve deadline/work exhaustion rather than misclassifying SQLite's
        // interrupted query as corrupt storage. No partial success is returned.
        budget.check()?;
        result?;
        self.verify_storage_files().map_err(storage)?;
        budget.check()
    }

    fn inspect_snapshot(&self, namespaces: &[String], budget: &Budget<'_>) -> Inspected<()> {
        let tx = self.connection.unchecked_transaction().map_err(sql)?;
        let app: i64 = tx
            .pragma_query_value(None, "application_id", |r| r.get(0))
            .map_err(sql)?;
        let version: i64 = tx
            .pragma_query_value(None, "user_version", |r| r.get(0))
            .map_err(sql)?;
        if app != APPLICATION_ID || version != 1 {
            return Err(storage(Error::Corrupt));
        }
        // Bound diagnostic allocation and rows; never return SQLite diagnostics.
        let check: bool = tx
            .query_row("PRAGMA quick_check(1)", [], |r| {
                Ok(matches!(
                    r.get_ref(0)?,
                    rusqlite::types::ValueRef::Text(b"ok")
                ))
            })
            .map_err(sql)?;
        if !check {
            return Err(storage(Error::Corrupt));
        }
        {
            let mut stmt = tx.prepare(
                "SELECT name = ?1 AND sql = ?2 FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY name LIMIT 3",
            ).map_err(sql)?;
            let definitions: Vec<bool> = stmt
                .query_map(params!["meta", META_SQL], |r| r.get(0))
                .map_err(sql)?
                .collect::<std::result::Result<_, _>>()
                .map_err(sql)?;
            if definitions != [true, false] {
                return Err(storage(Error::Corrupt));
            }
            let records: bool = tx.query_row(
                "SELECT name = 'records' AND sql = ?1 FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY name LIMIT 1 OFFSET 1",
                [RECORD_SQL], |r| r.get(0),
            ).map_err(sql)?;
            if !records {
                return Err(storage(Error::Corrupt));
            }
        }
        let (revision, encoded): (i64, String) = tx.query_row(
            "SELECT revision,limits,length(limits) FROM meta WHERE id = 1 AND revision >= 0 AND typeof(revision) = 'integer'",
            [], |r| {
                let length: i64 = r.get(2)?;
                if !(1..=4096).contains(&length) {
                    return Err(rusqlite::Error::InvalidQuery);
                }
                Ok((r.get(0)?, r.get(1)?))
            },
        ).map_err(sql)?;
        // Match Store::open's semantic comparison, not JSON key order/layout.
        if serde_json::from_str::<Limits>(&encoded).map_err(|_| storage(Error::Corrupt))?
            != self.limits
        {
            return Err(storage(Error::Corrupt));
        }
        let count: i64 = tx
            .query_row("SELECT count(*) FROM meta", [], |r| r.get(0))
            .map_err(sql)?;
        if count != 1 {
            return Err(storage(Error::Corrupt));
        }
        Self::capacity(&tx, &self.limits).map_err(storage)?;
        for namespace in namespaces {
            budget.check()?;
            let mut stmt = tx.prepare(
                "SELECT key,revision,value,digest,length(key),length(value),length(digest) FROM records WHERE namespace = ?1 ORDER BY key LIMIT ?2",
            ).map_err(sql)?;
            let mut rows = stmt
                .query(params![namespace, self.limits.records as i64 + 1])
                .map_err(sql)?;
            let mut count = 0;
            while let Some(row) = rows.next().map_err(sql)? {
                budget.check()?;
                count += 1;
                if count > self.limits.records {
                    return Err(storage(Error::Limit));
                }
                let key_size: i64 = row.get(4).map_err(sql)?;
                let value_size: Option<i64> = row.get(5).map_err(sql)?;
                let digest_size: i64 = row.get(6).map_err(sql)?;
                if !(1..=256).contains(&key_size)
                    || value_size
                        .is_some_and(|n| n < 0 || n as u64 > self.limits.value_bytes as u64)
                    || digest_size != 32
                {
                    return Err(storage(Error::Corrupt));
                }
                let key: String = row.get(0).map_err(sql)?;
                let record_revision: i64 = row.get(1).map_err(sql)?;
                let value: Option<Vec<u8>> = row.get(2).map_err(sql)?;
                let digest: Vec<u8> = row.get(3).map_err(sql)?;
                if !valid_name(&key, 256) || record_revision <= 0 || record_revision > revision {
                    return Err(storage(Error::Corrupt));
                }
                let value = value
                    .as_deref()
                    .map(std::str::from_utf8)
                    .transpose()
                    .map_err(|_| storage(Error::Corrupt))?;
                if digest != record_digest(namespace, &key, record_revision as u64, value) {
                    return Err(storage(Error::Corrupt));
                }
                budget.check()?;
            }
        }
        budget.check()?;
        tx.commit().map_err(sql)
    }
}

#[cfg(test)]
#[path = "inspection_tests.rs"]
mod tests;
