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
import errno
import fcntl
import hashlib
import hmac
import http.client
import io
import ipaddress
import json
import math
import os
import re
import resource
import select
import shutil
import socket
import sqlite3
import ssl
import stat
import sys
import threading
import time
import uuid
import weakref
from collections import defaultdict, deque
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from agent import (AuditWriteError, ensure_directory, scratch_files, sync_directory,
                   write_durably)


API_VERSION = "v1"
# Deliberately not a releasable version string. product_main always passes the
# real one from the VERSION file; this only answers when a ProductService is
# constructed without it (tests, library use), and a value that looked like a
# published release would let such a service claim to BE one on /v1/version.
DEFAULT_VERSION = "0.0.0-unset"
MAX_AUTH_TOKEN_BYTES = 4096
MAX_PRODUCT_REQUEST_BYTES = 1024 * 1024
MAX_SESSION_BYTES = 128
MAX_MESSAGE_BYTES = 256 * 1024
# What a client can make the listener hold before its request is
# authenticated (audit F-08). At most MAX_CONNECTIONS connections are handled
# at a time, and the next ones wait in a listen queue LISTEN_BACKLOG deep. A
# request's TLS handshake, head and body must arrive within
# REQUEST_DEADLINE_SECONDS of the server taking its connection, however they
# are paced, and its header lines may total MAX_REQUEST_HEAD_BYTES.
# serve_product() takes the first two as arguments; these are its defaults.
MAX_CONNECTIONS = 128
REQUEST_DEADLINE_SECONDS = 60
MAX_REQUEST_HEAD_BYTES = 64 * 1024
LISTEN_BACKLOG = 128
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
# An effective setting's key becomes part of a Prometheus metric name.
SETTING_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
HISTOGRAM_BUCKETS_MS = (
    10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2000.0,
    5000.0, 10000.0, 30000.0, 60000.0, 120000.0,
)
PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
# Scheduled turns run on a pool of at most SCHEDULER_WORKERS threads, at most
# SCHEDULED_TURNS_PER_TENANT of one tenant's at a time, so one tenant's
# schedules cannot hold up another's (audit F-06).
SCHEDULER_WORKERS = 4
SCHEDULED_TURNS_PER_TENANT = 1
# The shortest interval the product accepts for a schedule (audit F-06): with
# no floor above 1 ms, a tenant could keep every one of its schedules due.
MIN_SCHEDULE_EVERY_MS = 60_000
# The most bytes one tenant's schedule messages may take in the schedule store
# unless PI_MAX_SCHEDULE_BYTES_PER_TENANT says otherwise (audit F-27): 32
# plain-text messages of MAX_MESSAGE_BYTES, what a tenant could hold before.
MAX_SCHEDULE_BYTES_PER_TENANT = 8 * 1024 * 1024
KNOWN_SCOPES = frozenset({
    "chat",
    "ops:read",
    "schedules:read",
    "schedules:write",
    "sessions:read",
    "sessions:delete",
})
# A principal's tool grants (audit F-09). A policy names the secrets its tools
# may use with the grammar the manifest's {SECRET:name} tokens use, short
# enough to be a file name; their values are its tenant's, in TenantSecrets.
SECRET_NAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")
# A host the principal's fetch may reach, as the runtime matches one: exactly,
# or with "*." for that name and every name under it. Lowercase, because the
# runtime compares exactly, and a name that can never match would read as a
# deny nobody meant. A bare "*" (any host, internal ones included) is refused.
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
NET_HOST_RE = re.compile(rf"^(?:\*\.)?{_HOST_LABEL}(?:\.{_HOST_LABEL})*$")
MAX_NET_HOST_CHARS = 253
# The largest tenant secret value: the size of the largest bearer credential
# this service accepts.
MAX_TENANT_SECRET_BYTES = 4096


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
    # What the principal's tools may use (audit F-09): the names of its
    # tenant's secrets, and the hosts its fetch may reach. Names only; the
    # values are its tenant's, in TenantSecrets.
    secrets: frozenset[str] = frozenset()
    net_allowlist: frozenset[str] = frozenset()


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
            secret_names = entry.get("secrets", [])
            hosts = entry.get("net_allowlist", [])
            if not isinstance(secret_names, list) or not all(
                    isinstance(v, str) and SECRET_NAME_RE.fullmatch(v)
                    for v in secret_names):
                raise ConfigError(
                    f"auth entry {i} secrets must be a list of secret names "
                    f"(lowercase letters, digits and '_', at most 64)")
            if not isinstance(hosts, list) or not all(isinstance(v, str) for v in hosts):
                raise ConfigError(f"auth entry {i} net_allowlist must be a string list")
            if "*" in hosts:
                raise ConfigError(
                    f"auth entry {i} net_allowlist cannot be '*', which would let its "
                    f"fetch reach any host, internal ones included: list the hosts")
            malformed = [v for v in hosts
                         if len(v) > MAX_NET_HOST_CHARS or not NET_HOST_RE.fullmatch(v)]
            if malformed:
                raise ConfigError(
                    f"auth entry {i} net_allowlist has entries that are not host "
                    f"names: {malformed!r} (a lowercase name or address, or '*.' and "
                    f"a name; no scheme, port or path)")
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
            # Every field of the policy, the tool grants included: two
            # credentials for one principal that differ on any of them would
            # let either holder have the wider one, depending on which the
            # scheduler's lookup found first.
            policy = (frozenset(scopes), frozenset(tools), frozenset(secret_names),
                      frozenset(hosts))
            if identity in identity_policies and identity_policies[identity] != policy:
                raise ConfigError(
                    f"auth entry {i} conflicts with another credential policy for "
                    f"principal {principal!r}")
            seen_hashes.add(digest)
            identity_policies[identity] = policy
            parsed.append(Credential(
                digest, Principal(
                    principal.strip(), tenant.strip(), frozenset(scopes), frozenset(tools),
                    frozenset(secret_names), frozenset(hosts)),
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


def tenant_secret_path(root, tenant_id):
    """Where a tenant's secrets are under PI_TENANT_SECRETS_DIR: in a
    directory named by the SHA-256 of the tenant. A tenant's name may hold
    any character, and two names a case-insensitive file system folds
    together would otherwise share one directory, and so one credential."""
    return Path(root) / hashlib.sha256(tenant_id.encode()).hexdigest()


def _check_private(path, shown, *, directory, follow=False):
    """The status of ``path`` once it is checked to be a directory or a
    regular file that only the user this service runs as can reach. A
    symbolic link is refused unless ``follow``: its target lies outside what
    was checked. A missing path raises FileNotFoundError, for the caller to
    say what was expected there."""
    try:
        info = os.stat(path) if follow else os.lstat(path)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise ConfigError(
            f"{shown} cannot be read ({errno.errorcode.get(error.errno, error.errno)})"
        ) from None
    if stat.S_ISLNK(info.st_mode):
        raise ConfigError(
            f"{shown} is a symbolic link; the tenant secret source holds directories "
            f"and regular files only")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ConfigError(f"{shown} is not a {'directory' if directory else 'regular file'}")
    if info.st_uid != os.geteuid():
        raise ConfigError(
            f"{shown} must be owned by the user the service runs as "
            f"(uid {os.geteuid()}), not by uid {info.st_uid}")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        raise ConfigError(
            f"{shown} must not be accessible to its group or to other users: its mode "
            f"is {oct(mode)}; make it {'0700' if directory else '0600 or 0400'}")
    return info


def _read_secret_value(path, checked, shown):
    """The value in the file ``checked`` (its status, from _check_private):
    one line of printable ASCII with no spaces, at most
    MAX_TENANT_SECRET_BYTES, one trailing newline dropped. The file read must
    be the file checked. A value is never part of an error: it may be a
    credential with a typo in it."""
    try:
        with open(path, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (checked.st_dev, checked.st_ino):
                raise ConfigError(f"{shown} was replaced while it was read")
            raw = handle.read(MAX_TENANT_SECRET_BYTES + 3)
    except OSError as error:
        raise ConfigError(
            f"{shown} cannot be read ({errno.errorcode.get(error.errno, error.errno)})"
        ) from None
    if raw.endswith(b"\r\n"):
        raw = raw[:-2]
    elif raw.endswith(b"\n"):
        raw = raw[:-1]
    if len(raw) > MAX_TENANT_SECRET_BYTES:
        raise ConfigError(f"{shown} is longer than {MAX_TENANT_SECRET_BYTES} bytes")
    if not raw:
        raise ConfigError(f"{shown} is empty")
    if not all(0x21 <= byte <= 0x7E for byte in raw):
        raise ConfigError(
            f"{shown} must hold one line of printable ASCII with no spaces "
            f"(a trailing newline is allowed)")
    return raw.decode("ascii")


class TenantSecrets:
    """Each tenant's tool credentials, {tenant: {secret name: value}} (audit
    F-09).

    A principal's turn is given the values of the secrets its own policy
    names, from its own tenant's, and nothing else. Product startup reads
    them once from the directory the operator mounts (``from_directory``),
    as it reads the auth file once: a change takes a restart. The values are
    never shown, in a repr or in an error."""

    def __init__(self, values=None):
        self._values = {tenant: dict(secrets) for tenant, secrets in (values or {}).items()}

    def __repr__(self):
        return f"<TenantSecrets: {sum(map(len, self._values.values()))} value(s)>"

    def missing(self, principal):
        """The secrets ``principal``'s policy names that its tenant has no
        value for."""
        held = self._values.get(principal.tenant_id, {})
        return sorted(name for name in principal.secrets if not held.get(name))

    def for_principal(self, principal):
        """{name: value} for each secret ``principal``'s policy names. A
        KeyError (naming what is missing, never a value) when its tenant has
        no value for one."""
        missing = self.missing(principal)
        if missing:
            raise KeyError(missing)
        held = self._values[principal.tenant_id] if principal.secrets else {}
        return {name: held[name] for name in sorted(principal.secrets)}

    @classmethod
    def from_directory(cls, root, principals):
        """The secrets ``principals`` name, read from ``root``
        (PI_TENANT_SECRETS_DIR; None when it is not set).

        The layout is ``<root>/<SHA-256 of the tenant>/<secret name>``
        (tenant_secret_path), one value per file, as secret stores and
        orchestrators mount secrets. Only the names some policy names are
        read. What another local user could read or replace is refused: the
        root, each tenant's directory and each file must belong to the user
        the service runs as and give its group and others no access at all.
        Inside the root nothing may be a symbolic link; the root itself may
        be reached through one, since the operator names it (and /tmp and
        /var are links on macOS). The file read must be the file checked.

        A policy that names a secret its tenant has no value for is a
        ConfigError, as a policy that names an unknown tool is."""
        wanted = {}
        for principal in principals:
            for name in principal.secrets:
                wanted.setdefault(principal.tenant_id, {}).setdefault(
                    name, []).append(principal.principal_id)
        if root is None:
            if wanted:
                tenant, names = sorted(wanted.items())[0]
                name, holders = sorted(names.items())[0]
                raise ConfigError(
                    f"principal {sorted(holders)[0]!r} of tenant {tenant!r} names "
                    f"secret {name!r}, which has no value: set PI_TENANT_SECRETS_DIR "
                    f"to the directory that holds each tenant's secrets")
            return cls()
        shown_root = f"PI_TENANT_SECRETS_DIR ({root})"
        try:
            _check_private(root, shown_root, directory=True, follow=True)
        except FileNotFoundError:
            raise ConfigError(f"{shown_root} does not exist") from None
        values = {}
        for tenant, names in sorted(wanted.items()):
            directory = tenant_secret_path(root, tenant)
            try:
                _check_private(directory, str(directory), directory=True)
            except FileNotFoundError:
                name, holders = sorted(names.items())[0]
                raise ConfigError(
                    f"principal {sorted(holders)[0]!r} of tenant {tenant!r} names "
                    f"secret {name!r}, which has no value: {directory} does not "
                    f"exist") from None
            for name, holders in sorted(names.items()):
                path = directory / name
                try:
                    checked = _check_private(path, str(path), directory=False)
                except FileNotFoundError:
                    raise ConfigError(
                        f"principal {sorted(holders)[0]!r} of tenant {tenant!r} names "
                        f"secret {name!r}, which has no value: {path} does not "
                        f"exist") from None
                values.setdefault(tenant, {})[name] = _read_secret_value(
                    path, checked, str(path))
        return cls(values)


def principal_tool_grants(principal, tenant_secrets):
    """The tool grants of ``principal``'s turn, as turn_with_usage keywords
    (audit F-09): the values of the secrets its policy names, its own
    tenant's, and the hosts its fetch may reach. None when its tenant has no
    value for a secret it names.

    Only what the policy grants is passed. For a principal that names
    neither that is nothing, which the agent reads as no grant at all: a
    principal's turn never falls back to the deployment's. An agent written
    before these keywords existed then serves such a principal unchanged."""
    grants = {}
    if principal.secrets:
        if tenant_secrets is None or tenant_secrets.missing(principal):
            return None
        grants["secrets"] = tenant_secrets.for_principal(principal)
    if principal.net_allowlist:
        grants["net_allowlist"] = sorted(principal.net_allowlist)
    return grants


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
    def __init__(self, store, lease_id, incremental_tokens=False):
        self.store = store
        self.lease_id = lease_id
        self.incremental_tokens = incremental_tokens
        self._token_budget = None
        self._released = False

    def token_budget(self, limit):
        if not self.incremental_tokens or self._released:
            raise ValueError("incremental token lease required")
        if self._token_budget is None:
            self._token_budget = _TurnTokenBudget(self, limit)
        elif self._token_budget.limit != limit:
            raise ValueError("turn token limit cannot change during a lease")
        return self._token_budget

    def release(self, *, usage=None, session_id=None,
                storage_bytes=None, audit_bytes=None, last_activity_unix=None):
        if not self._released:
            self.store.settle_turn(
                self.lease_id, usage=None if self.incremental_tokens else usage, session_id=session_id,
                storage_bytes=storage_bytes, audit_bytes=audit_bytes,
                last_activity_unix=last_activity_unix)
            self._released = True


class _TurnTokenBudget:
    """Per-turn ceiling over durable per-attempt debits, including uncertain calls."""

    def __init__(self, lease, limit):
        if type(limit) is not int or limit <= 0:
            raise ValueError("turn token limit must be positive")
        self.lease, self.limit, self.spent = lease, limit, 0

    def reserve(self, amount):
        if type(amount) is not int or amount <= 0:
            raise ValueError("token reservation must be positive")
        if self.spent + amount > self.limit:
            raise ProductError(429, "turn_token_budget_exhausted",
                               "turn token budget exhausted; completed progress was saved")
        if self.lease._released:
            raise ProductError(409, "token_lease_expired", "turn token lease expired")
        day = self.lease.store.reserve_model_tokens(self.lease.lease_id, amount)
        self.spent += amount
        return _TokenAttempt(self, amount, day)


class _TokenAttempt:
    def __init__(self, budget, amount, day):
        self.budget, self.amount, self.day = budget, amount, day
        self.settled = False

    def settle(self, actual):
        if type(actual) is not int or actual < 0:
            raise ValueError("invalid provider usage")
        if self.settled:
            return
        budget = self.budget
        budget.lease.store.reconcile_model_tokens(
            budget.lease.lease_id, self.day, actual - self.amount)
        budget.spent += actual - self.amount
        self.settled = True
        if actual > self.amount:
            # Preserve the actual charge. Clamping usage would conceal a
            # provider that exceeded the preflight allowance.
            raise ProductError(502, "provider_token_budget_exceeded",
                               "provider exceeded its token allowance; turn stopped")


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
        ensure_directory(self.path.parent)
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

    def acquire_turn(self, tenant_id, *, session_id=None, incremental_tokens=False):
        if incremental_tokens and not self.resource_limits_enabled:
            raise ConfigError("incremental token budgets require resource quotas")
        now = self.clock()
        tenant = self._tenant(tenant_id)
        token_reservation = 0 if incremental_tokens else self.token_reservation_per_turn
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
                if tokens + reserved_tokens + token_reservation > (
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
                 token_reservation, self.storage_reservation_per_turn,
                 self.audit_reservation_per_turn))
            db.execute("COMMIT")
        return _DurableTurnLease(self, lease_id, incremental_tokens)

    def reserve_model_tokens(self, lease_id, amount):
        """Debit before dispatch. A lost process cannot refund uncertain spending.

        Existing token_windows and turn_leases suffice: no schema migration,
        and older readers conservatively see the debit as consumed tokens.
        """
        now = self.clock()
        day = int(now // 86400) * 86400
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT tenant FROM turn_leases WHERE lease_id=? AND expires>?",
                             (lease_id, now)).fetchone()
            if row is None:
                raise ProductError(409, "token_lease_expired", "turn token lease expired")
            tenant = row[0]
            current = db.execute("SELECT tokens FROM token_windows WHERE tenant=? AND window_start=?",
                                 (tenant, day)).fetchone()
            used = current[0] if current else 0
            held = db.execute("SELECT COALESCE(SUM(token_reservation),0) FROM turn_leases "
                              "WHERE tenant=? AND expires>?", (tenant, now)).fetchone()[0]
            if used + held + amount > self.tokens_per_day:
                raise ProductError(429, "token_quota_exceeded", "tenant daily token quota exceeded",
                                   retry_after=max(1, int(day + 86400 - now)))
            db.execute("INSERT INTO token_windows(tenant,window_start,tokens) VALUES(?,?,?) "
                       "ON CONFLICT(tenant) DO UPDATE SET window_start=excluded.window_start, tokens=excluded.tokens",
                       (tenant, day, used + amount))
            db.execute("COMMIT")
        return day

    def reconcile_model_tokens(self, lease_id, day, difference):
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT tenant FROM turn_leases WHERE lease_id=? AND expires>?",
                             (lease_id, self.clock())).fetchone()
            if row is None:
                raise ProductError(409, "token_lease_expired", "turn token lease expired")
            # A previous day's receipt must never credit today's spending.
            db.execute("UPDATE token_windows SET tokens=tokens+? WHERE tenant=? AND window_start=?",
                       (difference, row[0], day))
            db.execute("COMMIT")

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

    def clear_session_bytes(self, tenant_sha256, session_sha256):
        """Record that a session's files are about to be removed: zero the
        bytes its row records, and keep the row.

        A deletion removes the files and then the row. If it stops between
        the two, the row is left with files that are gone, which is what
        lost state looks like to startup (`reconcile_and_report`). A row
        that records nothing expects nothing, so the deletion is retried
        and not reported.
        """
        with closing(self._connect()) as db:
            db.execute(
                "UPDATE session_usage SET storage_bytes=0, audit_bytes=0 "
                "WHERE tenant=? AND session=?", (tenant_sha256, session_sha256))

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

    def _registered(self):
        """Every registry row: (tenant, session, internal session, recorded
        storage bytes, recorded audit bytes)."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT tenant, session, internal_session, storage_bytes, "
                "audit_bytes FROM session_usage ORDER BY tenant, session").fetchall()
        if any(not isinstance(row[2], str) or not row[2] for row in rows):
            raise ConfigError(
                "quota usage contains an unmigrated session without registry identity")
        return rows

    def registered_sessions(self):
        return tuple(row[:3] for row in self._registered())

    def reconcile_registered(self, measure):
        """Repair exact file usage after crashes before accepting traffic."""
        return self.reconcile_and_report(measure)["sessions"]

    def reconcile_and_report(self, measure):
        """Set every registered session's bytes to what `measure` finds on
        disk, and count the rows whose files are gone.

        A row is the record of a settled turn, and its bytes are what that
        turn left on disk. Bytes recorded for a surface that now measures
        zero (conversation and sandbox files, or the audit chain) were lost
        outside the service: a power loss (none of these writes was synced
        before 2026-11-10), a failed disk, a partial restore. Such a row used
        to be set to zero without a word (audit F-13), and the tenant found
        an empty session. A deletion zeroes its row first
        (`clear_session_bytes`), so one that was interrupted is not counted.
        Returns {"sessions", "sessions_missing_storage",
        "sessions_missing_audit"}.
        """
        measured = []
        missing_storage = missing_audit = 0
        for tenant, session, internal_session, storage, audit in self._registered():
            footprint = measure(internal_session)
            storage_bytes = max(0, int(footprint["storage_bytes"]))
            audit_bytes = max(0, int(footprint["audit_bytes"]))
            missing_storage += storage > 0 and storage_bytes == 0
            missing_audit += audit > 0 and audit_bytes == 0
            measured.append((
                storage_bytes, audit_bytes,
                max(0.0, float(footprint.get("last_activity_unix", 0))),
                tenant, session))
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany(
                "UPDATE session_usage SET storage_bytes=?, audit_bytes=?, "
                "last_activity_unix=MAX(last_activity_unix, ?) "
                "WHERE tenant=? AND session=?", measured)
            db.execute("COMMIT")
        return {
            "sessions": len(measured),
            "sessions_missing_storage": missing_storage,
            "sessions_missing_audit": missing_audit,
        }

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

    def transport_error(self, error_class):
        """A connection the transport answered or lost without dispatching
        it, counted by class only, in sigil_pi_http_errors_total.

        It is kept out of the request series, which holds the requests the
        service routed. The error-rate alert divides that series, and a 501
        is a 5xx any client can cause with a made-up method."""
        with self._lock:
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
    for key, help_text in (
            ("compiler_process_count", "Live compiler processes."),
            ("compiler_resident_memory_bytes",
             "Resident memory summed over the live compiler processes."),
            ("compiler_thread_count", "Threads summed over the live compiler processes."),
            ("compiler_open_fd_count",
             "Open file descriptors summed over the live compiler processes.")):
        if resources.get(key) is not None:
            name = "sigil_pi_" + key
            family(name, "gauge", help_text)
            sample(name, resources[key])

    # Numbers only. A string setting (the model, the provider's host) would
    # have to be a label, and docs/api.md keeps model values out of labels.
    for key, value in sorted(metrics.get("effective_settings", {}).items()):
        if isinstance(value, (bool, int, float)):
            name = "sigil_pi_setting_" + key
            family(name, "gauge", "Effective product setting.")
            sample(name, value)

    quota = metrics.get("tenant_quota_totals", {})
    for key, value in sorted(quota.items()):
        name = "sigil_pi_quota_" + key
        family(name, "gauge", "Aggregate tenant quota usage without tenant labels.")
        sample(name, value)

    for prefix in ("audit_verification", "retention", "scheduler"):
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


def _log_event(sink, record):
    """Hand an event to its log sink, which may fail.

    In production the sink is stdout, and a full disk under it raises. Each
    loop records what an event says, in its counters and its health, before
    it logs it, so readiness and the metrics lose nothing when the line is
    lost. An event that cannot be written must not end the loop that tried to
    write it (audit F-05), nor turn a check that passed into one that failed.
    Nor may it cost a request its answer: dispatch() counts a request before
    it logs it, and the transport counts what it logs (audit F-25).
    """
    try:
        sink(record)
    except Exception:
        pass


def _audit_write_failure(error):
    """Whether a turn ended because a forge's audit record could not be
    written: ``error`` is the AuditWriteError the product's strict log raised,
    or was raised while one propagated, however many errors deep.

    The product answers a turn's failure by its type, and a record that
    could not be written outranks every other cause (audit F-04). The fault
    behind it, a full disk or an I/O error, usually fails what runs next as
    well: the settlement that reads the session's footprint, the quota
    write. Raised in a ``finally`` while the AuditWriteError propagated, that
    error replaced it, and the turn was answered as whatever failed last.
    Both links are followed, ``__context__`` too when ``from None`` hid it.
    """
    seen = set()
    pending = [error]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        if isinstance(current, AuditWriteError):
            return True
        seen.add(id(current))
        pending.extend((current.__cause__, current.__context__))
    return False


# The stable answer to a chat turn whose audit record could not be written.
AUDIT_WRITE_FAILED = ("audit_write_failed", "turn audit record could not be written")


def _report_audit_write_failure(monitor, turn):
    """Tell the audit monitor, when there is one, that a record could not be
    written. Components are asked for what they offer, as readiness asks
    them; a monitor that keeps no count of failed writes is not told."""
    report = getattr(monitor, "write_failed", None)
    if callable(report):
        report(turn=turn)


def _running_thread(loop, thread, stop):
    """The thread a background loop is running on, or None when ``start()``
    should begin one.

    A ``stop()`` that timed out leaves the old thread finishing its round with
    the stop flag set. Returning that thread, as ``start()`` once did, reports
    a loop that ends a moment later and is never started again, so that case
    is refused.
    """
    if thread is None or not thread.is_alive():
        return None
    if stop.is_set():
        raise RuntimeError(
            f"{loop} is still stopping; start it again once stop() has returned True")
    return thread


def _ended_on_its_own(thread, stop):
    """Whether a background loop's thread has ended without a stop() request."""
    return thread is not None and not thread.is_alive() and not stop.is_set()


class InFlightWork:
    """The work a drain waits for, and the switch that refuses more of it.

    A chat turn, a scheduled turn, an export, a deletion and a schedule change
    each enter before they touch state and leave when they have finished. Once
    draining has begun nothing enters, so the count only falls, and the drain
    is complete when it reaches zero. The service and the scheduler it runs
    with share one of these: scheduled turns once ignored the draining state,
    and only chat turns were counted (audit F-07, F-26).
    """

    def __init__(self):
        self._changed = threading.Condition()
        self._draining = False
        self._active = 0

    @property
    def draining(self):
        with self._changed:
            return self._draining

    def start_draining(self):
        with self._changed:
            self._draining = True

    def enter(self):
        """Count one piece of work as in flight. False, and nothing is
        counted, once draining has begun."""
        with self._changed:
            if self._draining:
                return False
            self._active += 1
            return True

    def leave(self):
        with self._changed:
            self._active -= 1
            self._changed.notify_all()

    def wait(self, timeout_s):
        """Wait until nothing is in flight; False if that takes longer than
        ``timeout_s``."""
        deadline = time.monotonic() + timeout_s
        with self._changed:
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._changed.wait(remaining)
            return True


class AuditVerificationMonitor:
    """Periodically verify every signed audit chain without logging contents,
    and keep the count of audit records that could not be written.

    Readiness reads both through ``is_healthy()``, as the one
    ``audit_verification`` component: a chain that fails verification, or a
    record that could not be written (``write_failed``), fails it. The
    metrics keep them apart, so that an alert can tell a full disk from a
    broken chain: ``healthy`` is the last verification's verdict,
    ``writes_healthy`` is false once a record could not be written, and
    ``write_failures_total`` counts those records.
    """

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
        self._write_failures = 0

    @staticmethod
    def _print_log(record):
        print(json.dumps(record, sort_keys=True, separators=(",", ":")), flush=True)

    def write_failed(self, *, turn):
        """A forge's audit record could not be written, and the turn it
        belonged to failed with it (audit F-04). ``turn`` is the path that
        ran it, ``chat`` or ``scheduled``, and is all the event says: no
        tenant, session, file name or OS error reaches the log.

        The count is kept before the event is logged, so a sink that fails
        on the same full disk loses only the line. From the first failure
        this monitor is unhealthy until the process is replaced. A
        verification that passes later does not clear it: the chain a failed
        append leaves is a valid prefix and verifies, so it is no evidence
        that every execution since was recorded; and readiness that failed
        would keep away the traffic that could show writes work again.
        Startup verifies every chain before the replacement serves.
        """
        with self._lock:
            self._write_failures += 1
        _log_event(self.log_sink, {
            "event": "audit_write",
            "severity": "critical",
            "status": "failed",
            "turn": turn,
        })

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
        _log_event(self.log_sink, {
            "event": "audit_verification",
            "severity": "info" if ok else "critical",
            "status": "ok" if ok else "failed",
            "chains": chains,
            "records": records,
        })
        return ok

    def _check_did_not_complete(self):
        """A check that raised verified nothing: fail closed until one completes."""
        with self._lock:
            self._checks += 1
            self._failures += 1
            self._healthy = False
            self._chains = 0
            self._records = 0
        _log_event(self.log_sink, {
            "event": "audit_verification",
            "severity": "critical",
            "status": "failed",
            "chains": 0,
            "records": 0,
        })

    def is_healthy(self):
        snapshot = self.snapshot()
        return snapshot["healthy"] and snapshot["writes_healthy"]

    def snapshot(self):
        with self._lock:
            return {
                "checks_total": self._checks,
                "failures_total": self._failures,
                # The last verdict stands only while something is still
                # verifying: a thread that ended on its own verifies nothing.
                "healthy": self._healthy and not _ended_on_its_own(
                    self._thread, self._stop),
                "last_check_unix": self._last_check,
                "chains": self._chains,
                "records": self._records,
                "writes_healthy": self._write_failures == 0,
                "write_failures_total": self._write_failures,
            }

    def start(self):
        if _running_thread("the audit verification monitor",
                           self._thread, self._stop) is not None:
            return
        self._stop.clear()

        def loop():
            while not self._stop.wait(self.interval_s):
                # Nothing a check raises ends verification (audit F-05).
                try:
                    self.check()
                except Exception:
                    self._check_did_not_complete()

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        self._thread = thread

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

    @contextmanager
    def hold_if_free(self, digest):
        """Take, without waiting, the cross-worker lock of the session whose
        files are named `digest` (the sha256 of its internal id, which names
        its lock file too); yield whether it was taken.

        For startup's scratch sweep, which knows a session only by a file
        name. A session whose lock is held may have a save in flight, so its
        scratch is not orphaned. The file lock excludes `hold` in this
        process as well as in another; without a lock directory there is one
        process, whose startup runs before any turn, and nothing to take.
        """
        if self.lock_dir is None:
            yield True
            return
        with (self.lock_dir / f"{digest}.lock").open("a+b") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def size(self):
        with self._guard:
            return len(self._entries)


class ProductScheduleStore:
    """Tenant-scoped durable schedules with per-tenant quotas on how many
    schedules a tenant holds and how many bytes their messages take."""

    def __init__(self, path, max_per_tenant=32, clock=None, min_every_ms=1,
                 max_bytes_per_tenant=MAX_SCHEDULE_BYTES_PER_TENANT):
        if max_per_tenant <= 0:
            raise ConfigError("max_schedules_per_tenant must be positive")
        if (isinstance(min_every_ms, bool) or not isinstance(min_every_ms, int)
                or min_every_ms <= 0):
            raise ConfigError("schedule min_every_ms must be a positive integer")
        if (isinstance(max_bytes_per_tenant, bool)
                or not isinstance(max_bytes_per_tenant, int) or max_bytes_per_tenant <= 0):
            raise ConfigError("max_schedule_bytes_per_tenant must be a positive integer")
        self.path = Path(path)
        ensure_directory(self.path.parent)
        self.max_per_tenant = max_per_tenant
        # What a tenant's messages may take in the store (audit F-27): every
        # claim and completion reads and rewrites the whole store, so one
        # tenant's messages used to cost every other tenant's turns, charged
        # to no quota. product_main passes PI_MAX_SCHEDULE_BYTES_PER_TENANT.
        self.max_bytes_per_tenant = max_bytes_per_tenant
        # The shortest interval put() accepts; product_main passes
        # MIN_SCHEDULE_EVERY_MS. An entry stored below it, by a release that
        # had no floor, loads as it was written and runs no more often than
        # the floor (claim_due and ProductScheduler._due).
        self.min_every_ms = min_every_ms
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

    @staticmethod
    def _stored_bytes(message):
        """The bytes a message takes in the store, quotes aside: its UTF-8
        bytes, and the escapes JSON adds for a quote, a backslash or a
        control character, which _save writes as two to six bytes."""
        return len(json.dumps(message, ensure_ascii=False).encode()) - 2

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
        # The cross-process lock serializes writers. write_durably keeps a
        # killed writer's incomplete bytes away from the committed JSON, syncs
        # the new bytes before they take the name and the directory after, and
        # removes its scratch file when the save fails (audit F-13, F-30).
        write_durably(self.path, json.dumps(
            data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())

    def remove_scratch(self):
        """Remove the scratch files that killed saves left beside the store,
        which hold whole schedules, messages included; return how many.

        Under the store's lock, which every save holds, so a save in flight
        in another worker is never touched. Scratch is write_durably's
        `.<name>.<16 hex>.tmp` and, from releases before 2026-11-10,
        `.<name>.<pid>.tmp`. The removal is synced.
        """
        legacy = re.compile(rf"\.{re.escape(self.path.name)}\.[0-9]+\.tmp")
        with self._locked():
            leftovers = set(scratch_files(self.path))
            with os.scandir(self.path.parent) as entries:
                leftovers.update(self.path.parent / entry.name for entry in entries
                                 if legacy.fullmatch(entry.name))
            for leftover in leftovers:
                leftover.unlink(missing_ok=True)
            if leftovers:
                sync_directory(self.path.parent)
            return len(leftovers)

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
        if every_ms < self.min_every_ms:
            raise ProductError(400, "invalid_interval",
                               f"every_ms must be at least {self.min_every_ms}")
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
            # The tenant's messages may not grow past its byte quota. A put
            # that does not grow them is accepted, so a tenant over a quota
            # lowered since, or a store from before there was one, can still
            # replace a schedule (audit F-27).
            added = self._stored_bytes(message)
            others = sum(self._stored_bytes(entry.get("message"))
                         for other, entry in data.items()
                         if other != key and entry.get("tenant_sha256") == tenant_hash)
            if (others + added > self.max_bytes_per_tenant
                    and added > self._stored_bytes(prior.get("message", ""))):
                raise ProductError(409, "schedule_quota_exceeded",
                                   "tenant schedule byte quota reached")
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
            if entry is None or not ProductScheduler._due(entry, now, self.min_every_ms):
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

    def release_claim(self, key, owner):
        """Give back the claim of a run that never started.

        Nothing is recorded: the entry is as it was before ``claim_due``,
        still due and with no lease, so it does not wait out a lease that
        nobody holds.
        """
        with self._locked():
            data = self._load()
            entry = data.get(key)
            if entry is None or entry.get("lease_owner") != owner:
                return False
            entry.pop("lease_owner", None)
            entry.pop("lease_until", None)
            self._save(data)
            return True


class _ScheduledTurn:
    """An entry's place in the scheduler's pool, from just before its claim
    until its run is recorded or its claim given back."""

    __slots__ = ("key", "tenant", "started", "done")

    def __init__(self, key, tenant):
        self.key = key
        self.tenant = tenant
        self.started = time.monotonic()
        self.done = False


class _Tick:
    """What one tick() keeps.

    The tick owes the entries that were due at its first look (``owed``,
    until each is started or proves not this tick's to run) and waits for
    their turns (``mine``); its own thread runs the first of them
    (``first``). An entry that falls due later it starts but does not wait
    for, so a tick ends however busy the pool stays. Also what its looks
    raised, in the order they raised it, the entries the store refused to
    claim, and whether reading the entries failed, after which it starts
    nothing more."""

    __slots__ = ("owed", "mine", "first", "failures", "refused", "unlisted")

    def __init__(self):
        self.owed = None            # set at the first look
        self.mine = []
        self.first = None
        self.failures = []
        self.refused = set()
        self.unlisted = False

    def over(self):
        return not self.owed and all(turn.done for turn in self.mine)


class ProductScheduler:
    """Runs schedules under the creator's current tenant/tool policy.

    One thread runs ``tick()`` on a cadence. Nothing a tick raises ends that
    thread: a failed tick is counted and reported, and the next one runs
    (audit F-05). The loop reports its own liveness through ``is_healthy()``
    and ``snapshot()``, which readiness and the metrics read: whether the
    thread is alive, when a tick last completed, and whether anything has
    completed within the stall bound.

    The turns run on a pool, at most ``workers`` at once and at most
    ``turns_per_tenant`` of one tenant's. They once ran one after another on
    the loop's thread, in store order, so one tenant's due entries held up
    every other tenant's (audit F-06). The thread that calls ``tick()`` still
    runs the first turn the tick starts; every other turn has a thread of its
    own, and a looker thread goes on looking at the store while the first
    runs. A tick waits for the entries that were due when it began, and
    starts, without waiting for them, those that fall due meanwhile.
    """

    def __init__(self, store: ProductScheduleStore, agent, auth: AuthRegistry,
                 clock=None, interval_s=1.0, session_locks=None, lease_s=300,
                 quota_store=None, data_manager=None, turn_deadline_s=120,
                 log_sink=None, work=None, workers=SCHEDULER_WORKERS,
                 turns_per_tenant=SCHEDULED_TURNS_PER_TENANT, audit_monitor=None,
                 tenant_secrets=None, max_tokens_per_turn=None):
        self.store = store
        self.agent = agent
        self.auth = auth
        # Each tenant's tool credentials (TenantSecrets), shared with the
        # service: a scheduled turn's grants come from its creator's policy
        # as it stands when the turn runs, as its tools do (audit F-09).
        self.tenant_secrets = tenant_secrets
        if max_tokens_per_turn is not None:
            if type(max_tokens_per_turn) is not int or max_tokens_per_turn <= 0:
                raise ConfigError("max_tokens_per_turn must be a positive integer")
            if quota_store is None or not quota_store.resource_limits_enabled:
                raise ConfigError("turn token budgets require resource quotas")
        self.max_tokens_per_turn = max_tokens_per_turn
        # The service's audit monitor, when product startup shares it: a
        # scheduled turn whose audit record could not be written is counted
        # there and fails readiness, exactly as a chat turn's is.
        self.audit_monitor = audit_monitor
        self.clock = clock or time
        self.interval_s = interval_s
        # The service's count of work in flight, when product startup shares
        # it: a scheduled turn is then refused while the service drains, and
        # waited for, exactly as a chat turn is.
        self.work = work if work is not None else InFlightWork()
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
        for name, value in (("workers", workers), ("turns_per_tenant", turns_per_tenant)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ConfigError(f"scheduler {name} must be a positive integer")
        self.lease_s = lease_s
        self.turn_deadline_s = turn_deadline_s
        self.workers = workers
        self.turns_per_tenant = turns_per_tenant
        self.log_sink = log_sink or self._print_log
        # A scheduled turn may take as long as its lease allows, and the loop
        # waits one interval between ticks. A scheduler that has completed
        # neither a tick nor a run for longer than both is stuck, not busy.
        self.stall_after_s = lease_s + interval_s
        self.owner = f"{os.getpid()}-{uuid.uuid4().hex}"
        # The pool, all under _guard: the entries whose turn is in flight in
        # this process (one entry never runs twice at once), the turns in
        # flight, each tenant's share of them, and when each tenant was last
        # given a turn, so that a full pool serves tenants in turn.
        self._running = set()
        self._guard = threading.Lock()
        self._turn_done = threading.Condition(self._guard)
        self._pool = set()
        self._tenant_turns = {}
        # What recording a turn's run raised. A tick waits only for the turns
        # it owes, so the turn's own tick may have ended: the next tick to
        # end reports it.
        self._unreported = []
        self._served = {}
        self._dispatches = 0
        self._stop = threading.Event()
        self._thread = None
        self._liveness = threading.Lock()
        self._ticks = 0
        self._tick_failures = 0
        self._failing_ticks = 0     # in a row: a run of failures is one event
        self._last_tick = None      # this scheduler's clock when a tick last completed
        self._last_tick_at = None   # time.monotonic() at that moment
        self._progress_at = None    # ... or when a run was last recorded, if later

    @staticmethod
    def _print_log(record):
        print(json.dumps(record, sort_keys=True, separators=(",", ":")), flush=True)

    @staticmethod
    def _due(entry, now, min_every_ms=1):
        """Whether an entry is due at ``now``. An interval below the store's
        floor counts as the floor, so a schedule stored before there was one
        runs no more often than a schedule created now could."""
        return (entry.get("last_run") is None
                or (now - entry["last_run"]) * 1000.0
                >= max(entry["every_ms"], min_every_ms))

    @staticmethod
    def _due_since(entry, min_every_ms):
        """When an entry fell due: when it was created, if it never ran."""
        if entry.get("last_run") is None:
            return entry.get("created_unix", 0)
        return entry["last_run"] + max(entry["every_ms"], min_every_ms) / 1000.0

    def _halted(self):
        """Whether nothing more may start: this scheduler was asked to stop,
        or the service it runs with is draining."""
        return self._stop.is_set() or self.work.draining

    def tick(self):
        """Start the turn of each due entry the pool has room for. Return once
        each entry that was due at the tick's first look has run once, or
        proved not this tick's to run, and its turn has finished.

        The calling thread runs the first turn itself; each other turn runs
        on a thread of its own, at most ``workers`` at once and at most
        ``turns_per_tenant`` of one tenant's. Until the tick ends, a looker
        looks at the store again whenever a turn finishes, and at least every
        ``interval_s``, and starts what has fallen due or found room since: a
        tenant's next entry once its turn is done, another tenant's at once.
        An entry that falls due after the first look is started but not
        waited for, so a tick ends however busy the pool stays; its turn goes
        on in the pool. Turns once ran inline, one after another in store
        order, so one tenant's due entries held up every other tenant's
        (audit F-06). When the pool is full, the tenant served least recently
        goes first, and of a tenant's own entries the one due longest. The
        clock is read for each claim, so a lease runs from when its claim is
        made, not from when the tick began.

        A scheduled run that fails is state: it is recorded as the entry's
        ``last_status`` and the tick goes on. A schedule store that fails is
        not: reading the entries or claiming one fails the tick, and so does
        recording a run, which fails the next tick to end once it has
        happened; the loop in ``start()`` counts and reports it. An entry the
        store refuses to claim is not tried again in the same tick, and does
        not hold up the others.

        Nothing starts once a stop or a drain has been requested. The tick
        looks before each claim and claims no more; it used to claim and run
        every entry that was due, long after the request (audit F-07). Each
        turn looks again directly before the agent is asked, because a
        claimed entry can wait for its session for as long as a turn lasts:
        that turn does not start, its claim is given back and nothing is
        recorded, so the entry is still due when a scheduler next starts.
        From before its claim until its run is recorded, an entry is counted
        as work in flight, which is what a drain waits for.
        """
        if self._halted():
            return
        tick = _Tick()
        self._look_again(tick, keep_first=True)
        looker = None
        if tick.first is not None:
            try:
                looker = self._start_thread("pi-scheduler-look", self._keep_looking, tick)
            except Exception:
                pass
            try:
                self._run_turn(*tick.first)
            finally:
                if looker is not None:
                    looker.join()
            if looker is None:
                # No thread to look with while the turn ran: this one looks
                # now that it is done, and goes on until the tick is over.
                self._look_again(tick)
        if looker is None:
            self._keep_looking(tick)
        with self._guard:
            failures = tick.failures + self._unreported
            self._unreported = []
        if failures:
            raise failures[0]

    def _keep_looking(self, tick):
        """Until the tick is over: wait for a turn to finish, or for
        ``interval_s``, and look again."""
        while True:
            stopped = tick.unlisted or self._halted()
            with self._turn_done:
                if stopped:
                    # Nothing more starts in this tick: what it still owes
                    # stays due, for the next scheduler.
                    tick.owed = set()
                if tick.over():
                    return
                self._turn_done.wait(self.interval_s)
            self._look_again(tick)

    def _look_again(self, tick, keep_first=False):
        if tick.unlisted or self._halted():
            return
        try:
            self._look(tick, keep_first)
        except Exception as error:
            # Reading the entries failed. The turns already started are
            # waited for; then the tick fails with it.
            tick.failures.append(error)
            tick.unlisted = True

    def _look(self, tick, keep_first=False):
        """One look at the store: claim each due entry the pool has room for,
        in the order tick() describes, and start its turn on a thread of its
        own. With ``keep_first`` the first entry claimed is not started but
        kept, as ``tick.first`` (its place in the pool and the claimed entry),
        for the tick's own thread to run."""
        floor = self.store.min_every_ms
        now = self.clock.time()
        listing = self.store.entries()
        due = []
        for index, (key, entry) in enumerate(listing):
            if key in tick.refused:
                continue
            try:
                # Not due, or claimed and its lease still runs: a claim would
                # read and rewrite the store for nothing.
                if not self._due(entry, now, floor) or entry.get("lease_until", 0) > now:
                    continue
                since = float(self._due_since(entry, floor))
            except Exception:
                # An entry this look cannot read is left to claim_due, which
                # refuses it alone, as it did before there was a look.
                since = float("-inf")
            due.append((since, index, key, entry.get("tenant_sha256")))
        due.sort(key=lambda candidate: candidate[:2])
        due_now = {key for _, _, key, _ in due}
        if tick.owed is None:
            tick.owed = due_now         # what this tick owes: the entries due at its first look
        else:
            # One no longer due has run, gone, or been claimed elsewhere.
            tick.owed &= due_now
        with self._guard:
            listed = {entry.get("tenant_sha256") for _, entry in listing}
            self._served = {tenant: at for tenant, at in self._served.items()
                            if tenant in listed}
            served = dict(self._served)
        # Every tenant's first entry before any tenant's second; among them,
        # the tenant given a turn least recently first, then the entry due
        # longest.
        ranks = {}
        order = []
        for position, (_, _, key, tenant) in enumerate(due):
            rank = ranks.get(tenant, 0)
            ranks[tenant] = rank + 1
            order.append((rank, served.get(tenant, 0), position, key, tenant))
        order.sort(key=lambda candidate: candidate[:3])
        for _, _, _, key, tenant in order:
            if self._halted():
                break
            turn = _ScheduledTurn(key, tenant)
            with self._guard:
                if len(self._pool) >= self.workers:
                    break           # the pool is full: the rest wait for room
                if (key in self._running
                        or self._tenant_turns.get(tenant, 0) >= self.turns_per_tenant):
                    continue
                self._running.add(key)
                self._pool.add(turn)
                self._tenant_turns[tenant] = self._tenant_turns.get(tenant, 0) + 1
            if not self.work.enter():
                self._finish(turn, counted=False)     # the drain began just now
                break
            try:
                entry = self.store.claim_due(key, self.clock.time(), self.owner, self.lease_s)
            except Exception as error:
                # Claiming rewrites the store, so a store that can be read but
                # not written fails here for every due entry. Nothing was
                # claimed, so there is no run to record as state, and
                # swallowed here the failure left no trace at all. The other
                # entries are still served; then the tick fails.
                tick.refused.add(key)
                tick.failures.append(error)
                entry = None
            if entry is None:
                tick.owed.discard(key)
                self._finish(turn)
                continue
            with self._guard:
                self._dispatches += 1
                self._served[tenant] = self._dispatches
            if key in tick.owed:
                tick.owed.discard(key)
                tick.mine.append(turn)
            if keep_first and tick.first is None:
                tick.first = (turn, entry)
                continue
            try:
                self._start_thread("pi-scheduled-turn", self._run_turn, turn, entry)
            except Exception as error:
                # No thread for the turn: its claim is given back, as for a
                # turn the stop held back, it is not tried again in this
                # tick, and the tick fails.
                tick.failures.append(error)
                tick.refused.add(key)
                try:
                    self.store.release_claim(key, self.owner)
                except Exception as release_error:
                    tick.failures.append(release_error)
                finally:
                    self._finish(turn)
                break

    @staticmethod
    def _start_thread(name, target, *args):
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        thread.start()
        return thread

    def _finish(self, turn, *, counted=True):
        """Give an entry's place in the pool back, and wake the ticks."""
        with self._guard:
            self._running.discard(turn.key)
            self._pool.discard(turn)
            left = self._tenant_turns[turn.tenant] - 1
            if left:
                self._tenant_turns[turn.tenant] = left
            else:
                del self._tenant_turns[turn.tenant]
        if counted:
            self.work.leave()
        with self._turn_done:
            turn.done = True
            self._turn_done.notify_all()

    def _run_turn(self, turn, entry):
        """A claimed entry's turn: run it, record it (or give its claim
        back), and leave the pool. What recording raises fails the next tick
        to end."""
        key = turn.key
        status = "failed"
        held_back = False
        try:
            try:
                deadline = time.monotonic() + self.turn_deadline_s
                with self.session_locks.hold(
                        entry["internal_session"], deadline_monotonic=deadline):
                    # Deletion can remove an entry after tick's snapshot. Re-read
                    # under the session operation lock so a stale snapshot cannot
                    # resurrect deleted state by running one final turn.
                    current = self.store.get(key)
                    if current is None:
                        return
                    entry = current
                    principal = self.auth.policy(entry["tenant_sha256"], entry["principal"])
                    if principal is None or "schedules:write" not in principal.scopes:
                        status = "policy_revoked"
                        return
                    known = set(getattr(self.agent, "manifest", {}))
                    tools = (known if principal.tools == frozenset({"*"})
                             else set(principal.tools))
                    if tools - known:
                        status = "policy_invalid"
                        return
                    grants = principal_tool_grants(principal, self.tenant_secrets)
                    if grants is None:
                        status = "policy_invalid"
                        return
                    turn_lease = (self.quota_store.acquire_turn(
                                      principal.tenant_id,
                                      session_id=entry["internal_session"],
                                      incremental_tokens=self.max_tokens_per_turn is not None)
                                  if self.quota_store is not None else None)
                    usage = None
                    footprint = None
                    # Whether the agent was asked for this turn. A turn that
                    # is held back never is, and the agent's count of tokens,
                    # kept per thread, is then not this turn's. Settled with
                    # it, the tenant could pay for another turn a second time.
                    asked = False
                    try:
                        if self._halted():
                            # The stop or the drain was requested while this
                            # entry waited for its session or its quota. Its
                            # turn does not start.
                            held_back = True
                            return
                        if self.max_tokens_per_turn is not None:
                            grants["token_budget"] = turn_lease.token_budget(self.max_tokens_per_turn)
                        asked = True
                        try:
                            _, usage = self.agent.turn_with_usage(
                                entry["internal_session"], entry["message"],
                                allowed_tools=tools, deadline_monotonic=deadline,
                                **grants)
                        finally:
                            if (self.quota_store is not None
                                    and self.quota_store.resource_limits_enabled):
                                footprint = self.data_manager.footprint(
                                    entry["internal_session"])
                    finally:
                        if turn_lease is not None:
                            telemetry = (self.agent.turn_telemetry() if asked
                                         and hasattr(self.agent, "turn_telemetry") else {})
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
            except Exception as error:
                # A scheduled failure is state, not a reason to stop every
                # tenant's schedule loop. No exception string reaches the
                # store. A record that could not be written outranks every
                # other cause, as it does for a chat turn (audit F-04).
                if _audit_write_failure(error):
                    status = "audit_write_failed"
                    _report_audit_write_failure(self.audit_monitor, "scheduled")
                elif isinstance(error, TimeoutError):
                    status = "deadline_exceeded"
                elif isinstance(error, ProductError) and error.code in {
                        "token_quota_exceeded", "storage_quota_exceeded",
                        "turn_token_budget_exhausted", "audit_quota_exceeded"}:
                    status = "quota_exceeded"
                else:
                    status = "failed"
            finally:
                if held_back:
                    # Nothing ran, so nothing is recorded: the entry is due,
                    # and unclaimed, for the next scheduler.
                    self.store.release_claim(key, self.owner)
                else:
                    self.store.complete_claim(key, self.owner, self.clock.time(), status)
                    self._progressed()
        except Exception as error:
            # Recording the run, or giving its claim back, rewrites the store,
            # so a full disk lands here, and fails the next tick to end: the
            # one that owes this turn waits for it. The entry is released
            # below all the same: a key left running would never run again in
            # this process. Its claim stays in the store and expires with its
            # lease.
            with self._guard:
                self._unreported.append(error)
        finally:
            self._finish(turn)

    def _progressed(self):
        with self._liveness:
            self._progress_at = time.monotonic()

    def _tick_completed(self):
        completed = self.clock.time()
        with self._liveness:
            self._ticks += 1
            self._last_tick = completed
            self._last_tick_at = self._progress_at = time.monotonic()
            failed, self._failing_ticks = self._failing_ticks, 0
        if failed:
            _log_event(self.log_sink, {
                "event": "scheduler_tick",
                "severity": "info",
                "status": "ok",
                "failed_ticks": failed,
            })

    def _tick_failed(self):
        with self._liveness:
            self._tick_failures += 1
            self._failing_ticks += 1
            first = self._failing_ticks == 1
        # No exception text: it can carry a path or a tenant's data. One event
        # for a run of failures, not one a second while a disk stays full;
        # the counter above has every one of them.
        if first:
            _log_event(self.log_sink, {
                "event": "scheduler_tick",
                "severity": "critical",
                "status": "failed",
            })

    def is_healthy(self):
        """Whether the loop is running, has completed something recently, and
        has no turn in flight past the stall bound."""
        return self.snapshot()["healthy"]

    def snapshot(self):
        """The loop's liveness, content-free, for readiness and the metrics.

        ``last_tick_age_seconds`` is the age of the last completed tick.
        A tick lasts as long as the turns it started, so that age alone would
        call a busy scheduler stuck. Health is judged by
        ``last_progress_age_seconds`` instead: the time since the loop last
        completed a tick or recorded a scheduled run. The loop is healthy
        while its thread is alive, that age is within ``stall_after_seconds``
        (the schedule lease plus the tick interval), and no turn has been in
        flight for longer than that. One turn that did not return used to
        stop every tenant's schedules, and the age showed it; with the pool
        the other tenants' runs keep the age fresh. A scheduler that was
        never started, or has stopped, is not healthy.
        """
        thread = self._thread
        alive = thread is not None and thread.is_alive()
        now = time.monotonic()

        # Every age the verdict compares with the bound is the age as the
        # report gives it, to the millisecond. A turn judged by its exact age
        # made the loop unhealthy while the progress age it reported was no
        # more than the bound.
        def age(at):
            return None if at is None else round(max(0.0, now - at), 3)

        with self._guard:
            stuck = any(age(turn.started) > self.stall_after_s for turn in self._pool)
        with self._liveness:
            tick_age, progress_age = age(self._last_tick_at), age(self._progress_at)
            return {
                "healthy": (alive and not stuck and progress_age is not None
                            and progress_age <= self.stall_after_s),
                "thread_alive": alive,
                "ticks_total": self._ticks,
                "tick_failures_total": self._tick_failures,
                "last_tick_unix": self._last_tick,
                "last_tick_age_seconds": tick_age,
                "last_progress_age_seconds": progress_age,
                "stall_after_seconds": self.stall_after_s,
            }

    def start(self):
        running = _running_thread("the product scheduler", self._thread, self._stop)
        if running is not None:
            return running
        self._stop.clear()
        self._progressed()      # the stall bound runs from here to the first tick

        def run():
            while not self._stop.is_set():
                # One error from the schedule store ended this thread, and
                # with it every tenant's schedules until the process was
                # restarted (audit F-05). Nothing a tick raises ends it now.
                try:
                    self.tick()
                    self._tick_completed()
                except Exception:
                    self._tick_failed()
                self._stop.wait(self.interval_s)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self._thread = thread
        return thread

    def stop(self, timeout_s=None):
        """Ask the loop to stop, and wait up to ``timeout_s`` for its thread
        and for every turn in the pool, whichever tick started it. True once
        nothing of this scheduler is left running: product_main's drain
        relies on that."""
        self._stop.set()
        with self._turn_done:
            self._turn_done.notify_all()    # a tick waiting to look again sees the stop now
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        if self._thread is not None:
            self._thread.join(timeout_s)
            if self._thread.is_alive():
                return False
        with self._turn_done:
            while self._pool:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._turn_done.wait(remaining)
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
        """Remove every file that holds the session's content; caller owns
        the session lock.

        That includes the scratch files killed saves left beside the
        conversation (audit F-30): SessionStore.delete takes them with it.
        Each removal is synced into its directory before this returns, so a
        power loss cannot bring a file back after the registry row that
        attributed it is gone; startup refuses state it cannot attribute
        (audit F-13).
        """
        session_path, sandbox, audit_path = self._paths(internal_session)
        deleted = {
            "conversation": session_path.exists(),
            "sandbox": sandbox.exists(),
            "audit": audit_path.exists(),
        }
        self.agent.store.delete(internal_session)
        if sandbox.exists():
            shutil.rmtree(sandbox)
            sync_directory(sandbox.parent)
        if audit_path.exists():
            audit_path.unlink()
            sync_directory(audit_path.parent)
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

    def remove_orphaned_scratch(self):
        """Remove the scratch files that killed saves left: conversations
        (audit F-30) and schedules. Returns how many were removed; the
        removals are synced.

        For startup, before this worker serves. A save still in flight then
        belongs to another worker, which holds that session's lock or the
        schedule store's, so a locked session's scratch is left for a later
        start.
        """
        store = self.agent.store
        removed = 0
        for scratch in store.scratch_files():
            digest = re.search(r"[0-9a-f]{64}", scratch.name).group()
            with self.session_locks.hold_if_free(digest) as free:
                if free:
                    try:
                        scratch.unlink()
                    except FileNotFoundError:
                        continue  # renamed into place by the save that owned it
                    removed += 1
        if removed:
            sync_directory(store.dir)
        return removed + self.schedule_store.remove_scratch()

    @staticmethod
    def _print_log(record):
        print(json.dumps(record, sort_keys=True, separators=(",", ":")), flush=True)

    def reconcile_at_startup(self, log_sink=None):
        """Bring the files and the registry into agreement before this
        worker serves, and say what was found.

        Refuses files no registry row attributes, as before. Then removes
        orphaned scratch, sets every row's bytes to what is on disk, and
        emits one `state_reconciliation` event that carries counts and
        nothing else. It is critical when a row recorded bytes for files
        that are gone (`DurableQuotaStore.reconcile_and_report`). Startup
        goes on all the same: refusing every tenant would bring no file
        back. The event is then the only trace, because the row now agrees
        with the disk. Returns whether nothing was lost.
        """
        if self.quota_store is None:
            raise ConfigError("startup reconciliation requires the durable quota registry")
        self.verify_quota_registry(self.quota_store.registered_sessions())
        removed = self.remove_orphaned_scratch()
        report = self.quota_store.reconcile_and_report(self.footprint)
        lost = bool(report["sessions_missing_storage"] or report["sessions_missing_audit"])
        (log_sink or self._print_log)({
            "event": "state_reconciliation",
            "severity": "critical" if lost else "info",
            "status": "failed" if lost else "ok",
            **report,
            "scratch_files_removed": removed,
        })
        return not lost

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
            if self.quota_store is not None:
                # Before any file goes: see clear_session_bytes.
                self.quota_store.clear_session_bytes(
                    hashlib.sha256(principal.tenant_id.encode()).hexdigest(),
                    hashlib.sha256(internal.encode()).hexdigest())
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
    row that startup reconciliation can safely repair and retry. The row's
    recorded bytes are zeroed first, so that such a row is not reported as
    lost files, and a row kept because a turn woke the session mid-sweep
    does not charge the tenant for files the sweep removed.
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
                if self._stop.is_set():
                    # Asked to stop: delete nothing more. A sweep used to go
                    # on through every inactive session after the request,
                    # as the scheduler did through every due entry (audit
                    # F-07). What it did not reach waits for the next sweep,
                    # which startup runs before it serves.
                    break
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
                    self.quota_store.clear_session_bytes(tenant, session)
                    self.data_manager.delete_internal_files(internal)
                    if self.quota_store.remove_registered_session(
                            tenant, session, cutoff_unix=cutoff):
                        deleted_sessions += 1

            # A schedule may exist before its first turn creates a session
            # registry entry, so it has an independent retention path.
            if not self._stop.is_set():
                deleted_schedules += self.schedule_store.remove_inactive(cutoff, now)
        except Exception:
            with self._lock:
                self._checks += 1
                self._failures += 1
                self._deleted_sessions += deleted_sessions
                self._deleted_schedules += deleted_schedules
                self._last_check = now
                self._healthy = False
            _log_event(self.log_sink, {
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
            _log_event(self.log_sink, {
                "event": "retention_cleanup",
                "severity": "info",
                "status": "ok",
                "deleted_sessions": deleted_sessions,
                "deleted_schedules": deleted_schedules,
            })
        return True

    def _sweep_did_not_complete(self):
        """A sweep that raised enforced nothing: fail closed until one completes."""
        with self._lock:
            self._checks += 1
            self._failures += 1
            self._healthy = False
        _log_event(self.log_sink, {
            "event": "retention_cleanup",
            "severity": "critical",
            "status": "failed",
            "deleted_sessions": 0,
            "deleted_schedules": 0,
        })

    def is_healthy(self):
        return self.snapshot()["healthy"]

    def snapshot(self):
        with self._lock:
            return {
                "checks_total": self._checks,
                "failures_total": self._failures,
                # The last verdict stands only while something is still
                # sweeping: a thread that ended on its own enforces nothing.
                "healthy": self._healthy and not _ended_on_its_own(
                    self._thread, self._stop),
                "last_check_unix": self._last_check,
                "deleted_sessions_total": self._deleted_sessions,
                "deleted_schedules_total": self._deleted_schedules,
                "retention_seconds": self.retention_s,
            }

    def start(self):
        running = _running_thread("the retention monitor", self._thread, self._stop)
        if running is not None:
            return running
        self._stop.clear()

        def run():
            while not self._stop.is_set():
                # Nothing a sweep raises ends retention (audit F-05).
                try:
                    self.tick()
                except Exception:
                    self._sweep_did_not_complete()
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


COMPILER_RESOURCES = ("resident_memory_bytes", "thread_count", "open_fd_count")


class CompilerProcesses:
    """Which processes are this host's compilers.

    Guests run inside the compiler, not in this Python process, so the host's
    own rusage, threads and descriptor table say nothing about them (audit
    F-18). ``product_main.build_runtime`` registers every compiler it spawns
    here, and ``pids()`` names the ones still alive, however many a runtime
    holds. Held weakly: a compiler the runtime has let go of is neither kept
    alive nor left unreaped by being counted.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._clients = weakref.WeakSet()

    def track(self, client):
        with self._lock:
            self._clients.add(client)
        return client

    def pids(self):
        with self._lock:
            clients = list(self._clients)
        # is_healthy polls the child, so one that has exited is reaped and
        # dropped here rather than counted. The pid is read from the client's
        # process handle; tests/test_operations_metrics.py pins that against
        # real ProductionSigilMCP clients.
        return [client._proc.pid for client in clients if client.is_healthy]


def _proc_resources(pid, proc_root=Path("/proc")):
    """Linux: one process's resources from /proc, or None once it has exited."""
    process = Path(proc_root) / str(pid)
    try:
        status = (process / "status").read_text()
    except OSError:
        return None
    fields = {}
    for line in status.splitlines():
        name, _, value = line.partition(":")
        fields[name] = value.split()
    try:
        # The kernel writes the Vm* lines only while the process still has
        # an address space, so a zombie, which has exited but is not yet
        # reaped, has no VmRSS and is reported as gone.
        resident = int(fields["VmRSS"][0]) * 1024   # the kernel reports kB
        threads = int(fields["Threads"][0])
    except (KeyError, IndexError, ValueError):
        return None
    try:
        open_fds = sum(1 for _ in (process / "fd").iterdir())
    except OSError:
        open_fds = None
    return {"resident_memory_bytes": resident, "thread_count": threads,
            "open_fd_count": open_fds}


_PROC_PIDLISTFDS = 1            # <sys/proc_info.h>
_PROC_PIDTASKINFO = 4
_PROC_FDINFO_BYTES = 8          # struct proc_fdinfo: int32 fd, uint32 type
_LIBPROC = None


def _load_libproc():
    """macOS exposes another process's resources only through libproc. Loaded
    on first use, so no other platform loads ctypes at all."""
    global _LIBPROC
    if _LIBPROC is None:
        import ctypes

        class TaskInfo(ctypes.Structure):           # struct proc_taskinfo
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "virtual_size", "resident_size", "total_user", "total_system",
                "threads_user", "threads_system")] + [(name, ctypes.c_int32) for name in (
                    "policy", "faults", "pageins", "cow_faults", "messages_sent",
                    "messages_received", "syscalls_mach", "syscalls_unix", "csw",
                    "threadnum", "numrunning", "priority")]

        pidinfo = ctypes.CDLL("/usr/lib/libproc.dylib").proc_pidinfo
        pidinfo.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                            ctypes.c_void_p, ctypes.c_int)
        pidinfo.restype = ctypes.c_int
        _LIBPROC = (ctypes, pidinfo, TaskInfo)
    return _LIBPROC


def _libproc_resources(pid):
    """macOS: one process's resources from the kernel, or None once it has
    exited (a zombie included) or when libproc cannot be loaded."""
    try:
        ctypes, pidinfo, TaskInfo = _load_libproc()
    except (OSError, AttributeError):
        return None
    task = TaskInfo()
    size = ctypes.sizeof(task)
    if pidinfo(pid, _PROC_PIDTASKINFO, 0, ctypes.byref(task), size) != size:
        return None
    # The first call sizes the descriptor table, the second lists it; spare
    # entries absorb descriptors opened in between.
    capacity = max(0, pidinfo(pid, _PROC_PIDLISTFDS, 0, None, 0)) + 32 * _PROC_FDINFO_BYTES
    used = pidinfo(pid, _PROC_PIDLISTFDS, 0, ctypes.create_string_buffer(capacity), capacity)
    return {"resident_memory_bytes": int(task.resident_size),
            "thread_count": int(task.threadnum),
            "open_fd_count": used // _PROC_FDINFO_BYTES if used > 0 else None}


def process_measurer(platform=None):
    """How this platform measures one process, or None where it exposes
    nothing the host can read without a third-party dependency."""
    platform = sys.platform if platform is None else platform
    if platform.startswith("linux"):
        return _proc_resources
    if platform == "darwin":
        return _libproc_resources
    return None


def compiler_resources(pids, measure):
    """Totals over the live compilers, with their count.

    A total is reported only when every compiler could be measured: a sum
    that left one out would understate exactly the growth an operator is
    looking for. Where the platform measures nothing (``measure`` is None),
    the count still holds and every total is unknown.
    """
    pids = list(pids)
    totals = dict.fromkeys(COMPILER_RESOURCES, None if measure is None else 0)
    for pid in pids:
        sample = None if measure is None else measure(pid)
        for key in COMPILER_RESOURCES:
            value = None if sample is None else sample[key]
            totals[key] = (None if value is None or totals[key] is None
                           else totals[key] + value)
    return {"compiler_process_count": len(pids),
            **{f"compiler_{key}": value for key, value in totals.items()}}


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
                 turn_deadline_s=120, resource_root=None,
                 compiler_pids=None, effective_settings=None, scheduler=None,
                 work=None, tenant_secrets=None, max_tokens_per_turn=None):
        self.agent = agent
        self.auth = auth
        self.version = version
        self.rate = FixedWindowRateLimiter(requests_per_minute)
        # An unavailable durable admission store must not prevent a readiness
        # probe from reporting that store as unhealthy. The emergency path is
        # still authenticated/scoped and locally rate-limited at the same bound.
        self._readiness_emergency_rate = FixedWindowRateLimiter(
            quota_store.requests_per_minute if quota_store is not None
            else requests_per_minute)
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
        # The scheduler product startup runs, asked for its own liveness. A
        # scheduler whose thread had ended used to leave readiness green and
        # the metrics silent (audit F-05).
        self.scheduler = scheduler
        self.resource_root = Path(resource_root) if resource_root is not None else None
        # Product startup supplies both. ``compiler_pids`` names the live
        # compiler processes (CompilerProcesses.pids); ``effective_settings``
        # is the configuration in force, reported as given.
        self.compiler_pids = compiler_pids
        if effective_settings is not None:
            effective_settings = dict(effective_settings)
            for key, value in effective_settings.items():
                if not (isinstance(key, str) and SETTING_KEY_RE.fullmatch(key)
                        and (value is None or isinstance(value, (str, int, float)))):
                    raise ConfigError(
                        f"effective setting {key!r} cannot be exported: it needs a "
                        f"lowercase name and a string, number or null value")
        self.effective_settings = effective_settings
        if turn_deadline_s <= 0:
            raise ConfigError("turn_deadline_s must be positive")
        self.turn_deadline_s = turn_deadline_s
        # What a drain waits for. Product startup hands the same object to
        # the scheduler, so that both refuse and count work in one place.
        self.work = work if work is not None else InFlightWork()
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
        # Each tenant's tool credentials (audit F-09). A policy that names a
        # secret its tenant has no value for is refused here, as one that
        # names an unknown tool is.
        self.tenant_secrets = tenant_secrets
        if max_tokens_per_turn is not None:
            if type(max_tokens_per_turn) is not int or max_tokens_per_turn <= 0:
                raise ConfigError("max_tokens_per_turn must be a positive integer")
            if quota_store is None or not quota_store.resource_limits_enabled:
                raise ConfigError("turn token budgets require resource quotas")
        self.max_tokens_per_turn = max_tokens_per_turn
        for principal in auth.principals:
            if principal_tool_grants(principal, tenant_secrets) is None:
                missing = (sorted(principal.secrets) if tenant_secrets is None
                           else tenant_secrets.missing(principal))
                raise ConfigError(
                    f"principal {principal.principal_id!r} of tenant "
                    f"{principal.tenant_id!r} names secrets its tenant has no value "
                    f"for: {missing}")

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

    def _tool_grants(self, principal):
        """The principal's tool grants for its turn (principal_tool_grants).
        Startup refuses a policy whose tenant lacks a secret it names; an
        embedding host that replaced the secrets afterwards gets the same
        answer as for a tool the manifest no longer has."""
        grants = principal_tool_grants(principal, self.tenant_secrets)
        if grants is None:
            raise ProductError(503, "configuration_error", "tool authorization is invalid")
        return grants

    def _turn_usage(self, usage):
        """A turn's token usage: what the agent returned or, for a turn that
        raised, what its telemetry had counted by then."""
        telemetry = (self.agent.turn_telemetry()
                     if hasattr(self.agent, "turn_telemetry") else {})
        if usage is None and (
                "input_tokens" in telemetry or "output_tokens" in telemetry):
            usage = {
                "input_tokens": max(0, int(telemetry.get("input_tokens", 0))),
                "output_tokens": max(0, int(telemetry.get("output_tokens", 0))),
            }
        return usage

    def start_draining(self):
        """Reject new turns and make readiness fail before process shutdown."""
        self.work.start_draining()

    def wait_for_drain(self, timeout_s: float) -> bool:
        if timeout_s < 0:
            raise ValueError("timeout_s must be non-negative")
        return self.work.wait(timeout_s)

    def transport_error(self, error_class, *, request_id=None, status=None):
        """Count a connection the transport answered or lost by itself, by
        `error_class` (ServiceMetrics.transport_error).

        With a `request_id`, a request that dispatch() logged could not be
        given its answer, and a second event says so. It holds nothing from
        the request but the id its first event already holds. A connection
        that ended before a request was read writes no event: there is no
        request to log, and a line per connection would let any client that
        can open one write to the log."""
        self.metrics.transport_error(error_class)
        if request_id is not None:
            _log_event(self.log_sink, {
                "event": "http_response_failed",
                "request_id": request_id,
                "status": status,
                "failure_classification": error_class,
            })

    def _turn_enter(self):
        if not self.work.enter():
            raise ProductError(503, "service_draining", "service is draining")

    def _turn_leave(self):
        self.work.leave()

    @contextmanager
    def _in_flight(self):
        """One piece of a tenant's work, from before it touches state until
        it has finished: refused once draining has begun, and waited for by
        the drain until it leaves. Chat turns were the only work counted, so
        a drain could report complete with a deletion halfway through
        (audit F-26)."""
        self._turn_enter()
        try:
            yield
        finally:
            self._turn_leave()

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
        draining = self.work.draining
        probes = {"runtime": self.readiness}
        for name, component in (
                ("quota_store", self.quota_store),
                ("schedule_store", self.schedule_store),
                ("state_storage", self.data_manager),
                ("audit_verification", self.audit_monitor),
                ("retention", self.retention_monitor),
                ("scheduler", self.scheduler)):
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
        dependency. Those first figures describe THIS process, the host;
        guests run in the compiler processes, whose figures are reported
        separately, as totals with a count, when startup supplies their pids.
        Storage inspection is enabled only when product startup supplies the
        state root.
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
        if self.compiler_pids is not None:
            snapshot.update(compiler_resources(self.compiler_pids(), process_measurer()))
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
        if self.effective_settings is not None:
            metrics["effective_settings"] = dict(self.effective_settings)
        if self.audit_monitor is not None:
            metrics["audit_verification"] = self.audit_monitor.snapshot()
        if self.retention_monitor is not None:
            metrics["retention"] = self.retention_monitor.snapshot()
        # Asked for what it offers, as readiness asks every component.
        scheduler_report = getattr(self.scheduler, "snapshot", None)
        if callable(scheduler_report):
            metrics["scheduler"] = scheduler_report()
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

    def authenticate(self, headers):
        """The principal whose credential a request's headers carry. Raises
        ProductError (401) when they carry no valid one.

        The transport calls this before it reads any of a request's body and
        hands the outcome to dispatch(), which does not authenticate again: a
        client without a credential used to have its declared body, up to 1
        MiB, read or waited for before it was refused (audit F-08).
        """
        return self.auth.authenticate(headers.get("Authorization"))

    def dispatch(self, method: str, path: str, headers, body: bytes = b"", *,
                 authenticated=None):
        # `authenticated` is what authenticate() returned for these headers,
        # or the ProductError it raised, when the transport has already
        # authenticated the request.
        started = time.monotonic()
        request_id = _request_id(headers)
        principal = None
        route = "unknown"
        status = 500
        error_class = None
        extra_headers = {"Cache-Control": "no-store", "X-Request-ID": request_id}
        try:
            if isinstance(authenticated, ProductError):
                raise authenticated
            principal = (self.authenticate(headers) if authenticated is None
                         else authenticated)
            readiness_admission_failed = False
            if self.quota_store is not None:
                try:
                    admitted, retry_after = self.quota_store.admit_request(principal.tenant_id)
                except (OSError, sqlite3.Error):
                    if method != "GET" or path != "/v1/ready":
                        raise
                    self._require(principal, "ops:read")
                    admitted, retry_after = self._readiness_emergency_rate.admit(principal.tenant_id)
                    readiness_admission_failed = True
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
                grants = self._tool_grants(principal)
                deadline = time.monotonic() + self.turn_deadline_s
                turn_capacity = (self.quota_store.acquire_turn(
                                     principal.tenant_id, session_id=internal_session,
                        incremental_tokens=self.max_tokens_per_turn is not None)
                                 if self.quota_store is not None
                                 else self.concurrency.acquire(principal.tenant_id))
                usage = None
                # Whether the agent was asked for this turn. A request whose
                # deadline passes while it waits for its session never is,
                # and the agent's per-thread count of tokens and tool calls
                # is then still that of the last turn this thread served.
                # Read as this request's, it charged the tenant that turn's
                # tokens a second time.
                asked = False
                # Takes this turn out of the drain's count of work in flight,
                # once it has entered it.
                in_flight = ExitStack()
                try:
                    self._turn_enter()
                    in_flight.callback(self._turn_leave)
                    turn_started = time.monotonic()
                    self.metrics.turn_start()
                    turn_error = True
                    queue_wait_ms = 0.0
                    try:
                        queue_started = time.monotonic()
                        with self.session_locks.hold(
                                internal_session, deadline_monotonic=deadline):
                            queue_wait_ms = (time.monotonic() - queue_started) * 1000
                            if self.max_tokens_per_turn is not None:
                                grants["token_budget"] = turn_capacity.token_budget(self.max_tokens_per_turn)
                            asked = True
                            try:
                                reply, usage = self.agent.turn_with_usage(
                                    internal_session, message, allowed_tools=allowed_tools,
                                    deadline_monotonic=deadline, **grants)
                            finally:
                                if (self.quota_store is not None
                                        and self.quota_store.resource_limits_enabled):
                                    # Settle before the session lock is released,
                                    # as a scheduled turn does. Settled after it, a
                                    # DELETE could take the lock in between, and
                                    # this then re-created the row the DELETE had
                                    # removed, with the bytes of files that were
                                    # gone.
                                    footprint = self.data_manager.footprint(internal_session)
                                    usage = self._turn_usage(usage)
                                    turn_capacity.release(
                                        usage=usage, session_id=internal_session,
                                        storage_bytes=footprint["storage_bytes"],
                                        audit_bytes=footprint["audit_bytes"])
                        turn_error = False
                    except Exception as e:
                        # A record that could not be written is the turn's
                        # outcome, whatever failed after it: the reply is
                        # withheld, and the tenant is not told the model
                        # failed (audit F-04).
                        if _audit_write_failure(e):
                            _report_audit_write_failure(self.audit_monitor, "chat")
                            raise ProductError(500, *AUDIT_WRITE_FAILED) from e
                        if isinstance(e, TimeoutError):
                            raise ProductError(
                                504, "turn_deadline_exceeded",
                                "turn deadline exceeded") from e
                        if isinstance(e, RuntimeError):
                            raise ProductError(
                                502, "agent_failure", "agent turn failed") from e
                        raise
                    finally:
                        telemetry = (self.agent.turn_telemetry() if asked
                                     and hasattr(self.agent, "turn_telemetry") else {})
                        queue_wait_ms += max(
                            0.0, float(telemetry.get("forge_queue_wait_ms", 0.0)))
                        extra_headers["Server-Timing"] = (
                            f"queue;dur={queue_wait_ms:.3f}")
                        tool_calls = max(0, int(telemetry.get("tool_calls", 0)))
                        retries = max(0, int(telemetry.get("retries", 0)))
                        if asked:
                            usage = self._turn_usage(usage)
                        extra_headers["X-Sigil-Tool-Calls"] = str(tool_calls)
                        extra_headers["X-Sigil-Retries"] = str(retries)
                        self.metrics.turn_finish(
                            (time.monotonic() - turn_started) * 1000,
                            queue_wait_ms=queue_wait_ms, usage=usage,
                            tool_calls=tool_calls, retries=retries,
                            error=turn_error)
                finally:
                    try:
                        if self.quota_store is not None:
                            # A turn that reached its session has settled, under
                            # the session lock, and this is a no-op. One that did
                            # not frees its reservation here; the session's
                            # recorded bytes are only ever written under that lock.
                            turn_capacity.release(usage=usage)
                        else:
                            turn_capacity.release()
                    finally:
                        # Left only now: the release above writes the quota
                        # store, and the turn used to leave the drain's count
                        # before it, so a drain could complete with that
                        # write half done.
                        in_flight.close()
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
                if readiness_admission_failed:
                    readiness["ready"] = False
                    readiness["dependencies"]["quota_store"] = False
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
                with self._in_flight():
                    schedules = self.schedule_store.for_tenant(principal.tenant_id)
                status, payload = 200, {
                    "schedules": schedules,
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
                with self._in_flight():
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
                with self._in_flight():
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
                with self._in_flight():
                    exported = self.data_manager.export(principal, session)
                status, payload = 200, {
                    "data": exported,
                    "request_id": request_id,
                }
            elif method == "DELETE" and path.startswith("/v1/sessions/"):
                route = "/v1/sessions/{session}"
                self._require(principal, "sessions:delete")
                if self.data_manager is None:
                    raise ProductError(503, "data_lifecycle_unavailable",
                                       "session data service is not configured")
                session = unquote(path[len("/v1/sessions/"):])
                with self._in_flight():
                    deleted = self.data_manager.delete(principal, session)
                status, payload = 200, {
                    "deleted": deleted,
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
        except Exception as e:
            # The product contract never returns exception strings. Operators
            # correlate the request id with the structured failure log.
            status = 500
            # A turn whose audit record could not be written was counted
            # when it failed, and answers as what it is even when the quota
            # store, on the same full disk, failed again in the last
            # `finally` and replaced that answer on the way out.
            error_class, message = (
                AUDIT_WRITE_FAILED if _audit_write_failure(e)
                else ("internal_error", "internal service error"))
            payload = {"error": {"code": error_class, "message": message},
                       "request_id": request_id}
        finally:
            duration_ms = round((time.monotonic() - started) * 1000, 3)
            self.metrics.request(method, route, status, error_class)
            # A request whose log line cannot be written (a full disk or a
            # closed pipe under stdout) is still answered. Its work, its charge
            # and its audit record stand either way and the metrics have
            # counted it; a client denied the answer would only retry work
            # that was done. The sink used to raise from here, which dropped
            # every connection, readiness probes included, with a traceback.
            _log_event(self.log_sink, {
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


_DIGITS_RE = re.compile(r"[0-9]+")
# What the listener says it is. The interpreter and its version, which
# BaseHTTPRequestHandler names, are no client's business (audit F-25).
SERVER_NAME = "sigil-pi"
# BaseHTTPRequestHandler.send_error's statuses, for a request the server could
# not read or route, as product errors. A status not listed answers with its
# class's entry.
TRANSPORT_ERRORS = {
    400: ("malformed_request", "malformed request"),
    414: ("request_line_too_long", "request line too long"),
    431: ("request_head_too_large", "request header fields too large"),
    501: ("method_not_supported", "method not supported"),
    505: ("http_version_not_supported", "HTTP version not supported"),
}
_TRANSPORT_ERROR_BY_CLASS = {4: TRANSPORT_ERRORS[400],
                             5: ("internal_error", "internal service error")}
# serve_forever() asks for a connection when one is waiting. With no slot free
# the server waits this long for one before it returns to serve_forever(),
# which checks for shutdown() between asks.
_CAP_POLL_SECONDS = 0.5
# A request answered before all of it was read is drained before the
# connection closes: until the client pauses this long, ends its side, or has
# sent this many more bytes, or the request deadline passes.
_DISCARD_PAUSE_SECONDS = 0.25
_DISCARD_LIMIT_BYTES = 2 * MAX_PRODUCT_REQUEST_BYTES


def _declared_length(headers):
    """The body length a request's headers declare: 0 without Content-Length,
    None when the declaration is invalid.

    RFC 9112 section 6.3: Content-Length is 1*DIGIT, and an invalid value, or
    values that disagree, is a framing error, answered 400 before the
    connection closes. int() took "+38", "3_8" and the first of two different
    lengths. Whitespace around the value is not part of it, and one value
    repeated is that value. A Transfer-Encoding, which overrides any
    Content-Length (section 6.3), declares a coding this server does not
    read: a chunked body was taken as an empty one (audit F-25).
    """
    if headers.get_all("Transfer-Encoding"):
        return None
    values = {value.strip(" \t") for value in headers.get_all("Content-Length") or ()}
    if not values:
        return 0
    if len(values) != 1:
        return None
    (value,) = values
    if not _DIGITS_RE.fullmatch(value):
        return None
    significant = value.lstrip("0")
    # int() refuses strings of more than 4,300 digits. A length this long is
    # over every limit, so its exact size does not matter.
    return int(significant or "0") if len(significant) <= 18 else 10 ** 18


class _RequestHeadTooLarge(http.client.HTTPException):
    """Header lines past MAX_REQUEST_HEAD_BYTES. BaseHTTPRequestHandler answers
    an HTTPException raised while it reads the headers with 431."""


class _RequestHeadIncomplete(OSError):
    """A request head that ended before the line end or the blank line that
    completes it: the client closed its side, or reset a TLS connection,
    which reads as an end. The standard library processed what it had as a
    whole request. RFC 9112 section 8 calls such a message incomplete, as a
    body short of its Content-Length is, and it is not processed; it is
    counted as a head cut off by a reset is (audit F-25)."""


class _ConnectionInput(io.RawIOBase):
    """A connection's input, read under its request's deadline: each read waits
    at most `timeout_s`, and none past the deadline. The socket timeout alone
    starts again at every read, so a client sending a byte at a time held its
    connection for as long as it went on (audit F-08)."""

    def __init__(self, connection, deadline, timeout_s):
        self.connection = connection
        self.deadline = deadline
        self.timeout_s = timeout_s
        # Whether a read waited out its timeout or the deadline: the standard
        # library ends a request whose head times out without a word.
        self.timed_out = False

    def readable(self):
        return True

    def readinto(self, buffer):
        try:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("request deadline passed")
            self.connection.settimeout(min(self.timeout_s, left))
            return self.connection.recv_into(buffer)
        except TimeoutError:
            self.timed_out = True
            raise


class _RequestReader(io.BufferedReader):
    """A handler's rfile. While the header lines are read, it counts them
    against `head_budget`: the standard library allows 100 lines of 64 KiB,
    and 16 connections sending that much and no credential grew the server by
    7.8 MiB each (audit F-08)."""

    head_budget = None

    def readline(self, size=-1):
        line = super().readline(size)
        if self.head_budget is not None:
            self.head_budget -= len(line)
            if self.head_budget < 0:
                raise _RequestHeadTooLarge("request head too large")
            if not line.endswith(b"\n") and (size < 0 or len(line) < size):
                raise _RequestHeadIncomplete("request head ended early")
        return line


def _readable(sock):
    poller = select.poll()
    poller.register(sock, select.POLLIN)
    return bool(poller.poll(0))


class ProductHTTPServer(ThreadingHTTPServer):
    """A thread per connection, for at most `max_connections` at a time.

    At the cap the server takes no connection until one ends. The kernel
    completes the next ones, which wait unanswered in the listen queue and are
    taken in turn as connections end. Without a cap, clients that never
    authenticated held as many threads as they opened connections (audit
    F-08).
    """

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = LISTEN_BACKLOG

    def __init__(self, server_address, handler, *, max_connections=MAX_CONNECTIONS):
        if (isinstance(max_connections, bool) or not isinstance(max_connections, int)
                or max_connections < 1):
            raise ConfigError("max_connections must be a positive integer")
        self._slots = threading.BoundedSemaphore(max_connections)
        self._answers = 0
        self._answers_changed = threading.Condition()
        # Set by drain_service(): until when server_close() waits for the
        # answers of requests already dispatched.
        self.drain_deadline = None
        super().__init__(server_address, handler)

    @contextmanager
    def answering(self):
        """One dispatched request, from before dispatch() until its answer
        has been written or has failed."""
        with self._answers_changed:
            self._answers += 1
        try:
            yield
        finally:
            with self._answers_changed:
                self._answers -= 1
                self._answers_changed.notify_all()

    def server_close(self):
        """Close the listening socket. After a drain, also wait until the
        drain's deadline for the answers still being written.

        A turn leaves the drain's count of work before its log line and its
        answer are written, so a drain could complete, and the process exit,
        with the answer unwritten: handler threads are daemons. The answers
        are waited for, not vouched for. One still unwritten at the deadline
        (a client reading slowly) leaves the state as drained as it is, and
        the drain's result is the work's."""
        super().server_close()
        if self.drain_deadline is None:
            return
        with self._answers_changed:
            while self._answers:
                remaining = self.drain_deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._answers_changed.wait(remaining)

    def get_request(self):
        if not self._slots.acquire(blocking=False):
            if not self._slots.acquire(timeout=_CAP_POLL_SECONDS):
                raise OSError("at the connection cap")
            # The connection serve_forever() saw may have gone during the
            # wait, and accept() would then hold the serving thread, and
            # shutdown(), until another came.
            if not _readable(self.socket):
                self._slots.release()
                raise OSError("no connection is waiting")
        try:
            return super().get_request()
        except BaseException:
            self._slots.release()
            raise

    def shutdown_request(self, request):
        # Each connection get_request() returned ends here once, however it
        # ended.
        try:
            super().shutdown_request(request)
        finally:
            self._slots.release()


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
                  ssl_context=None, socket_timeout_s=30,
                  request_deadline_s=REQUEST_DEADLINE_SECONDS,
                  max_connections=MAX_CONNECTIONS):
    """Serve the v1 contract.

    Plaintext is accepted only on loopback, where a local TLS-terminating proxy
    can connect. Binding any external/wildcard address without a server TLS
    context is a startup error, not an operator warning.

    At most `max_connections` connections are handled at a time. Each read of
    a request waits at most `socket_timeout_s`, and the whole request, its
    TLS handshake, head and body, must arrive within `request_deadline_s` of
    the server taking its connection. A request is authenticated from its
    headers before any of its body is read (audit F-08).
    """
    if socket_timeout_s <= 0:
        raise ConfigError("socket_timeout_s must be positive")
    if request_deadline_s <= 0:
        raise ConfigError("request_deadline_s must be positive")
    validate_transport(host, ssl_context)

    class Handler(BaseHTTPRequestHandler):
        # A request line without a version is read as HTTP/1.0, not as
        # HTTP/0.9, whose answer is a body alone: no status line and none of
        # the contract's headers. The standard library's own answers, before
        # it has read a request's version, were written that way too.
        default_request_version = "HTTP/1.0"

        def version_string(self):
            return SERVER_NAME

        def setup(self):
            deadline = time.monotonic() + request_deadline_s
            super().setup()
            # The handshake and the request are read under the deadline as well
            # as the socket timeout; answers are written under the socket
            # timeout alone (send_response).
            self.connection.settimeout(min(socket_timeout_s, request_deadline_s))
            self.rfile.close()
            self.rfile = _RequestReader(
                _ConnectionInput(self.connection, deadline, socket_timeout_s))
            self._request_read = False
            self._answered = False

        def handle(self):
            # The TLS handshake runs here, on this connection's own thread and
            # under the timeout set in setup(), which CPython applies to the
            # whole handshake. In accept(), on the serving thread, a client that
            # never spoke stalled every other client (audit F-01). A handshake
            # that fails or times out gets no response and no traceback: the
            # connection is closed, and counted by how it ended.
            if ssl_context is not None:
                try:
                    self.connection.do_handshake()
                except TimeoutError:
                    service.transport_error("request_timeout")
                    return
                except OSError:  # ssl.SSLError, ConnectionError
                    service.transport_error("tls_handshake_failed")
                    return
            try:
                super().handle()
            except OSError:
                # Reading the request failed: a reset, or TLS records that
                # could not be read. socketserver printed it as a traceback.
                # (An answer that cannot be written is _write's.)
                service.transport_error("request_aborted")
                return
            if not self._answered:
                if self.rfile.raw.timed_out:
                    # The head did not arrive within the socket timeout or
                    # the deadline; the standard library closes without a
                    # word.
                    service.transport_error("request_timeout")
            elif not self._request_read:
                self._discard_unread()

        def parse_request(self):
            if not self.raw_requestline.endswith(b"\n"):
                # The line ended with the input; a longer one got its 414.
                raise _RequestHeadIncomplete("request line ended early")
            words = str(self.raw_requestline, "iso-8859-1").split()
            if len(words) == 2 and words[0] == "GET":
                # Read as HTTP/1.0, headers included, before the standard
                # library chooses: Python 3.14 reads a line without a version
                # as HTTP/0.9 and leaves its headers unread, where 3.12 reads
                # them. Other methods keep the standard library's 400.
                self.raw_requestline = (
                    self.raw_requestline.rstrip(b"\r\n") + b" HTTP/1.0\r\n")
            self.rfile.head_budget = MAX_REQUEST_HEAD_BYTES
            try:
                return super().parse_request()
            finally:
                self.rfile.head_budget = None

        def send_response(self, code, message=None):
            # The request deadline bounds receiving a request, not answering
            # it: a turn may outlast it, and its answer is then still written.
            self.connection.settimeout(socket_timeout_s)
            self._answered = True
            super().send_response(code, message)

        def do_GET(self):
            self._request_read = True
            self._dispatch(b"")

        def do_POST(self):
            length = _declared_length(self.headers)
            if length is None or length > MAX_PRODUCT_REQUEST_BYTES:
                self._refuse_length()
                return
            # Authenticated from the headers alone, before any of the body is
            # read or waited for (audit F-08).
            try:
                principal = service.authenticate(self.headers)
            except ProductError as refusal:
                self._dispatch(b"", authenticated=refusal)
                return
            try:
                body = self.rfile.read(length)
            except OSError:  # the socket timeout, the deadline, a reset
                body = None
            if body is None or len(body) != length:
                # RFC 9112 section 8: a body that ends or stops before its
                # declared length is incomplete, and is not processed.
                self._refuse_length()
                return
            self._request_read = True
            self._dispatch(body, authenticated=principal)

        def do_DELETE(self):
            self._request_read = True
            self._dispatch(b"")

        def _dispatch(self, body, authenticated=None):
            with self.server.answering():
                status, headers, payload = service.dispatch(
                    self.command, self.path, self.headers, body,
                    authenticated=authenticated)
                self._write(status, headers, payload)

        def _refuse_length(self):
            # Answered, counted and logged as dispatch() answers any error.
            # Written here, the refusal had no request log line, no request
            # metric, no X-Request-ID and no Cache-Control.
            self._dispatch(b"", authenticated=ProductError(
                400, "invalid_content_length", "invalid or oversized Content-Length"))

        def send_error(self, code, message=None, explain=None):
            # The standard library's answer to a request it cannot read or
            # route (400, 414, 431, 501, 505) was an HTML page, with a reason
            # phrase that could repeat the request: "501 Unsupported method
            # ('PUT')". Here it is a product error, with a request id and
            # nothing from the request but a valid X-Request-ID. It is counted
            # by class, not logged or counted as a request: the request log
            # and the request metrics hold the requests the service routed.
            error_code, error_message = TRANSPORT_ERRORS.get(
                code, _TRANSPORT_ERROR_BY_CLASS[5 if code >= 500 else 4])
            service.transport_error(error_code)
            request_id = _request_id(getattr(self, "headers", None) or {})
            self._write(code, {"Cache-Control": "no-store", "X-Request-ID": request_id},
                        {"error": {"code": error_code, "message": error_message},
                         "request_id": request_id}, logged=False)

        def _discard_unread(self):
            # Answered before all of the request was read: refused from its
            # headers, or malformed. Closing a connection with input unread
            # resets it, and a client still sending its body would lose the
            # answer to the reset. So the answer and the end of the stream go
            # first, and what is still arriving is then read and dropped,
            # never kept.
            self.rfile.raw.timeout_s = _DISCARD_PAUSE_SECONDS
            left = _DISCARD_LIMIT_BYTES
            try:
                self.connection.shutdown(socket.SHUT_WR)
                while left > 0:
                    dropped = self.rfile.read1(min(left, 65536))
                    if not dropped:
                        return
                    left -= len(dropped)
            except OSError:  # the pause, the deadline, a reset
                return

        def _write(self, status, headers, payload, *, logged=True):
            # `logged`: dispatch() wrote this request's log line.
            if isinstance(payload, PrometheusPayload):
                raw = payload.body.encode()
                content_type = PROMETHEUS_CONTENT_TYPE
            else:
                raw = json.dumps(
                    payload, separators=(",", ":"), ensure_ascii=False).encode()
                content_type = "application/json"
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(raw)
            except OSError:
                # The client closed, reset or stopped reading before its
                # answer was written, plain or TLS. socketserver printed a
                # traceback; here it is counted, and a logged request gets a
                # second, content-free event.
                self.close_connection = True
                service.transport_error(
                    "client_disconnected",
                    request_id=headers["X-Request-ID"] if logged else None,
                    status=status)

        def log_message(self, *args):
            pass

    server = ProductHTTPServer((host, port), Handler, max_connections=max_connections)
    if ssl_context is not None:
        # accept() returns once the TCP connection exists; Handler.handle()
        # performs the handshake.
        server.socket = ssl_context.wrap_socket(
            server.socket, server_side=True, do_handshake_on_connect=False)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def drain_service(service, listener, loops, timeout_s):
    """Stop taking work, then wait for the work in flight, on one budget.

    ``timeout_s`` is the whole drain's. Shutdown used to wait for the
    scheduler, each monitor and the requests one after the other and gave
    each the full budget, up to four times the documented one (audit F-26).
    Here the service refuses new work first, which is also what stops the
    scheduler starting turns. Every loop is then asked to stop, so that none
    begins another round while the drain waits for a different one. Then the
    listener, the loops and the work in flight are all waited for at once,
    each with the whole budget, and the drain stops waiting at its one
    deadline whatever is still running. Returns whether everything finished
    by then, which is what the clean-shutdown marker vouches for. Closing
    the listener then waits, until the same deadline, for the answers of
    requests already dispatched (ProductHTTPServer.server_close).
    """
    deadline = time.monotonic() + timeout_s
    service.start_draining()
    for loop in loops:
        loop.stop(0)

    def listener_stopped():
        listener.shutdown()
        return True

    waits = [listener_stopped,
             *[lambda loop=loop: loop.stop(timeout_s) for loop in loops],
             lambda: service.wait_for_drain(timeout_s)]
    finished = [False] * len(waits)

    def wait(index):
        finished[index] = bool(waits[index]())

    waiting = [threading.Thread(target=wait, args=(index,), daemon=True)
               for index in range(len(waits))]
    for thread in waiting:
        thread.start()
    for thread in waiting:
        thread.join(max(0.0, deadline - time.monotonic()))
    listener.drain_deadline = deadline
    listener.server_close()
    return all(finished)
