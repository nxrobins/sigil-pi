#!/usr/bin/env python3
"""Run a digest-bound clean-install, restore, upgrade, and rollback drill.

Two release archives are verified and installed, a state backup is restored
with the old release's own tool, and four phases follow in order: the old
release on a clean state, the old release on the restored state, the
candidate on it, and the old release on it again.

EVERY PHASE RUNS A TURN. A release that starts has shown that it starts. So
each phase waits for readiness, requires an answer from `/v1/version` and
`/v1/metrics`, reads one session through the release (its conversation and
its audit chain must be exactly what was committed, and then what the phases
before it left), runs one turn in that session against a stand-in provider on
loopback, and reads the session again (the turn must have been committed
after what was there). The candidate therefore continues a conversation the
old release wrote, and the rollback continues one the candidate wrote. Any
answer other than the expected one fails the phase, and the drill stops there.

RECOVERY IS TIMED TO THE FIRST SUCCESSFUL TURN, from the start of the restore
and from the start of the rollback.

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
import urllib.error
import urllib.request
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
DEFAULT_PROBE_TIMEOUT = 60

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
                "for the three phases after it",
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
    except (OSError, tarfile.TarError) as error:
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
            if manifest.get("schema_version") != 1:
                raise ReleaseDrillError("state backup schema is not supported by this drill")
            entries = manifest.get("files")
            if not isinstance(entries, list):
                raise ReleaseDrillError("state backup manifest file list is invalid")
            payloads = {}
            for entry in entries:
                if not isinstance(entry, dict):
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
    except (OSError, tarfile.TarError, json.JSONDecodeError) as error:
        raise ReleaseDrillError(f"cannot read state backup: {error}") from error


def _run_state_tool(release_root, command, *, audit_key, backup, state):
    script = Path(release_root) / "app" / "state_tool.py"
    args = [sys.executable, str(script), command]
    if command == "restore":
        args.extend(["--backup", str(backup), "--state", str(state)])
    else:
        args.extend(["--state", str(state), "--output", str(backup)])
    environment = dict(os.environ)
    environment["PI_AUDIT_KEY"] = audit_key
    result = subprocess.run(
        args, cwd=script.parent, env=environment, capture_output=True, text=True)
    if result.returncode:
        diagnostic = (result.stderr or result.stdout).strip()[-2000:]
        raise ReleaseDrillError(f"state {command} failed: {diagnostic}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ReleaseDrillError(f"state {command} returned invalid evidence") from error


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
        try:
            messages = json.loads(raw).get("messages")
        except (ValueError, AttributeError):
            messages = None
        with self.server.lock:
            number = len(self.server.requests)
            self.server.requests.append(messages if isinstance(messages, list) else None)
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

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.server_address[1]}/v1/messages"

    @staticmethod
    def reply(number):
        return f"The recovery drill's stand-in provider, reply {number + 1}."


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
                   session, message, log_path=None):
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
    path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")))
    path.chmod(0o600)


def _write_report(output, report):
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")
        temporary.chmod(0o600)
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
    that stops writes a report that says where and why, and then raises."""
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
             "restore_seconds": None, "rollback_seconds": None}

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

        def execute(name, installed, state, expected):
            record = {"name": name, "artifact_sha256": installed["sha256"],
                      "version": installed["version"]}
            message = f"Recovery drill, phase {name}: continue this conversation."
            first = len(provider.requests)
            phase_started = time.monotonic()
            try:
                _switch_current(current, installed["root"])
                result = probe_fn(
                    current.resolve(), state, auth_file, token, audit_key,
                    endpoint=provider.endpoint, session_token=session_token,
                    session=session, message=message,
                    log_path=logs / f"{len(phases) + 1}-{name}-service.log")
                if result.get("version") != installed["version"]:
                    raise ReleaseDrillError("the probe returned the wrong version")
                timings = {key: result.get(key) for key in (
                    "ready_seconds", "first_turn_seconds", "total_seconds")}
                if not all(isinstance(value, (int, float)) for value in timings.values()):
                    raise ReleaseDrillError("the probe did not time the phase")
                asked = provider.requests[first:]
                turn = _judge_turn(
                    result, expected=expected, session=session, message=message,
                    requests=asked,
                    replies=[provider.reply(first + n) for n in range(len(asked))])
            except (OSError, ReleaseDrillError) as error:
                phases.append({**record, "failed": True, "error": f"{name}: {error}"})
                raise ReleaseDrillError(f"{name}: {error}") from error
            reported.setdefault("new" if installed is new else "old",
                                result.get("effective_settings"))
            phases.append({
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

        execute("clean_install", old, root / "clean-state", None)

        restored_state = root / "restored-state"
        restore_started = time.monotonic()
        _run_state_tool(
            old["root"], "restore", audit_key=audit_key,
            backup=state_backup, state=restored_state)
        restored = execute("restore_old", old, restored_state, committed)
        known["restore_seconds"] = restore_seconds = seconds_to_first_turn(
            restore_started, restored)
        upgraded = execute("upgrade_new", new, restored_state, restored["after"])
        rollback_started = time.monotonic()
        rolled_back = execute("rollback_old", old, restored_state, upgraded["after"])
        known["rollback_seconds"] = rollback_seconds = seconds_to_first_turn(
            rollback_started, rolled_back)

        post_backup = root / "post-rollback-state.tar.gz"
        _run_state_tool(
            old["root"], "backup", audit_key=audit_key,
            backup=post_backup, state=restored_state)
        _, after_payloads = _backup_payloads(post_backup)
        continuity = _continuity(before_payloads, after_payloads, tenant, session)
        continued = continuity["session"] = {"tenant": tenant, **continuity["session"]}
        known["continuity"] = continuity
        mechanics_failures = []
        if not continuity["preserved"]:
            mechanics_failures.append("committed customer state changed or disappeared")
        # Three phases ran on the restored state; each added a message, a
        # reply and at least one audit record to the session.
        if (continued["messages_after"] < continued["committed_messages"] + 6
                or continued["audit_records_after"] < continued["committed_audit_records"] + 3):
            mechanics_failures.append(
                "the state backed up after the rollback does not hold the "
                "turns the phases ran")
        if restore_seconds > max_restore_s:
            mechanics_failures.append("restore/RTO exceeded the configured threshold")
        if rollback_seconds > max_rollback_s:
            mechanics_failures.append("rollback exceeded the configured threshold")
        qualification_failures = list(mechanics_failures)
        if not actual_probe:
            qualification_failures.append("probe was injected rather than the real service")
        if old["sha256"] == new["sha256"] or old["version"] == new["version"]:
            qualification_failures.append("old and new releases are not distinct versions")
        if backup_age < 0 or backup_age > max_backup_age_s:
            qualification_failures.append("input backup does not demonstrate the RPO threshold")
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
    except (OSError, ReleaseDrillError) as error:
        if not output.exists():
            try:
                _write_report(output, report_of(
                    [str(error)], [f"the drill did not complete: {error}"]))
            except OSError:
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
    print(json.dumps(report, sort_keys=True))
    if not report["qualification_eligible"] and not args.mechanics_only:
        parser.exit(1, "release drill completed but is not qualification-eligible\n")


if __name__ == "__main__":
    main()
