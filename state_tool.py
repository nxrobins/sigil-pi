#!/usr/bin/env python3
"""Validated offline backup and restore for sigil-pi product state.

The service must be cleanly drained before backup. Restore is fail-closed and
never overwrites an existing state directory.
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
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
import time
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath

from agent import verify_audit_dir

# The bundle ships docs/support-matrix.md, which lists Python <3.12 as
# unsupported and requires that "unsupported selections must fail startup where
# the process can detect them" — and the release SBOM stamps
# python.requires >=3.12. Nothing enforced it, so a bundle would happily start,
# bind a port and serve real signed-audit turns on an interpreter its own
# packaged documentation forbids. Checked at import, before any work.
MINIMUM_PYTHON = (3, 12)
if sys.version_info < MINIMUM_PYTHON:
    raise SystemExit(
        f"sigil-pi requires Python {'.'.join(map(str, MINIMUM_PYTHON))} or newer; "
        f"this is {'.'.join(map(str, sys.version_info[:3]))}. See "
        f"docs/support-matrix.md — older interpreters are outside the supported set.")


SCHEMA_VERSION = 1
CLEAN_MARKER = ".product-clean-shutdown.json"
MANIFEST_NAME = "manifest.json"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024 * 1024
MANAGED_BACKUP_RE = re.compile(
    r"^sigil-pi-state-([0-9]{10})-([0-9a-f]{64})\.tar\.gz$")
BACKUP_LOCK_NAME = ".sigil-pi-backup-retention.lock"


class StateToolError(RuntimeError):
    pass


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


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


def _validate_quota_database(path, state_dir):
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
            expected_full = {
                hashlib.sha256(internal.encode()).hexdigest() for internal in internals}
            expected_short = {value[:16] for value in expected_full}
            actual_sessions = {
                item.stem for item in (state_dir / "sessions").glob("*.kv")}
            actual_audit = {
                item.stem for item in (state_dir / "audit").glob("*.jsonl")}
            sandboxes = state_dir / "sandboxes"
            actual_sandboxes = ({item.name for item in sandboxes.iterdir() if item.is_dir()}
                                if sandboxes.exists() else set())
            if (actual_sessions - expected_full or actual_audit - expected_full
                    or actual_sandboxes - expected_short):
                raise StateToolError("customer state is missing quota registry entries")
    except sqlite3.DatabaseError as error:
        raise StateToolError("quota database is corrupt") from error


def _state_file_bytes(path):
    """Capture committed SQLite state, including any valid WAL contents."""
    if path.name != "product-quotas.sqlite3":
        return path.read_bytes()
    with tempfile.TemporaryDirectory(prefix="sigil-pi-quota-snapshot-") as temp:
        snapshot = Path(temp) / "quota.sqlite3"
        with closing(sqlite3.connect(
                f"file:{path}?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(snapshot)) as destination:
                source.backup(destination)
        return snapshot.read_bytes()


def _validate_state(state_dir, audit_key):
    marker = state_dir / CLEAN_MARKER
    if marker.is_symlink() or not marker.is_file():
        raise StateToolError("state has no clean-shutdown marker; drain the product service first")
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

    schedule = state_dir / "product-schedules.json"
    if schedule.exists():
        try:
            value = json.loads(schedule.read_bytes() or b"{}")
        except (OSError, json.JSONDecodeError) as error:
            raise StateToolError("schedule state is corrupt") from error
        if not isinstance(value, dict):
            raise StateToolError("schedule state is not an object")

    _validate_quota_database(state_dir / "product-quotas.sqlite3", state_dir)

    report = verify_audit_dir(state_dir / "audit", key=audit_key)
    if not report["ok"]:
        raise StateToolError(f"audit verification failed for {len(report['problems'])} chain(s)")
    return marker_doc, report


def create_backup(state_dir, output, *, audit_key, max_bytes=DEFAULT_MAX_BYTES, now=None):
    state_dir = Path(state_dir).resolve()
    output = Path(output)
    if not state_dir.is_dir():
        raise StateToolError(f"state directory does not exist: {state_dir}")
    if not audit_key:
        raise StateToolError("an audit verification key is required")
    if max_bytes <= 0:
        raise StateToolError("max_bytes must be positive")
    state_files = _regular_files(state_dir)
    marker, audit_report = _validate_state(state_dir, audit_key)
    entries = []
    total = 0
    payloads = []
    for path in state_files:
        data = _state_file_bytes(path)
        total += len(data)
        if total > max_bytes:
            raise StateToolError("state exceeds the configured backup size limit")
        relative = path.relative_to(state_dir).as_posix()
        entries.append({"path": relative, "sha256": _sha256_bytes(data), "size": len(data)})
        payloads.append((relative, data))

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "product_version": marker.get("product_version"),
        "created_unix": int(time.time() if now is None else now),
        "clean_shutdown_unix": marker.get("clean_shutdown_unix"),
        "audit_chains": audit_report["chains"],
        "audit_records": audit_report["records"],
        "files": entries,
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_symlink() or output.exists():
        raise StateToolError("backup output already exists; refusing to overwrite")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as archive:
                    for name, data in [(MANIFEST_NAME, manifest_bytes), *[(f"data/{p}", d) for p, d in payloads]]:
                        info = tarfile.TarInfo(name)
                        info.size = len(data)
                        info.mode = 0o600
                        info.uid = info.gid = 0
                        info.uname = info.gname = "root"
                        info.mtime = 0
                        archive.addfile(info, io.BytesIO(data))
        temporary.chmod(0o600)
        try:
            os.link(temporary, output)
        except FileExistsError as error:
            raise StateToolError(
                "backup output already exists; refusing to overwrite") from error
    finally:
        temporary.unlink(missing_ok=True)
    return manifest


def _private_backup_directory(backup_dir):
    backup_dir = Path(backup_dir)
    if backup_dir.is_symlink():
        raise StateToolError("managed backup directory must not be a symlink")
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
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


def _managed_backup_metadata(path, *, max_bytes=DEFAULT_MAX_BYTES):
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
            manifest = json.load(archive.extractfile(manifest_member))
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
    except (OSError, tarfile.TarError, json.JSONDecodeError) as error:
        raise StateToolError(f"managed backup cannot be validated: {path.name}") from error
    created_unix = manifest.get("created_unix")
    if (not isinstance(created_unix, int) or isinstance(created_unix, bool)
            or created_unix != int(match.group(1))):
        raise StateToolError("managed backup creation time does not match its name")
    return {"name": path.name, "sha256": expected_digest,
            "created_unix": created_unix}


def _prune_managed_backups_locked(backup_dir, *, retention_s, now,
                                  max_bytes=DEFAULT_MAX_BYTES):
    managed = []
    for path in sorted(backup_dir.iterdir()):
        if MANAGED_BACKUP_RE.fullmatch(path.name):
            managed.append(_managed_backup_metadata(path, max_bytes=max_bytes))
    expired = [
        item for item in managed if now - item["created_unix"] >= retention_s]
    # Validate the complete managed set before deleting the first file, so a
    # tampered neighbor never leaves a partially applied retention cycle.
    for item in expired:
        (backup_dir / item["name"]).unlink()
    if expired:
        directory = os.open(backup_dir, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return expired


def run_managed_backup_cycle(state_dir, backup_dir, *, audit_key, retention_s,
                             max_bytes=DEFAULT_MAX_BYTES, now=None):
    """Create a validated backup, then remove only validated expired backups."""
    if (not isinstance(retention_s, int) or isinstance(retention_s, bool)
            or retention_s <= 0):
        raise StateToolError("retention_s must be a positive integer")
    created_unix = int(time.time() if now is None else now)
    if created_unix < 0:
        raise StateToolError("managed backup time must be non-negative")
    backup_dir = _private_backup_directory(backup_dir)
    with _backup_directory_lock(backup_dir):
        staging = backup_dir / (
            f".sigil-pi-state-{os.getpid()}-{secrets.token_hex(8)}.tar.gz")
        try:
            manifest = create_backup(
                state_dir, staging, audit_key=audit_key,
                max_bytes=max_bytes, now=created_unix)
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
            deleted = _prune_managed_backups_locked(
                backup_dir, retention_s=retention_s, now=created_unix,
                max_bytes=max_bytes)
        finally:
            staging.unlink(missing_ok=True)
    return {
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


def restore_backup(backup, state_dir, *, audit_key, max_bytes=DEFAULT_MAX_BYTES):
    backup = Path(backup)
    state_dir = Path(state_dir).resolve()
    if max_bytes <= 0:
        raise StateToolError("max_bytes must be positive")
    if not audit_key:
        raise StateToolError("an audit verification key is required")
    if state_dir.exists():
        raise StateToolError("restore target already exists; refusing to overwrite state")
    state_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{state_dir.name}.restore-", dir=state_dir.parent))
    try:
        with tarfile.open(backup, "r:gz") as archive:
            by_name = _members(archive, max_bytes)
            manifest_member = by_name.pop(MANIFEST_NAME, None)
            if manifest_member is None or manifest_member.size > 4 * 1024 * 1024:
                raise StateToolError("backup manifest is missing or too large")
            try:
                manifest = json.load(archive.extractfile(manifest_member))
            except (TypeError, json.JSONDecodeError) as error:
                raise StateToolError("backup manifest is invalid") from error
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
                data = archive.extractfile(member).read()
                if _sha256_bytes(data) != entry.get("sha256"):
                    raise StateToolError(f"backup checksum failed: {relative}")
                destination = temporary.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                destination.write_bytes(data)
                destination.chmod(0o600)
            if set(by_name) != expected_names:
                raise StateToolError("backup contains undeclared data members")

        restored_marker = temporary / CLEAN_MARKER
        restored_marker.write_text(json.dumps({
            "schema_version": SCHEMA_VERSION,
            "product_version": manifest.get("product_version"),
            "restored_unix": int(time.time()),
            "source_created_unix": manifest.get("created_unix"),
        }, sort_keys=True, separators=(",", ":")) + "\n")
        restored_marker.chmod(0o600)
        for directory in sorted((path for path in temporary.rglob("*") if path.is_dir()), reverse=True):
            directory.chmod(0o700)
        temporary.chmod(0o700)
        _validate_state(temporary, audit_key)
        os.replace(temporary, state_dir)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


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
    cycle_parser = sub.add_parser("backup-cycle")
    cycle_parser.add_argument("--state", required=True)
    cycle_parser.add_argument("--backup-dir", required=True)
    cycle_parser.add_argument("--retention-days", type=int, required=True)
    cycle_parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args()
    try:
        if args.command == "backup":
            key = os.environ.get("PI_AUDIT_KEY", "").encode()
            manifest = create_backup(
                args.state, args.output, audit_key=key, max_bytes=args.max_bytes)
        elif args.command == "restore":
            key = os.environ.get("PI_AUDIT_KEY", "").encode()
            manifest = restore_backup(
                args.backup, args.state, audit_key=key, max_bytes=args.max_bytes)
        else:
            key = os.environ.get("PI_AUDIT_KEY", "").encode()
            manifest = run_managed_backup_cycle(
                args.state, args.backup_dir, audit_key=key,
                retention_s=args.retention_days * 86400,
                max_bytes=args.max_bytes)
    except (OSError, tarfile.TarError, StateToolError) as error:
        parser.exit(2, f"state operation failed: {error}\n")
    print(json.dumps(manifest, sort_keys=True))


if __name__ == "__main__":
    main()
