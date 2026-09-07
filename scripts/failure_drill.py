#!/usr/bin/env python3
"""Inject the six required failure classes into a real release and report.

``docs/product-readiness.md`` areas 3 and 4 both end at the same missing
artifact: a digest-bound report showing every one of six failure categories
driven against the exact candidate on the final topology, with committed state
intact and the service recovered afterwards. ``config/failure-injection-matrix.json``
already binds those six categories to collected regression tests — but a unit
test proves the code path, not the deployed bundle. This drill is the other
half: it installs the published archive, boots the release's own service, and
breaks it six ways.

WHAT MAKES A CATEGORY PASS, and why all three parts are required:

* ``service_recovered`` — after the injection the release serves again. A
  failure the host never comes back from is an outage, not a handled failure.
* ``state_integrity_verified`` — every committed customer file (sessions,
  sandboxes, audit chains) is byte-identical to a backup taken BEFORE the
  injection. Taken with the release's own ``state_tool.py``, which refuses to
  run without a clean-shutdown marker and verifies the signed audit chains, so
  a corrupted chain cannot be backed up and quietly compared against itself.
* ``passed`` — the release also behaved the way its own documentation says it
  does (a stable error, a fail-closed refusal, a replaced runtime), not merely
  that it survived.

ANTI-VACUITY. Each category runs on its own fresh state directory and commits
a real turn through the release's forge path BEFORE anything is injected, so
there is always committed state that could be lost. A category whose baseline
committed nothing is reported as failing rather than trivially passing, and a
run whose service was injected by a test can never be qualification-eligible.

The report this writes is the input to
``scripts/check_readiness_evidence.py``; the drill deliberately owns no opinion
about whether the evidence is adequate, only about whether it is true.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import http.client
import json
import os
import platform
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MINIMUM_PYTHON = (3, 12)
if sys.version_info < MINIMUM_PYTHON:
    raise SystemExit(
        f"sigil-pi requires Python {'.'.join(map(str, MINIMUM_PYTHON))} or newer; "
        f"this is {'.'.join(map(str, sys.version_info[:3]))}. See "
        f"docs/support-matrix.md — older interpreters are outside the supported set.")

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR.parent))

from scripts.release_drill import (  # noqa: E402
    DEFAULT_PROBE_TIMEOUT,
    ReleaseDrillError,
    _backup_payloads,
    _customer_continuity,
    _install_artifact,
    _read_private_key,
)

REPORT_SCHEMA = 1

# The exact set docs/product-readiness.md requires and
# check_readiness_evidence.FAILURE_CATEGORIES enforces. A guard test pins the
# two together: renaming one without the other would silently stop producing
# evidence for a required category while still reporting six passes.
CATEGORIES = (
    "runtime_crashes",
    "unavailable_model_providers",
    "full_disks",
    "corrupt_state",
    "network_failures",
    "interrupted_writes",
)

# Past this, a filesystem is not bounded in any way this drill can exhaust, so
# a "full disk" injection against it would be a claim about nothing.
MAX_FILESYSTEM_MB = 256
MAX_BALLAST_BYTES = MAX_FILESYSTEM_MB * 1024 * 1024
BALLAST_CHUNK = 4 * 1024 * 1024


class FailureDrillError(RuntimeError):
    pass


def _verify_bounded_mount(path):
    """Prove this is a small tmpfs mount BEFORE writing any ballast."""
    path = Path(path)
    if sys.platform != "linux" or path.is_symlink() or not os.path.ismount(path):
        raise FailureDrillError("refusing to fill an unverified filesystem")
    result = subprocess.run(
        ["findmnt", "--json", "--mountpoint", str(path), "--output", "TARGET,FSTYPE"],
        capture_output=True, text=True, check=True)
    mounts = json.loads(result.stdout).get("filesystems", [])
    if (len(mounts) != 1 or mounts[0].get("fstype") != "tmpfs"
            or mounts[0].get("target") != str(path)):
        raise FailureDrillError("refusing to fill anything except the drill's tmpfs")
    stats = os.statvfs(path)
    size = stats.f_blocks * stats.f_frsize
    if not 0 < size <= MAX_BALLAST_BYTES:
        raise FailureDrillError("tmpfs exceeds the safe drill size limit")
    return size


class _WriteBoundary:
    """Observe a real Linux rename boundary without modifying the candidate."""

    def __init__(self, directory, target):
        if sys.platform != "linux" or not shutil.which("cc"):
            raise FailureDrillError("write-boundary injection requires Linux and cc")
        directory = Path(directory)
        self.target = Path(target)
        self.temporary = self.target.with_suffix(".kv.tmp")
        self.marker = directory / "rename-stopped.pid"
        self.library = directory / "failure-drill-rename.so"
        subprocess.run([
            "cc", "-shared", "-fPIC", "-Wall", "-Werror", "-o", str(self.library),
            str(SCRIPTS_DIR / "failure_drill_rename.c"), "-ldl"],
            check=True, capture_output=True, text=True)

    def environment(self):
        return {"LD_PRELOAD": str(self.library),
                "SIGIL_DRILL_RENAME_TARGET": str(self.target),
                "SIGIL_DRILL_RENAME_MARKER": str(self.marker)}

    def wait(self, process, timeout=40):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            if self.marker.exists():
                pid = self.marker.read_text().strip()
                status = Path(f"/proc/{process.pid}/status").read_text()
                stopped = any(line.startswith("State:\tT") for line in status.splitlines())
                if pid == str(process.pid) and stopped:
                    if (self.target.exists() or not self.temporary.is_file()
                            or not self.temporary.stat().st_size):
                        raise FailureDrillError("rename stopped without an uncommitted write")
                    return {"host_pid": process.pid, "boundary_observed": True,
                            "temporary_bytes": self.temporary.stat().st_size,
                            "temporary_sha256": hashlib.sha256(
                                self.temporary.read_bytes()).hexdigest()}
            time.sleep(0.02)
        raise FailureDrillError("no stopped host at the session rename boundary was observed")


# ── the switchable provider ─────────────────────────────────────────────────

class _ProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        return

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        mode = self.server.mode
        self.server.requests += 1
        if mode == "reset":
            # A connection accepted and then dropped with no response at all:
            # a transport fault, which is a different class from a provider
            # that is up and answering with an error.
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        if mode == "unavailable":
            body = json.dumps({"type": "error", "error": {
                "type": "overloaded_error", "message": "provider unavailable"}}).encode()
            self.send_response(503)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps({
            "id": "msg_failure_drill", "type": "message", "role": "assistant",
            "model": "failure-drill-mock",
            "content": [{"type": "text", "text": "failure drill acknowledgement"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Provider(ThreadingHTTPServer):
    """An Anthropic-shaped endpoint whose behavior the drill can change.

    Faking the provider keeps the drill hermetic while leaving everything the
    drill actually measures real: the release's own forge path, state writes,
    signed audit chain and error mapping all run unchanged regardless of who
    answers the completion call at the end of them.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _ProviderHandler)
        self.mode = "healthy"
        self.requests = 0

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.server_address[1]}/v1/messages"


# ── the real service under test ─────────────────────────────────────────────

def _free_loopback_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _request(method, url, token, payload=None, timeout=5):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Request-ID": "failure-drill",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.load(error)
        except (ValueError, json.JSONDecodeError):
            return error.code, {}


class _Service:
    """One installed release, booted the way a production host boots it."""

    def __init__(self, release_root, state_dir, *, auth_file, token, audit_key,
                 endpoint, label, timeout_s=DEFAULT_PROBE_TIMEOUT):
        self.release_root = Path(release_root)
        self.state_dir = Path(state_dir)
        self.auth_file = Path(auth_file)
        self.token = token
        self.audit_key = audit_key
        self.endpoint = endpoint
        self.label = label
        self.timeout_s = timeout_s
        self.port = None
        self.process = None
        self.write_boundary = None
        # Observe state-disk exhaustion without also exhausting the observer's
        # log file (stdout would otherwise fail for reasons outside the host).
        self.log_path = self.release_root.parent / f"{label}-service.log"

    # ── lifecycle ────────────────────────────────────────────────────────
    def _environment(self):
        environment = {
            key: value for key, value in os.environ.items()
            if key in {"PATH", "LANG", "LC_ALL", "TMPDIR"}
        }
        environment.update({
            "ANTHROPIC_API_KEY": "failure-drill-provider-placeholder",
            "PI_ENDPOINT": self.endpoint,
            "PI_AUDIT_KEY": self.audit_key,
            "PI_AUTH_FILE": str(self.auth_file),
            "PI_STATE": str(self.state_dir),
            "PI_HOST": "127.0.0.1",
            "PI_PORT": str(self.port),
            "PI_DRAIN_TIMEOUT_SECONDS": "30",
            "PI_TURN_DEADLINE_SECONDS": "30",
            "PI_TURN_LEASE_SECONDS": "60",
            "PI_MCP_TIMEOUT_SECONDS": "30",
            "PI_AUDIT_VERIFY_SECONDS": "3600",
            "PI_RETENTION_DAYS": "365000",
            "PI_RETENTION_INTERVAL_SECONDS": "3600",
            "PI_LLM_RETRIES": "1",
            "PI_SYSTEM_FILE": str(self.state_dir.parent / "no-system-prompt"),
        })
        if self.write_boundary is not None:
            environment.update(self.write_boundary.environment())
        return environment

    def prepare_write_interruption(self, session):
        # Same public tenant/session addressing contract as the installed host.
        tenant_hash = hashlib.sha256(b"failure-drill-tenant").hexdigest()
        session_hash = hashlib.sha256(session.encode()).hexdigest()
        internal = f"v1:{tenant_hash}:{session_hash}"
        filename = hashlib.sha256(internal.encode()).hexdigest() + ".kv"
        self.write_boundary = _WriteBoundary(
            self.state_dir.parent, self.state_dir / "sessions" / filename)

    def wait_write_interruption(self):
        return self.write_boundary.wait(self.process)

    def interrupted_write_was_adopted(self):
        return self.write_boundary.target.exists()

    def _spawn(self):
        self.port = _free_loopback_port()
        log = self.log_path.open("a+")
        try:
            self.process = subprocess.Popen(
                [str(self.release_root / "bin" / "sigil-pi")],
                cwd=self.release_root, env=self._environment(),
                stdout=log, stderr=log, text=True, start_new_session=True)
        finally:
            log.close()

    def _log_tail(self, limit=3000):
        try:
            return self.log_path.read_text()[-limit:]
        except OSError:
            return ""

    def start(self):
        ok, detail = self.try_start()
        if not ok:
            raise FailureDrillError(f"{self.label} service never became ready: {detail}")
        return detail

    def try_start(self):
        """Boot and wait for readiness; return (ready, detail) rather than raise.

        Fail-closed startup is an expected OUTCOME for one of the six
        categories, so refusing to boot must be observable, not exceptional.
        """
        self._spawn()
        deadline = time.monotonic() + self.timeout_s
        base = f"http://127.0.0.1:{self.port}"
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                return False, self._log_tail()
            try:
                status, body = _request("GET", f"{base}/v1/ready", self.token, timeout=2)
                if status == 200 and body.get("status") == "ready":
                    return True, ""
            except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
                pass
            time.sleep(0.05)
        return False, self._log_tail()

    def readiness(self):
        return _request("GET", f"http://127.0.0.1:{self.port}/v1/ready",
                        self.token, timeout=5)

    def chat(self, session, message, timeout=45):
        return _request(
            "POST", f"http://127.0.0.1:{self.port}/v1/chat", self.token,
            payload={"session": session, "message": message}, timeout=timeout)

    def runtime_pids(self):
        """PIDs of the forge children this host owns."""
        if self.process is None or self.process.poll() is not None:
            return []
        result = subprocess.run(
            ["pgrep", "-P", str(self.process.pid)],
            capture_output=True, text=True)
        return [int(line) for line in result.stdout.split() if line.isdigit()]

    def kill_runtime(self):
        """SIGKILL the forge children, leaving the host process alive."""
        killed = []
        for pid in self.runtime_pids():
            try:
                os.kill(pid, signal.SIGKILL)
                killed.append(pid)
            except OSError:
                pass
        return killed

    def kill(self):
        """SIGKILL the host itself — no drain, no clean-shutdown marker."""
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=10)

    def drain(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=35)
            except subprocess.TimeoutExpired:
                self.kill()
        if self.process.returncode != 0:
            raise FailureDrillError(
                f"{self.label} service did not drain cleanly: {self._log_tail()}")

    # ── the state filesystem ─────────────────────────────────────────────
    def exhaust_state_filesystem(self):
        """Fill the state filesystem until a real write fails with ENOSPC.

        Both the mount boundary and ENOSPC must be observed: absence of mount
        privilege must never fall back to filling the developer's host disk.
        """
        _verify_bounded_mount(self.state_dir.parent)
        if self.state_dir.is_symlink():
            raise FailureDrillError("state directory must not be a symlink")
        ballast = self.state_dir / ".failure-drill-ballast"
        written = 0
        chunk = b"\0" * BALLAST_CHUNK
        try:
            with ballast.open("xb") as sink:
                while written <= MAX_BALLAST_BYTES:
                    sink.write(chunk)
                    sink.flush()
                    os.fsync(sink.fileno())
                    written += len(chunk)
        except OSError as error:
            if error.errno != errno.ENOSPC:
                raise FailureDrillError(
                    f"filling the state filesystem failed unexpectedly: {error}") from error
            return f"{written} bytes of ballast until errno {error.errno}"
        ballast.unlink(missing_ok=True)
        raise FailureDrillError(
            "the state filesystem absorbed "
            f"{MAX_BALLAST_BYTES} bytes without filling; it is not bounded, so "
            "no genuine full-disk failure could be injected")

    def relieve_state_filesystem(self):
        (self.state_dir / ".failure-drill-ballast").unlink(missing_ok=True)


# ── a bounded filesystem for the full-disk category ─────────────────────────

class _BoundedFilesystem:
    """A small real filesystem, so ENOSPC can be injected without faking it.

    Only Linux is implemented, deliberately: the published artifacts are
    linux-x86_64 and their embedded runtime cannot exec elsewhere, so the
    qualifying drill runs on a Linux host or not at all. Anywhere else this
    reports itself unavailable and the drill records a qualification failure
    rather than substituting a weaker injection.
    """

    def __init__(self, path, size_mb=64, *, enabled=True):
        self.path = Path(path)
        self.size_mb = size_mb
        self.enabled = enabled
        self.kind = None
        self.detail = "not attempted"

    def __enter__(self):
        if type(self.size_mb) is not int or not 8 <= self.size_mb <= MAX_FILESYSTEM_MB:
            raise FailureDrillError(f"filesystem size must be 8..{MAX_FILESYSTEM_MB} MiB")
        self.path.mkdir(parents=True, exist_ok=False)
        if (not self.enabled or sys.platform != "linux"
                or not all(shutil.which(tool) for tool in ("mount", "findmnt", "sudo"))):
            self.detail = f"unsupported platform: {sys.platform}"
            return self
        # uid/gid, because a tmpfs mounted by root is root-owned mode 755 and
        # the unprivileged service could not write a single byte into it — the
        # category would die creating its state directory rather than filling
        # the filesystem, which is a different failure wearing the same name.
        options = (f"size={self.size_mb}m,uid={os.getuid()},gid={os.getgid()}")
        result = subprocess.run(
            ["sudo", "-n", "mount", "-t", "tmpfs", "-o", options,
             "tmpfs", str(self.path)],
            capture_output=True, text=True)
        if result.returncode == 0:
            self.kind = "tmpfs"
            self.detail = f"tmpfs {self.size_mb}m"
        else:
            self.detail = (result.stderr or result.stdout).strip()[-200:]
        return self

    def __exit__(self, *_):
        if self.kind == "tmpfs":
            result = subprocess.run(["sudo", "-n", "umount", str(self.path)],
                                    capture_output=True, text=True)
            if result.returncode:
                raise FailureDrillError(f"could not unmount disposable filesystem: {self.path}")
        return False


# ── state integrity ─────────────────────────────────────────────────────────

def _state_backup(release_root, state_dir, output, audit_key):
    """Back up with the RELEASE's own state_tool, not this checkout's."""
    script = Path(release_root) / "app" / "state_tool.py"
    environment = dict(os.environ)
    environment["PI_AUDIT_KEY"] = audit_key
    result = subprocess.run(
        [sys.executable, str(script), "backup",
         "--state", str(state_dir), "--output", str(output)],
        cwd=script.parent, env=environment, capture_output=True, text=True)
    if result.returncode:
        diagnostic = (result.stderr or result.stdout).strip()[-2000:]
        raise FailureDrillError(f"state backup failed: {diagnostic}")
    return output


def _write_auth(path, token, now):
    document = {"tokens": [{
        "sha256": hashlib.sha256(token.encode()).hexdigest(),
        "principal": "failure-drill-operator",
        "tenant": "failure-drill-tenant",
        "scopes": ["chat", "ops:read"],
        "tools": [],
        "not_before_unix": int(now) - 60,
        "expires_unix": int(now) + 3600,
    }]}
    path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")))
    path.chmod(0o600)


# ── the six injections ──────────────────────────────────────────────────────
#
# Each returns the observation for one category. The surrounding harness owns
# the baseline turn, the before/after backups and the continuity comparison,
# so an injector only has to break something and say what it saw.

def _inject_runtime_crashes(ctx):
    service = ctx.boot()
    killed = service.kill_runtime()
    status, body = service.chat("runtime-crash-recovery", "after the runtime died")
    ready_status, _ = service.readiness()
    replacements = service.runtime_pids()
    service.drain()
    failures = []
    if not killed:
        failures.append("no forge child was found to kill; nothing was injected")
    if not replacements or set(replacements) & set(killed):
        failures.append("no distinct replacement forge child was observed")
    if status != 200:
        failures.append(f"the turn after the crash did not succeed (status {status})")
    if ready_status != 200:
        failures.append("the host did not report ready after the runtime was killed")
    return {
        "injection": "SIGKILL every forge child of the running host, mid-service",
        "expected_behavior": (
            "the host survives, retires the killed compiler and replaces it on the "
            "next forge, so the following turn succeeds and the subsequent "
            "readiness probe passes"),
        "observed_behavior": (
            f"killed forge children {killed or 'none'}; the next turn returned "
            f"{status} and /v1/ready returned {ready_status}"),
        "service_recovered": ready_status == 200 and status == 200,
        "failures": failures,
        "detail": {"killed_pids": killed, "replacement_pids": replacements, "turn_status": status,
                   "reply": bool(body.get("reply"))},
    }


def _inject_unavailable_model_providers(ctx):
    service = ctx.boot()
    ctx.provider.mode = "unavailable"
    before_requests = ctx.provider.requests
    status, body = service.chat("provider-outage", "while the provider is down")
    fault_requests = ctx.provider.requests - before_requests
    ready_status, _ = service.readiness()
    ctx.provider.mode = "healthy"
    recovered_status, _ = service.chat("provider-outage", "after the provider returns")
    service.drain()
    code = (body.get("error") or {}).get("code")
    message = (body.get("error") or {}).get("message", "")
    failures = []
    if fault_requests < 1:
        failures.append("the unavailable provider received no request; no fault was exercised")
    if status != 502 or code != "agent_failure":
        failures.append(
            f"a provider outage did not map to the stable 502 agent_failure "
            f"(got {status} {code})")
    if message != "agent turn failed":
        failures.append("the stable error leaked provider diagnostics")
    if ready_status != 200:
        failures.append("readiness dropped for a provider outage the host should absorb")
    if recovered_status != 200:
        failures.append("the host did not serve again once the provider returned")
    return {
        "injection": (
            "the configured completion endpoint answers every request with 503 "
            "overloaded_error for the duration of one turn"),
        "expected_behavior": (
            "the turn fails with the stable 502 agent_failure carrying no provider "
            "diagnostics, readiness stays up because the dependency is external, "
            "and the next turn succeeds once the "
            "provider returns"),
        "observed_behavior": (
            f"the turn returned {status} {code!r}; /v1/ready returned {ready_status}; "
            f"the turn after recovery returned {recovered_status}"),
        "service_recovered": ready_status == 200 and recovered_status == 200,
        "failures": failures,
        "detail": {"outage_status": status, "error_code": code,
                   "recovered_status": recovered_status, "fault_requests": fault_requests},
    }


def _inject_network_failures(ctx):
    service = ctx.boot()
    ctx.provider.mode = "reset"
    before_requests = ctx.provider.requests
    status, body = service.chat("network-fault", "while the transport breaks")
    fault_requests = ctx.provider.requests - before_requests
    ready_status, _ = service.readiness()
    ctx.provider.mode = "healthy"
    recovered_status, _ = service.chat("network-fault", "after the transport heals")
    service.drain()
    code = (body.get("error") or {}).get("code")
    failures = []
    if fault_requests < 1:
        failures.append("the reset endpoint received no connection; no fault was exercised")
    if (status, body.get("error")) not in (
            (502, {"code": "agent_failure", "message": "agent turn failed"}),
            (504, {"code": "turn_deadline_exceeded", "message": "turn deadline exceeded"})):
        failures.append(
            f"a transport fault did not map to a stable gateway error (got {status})")
    if ready_status != 200:
        failures.append("readiness dropped for a transient transport fault")
    if recovered_status != 200:
        failures.append("the host did not serve again once the transport healed")
    return {
        "injection": (
            "the completion endpoint accepts the connection and closes it without "
            "a response, so the call fails at the transport rather than with a status"),
        "expected_behavior": (
            "the turn fails with a stable gateway error within its deadline, "
            "without disclosing transport diagnostics; "
            "readiness is unaffected and the next turn succeeds"),
        "observed_behavior": (
            f"the turn returned {status} {code!r}; /v1/ready returned {ready_status}; "
            f"the turn after the fault cleared returned {recovered_status}"),
        "service_recovered": ready_status == 200 and recovered_status == 200,
        "failures": failures,
        "detail": {"fault_status": status, "error_code": code,
                   "recovered_status": recovered_status, "fault_requests": fault_requests},
    }


def _inject_full_disks(ctx):
    service = ctx.boot()
    try:
        ballast = service.exhaust_state_filesystem()
        status, body = service.chat("full-disk", "while the state filesystem is full")
        ready_status, _ = service.readiness()
    finally:
        service.relieve_state_filesystem()
    recovered_status, _ = service.chat("full-disk", "after space is reclaimed")
    service.drain()
    failures = []
    if status != 500 or body.get("error") != {
            "code": "internal_error", "message": "internal service error"}:
        failures.append(f"full disk did not return the stable 500 internal_error (got {status})")
    if recovered_status != 200:
        failures.append("the host did not serve again once space was reclaimed")
    if ready_status not in (200, 503):
        failures.append(f"readiness returned an unexpected status ({ready_status})")
    return {
        "injection": (
            f"the state filesystem is filled to ENOSPC before a turn ({ballast})"),
        "expected_behavior": (
            "the turn fails with a stable bounded error instead of a partial write, "
            "previously committed sessions, sandboxes and audit chains are untouched, "
            "and the host serves again once "
            "space is reclaimed"),
        "observed_behavior": (
            f"the turn under ENOSPC returned {status}; /v1/ready returned "
            f"{ready_status}; the turn after reclaiming space returned {recovered_status}"),
        "service_recovered": recovered_status == 200,
        "failures": failures,
        "detail": {"ballast": ballast, "full_status": status,
                   "recovered_status": recovered_status},
    }


def _inject_corrupt_state(ctx):
    """Corrupt a committed audit chain while the host is stopped.

    The audit chain is chosen because the release's own startup verifies every
    signed chain before it will serve (`existing audit chains failed signature
    verification`), so the expected behavior is the strongest one available:
    refusing to start at all rather than serving over corruption.
    """
    chains = sorted((ctx.state_dir / "audit").glob("*.jsonl"))
    if not chains:
        return {
            "injection": "none: the baseline committed no audit chain to corrupt",
            "expected_behavior": "a corrupted signed audit chain is refused at startup",
            "observed_behavior": "no audit chain existed, so nothing was injected",
            "service_recovered": False,
            "failures": ["the baseline committed no audit chain; the injection was vacuous"],
            "detail": {},
        }
    target = chains[0]
    original = target.read_bytes()
    lines = original.splitlines(keepends=True)
    first = json.loads(lines[0])
    first["kind"] = "TAMPERED"
    lines[0] = (json.dumps(first) + "\n").encode()
    target.write_bytes(b"".join(lines))
    corrupted = target.read_bytes() != original
    try:
        started, detail = ctx.boot_expecting_refusal()
    finally:
        # Repair even when a probe fails; recovery is still tested below.
        target.write_bytes(original)
    repaired = ctx.boot()
    ready_status, _ = repaired.readiness()
    repaired.drain()
    refusal = ""
    if not started and detail.strip():
        refusal = ": " + detail.strip().splitlines()[-1][:200]
    failures = []
    if not corrupted:
        failures.append("the audit chain was not actually modified")
    if started:
        failures.append("the release served over a corrupted signed audit chain")
    if not started and "existing audit chains failed signature verification" not in detail:
        failures.append("startup refusal did not identify the audit signature failure")
    if ready_status != 200:
        failures.append("the release did not recover once the corruption was repaired")
    return {
        "injection": (
            "one committed signed audit record is edited in place while the host is "
            "stopped, then the release is restarted against that state"),
        "expected_behavior": (
            "startup verification detects the broken signature and the release "
            "refuses to serve rather than starting over corrupt state; once the "
            "record is repaired the same state directory boots and is ready"),
        "observed_behavior": (
            f"the release {'started anyway' if started else 'refused to start'}"
            f"{refusal}; after repair /v1/ready returned {ready_status}"),
        "service_recovered": ready_status == 200,
        "failures": failures,
        "detail": {"chain": target.name, "started_over_corruption": started},
    }


def _inject_interrupted_writes(ctx):
    """SIGKILL at an observed pre-rename boundary, never at a guessed delay."""
    service = ctx.boot(interrupt_session="interrupted-write")
    outcome = {}

    def turn():
        try:
            outcome["result"] = service.chat("interrupted-write", "a turn to interrupt")
        except Exception as error:  # the host is about to be killed under it
            outcome["error"] = repr(error)

    worker = threading.Thread(target=turn, daemon=True)
    worker.start()
    try:
        boundary = service.wait_write_interruption()
    finally:
        service.kill()
        worker.join(timeout=10)
    restarted = ctx.boot()
    ready_status, _ = restarted.readiness()
    status, _ = restarted.chat("interrupted-write-recovery", "after the crash")
    restarted.drain()
    adopted = service.interrupted_write_was_adopted()
    stray = sorted(
        path.name for path in ctx.state_dir.rglob("*.tmp") if path.is_file())
    failures = []
    if ready_status != 200:
        failures.append("the host did not become ready after being killed mid-write")
    if status != 200:
        failures.append("the host did not accept a new turn after being killed mid-write")
    if worker.is_alive() or "result" in outcome:
        failures.append("the interrupted request was not observed to disconnect before completion")
    if not boundary.get("boundary_observed"):
        failures.append("the file-replacement boundary was not observed")
    if adopted:
        failures.append("the uncommitted session write was adopted on restart")
    return {
        "injection": (
            "a Linux LD_PRELOAD rename observer stops the host after its session "
            "temporary file is closed but before replacement; the harness verifies "
            "the stopped PID and temporary bytes, then SIGKILLs its process group"),
        "expected_behavior": (
            "the atomic replace discipline leaves every previously committed file "
            "intact, no torn temporary file is adopted on restart, and the release "
            "becomes ready and accepts new turns"),
        "observed_behavior": (
            f"the interrupted turn ended as {outcome.get('result', outcome.get('error'))!r}; "
            f"after restart /v1/ready returned {ready_status} and a new turn returned "
            f"{status}; uncommitted session adopted: {adopted}; "
            f"ignored temporary files retained: {stray or 'none'}"),
        "service_recovered": ready_status == 200 and status == 200,
        "failures": failures,
        "detail": {**boundary, "uncommitted_session_adopted": adopted,
                   "ignored_temporary_files": stray},
    }


INJECTORS = {
    "runtime_crashes": _inject_runtime_crashes,
    "unavailable_model_providers": _inject_unavailable_model_providers,
    "network_failures": _inject_network_failures,
    "full_disks": _inject_full_disks,
    "corrupt_state": _inject_corrupt_state,
    "interrupted_writes": _inject_interrupted_writes,
}


# ── the harness ─────────────────────────────────────────────────────────────

class _CategoryContext:
    """Everything one category needs, and nothing it should not have."""

    def __init__(self, *, release_root, state_dir, auth_file, token, audit_key,
                 provider, service_factory, label):
        self.release_root = release_root
        self.state_dir = state_dir
        self.auth_file = auth_file
        self.token = token
        self.audit_key = audit_key
        self.provider = provider
        self.label = label
        self._factory = service_factory
        self._services = []

    def _build(self):
        service = self._factory(
            self.release_root, self.state_dir, auth_file=self.auth_file,
            token=self.token, audit_key=self.audit_key,
            endpoint=self.provider.endpoint, label=self.label)
        self._services.append(service)
        return service

    def boot(self, interrupt_session=None):
        service = self._build()
        if interrupt_session is not None:
            service.prepare_write_interruption(interrupt_session)
        service.start()
        return service

    def boot_expecting_refusal(self):
        """Boot where refusing to start is a valid, expected outcome."""
        service = self._build()
        started, detail = service.try_start()
        if started:
            service.drain()
        else:
            service.kill()
        return started, detail

    def shutdown(self):
        for service in self._services:
            try:
                service.drain()
            except Exception:
                try:
                    service.kill()
                except Exception:
                    pass


def _run_category(name, *, release_root, state_root, archive_dir, auth_file,
                  token, audit_key, provider, service_factory, evidence_url):
    """Baseline, inject, then prove state survived and the service came back."""
    state_dir = Path(state_root) / name
    state_dir.mkdir(parents=True, exist_ok=True)
    context = _CategoryContext(
        release_root=release_root, state_dir=state_dir, auth_file=auth_file,
        token=token, audit_key=audit_key, provider=provider,
        service_factory=service_factory, label=name)
    provider.mode = "healthy"
    failures = []
    try:
        # ANTI-VACUITY: commit real state through the release before breaking
        # anything, so "committed state survived" is a claim about something.
        baseline = context.boot()
        status, _ = baseline.chat(f"{name}-baseline", "commit one real turn")
        if status != 200:
            raise FailureDrillError(f"the baseline turn failed with status {status}")
        baseline.drain()
        before_archive = Path(archive_dir) / f"{name}-before.tar.gz"
        _state_backup(release_root, state_dir, before_archive, audit_key)
        _, before = _backup_payloads(before_archive)

        observation = INJECTORS[name](context)

        after_archive = Path(archive_dir) / f"{name}-after.tar.gz"
        _state_backup(release_root, state_dir, after_archive, audit_key)
        _, after = _backup_payloads(after_archive)
        continuity = _customer_continuity(before, after)
    finally:
        context.shutdown()

    failures.extend(observation.get("failures", []))
    if continuity["committed_customer_files"] == 0:
        failures.append("no committed customer state existed, so integrity is vacuous")
    if not continuity["preserved"]:
        failures.append(
            f"committed customer state changed or disappeared: {continuity['lost_or_changed']}")
    integrity = continuity["preserved"] and continuity["committed_customer_files"] > 0
    recovered = bool(observation.get("service_recovered"))
    return {
        "passed": not failures and integrity and recovered,
        "state_integrity_verified": integrity,
        "service_recovered": recovered,
        "injection": observation["injection"],
        "expected_behavior": observation["expected_behavior"],
        "observed_behavior": observation["observed_behavior"],
        "evidence_url": evidence_url,
        "continuity": continuity,
        "failures": failures,
        "detail": observation.get("detail", {}),
    }


def run_drill(*, artifact, audit_key_file, evidence_url, output, work_dir=None,
              service_factory=None, now_fn=time.time, bounded_filesystem_mb=64):
    output = Path(output).absolute()
    if type(bounded_filesystem_mb) is not int or not 8 <= bounded_filesystem_mb <= MAX_FILESYSTEM_MB:
        raise FailureDrillError(f"filesystem size must be 8..{MAX_FILESYSTEM_MB} MiB")
    if output.exists() or output.is_symlink():
        raise FailureDrillError("refusing to overwrite an existing drill report")
    try:
        audit_key = _read_private_key(audit_key_file)
    except ReleaseDrillError as error:
        raise FailureDrillError(str(error)) from error
    if not isinstance(evidence_url, str) or not evidence_url.startswith("https://"):
        raise FailureDrillError(
            "evidence URL must be an https:// link to the immutable raw run")

    owned = work_dir is None
    root = (Path(tempfile.mkdtemp(prefix="sigil-pi-failure-drill-")) if owned
            else Path(work_dir).absolute())
    if not owned:
        if root.exists() or root.is_symlink():
            raise FailureDrillError("work directory must not already exist")
        root.mkdir(parents=True, mode=0o700)
    root = root.resolve()

    real_service = service_factory is None
    factory = service_factory or _Service
    started_unix = now_fn()
    provider = _Provider()
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    try:
        installed = _install_artifact(Path(artifact).resolve(), root / "releases", "candidate")
        release_root = installed["root"]
        token = secrets.token_urlsafe(32)
        auth_file = root / "auth.json"
        _write_auth(auth_file, token, started_unix)

        archives = root / "archives"
        archives.mkdir(parents=True, exist_ok=True)
        categories = {}
        qualification_failures = []
        # Only the full-disk category needs a bounded filesystem, and mounting
        # one costs a privileged call, so it wraps that category alone.
        with _BoundedFilesystem(root / "bounded-state", bounded_filesystem_mb,
                                enabled=real_service) as bounded:
            for name in CATEGORIES:
                state_root = (bounded.path if name == "full_disks" and bounded.kind
                              else root / "state")
                state_root.mkdir(parents=True, exist_ok=True)
                try:
                    if name == "full_disks" and real_service and bounded.kind is None:
                        raise FailureDrillError("full-disk injection skipped: no disposable tmpfs")
                    result = _run_category(
                        name, release_root=release_root, state_root=state_root,
                        archive_dir=archives, auth_file=auth_file, token=token, audit_key=audit_key,
                        provider=provider, service_factory=factory,
                        evidence_url=evidence_url)
                except (OSError, ValueError, http.client.HTTPException,
                        subprocess.SubprocessError, FailureDrillError, ReleaseDrillError) as error:
                    result = {
                        "passed": False,
                        "state_integrity_verified": False,
                        "service_recovered": False,
                        "injection": f"{name} injection aborted",
                        "expected_behavior": "the category completes and reports an outcome",
                        "observed_behavior": f"the drill could not complete it: {error}",
                        "evidence_url": evidence_url,
                        "continuity": {},
                        "failures": [str(error)],
                        "detail": {},
                    }
                categories[name] = result
                if not result["passed"]:
                    reasons = "; ".join(result["failures"]) or "unspecified"
                    qualification_failures.append(f"{name} did not pass: {reasons}")
            bounded_kind, bounded_detail = bounded.kind, bounded.detail

        if not real_service:
            qualification_failures.append(
                "service was injected rather than the real release")
        if real_service and bounded_kind is None:
            qualification_failures.append(
                "the state filesystem could not be bounded, so the full-disk "
                f"injection did not exhaust a real filesystem ({bounded_detail})")

        report = {
            "schema_version": REPORT_SCHEMA,
            "test": "sigil-pi-v1-failure-injection",
            "started_unix": int(started_unix),
            "completed_unix": int(now_fn()),
            "artifact": {
                "version": installed["version"],
                "sha256": installed["sha256"],
            },
            "qualification_eligible": not qualification_failures,
            "qualification_failures": qualification_failures,
            "topology": {
                "workers": 1,
                "state_filesystem": "local-posix",
                "production_artifact": real_service,
                "bounded_filesystem": bounded_kind,
                "bounded_filesystem_detail": bounded_detail,
                "platform": platform.platform(),
                "python": platform.python_version(),
            },
            "categories": categories,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
                stream.write(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")
            # link is atomic and refuses an output created since our initial check.
            os.link(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        return report
    finally:
        provider.shutdown()
        provider.server_close()
        if owned and not os.path.ismount(root / "bounded-state"):
            shutil.rmtree(root, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True,
                        help="the candidate release archive to drill")
    parser.add_argument("--audit-key-file", required=True)
    parser.add_argument("--evidence-url", required=True,
                        help="https link to the immutable raw run this report comes from")
    parser.add_argument("--output", required=True)
    parser.add_argument("--work-dir")
    parser.add_argument("--bounded-filesystem-mb", type=int, default=64)
    parser.add_argument(
        "--mechanics-only", action="store_true",
        help="write a non-qualifying report instead of failing when the host "
             "cannot satisfy every qualification rule")
    args = parser.parse_args()
    try:
        report = run_drill(
            artifact=args.artifact,
            audit_key_file=args.audit_key_file,
            evidence_url=args.evidence_url,
            output=args.output,
            work_dir=args.work_dir,
            bounded_filesystem_mb=args.bounded_filesystem_mb)
    except (OSError, FailureDrillError, ReleaseDrillError) as error:
        parser.exit(2, f"failure drill failed: {error}\n")
    print(json.dumps(report, sort_keys=True))
    if not report["qualification_eligible"] and not args.mechanics_only:
        parser.exit(1, "failure drill completed but is not qualification-eligible\n")


if __name__ == "__main__":
    main()
