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
* ``state_integrity_verified`` — the session the fault was injected into still
  holds everything it had committed, and every other committed customer file
  (sessions, sandboxes, audit chains) is byte-identical, against a backup taken
  BEFORE the injection. Taken with the release's own ``state_tool.py``, which
  refuses to run without a clean-shutdown marker and verifies the signed audit
  chains, so a corrupted chain cannot be backed up and quietly compared
  against itself.
* ``passed`` — the release also behaved the way its own documentation says it
  does (a stable error, a fail-closed refusal, a replaced runtime), not merely
  that it survived.

THE FAULT LANDS ON COMMITTED STATE. Each category runs on its own fresh state
directory and commits a real turn through the release's forge path BEFORE
anything is injected, and then injects its fault into THAT session: the turn
that fails, the write that is interrupted and the chain that is corrupted are
all the committed session's own. Until audit 2026-11-10 (F-18) the faults went
to other sessions and only files that existed beforehand were compared, so the
verdict was about bystanders: a release that lost the session it was writing
would have passed. A failed turn and the turn that shows recovery both append
to the session, so it is compared by content (its committed messages and audit
records must still open the conversation and the chain, unchanged and in
order); a category whose session committed nothing fails rather than passing
trivially, and a run whose service was injected by a test can never be
qualification-eligible.

THE CONFIGURATION IS THE ONE THAT SHIPS. The service is given only what it
cannot start without (addresses, paths, credentials, the stand-in provider);
every timer, retry budget and limit is left to the release. The report records
that wiring, any setting a rehearsal overrode, and the settings the release
itself reports as in force. A run that shortened a timer or a retry budget is
recorded as such and is not qualification-eligible: a lease recovery measured
at 60 seconds says nothing about a release that ships with 300. Nor is a run
whose release reports no settings, since its configuration is then only the
harness's word.

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
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR.parent))

import toolchain  # noqa: E402

# The supported interpreters are defined once, in toolchain.py. This script
# checked only the floor, against a literal of its own, and so started on
# Python 3.15 (audit 2026-11-10, F-24). Refused here, before any other module
# of the project is loaded.
if not (toolchain.MINIMUM_PYTHON <= sys.version_info[:2]
        < toolchain.FIRST_UNSUPPORTED_PYTHON):
    raise SystemExit(toolchain.unsupported_python_message(sys.version_info))

from scripts.release_drill import (  # noqa: E402
    DEFAULT_PROBE_TIMEOUT,
    ReleaseDrillError,
    _backup_payloads,
    _create_private,
    _install_artifact,
    _read_private_key,
)
# Where a session is kept, and what it means for committed state to have
# survived, are defined once, in release_drill.py, for both drills.
from scripts.release_drill import _messages, _setting  # noqa: E402
from scripts.release_drill import _continuity as _committed_continuity  # noqa: E402
from scripts.release_drill import _session_files as _tenant_session_files  # noqa: E402

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


# ── the session a fault is injected into ────────────────────────────────────

TENANT = "failure-drill-tenant"


def _session_files(session):
    """Where the release keeps one of the drill tenant's sessions, as paths
    inside its state directory: (conversation, audit chain)."""
    return _tenant_session_files(TENANT, session)


def _continuity(before, after, session):
    """How the files committed before the injection fared.

    ``session`` is the one the fault was injected into. The turn that failed
    and the turn that showed recovery both append to it, so it is compared by
    what it had committed: its committed messages must still open the
    conversation and its committed audit records must still open the chain,
    unchanged and in order. Dropping, reordering or altering any of them, or
    losing a file, is a loss; growth is not. Every other committed file must
    be byte-identical, because nothing addressed it. The rule itself is
    release_drill's, which the recovery drill applies to the session its
    phases continue.
    """
    continuity = _committed_continuity(before, after, TENANT, session)
    continuity["fault_session"] = continuity.pop("session")
    return continuity


# ── the configuration the release runs with ─────────────────────────────────
#
# A drill is evidence about the configuration that ships, so it gives the
# service only what it cannot start without and leaves every timer, retry
# budget and limit to the release. Until audit 2026-11-10 (F-18) it set a
# 60-second lease (the release ships 300), 30-second turn and compiler
# deadlines (120 and 90) and one model retry (2), and recorded none of it.

# What the drill supplies, as the report records it. The values themselves are
# a secret, a credential file, and paths and ports that differ on every run.
WIRING = {
    "ANTHROPIC_API_KEY": "a placeholder: no request reaches a real provider",
    "PI_ENDPOINT": "the drill's stand-in provider, on loopback",
    "PI_AUDIT_KEY": "the key in --audit-key-file",
    "PI_AUTH_FILE": "one credential for the drill's tenant: scopes chat and "
                    "ops:read, no tools, valid for an hour",
    "PI_STATE": "a fresh state directory for each category",
    "PI_HOST": "127.0.0.1",
    "PI_PORT": "an unused loopback port for each boot",
}
# Settings a rehearsal may not override: the wiring above, anything whose
# value is a secret (the report records overrides in full), and TLS, which
# this harness does not speak.
_NOT_OVERRIDABLE = re.compile(r"PI_SECRET_.*|PI_TLS_.*")
# A setting that is a duration or a retry budget, by the name the release
# reports it under. Shortening one makes a failure look shorter-lived than it
# is in the shipped configuration.
_BUDGETS = ("_seconds", "_days", "_retries", "_ms")
# How long this harness waits where the release's timers decide: a turn may
# take its whole deadline, a drain its whole timeout, a crashed turn's lease
# its whole term. These are the values of releases that do not report their
# settings (every release before audit 2026-11-10); one that does is waited
# on by what it reports.
_ASSUMED_WAITS = {
    "turn_deadline_seconds": 120,
    "drain_timeout_seconds": 120,
    "turn_lease_seconds": 300,
}


def _validated_settings(service_settings):
    settings = {}
    for name, value in (service_settings or {}).items():
        if (not isinstance(name, str) or not re.fullmatch(r"PI_[A-Z0-9_]+", name)
                or name in WIRING or _NOT_OVERRIDABLE.fullmatch(name)
                or not isinstance(value, str) or not value.strip()):
            raise FailureDrillError(
                f"not a service setting a drill can override: {name!r}={value!r}. "
                "Give a PI_* setting of the release, other than the drill's own "
                "wiring, a secret or TLS, as NAME=VALUE text")
        settings[name] = value.strip()
    return settings


def _waits(settings, reported):
    waits = dict(_ASSUMED_WAITS)
    for name, value in settings.items():
        if _setting(name) in waits and value.isdigit() and int(value) > 0:
            waits[_setting(name)] = int(value)
    for key in waits:
        value = (reported or {}).get(key)
        if type(value) is int and value > 0:
            waits[key] = value
    return waits


def _configuration(settings, *, shipped, in_force, endpoint):
    """The report's record of what the release ran with, and the reasons, if
    any, why that configuration cannot qualify.

    ``shipped`` and ``in_force`` are the effective settings the release
    reported with nothing overridden and with ``settings`` applied; None for a
    release that reports none.
    """
    overrides, shortened, unknown, ignored = [], [], [], []
    for name in sorted(settings):
        key, value = _setting(name), settings[name]
        default = None if shipped is None else shipped.get(key)
        applied = None if in_force is None else in_force.get(key)
        is_shorter = False
        if key.endswith(_BUDGETS):
            try:
                is_shorter = int(value) < default
            except (TypeError, ValueError):
                is_shorter = None  # the shipped value is not known
        overrides.append({"setting": name, "value": value, "in_force": applied,
                          "shipped_default": default, "shortened": is_shorter})
        if is_shorter:
            shortened.append(f"{name}={value} (shipped {default})")
        elif is_shorter is None:
            unknown.append(name)
        if in_force is not None and str(applied) != value:
            ignored.append(f"{name}={value} (it reports {applied!r})")
    failures = []
    if in_force is None:
        failures.append(
            "the release does not report its effective settings, so the "
            "configuration in force is not recorded")
    else:
        if shortened:
            failures.append(
                "the release ran with timers or retry budgets shorter than it "
                "ships with: " + ", ".join(shortened))
        if unknown:
            failures.append(
                "the release does not report what it ships for "
                + ", ".join(unknown) + ", so the override cannot be shown to "
                "shorten no timer")
        if ignored:
            failures.append(
                "the release does not report these overrides as in force: "
                + ", ".join(ignored))
    record = {
        "provider": {
            "kind": "stand-in",
            "endpoint": endpoint,
            "description": "an Anthropic-shaped endpoint on loopback that the "
                           "drill switches between healthy, unavailable and "
                           "reset; no request leaves this host",
        },
        "wiring": dict(WIRING),
        "overrides": overrides,
        "shortened": [entry["setting"] for entry in overrides if entry["shortened"]],
        "effective_settings": in_force,
    }
    return record, failures


class _WriteBoundary:
    """Observe a real Linux rename boundary without modifying the candidate.

    The target is a session file that already holds a committed conversation,
    so the save being interrupted REPLACES a file. An LD_PRELOAD observer
    stops the host as it calls rename(2) on that file: after the new
    conversation has been written in full to a scratch file, and before that
    file takes the committed one's name. What the scratch file is called is
    the release's business (``<name>.tmp`` before 2026-11-10, a unique
    ``.<name>.<16 hex>.tmp`` since), so it is found as the one file that has
    appeared beside the target since the boundary was armed, never by name.
    Until audit 2026-11-10 (F-18) this required that the target did not exist
    yet: it only ever interrupted the creation of a file.
    """

    def __init__(self, directory, target):
        if sys.platform != "linux" or not shutil.which("cc"):
            raise FailureDrillError("write-boundary injection requires Linux and cc")
        directory = Path(directory)
        self.target = Path(target)
        try:
            self.committed = self.target.read_bytes()
        except OSError:
            self.committed = b""
        if not _messages(self.committed):
            raise FailureDrillError(
                "the session to interrupt holds no committed conversation, so "
                "there is no file whose replacement could be interrupted")
        self.beside = self._beside()
        self.marker = directory / "rename-stopped.pid"
        self.library = directory / "failure-drill-rename.so"
        subprocess.run([
            "cc", "-shared", "-fPIC", "-Wall", "-Werror", "-o", str(self.library),
            str(SCRIPTS_DIR / "failure_drill_rename.c"), "-ldl"],
            check=True, capture_output=True, text=True)

    def _beside(self):
        with os.scandir(self.target.parent) as entries:
            return {entry.name for entry in entries
                    if entry.is_file(follow_symlinks=False)}

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
                    return self._observe(process)
            time.sleep(0.02)
        raise FailureDrillError("no stopped host at the session rename boundary was observed")

    def _observe(self, process):
        """What is on disk while the host is stopped at its rename."""
        try:
            in_place = self.target.read_bytes()
        except OSError:
            in_place = None
        if in_place != self.committed:
            raise FailureDrillError(
                "the committed session file changed before its replacement was reached")
        scratch = sorted(self._beside() - self.beside)
        data = (self.target.parent / scratch[0]).read_bytes() if len(scratch) == 1 else b""
        if not data or data == self.committed:
            raise FailureDrillError(
                "rename stopped without an uncommitted write beside the committed "
                f"file (files that appeared: {scratch or 'none'})")
        return {"host_pid": process.pid, "boundary_observed": True,
                "committed_bytes": len(self.committed),
                "committed_sha256": hashlib.sha256(self.committed).hexdigest(),
                "temporary_file": scratch[0],
                "temporary_bytes": len(data),
                "temporary_sha256": hashlib.sha256(data).hexdigest()}

    def adopted(self):
        """Whether the committed file is no longer exactly what was committed:
        the stopped write reached it after all, or the kill damaged it."""
        try:
            return self.target.read_bytes() != self.committed
        except OSError:
            return True


# ── the switchable provider ─────────────────────────────────────────────────

class _ProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        return

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        if self.path.endswith("/count_tokens"):
            body = b'{"input_tokens":1}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
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
                 endpoint, label, timeout_s=DEFAULT_PROBE_TIMEOUT, settings=None,
                 waits=None):
        self.release_root = Path(release_root)
        self.state_dir = Path(state_dir)
        self.auth_file = Path(auth_file)
        self.token = token
        self.audit_key = audit_key
        self.endpoint = endpoint
        self.label = label
        self.timeout_s = timeout_s
        # A rehearsal's overrides; empty for a drill of what ships.
        self.settings = dict(settings or {})
        waits = {**_ASSUMED_WAITS, **(waits or {})}
        # The release's timers decide these waits; the margins are for the
        # request itself and for the process to exit.
        self.turn_wait_s = waits["turn_deadline_seconds"] + 15
        self.drain_wait_s = waits["drain_timeout_seconds"] + 15
        self.lease_wait_s = waits["turn_lease_seconds"] + 5
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
        # Exactly the wiring the report records (WIRING), then a rehearsal's
        # overrides. No timer, retry budget or limit is set here: the release
        # runs with the ones it ships.
        environment.update({
            "ANTHROPIC_API_KEY": "failure-drill-provider-placeholder",
            "PI_ENDPOINT": self.endpoint,
            "PI_AUDIT_KEY": self.audit_key,
            "PI_AUTH_FILE": str(self.auth_file),
            "PI_STATE": str(self.state_dir),
            "PI_HOST": "127.0.0.1",
            "PI_PORT": str(self.port),
        })
        environment.update(self.settings)
        if self.write_boundary is not None:
            environment.update(self.write_boundary.environment())
        return environment

    def prepare_write_interruption(self, session):
        """Arm the boundary on the session's own conversation file, which must
        already hold what the session committed."""
        conversation, _ = _session_files(session)
        self.write_boundary = _WriteBoundary(
            self.state_dir.parent, self.state_dir / conversation)

    def wait_write_interruption(self):
        return self.write_boundary.wait(self.process, timeout=self.turn_wait_s)

    def interrupted_write_was_adopted(self):
        return self.write_boundary.adopted()

    def wait_crash_leases(self, timeout_s=None):
        """Follow the packaged failed-shutdown runbook; never delete leases."""
        timeout_s = self.lease_wait_s if timeout_s is None else timeout_s
        started = time.monotonic()
        database = self.state_dir / "product-quotas.sqlite3"
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            count, expires = connection.execute(
                "SELECT COUNT(*), MAX(expires) FROM turn_leases").fetchone()
        finally:
            connection.close()
        if not count:
            raise FailureDrillError("no persisted lease existed for the interrupted turn")
        while True:
            remaining = expires - time.time()
            elapsed = time.monotonic() - started
            if remaining <= 0:
                return {"lease_wait_seconds": elapsed, "crashed_leases": count}
            if elapsed + remaining > timeout_s:
                raise FailureDrillError("crash lease expiry exceeds the bounded recovery wait")
            time.sleep(min(remaining + 0.01, 1.0))

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

    def effective_settings(self):
        """The configuration in force, as the release itself reports it in
        /v1/metrics; None for a release that reports none (every release
        before audit 2026-11-10, F-18)."""
        status, body = _request("GET", f"http://127.0.0.1:{self.port}/v1/metrics",
                                self.token, timeout=5)
        if status != 200:
            raise FailureDrillError(f"/v1/metrics returned {status}")
        metrics = body.get("metrics")
        settings = metrics.get("effective_settings") if isinstance(metrics, dict) else None
        return settings if isinstance(settings, dict) else None

    def chat(self, session, message, timeout=None):
        return _request(
            "POST", f"http://127.0.0.1:{self.port}/v1/chat", self.token,
            payload={"session": session, "message": message},
            timeout=self.turn_wait_s if timeout is None else timeout)

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
                self.process.wait(timeout=self.drain_wait_s)
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
        "tenant": TENANT,
        "scopes": ["chat", "ops:read"],
        "tools": [],
        "not_before_unix": int(now) - 60,
        "expires_unix": int(now) + 3600,
    }]}
    # Private from the moment it exists: it holds a chat credential.
    _create_private(path, json.dumps(document, sort_keys=True, separators=(",", ":")))


# ── the six injections ──────────────────────────────────────────────────────
#
# Each returns the observation for one category. The surrounding harness owns
# the committed turn, the before/after backups and the continuity comparison,
# so an injector only has to break something and say what it saw.
#
# Every turn goes through ``ctx.turn``, which addresses the one session the
# category committed and will compare: an injector has no session of its own
# to name. That is what makes the integrity verdict a statement about the
# file the fault touched. An injector that watched the committed files
# directly, at a moment the final comparison cannot see, reports what it saw
# as ``state_intact``.

def _inject_runtime_crashes(ctx):
    service = ctx.boot()
    killed = service.kill_runtime()
    fault_status, fault_body = ctx.turn(service, "detect the dead runtime")
    status, body = ctx.turn(service, "a new call after detection")
    ready_status, _ = service.readiness()
    replacements = service.runtime_pids()
    service.drain()
    failures = []
    if not killed:
        failures.append("no forge child was found to kill; nothing was injected")
    if fault_status != 502 or fault_body.get("error") != {
            "code": "agent_failure", "message": "agent turn failed"}:
        failures.append("the crash-detecting call did not return the stable 502 agent_failure")
    if not replacements or set(replacements) & set(killed):
        failures.append("no distinct replacement forge child was observed")
    if status != 200:
        failures.append(f"the turn after the crash did not succeed (status {status})")
    if ready_status != 200:
        failures.append("the host did not report ready after the runtime was killed")
    return {
        "injection": (
            "SIGKILL every forge child of the running host, then a turn on the "
            "session that holds the committed conversation"),
        "expected_behavior": (
            "the detecting call fails with stable 502 agent_failure, retiring the "
            "compiler without replaying uncertain side effects; a separate next "
            "call on the same session obtains a replacement, succeeds, and "
            "readiness passes; the session keeps everything it had committed"),
        "observed_behavior": (
            f"killed forge children {killed or 'none'}; the detecting call returned "
            f"{fault_status}; a separate recovery turn returned "
            f"{status} and /v1/ready returned {ready_status}"),
        "service_recovered": ready_status == 200 and status == 200,
        "failures": failures,
        "detail": {"killed_pids": killed, "replacement_pids": replacements, "turn_status": status,
                   "fault_status": fault_status, "reply": bool(body.get("reply"))},
    }


def _inject_unavailable_model_providers(ctx):
    service = ctx.boot()
    ctx.provider.mode = "unavailable"
    before_requests = ctx.provider.requests
    status, body = ctx.turn(service, "while the provider is down")
    fault_requests = ctx.provider.requests - before_requests
    ready_status, _ = service.readiness()
    ctx.provider.mode = "healthy"
    recovered_status, _ = ctx.turn(service, "after the provider returns")
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
            "overloaded_error for the duration of one turn on the session that "
            "holds the committed conversation"),
        "expected_behavior": (
            "the turn fails with the stable 502 agent_failure carrying no provider "
            "diagnostics, readiness stays up because the dependency is external, "
            "the next turn on the same session succeeds once the provider "
            "returns, and the session keeps everything it had committed"),
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
    status, body = ctx.turn(service, "while the transport breaks")
    fault_requests = ctx.provider.requests - before_requests
    ready_status, _ = service.readiness()
    ctx.provider.mode = "healthy"
    recovered_status, _ = ctx.turn(service, "after the transport heals")
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
            "a response, so a turn on the session that holds the committed "
            "conversation fails at the transport rather than with a status"),
        "expected_behavior": (
            "the turn fails with a stable gateway error within its deadline, "
            "without disclosing transport diagnostics; "
            "readiness is unaffected, the next turn on the same session succeeds, "
            "and the session keeps everything it had committed"),
        "observed_behavior": (
            f"the turn returned {status} {code!r}; /v1/ready returned {ready_status}; "
            f"the turn after the fault cleared returned {recovered_status}"),
        "service_recovered": ready_status == 200 and recovered_status == 200,
        "failures": failures,
        "detail": {"fault_status": status, "error_code": code,
                   "recovered_status": recovered_status, "fault_requests": fault_requests},
    }


# The product's stable answers to a turn that meets a full state filesystem.
# Its audit log is strict (audit F-04), so the write that fails first decides
# which one a turn gets: the audit append answers audit_write_failed, any other
# state write internal_error. Both are content-free; nothing else is accepted.
FULL_DISK_ERRORS = (
    {"code": "internal_error", "message": "internal service error"},
    {"code": "audit_write_failed", "message": "turn audit record could not be written"},
)


def _inject_full_disks(ctx):
    service = ctx.boot()
    try:
        ballast = service.exhaust_state_filesystem()
        status, body = ctx.turn(service, "while the state filesystem is full")
        ready_status, _ = service.readiness()
    finally:
        service.relieve_state_filesystem()
    recovered_status, _ = ctx.turn(service, "after space is reclaimed")
    service.drain()
    failures = []
    if status != 500 or body.get("error") not in FULL_DISK_ERRORS:
        failures.append(
            "full disk did not return a stable 500 internal_error or audit_write_failed "
            f"(got {status})")
    if recovered_status != 200:
        failures.append("the host did not serve again once space was reclaimed")
    if ready_status not in (200, 503):
        failures.append(f"readiness returned an unexpected status ({ready_status})")
    return {
        "injection": (
            f"the state filesystem is filled to ENOSPC before a turn on the "
            f"session that holds the committed conversation ({ballast})"),
        "expected_behavior": (
            "the turn fails with a stable bounded error instead of a partial write, "
            "the session keeps everything it had committed, every other "
            "committed session, sandbox and audit chain is untouched, "
            "and the host serves that session again once "
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
    """Corrupt the committed session's audit chain while the host is stopped.

    The audit chain is chosen because the release's own startup verifies every
    signed chain before it will serve (`existing audit chains failed signature
    verification`), so the expected behavior is the strongest one available:
    refusing to start at all rather than serving over corruption.

    The harness has to repair the record before the release can recover, and a
    comparison made after that repair says nothing about what the release did
    to the corrupted chain: until audit 2026-11-10 (F-18) it was the only
    comparison made. So the committed files are read between the refusal and
    the repair, where a release that deleted, truncated or rewrote anything
    while refusing shows.
    """
    _, chain = _session_files(ctx.session)
    committed = ctx.committed_files()
    original = committed.get(chain)
    if not original:
        return {
            "injection": "none: the session committed no audit chain to corrupt",
            "expected_behavior": "a corrupted signed audit chain is refused at startup",
            "observed_behavior": "no audit chain existed, so nothing was injected",
            "service_recovered": False,
            "failures": ["the session committed no audit chain; the injection was vacuous"],
            "detail": {},
        }
    target = ctx.state_dir / chain
    lines = original.splitlines(keepends=True)
    first = json.loads(lines[0])
    first["kind"] = "TAMPERED"
    lines[0] = (json.dumps(first) + "\n").encode()
    tampered = b"".join(lines)
    target.write_bytes(tampered)
    corrupted = target.read_bytes() != original
    try:
        started, detail = ctx.boot_expecting_refusal()
        left = ctx.committed_files()
    finally:
        # Repair even when a probe fails; recovery is still tested below.
        target.write_bytes(original)
    expected = {**committed, chain: tampered}
    altered = sorted(path for path in set(expected) | set(left)
                     if left.get(path) != expected.get(path))
    repaired = ctx.boot()
    ready_status, _ = repaired.readiness()
    turn_status, _ = ctx.turn(repaired, "after the record is repaired")
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
    if altered:
        failures.append(
            "the refused start changed committed files instead of leaving them "
            f"as it found them: {altered}")
    if ready_status != 200:
        failures.append("the release did not recover once the corruption was repaired")
    if turn_status != 200:
        failures.append(
            f"the repaired session did not accept a turn (status {turn_status})")
    return {
        "injection": (
            "one signed audit record of the committed session is edited in place "
            "while the host is stopped, then the release is restarted against "
            "that state"),
        "expected_behavior": (
            "startup verification detects the broken signature and the release "
            "refuses to serve rather than starting over corrupt state, leaving "
            "the corrupted chain and every other committed file exactly as it "
            "found them; once the harness repairs the record the same state "
            "directory boots, is ready, and the session accepts a turn"),
        "observed_behavior": (
            f"the release {'started anyway' if started else 'refused to start'}"
            f"{refusal}; committed files the refused start changed: "
            f"{altered or 'none'}; after repair /v1/ready returned {ready_status} "
            f"and a turn on the session returned {turn_status}"),
        "service_recovered": ready_status == 200 and turn_status == 200,
        "state_intact": not altered,
        "failures": failures,
        "detail": {"chain": target.name, "started_over_corruption": started,
                   "refused_start_left_committed_files_unchanged": not altered,
                   "turn_after_repair_status": turn_status},
    }


def _inject_interrupted_writes(ctx):
    """SIGKILL at an observed pre-rename boundary, never at a guessed delay."""
    service = ctx.boot(interrupt_write=True)
    outcome = {}

    def turn():
        try:
            outcome["result"] = ctx.turn(service, "a turn to interrupt")
        except Exception as error:  # the host is about to be killed under it
            outcome["error"] = repr(error)

    worker = threading.Thread(target=turn, daemon=True)
    worker.start()
    try:
        boundary = service.wait_write_interruption()
    finally:
        service.kill()
        worker.join(timeout=10)
    lease_recovery = service.wait_crash_leases()
    restarted = ctx.boot()
    ready_status, _ = restarted.readiness()
    # Both read before another turn reaches the session: after the kill and
    # the restart its file must still be exactly what was committed, and the
    # killed save's scratch, which holds a conversation that never committed,
    # must be gone.
    adopted = service.interrupted_write_was_adopted()
    stray = sorted(path.relative_to(ctx.state_dir).as_posix()
                   for path in ctx.state_dir.rglob("*.tmp") if path.is_file())
    status, _ = ctx.turn(restarted, "after the crash")
    restarted.drain()
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
        failures.append(
            "the committed session file changed across the kill and the restart: "
            "the uncommitted write was adopted, or the file was damaged")
    if stray:
        failures.append(f"startup left the killed save's scratch behind: {stray}")
    return {
        "injection": (
            "a Linux LD_PRELOAD rename observer stops the host as it replaces the "
            "committed session's conversation file: after the new conversation is "
            "written in full to a scratch file and before the rename; the harness "
            "verifies the stopped PID, the committed file still in place and the "
            "scratch bytes, then SIGKILLs the host's process group"),
        "expected_behavior": (
            "after the documented lease-expiry wait, the atomic replace discipline "
            "leaves the committed conversation exactly as it was and every other "
            "committed file intact, the uncommitted write is not adopted on "
            "restart and its scratch file is removed, and the release "
            "becomes ready and accepts a new turn on that session"),
        "observed_behavior": (
            f"the interrupted turn ended as {outcome.get('result', outcome.get('error'))!r}; "
            f"after restart /v1/ready returned {ready_status} and a new turn returned "
            f"{status}; committed file changed or uncommitted write adopted: {adopted}; "
            f"scratch files left after the restart: {stray or 'none'}"),
        "service_recovered": ready_status == 200 and status == 200,
        "state_intact": not adopted,
        "failures": failures,
        "detail": {**boundary, **lease_recovery, "uncommitted_session_adopted": adopted,
                   "scratch_files_after_restart": stray},
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
                 provider, service_factory, label, session, settings=None, waits=None):
        self.release_root = release_root
        self.state_dir = state_dir
        self.auth_file = auth_file
        self.token = token
        self.audit_key = audit_key
        self.provider = provider
        self.label = label
        # The one session this category commits, faults and compares.
        self.session = session
        self.settings = settings or {}
        self.waits = waits
        self._factory = service_factory
        self._services = []

    def _build(self):
        service = self._factory(
            self.release_root, self.state_dir, auth_file=self.auth_file,
            token=self.token, audit_key=self.audit_key,
            endpoint=self.provider.endpoint, label=self.label,
            settings=self.settings, waits=self.waits)
        self._services.append(service)
        return service

    def boot(self, interrupt_write=False):
        service = self._build()
        if interrupt_write:
            service.prepare_write_interruption(self.session)
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

    def turn(self, service, message):
        """One turn on the category's session. The only way a turn is sent,
        so no fault can be addressed to a session the verdict is not about."""
        return service.chat(self.session, message)

    def committed_files(self):
        """The committed customer files as they are on disk now, by the path a
        backup gives them. For use while no service is running on the state."""
        files = {}
        for surface, suffix in (("sessions", ".kv"), ("audit", ".jsonl"),
                                ("sandboxes", None)):
            root = self.state_dir / surface
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if (path.is_file() and not path.is_symlink()
                        and suffix in (None, path.suffix)):
                    files[path.relative_to(self.state_dir).as_posix()] = path.read_bytes()
        return files

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
                  token, audit_key, provider, service_factory, evidence_url,
                  settings=None, waits=None):
    """Commit, inject into what was committed, then prove it survived and the
    service came back."""
    state_dir = Path(state_root) / name
    state_dir.mkdir(parents=True, exist_ok=True)
    session = f"{name}-committed"
    context = _CategoryContext(
        release_root=release_root, state_dir=state_dir, auth_file=auth_file,
        token=token, audit_key=audit_key, provider=provider,
        service_factory=service_factory, label=name, session=session,
        settings=settings, waits=waits)
    provider.mode = "healthy"
    failures = []
    try:
        # ANTI-VACUITY: commit real state through the release before breaking
        # anything, in the session the fault will be injected into, so
        # "committed state survived" is a claim about the file the fault
        # touched.
        baseline = context.boot()
        status, _ = context.turn(baseline, "commit one real turn")
        if status != 200:
            raise FailureDrillError(f"the baseline turn failed with status {status}")
        baseline.drain()
        before_archive = Path(archive_dir) / f"{name}-before.tar.gz"
        _state_backup(release_root, state_dir, before_archive, audit_key)
        _, before = _backup_payloads(before_archive)

        observation = INJECTORS[name](context)

        after_archive = Path(archive_dir) / f"{name}-after.tar.gz"
        try:
            _state_backup(release_root, state_dir, after_archive, audit_key)
            _, after = _backup_payloads(after_archive)
            continuity = _continuity(before, after, session)
        except (OSError, FailureDrillError, ReleaseDrillError) as error:
            # Preserve the observed injection/recovery even if integrity could
            # not be established. Missing verification is never a pass.
            continuity = _continuity(before, before, session)
            continuity.update(preserved=False, verification_error=str(error))
            continuity["fault_session"]["preserved"] = False
            failures.append(str(error))
    finally:
        context.shutdown()

    failures.extend(observation.get("failures", []))
    fault = continuity["fault_session"]
    if continuity["committed_customer_files"] == 0:
        failures.append("no committed customer state existed, so integrity is vacuous")
    if not fault["held_committed_content"]:
        failures.append(
            "the session the fault was injected into held no committed "
            "conversation and audit chain, so the integrity verdict would be "
            "about files the fault never touched")
    if continuity["lost_or_changed"]:
        failures.append(
            f"committed customer state changed or disappeared: {continuity['lost_or_changed']}")
    integrity = (continuity["preserved"] and fault["preserved"]
                 and observation.get("state_intact", True))
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


_DRILL_ERRORS = (OSError, ValueError, http.client.HTTPException,
                 subprocess.SubprocessError, FailureDrillError, ReleaseDrillError)


def _read_configuration(factory, *, release_root, state_root, auth_file, token,
                        audit_key, provider, settings):
    """Ask the release what it runs with, before anything is injected.

    One boot with only the wiring gives the settings the release ships with.
    A rehearsal's overrides get a second boot, so that what is in force can be
    compared with what ships. Returns the record for the report, the reasons
    the configuration cannot qualify, and how long to wait on this release.
    """
    def reported(label, given):
        state_dir = Path(state_root) / label
        state_dir.mkdir(parents=True, exist_ok=True)
        service = factory(
            release_root, state_dir, auth_file=auth_file, token=token,
            audit_key=audit_key, endpoint=provider.endpoint, label=label,
            settings=given, waits=_waits(given, None))
        try:
            service.start()
            settings = service.effective_settings()
            service.drain()
            return settings
        except BaseException:
            # Nothing of this boot is kept, so a release that would not
            # start, answer or drain is simply stopped.
            try:
                service.kill()
            except Exception:
                pass
            raise

    shipped = in_force = problem = None
    try:
        shipped = reported("configuration-shipped", {})
        in_force = reported("configuration-in-force", settings) if settings else shipped
    except _DRILL_ERRORS as error:
        shipped = in_force = None
        problem = f"the release's configuration could not be read: {error}"
    record, failures = _configuration(
        settings, shipped=shipped, in_force=in_force, endpoint=provider.endpoint)
    return record, [problem] if problem else failures, _waits(settings, in_force)


def run_drill(*, artifact, audit_key_file, evidence_url, output, work_dir=None,
              service_factory=None, now_fn=time.time, bounded_filesystem_mb=64,
              service_settings=None):
    """``service_settings`` are a rehearsal's overrides of the release's own
    settings (``{"PI_TURN_LEASE_SECONDS": "60"}``). They are recorded in the
    report, and a run that shortens a timer or a retry budget with them is
    not qualification-eligible."""
    output = Path(output).absolute()
    if type(bounded_filesystem_mb) is not int or not 8 <= bounded_filesystem_mb <= MAX_FILESYSTEM_MB:
        raise FailureDrillError(f"filesystem size must be 8..{MAX_FILESYSTEM_MB} MiB")
    settings = _validated_settings(service_settings)
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
        configuration, configuration_failures, waits = _read_configuration(
            factory, release_root=release_root, state_root=root / "state",
            auth_file=auth_file, token=token, audit_key=audit_key,
            provider=provider, settings=settings)
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
                        evidence_url=evidence_url, settings=settings, waits=waits)
                except _DRILL_ERRORS as error:
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
        qualification_failures.extend(configuration_failures)

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
            "configuration": configuration,
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


def _service_setting(text):
    name, separator, value = text.partition("=")
    if not separator:
        raise argparse.ArgumentTypeError(f"expected NAME=VALUE, got {text!r}")
    return name, value


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
        "--service-setting", action="append", default=[], type=_service_setting,
        metavar="NAME=VALUE",
        help="for a rehearsal: start the release with this PI_* setting instead "
             "of the one it ships with (repeatable). The report records it; a "
             "run that shortens a timer or a retry budget cannot qualify")
    parser.add_argument(
        "--mechanics-only", action="store_true",
        help="write a non-qualifying report instead of failing when the host "
             "cannot satisfy every qualification rule")
    args = parser.parse_args()
    if len({name for name, _ in args.service_setting}) != len(args.service_setting):
        parser.exit(2, "failure drill failed: a --service-setting is given twice\n")
    try:
        report = run_drill(
            artifact=args.artifact,
            audit_key_file=args.audit_key_file,
            evidence_url=args.evidence_url,
            output=args.output,
            work_dir=args.work_dir,
            bounded_filesystem_mb=args.bounded_filesystem_mb,
            service_settings=dict(args.service_setting))
    except (OSError, FailureDrillError, ReleaseDrillError) as error:
        parser.exit(2, f"failure drill failed: {error}\n")
    except Exception as error:
        # Exit 1 means a drill that completed and is not eligible; a drill
        # that stopped on an error nobody foresaw did not complete.
        traceback.print_exc()
        parser.exit(2, f"failure drill failed: {type(error).__name__}: {error}\n")
    print(json.dumps(report, sort_keys=True))
    if not report["qualification_eligible"] and not args.mechanics_only:
        parser.exit(1, "failure drill completed but is not qualification-eligible\n")


if __name__ == "__main__":
    main()
