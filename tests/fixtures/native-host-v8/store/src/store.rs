//! `sigil-store/v1`: atomic compare-and-set over bounded, scoped opaque records.
//! This module knows nothing about conversations, tools, delivery phases or quotas.

use rusqlite::{Connection, OpenFlags, OptionalExtension, TransactionBehavior, params};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::time::Duration;

const APPLICATION_ID: i64 = 0x53475431;
const META_SQL: &str = "CREATE TABLE meta (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL CHECK(revision>=0), limits TEXT NOT NULL)";
const RECORD_SQL: &str = "CREATE TABLE records (namespace TEXT NOT NULL, key TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0), value BLOB, digest BLOB NOT NULL, PRIMARY KEY(namespace,key)) WITHOUT ROWID";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Error {
    Invalid,
    Denied,
    Conflict,
    AlreadyExists,
    Missing,
    Busy,
    Corrupt,
    Storage,
    Limit,
    CommitUncertain,
    ReopenRequired,
}

pub type Result<T> = std::result::Result<T, Error>;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Limits {
    pub value_bytes: usize,
    pub batch_bytes: usize,
    pub batch_items: usize,
    pub live_bytes: u64,
    pub records: u64,
    pub database_pages: u32,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            value_bytes: 2 * 1024 * 1024,
            batch_bytes: 8 * 1024 * 1024,
            batch_items: 64,
            live_bytes: 128 * 1024 * 1024,
            records: 100_000,
            database_pages: 262_144,
        }
    }
}

impl Limits {
    fn validate(&self) -> Result<()> {
        if self.value_bytes == 0
            || self.value_bytes > 2 * 1024 * 1024
            || self.batch_bytes < self.value_bytes
            || self.batch_bytes > 8 * 1024 * 1024
            || self.batch_items == 0
            || self.batch_items > 64
            || self.live_bytes < self.value_bytes as u64
            || self.live_bytes > 1024 * 1024 * 1024
            || self.records == 0
            || self.records > 100_000
            || self.database_pages < 16
            || self.database_pages > 262_144
        {
            return Err(Error::Invalid);
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Access {
    Read,
    ReadWrite,
    CreateOnly,
}

/// Minted by the embedding trusted host, not deserializable from guest messages.
pub struct Scope {
    boot: [u8; 32],
    grants: BTreeMap<String, Access>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Check {
    pub namespace: String,
    pub key: String,
    pub revision: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Mutation {
    pub namespace: String,
    pub key: String,
    pub revision: u64,
    /// None leaves a versioned tombstone; it does not recreate revision zero.
    #[serde(deserialize_with = "required_nullable")]
    pub value: Option<String>,
}

fn required_nullable<'de, D: serde::Deserializer<'de>>(
    input: D,
) -> std::result::Result<Option<String>, D::Error> {
    Option::<String>::deserialize(input)
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Batch {
    pub checks: Vec<Check>,
    pub writes: Vec<Mutation>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Record {
    pub revision: u64,
    pub value: Option<String>,
}

/// A caller-selected opaque record address, never an authority or dependency.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReadKey {
    pub namespace: String,
    pub key: String,
}

// The immediate consumer has three independent admission snapshots. This is
// deliberately not an unbounded query language or a host-side domain traversal.
pub const READ_SET_ITEMS: usize = 3;
pub const READ_SET_VALUE_BYTES: usize = 2 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Receipt {
    pub revision: u64,
}

/// Bounded discovery hints, not a snapshot or authority to execute a record.
/// `next` is an exclusive lexical cursor; a full final page may require one
/// additional empty read. Applications choose eligibility and rescan policy.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct KeyPage {
    pub keys: Vec<String>,
    pub next: Option<String>,
}

/// Native record facts only. Presence does not imply application eligibility;
/// an empty value and a tombstone are distinct facts. No payload is serialized.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RecordMetadata {
    pub key: String,
    #[serde(serialize_with = "metadata_revision")]
    pub revision: u64,
    pub present: bool,
    pub value_bytes: usize,
}

fn metadata_revision<S: serde::Serializer>(
    value: &u64,
    serializer: S,
) -> std::result::Result<S::Ok, S::Error> {
    serializer.serialize_str(&value.to_string())
}

/// One live lexical page, not a durable collection snapshot or a capability.
/// Tombstones are included and the cursor tracks the last scanned key.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct MetadataPage {
    pub entries: Vec<RecordMetadata>,
    pub next: Option<String>,
}

#[derive(Clone, Copy)]
pub enum OpenMode {
    CreateNew,
    Existing,
}

struct OwnedLock {
    file: File,
    process: u32,
}

impl std::ops::Deref for OwnedLock {
    type Target = File;
    fn deref(&self) -> &File {
        &self.file
    }
}

impl Drop for OwnedLock {
    fn drop(&mut self) {
        // Closing only this descriptor is insufficient if a concurrently spawned
        // child temporarily inherited the open file description before exec.
        // Release explicitly; no untrusted caller can obtain this descriptor.
        if self.process == std::process::id() {
            let _ = self.file.unlock();
        }
    }
}

/// One connection and one owned lock. Never expose the underlying Connection.
pub struct Store {
    // Fields drop in declaration order: close SQLite before releasing ownership.
    connection: Connection,
    owner: OwnedLock,
    directory: File,
    root: PathBuf,
    database_identity: (u64, u64),
    boot: [u8; 32],
    limits: Limits,
    poisoned: bool,
}

fn identity(metadata: &fs::Metadata) -> (u64, u64) {
    (metadata.dev(), metadata.ino())
}

fn private_metadata(path: &Path, directory: bool) -> Result<fs::Metadata> {
    let metadata = fs::symlink_metadata(path).map_err(|_| Error::Storage)?;
    // SAFETY: geteuid has no arguments or memory access through caller pointers.
    let uid = unsafe { libc::geteuid() };
    if metadata.uid() != uid
        || metadata.mode() & 0o077 != 0
        || (directory && !metadata.is_dir())
        || (!directory && (!metadata.is_file() || metadata.nlink() != 1))
    {
        return Err(Error::Denied);
    }
    Ok(metadata)
}

fn valid_name(value: &str, max: usize) -> bool {
    !value.is_empty()
        && value.len() <= max
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
}

fn record_digest(namespace: &str, key: &str, revision: u64, value: Option<&str>) -> Vec<u8> {
    let mut hasher = Sha256::new();
    hasher.update(b"sigil-store/record/v1\0");
    for part in [namespace, key] {
        hasher.update((part.len() as u64).to_be_bytes());
        hasher.update(part.as_bytes());
    }
    hasher.update(revision.to_be_bytes());
    hasher.update([u8::from(value.is_some())]);
    if let Some(value) = value {
        hasher.update(value.as_bytes());
    }
    hasher.finalize().to_vec()
}

impl Store {
    /// Root must already exist, belong to this uid and have no group/other access.
    /// Initialization is explicit; missing/corrupt existing state never becomes new.
    pub fn open(root: &Path, mode: OpenMode, limits: Limits) -> Result<Self> {
        limits.validate()?;
        let before = private_metadata(root, true)?;
        let root = root.canonicalize().map_err(|_| Error::Storage)?;
        let directory = File::open(&root).map_err(|_| Error::Storage)?;
        if identity(&before) != identity(&directory.metadata().map_err(|_| Error::Storage)?) {
            return Err(Error::Denied);
        }
        let owner_path = root.join("owner.lock");
        let owner = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(&owner_path)
            .map_err(|_| Error::Denied)?;
        let owner_metadata = private_metadata(&owner_path, false)?;
        if identity(&owner_metadata) != identity(&owner.metadata().map_err(|_| Error::Storage)?) {
            return Err(Error::Denied);
        }
        owner.try_lock().map_err(|_| Error::Busy)?;
        let owner = OwnedLock {
            file: owner,
            process: std::process::id(),
        };
        for name in [
            "records.sqlite-journal",
            "records.sqlite-wal",
            "records.sqlite-shm",
        ] {
            let path = root.join(name);
            if path.try_exists().map_err(|_| Error::Storage)? {
                private_metadata(&path, false)?;
                if name != "records.sqlite-journal" {
                    return Err(Error::Corrupt);
                }
            } else if fs::symlink_metadata(&path).is_ok() {
                return Err(Error::Denied);
            }
        }
        let db_path = root.join("records.sqlite");
        match mode {
            OpenMode::CreateNew => {
                OpenOptions::new()
                    .read(true)
                    .write(true)
                    .create_new(true)
                    .mode(0o600)
                    .open(&db_path)
                    .map_err(|e| {
                        if e.kind() == std::io::ErrorKind::AlreadyExists {
                            Error::AlreadyExists
                        } else {
                            Error::Storage
                        }
                    })?;
            }
            OpenMode::Existing => {
                if !db_path.try_exists().map_err(|_| Error::Storage)? {
                    return Err(Error::Missing);
                }
            }
        }
        let database_metadata = private_metadata(&db_path, false)?;
        if database_metadata.len() > u64::from(limits.database_pages) * 4096 {
            return Err(Error::Limit);
        }
        let database_identity = identity(&database_metadata);
        let mut connection = Connection::open_with_flags(
            &db_path,
            OpenFlags::SQLITE_OPEN_READ_WRITE
                | OpenFlags::SQLITE_OPEN_NO_MUTEX
                | OpenFlags::SQLITE_OPEN_NOFOLLOW,
        )
        .map_err(|_| Error::Corrupt)?;
        // Require a patched SQLite even though this serialized backend does not use WAL.
        if rusqlite::version_number() < 3_051_003 {
            return Err(Error::Storage);
        }
        if matches!(mode, OpenMode::Existing) {
            let app_id: i64 = connection
                .pragma_query_value(None, "application_id", |r| r.get(0))
                .map_err(|_| Error::Corrupt)?;
            let schema: i64 = connection
                .pragma_query_value(None, "user_version", |r| r.get(0))
                .map_err(|_| Error::Corrupt)?;
            if app_id != APPLICATION_ID || schema != 1 {
                return Err(Error::Corrupt);
            }
        }
        connection
            .busy_timeout(Duration::ZERO)
            .map_err(|_| Error::Storage)?;
        connection
            .execute_batch(
                "PRAGMA trusted_schema=OFF; PRAGMA temp_store=MEMORY;
            PRAGMA mmap_size=0; PRAGMA cache_size=-2048; PRAGMA fullfsync=ON;
            PRAGMA journal_mode=DELETE; PRAGMA synchronous=EXTRA;",
            )
            .map_err(|_| Error::Corrupt)?;
        connection
            .pragma_update(None, "max_page_count", limits.database_pages)
            .map_err(|_| Error::Storage)?;
        let journal: String = connection
            .pragma_query_value(None, "journal_mode", |r| r.get(0))
            .map_err(|_| Error::Storage)?;
        let sync: i64 = connection
            .pragma_query_value(None, "synchronous", |r| r.get(0))
            .map_err(|_| Error::Storage)?;
        let page_size: i64 = connection
            .pragma_query_value(None, "page_size", |r| r.get(0))
            .map_err(|_| Error::Storage)?;
        let page_limit: u32 = connection
            .pragma_query_value(None, "max_page_count", |r| r.get(0))
            .map_err(|_| Error::Storage)?;
        if journal != "delete"
            || sync != 3
            || page_size != 4096
            || page_limit != limits.database_pages
        {
            return Err(Error::Corrupt);
        }
        if matches!(mode, OpenMode::CreateNew) {
            let tx = connection
                .transaction_with_behavior(TransactionBehavior::Immediate)
                .map_err(|_| Error::Storage)?;
            tx.execute_batch(META_SQL).map_err(|_| Error::Storage)?;
            tx.execute_batch(RECORD_SQL).map_err(|_| Error::Storage)?;
            tx.execute(
                "INSERT INTO meta VALUES (1,0,?1)",
                [serde_json::to_string(&limits).map_err(|_| Error::Invalid)?],
            )
            .map_err(|_| Error::Storage)?;
            tx.pragma_update(None, "application_id", APPLICATION_ID)
                .map_err(|_| Error::Storage)?;
            tx.pragma_update(None, "user_version", 1)
                .map_err(|_| Error::Storage)?;
            tx.commit().map_err(|_| Error::CommitUncertain)?;
            directory.sync_all().map_err(|_| Error::CommitUncertain)?;
        }
        let app_id: i64 = connection
            .pragma_query_value(None, "application_id", |r| r.get(0))
            .map_err(|_| Error::Corrupt)?;
        let schema: i64 = connection
            .pragma_query_value(None, "user_version", |r| r.get(0))
            .map_err(|_| Error::Corrupt)?;
        let check: String = connection
            .query_row("PRAGMA integrity_check", [], |r| r.get(0))
            .map_err(|_| Error::Corrupt)?;
        if app_id != APPLICATION_ID || schema != 1 || check != "ok" {
            return Err(Error::Corrupt);
        }
        let definitions = {
            let mut stmt = connection.prepare("SELECT name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY name").map_err(|_| Error::Corrupt)?;
            stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))
                .map_err(|_| Error::Corrupt)?
                .collect::<std::result::Result<Vec<_>, _>>()
                .map_err(|_| Error::Corrupt)?
        };
        if definitions
            != vec![
                ("meta".to_owned(), META_SQL.to_owned()),
                ("records".to_owned(), RECORD_SQL.to_owned()),
            ]
        {
            return Err(Error::Corrupt);
        }
        let encoded: String = connection
            .query_row("SELECT limits FROM meta WHERE id=1", [], |r| r.get(0))
            .map_err(|_| Error::Corrupt)?;
        if serde_json::from_str::<Limits>(&encoded).map_err(|_| Error::Corrupt)? != limits {
            return Err(Error::Invalid);
        }
        let mut boot = [0; 32];
        File::open("/dev/urandom")
            .and_then(|mut f| f.read_exact(&mut boot))
            .map_err(|_| Error::Storage)?;
        let store = Self {
            connection,
            owner,
            directory,
            root,
            database_identity,
            boot,
            limits,
            poisoned: false,
        };
        store.verify_files()?;
        store.validate_records()?;
        Ok(store)
    }

    /// Trusted bootstrap API. Never map this method to an untrusted request.
    pub fn scope(&self, grants: BTreeMap<String, Access>) -> Result<Scope> {
        self.verify_files()?;
        Self::validate_grants(&grants)?;
        Ok(Scope {
            boot: self.boot,
            grants,
        })
    }

    pub fn validate_grants(grants: &BTreeMap<String, Access>) -> Result<()> {
        if grants.is_empty() || grants.len() > 64 || grants.keys().any(|n| !valid_name(n, 128)) {
            return Err(Error::Invalid);
        }
        Ok(())
    }

    fn verify_files(&self) -> Result<()> {
        if self.owner.process != std::process::id() {
            return Err(Error::Denied);
        }
        if self.poisoned {
            return Err(Error::ReopenRequired);
        }
        let directory = private_metadata(&self.root, true)?;
        let owner = private_metadata(&self.root.join("owner.lock"), false)?;
        if identity(&directory) != identity(&self.directory.metadata().map_err(|_| Error::Storage)?)
            || identity(&owner) != identity(&self.owner.metadata().map_err(|_| Error::Storage)?)
            || identity(&private_metadata(&self.root.join("records.sqlite"), false)?)
                != self.database_identity
        {
            return Err(Error::Denied);
        }
        Ok(())
    }

    fn authorize(
        &self,
        scope: &Scope,
        namespace: &str,
        key: &str,
        write: bool,
        revision: u64,
        deleting: bool,
    ) -> Result<()> {
        if scope.boot != self.boot {
            return Err(Error::Denied);
        }
        if !valid_name(namespace, 128) || !valid_name(key, 256) || revision > i64::MAX as u64 {
            return Err(Error::Invalid);
        }
        match scope.grants.get(namespace) {
            Some(Access::ReadWrite) => Ok(()),
            Some(Access::Read) if !write => Ok(()),
            Some(Access::CreateOnly) if write && revision == 0 && !deleting => Ok(()),
            _ => Err(Error::Denied),
        }
    }

    fn read_record(
        connection: &Connection,
        namespace: &str,
        key: &str,
        max: usize,
    ) -> Result<Record> {
        let row = connection
            .query_row(
                "SELECT revision,value,digest,length(value),length(digest) FROM records WHERE namespace=?1 AND key=?2",
                params![namespace, key],
                |r| {
                    let size: Option<i64> = r.get(3)?;
                    let digest_size: i64 = r.get(4)?;
                    if size.is_some_and(|n| n < 0 || n as u64 > max as u64) || digest_size != 32 {
                        return Err(rusqlite::Error::InvalidQuery);
                    }
                    Ok((
                        r.get::<_, i64>(0)?,
                        r.get::<_, Option<Vec<u8>>>(1)?,
                        r.get::<_, Vec<u8>>(2)?,
                    ))
                },
            )
            .optional()
            .map_err(|_| Error::Corrupt)?;
        let Some((revision, value, digest)) = row else {
            return Ok(Record {
                revision: 0,
                value: None,
            });
        };
        if revision <= 0 || value.as_ref().is_some_and(|v| v.len() > max) {
            return Err(Error::Corrupt);
        }
        let value = value
            .map(String::from_utf8)
            .transpose()
            .map_err(|_| Error::Corrupt)?;
        if digest != record_digest(namespace, key, revision as u64, value.as_deref()) {
            return Err(Error::Corrupt);
        }
        Ok(Record {
            revision: revision as u64,
            value,
        })
    }

    fn validate_records(&self) -> Result<()> {
        self.check_capacity()?;
        let mut stmt = self
            .connection
            .prepare("SELECT namespace,key FROM records")
            .map_err(|_| Error::Corrupt)?;
        let mut rows = stmt.query([]).map_err(|_| Error::Corrupt)?;
        while let Some(row) = rows.next().map_err(|_| Error::Corrupt)? {
            let namespace: String = row.get(0).map_err(|_| Error::Corrupt)?;
            let key: String = row.get(1).map_err(|_| Error::Corrupt)?;
            if !valid_name(&namespace, 128) || !valid_name(&key, 256) {
                return Err(Error::Corrupt);
            }
            Self::read_record(&self.connection, &namespace, &key, self.limits.value_bytes)?;
        }
        Ok(())
    }

    fn capacity(connection: &Connection, limits: &Limits) -> Result<()> {
        let (count, bytes): (i64,i64) = connection.query_row(
            "SELECT count(*),coalesce(sum(coalesce(length(value),0)+length(namespace)+length(key)+96),0) FROM records", [],
            |r| Ok((r.get(0)?,r.get(1)?))).map_err(|_| Error::Corrupt)?;
        if count < 0 || bytes < 0 {
            return Err(Error::Corrupt);
        }
        if count as u64 > limits.records || bytes as u64 > limits.live_bytes {
            return Err(Error::Limit);
        }
        Ok(())
    }

    fn check_capacity(&self) -> Result<()> {
        Self::capacity(&self.connection, &self.limits)
    }

    pub fn get(&self, scope: &Scope, namespace: &str, key: &str) -> Result<Record> {
        self.authorize(scope, namespace, key, false, 0, false)?;
        self.verify_files()?;
        Self::read_record(&self.connection, namespace, key, self.limits.value_bytes)
    }

    /// Read one bounded set in caller order from one SQLite read transaction.
    /// All addresses require read authority before any record is read. Missing,
    /// empty and tombstoned values preserve ordinary get() semantics. A failure
    /// returns no prefix; successful observations still require actual CAS on
    /// later writes. No claims, reservations, application parsing or writes occur.
    pub fn get_many(&mut self, scope: &Scope, keys: &[ReadKey]) -> Result<Vec<Record>> {
        self.get_many_inner(scope, keys, || {})
    }

    // A private test hook exercises an actual competing database commit. It is not
    // exposed through an embedding host or guest-controlled command.
    fn get_many_inner(
        &mut self,
        scope: &Scope,
        keys: &[ReadKey],
        after_first: impl FnOnce(),
    ) -> Result<Vec<Record>> {
        if keys.is_empty() || keys.len() > READ_SET_ITEMS {
            return Err(Error::Invalid);
        }
        let mut seen = BTreeSet::new();
        for key in keys {
            self.authorize(scope, &key.namespace, &key.key, false, 0, false)?;
            if !seen.insert((&key.namespace, &key.key)) {
                return Err(Error::Invalid);
            }
        }
        self.verify_files()?;
        let limit = self.limits.value_bytes;
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Deferred)
            .map_err(|_| Error::Storage)?;
        // Check the aggregate inside the SAME snapshot before allocating any
        // payload. Each record also retains the configured single-value ceiling.
        let mut total = 0usize;
        for key in keys {
            let bytes: Option<Option<i64>> = tx
                .query_row(
                    "SELECT length(value) FROM records WHERE namespace=?1 AND key=?2",
                    params![key.namespace, key.key],
                    |row| row.get(0),
                )
                .optional()
                .map_err(|_| Error::Corrupt)?;
            let bytes = bytes.flatten().unwrap_or(0);
            if bytes < 0 || bytes as u64 > limit as u64 {
                return Err(Error::Corrupt);
            }
            total = total.checked_add(bytes as usize).ok_or(Error::Limit)?;
            if total > READ_SET_VALUE_BYTES {
                return Err(Error::Limit);
            }
        }
        let mut result = Vec::with_capacity(keys.len());
        let mut after_first = Some(after_first);
        for key in keys {
            result.push(Self::read_record(&tx, &key.namespace, &key.key, limit)?);
            if let Some(callback) = after_first.take() {
                callback();
            }
        }
        tx.commit().map_err(|_| Error::Storage)?;
        self.verify_files()?;
        Ok(result)
    }

    /// List one bounded page in a single explicitly read-authorized namespace.
    /// Tombstones remain discoverable. No values, revisions or domain decisions
    /// are returned; consumers must read current records and use actual CAS.
    pub fn keys(
        &self,
        scope: &Scope,
        namespace: &str,
        after: Option<&str>,
        limit: usize,
    ) -> Result<KeyPage> {
        let page = self.metadata(scope, namespace, after, limit)?;
        Ok(KeyPage {
            keys: page.entries.into_iter().map(|entry| entry.key).collect(),
            next: page.next,
        })
    }

    /// Reuse the same scoped bounded scan and integrity checks as key discovery.
    /// The host reports presence/size/revision; applications decide visibility.
    pub fn metadata(
        &self,
        scope: &Scope,
        namespace: &str,
        after: Option<&str>,
        limit: usize,
    ) -> Result<MetadataPage> {
        if !(1..=128).contains(&limit) || after.is_some_and(|v| !valid_name(v, 256)) {
            return Err(Error::Invalid);
        }
        self.authorize(scope, namespace, after.unwrap_or("0"), false, 0, false)?;
        self.verify_files()?;
        // Bound allocation even if storage is corrupted with an oversized key.
        let mut query = self.connection.prepare(
            "SELECT substr(key,1,257),length(CAST(key AS BLOB)) FROM records WHERE namespace=?1 AND key>?2 COLLATE BINARY ORDER BY key COLLATE BINARY LIMIT ?3"
        ).map_err(|_| Error::Corrupt)?;
        let rows = query
            .query_map(params![namespace, after.unwrap_or(""), limit as i64], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?))
            })
            .map_err(|_| Error::Corrupt)?;
        let mut entries: Vec<RecordMetadata> = Vec::new();
        for row in rows {
            let (key, bytes) = row.map_err(|_| Error::Corrupt)?;
            if !valid_name(&key, 256)
                || bytes != key.len() as i64
                || entries
                    .last()
                    .map(|entry| entry.key.as_str())
                    .unwrap_or(after.unwrap_or(""))
                    >= key.as_str()
            {
                return Err(Error::Corrupt);
            }
            // Reuse normal bounded record integrity checks before returning a
            // discovered key. These checks do not make the page a durable claim.
            let record =
                Self::read_record(&self.connection, namespace, &key, self.limits.value_bytes)?;
            if record.revision == 0 {
                return Err(Error::Corrupt);
            }
            entries.push(RecordMetadata {
                key,
                revision: record.revision,
                present: record.value.is_some(),
                value_bytes: record.value.as_ref().map_or(0, String::len),
            });
        }
        let next = (entries.len() == limit)
            .then(|| entries.last().map(|entry| entry.key.clone()))
            .flatten();
        Ok(MetadataPage { entries, next })
    }

    /// Check only whether a create-authorized slot has never been used. Does not
    /// expose retained bytes/revisions, reserve the slot, or grant read access.
    /// The caller must retain exclusive store ownership until its causal action.
    pub fn unused_create_slot(&self, scope: &Scope, namespace: &str, key: &str) -> Result<bool> {
        self.authorize(scope, namespace, key, true, 0, false)?;
        self.verify_files()?;
        let record = Self::read_record(&self.connection, namespace, key, self.limits.value_bytes)?;
        Ok(record.revision == 0 && record.value.is_none())
    }

    pub fn commit(&mut self, scope: &Scope, batch: &Batch) -> Result<Receipt> {
        self.commit_inner(scope, batch, || {})
    }

    // The private callback enables real process-kill tests; no runtime fault knob
    // or untrusted SQL execution is exposed by the public API or stdio adapter.
    fn commit_inner(
        &mut self,
        scope: &Scope,
        batch: &Batch,
        before_commit: impl FnOnce(),
    ) -> Result<Receipt> {
        if batch.writes.is_empty()
            || batch.writes.len() + batch.checks.len() > self.limits.batch_items
        {
            return Err(Error::Invalid);
        }
        let mut seen = BTreeSet::new();
        let mut bytes = 0usize;
        for check in &batch.checks {
            self.authorize(
                scope,
                &check.namespace,
                &check.key,
                false,
                check.revision,
                false,
            )?;
            if !seen.insert((&check.namespace, &check.key)) {
                return Err(Error::Invalid);
            }
        }
        for write in &batch.writes {
            self.authorize(
                scope,
                &write.namespace,
                &write.key,
                true,
                write.revision,
                write.value.is_none(),
            )?;
            if !seen.insert((&write.namespace, &write.key)) {
                return Err(Error::Invalid);
            }
            let size = write.value.as_ref().map_or(0, String::len);
            if size > self.limits.value_bytes {
                return Err(Error::Limit);
            }
            bytes = bytes.checked_add(size).ok_or(Error::Limit)?;
        }
        if bytes > self.limits.batch_bytes {
            return Err(Error::Limit);
        }
        self.verify_files()?;
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|_| Error::Storage)?;
        for check in &batch.checks {
            if Self::read_record(&tx, &check.namespace, &check.key, self.limits.value_bytes)?
                .revision
                != check.revision
            {
                return Err(Error::Conflict);
            }
        }
        for write in &batch.writes {
            let old =
                Self::read_record(&tx, &write.namespace, &write.key, self.limits.value_bytes)?;
            if old.revision != write.revision {
                return Err(Error::Conflict);
            }
            if old.revision == i64::MAX as u64 {
                return Err(Error::Limit);
            }
        }
        let revision: i64 = tx
            .query_row("SELECT revision FROM meta WHERE id=1", [], |r| r.get(0))
            .map_err(|_| Error::Corrupt)?;
        if revision < 0 {
            return Err(Error::Corrupt);
        }
        if revision == i64::MAX {
            return Err(Error::Limit);
        }
        for write in &batch.writes {
            if tx.execute("INSERT INTO records(namespace,key,revision,value,digest) VALUES (?1,?2,?3,?4,?5)
                ON CONFLICT(namespace,key) DO UPDATE SET revision=excluded.revision,value=excluded.value,digest=excluded.digest",
                params![write.namespace,write.key,(write.revision+1) as i64,write.value.as_ref().map(|v| v.as_bytes()),record_digest(&write.namespace,&write.key,write.revision+1,write.value.as_deref())]).is_err() {
                self.poisoned = true;
                return Err(Error::Storage);
            }
        }
        Self::capacity(&tx, &self.limits)?;
        if tx
            .execute("UPDATE meta SET revision=?1 WHERE id=1", [revision + 1])
            .is_err()
        {
            self.poisoned = true;
            return Err(Error::Storage);
        }
        before_commit();
        if tx.commit().is_err() {
            self.poisoned = true;
            return Err(Error::CommitUncertain);
        }
        Ok(Receipt {
            revision: (revision + 1) as u64,
        })
    }
}

#[cfg(test)]
#[path = "store_tests.rs"]
mod tests;

#[cfg(test)]
#[path = "readset_tests.rs"]
mod readset_tests;
