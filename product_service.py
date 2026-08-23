#!/usr/bin/env python3
"""Authenticated, tenant-scoped HTTP boundary for the sigil-pi v1 product.

The historical ``agent.serve`` endpoint is deliberately loopback-only and is
kept as a research/demo surface.  This module is the product contract: every
route authenticates before dispatch, tenant identity comes from the credential
rather than request JSON, and the external session name is irreversibly
namespaced before it reaches PiAgent's durable state.

This is intentionally dependency-free so the security boundary stays small.
It is not, by itself, the complete production deployment: artifact packaging,
multi-process storage, backup/restore, and the external readiness evidence are
tracked in docs/product-readiness.md.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import resource
import shutil
import sqlite3
import ssl
import sys
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import closing, contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote


API_VERSION = "v1"
DEFAULT_VERSION = "0.1.0-dev"
MAX_AUTH_TOKEN_BYTES = 4096
MAX_PRODUCT_REQUEST_BYTES = 1024 * 1024
MAX_SESSION_BYTES = 128
MAX_MESSAGE_BYTES = 256 * 1024
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
HISTOGRAM_BUCKETS_MS = (
    10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2000.0,
    5000.0, 10000.0, 30000.0, 60000.0, 120000.0,
)
PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
KNOWN_SCOPES = frozenset({
    "chat",
    "ops:read",
    "schedules:read",
    "schedules:write",
    "sessions:read",
    "sessions:delete",
})


class ConfigError(ValueError):
    """Product configuration is unsafe or ambiguous."""


class ProductError(Exception):
    def __init__(self, status: int, code: str, message: str, *, retry_after=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


@dataclass(frozen=True)
class Principal:
    principal_id: str
    tenant_id: str
    scopes: frozenset[str]
    tools: frozenset[str]


@dataclass(frozen=True)
class Credential:
    digest: str
    principal: Principal
    not_before_unix: int | None
    expires_unix: int | None


@dataclass(frozen=True)
class PrometheusPayload:
    body: str


class AuthRegistry:
    """Bearer-token verifier backed by SHA-256 token digests.

    The configuration contains digests, never bearer tokens. Tokens must be
    high-entropy credentials supplied through a secret manager; hashing is not
    a substitute for entropy, but it prevents the auth file from being an
    immediately usable bearer-token list.
    """

    def __init__(self, entries, *, clock=None, require_expiry=False,
                 max_lifetime_s=None):
        parsed = []
        seen_hashes = set()
        identity_policies = {}
        self.clock = clock or time.time
        if max_lifetime_s is not None and max_lifetime_s <= 0:
            raise ConfigError("maximum credential lifetime must be positive")
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ConfigError(f"auth entry {i} must be an object")
            digest = entry.get("sha256")
            principal = entry.get("principal")
            tenant = entry.get("tenant")
            scopes = entry.get("scopes")
            tools = entry.get("tools", [])
            if not isinstance(digest, str) or not HASH_RE.fullmatch(digest):
                raise ConfigError(f"auth entry {i} sha256 must be 64 lowercase hex characters")
            if digest in seen_hashes:
                raise ConfigError(f"auth entry {i} duplicates a token digest")
            if not isinstance(principal, str) or not principal.strip():
                raise ConfigError(f"auth entry {i} principal must be a non-empty string")
            if not isinstance(tenant, str) or not tenant.strip():
                raise ConfigError(f"auth entry {i} tenant must be a non-empty string")
            if not isinstance(scopes, list) or not all(isinstance(v, str) for v in scopes):
                raise ConfigError(f"auth entry {i} scopes must be a string list")
            unknown = set(scopes) - KNOWN_SCOPES
            if unknown:
                raise ConfigError(f"auth entry {i} has unknown scopes: {sorted(unknown)}")
            if not isinstance(tools, list) or not all(isinstance(v, str) for v in tools):
                raise ConfigError(f"auth entry {i} tools must be a string list")
            if "*" in tools and len(tools) != 1:
                raise ConfigError(f"auth entry {i} tools '*' must appear alone")
            not_before = entry.get("not_before_unix")
            expires = entry.get("expires_unix")
            if require_expiry and (not_before is None or expires is None):
                raise ConfigError(
                    f"auth entry {i} requires not_before_unix and expires_unix")
            if (not_before is None) != (expires is None):
                raise ConfigError(
                    f"auth entry {i} validity requires both not_before_unix and expires_unix")
            if not_before is not None:
                if (not isinstance(not_before, int) or isinstance(not_before, bool)
                        or not isinstance(expires, int) or isinstance(expires, bool)):
                    raise ConfigError(
                        f"auth entry {i} validity timestamps must be integer Unix seconds")
                if not_before < 0 or expires <= not_before:
                    raise ConfigError(
                        f"auth entry {i} expires_unix must be after not_before_unix")
                if (max_lifetime_s is not None
                        and expires - not_before > max_lifetime_s):
                    raise ConfigError(
                        f"auth entry {i} exceeds the maximum credential lifetime")
            identity = (hashlib.sha256(tenant.strip().encode()).hexdigest(),
                        principal.strip())
            policy = (frozenset(scopes), frozenset(tools))
            if identity in identity_policies and identity_policies[identity] != policy:
                raise ConfigError(
                    f"auth entry {i} conflicts with another credential policy for "
                    f"principal {principal!r}")
            seen_hashes.add(digest)
            identity_policies[identity] = policy
            parsed.append(Credential(
                digest, Principal(
                    principal.strip(), tenant.strip(), frozenset(scopes), frozenset(tools)),
                not_before, expires))
        if not parsed:
            raise ConfigError("auth registry must contain at least one credential")
        now = self.clock()
        if require_expiry and not any(self._active(record, now) for record in parsed):
            raise ConfigError("auth registry has no currently active credential")
        self._entries = tuple(parsed)

    @property
    def principals(self):
        return tuple(record.principal for record in self._entries)

    def _active(self, record, now=None):
        if record.not_before_unix is None:
            return True
        now = self.clock() if now is None else now
        return record.not_before_unix <= now < record.expires_unix

    @classmethod
    def from_file(cls, path, **kwargs):
        path = Path(path)
        try:
            doc = json.loads(path.read_text())
        except FileNotFoundError as e:
            raise ConfigError(f"auth file does not exist: {path}") from e
        except json.JSONDecodeError as e:
            raise ConfigError(f"auth file is not valid JSON: {path}: {e}") from e
        if not isinstance(doc, dict) or not isinstance(doc.get("tokens"), list):
            raise ConfigError("auth file must be an object with a tokens list")
        return cls(doc["tokens"], **kwargs)

    def authenticate(self, authorization: str | None) -> Principal:
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise ProductError(401, "authentication_required", "bearer authentication required")
        token = authorization[len("Bearer "):]
        raw = token.encode()
        if not token or len(raw) > MAX_AUTH_TOKEN_BYTES:
            raise ProductError(401, "invalid_credential", "invalid bearer credential")
        candidate = hashlib.sha256(raw).hexdigest()
        # Do not use a dict lookup: compare every configured digest so matching
        # position is not observable through this process's comparison path.
        match = None
        now = self.clock()
        for record in self._entries:
            if (hmac.compare_digest(candidate, record.digest)
                    and self._active(record, now)):
                match = record.principal
        if match is None:
            raise ProductError(401, "invalid_credential", "invalid bearer credential")
        return match

    def policy(self, tenant_sha256: str, principal_id: str) -> Principal | None:
        now = self.clock()
        matches = [record.principal for record in self._entries
                   if self._active(record, now)
                   and record.principal.principal_id == principal_id
                   and hashlib.sha256(record.principal.tenant_id.encode()).hexdigest()
                   == tenant_sha256]
        return matches[0] if matches else None


class FixedWindowRateLimiter:
    """Bounded per-tenant request rate with an injectable monotonic clock."""

    def __init__(self, requests_per_minute: int, clock=None):
        if requests_per_minute <= 0:
            raise ConfigError("requests_per_minute must be positive")
        self.limit = requests_per_minute
        self.clock = clock or time.monotonic
        self._events = defaultdict(deque)
        self._lock = threading.Lock()

    def admit(self, tenant_id: str) -> tuple[bool, int]:
        now = self.clock()
        cutoff = now - 60.0
        with self._lock:
            events = self._events[tenant_id]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                retry = max(1, int(61 - (now - events[0])))
                return False, retry
            events.append(now)
            return True, 0


class TenantConcurrency:
    def __init__(self, limit: int):
        if limit <= 0:
            raise ConfigError("max_concurrent_turns must be positive")
        self.limit = limit
        self._semaphores = {}
        self._lock = threading.Lock()

    def _for(self, tenant_id):
        with self._lock:
            return self._semaphores.setdefault(
                tenant_id, threading.BoundedSemaphore(self.limit))

    def acquire(self, tenant_id):
        semaphore = self._for(tenant_id)
        if not semaphore.acquire(blocking=False):
            raise ProductError(429, "tenant_busy", "tenant concurrency limit reached",
                               retry_after=1)
        return semaphore


class _DurableTurnLease:
    def __init__(self, store, lease_id):
        self.store = store
        self.lease_id = lease_id
        self._released = False

    def release(self, *, usage=None, session_id=None,
                storage_bytes=None, audit_bytes=None, last_activity_unix=None):
        if not self._released:
            self.store.settle_turn(
                self.lease_id, usage=usage, session_id=session_id,
                storage_bytes=storage_bytes, audit_bytes=audit_bytes,
                last_activity_unix=last_activity_unix)
            self._released = True


class DurableQuotaStore:
    """Cross-worker tenant request, concurrency, and resource quotas.

    Resource quota admission reserves a conservative per-turn allowance so
    concurrent workers cannot each observe the same remaining capacity. On
    completion the reservation is replaced by actual model usage and exact
    session/audit file sizes. Tenant and session identities are hashed before
    entering the operational database.
    """

    def __init__(self, path, *, requests_per_minute=60, max_concurrent_turns=2,
                 turn_lease_s=300, tokens_per_day=None,
                 token_reservation_per_turn=None, storage_bytes_per_tenant=None,
                 storage_reservation_per_turn=None, audit_bytes_per_tenant=None,
                 audit_reservation_per_turn=None, clock=None):
        if requests_per_minute <= 0 or max_concurrent_turns <= 0 or turn_lease_s <= 0:
            raise ConfigError("durable quota limits and lease must be positive")
        resource_values = (
            tokens_per_day, token_reservation_per_turn,
            storage_bytes_per_tenant, storage_reservation_per_turn,
            audit_bytes_per_tenant, audit_reservation_per_turn,
        )
        configured = any(value is not None for value in resource_values)
        if configured and any(value is None for value in resource_values):
            raise ConfigError("all durable token/storage/audit quota limits are required")
        if configured and any(value <= 0 for value in resource_values):
            raise ConfigError("durable token/storage/audit quota limits must be positive")
        if configured and (
                token_reservation_per_turn > tokens_per_day
                or storage_reservation_per_turn > storage_bytes_per_tenant
                or audit_reservation_per_turn > audit_bytes_per_tenant):
            raise ConfigError("per-turn resource reservations must not exceed tenant limits")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.requests_per_minute = requests_per_minute
        self.max_concurrent_turns = max_concurrent_turns
        self.turn_lease_s = turn_lease_s
        self.tokens_per_day = tokens_per_day
        self.token_reservation_per_turn = token_reservation_per_turn or 0
        self.storage_bytes_per_tenant = storage_bytes_per_tenant
        self.storage_reservation_per_turn = storage_reservation_per_turn or 0
        self.audit_bytes_per_tenant = audit_bytes_per_tenant
        self.audit_reservation_per_turn = audit_reservation_per_turn or 0
        self.resource_limits_enabled = configured
        self.clock = clock or time.time
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("""CREATE TABLE IF NOT EXISTS request_windows (
                tenant TEXT PRIMARY KEY, window_start INTEGER NOT NULL, count INTEGER NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS turn_leases (
                lease_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, expires REAL NOT NULL,
                token_reservation INTEGER NOT NULL DEFAULT 0,
                storage_reservation INTEGER NOT NULL DEFAULT 0,
                audit_reservation INTEGER NOT NULL DEFAULT 0
            )""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(turn_leases)")}
            for column in ("token_reservation", "storage_reservation",
                           "audit_reservation"):
                if column not in columns:
                    db.execute(
                        f"ALTER TABLE turn_leases ADD COLUMN {column} "
                        "INTEGER NOT NULL DEFAULT 0")
            db.execute("CREATE INDEX IF NOT EXISTS turn_leases_tenant ON turn_leases(tenant)")
            db.execute("""CREATE TABLE IF NOT EXISTS token_windows (
                tenant TEXT PRIMARY KEY, window_start INTEGER NOT NULL, tokens INTEGER NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS session_usage (
                tenant TEXT NOT NULL, session TEXT NOT NULL,
                internal_session TEXT, storage_bytes INTEGER NOT NULL,
                audit_bytes INTEGER NOT NULL,
                last_activity_unix REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(tenant, session)
            )""")
            usage_columns = {
                row[1] for row in db.execute("PRAGMA table_info(session_usage)")}
            if "internal_session" not in usage_columns:
                db.execute("ALTER TABLE session_usage ADD COLUMN internal_session TEXT")
            if "last_activity_unix" not in usage_columns:
                db.execute(
                    "ALTER TABLE session_usage ADD COLUMN last_activity_unix "
                    "REAL NOT NULL DEFAULT 0")
            db.execute("DELETE FROM turn_leases WHERE expires<=?", (self.clock(),))

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5, isolation_level=None)

    def is_healthy(self):
        """Cheap, read-only schema and SQLite integrity probe for readiness."""
        expected = {
            "request_windows", "turn_leases", "token_windows", "session_usage"}
        try:
            with closing(self._connect()) as db:
                tables = {
                    row[0] for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")}
                quick_check = db.execute("PRAGMA quick_check(1)").fetchone()
            return expected <= tables and quick_check == ("ok",)
        except (OSError, sqlite3.Error):
            return False

    @staticmethod
    def _tenant(tenant_id):
        return hashlib.sha256(tenant_id.encode()).hexdigest()

    def admit_request(self, tenant_id):
        now = self.clock()
        window = int(now // 60) * 60
        tenant = self._tenant(tenant_id)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT window_start, count FROM request_windows WHERE tenant=?",
                (tenant,)).fetchone()
            count = 0 if row is None or row[0] != window else row[1]
            if count >= self.requests_per_minute:
                db.execute("ROLLBACK")
                return False, max(1, int(window + 60 - now))
            db.execute(
                "INSERT INTO request_windows(tenant, window_start, count) VALUES(?,?,?) "
                "ON CONFLICT(tenant) DO UPDATE SET window_start=excluded.window_start, "
                "count=excluded.count",
                (tenant, window, count + 1))
            db.execute("COMMIT")
        return True, 0

    def acquire_turn(self, tenant_id, *, session_id=None):
        now = self.clock()
        tenant = self._tenant(tenant_id)
        lease_id = uuid.uuid4().hex
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM turn_leases WHERE expires<=?", (now,))
            active = db.execute(
                "SELECT COUNT(*) FROM turn_leases WHERE tenant=?", (tenant,)).fetchone()[0]
            if active >= self.max_concurrent_turns:
                db.execute("ROLLBACK")
                raise ProductError(429, "tenant_busy", "tenant concurrency limit reached",
                                   retry_after=1)
            if self.resource_limits_enabled:
                day = int(now // 86400) * 86400
                token_row = db.execute(
                    "SELECT window_start, tokens FROM token_windows WHERE tenant=?",
                    (tenant,)).fetchone()
                tokens = 0 if token_row is None or token_row[0] != day else token_row[1]
                reserved_tokens = db.execute(
                    "SELECT COALESCE(SUM(token_reservation),0) FROM turn_leases "
                    "WHERE tenant=?", (tenant,)).fetchone()[0]
                if tokens + reserved_tokens + self.token_reservation_per_turn > (
                        self.tokens_per_day):
                    db.execute("ROLLBACK")
                    raise ProductError(
                        429, "token_quota_exceeded", "tenant daily token quota exceeded",
                        retry_after=max(1, int(day + 86400 - now)))
                storage = db.execute(
                    "SELECT COALESCE(SUM(storage_bytes),0) FROM session_usage "
                    "WHERE tenant=?", (tenant,)).fetchone()[0]
                reserved_storage = db.execute(
                    "SELECT COALESCE(SUM(storage_reservation),0) FROM turn_leases "
                    "WHERE tenant=?", (tenant,)).fetchone()[0]
                if storage + reserved_storage + self.storage_reservation_per_turn > (
                        self.storage_bytes_per_tenant):
                    db.execute("ROLLBACK")
                    raise ProductError(
                        507, "storage_quota_exceeded", "tenant storage quota exceeded")
                audit = db.execute(
                    "SELECT COALESCE(SUM(audit_bytes),0) FROM session_usage "
                    "WHERE tenant=?", (tenant,)).fetchone()[0]
                reserved_audit = db.execute(
                    "SELECT COALESCE(SUM(audit_reservation),0) FROM turn_leases "
                    "WHERE tenant=?", (tenant,)).fetchone()[0]
                if audit + reserved_audit + self.audit_reservation_per_turn > (
                        self.audit_bytes_per_tenant):
                    db.execute("ROLLBACK")
                    raise ProductError(
                        507, "audit_quota_exceeded", "tenant audit quota exceeded")
                if session_id is not None:
                    session = hashlib.sha256(session_id.encode()).hexdigest()
                    db.execute(
                        "INSERT INTO session_usage(tenant, session, internal_session, "
                        "storage_bytes, audit_bytes, last_activity_unix) "
                        "VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(tenant,session) DO UPDATE SET "
                        "internal_session=excluded.internal_session, "
                        "last_activity_unix=MAX(session_usage.last_activity_unix, "
                        "excluded.last_activity_unix)",
                        (tenant, session, session_id, 0, 0, now))
            db.execute(
                "INSERT INTO turn_leases(lease_id, tenant, expires, token_reservation, "
                "storage_reservation, audit_reservation) VALUES(?,?,?,?,?,?)",
                (lease_id, tenant, now + self.turn_lease_s,
                 self.token_reservation_per_turn, self.storage_reservation_per_turn,
                 self.audit_reservation_per_turn))
            db.execute("COMMIT")
        return _DurableTurnLease(self, lease_id)

    def settle_turn(self, lease_id, *, usage=None, session_id=None,
                    storage_bytes=None, audit_bytes=None,
                    last_activity_unix=None):
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT tenant FROM turn_leases WHERE lease_id=?", (lease_id,)).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                return False
            tenant = row[0]
            db.execute("DELETE FROM turn_leases WHERE lease_id=?", (lease_id,))
            if self.resource_limits_enabled:
                actual_tokens = 0
                if usage:
                    actual_tokens = max(0, int(usage.get("input_tokens", 0)))
                    actual_tokens += max(0, int(usage.get("output_tokens", 0)))
                if actual_tokens:
                    now = self.clock()
                    day = int(now // 86400) * 86400
                    current = db.execute(
                        "SELECT window_start, tokens FROM token_windows WHERE tenant=?",
                        (tenant,)).fetchone()
                    prior = 0 if current is None or current[0] != day else current[1]
                    db.execute(
                        "INSERT INTO token_windows(tenant, window_start, tokens) "
                        "VALUES(?,?,?) ON CONFLICT(tenant) DO UPDATE SET "
                        "window_start=excluded.window_start, tokens=excluded.tokens",
                        (tenant, day, prior + actual_tokens))
                if (session_id is not None and storage_bytes is not None
                        and audit_bytes is not None):
                    storage_bytes = max(0, int(storage_bytes))
                    audit_bytes = max(0, int(audit_bytes))
                    session = hashlib.sha256(session_id.encode()).hexdigest()
                    activity = self.clock()
                    if last_activity_unix is not None:
                        activity = max(activity, float(last_activity_unix))
                    db.execute(
                        "INSERT INTO session_usage(tenant, session, internal_session, "
                        "storage_bytes, audit_bytes, last_activity_unix) "
                        "VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(tenant,session) DO UPDATE SET "
                        "internal_session=excluded.internal_session, "
                        "storage_bytes=excluded.storage_bytes, "
                        "audit_bytes=excluded.audit_bytes, "
                        "last_activity_unix=MAX(session_usage.last_activity_unix, "
                        "excluded.last_activity_unix)",
                        (tenant, session, session_id, storage_bytes, audit_bytes,
                         activity))
            db.execute("COMMIT")
            return True

    def release_turn(self, lease_id):
        """Compatibility wrapper for callers that do not settle resources."""
        return self.settle_turn(lease_id)

    def remove_session_usage(self, tenant_id, session_id):
        tenant = self._tenant(tenant_id)
        session = hashlib.sha256(session_id.encode()).hexdigest()
        with closing(self._connect()) as db:
            db.execute(
                "DELETE FROM session_usage WHERE tenant=? AND session=?",
                (tenant, session))

    def tenant_usage(self, tenant_id):
        """Read one tenant's counters for tests/operator diagnostics, without labels."""
        tenant = self._tenant(tenant_id)
        now = self.clock()
        day = int(now // 86400) * 86400
        with closing(self._connect()) as db:
            token_row = db.execute(
                "SELECT window_start, tokens FROM token_windows WHERE tenant=?",
                (tenant,)).fetchone()
            tokens = 0 if token_row is None or token_row[0] != day else token_row[1]
            storage, audit = db.execute(
                "SELECT COALESCE(SUM(storage_bytes),0), COALESCE(SUM(audit_bytes),0) "
                "FROM session_usage WHERE tenant=?", (tenant,)).fetchone()
            reserved = db.execute(
                "SELECT COALESCE(SUM(token_reservation),0), "
                "COALESCE(SUM(storage_reservation),0), "
                "COALESCE(SUM(audit_reservation),0) FROM turn_leases WHERE tenant=?",
                (tenant,)).fetchone()
        return {
            "tokens_today": tokens,
            "storage_bytes": storage,
            "audit_bytes": audit,
            "reserved_tokens": reserved[0],
            "reserved_storage_bytes": reserved[1],
            "reserved_audit_bytes": reserved[2],
        }

    def aggregate_usage(self):
        """Low-cardinality totals for authenticated operational metrics."""
        now = self.clock()
        day = int(now // 86400) * 86400
        with closing(self._connect()) as db:
            tokens = db.execute(
                "SELECT COALESCE(SUM(tokens),0) FROM token_windows "
                "WHERE window_start=?", (day,)).fetchone()[0]
            storage, audit = db.execute(
                "SELECT COALESCE(SUM(storage_bytes),0), "
                "COALESCE(SUM(audit_bytes),0) FROM session_usage").fetchone()
            leases, reserved_tokens, reserved_storage, reserved_audit = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(token_reservation),0), "
                "COALESCE(SUM(storage_reservation),0), "
                "COALESCE(SUM(audit_reservation),0) FROM turn_leases").fetchone()
        return {
            "tokens_today": tokens,
            "storage_bytes": storage,
            "audit_bytes": audit,
            "active_leases": leases,
            "reserved_tokens": reserved_tokens,
            "reserved_storage_bytes": reserved_storage,
            "reserved_audit_bytes": reserved_audit,
        }

    def registered_sessions(self):
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT tenant, session, internal_session FROM session_usage "
                "ORDER BY tenant, session").fetchall()
        if any(not isinstance(row[2], str) or not row[2] for row in rows):
            raise ConfigError(
                "quota usage contains an unmigrated session without registry identity")
        return tuple(rows)

    def reconcile_registered(self, measure):
        """Repair exact file usage after crashes before accepting traffic."""
        rows = self.registered_sessions()
        measured = []
        for tenant, session, internal_session in rows:
            footprint = measure(internal_session)
            measured.append((
                max(0, int(footprint["storage_bytes"])),
                max(0, int(footprint["audit_bytes"])),
                max(0.0, float(footprint.get("last_activity_unix", 0))),
                tenant, session))
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany(
                "UPDATE session_usage SET storage_bytes=?, audit_bytes=?, "
                "last_activity_unix=MAX(last_activity_unix, ?) "
                "WHERE tenant=? AND session=?", measured)
            db.execute("COMMIT")
        return len(measured)

    def inactive_sessions(self, cutoff_unix):
        with closing(self._connect()) as db:
            return tuple(db.execute(
                "SELECT tenant, session, internal_session, last_activity_unix "
                "FROM session_usage WHERE last_activity_unix<=? "
                "ORDER BY tenant, session", (cutoff_unix,)).fetchall())

    def registered_session_inactive(self, tenant_sha256, session_sha256,
                                    internal_session, cutoff_unix):
        """Re-check a sweep candidate after acquiring its operation lock."""
        with closing(self._connect()) as db:
            return db.execute(
                "SELECT 1 FROM session_usage WHERE tenant=? AND session=? "
                "AND internal_session=? AND last_activity_unix<=?",
                (tenant_sha256, session_sha256, internal_session,
                 cutoff_unix)).fetchone() is not None

    def remove_registered_session(self, tenant_sha256, session_sha256,
                                  *, cutoff_unix=None):
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            if cutoff_unix is None:
                result = db.execute(
                    "DELETE FROM session_usage WHERE tenant=? AND session=?",
                    (tenant_sha256, session_sha256))
            else:
                result = db.execute(
                    "DELETE FROM session_usage WHERE tenant=? AND session=? "
                    "AND last_activity_unix<=?",
                    (tenant_sha256, session_sha256, cutoff_unix))
            db.execute("COMMIT")
            return result.rowcount == 1


class ServiceMetrics:
    """Low-cardinality process metrics; no principal, tenant, or session labels."""

    def __init__(self):
        self._lock = threading.Lock()
        self._started = time.time()
        self._requests = defaultdict(int)
        self._turns = 0
        self._turn_errors = 0
        self._turn_duration_ms = 0.0
        self._queue_wait_ms = 0.0
        self._queue_wait_ms_max = 0.0
        self._turn_duration_buckets = [0] * len(HISTOGRAM_BUCKETS_MS)
        self._queue_wait_buckets = [0] * len(HISTOGRAM_BUCKETS_MS)
        self._input_tokens = 0
        self._output_tokens = 0
        self._tool_calls = 0
        self._retries = 0
        self._active_turns = 0
        self._error_classes = defaultdict(int)

    def request(self, method, route, status, error_class=None):
        with self._lock:
            self._requests[(method, route, str(status))] += 1
            if error_class:
                self._error_classes[error_class] += 1

    def turn_start(self):
        with self._lock:
            self._active_turns += 1

    def turn_finish(self, duration_ms, *, queue_wait_ms=0.0, usage=None,
                    tool_calls=0, retries=0, error=False):
        duration_ms = float(duration_ms)
        queue_wait_ms = float(queue_wait_ms)
        if not math.isfinite(duration_ms) or duration_ms < 0:
            duration_ms = 0.0
        if not math.isfinite(queue_wait_ms) or queue_wait_ms < 0:
            queue_wait_ms = 0.0
        with self._lock:
            self._active_turns -= 1
            self._turns += 1
            self._turn_duration_ms += duration_ms
            self._queue_wait_ms += queue_wait_ms
            self._queue_wait_ms_max = max(self._queue_wait_ms_max, queue_wait_ms)
            for index, bound in enumerate(HISTOGRAM_BUCKETS_MS):
                if duration_ms <= bound:
                    self._turn_duration_buckets[index] += 1
                if queue_wait_ms <= bound:
                    self._queue_wait_buckets[index] += 1
            if usage:
                self._input_tokens += max(0, int(usage.get("input_tokens", 0)))
                self._output_tokens += max(0, int(usage.get("output_tokens", 0)))
            self._tool_calls += max(0, int(tool_calls))
            self._retries += max(0, int(retries))
            if error:
                self._turn_errors += 1

    def snapshot(self):
        with self._lock:
            requests = [
                {"method": k[0], "route": k[1], "status": int(k[2]), "count": count}
                for k, count in sorted(self._requests.items())
            ]
            def histogram(counts, total, sum_value):
                return {
                    "buckets": [
                        {"le": bound, "count": counts[index]}
                        for index, bound in enumerate(HISTOGRAM_BUCKETS_MS)
                    ],
                    "count": total,
                    "sum": round(sum_value, 3),
                }

            return {
                "started_at": self._started,
                "uptime_seconds": max(0.0, time.time() - self._started),
                "requests": requests,
                "turns_total": self._turns,
                "turn_errors_total": self._turn_errors,
                "turn_duration_ms_total": round(self._turn_duration_ms, 3),
                "queue_wait_ms_total": round(self._queue_wait_ms, 3),
                "queue_wait_ms_max": round(self._queue_wait_ms_max, 3),
                "turn_duration_ms": histogram(
                    self._turn_duration_buckets, self._turns,
                    self._turn_duration_ms),
                "queue_wait_ms": histogram(
                    self._queue_wait_buckets, self._turns,
                    self._queue_wait_ms),
                "input_tokens_total": self._input_tokens,
                "output_tokens_total": self._output_tokens,
                "tool_calls_total": self._tool_calls,
                "retries_total": self._retries,
                "active_turns": self._active_turns,
                "error_classes": dict(sorted(self._error_classes.items())),
            }


def _prometheus_label(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _prometheus_value(value):
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    number = float(value)
    return repr(number) if math.isfinite(number) else "0"


def render_prometheus_metrics(metrics, version):
    """Render the bounded operational snapshot in Prometheus text format."""
    lines = []

    def family(name, kind, help_text):
        lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"))

    def sample(name, value, labels=None):
        suffix = ""
        if labels:
            rendered = ",".join(
                f'{key}="{_prometheus_label(label)}"'
                for key, label in sorted(labels.items()))
            suffix = "{" + rendered + "}"
        lines.append(f"{name}{suffix} {_prometheus_value(value)}")

    family("sigil_pi_info", "gauge", "Static sigil-pi build information.")
    sample("sigil_pi_info", 1, {"api_version": API_VERSION, "version": version})

    family("sigil_pi_http_requests_total", "counter", "Authenticated HTTP requests.")
    for request in metrics["requests"]:
        sample("sigil_pi_http_requests_total", request["count"], {
            "method": request["method"], "route": request["route"],
            "status": request["status"],
        })
    family("sigil_pi_http_errors_total", "counter", "Stable HTTP failure classes.")
    for error_class, count in metrics["error_classes"].items():
        sample("sigil_pi_http_errors_total", count, {"class": error_class})

    simple = (
        ("uptime_seconds", "sigil_pi_uptime_seconds", "gauge", "Process uptime."),
        ("turns_total", "sigil_pi_turns_total", "counter", "Completed turns."),
        ("turn_errors_total", "sigil_pi_turn_errors_total", "counter", "Failed turns."),
        ("active_turns", "sigil_pi_active_turns", "gauge", "Active turns."),
        ("input_tokens_total", "sigil_pi_input_tokens_total", "counter", "Input tokens."),
        ("output_tokens_total", "sigil_pi_output_tokens_total", "counter", "Output tokens."),
        ("tool_calls_total", "sigil_pi_tool_calls_total", "counter", "Tool calls."),
        ("retries_total", "sigil_pi_retries_total", "counter", "Provider retries."),
        ("dependency_ready", "sigil_pi_dependency_ready", "gauge", "Dependency readiness."),
        ("service_draining", "sigil_pi_service_draining", "gauge", "Graceful drain state."),
        ("runtime_generation", "sigil_pi_runtime_generation", "gauge",
         "Compiler generations started by this process."),
        ("runtime_unhealthy_replacements_total",
         "sigil_pi_runtime_unhealthy_replacements_total", "counter",
         "Compiler replacements not explained by a caller's expired turn budget."),
    )
    for key, name, kind, help_text in simple:
        if key not in metrics:
            continue          # a host whose runtime does not report generations
        family(name, kind, help_text)
        sample(name, metrics[key])

    family(
        "sigil_pi_dependency_component_ready", "gauge",
        "Readiness of a bounded in-process dependency component.")
    for dependency, ready in sorted(metrics.get("dependency_components", {}).items()):
        sample(
            "sigil_pi_dependency_component_ready", ready,
            {"dependency": dependency})

    for key, name, help_text in (
            ("turn_duration_ms", "sigil_pi_turn_duration_ms", "Turn latency in milliseconds."),
            ("queue_wait_ms", "sigil_pi_queue_wait_ms", "Queue wait in milliseconds.")):
        histogram = metrics[key]
        family(name, "histogram", help_text)
        for bucket in histogram["buckets"]:
            sample(name + "_bucket", bucket["count"], {"le": bucket["le"]})
        sample(name + "_bucket", histogram["count"], {"le": "+Inf"})
        sample(name + "_sum", histogram["sum"])
        sample(name + "_count", histogram["count"])

    resources = metrics.get("resources", {})
    for key, kind in (
            ("memory_peak_bytes", "gauge"), ("thread_count", "gauge"),
            ("open_fd_count", "gauge"), ("state_storage_bytes", "gauge"),
            ("audit_storage_bytes", "gauge"), ("sandbox_storage_bytes", "gauge"),
            ("disk_free_bytes", "gauge"), ("disk_total_bytes", "gauge")):
        if resources.get(key) is not None:
            name = "sigil_pi_" + key
            family(name, kind, "Process resource measurement.")
            sample(name, resources[key])

    quota = metrics.get("tenant_quota_totals", {})
    for key, value in sorted(quota.items()):
        name = "sigil_pi_quota_" + key
        family(name, "gauge", "Aggregate tenant quota usage without tenant labels.")
        sample(name, value)

    for prefix in ("audit_verification", "retention"):
        monitor = metrics.get(prefix)
        if monitor is None:
            continue
        for key, value in sorted(monitor.items()):
            if value is None or not isinstance(value, (bool, int, float)):
                continue
            name = f"sigil_pi_{prefix}_{key}"
            family(name, "gauge", f"{prefix.replace('_', ' ')} status.")
            sample(name, value)
    return "\n".join(lines) + "\n"


class AuditVerificationMonitor:
    """Periodically verify every signed audit chain without logging contents."""

    def __init__(self, verify, *, interval_s=60, clock=None, log_sink=None):
        if interval_s <= 0:
            raise ConfigError("audit verification interval must be positive")
        self.verify = verify
        self.interval_s = interval_s
        self.clock = clock or time.time
        self.log_sink = log_sink or self._print_log
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._checks = 0
        self._failures = 0
        self._healthy = False
        self._last_check = None
        self._chains = 0
        self._records = 0

    @staticmethod
    def _print_log(record):
        print(json.dumps(record, sort_keys=True, separators=(",", ":")), flush=True)

    def check(self):
        try:
            report = self.verify()
            ok = bool(report.get("ok"))
            chains = max(0, int(report.get("chains", 0)))
            records = max(0, int(report.get("records", 0)))
        except Exception:
            ok, chains, records = False, 0, 0
        checked_at = self.clock()
        with self._lock:
            self._checks += 1
            self._failures += 0 if ok else 1
            self._healthy = ok
            self._last_check = checked_at
            self._chains = chains
            self._records = records
        self.log_sink({
            "event": "audit_verification",
            "severity": "info" if ok else "critical",
            "status": "ok" if ok else "failed",
            "chains": chains,
            "records": records,
        })
        return ok

    def is_healthy(self):
        with self._lock:
            return self._healthy

    def snapshot(self):
        with self._lock:
            return {
                "checks_total": self._checks,
                "failures_total": self._failures,
                "healthy": self._healthy,
                "last_check_unix": self._last_check,
                "chains": self._chains,
                "records": self._records,
            }

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.wait(self.interval_s):
                self.check()

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self, timeout_s):
        self._stop.set()
        if self._thread is None:
            return True
        self._thread.join(timeout_s)
        return not self._thread.is_alive()


class SessionOperationLocks:
    """Serialize product turns, scheduled turns, exports, and deletion.

    Entries are reference-counted and removed after their last waiter, avoiding
    the unbounded session-lock registry used by the research host.
    """

    def __init__(self, lock_dir=None):
        self._guard = threading.Lock()
        self._entries = {}
        self.lock_dir = Path(lock_dir) if lock_dir is not None else None
        if self.lock_dir is not None:
            self.lock_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def hold(self, internal_session, *, deadline_monotonic=None):
        with self._guard:
            lock, references = self._entries.get(
                internal_session, (threading.Lock(), 0))
            self._entries[internal_session] = (lock, references + 1)
        if deadline_monotonic is None:
            acquired = lock.acquire()
        else:
            remaining = deadline_monotonic - time.monotonic()
            acquired = remaining > 0 and lock.acquire(timeout=remaining)
        if not acquired:
            with self._guard:
                current_lock, references = self._entries[internal_session]
                if references == 1:
                    del self._entries[internal_session]
                else:
                    self._entries[internal_session] = (current_lock, references - 1)
            raise TimeoutError("turn deadline exceeded while waiting for session operation")
        lock_file = None
        try:
            if self.lock_dir is not None:
                name = hashlib.sha256(internal_session.encode()).hexdigest() + ".lock"
                lock_file = (self.lock_dir / name).open("a+b")
                if deadline_monotonic is None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                else:
                    while True:
                        try:
                            fcntl.flock(
                                lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError as error:
                            remaining = deadline_monotonic - time.monotonic()
                            if remaining <= 0:
                                raise TimeoutError(
                                    "turn deadline exceeded on cross-worker session lock") from error
                            time.sleep(min(0.01, remaining))
            yield
        finally:
            if lock_file is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
            lock.release()
            with self._guard:
                current_lock, references = self._entries[internal_session]
                if references == 1:
                    del self._entries[internal_session]
                else:
                    self._entries[internal_session] = (current_lock, references - 1)

    def size(self):
        with self._guard:
            return len(self._entries)


class ProductScheduleStore:
    """Tenant-scoped durable schedules with a per-tenant cardinality quota."""

    def __init__(self, path, max_per_tenant=32, clock=None):
        if max_per_tenant <= 0:
            raise ConfigError("max_schedules_per_tenant must be positive")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_per_tenant = max_per_tenant
        self.clock = clock or time.time
        self._lock = threading.Lock()
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @contextmanager
    def _locked(self):
        """One schedule transaction across threads and product workers."""
        with self._lock:
            with self._lock_path.open("a+b") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _tenant_hash(tenant_id):
        return hashlib.sha256(tenant_id.encode()).hexdigest()

    @classmethod
    def _key(cls, tenant_id, name):
        return f"{cls._tenant_hash(tenant_id)}:{name}"

    def _load(self):
        if self.path.is_symlink():
            raise RuntimeError("product schedule store is corrupt")
        if not self.path.exists():
            return {}
        if not self.path.is_file():
            raise RuntimeError("product schedule store is corrupt")
        try:
            doc = json.loads(self.path.read_bytes() or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise RuntimeError("product schedule store is corrupt") from e
        if not isinstance(doc, dict):
            raise RuntimeError("product schedule store is corrupt")
        return doc

    def _save(self, data):
        # The cross-process lock serializes writers. A per-process temp keeps a
        # killed writer's incomplete bytes away from the committed JSON.
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False).encode())
        tmp.replace(self.path)

    def is_healthy(self):
        """Return whether the schedule store can be locked and decoded safely."""
        try:
            with self._locked():
                self._load()
            return True
        except (OSError, RuntimeError):
            return False

    def put(self, principal: Principal, *, name, session, message, every_ms):
        if not isinstance(name, str) or not SESSION_RE.fullmatch(name):
            raise ProductError(400, "invalid_schedule_name",
                               "schedule name must use the session-name character set")
        # Reuse the public chat validators so scheduled turns cannot bypass the
        # request boundary's session or message limits.
        ProductService._chat_request({"session": session, "message": message})
        if isinstance(every_ms, bool):
            raise ProductError(400, "invalid_interval", "every_ms must be a positive integer")
        try:
            every_ms = int(every_ms)
        except (TypeError, ValueError) as e:
            raise ProductError(400, "invalid_interval",
                               "every_ms must be a positive integer") from e
        if every_ms <= 0:
            raise ProductError(400, "invalid_interval", "every_ms must be a positive integer")
        tenant_hash = self._tenant_hash(principal.tenant_id)
        key = self._key(principal.tenant_id, name)
        with self._locked():
            data = self._load()
            tenant_count = sum(1 for e in data.values()
                               if e.get("tenant_sha256") == tenant_hash)
            if key not in data and tenant_count >= self.max_per_tenant:
                raise ProductError(409, "schedule_quota_exceeded",
                                   "tenant schedule quota reached")
            prior = data.get(key, {})
            now = self.clock()
            data[key] = {
                "tenant_sha256": tenant_hash,
                "principal": principal.principal_id,
                "name": name,
                "session": session,
                "internal_session": _internal_session(principal.tenant_id, session),
                "message": message,
                "every_ms": every_ms,
                "last_run": prior.get("last_run"),
                "last_status": prior.get("last_status"),
                "created_unix": prior.get("created_unix", now),
                "updated_unix": now,
            }
            self._save(data)

    def for_tenant(self, tenant_id):
        tenant_hash = self._tenant_hash(tenant_id)
        with self._locked():
            entries = [e for e in self._load().values()
                       if e.get("tenant_sha256") == tenant_hash]
        return [{k: e.get(k) for k in (
            "name", "session", "message", "every_ms", "last_run", "last_status")}
                for e in sorted(entries, key=lambda entry: entry["name"])]

    def remove(self, tenant_id, name):
        key = self._key(tenant_id, name)
        with self._locked():
            data = self._load()
            if key not in data:
                return False
            del data[key]
            self._save(data)
            return True

    def entries(self):
        with self._locked():
            return [(key, dict(value)) for key, value in self._load().items()]

    def get(self, key):
        with self._locked():
            value = self._load().get(key)
            return dict(value) if value is not None else None

    def remove_session(self, tenant_id, external_session):
        tenant_hash = self._tenant_hash(tenant_id)
        with self._locked():
            data = self._load()
            doomed = [key for key, entry in data.items()
                      if entry.get("tenant_sha256") == tenant_hash
                      and entry.get("session") == external_session]
            for key in doomed:
                del data[key]
            if doomed:
                self._save(data)
            return len(doomed)

    def remove_internal(self, tenant_sha256, internal_session):
        with self._locked():
            data = self._load()
            doomed = [key for key, entry in data.items()
                      if entry.get("tenant_sha256") == tenant_sha256
                      and entry.get("internal_session") == internal_session]
            for key in doomed:
                del data[key]
            if doomed:
                self._save(data)
            return len(doomed)

    def internal_active(self, tenant_sha256, internal_session,
                        cutoff_unix, now_unix):
        """Whether a schedule keeps its associated session active."""
        with self._locked():
            data = self._load()
            legacy_activity = (self.path.stat().st_mtime
                               if self.path.exists() else now_unix)
            for entry in data.values():
                if (entry.get("tenant_sha256") != tenant_sha256
                        or entry.get("internal_session") != internal_session):
                    continue
                activity = max(
                    float(entry.get("created_unix", legacy_activity)
                          or legacy_activity),
                    float(entry.get("updated_unix", legacy_activity)
                          or legacy_activity),
                    float(entry.get("last_run", 0) or 0),
                )
                if (activity > cutoff_unix
                        or float(entry.get("lease_until", 0) or 0) > now_unix):
                    return True
            return False

    def remove_inactive(self, cutoff_unix, now_unix):
        with self._locked():
            data = self._load()
            # Entries written by a pre-retention release have no timestamps.
            # Use the store's mtime as a conservative migration boundary rather
            # than deleting every legacy schedule on the first upgraded sweep.
            legacy_activity = (self.path.stat().st_mtime
                               if self.path.exists() else now_unix)
            doomed = []
            for key, entry in data.items():
                activity = max(
                    float(entry.get("created_unix", legacy_activity)
                          or legacy_activity),
                    float(entry.get("updated_unix", legacy_activity)
                          or legacy_activity),
                    float(entry.get("last_run", 0) or 0),
                )
                if (activity <= cutoff_unix
                        and float(entry.get("lease_until", 0) or 0) <= now_unix):
                    doomed.append(key)
            for key in doomed:
                del data[key]
            if doomed:
                self._save(data)
            return len(doomed)

    def mark_run(self, key, when, status):
        with self._locked():
            data = self._load()
            if key in data:
                data[key]["last_run"] = when
                data[key]["last_status"] = status
                data[key]["updated_unix"] = when
                self._save(data)

    def claim_due(self, key, now, owner, lease_s):
        """Atomically lease one due entry to one scheduler process."""
        with self._locked():
            data = self._load()
            entry = data.get(key)
            if entry is None or not ProductScheduler._due(entry, now):
                return None
            if entry.get("lease_until", 0) > now:
                return None
            entry["lease_owner"] = owner
            entry["lease_until"] = now + lease_s
            self._save(data)
            return dict(entry)

    def complete_claim(self, key, owner, when, status):
        with self._locked():
            data = self._load()
            entry = data.get(key)
            if entry is None or entry.get("lease_owner") != owner:
                return False
            entry["last_run"] = when
            entry["last_status"] = status
            entry["updated_unix"] = when
            entry.pop("lease_owner", None)
            entry.pop("lease_until", None)
            self._save(data)
            return True


class ProductScheduler:
    """Runs schedules under the creator's current tenant/tool policy."""

    def __init__(self, store: ProductScheduleStore, agent, auth: AuthRegistry,
                 clock=None, interval_s=1.0, session_locks=None, lease_s=300,
                 quota_store=None, data_manager=None, turn_deadline_s=120):
        self.store = store
        self.agent = agent
        self.auth = auth
        self.clock = clock or time
        self.interval_s = interval_s
        self.session_locks = session_locks or SessionOperationLocks()
        self.quota_store = quota_store
        self.data_manager = data_manager
        if (quota_store is not None and quota_store.resource_limits_enabled
                and data_manager is None):
            raise ConfigError(
                "scheduled resource quota enforcement requires the product data manager")
        if lease_s <= 0:
            raise ConfigError("schedule lease_s must be positive")
        if turn_deadline_s <= 0:
            raise ConfigError("schedule turn_deadline_s must be positive")
        self.lease_s = lease_s
        self.turn_deadline_s = turn_deadline_s
        self.owner = f"{os.getpid()}-{uuid.uuid4().hex}"
        self._running = set()
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    @staticmethod
    def _due(entry, now):
        return (entry.get("last_run") is None
                or (now - entry["last_run"]) * 1000.0 >= entry["every_ms"])

    def tick(self):
        now = self.clock.time()
        for key, _ in self.store.entries():
            with self._guard:
                if key in self._running:
                    continue
                self._running.add(key)
            status = "failed"
            claimed = False
            try:
                entry = self.store.claim_due(key, now, self.owner, self.lease_s)
                if entry is None:
                    continue
                claimed = True
                deadline = time.monotonic() + self.turn_deadline_s
                with self.session_locks.hold(
                        entry["internal_session"], deadline_monotonic=deadline):
                    # Deletion can remove an entry after tick's snapshot. Re-read
                    # under the session operation lock so a stale snapshot cannot
                    # resurrect deleted state by running one final turn.
                    current = self.store.get(key)
                    if current is None:
                        continue
                    entry = current
                    principal = self.auth.policy(entry["tenant_sha256"], entry["principal"])
                    if principal is None or "schedules:write" not in principal.scopes:
                        status = "policy_revoked"
                        continue
                    known = set(getattr(self.agent, "manifest", {}))
                    tools = (known if principal.tools == frozenset({"*"})
                             else set(principal.tools))
                    if tools - known:
                        status = "policy_invalid"
                        continue
                    turn_lease = (self.quota_store.acquire_turn(
                                      principal.tenant_id,
                                      session_id=entry["internal_session"])
                                  if self.quota_store is not None else None)
                    usage = None
                    footprint = None
                    try:
                        try:
                            _, usage = self.agent.turn_with_usage(
                                entry["internal_session"], entry["message"],
                                allowed_tools=tools, deadline_monotonic=deadline)
                        finally:
                            if (self.quota_store is not None
                                    and self.quota_store.resource_limits_enabled):
                                footprint = self.data_manager.footprint(
                                    entry["internal_session"])
                    finally:
                        if turn_lease is not None:
                            telemetry = (self.agent.turn_telemetry()
                                         if hasattr(self.agent, "turn_telemetry") else {})
                            if usage is None and (
                                    "input_tokens" in telemetry
                                    or "output_tokens" in telemetry):
                                usage = {
                                    "input_tokens": max(
                                        0, int(telemetry.get("input_tokens", 0))),
                                    "output_tokens": max(
                                        0, int(telemetry.get("output_tokens", 0))),
                                }
                            turn_lease.release(
                                usage=usage,
                                session_id=(entry["internal_session"]
                                            if footprint is not None else None),
                                storage_bytes=(None if footprint is None
                                               else footprint["storage_bytes"]),
                                audit_bytes=(None if footprint is None
                                             else footprint["audit_bytes"]))
                    status = "ok"
            except TimeoutError:
                status = "deadline_exceeded"
            except ProductError as error:
                if error.code in {
                        "token_quota_exceeded", "storage_quota_exceeded",
                        "audit_quota_exceeded"}:
                    status = "quota_exceeded"
                else:
                    status = "failed"
            except Exception:
                # A scheduled failure is state, not a reason to stop every
                # tenant's schedule loop. No exception string reaches the store.
                status = "failed"
            finally:
                if claimed:
                    self.store.complete_claim(
                        key, self.owner, self.clock.time(), status)
                with self._guard:
                    self._running.discard(key)

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self._thread

        def run():
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(self.interval_s)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self._thread = thread
        return thread

    def stop(self, timeout_s=None):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
            return not self._thread.is_alive()
        return True


class ProductDataManager:
    """Tenant-authorized export and deletion of the supported v1 state surfaces."""

    SCHEMA_VERSION = 1

    def __init__(self, agent, schedule_store, session_locks,
                 max_export_bytes=10 * 1024 * 1024, quota_store=None):
        if max_export_bytes <= 0:
            raise ConfigError("max_export_bytes must be positive")
        if getattr(agent, "memory", None) is not None:
            raise ConfigError("product data lifecycle requires memory to be disabled")
        self.agent = agent
        self.schedule_store = schedule_store
        self.session_locks = session_locks
        self.max_export_bytes = max_export_bytes
        self.quota_store = quota_store

    def _paths(self, internal_session):
        session_path = self.agent.store._path(internal_session)
        sandbox = (Path(self.agent.sandbox_root)
                   / hashlib.sha256(internal_session.encode()).hexdigest()[:16])
        audit = self.agent.audit._path(internal_session)
        return session_path, sandbox, audit

    def is_healthy(self):
        """Read-only accessibility probe for every active customer-state root."""
        roots = (
            Path(self.agent.store.dir), Path(self.agent.sandbox_root),
            Path(self.agent.audit.dir),
        )
        try:
            return all(
                root.is_dir() and not root.is_symlink()
                and os.access(root, os.R_OK | os.W_OK)
                for root in roots)
        except OSError:
            return False

    def footprint(self, internal_session):
        """Exact supported customer-state bytes for one internal session.

        Conversation and regular sandbox files count toward tenant storage;
        the separately bounded signed chain counts toward the audit quota.
        Links are never followed, so a corrupt/unsupported sandbox cannot turn
        quota accounting into an arbitrary host-filesystem traversal.
        """
        session_path, sandbox, audit_path = self._paths(internal_session)
        storage_bytes = session_path.stat().st_size if session_path.is_file() else 0
        activity = session_path.stat().st_mtime if session_path.is_file() else 0.0
        if sandbox.exists():
            for directory, subdirs, files in os.walk(sandbox, followlinks=False):
                subdirs[:] = [name for name in subdirs
                              if not (Path(directory) / name).is_symlink()]
                for name in files:
                    path = Path(directory) / name
                    try:
                        if not path.is_symlink() and path.is_file():
                            info = path.stat()
                            storage_bytes += info.st_size
                            activity = max(activity, info.st_mtime)
                    except OSError:
                        continue
        audit_bytes = audit_path.stat().st_size if audit_path.is_file() else 0
        if audit_path.is_file():
            activity = max(activity, audit_path.stat().st_mtime)
        return {
            "storage_bytes": storage_bytes,
            "audit_bytes": audit_bytes,
            "last_activity_unix": activity,
        }

    def delete_internal_files(self, internal_session):
        """Remove supported per-session files; caller owns the session lock."""
        session_path, sandbox, audit_path = self._paths(internal_session)
        deleted = {
            "conversation": session_path.exists(),
            "sandbox": sandbox.exists(),
            "audit": audit_path.exists(),
        }
        if session_path.exists():
            session_path.unlink()
        if sandbox.exists():
            shutil.rmtree(sandbox)
        if audit_path.exists():
            audit_path.unlink()
        return deleted

    def verify_quota_registry(self, registered_sessions):
        """Refuse customer files that cannot be attributed after an upgrade/crash."""
        internals = {row[2] for row in registered_sessions}
        expected_full = {
            hashlib.sha256(value.encode()).hexdigest() for value in internals}
        expected_short = {value[:16] for value in expected_full}
        actual_sessions = {
            path.stem for path in self.agent.store.dir.glob("*.kv") if path.is_file()}
        actual_audit = {
            path.stem for path in self.agent.audit.dir.glob("*.jsonl") if path.is_file()}
        sandbox_root = Path(self.agent.sandbox_root)
        actual_sandboxes = ({path.name for path in sandbox_root.iterdir() if path.is_dir()}
                            if sandbox_root.exists() else set())
        if (actual_sessions - expected_full or actual_audit - expected_full
                or actual_sandboxes - expected_short):
            raise ConfigError(
                "customer state lacks durable quota registry entries; "
                "a supported state migration is required")

    def export(self, principal, external_session):
        ProductService._validate_session(external_session)
        internal = _internal_session(principal.tenant_id, external_session)
        with self.session_locks.hold(internal):
            session_path, sandbox, audit_path = self._paths(internal)
            schedules = [entry for entry in self.schedule_store.for_tenant(principal.tenant_id)
                         if entry["session"] == external_session]
            exists = session_path.exists() or sandbox.exists() or audit_path.exists() or schedules
            if not exists:
                raise ProductError(404, "session_not_found", "session not found")
            conversation = self.agent.store.load(internal) if session_path.exists() else []
            audit = self.agent.audit.read(internal) if audit_path.exists() else []
            files = []
            total = len(json.dumps(conversation, ensure_ascii=False).encode())
            total += len(json.dumps(audit, ensure_ascii=False).encode())
            if sandbox.exists():
                for path in sorted(sandbox.rglob("*")):
                    if path.is_symlink():
                        raise ProductError(409, "unsafe_session_data",
                                           "session sandbox contains an unsupported symlink")
                    if not path.is_file():
                        continue
                    raw = path.read_bytes()
                    total += len(raw)
                    if total > self.max_export_bytes:
                        raise ProductError(413, "export_too_large",
                                           "session export exceeds the configured limit")
                    files.append({
                        "path": path.relative_to(sandbox).as_posix(),
                        "size": len(raw),
                        "content_base64": base64.b64encode(raw).decode(),
                    })
            if total > self.max_export_bytes:
                raise ProductError(413, "export_too_large",
                                   "session export exceeds the configured limit")
            return {
                "schema_version": self.SCHEMA_VERSION,
                "session": external_session,
                "conversation": conversation,
                "files": files,
                "schedules": schedules,
                "audit": audit,
            }

    def delete(self, principal, external_session):
        ProductService._validate_session(external_session)
        internal = _internal_session(principal.tenant_id, external_session)
        with self.session_locks.hold(internal):
            # Remove triggers first. ProductScheduler re-reads under the same
            # lock, so a stale due snapshot cannot run after this point.
            schedule_count = self.schedule_store.remove_session(
                principal.tenant_id, external_session)
            session_path, sandbox, audit_path = self._paths(internal)
            deleted = {
                "conversation": session_path.exists(),
                "sandbox": sandbox.exists(),
                "audit": audit_path.exists(),
                "schedules": schedule_count,
            }
            self.delete_internal_files(internal)
            if not any((deleted["conversation"], deleted["sandbox"], deleted["audit"],
                        deleted["schedules"])):
                raise ProductError(404, "session_not_found", "session not found")
            if self.quota_store is not None:
                self.quota_store.remove_session_usage(principal.tenant_id, internal)
            return deleted


class ProductRetentionMonitor:
    """Delete inactive supported product state and expose sweep health.

    Session state is removed under the same cross-worker lock used by chat,
    scheduling, export, and explicit deletion. The quota registry row is
    deleted last: a killed or failed sweep therefore leaves an attributable
    row that startup reconciliation can safely repair and retry.
    """

    def __init__(self, quota_store, data_manager, schedule_store, session_locks,
                 *, retention_s, interval_s=3600, clock=None, log_sink=None,
                 precheck=None):
        if retention_s <= 0:
            raise ConfigError("retention_s must be positive")
        if interval_s <= 0:
            raise ConfigError("retention interval must be positive")
        self.quota_store = quota_store
        self.data_manager = data_manager
        self.schedule_store = schedule_store
        self.session_locks = session_locks
        self.retention_s = retention_s
        self.interval_s = interval_s
        self.clock = clock or time.time
        self.log_sink = log_sink or self._print_log
        self.precheck = precheck
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._checks = 0
        self._failures = 0
        self._deleted_sessions = 0
        self._deleted_schedules = 0
        self._last_check = None
        self._healthy = False

    @staticmethod
    def _print_log(record):
        print(json.dumps(record, sort_keys=True, separators=(",", ":")), flush=True)

    def tick(self):
        now = float(self.clock())
        cutoff = now - self.retention_s
        deleted_sessions = 0
        deleted_schedules = 0
        try:
            # Retention must never erase the evidence of a chain-integrity
            # failure. Production wires this to signed-audit verification.
            if self.precheck is not None and not self.precheck():
                raise RuntimeError("retention precheck failed")
            for tenant, session, internal, _ in self.quota_store.inactive_sessions(cutoff):
                with self.session_locks.hold(internal):
                    # A request may have refreshed activity after the sweep's
                    # initial snapshot while it waited for this lock.
                    if not self.quota_store.registered_session_inactive(
                            tenant, session, internal, cutoff):
                        continue
                    if self.schedule_store.internal_active(
                            tenant, internal, cutoff, now):
                        continue
                    deleted_schedules += self.schedule_store.remove_internal(
                        tenant, internal)
                    self.data_manager.delete_internal_files(internal)
                    if self.quota_store.remove_registered_session(
                            tenant, session, cutoff_unix=cutoff):
                        deleted_sessions += 1

            # A schedule may exist before its first turn creates a session
            # registry entry, so it has an independent retention path.
            deleted_schedules += self.schedule_store.remove_inactive(cutoff, now)
        except Exception:
            with self._lock:
                self._checks += 1
                self._failures += 1
                self._deleted_sessions += deleted_sessions
                self._deleted_schedules += deleted_schedules
                self._last_check = now
                self._healthy = False
            self.log_sink({
                "event": "retention_cleanup",
                "severity": "critical",
                "status": "failed",
                "deleted_sessions": deleted_sessions,
                "deleted_schedules": deleted_schedules,
            })
            return False

        with self._lock:
            self._checks += 1
            self._deleted_sessions += deleted_sessions
            self._deleted_schedules += deleted_schedules
            self._last_check = now
            self._healthy = True
        if deleted_sessions or deleted_schedules:
            self.log_sink({
                "event": "retention_cleanup",
                "severity": "info",
                "status": "ok",
                "deleted_sessions": deleted_sessions,
                "deleted_schedules": deleted_schedules,
            })
        return True

    def is_healthy(self):
        with self._lock:
            return self._healthy

    def snapshot(self):
        with self._lock:
            return {
                "checks_total": self._checks,
                "failures_total": self._failures,
                "healthy": self._healthy,
                "last_check_unix": self._last_check,
                "deleted_sessions_total": self._deleted_sessions,
                "deleted_schedules_total": self._deleted_schedules,
                "retention_seconds": self.retention_s,
            }

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self._thread

        def run():
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(self.interval_s)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self._thread = thread
        return thread

    def stop(self, timeout_s=None):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
            return not self._thread.is_alive()
        return True


def _internal_session(tenant_id: str, external_session: str) -> str:
    tenant = hashlib.sha256(tenant_id.encode()).hexdigest()
    session = hashlib.sha256(external_session.encode()).hexdigest()
    return f"v1:{tenant}:{session}"


def _request_id(headers) -> str:
    supplied = headers.get("X-Request-ID")
    if supplied and REQUEST_ID_RE.fullmatch(supplied):
        return supplied
    return uuid.uuid4().hex


class ProductService:
    def __init__(self, agent, auth: AuthRegistry, *, version=DEFAULT_VERSION,
                 requests_per_minute=60, max_concurrent_turns=2,
                 readiness=None, log_sink=None, schedule_store=None,
                 session_locks=None, data_manager=None, quota_store=None,
                 audit_monitor=None, retention_monitor=None,
                 turn_deadline_s=120, resource_root=None):
        self.agent = agent
        self.auth = auth
        self.version = version
        self.rate = FixedWindowRateLimiter(requests_per_minute)
        self.concurrency = TenantConcurrency(max_concurrent_turns)
        self.metrics = ServiceMetrics()
        def runtime_ready():
            runtime = getattr(agent, "_mcp", None)
            return runtime is not None and bool(getattr(runtime, "is_healthy", True))

        self.readiness = readiness or runtime_ready
        self.log_sink = log_sink or self._print_log
        self.schedule_store = schedule_store
        self.session_locks = session_locks or SessionOperationLocks()
        self.data_manager = data_manager
        self.quota_store = quota_store
        if (quota_store is not None and quota_store.resource_limits_enabled
                and data_manager is None):
            raise ConfigError(
                "resource quota enforcement requires the product data manager")
        self.audit_monitor = audit_monitor
        self.retention_monitor = retention_monitor
        self.resource_root = Path(resource_root) if resource_root is not None else None
        if turn_deadline_s <= 0:
            raise ConfigError("turn_deadline_s must be positive")
        self.turn_deadline_s = turn_deadline_s
        self._lifecycle = threading.Condition()
        self._draining = False
        self._active_requests = 0
        memory = getattr(agent, "memory", None)
        if memory is not None and getattr(memory, "scope", None) != "session":
            raise ConfigError("product service requires session-scoped memory")
        known_tools = set(getattr(agent, "manifest", {}))
        for principal in auth.principals:
            if principal.tools == frozenset({"*"}):
                continue
            unknown = set(principal.tools) - known_tools
            if unknown:
                raise ConfigError(
                    f"principal {principal.principal_id!r} authorizes unknown tools: "
                    f"{sorted(unknown)}")

    @staticmethod
    def _print_log(record):
        print(json.dumps(record, sort_keys=True, separators=(",", ":")), flush=True)

    def _allowed_tools(self, principal):
        known = set(getattr(self.agent, "manifest", {}))
        if principal.tools == frozenset({"*"}):
            return known
        unknown = set(principal.tools) - known
        if unknown:
            # An auth policy typo must not silently create a narrower, surprising
            # deployment. Configuration is checked again per request because an
            # embeddable host may replace the manifest after construction.
            raise ProductError(503, "configuration_error", "tool authorization is invalid")
        return set(principal.tools)

    def start_draining(self):
        """Reject new turns and make readiness fail before process shutdown."""
        with self._lifecycle:
            self._draining = True

    def wait_for_drain(self, timeout_s: float) -> bool:
        if timeout_s < 0:
            raise ValueError("timeout_s must be non-negative")
        deadline = time.monotonic() + timeout_s
        with self._lifecycle:
            while self._active_requests:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._lifecycle.wait(remaining)
            return True

    def _turn_enter(self):
        with self._lifecycle:
            if self._draining:
                raise ProductError(503, "service_draining", "service is draining")
            self._active_requests += 1

    def _turn_leave(self):
        with self._lifecycle:
            self._active_requests -= 1
            self._lifecycle.notify_all()

    def is_ready(self):
        return self.readiness_snapshot()["ready"]

    @staticmethod
    def _probe_dependency(probe):
        try:
            return bool(probe())
        except Exception:
            # Readiness is a content-free safety boundary. A dependency probe
            # failure is unhealthy, never a diagnostic returned to the caller.
            return False

    def readiness_snapshot(self):
        """One content-free, bounded view of every in-process dependency."""
        with self._lifecycle:
            draining = self._draining
        probes = {"runtime": self.readiness}
        for name, component in (
                ("quota_store", self.quota_store),
                ("schedule_store", self.schedule_store),
                ("state_storage", self.data_manager),
                ("audit_verification", self.audit_monitor),
                ("retention", self.retention_monitor)):
            if component is not None:
                probe = getattr(component, "is_healthy", None)
                if callable(probe):
                    probes[name] = probe
        dependencies = {
            name: self._probe_dependency(probe)
            for name, probe in sorted(probes.items())}
        return {
            "ready": not draining and all(dependencies.values()),
            "draining": draining,
            "dependencies": dependencies,
        }

    @staticmethod
    def _tree_bytes(root):
        """Return regular-file bytes without following customer-controlled links."""
        total = 0
        try:
            for directory, subdirs, files in os.walk(root, followlinks=False):
                subdirs[:] = [name for name in subdirs
                              if not (Path(directory) / name).is_symlink()]
                for name in files:
                    path = Path(directory) / name
                    try:
                        if not path.is_symlink() and path.is_file():
                            total += path.stat().st_size
                    except OSError:
                        continue
        except OSError:
            return 0
        return total

    def resource_snapshot(self):
        """Low-cardinality host evidence for capacity runs.

        ``ru_maxrss`` is a process high-water mark (bytes on macOS, KiB on
        Linux), which makes a rising peak observable without another runtime
        dependency. Storage inspection is enabled only when product startup
        supplies the state root.
        """
        usage = resource.getrusage(resource.RUSAGE_SELF)
        memory_peak = int(usage.ru_maxrss)
        if sys.platform != "darwin":
            memory_peak *= 1024
        open_fds = None
        for fd_root in (Path("/proc/self/fd"), Path("/dev/fd")):
            try:
                open_fds = sum(1 for _ in fd_root.iterdir())
                break
            except OSError:
                continue
        snapshot = {
            "memory_peak_bytes": max(0, memory_peak),
            "thread_count": threading.active_count(),
            "open_fd_count": open_fds,
        }
        root = self.resource_root
        if root is not None and root.exists():
            disk = shutil.disk_usage(root)
            snapshot.update({
                "state_storage_bytes": self._tree_bytes(root),
                "audit_storage_bytes": self._tree_bytes(root / "audit"),
                "sandbox_storage_bytes": self._tree_bytes(root / "sandboxes"),
                "disk_free_bytes": disk.free,
                "disk_total_bytes": disk.total,
            })
        return snapshot

    def operational_metrics(self):
        """One bounded snapshot shared by the JSON and Prometheus contracts."""
        metrics = self.metrics.snapshot()
        readiness = self.readiness_snapshot()
        metrics["dependency_ready"] = readiness["ready"]
        metrics["dependency_components"] = readiness["dependencies"]
        metrics["service_draining"] = readiness["draining"]
        metrics["resources"] = self.resource_snapshot()
        # Present only when the runtime is a replacing endpoint; a raw client
        # (the research host, and every scripted double) reports neither.
        runtime = getattr(self.agent, "_mcp", None)
        for key, attribute in (("runtime_generation", "generation"),
                               ("runtime_unhealthy_replacements_total",
                                "unhealthy_replacements")):
            value = getattr(runtime, attribute, None)
            if isinstance(value, int):
                metrics[key] = value
        if self.quota_store is not None:
            metrics["tenant_quota_totals"] = self.quota_store.aggregate_usage()
        if self.audit_monitor is not None:
            metrics["audit_verification"] = self.audit_monitor.snapshot()
        if self.retention_monitor is not None:
            metrics["retention"] = self.retention_monitor.snapshot()
        return metrics

    @staticmethod
    def _require(principal, scope):
        if scope not in principal.scopes:
            raise ProductError(403, "permission_denied", "credential lacks required permission")

    @staticmethod
    def _json_object(body):
        try:
            obj = json.loads(body or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ProductError(400, "invalid_json", "request body must be a JSON object") from e
        if not isinstance(obj, dict):
            raise ProductError(400, "invalid_request", "request body must be a JSON object")
        return obj

    @staticmethod
    def _validate_session(session):
        if not isinstance(session, str) or not SESSION_RE.fullmatch(session):
            raise ProductError(
                400, "invalid_session",
                "session must be 1-128 ASCII letters, digits, dot, underscore, or hyphen")
        if len(session.encode()) > MAX_SESSION_BYTES:  # defensive if the regex changes
            raise ProductError(400, "invalid_session", "session is too large")

    @staticmethod
    def _validate_message(message):
        if not isinstance(message, str) or not message:
            raise ProductError(400, "invalid_message", "message must be a non-empty string")
        if len(message.encode()) > MAX_MESSAGE_BYTES:
            raise ProductError(413, "message_too_large", "message exceeds the product limit")

    @classmethod
    def _chat_request(cls, obj):
        if set(obj) != {"session", "message"}:
            raise ProductError(400, "invalid_request", "expected exactly session and message")
        session, message = obj["session"], obj["message"]
        cls._validate_session(session)
        cls._validate_message(message)
        return session, message

    def dispatch(self, method: str, path: str, headers, body: bytes = b""):
        started = time.monotonic()
        request_id = _request_id(headers)
        principal = None
        route = "unknown"
        status = 500
        error_class = None
        extra_headers = {"Cache-Control": "no-store", "X-Request-ID": request_id}
        try:
            principal = self.auth.authenticate(headers.get("Authorization"))
            if self.quota_store is not None:
                admitted, retry_after = self.quota_store.admit_request(principal.tenant_id)
            else:
                admitted, retry_after = self.rate.admit(principal.tenant_id)
            if not admitted:
                raise ProductError(429, "rate_limited", "tenant request rate exceeded",
                                   retry_after=retry_after)

            if method == "POST" and path == "/v1/chat":
                route = "/v1/chat"
                self._require(principal, "chat")
                external_session, message = self._chat_request(self._json_object(body))
                internal_session = _internal_session(principal.tenant_id, external_session)
                allowed_tools = self._allowed_tools(principal)
                deadline = time.monotonic() + self.turn_deadline_s
                turn_capacity = (self.quota_store.acquire_turn(
                                     principal.tenant_id, session_id=internal_session)
                                 if self.quota_store is not None
                                 else self.concurrency.acquire(principal.tenant_id))
                usage = None
                footprint = None
                try:
                    self._turn_enter()
                    turn_started = time.monotonic()
                    self.metrics.turn_start()
                    turn_error = True
                    queue_wait_ms = 0.0
                    try:
                        queue_started = time.monotonic()
                        with self.session_locks.hold(
                                internal_session, deadline_monotonic=deadline):
                            queue_wait_ms = (time.monotonic() - queue_started) * 1000
                            try:
                                reply, usage = self.agent.turn_with_usage(
                                    internal_session, message, allowed_tools=allowed_tools,
                                    deadline_monotonic=deadline)
                            finally:
                                if (self.quota_store is not None
                                        and self.quota_store.resource_limits_enabled):
                                    footprint = self.data_manager.footprint(internal_session)
                        turn_error = False
                    except TimeoutError as e:
                        raise ProductError(
                            504, "turn_deadline_exceeded", "turn deadline exceeded") from e
                    except RuntimeError as e:
                        raise ProductError(502, "agent_failure", "agent turn failed") from e
                    finally:
                        telemetry = (self.agent.turn_telemetry()
                                     if hasattr(self.agent, "turn_telemetry") else {})
                        queue_wait_ms += max(
                            0.0, float(telemetry.get("forge_queue_wait_ms", 0.0)))
                        extra_headers["Server-Timing"] = (
                            f"queue;dur={queue_wait_ms:.3f}")
                        tool_calls = max(0, int(telemetry.get("tool_calls", 0)))
                        retries = max(0, int(telemetry.get("retries", 0)))
                        if usage is None and (
                                "input_tokens" in telemetry or "output_tokens" in telemetry):
                            usage = {
                                "input_tokens": max(
                                    0, int(telemetry.get("input_tokens", 0))),
                                "output_tokens": max(
                                    0, int(telemetry.get("output_tokens", 0))),
                            }
                        extra_headers["X-Sigil-Tool-Calls"] = str(tool_calls)
                        extra_headers["X-Sigil-Retries"] = str(retries)
                        self.metrics.turn_finish(
                            (time.monotonic() - turn_started) * 1000,
                            queue_wait_ms=queue_wait_ms, usage=usage,
                            tool_calls=tool_calls, retries=retries,
                            error=turn_error)
                        self._turn_leave()
                finally:
                    if self.quota_store is not None:
                        turn_capacity.release(
                            usage=usage,
                            session_id=(internal_session if footprint is not None else None),
                            storage_bytes=(None if footprint is None
                                           else footprint["storage_bytes"]),
                            audit_bytes=(None if footprint is None
                                         else footprint["audit_bytes"]))
                    else:
                        turn_capacity.release()
                status, payload = 200, {
                    "request_id": request_id,
                    "session": external_session,
                    "reply": reply,
                    "usage": usage,
                }
            elif method == "GET" and path == "/v1/health":
                route = "/v1/health"
                self._require(principal, "ops:read")
                status, payload = 200, {"status": "ok", "request_id": request_id}
            elif method == "GET" and path == "/v1/ready":
                route = "/v1/ready"
                self._require(principal, "ops:read")
                readiness = self.readiness_snapshot()
                status = 200 if readiness["ready"] else 503
                payload = {
                    "status": "ready" if readiness["ready"] else "not_ready",
                    "dependencies": readiness["dependencies"],
                    "draining": readiness["draining"],
                    "request_id": request_id,
                }
            elif method == "GET" and path == "/v1/version":
                route = "/v1/version"
                self._require(principal, "ops:read")
                status, payload = 200, {"api_version": API_VERSION,
                                        "version": self.version,
                                        "request_id": request_id}
            elif method == "GET" and path == "/v1/metrics":
                route = "/v1/metrics"
                self._require(principal, "ops:read")
                status, payload = 200, {
                    "metrics": self.operational_metrics(), "request_id": request_id}
            elif method == "GET" and path == "/v1/metrics/prometheus":
                route = "/v1/metrics/prometheus"
                self._require(principal, "ops:read")
                status, payload = 200, PrometheusPayload(
                    render_prometheus_metrics(self.operational_metrics(), self.version))
            elif method == "GET" and path == "/v1/schedules":
                route = "/v1/schedules"
                self._require(principal, "schedules:read")
                if self.schedule_store is None:
                    raise ProductError(503, "schedules_unavailable",
                                       "schedule service is not configured")
                status, payload = 200, {
                    "schedules": self.schedule_store.for_tenant(principal.tenant_id),
                    "request_id": request_id,
                }
            elif method == "POST" and path == "/v1/schedules":
                route = "/v1/schedules"
                self._require(principal, "schedules:write")
                if self.schedule_store is None:
                    raise ProductError(503, "schedules_unavailable",
                                       "schedule service is not configured")
                obj = self._json_object(body)
                if set(obj) != {"name", "session", "message", "every_ms"}:
                    raise ProductError(
                        400, "invalid_request",
                        "expected exactly name, session, message, and every_ms")
                self.schedule_store.put(principal, **obj)
                status, payload = 201, {"created": True, "request_id": request_id}
            elif method == "DELETE" and path.startswith("/v1/schedules/"):
                route = "/v1/schedules/{name}"
                self._require(principal, "schedules:write")
                if self.schedule_store is None:
                    raise ProductError(503, "schedules_unavailable",
                                       "schedule service is not configured")
                name = unquote(path[len("/v1/schedules/"):])
                if not SESSION_RE.fullmatch(name):
                    raise ProductError(400, "invalid_schedule_name",
                                       "invalid schedule name")
                removed = self.schedule_store.remove(principal.tenant_id, name)
                if not removed:
                    raise ProductError(404, "schedule_not_found", "schedule not found")
                status, payload = 200, {"removed": True, "request_id": request_id}
            elif (method == "GET" and path.startswith("/v1/sessions/")
                  and path.endswith("/export")):
                route = "/v1/sessions/{session}/export"
                self._require(principal, "sessions:read")
                if self.data_manager is None:
                    raise ProductError(503, "data_lifecycle_unavailable",
                                       "session data service is not configured")
                encoded = path[len("/v1/sessions/"):-len("/export")]
                session = unquote(encoded.rstrip("/"))
                status, payload = 200, {
                    "data": self.data_manager.export(principal, session),
                    "request_id": request_id,
                }
            elif method == "DELETE" and path.startswith("/v1/sessions/"):
                route = "/v1/sessions/{session}"
                self._require(principal, "sessions:delete")
                if self.data_manager is None:
                    raise ProductError(503, "data_lifecycle_unavailable",
                                       "session data service is not configured")
                session = unquote(path[len("/v1/sessions/"):])
                status, payload = 200, {
                    "deleted": self.data_manager.delete(principal, session),
                    "request_id": request_id,
                }
            else:
                raise ProductError(404, "not_found", "route not found")
        except ProductError as e:
            status = e.status
            error_class = e.code
            if e.retry_after is not None:
                extra_headers["Retry-After"] = str(e.retry_after)
            if status == 401:
                extra_headers["WWW-Authenticate"] = 'Bearer realm="sigil-pi"'
            payload = {"error": {"code": e.code, "message": e.message},
                       "request_id": request_id}
        except Exception:
            # The product contract never returns exception strings. Operators
            # correlate the request id with the structured failure log.
            status = 500
            error_class = "internal_error"
            payload = {"error": {"code": "internal_error",
                                  "message": "internal service error"},
                       "request_id": request_id}
        finally:
            duration_ms = round((time.monotonic() - started) * 1000, 3)
            self.metrics.request(method, route, status, error_class)
            self.log_sink({
                "event": "http_request",
                "request_id": request_id,
                "method": method,
                "route": route,
                "status": status,
                "failure_classification": error_class,
                "duration_ms": duration_ms,
                "principal": principal.principal_id if principal else None,
                "tenant_sha256": (hashlib.sha256(principal.tenant_id.encode()).hexdigest()
                                  if principal else None),
            })
        return status, extra_headers, payload


class ProductHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_transport(host, ssl_context):
    if not isinstance(host, str) or not host:
        raise ConfigError("product bind host must be non-empty")
    if not _is_loopback(host) and ssl_context is None:
        raise ConfigError("non-loopback product binds require TLS")


def tls_context(cert_file, key_file):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=cert_file, keyfile=key_file)
    return context


def serve_product(service: ProductService, *, host="127.0.0.1", port=8080,
                  ssl_context=None, socket_timeout_s=30):
    """Serve the v1 contract.

    Plaintext is accepted only on loopback, where a local TLS-terminating proxy
    can connect. Binding any external/wildcard address without a server TLS
    context is a startup error, not an operator warning.
    """
    if socket_timeout_s <= 0:
        raise ConfigError("socket_timeout_s must be positive")
    validate_transport(host, ssl_context)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(socket_timeout_s)

        def do_GET(self):
            self._dispatch(b"")

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if not 0 <= n <= MAX_PRODUCT_REQUEST_BYTES:
                    raise ValueError
                body = self.rfile.read(n)
            except (ValueError, OSError):
                self._write(400, {}, {
                    "error": {"code": "invalid_content_length",
                              "message": "invalid or oversized Content-Length"},
                    "request_id": _request_id(self.headers),
                })
                return
            self._dispatch(body)

        def do_DELETE(self):
            self._dispatch(b"")

        def _dispatch(self, body):
            status, headers, payload = service.dispatch(
                self.command, self.path, self.headers, body)
            self._write(status, headers, payload)

        def _write(self, status, headers, payload):
            if isinstance(payload, PrometheusPayload):
                raw = payload.body.encode()
                content_type = PROMETHEUS_CONTENT_TYPE
            else:
                raw = json.dumps(
                    payload, separators=(",", ":"), ensure_ascii=False).encode()
                content_type = "application/json"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ProductHTTPServer((host, port), Handler)
    if ssl_context is not None:
        server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
