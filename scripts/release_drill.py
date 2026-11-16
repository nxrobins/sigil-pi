#!/usr/bin/env python3
"""Run a digest-bound clean-install, restore, upgrade, and rollback drill.

Two release archives are verified and installed, a state backup is restored
with the old release's own tool, and four phases follow in order: the old
release on a clean state, the old release on the restored state, the
candidate, and the old release again.

THE UPGRADE AND THE ROLLBACK ARE THE ONES docs/state-compatibility.md
DESCRIBES. The old release's own tool backs the state it served up; the
candidate's tool restores that pre-upgrade backup into a new path, with
`--skip-manifest-authentication` only when the archive carries no tag (a
release before manifest authentication wrote it), its digest recorded when it
was written and checked before; and the candidate's `state_tool.py migrate`,
when it has the command, dry-runs that copy and migrates it, with an immutable
pre-migration backup, when the dry run names work. The candidate starts
there. When the release declares `state migration required` (it migrated the
state, or its state formats differ from the old release's: see
_declaration), the old release never opens the candidate's state: the
rollback restores the pre-migration backup, or the pre-upgrade backup, into a
new path with the old release's own tool and starts the old release there.
Otherwise the old release starts on the state the candidate left. The report
records the declaration and every state command under `state_migration`.

EVERY PHASE RUNS A TURN. A release that starts has shown that it starts. So
each phase waits for readiness, requires an answer from `/v1/version` and
`/v1/metrics`, reads one session through the release (its conversation and
its audit chain must be exactly what was committed, and then what the phases
before it left), runs one turn in that session against a stand-in provider on
loopback, and reads the session again (the turn must have been committed
after what was there). The candidate therefore continues a conversation the
old release wrote, and the rollback continues the one the candidate wrote, or,
for `state migration required`, the one the old release left before the
upgrade. Any answer other than the expected one fails the phase, and the
drill stops there.

RECOVERY IS TIMED TO THE FIRST SUCCESSFUL TURN, from the start of the restore
and from the start of the rollback.

AND FROM AN ONLINE BACKUP, TAKEN WHILE TURNS RUN. Production backs up online,
no more than 15 minutes apart, so the 15-minute objective rests on that path
(audit 2026-11-10, F-03). After the four phases the candidate's own state tool
restores the old release's backup of the state the rollback left into a new
directory, and migrates it as for the upgrade, and the candidate serves it: it
reads the session and runs a turn as every phase does, and then,
while it serves turns in the session, its `state_tool.py backup --online`
copies the live state from another process. The first of those turns is held
at the provider until the backup has started. The archive is restored into
another new directory, and the candidate starts on it, reads the session
exactly as the archive holds it and runs a turn. The report records these two
starts under `online_backup`, with the backup's mode, its age when its restore
began and the longest time it held one session's lock.

A drill that stops still writes its report, which names the phase and the
reason and can never qualify, and each phase keeps the log of the service it
started (``<work-dir>/logs``). The state the phases ran on is compared with
the backup afterwards: the session they continued by content, everything else
byte for byte.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

# toolchain.py sits one directory up, in a checkout and in the bundle (app/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import toolchain  # noqa: E402

# The bundle ships docs/support-matrix.md, which lists Python <3.12 and >3.14 as
# unsupported and requires that "unsupported selections must fail startup where
# the process can detect them". The supported range is defined once, in
# toolchain.py, as the SBOM stamps it. This drill checked only the floor,
# against a literal of its own, and so started on Python 3.15, the interpreter
# its launcher (`exec python3`) found (audit 2026-11-10, F-24). Checked at
# import, before any work.
if not (toolchain.MINIMUM_PYTHON <= sys.version_info[:2]
        < toolchain.FIRST_UNSUPPORTED_PYTHON):
    raise SystemExit(toolchain.unsupported_python_message(sys.version_info))


REPORT_SCHEMA = 1
MAX_ARTIFACT_BYTES = 20 * 1024 * 1024 * 1024
MAX_MEMBERS = 100_000
# Startup permits the release's 90-second MCP handshake. Waiting for it
# must outlast that budget; measured restore/rollback RTO limits stay unchanged.
DEFAULT_PROBE_TIMEOUT = 120

# What a drill gives each release it boots, beyond where it listens and where
# its state and credentials are. Recorded in every report (`configuration`).
#
# Every other timer and retry budget is the release's own. Until audit
# 2026-11-10 (F-18) the drill and its fixture also set a 60-second lease (the
# releases ship 300), a 30-second turn deadline and compiler timeout (120 and
# 90) and a 30-second drain (120), and recorded none of it. None of the three
# kept here is shorter than what ships:
#   - retention is lengthened (90 days ship; the sweep interval is the shipped
#     one) because the drill boots a RESTORED backup, and a release's startup
#     retention sweep deletes any session that has been inactive for longer
#     than the retention period: a drill of an old backup would lose the very
#     sessions it is checking;
#   - the periodic audit re-verification is spaced out (60 seconds ship). The
#     probe is a boot that lasts seconds, and startup verifies every chain.
SERVICE_SETTINGS = {
    "PI_AUDIT_VERIFY_SECONDS": "3600",
    "PI_RETENTION_DAYS": "365000",
    "PI_RETENTION_INTERVAL_SECONDS": "3600",
}
# How long to wait for a release to exit after SIGTERM: its whole shipped
# drain timeout, and a margin for the process itself.
DRAIN_WAIT_SECONDS = 135
# How long a phase's turn may take: the release's whole shipped turn deadline
# (120 seconds), and a margin for the request itself.
TURN_WAIT_SECONDS = 135
# How long any other answer may take once the release is ready.
REQUEST_WAIT_SECONDS = 10

# Whose conversation every phase reads and continues. scripts/drill_fixture.py
# commits one real turn as this tenant, in this session, and backs it up; in
# the `Recovery drill` workflow that backup is this drill's input. An operator
# drilling a production-equivalent backup names a tenant and a session of
# their own (--tenant, --session).
FIXTURE_TENANT = "drill-fixture-tenant"
FIXTURE_SESSION = "drill-fixture-session"
# The tenant of the drill's own credential, which asks for readiness, version
# and metrics. A release charges every authenticated request to its tenant's
# request window (60 a minute as shipped), and that window is part of the
# restored state. Waiting for readiness is therefore never charged to the
# tenant whose session the phase goes on to continue.
OPERATOR_TENANT = "release-drill"
# The product's own rule for a session name (product_service.SESSION_RE).
_SESSION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
MEASURED_TO = "the first successful turn in the restored session"

# The two starts of the candidate that take and restore an online backup
# (see the module's notes). They are recorded under `online_backup`, beside
# the four phases, whose list the readiness gate reads as it is.
ONLINE_PHASES = ("backup_online_new", "restore_online_new")
ONLINE_COMMAND = "state_tool.py backup --online"
# The turns run while the backup is taken: one at a time, this far apart,
# until it has ended, and at least two and at most ONLINE_TURNS of them.
# Every turn is charged to the session tenant's request window (60 a minute
# as shipped), which the phases before them share.
ONLINE_TURNS = 20
ONLINE_TURN_GAP_SECONDS = 0.05
# How long the provider holds the first of them at most: the drill starts the
# backup as soon as that turn reaches the provider, and then lets it go.
HOLD_SECONDS = REQUEST_WAIT_SECONDS
# How long to wait for the backup. It waits for a busy session lock for at
# most its --lock-wait-seconds, which the drill leaves at the candidate's
# default (state_tool.DEFAULT_LOCK_WAIT_SECONDS, 150 seconds, above the
# shipped 120-second turn deadline); this is two such waits and a margin.
BACKUP_WAIT_SECONDS = 330

# What the drill supplies for a release to start at all, as the report
# records it. The values themselves are a secret, a credential file, and paths
# and ports that differ on every run.
WIRING = {
    "ANTHROPIC_API_KEY": "a placeholder: no request reaches a real provider",
    "PI_ENDPOINT": "the drill's stand-in provider, on loopback",
    "PI_AUDIT_KEY": "the key in --audit-key-file",
    "PI_AUTH_FILE": "two credentials with no tools, valid for an hour: one in "
                    "the drill's own tenant with scope ops:read, for "
                    "readiness, version and metrics; one in the session's "
                    "tenant with scopes chat and sessions:read, for reading "
                    "the session and running the turn",
    "PI_STATE": "a new directory for the clean install; the restored backup "
                "for the old release; for the candidate, its restore of the old "
                "release's pre-upgrade backup into a new directory, migrated "
                "when it must be; for the rollback, the state the candidate "
                "left or, when the release declares state migration required, "
                "the pre-migration or pre-upgrade backup restored into a new "
                "directory by the old release's tool; for the online backup, "
                "the old release's backup of the state the rollback left, "
                "restored into a new directory and migrated as for the "
                "upgrade, and then the online backup, restored into another",
    "PI_HOST": "127.0.0.1",
    "PI_PORT": "an unused loopback port for each start",
    "PI_SYSTEM_FILE": "a file that does not exist, so no system prompt",
}


def service_configuration():
    """What a report says about the services the drill booted."""
    return {
        "settings": dict(SERVICE_SETTINGS),
        "system_prompt": "none: PI_SYSTEM_FILE names a file that does not exist",
        "left_to_the_release": "every other timer, retry budget and limit",
    }


def _setting(name):
    """PI_RETENTION_DAYS -> retention_days, the name under which a release
    reports that setting in /v1/metrics."""
    return name.removeprefix("PI_").lower()


def _configuration_failures(reported):
    """Why the configuration the releases ran with is only the drill's word.

    ``reported`` holds the effective settings each release gave in
    /v1/metrics, under "old" and "new"; None for a release that reports none
    (every release before audit 2026-11-10). The candidate has to report
    them. The old release is already published and cannot be made to, so its
    silence is recorded and nothing more.
    """
    failures = []
    if reported.get("new") is None:
        failures.append(
            "the candidate does not report its effective settings, so the "
            "configuration in force is not recorded")
    for role, release in (("old", "old release"), ("new", "candidate")):
        in_force = reported.get(role)
        if in_force is None:
            continue
        ignored = [f"{name}={value} (it reports {in_force.get(_setting(name))!r})"
                   for name, value in sorted(SERVICE_SETTINGS.items())
                   if str(in_force.get(_setting(name))) != value]
        if ignored:
            failures.append(
                f"the {release} does not report these settings as in force: "
                + ", ".join(ignored))
    return failures


class ReleaseDrillError(RuntimeError):
    pass


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value):
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or not path.parts
            or any(part in ("", ".", "..") for part in path.parts)):
        raise ReleaseDrillError(f"unsafe archive path: {value!r}")
    return path


def _verify_manifest(root):
    manifest = root / "MANIFEST.sha256"
    if not manifest.is_file() or manifest.is_symlink():
        raise ReleaseDrillError("release MANIFEST.sha256 is missing")
    declared = {}
    for line in manifest.read_text().splitlines():
        digest, separator, relative = line.partition("  ")
        if (not separator or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or relative in declared):
            raise ReleaseDrillError("release manifest has an invalid or repeated entry")
        _safe_relative(relative)
        declared[relative] = digest
    actual = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file() and path != manifest
    }
    if set(actual) != set(declared):
        raise ReleaseDrillError("release manifest does not exactly cover installed files")
    for relative, path in actual.items():
        if _sha256(path) != declared[relative]:
            raise ReleaseDrillError(f"release manifest checksum failed: {relative}")


def _install_artifact(artifact, releases_dir, role):
    artifact = Path(artifact)
    releases_dir = Path(releases_dir)
    if artifact.is_symlink() or not artifact.is_file():
        raise ReleaseDrillError(f"{role} artifact is not a regular file")
    digest = _sha256(artifact)
    releases_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{role}-", dir=releases_dir))
    try:
        with tarfile.open(artifact, "r:gz") as archive:
            members = archive.getmembers()
            if len(members) > MAX_MEMBERS:
                raise ReleaseDrillError("release artifact contains too many members")
            seen = set()
            roots = set()
            total = 0
            for member in members:
                relative = _safe_relative(member.name.rstrip("/"))
                normalized = relative.as_posix()
                if normalized in seen:
                    raise ReleaseDrillError(f"release artifact repeats a path: {normalized}")
                seen.add(normalized)
                roots.add(relative.parts[0])
                if not (member.isdir() or member.isreg()):
                    raise ReleaseDrillError(
                        f"release artifact contains a link or special file: {normalized}")
                total += member.size
                if member.size < 0 or total > MAX_ARTIFACT_BYTES:
                    raise ReleaseDrillError("release artifact exceeds the extraction limit")
            if len(roots) != 1:
                raise ReleaseDrillError("release artifact must contain one top-level directory")
            for member in members:
                relative = _safe_relative(member.name.rstrip("/"))
                destination = staging.joinpath(*relative.parts)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True, mode=0o755)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                source = archive.extractfile(member)
                if source is None:
                    raise ReleaseDrillError(f"release member cannot be read: {relative}")
                with destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
                destination.chmod(0o755 if member.mode & 0o111 else 0o644)
        [top_name] = roots
        extracted = staging / top_name
        _verify_manifest(extracted)
        required = (
            "app/VERSION", "app/state_tool.py", "bin/sigil-pi",
            "runtime/target/release/sigil-mcp")
        if any(not (extracted / relative).is_file() for relative in required):
            raise ReleaseDrillError("release artifact lacks a required runtime file")
        version = (extracted / "app" / "VERSION").read_text().strip()
        if not version:
            raise ReleaseDrillError("installed release has an empty VERSION")
        destination = releases_dir / f"{role}-{digest[:12]}"
        if destination.exists() or destination.is_symlink():
            raise ReleaseDrillError(f"release install target already exists: {destination}")
        os.replace(extracted, destination)
        return {"root": destination, "sha256": digest, "version": version}
    # A truncated archive ends in EOFError and a damaged one in zlib.error.
    except (OSError, EOFError, zlib.error, tarfile.TarError) as error:
        raise ReleaseDrillError(f"cannot install {role} artifact: {error}") from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _switch_current(current, target):
    current, target = Path(current), Path(target).resolve()
    current.parent.mkdir(parents=True, exist_ok=True)
    temporary = current.with_name(f".{current.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    try:
        os.symlink(target, temporary)
        os.replace(temporary, current)
    finally:
        temporary.unlink(missing_ok=True)
    if current.resolve(strict=True) != target:
        raise ReleaseDrillError("atomic release switch did not select the expected target")


def _read_private_key(path):
    path = Path(path)
    try:
        info = path.lstat()
    except OSError as error:
        raise ReleaseDrillError("audit key file is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ReleaseDrillError("audit key must be a non-symlink regular file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ReleaseDrillError("audit key file must not be accessible by group or other")
    try:
        value = path.read_text().strip()
    except (OSError, UnicodeDecodeError) as error:
        raise ReleaseDrillError("audit key file must contain UTF-8 text") from error
    if len(value.encode()) < 32 or "\x00" in value or "\n" in value:
        raise ReleaseDrillError("audit key must be one line with at least 32 bytes")
    return value


def _backup_payloads(backup):
    backup = Path(backup)
    if backup.is_symlink() or not backup.is_file():
        raise ReleaseDrillError("state backup is not a regular file")
    try:
        with tarfile.open(backup, "r:gz") as archive:
            members = {member.name: member for member in archive.getmembers()}
            manifest_member = members.get("manifest.json")
            if manifest_member is None or not manifest_member.isreg():
                raise ReleaseDrillError("state backup has no regular manifest")
            manifest = json.load(archive.extractfile(manifest_member))
            if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
                raise ReleaseDrillError("state backup schema is not supported by this drill")
            entries = manifest.get("files")
            if not isinstance(entries, list):
                raise ReleaseDrillError("state backup manifest file list is invalid")
            payloads = {}
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                    raise ReleaseDrillError("state backup manifest entry is invalid")
                relative = _safe_relative(entry.get("path", "")).as_posix()
                if relative in payloads:
                    raise ReleaseDrillError("state backup manifest repeats a path")
                member = members.get(f"data/{relative}")
                if member is None or not member.isreg() or member.size != entry.get("size"):
                    raise ReleaseDrillError(f"state backup member is missing: {relative}")
                data = archive.extractfile(member).read()
                if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
                    raise ReleaseDrillError(f"state backup checksum failed: {relative}")
                payloads[relative] = data
            return manifest, payloads
    # A truncated stream ends in EOFError, a damaged one in zlib.error, and
    # JSON that is not UTF-8 in UnicodeDecodeError (a ValueError, as JSON
    # errors are): until audit 2026-11-10 (F-18) the first two left the drill
    # with no report.
    except (OSError, EOFError, zlib.error, tarfile.TarError, ValueError) as error:
        raise ReleaseDrillError(f"cannot read state backup: {error}") from error


def _state_tool_process(release_root, arguments, audit_key=None):
    """`state_tool.py <arguments>` of the release, run to its end."""
    script = Path(release_root) / "app" / "state_tool.py"
    environment = dict(os.environ)
    if audit_key is not None:
        environment["PI_AUDIT_KEY"] = audit_key
    return subprocess.run(
        [sys.executable, str(script), *map(str, arguments)], cwd=script.parent,
        env=environment, capture_output=True, text=True)


def _run_state_tool(release_root, command, *, audit_key, backup, state, options=()):
    """One state command of the release, and what it printed: `restore`
    installs `backup` as `state`, `migrate` moves `state` (with `backup` for
    its pre-migration backup, or none with --dry-run), and `backup` writes
    `state` to `backup`. `options` follow the command's own arguments."""
    if command == "restore":
        arguments = ["restore", "--backup", backup, "--state", state]
    elif command == "migrate":
        arguments = ["migrate", "--state", state, *(() if backup is None else ("--backup", backup))]
    else:
        arguments = [command, "--state", state, "--output", backup]
    result = _state_tool_process(release_root, [*arguments, *options], audit_key)
    if result.returncode:
        diagnostic = (result.stderr or result.stdout).strip()[-2000:]
        if command == "migrate":
            # A migration that has problems lists them on stdout, in its
            # report, and says only "listed above" on stderr.
            try:
                problems = json.loads(result.stdout).get("problems")
            except (ValueError, AttributeError):
                problems = None
            if problems:
                diagnostic += " " + json.dumps(problems, sort_keys=True)[-2000:]
        raise ReleaseDrillError(f"state {command} failed: {diagnostic}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ReleaseDrillError(f"state {command} returned invalid evidence") from error


# ── the upgrade and the rollback a release declares ──────────────────────
#
# docs/state-compatibility.md, "Upgrade and rollback rule": the old release,
# drained, backs its state up with its own state_tool.py; the candidate's
# state_tool.py restores that backup into a new path, and the candidate starts
# there. A release that changes an on-disk format declares `state migration
# required`: the candidate's `state_tool.py migrate` moves the restored copy,
# the dry run first, and the old release never opens the candidate's state;
# its rollback restores the pre-upgrade backup, or the migration's
# pre-migration backup, into a new path with the old release's own tool. A
# release that declares `state schema unchanged` rolls back onto the state the
# candidate left, which the rollback then proves the old release opens.
#
# Until audit 2026-11-10 (H6) the drill started the candidate on the state
# the old release had served and the old release on the state the candidate
# had served: a release with state layout 2 (F-23) refuses the first and a
# release before it the second, and the candidate's tool refused the old
# release's untagged backups (F-12), so no release that changes a format, or
# authenticates its backups, could qualify.

# The pax header on a backup's manifest that carries its authentication tag,
# and the restore option for an archive without one: state_tool.py's
# MANIFEST_TAG_HEADER and SKIP_AUTHENTICATION_FLAG, restated because the
# drill reads the archives of releases it imports nothing from
# (tests/test_recovery_drill_state_migration.py pins them to state_tool.py).
MANIFEST_TAG_HEADER = "SIGILPI.manifest_tag"
SKIP_AUTHENTICATION_FLAG = "--skip-manifest-authentication"
MIGRATION_REQUIRED = "state migration required"
SCHEMA_UNCHANGED = "state schema unchanged"


def _manifest_tagged(backup):
    """Whether the manifest of the backup at ``backup`` carries an
    authentication tag, as every release since manifest authentication
    writes on it. Whether the tag is genuine is the restoring tool's to
    check; a tag that is there is never skipped."""
    try:
        with tarfile.open(backup, "r:gz") as archive:
            for member in archive:
                if member.name == "manifest.json":
                    return MANIFEST_TAG_HEADER in member.pax_headers
    except (OSError, EOFError, zlib.error, tarfile.TarError) as error:
        raise ReleaseDrillError(f"cannot read state backup: {error}") from error
    raise ReleaseDrillError("state backup has no regular manifest")


def _state_tool_help(release_root, command):
    """The help of the release's `state_tool.py <command>`, or None when its
    state tool has no such command: argparse refuses an unknown command with
    exit status 2, before it does anything."""
    result = _state_tool_process(release_root, [command, "--help"])
    if result.returncode == 2:
        return None
    if result.returncode:
        diagnostic = (result.stderr or result.stdout).strip()[-2000:]
        raise ReleaseDrillError(f"state_tool.py {command} --help failed: {diagnostic}")
    return result.stdout


def _restore_options(tagged, flag_offered):
    """The options a restore of an archive the drill wrote itself is given:
    the flag that skips manifest authentication only for an archive without
    a tag, a release before manifest authentication wrote it, and only for a
    tool that has the flag (one before it does not authenticate at all)."""
    return [SKIP_AUTHENTICATION_FLAG] if not tagged and flag_offered else []


def _has_work(report):
    """Whether a `migrate --dry-run` report names anything to do: a step to
    take (`steps`, once the command holds more than one), a directory to
    move (`moves`), or a run that stopped and is to be finished."""
    return bool(report.get("steps") or report.get("moves") or report.get("resumed"))


def _formats(report):
    """The state formats a release's `migrate --dry-run` says it migrates to:
    every `to_*` field of its report, a nested one under its dotted path
    (`to_layout`; `schedule_store.to_schema`)."""
    found = {}

    def walk(value, prefix):
        for key, item in value.items():
            if isinstance(item, dict):
                walk(item, f"{prefix}{key}.")
            elif key.startswith("to_"):
                found[prefix + key] = item

    walk(report, "")
    return found


def _declaration(old_formats, new_formats, migrated):
    """What the candidate declares about the old release's state, and why.

    ``old_formats`` and ``new_formats`` are each release's `_formats`, None
    for a release whose state tool has no `migrate` command: it predates
    every state format the command moves state to. A release whose formats
    differ from the old release's writes state the old release refuses, even
    where its migration found nothing to move: a backup holds no empty
    directory, so the old release's sandboxes, empty until a tool writes a
    file, are not in its pre-upgrade backup, while the candidate's first turn
    makes one in the candidate's format (state layout 2, audit F-23)."""
    if migrated:
        return MIGRATION_REQUIRED, "the candidate's migration moved the old release's state"
    if old_formats != new_formats:
        named = {role: ("none: its state_tool.py has no migrate command" if formats is None
                        else formats)
                 for role, formats in (("old", old_formats), ("new", new_formats))}
        return MIGRATION_REQUIRED, (
            "the two releases' state formats differ (old: {old}; candidate: {new})".format(
                **named))
    return SCHEMA_UNCHANGED, (
        "both releases name the same state formats and the candidate's migration had "
        "nothing to move")


def _renamed(payloads, moves):
    """A backup's files (``payloads``, by path) under the names a migration
    gave them: each move takes one directory, `from`, to `to`."""
    if not isinstance(moves, list) or not all(
            isinstance(move, dict) and isinstance(move.get("from"), str)
            and isinstance(move.get("to"), str) for move in moves):
        raise ReleaseDrillError("the candidate's migration reported invalid moves")
    renamed = {}
    for path, data in payloads.items():
        for move in moves:
            prefix = move["from"].rstrip("/") + "/"
            if path.startswith(prefix):
                path = move["to"].rstrip("/") + "/" + path[len(prefix):]
                break
        renamed[path] = data
    return renamed


def _free_loopback_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


# ── committed customer state ─────────────────────────────────────────────
#
# One definition of where a session is kept and of what it means for
# committed state to have survived, for this drill and for
# scripts/failure_drill.py, which imports it from here: this file ships in
# the bundle and that one does not.

_CUSTOMER_STATE = ("sessions/", "sandboxes/", "audit/")


def _session_files(tenant, session):
    """Where a release keeps one tenant's session, as paths inside its state
    directory: (conversation, audit chain).

    This is the installed host's public tenant/session addressing contract
    (``product_service._internal_session`` and the ``SessionStore`` layout),
    restated because a drill works on a bundle from outside and imports none
    of its modules. tests/test_recovery_drill_turns.py and
    tests/test_failure_drill_integrity.py pin it to the product, so a layout
    that moved without this would fail there.
    """
    tenant_hash = hashlib.sha256(tenant.encode()).hexdigest()
    session_hash = hashlib.sha256(session.encode()).hexdigest()
    digest = hashlib.sha256(f"v1:{tenant_hash}:{session_hash}".encode()).hexdigest()
    return f"sessions/{digest}.kv", f"audit/{digest}.jsonl"


def _messages(conversation):
    """The message list a conversation file holds, or None if it holds none.
    An empty file reads as an empty conversation, as the release reads it."""
    try:
        messages = json.loads(conversation or b"[]")
    except (ValueError, UnicodeDecodeError):
        return None
    return messages if isinstance(messages, list) else None


def _records(chain):
    """The records an audit chain file holds, one to a line, or None if a
    line is not a record."""
    try:
        return [json.loads(line) for line in (chain or b"").decode().splitlines()
                if line.strip()]
    except (ValueError, UnicodeDecodeError):
        return None


def _continuity(before, after, tenant, session):
    """How the files committed in ``before`` fared in ``after`` (two backups'
    payloads).

    ``session`` of ``tenant`` is the one that was written to in between. It
    is compared by what it had committed: its committed messages must still
    open the conversation and its committed audit records must still open the
    chain, unchanged and in order. Dropping, reordering or altering any of
    them, or losing a file, is a loss; growth is not. Every other committed
    file must be byte-identical, because nothing addressed it.
    """
    conversation, chain = _session_files(tenant, session)
    committed = {path: data for path, data in before.items()
                 if path.startswith(_CUSTOMER_STATE)}
    lost = []
    for path, data in committed.items():
        now = after.get(path)
        if path == conversation:
            was, became = _messages(data), None if now is None else _messages(now)
            kept = was is not None and became is not None and became[:len(was)] == was
        elif path == chain:
            kept = now is not None and now.startswith(data)
        else:
            kept = now == data
        if not kept:
            lost.append(path)
    was = _messages(committed[conversation]) if conversation in committed else None
    held = bool(was) and bool(committed.get(chain))
    return {
        "committed_customer_files": len(committed),
        "committed_session_files": sum(path.startswith("sessions/") for path in committed),
        "lost_or_changed": sorted(lost),
        "preserved": not lost,
        "session": {
            "session": session,
            "conversation": conversation,
            "audit_chain": chain,
            "held_committed_content": held,
            "committed_messages": len(was or []),
            "messages_after": len(_messages(after.get(conversation, b"")) or []),
            "committed_audit_records": len(committed.get(chain, b"").splitlines()),
            "audit_records_after": len(after.get(chain, b"").splitlines()),
            "preserved": held and conversation not in lost and chain not in lost,
        },
    }


# ── the stand-in provider ────────────────────────────────────────────────

class _ProviderHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        return

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's spelling
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path.endswith("/count_tokens"):
            body = b'{"input_tokens":1}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            messages = json.loads(raw).get("messages")
        except (ValueError, AttributeError):
            messages = None
        with self.server.lock:
            number = len(self.server.requests)
            self.server.requests.append(messages if isinstance(messages, list) else None)
            hold = self.server.hold
        if (hold is not None and isinstance(messages, list) and messages
                and _role(messages[-1]) == "user" and _text(messages[-1]) == hold.message):
            hold.arrived.set()
            hold.release.wait(HOLD_SECONDS)
        body = json.dumps({
            "id": f"msg_recovery_drill_{number + 1}", "type": "message",
            "role": "assistant", "model": "recovery-drill-stand-in",
            "content": [{"type": "text", "text": self.server.reply(number)}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Provider(ThreadingHTTPServer):
    """An Anthropic-shaped completion endpoint on loopback that keeps the
    messages each request carried.

    A phase's turn runs the release's real path, compose, a solver-verified
    guest, a host-injected secret and a signed audit record, whoever answers
    the completion call at the end of it. Answering it here keeps the drill
    on this host and lets it see what the release sent: a turn that carried
    none of the session's conversation did not run in that session. Each
    reply is numbered, so a turn's reply can be told from every other.
    """

    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _ProviderHandler)
        self.lock = threading.Lock()
        self.requests = []  # the `messages` of each request, None if it had none
        self.hold = None  # a _Hold while the drill keeps a turn in flight

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.server_address[1]}/v1/messages"

    @staticmethod
    def reply(number):
        return f"The recovery drill's stand-in provider, reply {number + 1}."


class _Hold:
    """A request the provider holds: one whose last message is the user's
    ``message``. ``arrived`` is set when it comes in, and its reply waits for
    ``release``, for HOLD_SECONDS at most."""

    def __init__(self, message):
        self.message = message
        self.arrived = threading.Event()
        self.release = threading.Event()


# ── a phase: start the release, read the session, run a turn in it ───────

def _call(method, url, token, payload=None, timeout=REQUEST_WAIT_SECONDS):
    """One request to a release: (status, decoded JSON body or None). An HTTP
    error status is an answer; a connection that fails raises OSError or
    http.client.HTTPException."""
    headers = {"Authorization": f"Bearer {token}", "X-Request-ID": "release-drill-probe"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


def _error_code(body):
    error = body.get("error") if isinstance(body, dict) else None
    return error.get("code") if isinstance(error, dict) else None


def _probe_release(release_root, state_dir, auth_file, token, audit_key,
                   timeout_s=DEFAULT_PROBE_TIMEOUT, *, endpoint, session_token,
                   session, message, log_path=None, while_serving=None):
    """Start the release on ``state_dir`` and, once it is ready, ask for its
    version and metrics, read ``session``, run one turn in it and read it
    again. Returns what was observed; the caller judges it.

    Readiness is waited for, because a release takes time to start. Nothing
    after it is: any answer but 200 from /v1/version, /v1/metrics, the turn,
    or the session's export fails the phase with that answer. Until audit
    2026-11-10 (F-18) readiness was recorded first and a failing version or
    metrics request only repeated the wait, so a release whose metrics
    endpoint always failed passed when the wait ran out.

    ``token`` is the drill's own credential (ops:read); ``session_token`` is
    the one in the session's tenant (chat, sessions:read). The service's
    output goes to ``log_path`` when one is given, and stays there.

    ``while_serving``, when given, is called once the session has been read
    again, while the release still serves, with the release's base URL and a
    reader of the session; what it returns is kept as ``while_serving``.
    """
    release_root, state_dir = Path(release_root), Path(state_dir)
    port = _free_loopback_port()
    base = f"http://127.0.0.1:{port}"
    environment = {
        key: value for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "PYTHONPATH"}
    }
    environment.update({
        "ANTHROPIC_API_KEY": "release-drill-provider-placeholder",
        "PI_ENDPOINT": endpoint,
        "PI_AUDIT_KEY": audit_key,
        "PI_AUTH_FILE": str(auth_file),
        "PI_STATE": str(state_dir),
        "PI_HOST": "127.0.0.1",
        "PI_PORT": str(port),
        **SERVICE_SETTINGS,
        "PI_SYSTEM_FILE": str(state_dir.parent / "no-system-prompt"),
    })

    def answer(method, path, credential, payload=None, *, accept=(200,),
               timeout=REQUEST_WAIT_SECONDS):
        try:
            status, body = _call(method, base + path, credential, payload, timeout)
        except (OSError, http.client.HTTPException) as error:
            raise ReleaseDrillError(
                f"{method} {path} was not answered: {type(error).__name__}: {error}") from error
        if status not in accept or not isinstance(body, dict):
            code = _error_code(body)
            raise ReleaseDrillError(
                f"{method} {path} returned {status}" + (f" {code}" if code else ""))
        return status, body

    def read_session():
        """The session as the release serves it, or None if it holds none."""
        path = f"/v1/sessions/{session}/export"
        status, body = answer("GET", path, session_token, accept=(200, 404))
        if status == 404:
            if _error_code(body) != "session_not_found":
                raise ReleaseDrillError(f"GET {path} returned 404 {_error_code(body)}")
            return None
        data = body.get("data")
        conversation = data.get("conversation") if isinstance(data, dict) else None
        audit = data.get("audit") if isinstance(data, dict) else None
        if not isinstance(conversation, list) or not isinstance(audit, list):
            raise ReleaseDrillError(
                f"GET {path} returned no conversation and no audit records")
        return {"conversation": conversation, "audit": audit}

    log = (tempfile.TemporaryFile(mode="w+t") if log_path is None
           else Path(log_path).open("w+t"))
    process = subprocess.Popen(
        [str(release_root / "bin" / "sigil-pi")], cwd=release_root,
        env=environment, stdout=log, stderr=log, text=True)
    started = time.monotonic()
    try:
        ready_at = None
        deadline = started + timeout_s
        while time.monotonic() < deadline and process.poll() is None:
            try:
                status, ready = _call("GET", f"{base}/v1/ready", token, timeout=2)
            except (OSError, http.client.HTTPException):
                status, ready = None, None
            if status == 200 and isinstance(ready, dict) and ready.get("status") == "ready":
                ready_at = time.monotonic()
                break
            time.sleep(0.05)
        if ready_at is None:
            log.seek(0)
            raise ReleaseDrillError(
                "release did not become ready: " + log.read()[-3000:])

        _, version_body = answer("GET", "/v1/version", token)
        expected = (release_root / "app" / "VERSION").read_text().strip()
        if version_body.get("version") != expected:
            raise ReleaseDrillError("version endpoint does not match installed VERSION")
        _, metrics_body = answer("GET", "/v1/metrics", token)
        metrics = metrics_body.get("metrics")
        if not isinstance(metrics, dict):
            raise ReleaseDrillError("GET /v1/metrics returned no metrics")
        settings = metrics.get("effective_settings")

        before = read_session()
        turn_started = time.monotonic()
        _, turn = answer("POST", "/v1/chat", session_token,
                         {"session": session, "message": message},
                         timeout=TURN_WAIT_SECONDS)
        turn_done = time.monotonic()
        reply = turn.get("reply")
        if not isinstance(reply, str) or not reply:
            raise ReleaseDrillError("POST /v1/chat returned no reply")
        after = read_session()
        serving = None if while_serving is None else while_serving(base, read_session)
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=DRAIN_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        log.seek(0)
        diagnostic = log.read()[-3000:]
        log.close()
    if process.returncode != 0:
        raise ReleaseDrillError(f"release did not shut down cleanly: {diagnostic}")
    marker = state_dir / ".product-clean-shutdown.json"
    if not marker.is_file():
        raise ReleaseDrillError("release probe produced no clean-shutdown marker")
    return {
        "version": version_body["version"],
        "ready_seconds": round(ready_at - started, 6),
        "first_turn_seconds": round(turn_done - started, 6),
        "turn_seconds": round(turn_done - turn_started, 6),
        "total_seconds": round(time.monotonic() - started, 6),
        "effective_settings": settings if isinstance(settings, dict) else None,
        "before": before,
        "reply": reply,
        "after": after,
        "while_serving": serving,
    }


def _text(message):
    """The text a conversation message carries, whether its content is a
    string or a list of blocks."""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content
                       if isinstance(block, dict) and block.get("type") == "text")
    return ""


def _role(message):
    return message.get("role") if isinstance(message, dict) else None


def _first_difference(read, committed):
    for index, (one, other) in enumerate(zip(read, committed)):
        if one != other:
            return index
    return min(len(read), len(committed))


def _judge_turn(result, *, expected, session, message, requests, replies):
    """What a phase's turn showed, as the report records it; ReleaseDrillError
    if the phase did not read the session as committed, run a turn in it
    through the provider, and commit that turn after what was there.

    ``expected`` is the session as the phase must find it, a conversation and
    its audit records; None for the clean install, which has none to find.
    ``requests`` and ``replies`` are what the provider received and answered
    during the phase. The record holds counts: what a conversation says is
    not the report's to keep.
    """
    before, after, reply = result.get("before"), result.get("after"), result.get("reply")
    if expected is None:
        if before is not None:
            raise ReleaseDrillError(f"a clean install already holds session {session!r}")
        before = {"conversation": [], "audit": []}
    elif before is None:
        raise ReleaseDrillError(
            f"the release holds no session {session!r} for the tenant, so the "
            f"committed conversation cannot be read through it")
    else:
        for kind, name, unit in (("conversation", "a conversation", "message(s)"),
                                 ("audit", "an audit chain", "record(s)")):
            read, committed = before[kind], expected[kind]
            if read != committed:
                raise ReleaseDrillError(
                    f"the release read {name} of {len(read)} {unit} where "
                    f"{len(committed)} were committed; the first difference is "
                    f"at index {_first_difference(read, committed)}")

    carried = [messages for messages in requests
               if messages and _role(messages[-1]) == "user"
               and _text(messages[-1]) == message]
    if not carried:
        raise ReleaseDrillError(
            "the turn never reached the provider: none of the "
            f"{len(requests)} request(s) it received ends with the phase's message")
    history = len(carried[0]) - 1
    if before["conversation"] and not history:
        raise ReleaseDrillError(
            "the turn sent the provider none of the session's conversation")
    if reply not in replies:
        raise ReleaseDrillError("the turn's reply is not one the provider gave in this phase")

    if after is None:
        raise ReleaseDrillError(f"the release holds no session {session!r} after the turn")
    conversation, audit = after["conversation"], after["audit"]
    kept = len(before["conversation"])
    if conversation[:kept] != before["conversation"]:
        raise ReleaseDrillError(
            f"the turn changed the {kept} message(s) the conversation held before "
            f"it; the first difference is at index "
            f"{_first_difference(conversation, before['conversation'])}")
    added = conversation[kept:]
    if (len(added) < 2 or _role(added[0]) != "user" or _text(added[0]) != message
            or _role(added[-1]) != "assistant" or _text(added[-1]) != reply):
        raise ReleaseDrillError(
            f"the turn was not committed: after the {kept} message(s) the "
            f"conversation held before it, the release holds {len(added)} more, "
            f"which are not the phase's message and the reply to it")
    records = len(before["audit"])
    if audit[:records] != before["audit"] or len(audit) <= records:
        raise ReleaseDrillError(
            f"the turn left no audit record after the {records} the chain held "
            f"before it, or changed one of them")
    return {
        "session": session,
        "read_before_turn": expected is not None,
        "messages_before": kept,
        "messages_after": len(conversation),
        "audit_records_before": records,
        "audit_records_after": len(audit),
        "provider_requests": len(requests),
        "history_messages_sent": history,
        "seconds": result.get("turn_seconds"),
    }


# ── the online backup: turns while it is taken, and what it holds ────────

def _answered(turn):
    """How a turn was answered, for a message: "502 agent_failure", or how
    it was not."""
    if turn is None:
        return "not at all"
    if turn["status"] is None:
        return f"with no answer ({turn['error']})"
    return str(turn["status"]) + (f" {turn['code']}" if turn["code"] else "")


def _back_up_while_serving(base, read_session, *, session, session_token, provider,
                           prefix, command, environment):
    """Run turns in ``session`` of the release at ``base`` and, while they
    run, ``command``: the candidate's ``state_tool.py backup --online``, as
    another process. Then read the session again. Returns what was observed;
    _judge_load judges the turns, and the caller the archive.

    The first turn is held at ``provider`` until the backup has been started,
    so a turn holds the session's lock when the backup starts. More follow,
    one at a time and ONLINE_TURN_GAP_SECONDS apart, until the backup has
    ended: at least two, at most ONLINE_TURNS. A turn that is not answered
    200 ends them, and so does a first turn that never reached the provider.
    """
    messages = [f"{prefix}: turn {number} while the backup runs."
                for number in range(1, ONLINE_TURNS + 1)]
    hold = provider.hold = _Hold(messages[0])
    ended = threading.Event()
    turns = []

    def run_turns():
        for number, message in enumerate(messages, 1):
            if number > 1 and not hold.arrived.is_set():
                return  # the first never reached the provider
            if number > 2 and ended.is_set():
                return
            started = time.monotonic()
            status = body = error = None
            try:
                status, body = _call("POST", base + "/v1/chat", session_token,
                                     {"session": session, "message": message},
                                     timeout=TURN_WAIT_SECONDS)
            except (OSError, http.client.HTTPException) as failure:
                error = f"{type(failure).__name__}: {failure}"
            turns.append({
                "number": number, "message": message, "status": status,
                "code": _error_code(body), "error": error,
                "reply": body.get("reply") if isinstance(body, dict) else None,
                "started": started, "ended": time.monotonic(),
            })
            if status != 200:
                return
            ended.wait(ONLINE_TURN_GAP_SECONDS)

    first_request = len(provider.requests)
    worker = threading.Thread(target=run_turns, daemon=True)
    worker.start()
    process = None
    try:
        while not hold.arrived.wait(0.02) and worker.is_alive():
            pass
        if hold.arrived.is_set():
            process = subprocess.Popen(
                command, cwd=Path(command[1]).parent, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            launched = time.monotonic()  # the backup exists; the turn is still held
    finally:
        hold.release.set()
        if process is None:
            ended.set()
            provider.hold = None
    if process is None:
        worker.join(2 * TURN_WAIT_SECONDS)
        raise ReleaseDrillError(
            "the first turn run while backing up never reached the provider: it "
            f"was answered {_answered(turns[0] if turns else None)}")
    try:
        try:
            stdout, stderr = process.communicate(timeout=BACKUP_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise ReleaseDrillError(
                "the candidate's online backup did not end within "
                f"{BACKUP_WAIT_SECONDS} s") from None
        exited = time.monotonic()
    finally:
        ended.set()
        worker.join(2 * TURN_WAIT_SECONDS)
        provider.hold = None
    if worker.is_alive():
        raise ReleaseDrillError("the turns run while backing up did not end")
    if process.returncode:
        diagnostic = (stderr or stdout).strip()[-2000:]
        raise ReleaseDrillError(f"the candidate's online backup failed: {diagnostic}")
    try:
        printed = json.loads(stdout)
    except ValueError as error:
        raise ReleaseDrillError(
            "the candidate's online backup returned invalid evidence") from error
    return {"turns": turns, "launched": launched, "exited": exited,
            "first_request": first_request, "printed": printed,
            "after": read_session()}


def _judge_load(load, *, before, after, requests, replies):
    """What the turns run while the online backup was taken showed, as the
    report records it, and the length of the conversation at each turn
    boundary, from before the first to after the last; ReleaseDrillError
    unless each was answered 200 with a reply the provider gave, reached the
    provider with the session's conversation, and was committed after the one
    before it with an audit record, and unless the first was in flight when
    the backup started.

    ``before`` is the session before the first of them; ``after`` is the
    session read once the backup had ended and they had stopped.
    """
    turns = load["turns"]
    count = len(turns)
    history = []
    for turn in turns:
        label = f"turn {turn['number']} of {count} run while the online backup was taken"
        if turn["status"] != 200:
            raise ReleaseDrillError(f"{label} was answered {_answered(turn)}")
        if not isinstance(turn["reply"], str) or not turn["reply"]:
            raise ReleaseDrillError(f"{label} returned no reply")
        carried = [messages for messages in requests
                   if messages and _role(messages[-1]) == "user"
                   and _text(messages[-1]) == turn["message"]]
        if not carried:
            raise ReleaseDrillError(f"{label} never reached the provider")
        if before["conversation"] and len(carried[0]) < 2:
            raise ReleaseDrillError(
                f"{label} sent the provider none of the session's conversation")
        if turn["reply"] not in replies:
            raise ReleaseDrillError(f"{label}: its reply is not one the provider gave")
        history.append(len(carried[0]) - 1)
    first = turns[0]
    if not first["started"] < load["launched"] < first["ended"]:
        raise ReleaseDrillError("no turn was in flight when the online backup started")

    conversation, audit = after["conversation"], after["audit"]
    kept = len(before["conversation"])
    if conversation[:kept] != before["conversation"]:
        raise ReleaseDrillError(
            f"the turns run while the online backup was taken changed the {kept} "
            f"message(s) the session held before them; the first difference is at "
            f"index {_first_difference(conversation, before['conversation'])}")
    boundaries, position = [kept], kept
    for index, turn in enumerate(turns):
        label = f"turn {turn['number']} of {count} run while the online backup was taken"
        if (position >= len(conversation) or _role(conversation[position]) != "user"
                or _text(conversation[position]) != turn["message"]):
            raise ReleaseDrillError(
                f"{label} was not committed after the {position} message(s) before it")
        following = turns[index + 1]["message"] if index + 1 < count else None
        end = position + 1
        while end < len(conversation) and not (
                _role(conversation[end]) == "user" and _text(conversation[end]) == following):
            end += 1
        if (end - position < 2 or _role(conversation[end - 1]) != "assistant"
                or _text(conversation[end - 1]) != turn["reply"]):
            raise ReleaseDrillError(
                f"{label} was not committed: its message is not followed by its reply")
        boundaries.append(end)
        position = end
    records = len(before["audit"])
    if audit[:records] != before["audit"]:
        raise ReleaseDrillError(
            f"the turns run while the online backup was taken changed the {records} "
            f"audit record(s) the chain held before them")
    if len(audit) < records + count:
        raise ReleaseDrillError(
            "a turn run while the online backup was taken left no audit record: the "
            f"chain grew from {records} to {len(audit)} record(s) over {count} turn(s)")
    return {
        "turns": count,
        "answered": count,
        "in_flight_when_backup_started": True,
        "overlapping_backup": sum(turn["started"] < load["exited"]
                                  and turn["ended"] > load["launched"] for turn in turns),
        "messages_before": kept,
        "messages_after": len(conversation),
        "audit_records_before": records,
        "audit_records_after": len(audit),
        "history_messages_sent": history[0],
        "provider_requests": len(requests),
    }, boundaries


def _turns_held(archive, *, before, after, boundaries):
    """How many of the turns run while the online backup was taken
    ``archive`` (the session as the backup holds it) holds; ReleaseDrillError
    unless it is the session at one of their ``boundaries`` (see _judge_load),
    after the first, which was in flight when the backup started and so had
    to be waited for, with the audit chain as it stood there.

    A turn commits its messages and its audit records under the session's
    lock, and the backup copies the session under that lock, so whatever it
    holds is the session as one turn left it. Each turn adds one record at
    least, which is what ties the chain to the conversation.
    """
    conversation = archive["conversation"]
    held = next((number for number, length in enumerate(boundaries)
                 if conversation == after["conversation"][:length]), None)
    if held is None:
        raise ReleaseDrillError(
            f"the online backup holds the session as no turn left it: its "
            f"{len(conversation)} message(s) are not the conversation as it stood "
            f"before the turns run while it was taken or after any of them")
    if held == 0:
        raise ReleaseDrillError(
            "the online backup does not hold the turn that was in flight when it "
            f"started: it holds the {len(conversation)} message(s) the session had "
            "before the turns")
    chain, whole = archive["audit"], after["audit"]
    least = len(before["audit"]) + held
    most = len(whole) - (len(boundaries) - 1 - held)
    if chain != whole[:len(chain)] or not least <= len(chain) <= most:
        raise ReleaseDrillError(
            f"the online backup holds an audit chain of {len(chain)} record(s), which "
            f"is not the chain as it stood when the session held its "
            f"{len(conversation)} message(s)")
    return held


def _online_capture(manifest, printed):
    """An online backup's capture report, from its manifest; ReleaseDrillError
    if the manifest is not an online one, does not record the longest time
    the backup held one session's lock, has no integer creation time, or is
    not the manifest its tool printed."""
    mode = manifest.get("mode")
    capture = manifest.get("online")
    if mode != "online" or not isinstance(capture, dict):
        raise ReleaseDrillError(
            f"the candidate's backup is not an online one: its manifest has mode {mode!r}")
    longest = capture.get("max_session_lock_hold_ms")
    if isinstance(longest, bool) or not isinstance(longest, (int, float)) or not longest >= 0:
        raise ReleaseDrillError(
            "the online backup does not record its longest session lock hold")
    if not isinstance(manifest.get("created_unix"), int):
        raise ReleaseDrillError("the online backup lacks an integer creation time")
    if printed != manifest:
        raise ReleaseDrillError(
            "the online backup's archive does not hold the manifest its tool printed")
    return capture


def _create_private(path, text):
    """Write ``text`` to a new file at ``path`` that only its owner can open
    from the moment it exists: it is created with mode 0600, and a path that
    already exists, a link included, is refused.

    Until audit 2026-11-10 (F-18) the drills wrote a credentials file and
    then changed its mode, and in between it had the umask's (0644 by
    default) while it held a chat credential in the customer's tenant.
    """
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)


def _write_auth(path, *, token, session_token, tenant, now):
    def credential(value, principal, owner, scopes):
        return {
            "sha256": hashlib.sha256(value.encode()).hexdigest(),
            "principal": principal,
            "tenant": owner,
            "scopes": scopes,
            "tools": [],
            "not_before_unix": int(now) - 60,
            "expires_unix": int(now) + 3600,
        }

    document = {"tokens": [
        credential(token, "release-drill-operator", OPERATOR_TENANT, ["ops:read"]),
        credential(session_token, "release-drill-session", tenant,
                   ["chat", "sessions:read"]),
    ]}
    _create_private(path, json.dumps(document, sort_keys=True, separators=(",", ":")))


def _write_report(output, report):
    output.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp creates a new file with mode 0600 under a name no one chose.
    descriptor, name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp",
                                        dir=output.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def run_drill(*, old_artifact, new_artifact, state_backup, audit_key_file,
              output, work_dir=None, max_backup_age_s=900,
              max_restore_s=4 * 3600, max_rollback_s=15 * 60,
              probe=None, now_fn=time.time,
              tenant=FIXTURE_TENANT, session=FIXTURE_SESSION):
    """``tenant`` and ``session`` name the conversation every phase reads and
    continues; the backup must hold it. Once the inputs are accepted, a drill
    that stops, whatever stops it, writes a report that says where and why,
    and then raises."""
    if min(max_backup_age_s, max_restore_s, max_rollback_s) <= 0:
        raise ReleaseDrillError("all drill thresholds must be positive")
    if not isinstance(tenant, str) or not tenant.strip() or tenant == OPERATOR_TENANT:
        raise ReleaseDrillError(
            "the session's tenant must be a non-empty name other than "
            f"{OPERATOR_TENANT!r}, the tenant of the drill's own credential")
    if not isinstance(session, str) or not _SESSION_NAME.fullmatch(session):
        raise ReleaseDrillError(
            "the session must be a name the product accepts: 1 to 128 ASCII "
            "letters, digits, '.', '_' or '-', starting with a letter or digit")
    # All resolved up front: _probe_release exec's the launcher with
    # cwd=release_root, so any relative path derived from work_dir would be
    # re-resolved against that cwd and disappear (see drill_fixture).
    output = Path(output).resolve()
    if output.exists() or output.is_symlink():
        raise ReleaseDrillError("refusing to overwrite an existing drill report")
    audit_key = _read_private_key(audit_key_file)
    state_backup = Path(state_backup).resolve()
    started_unix = now_fn()
    owned = work_dir is None
    root = (Path(tempfile.mkdtemp(prefix="sigil-pi-release-drill-")) if owned
            else Path(work_dir).resolve())
    if not owned:
        if root.exists():
            raise ReleaseDrillError("work directory must not already exist")
        root.mkdir(parents=True)
    actual_probe = probe is None
    probe_fn = probe or _probe_release
    provider = _Provider()
    # It looks for its shutdown request twenty times a second, so that the
    # drill does not end by waiting half a second for it.
    threading.Thread(target=provider.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    # What the report says, filled in as the drill goes, so that a drill
    # which stops can still say how far it got.
    phases, reported = [], {}
    known = {"old": None, "new": None, "backup": None, "continuity": None,
             "restore_seconds": None, "rollback_seconds": None, "online_backup": None,
             "state_migration": None}

    def report_of(mechanics_failures, qualification_failures):
        def artifact(installed):
            return installed and {"version": installed["version"],
                                  "sha256": installed["sha256"]}

        return {
            "schema_version": REPORT_SCHEMA,
            "test": "sigil-pi-v1-release-recovery",
            "started_unix": int(started_unix),
            "completed_unix": int(now_fn()),
            "qualification_eligible": not qualification_failures,
            "qualification_failures": qualification_failures,
            "artifacts": {"old": artifact(known["old"]), "new": artifact(known["new"])},
            "backup": known["backup"],
            "topology": {
                "workers": 1,
                "state_filesystem": "local-posix",
                "platform": platform.platform(),
                "python": platform.python_version(),
                "real_service_probe": actual_probe,
            },
            "thresholds": {
                "maximum_backup_age_seconds": max_backup_age_s,
                "maximum_restore_seconds": max_restore_s,
                "maximum_rollback_seconds": max_rollback_s,
            },
            "configuration": {
                **service_configuration(),
                "wiring": dict(WIRING),
                "provider": {
                    "kind": "stand-in",
                    "endpoint": provider.endpoint,
                    "description": "an Anthropic-shaped endpoint on loopback "
                                   "that answers every turn; no request "
                                   "leaves this host",
                },
                "session": {"tenant": tenant, "session": session},
                # As each release reports them in /v1/metrics; null for one
                # that reports none.
                "effective_settings": {"old": reported.get("old"),
                                       "new": reported.get("new")},
            },
            "phases": phases,
            # The two starts of the candidate that take and restore an online
            # backup, and what they showed (see the module's notes).
            "online_backup": known["online_backup"],
            # The upgrade and the rollback the release declares, and every
            # state command they ran (see the notes above MANIFEST_TAG_HEADER).
            "state_migration": known["state_migration"],
            "evaluation": {
                "mechanics_passed": not mechanics_failures,
                "failures": mechanics_failures,
                "restore_rto_seconds": known["restore_seconds"],
                "rollback_seconds": known["rollback_seconds"],
                "measured_to": MEASURED_TO,
                "continuity": known["continuity"],
            },
        }

    try:
        releases = root / "releases"
        known["old"] = old = _install_artifact(Path(old_artifact).resolve(), releases, "old")
        known["new"] = new = _install_artifact(Path(new_artifact).resolve(), releases, "new")
        current = root / "current"
        logs = root / "logs"
        logs.mkdir()
        token, session_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        auth_file = root / "auth.json"
        _write_auth(auth_file, token=token, session_token=session_token,
                    tenant=tenant, now=started_unix)

        # The backup is read before any release starts: every phase after the
        # clean install is judged against what it holds.
        backup_manifest, before_payloads = _backup_payloads(state_backup)
        created = backup_manifest.get("created_unix")
        if not isinstance(created, int):
            raise ReleaseDrillError("state backup lacks an integer creation time")
        backup_age = started_unix - created
        if backup_age < -60:
            raise ReleaseDrillError("state backup creation time is in the future")
        known["backup"] = {
            "sha256": _sha256(state_backup),
            "schema_version": backup_manifest["schema_version"],
            "product_version": backup_manifest.get("product_version"),
            "created_unix": created,
            "age_seconds": round(backup_age, 6),
        }
        conversation_file, chain_file = _session_files(tenant, session)
        committed = {"conversation": _messages(before_payloads.get(conversation_file)),
                     "audit": _records(before_payloads.get(chain_file))}
        if conversation_file not in before_payloads or None in committed.values():
            raise ReleaseDrillError(
                f"state backup holds no conversation for session {session!r} of "
                f"tenant {tenant!r}; name one it holds with --tenant and --session")

        def execute(name, installed, state, expected, *, record_in=phases, number=None,
                    switch=True, while_serving=None, judge=None):
            """One start of ``installed`` on ``state``, recorded in
            ``record_in``. The four phases select the release through
            ``current``; the online backup's two starts run the candidate
            from where it is installed, so ``current`` stays where the
            rollback left it. ``while_serving`` goes to the probe, and
            ``judge`` judges what it returned beyond the phase's turn."""
            record = {"name": name, "artifact_sha256": installed["sha256"],
                      "version": installed["version"]}
            message = f"Recovery drill, phase {name}: continue this conversation."
            first = len(provider.requests)
            phase_started = time.monotonic()
            try:
                if switch:
                    _switch_current(current, installed["root"])
                serving = {} if while_serving is None else {"while_serving": while_serving}
                result = probe_fn(
                    current.resolve() if switch else installed["root"], state, auth_file,
                    token, audit_key,
                    endpoint=provider.endpoint, session_token=session_token,
                    session=session, message=message,
                    log_path=logs / f"{number or len(record_in) + 1}-{name}-service.log",
                    **serving)
                if result.get("version") != installed["version"]:
                    raise ReleaseDrillError("the probe returned the wrong version")
                timings = {key: result.get(key) for key in (
                    "ready_seconds", "first_turn_seconds", "total_seconds")}
                if not all(isinstance(value, (int, float)) for value in timings.values()):
                    raise ReleaseDrillError("the probe did not time the phase")
                # The phase's own turn asked the provider before anything
                # run while the release still served.
                served = result.get("while_serving")
                asked = provider.requests[first:(
                    served["first_request"] if served else len(provider.requests))]
                turn = _judge_turn(
                    result, expected=expected, session=session, message=message,
                    requests=asked,
                    replies=[provider.reply(first + n) for n in range(len(asked))])
                if judge is not None:
                    judge(result)
            except (OSError, ReleaseDrillError) as error:
                record_in.append({**record, "failed": True, "error": f"{name}: {error}"})
                raise ReleaseDrillError(f"{name}: {error}") from error
            reported.setdefault("new" if installed is new else "old",
                                result.get("effective_settings"))
            record_in.append({
                **record, **timings,
                "elapsed_seconds": round(time.monotonic() - phase_started, 6),
                "turn": turn,
            })
            return result

        def seconds_to_first_turn(since, phase):
            """From ``since`` to the end of the phase's turn: the time to now,
            less what the phase did after its turn (it read the session again
            and stopped the release)."""
            return round(time.monotonic() - since
                         - (phase["total_seconds"] - phase["first_turn_seconds"]), 6)

        def stopped(name, installed, record_in, error):
            """A start that never began: the state work before it failed."""
            record_in.append({
                "name": name, "artifact_sha256": installed["sha256"],
                "version": installed["version"], "failed": True, "error": f"{name}: {error}"})
            return ReleaseDrillError(f"{name}: {error}")

        execute("clean_install", old, root / "clean-state", None)

        restored_state = root / "restored-state"
        restore_started = time.monotonic()
        _run_state_tool(
            old["root"], "restore", audit_key=audit_key,
            backup=state_backup, state=restored_state)
        restored = execute("restore_old", old, restored_state, committed)
        known["restore_seconds"] = restore_seconds = seconds_to_first_turn(
            restore_started, restored)

        # ── the upgrade and the rollback the release declares (see the
        # notes above MANIFEST_TAG_HEADER), each state command a step ──
        change = known["state_migration"] = {
            "declaration": None, "because": None, "formats": {"old": None, "new": None},
            "commands": None, "rollback_continues": None,
            "upgrade": [], "rollback": [], "online": []}

        def role(installed):
            return "new" if installed is new else "old"

        def step(stage, started, record):
            change[stage].append({**record, "seconds": round(time.monotonic() - started, 6)})
            return record

        def back_up(stage, installed, state, archive):
            """The release's own offline backup of ``state``. The archive's
            digest is recorded as it is written: its established origin."""
            started = time.monotonic()
            _run_state_tool(installed["root"], "backup", audit_key=audit_key,
                            backup=archive, state=state)
            return step(stage, started, {
                "command": "backup", "release": role(installed), "state": state.name,
                "backup": archive.name, "sha256": _sha256(archive),
                "manifest_tag": _manifest_tagged(archive)})

        def restore(stage, installed, written, state):
            """``installed``'s restore, into the new path ``state``, of an
            archive this drill wrote (``written``): checked against the digest
            recorded when it was written, and given the flag that skips
            manifest authentication only when it carries no tag."""
            archive = root / written["backup"]
            if _sha256(archive) != written["sha256"]:
                raise ReleaseDrillError(
                    f"{archive.name} is not the archive the {written['release']} release "
                    "wrote: its SHA-256 changed")
            offered = tools[role(installed)]["skip_manifest_authentication"]
            options = _restore_options(written["manifest_tag"], offered)
            started = time.monotonic()
            manifest = _run_state_tool(installed["root"], "restore", audit_key=audit_key,
                                       backup=archive, state=state, options=options)
            if not offered:
                authentication = "not checked: the release predates manifest authentication"
            elif options:
                authentication = manifest.get("manifest_authentication")
                if authentication != "skipped":
                    raise ReleaseDrillError(
                        f"the restore of {archive.name} did not report that it skipped "
                        "manifest authentication")
            else:
                authentication = "authenticated"
            return step(stage, started, {
                "command": "restore", "release": role(installed), "backup": archive.name,
                "sha256": written["sha256"], "manifest_tag": written["manifest_tag"],
                "origin": f"written in this drill by the {written['release']} release's "
                          f"state_tool.py {written['command']}; its SHA-256 recorded then "
                          "and checked before this restore",
                "options": options, "manifest_authentication": authentication,
                "state": state.name})

        def dry_run(stage, installed, state):
            started = time.monotonic()
            report = _run_state_tool(installed["root"], "migrate", audit_key=audit_key,
                                     backup=None, state=state, options=["--dry-run"])
            if not isinstance(report, dict) or report.get("dry_run") is not True:
                raise ReleaseDrillError(
                    f"the {role(installed)} release's migrate --dry-run reported no dry run")
            step(stage, started, {
                "command": "migrate --dry-run", "release": role(installed),
                "state": state.name, "has_work": _has_work(report), "report": report})
            return report

        def migrate(stage, state, source, backup):
            """The candidate's migration of ``state``, its restore of the
            archive ``source``, when the candidate has the command: the dry
            run, and the migration itself when the dry run names work, with
            ``backup`` for its pre-migration backup. Returns the dry run's
            report (None without the command), the pre-migration backup as
            written (None when nothing ran) and the moves made."""
            if not tools["new"]["migrate"]:
                return None, None, []
            planned = dry_run(stage, new, state)
            if not _has_work(planned):
                return planned, None, []
            # Never the only copy: the archive this state came from is intact.
            if _sha256(root / source["backup"]) != source["sha256"]:
                raise ReleaseDrillError(
                    f"{source['backup']} changed before the migration of its restore")
            started = time.monotonic()
            report = _run_state_tool(new["root"], "migrate", audit_key=audit_key,
                                     backup=backup, state=state)
            if (not isinstance(report, dict) or report.get("dry_run") is not False
                    or report.get("backup") != str(backup) or not backup.is_file()
                    or _sha256(backup) != report.get("backup_sha256")):
                raise ReleaseDrillError(
                    "the candidate's migration did not write the pre-migration backup "
                    "its report names")
            if stat.S_IMODE(backup.stat().st_mode) & 0o222:
                raise ReleaseDrillError("the candidate's pre-migration backup is writable")
            written = {"command": "migrate", "release": "new", "backup": backup.name,
                       "sha256": report["backup_sha256"],
                       "manifest_tag": _manifest_tagged(backup)}
            step(stage, started, {
                "command": "migrate", "release": "new", "state": state.name,
                "pre_migration_backup": written, "report": report})
            return planned, written, report.get("moves", [])

        upgrade_state = root / "upgrade-state"
        try:
            pre_upgrade = back_up("upgrade", old, restored_state,
                                  root / "pre-upgrade-state.tar.gz")
            # What each release's state tool can do, from its own --help.
            tools = {
                name: {"migrate": _state_tool_help(installed["root"], "migrate") is not None,
                       "skip_manifest_authentication": SKIP_AUTHENTICATION_FLAG in (
                           _state_tool_help(installed["root"], "restore") or "")}
                for name, installed in (("old", old), ("new", new))}
            change["commands"] = tools
            if tools["old"]["migrate"]:
                change["formats"]["old"] = _formats(dry_run("upgrade", old, restored_state))
            restore("upgrade", new, pre_upgrade, upgrade_state)
            planned, pre_migration, _ = migrate(
                "upgrade", upgrade_state, pre_upgrade, root / "pre-migration-state.tar.gz")
            if planned is not None:
                change["formats"]["new"] = _formats(planned)
            change["declaration"], change["because"] = _declaration(
                change["formats"]["old"], change["formats"]["new"], pre_migration is not None)
        except (OSError, ReleaseDrillError) as error:
            raise stopped("upgrade_new", new, phases, error) from error
        upgraded = execute("upgrade_new", new, upgrade_state, restored["after"])

        rollback_started = time.monotonic()
        required = change["declaration"] == MIGRATION_REQUIRED
        if required:
            # The old release never opens the candidate's state: it starts on
            # the state before the upgrade, restored into a new path by its
            # own tool, and what the candidate wrote is not carried back.
            source = pre_migration or pre_upgrade
            rollback_state = root / "rollback-state"
            try:
                restore("rollback", old, source, rollback_state)
            except (OSError, ReleaseDrillError) as error:
                raise stopped("rollback_old", old, phases, error) from error
            change["rollback_continues"] = (
                f"the state in {source['backup']}, restored into {rollback_state.name}: "
                "the candidate's turn is not carried back")
            rolled_back = execute("rollback_old", old, rollback_state, restored["after"])
        else:
            rollback_state = upgrade_state
            change["rollback_continues"] = f"the state the candidate left, {upgrade_state.name}"
            rolled_back = execute("rollback_old", old, rollback_state, upgraded["after"])
        known["rollback_seconds"] = rollback_seconds = seconds_to_first_turn(
            rollback_started, rolled_back)

        post_backup = root / "post-rollback-state.tar.gz"
        post = back_up("rollback", old, rollback_state, post_backup)
        _, after_payloads = _backup_payloads(post_backup)
        continuity = _continuity(before_payloads, after_payloads, tenant, session)
        continued = continuity["session"] = {"tenant": tenant, **continuity["session"]}
        known["continuity"] = continuity

        # ── the online backup, taken while turns run, and its restore ──
        online = known["online_backup"] = {
            "release": {"version": new["version"], "sha256": new["sha256"]},
            "command": ONLINE_COMMAND,
            "phases": [],
        }

        # The candidate restores the old release's backup of the state the
        # rollback left, and migrates it, as it did for the upgrade.
        serving_state = root / "online-state"
        try:
            restore("online", new, post, serving_state)
            _, online_migration, online_moves = migrate(
                "online", serving_state, post, root / "online-pre-migration-state.tar.gz")
        except (OSError, ReleaseDrillError) as error:
            raise stopped(ONLINE_PHASES[0], new, online["phases"], error) from error
        archive = root / "online-backup.tar.gz"
        backup_environment = dict(os.environ)
        backup_environment["PI_AUDIT_KEY"] = audit_key
        command = [sys.executable, str(new["root"] / "app" / "state_tool.py"), "backup",
                   "--online", "--state", str(serving_state), "--output", str(archive)]
        captured = {}

        def back_up(base, read_session):
            return _back_up_while_serving(
                base, read_session, session=session, session_token=session_token,
                provider=provider, prefix=f"Recovery drill, phase {ONLINE_PHASES[0]}",
                command=command, environment=backup_environment)

        def judge_backup(result):
            served = result["while_serving"]
            asked = provider.requests[served["first_request"]:]
            load, boundaries = _judge_load(
                served, before=result["after"], after=served["after"], requests=asked,
                replies=[provider.reply(served["first_request"] + n)
                         for n in range(len(asked))])
            manifest, payloads = _backup_payloads(archive)
            capture = _online_capture(manifest, served["printed"])
            held = {"conversation": _messages(payloads.get(conversation_file)),
                    "audit": _records(payloads.get(chain_file))}
            if conversation_file not in payloads or None in held.values():
                raise ReleaseDrillError(
                    f"the online backup holds no conversation for session {session!r}")
            load["held_by_backup"] = _turns_held(
                held, before=result["after"], after=served["after"], boundaries=boundaries)
            # Every committed file, under the name the migration gave it.
            kept = _continuity(_renamed(after_payloads, online_moves), payloads,
                               tenant, session)
            online.update({
                "sha256": _sha256(archive),
                "schema_version": manifest["schema_version"],
                "mode": manifest["mode"],
                "product_version": manifest.get("product_version"),
                "created_unix": manifest["created_unix"],
                "capture": capture,
                "max_session_lock_hold_ms": capture["max_session_lock_hold_ms"],
                "backup_seconds": round(served["exited"] - served["launched"], 6),
                "turns_while_backing_up": load,
                "session_in_backup": {"messages": len(held["conversation"]),
                                      "audit_records": len(held["audit"])},
                "continuity": {key: kept[key] for key in (
                    "committed_customer_files", "lost_or_changed", "preserved")},
            })
            captured["session"] = held

        # The candidate continues the session the rollback left, as it was
        # backed up after the rollback, and serves turns in it while its own
        # tool backs it up.
        execute(ONLINE_PHASES[0], new, serving_state,
                {"conversation": _messages(after_payloads.get(conversation_file)) or [],
                 "audit": _records(after_payloads.get(chain_file)) or []},
                record_in=online["phases"], number=len(phases) + 1, switch=False,
                while_serving=back_up, judge=judge_backup)

        # Its age counts from its start (the manifest's created_unix) to the
        # start of its restore, on the host's clock, which stamped both.
        restored_online = root / "online-restored-state"
        online_restore_started = time.monotonic()
        began = time.time()
        online["restore_started_unix"] = round(began, 6)
        online["age_at_restore_seconds"] = round(began - online["created_unix"], 6)
        try:
            _run_state_tool(new["root"], "restore", audit_key=audit_key,
                            backup=archive, state=restored_online)
        except ReleaseDrillError as error:
            raise stopped(ONLINE_PHASES[1], new, online["phases"], error) from error
        execute(ONLINE_PHASES[1], new, restored_online, captured["session"],
                record_in=online["phases"], number=len(phases) + 2, switch=False)
        online["restore_rto_seconds"] = seconds_to_first_turn(
            online_restore_started, online["phases"][-1])
        online["measured_to"] = MEASURED_TO

        mechanics_failures = []
        if not continuity["preserved"]:
            mechanics_failures.append("committed customer state changed or disappeared")
        # Each phase after the restore added a message, a reply and at least
        # one audit record to the session the rollback left: three, or two
        # when the rollback restored the state from before the upgrade, which
        # does not hold the candidate's turn.
        turns = 2 if required else 3
        if (continued["messages_after"] < continued["committed_messages"] + 2 * turns
                or continued["audit_records_after"]
                < continued["committed_audit_records"] + turns):
            mechanics_failures.append(
                "the state backed up after the rollback does not hold the "
                "turns the phases ran")
        if online_migration is not None and not required:
            mechanics_failures.append(
                "the candidate migrated the state the old release left after the "
                f"rollback, so the release cannot declare {SCHEMA_UNCHANGED!r}")
        if restore_seconds > max_restore_s:
            mechanics_failures.append("restore/RTO exceeded the configured threshold")
        if rollback_seconds > max_rollback_s:
            mechanics_failures.append("rollback exceeded the configured threshold")
        if not online["continuity"]["preserved"]:
            mechanics_failures.append(
                "committed customer state changed or disappeared in the online backup")
        if online["restore_rto_seconds"] > max_restore_s:
            mechanics_failures.append(
                "restore from the online backup exceeded the configured RTO threshold")
        qualification_failures = list(mechanics_failures)
        if not actual_probe:
            qualification_failures.append("probe was injected rather than the real service")
        if old["sha256"] == new["sha256"] or old["version"] == new["version"]:
            qualification_failures.append("old and new releases are not distinct versions")
        if backup_age < 0 or backup_age > max_backup_age_s:
            qualification_failures.append("input backup does not demonstrate the RPO threshold")
        # The 15-minute objective rests on online backups (audit F-03).
        online_age = online["age_at_restore_seconds"]
        if not 0 <= online_age <= max_backup_age_s:
            qualification_failures.append(
                "the online backup does not demonstrate the RPO threshold: its "
                f"restore began {online_age} s after it was taken")
        if continuity["committed_session_files"] == 0:
            qualification_failures.append("backup contains no committed session anti-vacuity fixture")
        if not continued["held_committed_content"]:
            qualification_failures.append(
                "the session the phases read held no committed conversation and "
                "audit chain, so reading it proves nothing")
        qualification_failures.extend(_configuration_failures(reported))
        report = report_of(mechanics_failures, qualification_failures)
        _write_report(output, report)
        return report
    except BaseException as error:
        # Whatever stopped it. Until audit 2026-11-10 (F-18) only an OSError
        # or the drill's own error left a report.
        if not output.exists():
            reason = (str(error) if isinstance(error, (OSError, ReleaseDrillError))
                      else f"{type(error).__name__}: {error}")
            try:
                _write_report(output, report_of(
                    [reason], [f"the drill did not complete: {reason}"]))
            except Exception:
                pass  # the error that stopped the drill is the one to raise
        raise
    finally:
        provider.shutdown()
        provider.server_close()
        if owned:
            shutil.rmtree(root, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--old-artifact", required=True)
    parser.add_argument("--new-artifact", required=True)
    parser.add_argument("--state-backup", required=True)
    parser.add_argument("--audit-key-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--work-dir")
    parser.add_argument(
        "--tenant", default=FIXTURE_TENANT,
        help="the tenant whose session every phase reads and continues "
             "(default: the one scripts/drill_fixture.py commits its turn as)")
    parser.add_argument(
        "--session", default=FIXTURE_SESSION,
        help="that tenant's session; the backup must hold its conversation")
    parser.add_argument("--max-backup-age-seconds", type=float, default=900)
    parser.add_argument("--max-restore-seconds", type=float, default=4 * 3600)
    parser.add_argument("--max-rollback-seconds", type=float, default=15 * 60)
    parser.add_argument(
        "--mechanics-only", action="store_true",
        help="write a non-qualifying report instead of failing when artifacts/backup are fixtures")
    args = parser.parse_args()
    try:
        report = run_drill(
            old_artifact=args.old_artifact,
            new_artifact=args.new_artifact,
            state_backup=args.state_backup,
            audit_key_file=args.audit_key_file,
            output=args.output,
            work_dir=args.work_dir,
            max_backup_age_s=args.max_backup_age_seconds,
            max_restore_s=args.max_restore_seconds,
            max_rollback_s=args.max_rollback_seconds,
            tenant=args.tenant,
            session=args.session)
    except (OSError, ReleaseDrillError) as error:
        parser.exit(2, f"release drill failed: {error}\n")
    except Exception as error:
        # Exit 1 means a drill that completed and is not eligible; a drill
        # that stopped on an error nobody foresaw did not complete.
        traceback.print_exc()
        parser.exit(2, f"release drill failed: {type(error).__name__}: {error}\n")
    print(json.dumps(report, sort_keys=True))
    if not report["qualification_eligible"] and not args.mechanics_only:
        parser.exit(1, "release drill completed but is not qualification-eligible\n")


if __name__ == "__main__":
    main()
