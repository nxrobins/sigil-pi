#!/usr/bin/env python3
"""Validated backup and restore for sigil-pi product state.

An offline backup needs the service cleanly drained. An online backup
(--online) is taken while it serves: each session is copied under the lock
the service holds for that session's turns. Every archive's manifest carries
a tag derived from the audit key, which restore and the managed cycle check
before they act on it; archives are authenticated, not encrypted. Restore is
fail-closed and never overwrites an existing state directory.
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
import threading
import time
import zlib
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath

import toolchain

# The bundle ships docs/support-matrix.md, which lists Python <3.12 and >3.14 as
# unsupported and requires that "unsupported selections must fail startup where
# the process can detect them". Only the floor was checked here, after importing
# agent, so this tool started on Python 3.15.1 (audit F-24). Both bounds now come
# from the one definition in toolchain.py, checked at import before any of the
# project's other modules is loaded.
if not (toolchain.MINIMUM_PYTHON <= sys.version_info[:2]
        < toolchain.FIRST_UNSUPPORTED_PYTHON):
    raise SystemExit(toolchain.unsupported_python_message(sys.version_info))

from agent import (
    LEGACY_SANDBOX_NAME,
    MIGRATE_COMMAND,
    STATE_MIGRATION_JOURNAL,
    UNMIGRATED_STATE,
    ensure_directory,
    legacy_sandbox_directories,
    scratch_files,
    sync_directory,
    unmigrated_state,
    verify_audit_dir,
    write_durably,
)
from product_service import (
    SCHEDULE_STORE_MARKER,
    SCHEDULE_STORE_SCHEMA,
    SCHEDULE_TABLES,
    UNMIGRATED_SCHEDULES,
    iter_schedule_rows,
    read_schedule_rows,
    schedule_row_problem,
    schedule_rows,
    stray_schedule_messages,
    unmigrated_schedule_store,
    write_schedule_row,
)


SCHEMA_VERSION = 1
CLEAN_MARKER = ".product-clean-shutdown.json"
MANIFEST_NAME = "manifest.json"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024 * 1024
MANAGED_BACKUP_RE = re.compile(
    r"^sigil-pi-state-([0-9]{10})-([0-9a-f]{64})\.tar\.gz$")
BACKUP_LOCK_NAME = ".sigil-pi-backup-retention.lock"
_CHUNK_BYTES = 256 * 1024
# How long an online backup waits for one lock of the serving product. A turn
# holds its session's lock for at most the turn deadline, which defaults to
# 120 seconds (PI_TURN_DEADLINE_SECONDS); keep the wait above it.
DEFAULT_LOCK_WAIT_SECONDS = 150
_SESSION_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
# The private copy an online managed cycle works from: tempfile's name for a
# directory beside the cycle's staging archive.
_ONLINE_COPY_RE = re.compile(
    r"\.\.sigil-pi-state-[0-9]+-[0-9a-f]{16}\.tar\.gz\.[a-z0-9_]{8}\.online")
# The manifest's authentication tag (audit 2026-11-10, F-12; decision D5 (a)):
# a pax header on the manifest member, "v1:" and the hex HMAC-SHA256 of the
# manifest's exact bytes under a key derived from the audit key with the label.
MANIFEST_TAG_HEADER = "SIGILPI.manifest_tag"
MANIFEST_TAG_SCHEME = "v1"
MANIFEST_KEY_LABEL = b"sigil-pi backup manifest v1"
SKIP_AUTHENTICATION_FLAG = "--skip-manifest-authentication"
_RESTORE_REFUSALS = {
    "untagged": (
        "backup manifest carries no authentication tag: a release before manifest "
        "authentication wrote it, or the tag was removed; restore it with "
        f"{SKIP_AUTHENTICATION_FLAG} only if you know where it came from"),
    "failed": (
        "backup manifest failed authentication: the backup was changed after it was "
        "written, or written under another audit key"),
    "unknown scheme": (
        "backup manifest authentication tag uses a scheme this release does not know"),
}
_MANAGED_REFUSALS = {
    "untagged": (
        "managed backup manifest carries no authentication tag: {name}; move it out of "
        "the managed backup directory, and restore it only with "
        f"{SKIP_AUTHENTICATION_FLAG}"),
    "failed": "managed backup manifest failed authentication: {name}",
    "unknown scheme": (
        "managed backup manifest authentication tag uses a scheme this release does not "
        "know: {name}"),
}


class StateToolError(RuntimeError):
    pass


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value):
    if not isinstance(value, str):
        raise StateToolError(f"unsafe backup path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise StateToolError(f"unsafe backup path: {value!r}")
    return path


def _regular_files(state_dir):
    roots = ("sessions", "sandboxes", "audit")
    files = []
    for root_name in roots:
        root = state_dir / root_name
        if root.is_symlink():
            raise StateToolError(f"state surface is not a real directory: {root_name}")
        if not root.exists():
            continue
        if not root.is_dir():
            raise StateToolError(f"state surface is not a real directory: {root_name}")
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise StateToolError(f"state backup refuses symlink: {path.relative_to(state_dir)}")
            if path.is_file():
                if root_name == "sessions" and path.suffix != ".kv":
                    continue  # stale atomic-write scratch is not committed state
                if root_name == "audit" and path.suffix != ".jsonl":
                    continue  # only complete chain files are part of the contract
                files.append(path)
            elif not path.is_dir():
                raise StateToolError(f"state backup refuses special file: {path.relative_to(state_dir)}")
    schedule = state_dir / "product-schedules.json"
    if schedule.is_symlink():
        raise StateToolError("product-schedules.json must be a regular file")
    if schedule.exists():
        if not schedule.is_file():
            raise StateToolError("product-schedules.json must be a regular file")
        files.append(schedule)
    quota = state_dir / "product-quotas.sqlite3"
    if quota.is_symlink():
        raise StateToolError("product-quotas.sqlite3 must be a regular file")
    if quota.exists():
        if not quota.is_file():
            raise StateToolError("product-quotas.sqlite3 must be a regular file")
        files.append(quota)
    return sorted(files)


def _validate_schedule_rows(db, tables, *, schedule_format, legacy_schedules):
    """The schedule store's rows (schema 2, audit F-27): each one a release
    could have written, each with its message, no message without its
    schedule, and none unless product-schedules.json holds the marker, which
    is what makes a release before schema 2 refuse the state. A migration's
    input may hold rows beside the schema-1 document: a run that stopped
    after its commit, which the migration checks row by row."""
    if not {"schedules", "schedule_messages"} <= tables:
        raise StateToolError("schedule state is incomplete: a schedule table is missing")
    if stray_schedule_messages(db):
        raise StateToolError("schedule state is invalid: a message has no schedule")
    rows = 0
    for row in iter_schedule_rows(db):  # one at a time: a backup holds no store in memory
        problem = schedule_row_problem(row)
        if problem:
            raise StateToolError(f"schedule state is invalid: {problem}")
        rows += 1
    if rows and schedule_format != SCHEDULE_STORE_SCHEMA and not (
            legacy_schedules and schedule_format == 1):
        raise StateToolError(
            "schedule state is incomplete: the ledger holds schedules that "
            "product-schedules.json does not mark as schema 2")


def _validate_quota_database(path, state_dir, *, legacy_sandboxes=False,
                             schedule_format=None, legacy_schedules=False):
    if not path.exists():
        has_customer_state = any((
            (state_dir / "sessions").exists(),
            (state_dir / "sandboxes").exists(),
            (state_dir / "audit").exists(),
            (state_dir / "product-schedules.json").exists(),
        ))
        if has_customer_state:
            raise StateToolError("customer state has no durable quota registry")
        return
    required = {
        "request_windows", "turn_leases", "token_windows", "session_usage"}
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
            integrity = db.execute("PRAGMA quick_check").fetchone()
            if integrity != ("ok",):
                raise StateToolError("quota database failed integrity validation")
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not required <= tables:
                raise StateToolError("quota database schema is incomplete")
            if db.execute("SELECT COUNT(*) FROM turn_leases").fetchone()[0]:
                raise StateToolError(
                    "quota database contains active turn leases; clean drain is incomplete")
            invalid = db.execute(
                "SELECT COUNT(*) FROM token_windows WHERE tokens < 0").fetchone()[0]
            invalid += db.execute(
                "SELECT COUNT(*) FROM session_usage WHERE storage_bytes < 0 "
                "OR audit_bytes < 0 OR last_activity_unix < 0 "
                "OR last_activity_unix IS NULL "
                "OR length(tenant) != 64 OR length(session) != 64 "
                "OR tenant GLOB '*[^0-9a-f]*' OR session GLOB '*[^0-9a-f]*' "
                "OR internal_session IS NULL"
            ).fetchone()[0]
            if invalid:
                raise StateToolError("quota database contains invalid usage records")
            internals = []
            for tenant, session, internal in db.execute(
                    "SELECT tenant, session, internal_session FROM session_usage"):
                parts = internal.split(":")
                if (len(parts) != 3 or parts[0] != "v1" or parts[1] != tenant
                        or len(parts[2]) != 64
                        or any(character not in "0123456789abcdef" for character in parts[2])
                        or hashlib.sha256(internal.encode()).hexdigest() != session):
                    raise StateToolError("quota database contains invalid session registry")
                internals.append(internal)
            expected = {
                hashlib.sha256(internal.encode()).hexdigest() for internal in internals}
            actual_sessions = {
                item.stem for item in (state_dir / "sessions").glob("*.kv")}
            actual_audit = {
                item.stem for item in (state_dir / "audit").glob("*.jsonl")}
            sandboxes = state_dir / "sandboxes"
            actual_sandboxes = ({item.name for item in sandboxes.iterdir() if item.is_dir()}
                                if sandboxes.exists() else set())
            # Every surface is named by a registered session's full digest
            # (state layout 2). A sandbox of layout 1 is named by the first
            # 16 hex characters of it: refused, with the command that moves
            # it, except where layout 1 is an input, a restore and the
            # migration itself (audit F-23).
            legacy = {name for name in actual_sandboxes if LEGACY_SANDBOX_NAME.fullmatch(name)}
            if legacy and not legacy_sandboxes:
                raise StateToolError(UNMIGRATED_STATE)
            prefixes = {digest[:16] for digest in expected} if legacy else set()
            if (actual_sessions - expected or actual_audit - expected
                    or actual_sandboxes - legacy - expected or legacy - prefixes):
                raise StateToolError("customer state is missing quota registry entries")
            if {"schedules", "schedule_messages"} & tables:
                _validate_schedule_rows(db, tables, schedule_format=schedule_format,
                                        legacy_schedules=legacy_schedules)
    except sqlite3.DatabaseError as error:
        raise StateToolError("quota database is corrupt") from error


# ── durable, streamed archives (audit 2026-11-10, F-12) ──────────────────
#
# Before, `backup` read every state file into memory before writing the
# archive (up to the 10 GiB default limit), `restore` read each member whole,
# and nothing here was synced except the managed cycle's deletions: it
# published a new archive, deleted the expired ones and synced the directory,
# so a power loss could leave the old backups gone and the new one
# incomplete. Now an archive is written in two streamed passes over the state,
# because a manifest-first archive needs every digest before its first member,
# and the second pass is checked against the manifest. An archive or a
# restored tree is synced before its name is published, and the name after;
# a cycle deletes only once its new archive is durable, and a sync that fails
# is an error that stops it. The archive's bytes are those the buffered
# writer produced. tests/test_backup_durability.py pins each of these.
#
# The syncs are plain fsync(2), as in agent.py: stable storage on Linux, the
# supported production platform; on macOS not the drive's own cache.


def _snapshot_quota_database(path, snapshot):
    """Capture committed SQLite state, including any valid WAL contents."""
    with closing(sqlite3.connect(
            f"file:{path}?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(snapshot)) as destination:
            source.backup(destination)


def _streamed_digest(path, budget):
    """SHA-256 and size of `path`, read in chunks. A file that takes the state
    past `budget` bytes is the size-limit error, raised without reading on."""
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK_BYTES), b""):
            size += len(chunk)
            if size > budget:
                raise StateToolError("state exceeds the configured backup size limit")
            digest.update(chunk)
    return digest.hexdigest(), size


class _ManifestCheckedReader:
    """What tarfile copies a data member from: the state file read a second
    time, hashed as it goes, so the archive never holds bytes other than the
    ones its manifest, written first, describes."""

    def __init__(self, stream, entry):
        self._stream = stream
        self._entry = entry
        self._digest = hashlib.sha256()

    def _changed(self):
        return StateToolError(
            f"state changed while the backup was written: {self._entry['path']}")

    def read(self, size):
        data = self._stream.read(size)
        if len(data) < size:  # tarfile asks only for bytes the manifest declared
            raise self._changed()
        self._digest.update(data)
        return data

    def check_complete(self):
        if self._stream.read(1) or self._digest.hexdigest() != self._entry["sha256"]:
            raise self._changed()


def _add_member(archive, name, size, stream, *, pax_headers=None):
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o600
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mtime = 0
    if pax_headers:
        info.pax_headers = dict(pax_headers)
    archive.addfile(info, stream)


# ── authenticated manifests (audit 2026-11-10, F-12; decision D5 (a)) ────
#
# An archive's integrity was a list of digests kept inside the archive it
# describes: someone who could write to backup storage could change a file
# and its manifest entry together, and restore accepted the result. Only the
# audit chains, signed with the audit key, were checked. Schedules are
# executable state, so a planted entry ran as the principal it named.
#
# Now the manifest carries a tag, HMAC-SHA256 over its exact bytes under a key
# derived from the audit key with MANIFEST_KEY_LABEL: a key of its own,
# versioned by the label, and never the audit key itself. The tag is a pax
# header on the manifest member, so the archive holds the members every
# release wrote, and a release that does not know the header ignores it:
# releases before this one restore a tagged archive without checking it.
# Restore checks the tag before it parses the manifest; the managed cycle
# checks every archive's before it deletes anything. A manifest without a tag
# (every archive an older release wrote) is restored only under
# --skip-manifest-authentication, which skips that and nothing else; a tag
# that is there is always checked. The archive is not encrypted:
# confidentiality at rest stays with the storage it is written to.
# tests/test_backup_authentication.py pins each of these.


def _manifest_tag(audit_key, manifest_bytes):
    """The tag of a manifest whose exact bytes are `manifest_bytes`."""
    key = hmac.new(audit_key, MANIFEST_KEY_LABEL, hashlib.sha256).digest()
    digest = hmac.new(key, manifest_bytes, hashlib.sha256).hexdigest()
    return f"{MANIFEST_TAG_SCHEME}:{digest}"


def _manifest_authentication(member, manifest_bytes, audit_key):
    """How the archive's manifest `member`, whose bytes are `manifest_bytes`,
    stands under `audit_key`: "verified"; "untagged", with no tag at all;
    "unknown scheme", tagged in a scheme this release does not know; or
    "failed", a tag that does not match these bytes under this key."""
    tag = member.pax_headers.get(MANIFEST_TAG_HEADER)
    if tag is None:
        return "untagged"
    scheme, separator, _ = tag.partition(":")
    if separator and scheme != MANIFEST_TAG_SCHEME:
        return "unknown scheme"
    expected = _manifest_tag(audit_key, manifest_bytes).encode()
    if hmac.compare_digest(tag.encode("utf-8", "surrogateescape"), expected):
        return "verified"
    return "failed"


def _sync_file(path):
    """Sync the bytes of a file that already has its name."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private(path, chunks, *, sync=True):
    """Create `path` 0600 from `chunks`, synced before it is closed. A file
    that is itself scratch (an online backup's private copy) is not synced."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with open(descriptor, "wb") as stream:
        os.fchmod(descriptor, 0o600)
        for chunk in chunks:
            stream.write(chunk)
        stream.flush()
        if sync:
            os.fsync(descriptor)


def _hashed_chunks(stream, digest):
    for chunk in iter(lambda: stream.read(_CHUNK_BYTES), b""):
        digest.update(chunk)
        yield chunk


def _validate_state(state_dir, audit_key, *, drained=True, legacy_sandboxes=False,
                    legacy_schedules=False, schedule_document=None):
    """Validate a state tree before it is archived or installed. A drained
    state carries the clean-shutdown marker; `drained=False` is for an online
    backup's private copy, which the session locks made consistent and no
    marker vouches for. Everything else is checked the same.
    `legacy_sandboxes` accepts sandboxes of state layout 1 (audit F-23), and
    `legacy_schedules` a schedule store of schema 1 (F-27), for a restore and
    a migration; everything else refuses them, naming the migration. A dict
    passed as `schedule_document` receives a schema-1 document and its file's
    mtime, for the migration, which then reads the file no second time."""
    marker_doc = {}
    if drained:
        marker = state_dir / CLEAN_MARKER
        if marker.is_symlink() or not marker.is_file():
            raise StateToolError(
                "state has no clean-shutdown marker; drain the product service first")
        try:
            marker_doc = json.loads(marker.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise StateToolError("clean-shutdown marker is invalid") from error
        if marker_doc.get("schema_version") != SCHEMA_VERSION:
            raise StateToolError("clean-shutdown marker schema is unsupported")

    for path in sorted((state_dir / "sessions").glob("*.kv")) if (state_dir / "sessions").exists() else ():
        try:
            value = json.loads(path.read_bytes() or b"[]")
        except (OSError, json.JSONDecodeError) as error:
            raise StateToolError(f"conversation state is corrupt: {path.name}") from error
        if not isinstance(value, list):
            raise StateToolError(f"conversation state is not a message list: {path.name}")

    # The schedule store's marker (schema 2), or a schema-1 document: a JSON
    # object, its schedules, which only a restore and a migration accept.
    schedule = state_dir / "product-schedules.json"
    schedule_format = None
    if schedule.exists():
        try:
            content = schedule.read_bytes()
            value = None if content == SCHEDULE_STORE_MARKER else json.loads(content or b"{}")
        except (OSError, ValueError, RecursionError) as error:
            raise StateToolError("schedule state is corrupt") from error
        if value is None:
            schedule_format = SCHEDULE_STORE_SCHEMA
        elif not isinstance(value, dict):
            raise StateToolError("schedule state is not an object")
        elif not legacy_schedules:
            raise StateToolError(UNMIGRATED_SCHEDULES)
        else:
            schedule_format = 1
            if schedule_document is not None:
                schedule_document.update(document=value, mtime=schedule.stat().st_mtime)

    _validate_quota_database(state_dir / "product-quotas.sqlite3", state_dir,
                             legacy_sandboxes=legacy_sandboxes,
                             schedule_format=schedule_format,
                             legacy_schedules=legacy_schedules)

    report = verify_audit_dir(state_dir / "audit", key=audit_key)
    if not report["ok"]:
        raise StateToolError(f"audit verification failed for {len(report['problems'])} chain(s)")
    return marker_doc, report


# ── online backups (audit 2026-11-10, F-03) ──────────────────────────────
#
# A backup used to need the service stopped: `backup` refused a state without
# the clean-shutdown marker. With backups no more than 15 minutes apart, on
# one worker, 99.9% availability leaves 0.9 seconds for each stop. An online
# backup needs none. The product serializes every operation on a session (a
# turn, a scheduled turn, an export, a deletion) behind an advisory lock on
# locks/<digest>.lock, where <digest> is the sha256 of the session's internal
# id and also names its files, and it writes the schedule store's marker,
# product-schedules.json, behind a lock on product-schedules.json.lock. This
# tool takes the same locks, one at a time, and under each copies what the
# lock guards into a private directory beside the output. The quota ledger is
# captured last, through SQLite's backup API, and made to agree with what was
# copied; it holds the schedules too (schedule store schema 2). The copy is
# then validated and archived exactly as a drained state is, so the result is
# a schema-1 archive that every release restores.
#
# What an online archive holds:
# - each session as it was at one turn boundary, at some moment between the
#   start of the backup and the ledger's capture: not one instant across
#   sessions. `created_unix` is the start; no session in the archive is older
#   than that, so backup age counts from there.
# - every session that was registered, or had files, when the backup
#   started, unless the ledger no longer listed it when it was captured: a
#   session deleted meanwhile is left out whole, files and row. A session
#   created meanwhile is left out too.
# - each session's registry row with the bytes it recorded while the
#   session's lock was held. Under that lock the row and the files agree, so
#   startup on the restored tree has nothing to report; a row that already
#   recorded bytes for files that were gone is archived as it was, and is
#   reported after a restore as it would be on the live state.
# - no turn leases: they belong to turns in flight on the live service.
# - the schedules as the ledger's capture holds them, after the sessions, in
#   the one snapshot that holds the registry. A schedule may name a session
#   the archive does not hold, which is a state the service passes through
#   itself (a schedule exists before its first run). A schedule captured
#   while its run was in flight carries that run's lease; a restored service
#   leaves it alone until the lease expires, then runs it.
#
# A lock that cannot be taken within the wait fails the backup: a session is
# never skipped. State the registry cannot attribute fails it too, as it
# fails an offline backup. tests/test_online_backup.py pins each of these.


@contextmanager
def _product_lock(path, wait_s):
    """Hold the advisory lock the serving product takes on `path`, waiting at
    most `wait_s` seconds for it. Taken as the product takes it, an exclusive
    flock on the file, created if absent, so the two exclude each other
    across processes.

    A busy lock is waited for in the kernel's queue, not by polling. The
    product hands a busy session's lock from one queued turn to the next
    within microseconds, and a poll finds it free only by landing in that
    gap. flock has no timeout, so the wait runs on a thread of its own, which
    gives the lock straight back if the caller has stopped waiting for it."""
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o666)
    decided, granted = threading.Lock(), threading.Event()
    queued = {"waiting": False, "abandoned": False}

    def queue():
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with decided:
            queued["waiting"] = False
            if queued["abandoned"]:
                os.close(descriptor)  # which releases the lock
            else:
                granted.set()

    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            with decided:  # so the thread cannot be granted the lock before it is expected
                threading.Thread(target=queue, daemon=True).start()
                queued["waiting"] = True
            granted.wait(min(wait_s, threading.TIMEOUT_MAX))
            with decided:
                if queued["waiting"]:
                    raise StateToolError(
                        f"could not lock {path.name} within {wait_s:g} seconds; "
                        f"the backup was not taken") from None
        yield
    finally:
        with decided:
            if queued["waiting"]:
                queued["abandoned"] = True  # the queued thread closes it in its turn
            else:
                os.close(descriptor)  # which releases the lock


def _within_budget(stream, budget):
    """The chunks of `stream`, ending with the size-limit error at the first
    one that takes the copy past `budget` bytes."""
    for chunk in iter(lambda: stream.read(_CHUNK_BYTES), b""):
        budget -= len(chunk)
        if budget < 0:
            raise StateToolError("state exceeds the configured backup size limit")
        yield chunk


def _stage(state_dir, staging, relative, budget, made):
    """Copy what is at `relative` in a serving state to the same place under
    `staging`: one regular file, or every regular file below a directory.
    Returns the bytes copied, 0 when nothing is there. A link or a special
    file is refused, as an offline backup refuses it, and the copy stops at
    `budget` bytes. The caller holds the lock that guards these files.

    `made` is the set of directories this backup has made under `staging`,
    which is its own. A name that is taken there without its having made it
    is another state file's, on a filesystem that keeps the two apart less
    strictly than the state's does (one that ignores case, say). The copy
    would then hold one file where the state holds two, so it stops."""
    try:
        (state_dir / relative).lstat()
    except FileNotFoundError:
        return 0
    copied = 0
    pending = [relative]
    while pending:
        current = pending.pop()
        source = state_dir / current
        mode = source.lstat().st_mode
        if stat.S_ISDIR(mode):
            pending.extend(f"{current}/{name}" for name in os.listdir(source))
        elif stat.S_ISLNK(mode):
            raise StateToolError(f"state backup refuses symlink: {current}")
        elif not stat.S_ISREG(mode):
            raise StateToolError(f"state backup refuses special file: {current}")
        else:
            try:
                for parent in reversed(PurePosixPath(current).parents[:-1]):
                    if parent not in made:
                        (staging / parent).mkdir()
                        made.add(parent)
                if os.path.lexists(staging / current):
                    raise FileExistsError(current)
            except FileExistsError:
                raise StateToolError(
                    f"the backup directory cannot keep this state file's name apart "
                    f"from another's: {current}") from None
            with open(os.open(source, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
                _write_private(
                    staging / current, _within_budget(stream, budget - copied), sync=False)
            copied += (staging / current).stat().st_size
    return copied


def _registered_sessions(ledger):
    """The session digests the registry lists, in order. Each names a lock
    and three paths, so each is checked to be a digest first."""
    digests = [row[0] for row in ledger.execute(
        "SELECT DISTINCT session FROM session_usage ORDER BY session").fetchall()]
    if any(not isinstance(digest, str) or not _SESSION_DIGEST_RE.fullmatch(digest)
           for digest in digests):
        raise StateToolError("quota database contains invalid usage records")
    return digests


def _sessions_to_copy(state_dir, registered):
    """The sessions an online backup takes the lock of, in order: every
    registered one, and every other one that has files on disk.

    An offline backup refuses customer files no registry row attributes, and
    copying the registered sessions alone would leave such files out without
    a word. But files with no row are not yet unattributed. A turn is
    admitted, which writes its session's row, before it takes the session
    lock; a deletion that holds the lock meanwhile removes that row with the
    session; the turn then writes files, and restores the row only when it
    settles. So whether a row is missing is decided under the session's
    lock: a session that still has files and no row there is copied without
    one, and the copy's validation, which is an offline backup's, refuses
    it. Here the files only name the sessions to look at, by the names
    `_validate_quota_database` compares with the registry. A conversation, a
    chain and a sandbox each name their session by its full digest (a
    sandbox since state layout 2, audit F-23), and a sandbox is made only by
    a turn, which opens its session's lock file first. Files that can belong
    to no session (another name, or a sandbox with no such lock file) are
    refused here, and a sandbox of layout 1 with the command that migrates
    it. So is a surface that is not a real directory, as `_regular_files`
    refuses it."""
    sessions = set(registered)
    for root_name, pattern, by_name in (
            ("sessions", "*.kv", False), ("audit", "*.jsonl", False),
            ("sandboxes", "*", True)):
        root = state_dir / root_name
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise StateToolError(f"state surface is not a real directory: {root_name}")
        for path in root.glob(pattern):
            key = path.name if by_name else path.stem
            if key in sessions:
                continue
            if by_name and LEGACY_SANDBOX_NAME.fullmatch(key):
                raise StateToolError(UNMIGRATED_STATE)
            if not _SESSION_DIGEST_RE.fullmatch(key) or (
                    by_name and not (state_dir / "locks" / f"{key}.lock").exists()):
                raise StateToolError("customer state is missing quota registry entries")
            sessions.add(key)
    return sorted(sessions)


def _agree_with_copy(snapshot, captured):
    """Make the ledger's snapshot agree with the sessions that were copied.
    `captured` maps each to the (storage, audit) bytes its registry row
    recorded while its lock was held. A row of a session that is not among
    them (created meanwhile) is dropped, every other row is given those
    bytes, and turn leases are dropped. Returns (the sessions that still have
    a row, the number of rows dropped).

    Only what differs is written. A ledger that already agrees, as that of
    an idle service does, then stays byte for byte as SQLite captured it, and
    the archive holds the ledger an offline backup would; a statement that
    changes nothing still changes the file under some SQLite versions."""
    kept, dropped = set(), 0
    with closing(sqlite3.connect(snapshot, isolation_level=None)) as ledger:
        ledger.execute("BEGIN IMMEDIATE")
        if ledger.execute("SELECT COUNT(*) FROM turn_leases").fetchone()[0]:
            ledger.execute("DELETE FROM turn_leases")
        for digest, storage, audit in ledger.execute(
                "SELECT session, storage_bytes, audit_bytes FROM session_usage").fetchall():
            if digest not in captured:
                ledger.execute("DELETE FROM session_usage WHERE session=?", (digest,))
                dropped += 1
                continue
            kept.add(digest)
            if (storage, audit) != captured[digest]:
                ledger.execute(
                    "UPDATE session_usage SET storage_bytes=?, audit_bytes=? "
                    "WHERE session=?", (*captured[digest], digest))
        ledger.execute("COMMIT")
    return kept, dropped


def _copy_serving_state(state_dir, staging, *, lock_wait_s, max_bytes):
    """Fill `staging` with a consistent copy of a state that may be serving,
    as the notes above describe, and return the capture's report."""
    started = time.monotonic()
    ledger = state_dir / "product-quotas.sqlite3"
    schedule = state_dir / "product-schedules.json"
    for path in (ledger, schedule):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise StateToolError(f"{path.name} must be a regular file")
    sessions, captured, kept, dropped, longest = [], {}, set(), 0, 0.0
    if not ledger.exists():
        _validate_quota_database(ledger, state_dir)  # no registry: no customer state
    else:
        (state_dir / "locks").mkdir(exist_ok=True)
        budget, made = max_bytes, set()
        try:
            with closing(sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)) as live:
                sessions = _sessions_to_copy(state_dir, _registered_sessions(live))
                for digest in sessions:
                    surfaces = (f"sessions/{digest}.kv", f"sandboxes/{digest}",
                                f"audit/{digest}.jsonl")
                    with _product_lock(
                            state_dir / "locks" / f"{digest}.lock", lock_wait_s):
                        held = time.monotonic()
                        rows = live.execute(
                            "SELECT storage_bytes, audit_bytes FROM session_usage "
                            "WHERE session=?", (digest,)).fetchall()
                        for relative in surfaces:
                            budget -= _stage(state_dir, staging, relative, budget, made)
                        longest = max(longest, time.monotonic() - held)
                    if rows:
                        captured[digest] = rows[0]
            with _product_lock(schedule.with_name(f"{schedule.name}.lock"), lock_wait_s):
                _stage(state_dir, staging, schedule.name, budget, made)
            _snapshot_quota_database(ledger, staging / ledger.name)
            kept, dropped = _agree_with_copy(staging / ledger.name, captured)
        except sqlite3.Error as error:
            raise StateToolError("quota database is corrupt or cannot be read") from error
        for digest in captured.keys() - kept:  # deleted before the ledger was captured
            for relative in (f"sessions/{digest}.kv", f"audit/{digest}.jsonl"):
                (staging / relative).unlink(missing_ok=True)
            shutil.rmtree(staging / "sandboxes" / digest, ignore_errors=True)
    return {
        "sessions": len(kept),
        "sessions_created_meanwhile": dropped,
        "sessions_deleted_meanwhile": len(sessions) - len(kept),
        "max_session_lock_hold_ms": round(longest * 1000, 3),
        "capture_seconds": round(time.monotonic() - started, 6),
    }


def create_backup(state_dir, output, *, audit_key, max_bytes=DEFAULT_MAX_BYTES, now=None,
                  online=False, lock_wait_s=DEFAULT_LOCK_WAIT_SECONDS):
    """Write a validated archive of `state_dir` at `output`; return its
    manifest. Offline, the state must carry the clean-shutdown marker of a
    drained service. With `online` it may be serving: see the notes above."""
    state_dir = Path(state_dir).resolve()
    output = Path(output)
    if not state_dir.is_dir():
        raise StateToolError(f"state directory does not exist: {state_dir}")
    if not audit_key:
        raise StateToolError("an audit verification key is required")
    if max_bytes <= 0:
        raise StateToolError("max_bytes must be positive")
    # Layout 1, or a migration not finished. A schedule store of schema 1 is
    # refused, naming the migration, by the validation, which reads it before
    # anything is archived: online, from the copy made under the store's lock.
    problem = unmigrated_state(state_dir)
    if problem:
        raise StateToolError(problem)
    if not online:
        return _write_archive(
            state_dir, output, audit_key=audit_key, max_bytes=max_bytes, now=now)
    if not lock_wait_s >= 0:
        raise StateToolError("the lock wait must be zero or more seconds")
    started = time.time() if now is None else now  # backup age counts from here
    ensure_directory(output.parent)
    if output.is_symlink() or output.exists():
        raise StateToolError("backup output already exists; refusing to overwrite")
    # A whole copy of the state in the clear, so beside the archive it becomes
    # and nowhere else, private, and removed however this ends short of a kill.
    staging = Path(tempfile.mkdtemp(
        prefix=f".{output.name}.", suffix=".online", dir=output.parent))
    try:
        capture = _copy_serving_state(
            state_dir, staging, lock_wait_s=lock_wait_s, max_bytes=max_bytes)
        return _write_archive(
            staging, output, audit_key=audit_key, max_bytes=max_bytes, now=started,
            capture=capture)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _write_archive(state_dir, output, *, audit_key, max_bytes, now, capture=None,
                   legacy=False):
    """Validate the state tree at `state_dir` and publish its archive at
    `output`. `capture` is None for a drained state; for an online backup's
    private copy it is the capture's report, which the manifest then carries
    beside the schema-1 fields. `legacy` is for the migration's pre-migration
    backup, of the state as it is: layout 1, a schedule store of schema 1."""
    state_files = _regular_files(state_dir)
    marker, audit_report = _validate_state(state_dir, audit_key, drained=capture is None,
                                           legacy_sandboxes=legacy, legacy_schedules=legacy)
    with tempfile.TemporaryDirectory(prefix="sigil-pi-quota-snapshot-") as snapshots:
        entries = []
        sources = []
        total = 0
        for path in state_files:
            relative = path.relative_to(state_dir).as_posix()
            source = path
            if relative == "product-quotas.sqlite3":
                # The ledger at the root only: a sandbox file may carry any name.
                source = Path(snapshots) / "quota.sqlite3"
                _snapshot_quota_database(path, source)
            digest, size = _streamed_digest(source, max_bytes - total)
            total += size
            entries.append({"path": relative, "sha256": digest, "size": size})
            sources.append(source)

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "product_version": marker.get("product_version"),
            "created_unix": int(time.time() if now is None else now),
            "clean_shutdown_unix": marker.get("clean_shutdown_unix"),
            "audit_chains": audit_report["chains"],
            "audit_records": audit_report["records"],
            "files": entries,
        }
        if capture is not None:
            manifest.update(mode="online", online=capture)
        manifest_bytes = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
        tag = {MANIFEST_TAG_HEADER: _manifest_tag(audit_key, manifest_bytes)}
        ensure_directory(output.parent)
        if output.is_symlink() or output.exists():
            raise StateToolError("backup output already exists; refusing to overwrite")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with temporary.open("wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                    # pax, which carries the tag; tarfile's default format,
                    # named so that no other default can drop the header
                    with tarfile.open(fileobj=compressed, mode="w",
                                      format=tarfile.PAX_FORMAT) as archive:
                        _add_member(archive, MANIFEST_NAME, len(manifest_bytes),
                                    io.BytesIO(manifest_bytes), pax_headers=tag)
                        for entry, source in zip(entries, sources):
                            with source.open("rb") as stream:
                                reader = _ManifestCheckedReader(stream, entry)
                                _add_member(archive, f"data/{entry['path']}", entry["size"], reader)
                                reader.check_complete()
                os.fchmod(raw.fileno(), 0o600)
                raw.flush()
                os.fsync(raw.fileno())  # the bytes are durable before any name points at them
            try:
                os.link(temporary, output)
            except FileExistsError as error:
                raise StateToolError(
                    "backup output already exists; refusing to overwrite") from error
        finally:
            temporary.unlink(missing_ok=True)
    sync_directory(output.parent)  # and the name, before the caller is told
    return manifest


def _private_backup_directory(backup_dir):
    backup_dir = Path(backup_dir)
    if backup_dir.is_symlink():
        raise StateToolError("managed backup directory must not be a symlink")
    created = not backup_dir.exists()
    ensure_directory(backup_dir.parent)
    backup_dir.mkdir(exist_ok=True, mode=0o700)
    if created:
        sync_directory(backup_dir.parent)  # its archives are no more durable than its name
    info = backup_dir.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise StateToolError("managed backup path is not a directory")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise StateToolError(
            "managed backup directory must not be accessible by group or other")
    return backup_dir.resolve()


@contextmanager
def _backup_directory_lock(backup_dir):
    lock_path = backup_dir / BACKUP_LOCK_NAME
    if lock_path.is_symlink():
        raise StateToolError("managed backup lock must not be a symlink")
    with lock_path.open("a+b") as lock:
        lock_path.chmod(0o600)
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise StateToolError("managed backup lock must be one regular file")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _managed_backup_metadata(path, *, audit_key, max_bytes=DEFAULT_MAX_BYTES):
    """A managed archive's name, digest and creation time, once its name,
    file, manifest, members and the manifest's tag under `audit_key` are all
    what the cycle wrote: retention acts on nothing it cannot vouch for."""
    match = MANAGED_BACKUP_RE.fullmatch(path.name)
    if match is None:
        raise StateToolError("managed backup name is invalid")
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise StateToolError(
            f"managed backup is not one private regular file: {path.name}")
    expected_digest = match.group(2)
    if _sha256_file(path) != expected_digest:
        raise StateToolError(f"managed backup digest does not match its name: {path.name}")
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = _members(archive, max_bytes)
            manifest_member = members.pop(MANIFEST_NAME, None)
            if manifest_member is None or manifest_member.size > 4 * 1024 * 1024:
                raise StateToolError("managed backup manifest is missing or too large")
            manifest_bytes = archive.extractfile(manifest_member).read()
            authentication = _manifest_authentication(
                manifest_member, manifest_bytes, audit_key)
            if authentication != "verified":
                raise StateToolError(
                    _MANAGED_REFUSALS[authentication].format(name=path.name))
            manifest = json.loads(manifest_bytes)
            if not isinstance(manifest, dict):
                raise StateToolError("managed backup manifest is invalid")
            declared = manifest.get("files")
            if (manifest.get("schema_version") != SCHEMA_VERSION
                    or not isinstance(declared, list)):
                raise StateToolError("managed backup manifest is invalid")
            expected_members = set()
            for entry in declared:
                if not isinstance(entry, dict):
                    raise StateToolError("managed backup manifest entry is invalid")
                relative = _safe_relative(entry.get("path", "")).as_posix()
                member_name = f"data/{relative}"
                if member_name in expected_members:
                    raise StateToolError("managed backup manifest repeats a path")
                member = members.get(member_name)
                digest = entry.get("sha256")
                size = entry.get("size")
                if (member is None or member.size != size
                        or not isinstance(digest, str)
                        or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
                    raise StateToolError("managed backup manifest entry is invalid")
                expected_members.add(member_name)
            if set(members) != expected_members:
                raise StateToolError("managed backup has undeclared data members")
    # A manifest that is not UTF-8 JSON ends in a ValueError, one nested past
    # the parser's depth in RecursionError, and an archive cut short in
    # EOFError or zlib.error: each used to escape main as a traceback.
    except (OSError, EOFError, zlib.error, tarfile.TarError, ValueError,
            RecursionError) as error:
        raise StateToolError(f"managed backup cannot be validated: {path.name}") from error
    created_unix = manifest.get("created_unix")
    if (not isinstance(created_unix, int) or isinstance(created_unix, bool)
            or created_unix != int(match.group(1))):
        raise StateToolError("managed backup creation time does not match its name")
    return {"name": path.name, "sha256": expected_digest,
            "created_unix": created_unix}


def _prune_managed_backups_locked(backup_dir, *, audit_key, retention_s, now,
                                  max_bytes=DEFAULT_MAX_BYTES):
    managed = []
    for path in sorted(backup_dir.iterdir()):
        if MANAGED_BACKUP_RE.fullmatch(path.name):
            managed.append(_managed_backup_metadata(
                path, audit_key=audit_key, max_bytes=max_bytes))
    expired = [
        item for item in managed if now - item["created_unix"] >= retention_s]
    # Validate the complete managed set before deleting the first file, so a
    # tampered neighbor never leaves a partially applied retention cycle.
    for item in expired:
        (backup_dir / item["name"]).unlink()
    if expired:
        sync_directory(backup_dir)
    return expired


def run_managed_backup_cycle(state_dir, backup_dir, *, audit_key, retention_s,
                             max_bytes=DEFAULT_MAX_BYTES, now=None,
                             online=False, lock_wait_s=DEFAULT_LOCK_WAIT_SECONDS):
    """Create a validated backup, then remove only validated expired backups.
    With `online` the backup is taken from a state that may be serving."""
    if (not isinstance(retention_s, int) or isinstance(retention_s, bool)
            or retention_s <= 0):
        raise StateToolError("retention_s must be a positive integer")
    created_unix = int(time.time() if now is None else now)
    if created_unix < 0:
        raise StateToolError("managed backup time must be non-negative")
    backup_dir = _private_backup_directory(backup_dir)
    with _backup_directory_lock(backup_dir):
        # An online cycle that was killed left its private copy of the state
        # here, in the clear. Under the directory's lock no other cycle is
        # running, so any such copy is abandoned.
        for path in backup_dir.iterdir():
            if (_ONLINE_COPY_RE.fullmatch(path.name) and path.is_dir()
                    and not path.is_symlink()):
                shutil.rmtree(path)
        staging = backup_dir / (
            f".sigil-pi-state-{os.getpid()}-{secrets.token_hex(8)}.tar.gz")
        try:
            manifest = create_backup(
                state_dir, staging, audit_key=audit_key,
                max_bytes=max_bytes, now=created_unix,
                online=online, lock_wait_s=lock_wait_s)
            digest = _sha256_file(staging)
            final = backup_dir / (
                f"sigil-pi-state-{created_unix:010d}-{digest}.tar.gz")
            try:
                os.link(staging, final)
                created = True
            except FileExistsError:
                if _sha256_file(final) != digest:
                    raise StateToolError("managed backup name collision")
                created = False
            staging.unlink()
            # Nothing expired is deleted until the archive that replaces it
            # would survive a power loss: its bytes and its name, whether this
            # cycle wrote it or found it already published (audit F-12).
            _sync_file(final)
            sync_directory(backup_dir)
            deleted = _prune_managed_backups_locked(
                backup_dir, audit_key=audit_key, retention_s=retention_s,
                now=created_unix, max_bytes=max_bytes)
        finally:
            staging.unlink(missing_ok=True)
    result = {
        "schema_version": SCHEMA_VERSION,
        "created": {
            "created_new_file": created,
            "created_unix": manifest["created_unix"],
            "name": final.name,
            "sha256": digest,
        },
        "deleted": deleted,
        "retention_seconds": retention_s,
    }
    if online:
        result.update(mode="online", online=manifest["online"])
    return result


def _members(archive, max_bytes):
    members = archive.getmembers()
    if len(members) > 1_000_000:
        raise StateToolError("backup contains too many entries")
    total = 0
    by_name = {}
    for member in members:
        if not member.isreg():
            raise StateToolError(f"backup contains non-regular member: {member.name}")
        _safe_relative(member.name)
        if member.name in by_name:
            raise StateToolError(f"backup contains duplicate member: {member.name}")
        total += member.size
        if member.size < 0 or total > max_bytes:
            raise StateToolError("backup exceeds the configured restore size limit")
        by_name[member.name] = member
    return by_name


def restore_backup(backup, state_dir, *, audit_key, max_bytes=DEFAULT_MAX_BYTES,
                   skip_manifest_authentication=False):
    """Install the archive at `backup` as a new state directory `state_dir`
    and return its manifest, once its manifest's tag authenticates under
    `audit_key`. `skip_manifest_authentication` restores an archive whose
    manifest carries no tag at all, as every release before manifest
    authentication wrote them; the manifest it returns then says
    "manifest_authentication": "skipped". A tag that is there is checked
    either way, and so is everything else."""
    backup = Path(backup)
    state_dir = Path(state_dir).resolve()
    if max_bytes <= 0:
        raise StateToolError("max_bytes must be positive")
    if not audit_key:
        raise StateToolError("an audit verification key is required")
    if state_dir.exists():
        raise StateToolError("restore target already exists; refusing to overwrite state")
    ensure_directory(state_dir.parent)
    temporary = Path(tempfile.mkdtemp(prefix=f".{state_dir.name}.restore-", dir=state_dir.parent))
    try:
        with tarfile.open(backup, "r:gz") as archive:
            by_name = _members(archive, max_bytes)
            manifest_member = by_name.pop(MANIFEST_NAME, None)
            if manifest_member is None or manifest_member.size > 4 * 1024 * 1024:
                raise StateToolError("backup manifest is missing or too large")
            manifest_bytes = archive.extractfile(manifest_member).read()
            authentication = _manifest_authentication(
                manifest_member, manifest_bytes, audit_key)
            skipped = authentication == "untagged" and skip_manifest_authentication
            if authentication != "verified" and not skipped:
                raise StateToolError(_RESTORE_REFUSALS[authentication])
            try:
                manifest = json.loads(manifest_bytes)
            # not UTF-8, not JSON, or nested past the parser's depth
            except (ValueError, RecursionError) as error:
                raise StateToolError("backup manifest is invalid") from error
            if not isinstance(manifest, dict):
                raise StateToolError("backup manifest is invalid")
            if manifest.get("schema_version") != SCHEMA_VERSION:
                raise StateToolError("backup schema is unsupported")
            declared = manifest.get("files")
            if not isinstance(declared, list):
                raise StateToolError("backup manifest file list is invalid")
            expected_names = set()
            for entry in declared:
                if not isinstance(entry, dict):
                    raise StateToolError("backup manifest entry is invalid")
                relative = _safe_relative(entry.get("path", ""))
                member_name = f"data/{relative.as_posix()}"
                if member_name in expected_names:
                    raise StateToolError(f"backup manifest repeats a path: {relative}")
                expected_names.add(member_name)
                member = by_name.get(member_name)
                if member is None or member.size != entry.get("size"):
                    raise StateToolError(f"backup member is missing or has wrong size: {relative}")
                destination = temporary.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                # The manifest repeats no path, so a name already taken in the
                # staged tree is another file's on a volume that keeps names
                # apart less strictly than the backup's did (one that ignores
                # case, say): writing would leave one file where it holds two.
                if os.path.lexists(destination):
                    raise StateToolError(
                        "the restore target cannot keep this backup file's name apart "
                        f"from another's: {relative}")
                digest = hashlib.sha256()
                _write_private(destination, _hashed_chunks(archive.extractfile(member), digest))
                if digest.hexdigest() != entry.get("sha256"):
                    raise StateToolError(f"backup checksum failed: {relative}")
            if set(by_name) != expected_names:
                raise StateToolError("backup contains undeclared data members")

        _write_private(temporary / CLEAN_MARKER, [(json.dumps({
            "schema_version": SCHEMA_VERSION,
            "product_version": manifest.get("product_version"),
            "restored_unix": int(time.time()),
            "source_created_unix": manifest.get("created_unix"),
        }, sort_keys=True, separators=(",", ":")) + "\n").encode()])
        directories = sorted((path for path in temporary.rglob("*") if path.is_dir()), reverse=True)
        for directory in directories:
            directory.chmod(0o700)
        temporary.chmod(0o700)
        # A backup of state layout 1, or of a schedule store of schema 1,
        # restores as it was: a release before them wrote it, and `migrate`
        # converts it (audit F-23, F-27).
        _validate_state(temporary, audit_key, legacy_sandboxes=True, legacy_schedules=True)
        # Every file was synced as it was written; the tree's directories are
        # synced before its name is published, and the name after (F-12).
        for directory in [*directories, temporary]:
            sync_directory(directory)
        os.replace(temporary, state_dir)
        sync_directory(state_dir.parent)
        if skipped:
            return {**manifest, "manifest_authentication": "skipped"}
        return manifest
    except (EOFError, zlib.error) as error:  # an archive cut short or damaged
        shutil.rmtree(temporary, ignore_errors=True)
        raise StateToolError("backup archive is truncated or corrupt") from error
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


# ── state layout 2: sandbox names (audit 2026-11-10, F-23) ───────────────
#
# State layout 1 named a session's sandbox by the first 16 hex characters of
# the session's digest, 64 bits; layout 2 names it by the whole digest, as
# the conversation and the audit chain are named. `migrate` moves each
# layout-1 directory to its full name, as docs/state-compatibility.md
# requires of a migration:
# - Versioned. It is the step from layout 1 to layout 2, and says so in its
#   report and its journal. Its input is validated as layout 1 (or as a run
#   of this step that stopped), its output as layout 2, each with every
#   check a backup makes.
# - Dry-runnable. `--dry-run` validates, plans, lists every move and every
#   problem, and changes nothing.
# - An immutable backup first. Before its first rename it archives the
#   state as it is to a new path, read-only, never overwriting a file.
# - Restartable. After the backup and before the first rename it writes a
#   journal, durably, naming the backup and its digest. A run that finds the
#   journal resumes with that backup, intact; each rename is atomic, so the
#   moves left are the layout-1 directories still there. The journal goes
#   last, once the output validates. Until then the product, and a backup,
#   refuse the state.
# - Never merging or overwriting. A directory whose prefix two registered
#   sessions share (F-23's own case) holds files no one can attribute, and
#   one whose full name already exists would be merged into it. Either is
#   reported, and the run stops before it changes anything.
# It runs only on a drained state (the clean-shutdown marker), as an offline
# backup does, and one run at a time: it holds an exclusive lock on the
# state directory itself.
#
# The command takes every step a state needs, in order, under one backup and
# one journal, which names them: (1) "sandbox-full-digest", state layout 1 to
# 2, above; (2) "schedule-store-rows", the schedule store from schema 1, the
# JSON document product-schedules.json, to schema 2, rows of the quota ledger
# (audit 2026-11-10, F-27; product_service.py). Step 2's input check refuses,
# before anything changes, a document or an entry that no release wrote, and
# rows already in the ledger that are not exactly the document's: it never
# merges schedules. Its one change to the ledger is one transaction, the
# tables and every row, synced; then the marker replaces the document. A run
# stopped before that commit changed nothing in the ledger; one stopped after
# it finds the rows equal to the document, and writes the marker. A state
# with no document has nothing for step 2 to do.

MIGRATION = "sandbox-full-digest"
SCHEDULE_MIGRATION = "schedule-store-rows"
MIGRATION_STEPS = (MIGRATION, SCHEDULE_MIGRATION)
# Schema 2 names its run's steps; schema 1, the first migration's, named its
# one step as its migration, and is still resumed.
MIGRATION_JOURNAL_SCHEMA = 2
_MIGRATION_JOURNAL_MAX_BYTES = 64 * 1024
_NEVER_MERGED = "a migration never merges or overwrites a directory"


def _plan_sandbox_moves(legacy, registered, present):
    """The moves that take the layout-1 directories named in `legacy` to
    their full names, and why each other one cannot move. `registered` is
    every registered session digest, `present` every name in `sandboxes/`."""
    owners = {}
    for digest in registered:
        owners.setdefault(digest[:16], []).append(digest)
    moves, problems = [], []
    for name in sorted(legacy):
        source = f"sandboxes/{name}"
        claimed = sorted(owners.get(name, ()))
        if len(claimed) != 1:
            problems.append({
                "path": source, "sessions": claimed,
                "reason": (f"named by the prefix of {len(claimed)} registered sessions, so "
                           f"no one can say whose its files are; {_NEVER_MERGED}")})
        elif claimed[0] in present:
            problems.append({
                "path": source, "target": f"sandboxes/{claimed[0]}",
                "reason": f"its full name already exists; {_NEVER_MERGED}"})
        else:
            moves.append({"from": source, "to": f"sandboxes/{claimed[0]}"})
    return moves, problems


def _journal_steps(journal):
    """The steps of the run that wrote `journal`, or None for a journal this
    release cannot have written."""
    if journal.get("schema_version") == 1:
        return [MIGRATION] if journal.get("migration") == MIGRATION else None
    steps = journal.get("steps")
    if (journal.get("schema_version") == MIGRATION_JOURNAL_SCHEMA and isinstance(steps, list)
            and steps and steps == [step for step in MIGRATION_STEPS if step in steps]):
        return steps
    return None


def _plan_schedule_rows(found, ledger):
    """Step 2's input check: the rows the schema-1 document `found` holds
    (as _validate_state found it) becomes, or None when there is no document
    to convert. Raises, before anything changes, for a document or an entry
    that no release wrote, and for rows already in the ledger that are not
    exactly the document's: a run that stopped after its commit left the
    document's own."""
    if not found:
        return None
    try:
        rows = schedule_rows(found["document"], found["mtime"])
    except ValueError as error:
        raise StateToolError(f"schedule state cannot be migrated: {error} "
                             "(docs/runbooks.md#state-migration)") from error
    with closing(sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)) as db:
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        held = read_schedule_rows(db)[0] if "schedules" in tables else []
    if held and held != rows:
        raise StateToolError(
            "schedule state cannot be migrated: the ledger already holds schedule rows "
            "that are not product-schedules.json's, and a migration never merges "
            "schedules (docs/runbooks.md#state-migration)")
    return rows


def _commit_schedule_rows(ledger, rows):
    """Step 2's one change to the ledger: the schedule tables and every row,
    in one transaction, synced before it returns. Rows already there are the
    document's, as the input check saw: a run that stopped after this commit,
    which only the marker is left to finish."""
    try:
        with closing(sqlite3.connect(ledger, timeout=5, isolation_level=None)) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            for statement in SCHEDULE_TABLES:
                db.execute(statement)
            if not db.execute("SELECT COUNT(*) FROM schedules").fetchone()[0]:
                for row in rows:
                    write_schedule_row(db, row)
            db.execute("COMMIT")
    except sqlite3.Error as error:
        raise StateToolError(
            "the schedule rows could not be written; run the migration again") from error


def _read_migration_journal(path):
    """The journal of a migration that has not finished, or None."""
    if not (path.exists() or path.is_symlink()):
        return None
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("not a regular file")
        with path.open("rb") as stream:
            content = stream.read(_MIGRATION_JOURNAL_MAX_BYTES + 1)
        if len(content) > _MIGRATION_JOURNAL_MAX_BYTES:
            raise ValueError("larger than a journal")
        journal = json.loads(content)
        if (not isinstance(journal, dict)
                or _journal_steps(journal) is None
                or not isinstance(journal.get("backup"), str)
                or not isinstance(journal.get("backup_sha256"), str)
                or not _SESSION_DIGEST_RE.fullmatch(journal["backup_sha256"])):
            raise ValueError("not a journal this release wrote")
    except (OSError, ValueError) as error:
        raise StateToolError(
            f"state migration journal is invalid: {path.name}; restore the pre-migration "
            "backup into a new path and migrate that") from error
    return journal


def _verify_pre_migration_backup(journal):
    backup = Path(journal["backup"])
    try:
        intact = (backup.is_file() and not backup.is_symlink()
                  and _sha256_file(backup) == journal["backup_sha256"])
    except OSError:
        intact = False
    if not intact:
        raise StateToolError(
            f"the pre-migration backup {backup} is missing or changed since the migration "
            "began; a migration resumes only with it intact")


@contextmanager
def _migration_lock(state_dir):
    """One migration at a time on a state directory: an exclusive flock on
    the directory itself, which ends with its process."""
    descriptor = os.open(state_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise StateToolError("another state migration is running on this state") from error
        yield
    finally:
        os.close(descriptor)


def _finish_migration(state_dir, journal_path):
    """The last step: the journal goes, with any scratch a killed write of
    it left, durably."""
    for path in (journal_path, *scratch_files(journal_path)):
        path.unlink(missing_ok=True)
    sync_directory(state_dir)


def migrate_state(state_dir, backup=None, *, audit_key, dry_run=False,
                  max_bytes=DEFAULT_MAX_BYTES, now=None):
    """Take a drained state to layout 2 and its schedule store to schema 2,
    as the notes above describe, and return the report: the steps taken (or,
    dry, to take), each step's own part (the moves made or to make, the
    schedules converted), the problems, and the pre-migration backup.
    `backup` is a new path, required unless `dry_run`; a resumed run must
    name the one its journal names."""
    state_dir = Path(state_dir).resolve()
    if not state_dir.is_dir():
        raise StateToolError(f"state directory does not exist: {state_dir}")
    if not audit_key:
        raise StateToolError("an audit verification key is required")
    if max_bytes <= 0:
        raise StateToolError("max_bytes must be positive")
    if backup is None and not dry_run:
        raise StateToolError(
            "a migration needs --backup: a new file for the pre-migration backup")
    backup = None if backup is None else Path(os.path.abspath(backup))
    journal_path = state_dir / STATE_MIGRATION_JOURNAL
    with _migration_lock(state_dir):
        journal = _read_migration_journal(journal_path)
        if journal is not None:
            if backup is not None and str(backup) != journal["backup"]:
                raise StateToolError(
                    f"this migration began with the backup {journal['backup']}; run it "
                    "again with that --backup to finish it")
            _verify_pre_migration_backup(journal)
        found = {}
        _validate_state(state_dir, audit_key, legacy_sandboxes=True,  # the input
                        legacy_schedules=True, schedule_document=found)
        ledger = state_dir / "product-quotas.sqlite3"
        registered = []
        if ledger.exists():
            with closing(sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)) as db:
                registered = _registered_sessions(db)
        root = state_dir / "sandboxes"
        legacy = legacy_sandbox_directories(root)
        present = {entry.name for entry in root.iterdir()} if root.is_dir() else set()
        moves, problems = _plan_sandbox_moves(legacy, registered, present)
        rows = _plan_schedule_rows(found, ledger)
        taken = [] if journal is None else _journal_steps(journal)
        sandboxes = bool(legacy) or MIGRATION in taken
        schedules = rows is not None or SCHEDULE_MIGRATION in taken
        report = {
            "migration": MIGRATION,
            "from_layout": 1 if sandboxes else 2,
            "to_layout": 2,
            "schedule_store": {
                "migration": SCHEDULE_MIGRATION, "from_schema": 1 if schedules else 2,
                "to_schema": SCHEDULE_STORE_SCHEMA,
                "schedules": 0 if rows is None else len(rows)},
            "steps": [step for step, needed in zip(MIGRATION_STEPS, (sandboxes, schedules))
                      if needed],
            "dry_run": dry_run,
            "resumed": journal is not None,
            "moves": moves,
            "problems": problems,
            "backup": None if journal is None else journal["backup"],
            "backup_sha256": None if journal is None else journal["backup_sha256"],
        }
        if dry_run:
            return report
        if problems:
            raise StateToolError(
                "state migration refused before any change: " + "; ".join(
                    f"{problem['path']}: {problem['reason']}" for problem in problems)
                + " (docs/runbooks.md#state-migration)")
        if journal is None:
            if not report["steps"]:
                return report  # current already: nothing to back up or change
            _write_archive(state_dir, backup, audit_key=audit_key, max_bytes=max_bytes,
                           now=now, legacy=True)
            backup.chmod(0o400)
            journal = {
                "schema_version": MIGRATION_JOURNAL_SCHEMA,
                "steps": report["steps"],
                "backup": str(backup),
                "backup_sha256": _sha256_file(backup),
                "started_unix": int(time.time() if now is None else now),
            }
            write_durably(journal_path, (json.dumps(journal, sort_keys=True) + "\n").encode())
            report.update(backup=journal["backup"], backup_sha256=journal["backup_sha256"])
        for move in moves:
            target = state_dir / move["to"]
            if target.exists() or target.is_symlink():
                raise StateToolError(
                    f"{move['to']} appeared while {move['from']} was moving; {_NEVER_MERGED}")
            os.rename(state_dir / move["from"], target)
        # Whenever the migration has the step, not only when this run moved
        # something: a resumed run finds every rename made, but not whether
        # the entries reached the disk, since the run before it may have
        # failed at this sync or died before it (review R2, 2026-11-17).
        if sandboxes:
            sync_directory(root)
        if rows is not None:
            _commit_schedule_rows(ledger, rows)
            write_durably(state_dir / "product-schedules.json", SCHEDULE_STORE_MARKER)
        _validate_state(state_dir, audit_key)  # the output: layout 2, schedule store 2
        if schedules:
            # The same for the store's marker and ledger: a resumed run that
            # found the marker written writes nothing, so it syncs the
            # directory that holds them before the journal goes.
            sync_directory(state_dir)
        _finish_migration(state_dir, journal_path)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    backup_parser = sub.add_parser("backup")
    backup_parser.add_argument("--state", required=True)
    backup_parser.add_argument("--output", required=True)
    backup_parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    restore_parser = sub.add_parser("restore")
    restore_parser.add_argument("--backup", required=True)
    restore_parser.add_argument("--state", required=True)
    restore_parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    restore_parser.add_argument(
        SKIP_AUTHENTICATION_FLAG, action="store_true",
        help="restore a backup whose manifest carries no authentication tag, as "
             "every release before manifest authentication wrote them, without "
             "authenticating it; only for a backup whose origin you know. A tag "
             "that is there is always checked")
    cycle_parser = sub.add_parser("backup-cycle")
    cycle_parser.add_argument("--state", required=True)
    cycle_parser.add_argument("--backup-dir", required=True)
    cycle_parser.add_argument("--retention-days", type=int, required=True)
    cycle_parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    migrate_parser = sub.add_parser(
        "migrate", help="take a drained state to the current state layout and schedule "
                        "store schema (docs/state-compatibility.md)")
    migrate_parser.add_argument("--state", required=True)
    migrate_parser.add_argument(
        "--backup", help="a new file for the immutable pre-migration backup; required "
                         "unless --dry-run, and the same file to resume a run that stopped")
    migrate_parser.add_argument(
        "--dry-run", action="store_true",
        help="validate the state and list every step, move and problem; change nothing")
    migrate_parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    for online_parser in (backup_parser, cycle_parser):
        online_parser.add_argument(
            "--online", action="store_true",
            help="back up a state that is serving, without the clean-shutdown "
                 "marker: each session is copied under its own lock")
        online_parser.add_argument(
            "--lock-wait-seconds", type=float, default=DEFAULT_LOCK_WAIT_SECONDS,
            help="with --online, how long to wait for one lock before the backup "
                 "fails; keep it above PI_TURN_DEADLINE_SECONDS (default: %(default)s)")
    args = parser.parse_args()
    notice = None
    try:
        key = os.environ.get("PI_AUDIT_KEY", "").encode()
        if args.command == "backup":
            manifest = create_backup(
                args.state, args.output, audit_key=key, max_bytes=args.max_bytes,
                online=args.online, lock_wait_s=args.lock_wait_seconds)
        elif args.command == "restore":
            manifest = restore_backup(
                args.backup, args.state, audit_key=key, max_bytes=args.max_bytes,
                skip_manifest_authentication=args.skip_manifest_authentication)
            if manifest.get("manifest_authentication") == "skipped":
                print("state_tool.py: warning: restored a backup whose manifest carries "
                      "no authentication tag, without authenticating it "
                      f"({SKIP_AUTHENTICATION_FLAG})", file=sys.stderr)
            older = [what for what, found in (
                ("is at state layout 1",
                 legacy_sandbox_directories(Path(args.state) / "sandboxes")),
                ("holds a schedule store of schema 1",
                 unmigrated_schedule_store(args.state))) if found]
            if older:
                notice = (f"the restored state {' and '.join(older)}: this release opens "
                          f"and backs it up only after `{MIGRATE_COMMAND}` "
                          "(docs/state-compatibility.md)")
        elif args.command == "migrate":
            manifest = migrate_state(
                args.state, args.backup, audit_key=key, dry_run=args.dry_run,
                max_bytes=args.max_bytes)
        else:
            manifest = run_managed_backup_cycle(
                args.state, args.backup_dir, audit_key=key,
                retention_s=args.retention_days * 86400,
                max_bytes=args.max_bytes,
                online=args.online, lock_wait_s=args.lock_wait_seconds)
    except (OSError, tarfile.TarError, StateToolError) as error:
        parser.exit(2, f"state operation failed: {error}\n")
    print(json.dumps(manifest, sort_keys=True))
    if notice:
        print(f"state operation note: {notice}", file=sys.stderr)
    if args.command == "migrate" and manifest["problems"]:
        parser.exit(2, "state operation failed: state migration is blocked by "
                       f"{len(manifest['problems'])} problem(s), listed above\n")


if __name__ == "__main__":
    main()
