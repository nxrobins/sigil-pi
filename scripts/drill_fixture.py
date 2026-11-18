#!/usr/bin/env python3
"""Produce the release drill's third input: a fresh backup with real state.

``scripts/release_drill.py`` self-provisions its service environment, but it
cannot invent its ``--state-backup``: qualification requires a backup no older
than 15 minutes containing at least one COMMITTED SESSION whose signed audit
chain verifies under the drill's key. The 2026-08-20 mechanics run used a
hand-made "fresh stopped-state fixture"; nothing could reproduce it, so the
qualifying distinct-version drill could not run anywhere unattended.

This produces that backup HONESTLY — no state files are fabricated:

1. install the OLD release exactly as the drill does (same extraction and
   manifest verification, imported from release_drill rather than re-implemented);
2. boot its real service with a local Anthropic-shaped mock as the provider
   (``PI_ENDPOINT``): the drill measures state and recovery, not the provider;
3. run one real ``/v1/chat`` turn, which commits a session, a sandbox, a signed
   audit chain, and durable quota rows through the release's own code. The
   turn is committed as the tenant, and in the session, that the drill reads
   and continues in every phase (release_drill.FIXTURE_TENANT and
   FIXTURE_SESSION), and the evidence names both;
4. drain with SIGTERM and require the clean-shutdown marker, because the
   release's own ``state_tool.py backup`` refuses to run without it;
5. back up with the OLD release's ``state_tool.py`` — the ordering
   docs/state-compatibility.md requires of a real operator ("create the
   pre-upgrade backup with the old release").

The backup's ``created_unix`` is therefore genuinely the moment after a real
turn was committed, which is what makes the drill's RPO claim mean something.

With ``--migration-state``, that turn writes two nonempty sandbox files through
the release's real write_file tool. Two API-created schedules then complete
their first real runs before draining. The archive's file hashes and complete
schedule metadata are recorded for independent checks after every recovery phase.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import os
import signal
import socket
import subprocess
import sys
import threading
import time
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
    DRAIN_WAIT_SECONDS,
    FIXTURE_SESSION,
    FIXTURE_TENANT,
    SERVICE_SETTINGS,
    # How long the fixture's one turn may take: the release's whole shipped
    # turn deadline (120 seconds), and a margin for the request itself.
    TURN_WAIT_SECONDS,
    ReleaseDrillError,
    _backup_payloads,
    _create_private,
    _install_artifact,
    _read_private_key,
    _messages,
    _role,
    _session_files,
    _sha256,
    _text,
    service_configuration,
)


class DrillFixtureError(RuntimeError):
    pass


MIGRATION_FILES = {
    "migration-first.txt": "The old release wrote this first migration fixture.\n",
    "migration-second.txt": "Second fixture | preserved UTF-8: λ.\n",
}
MIGRATION_SCHEDULES = (
    {"name": "migration-daily", "session": FIXTURE_SESSION,
     "message": 'Daily fixture: preserve "quotes".', "every_ms": 86400000},
    {"name": "migration-weekly", "session": FIXTURE_SESSION,
     "message": "Weekly fixture: preserve λ.", "every_ms": 604800000},
)


class _MockProvider(ThreadingHTTPServer):
    """An Anthropic-shaped completion endpoint on loopback.

    The fixture's turn must exercise the release's REAL forge path — compose,
    solver-verified guest, host-injected secret, signed audit record — and all
    of that happens regardless of who answers the HTTP call at the end of it.
    Faking the provider keeps the fixture hermetic; faking anything below it
    would make the committed state a lie.
    """

    def __init__(self, migration_state=False):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path.endswith("/count_tokens"):
                    body = b'{"input_tokens":7}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                outer.requests += 1
                outer.turn_messages.append(json.loads(raw).get("messages"))
                writes = migration_state and outer.requests == 1
                content = ([{"type": "tool_use", "id": f"fixture_write_{index}",
                             "name": "write_file", "input": {"path": name, "content": body}}
                            for index, (name, body) in enumerate(MIGRATION_FILES.items())]
                           if writes else [{"type": "text",
                                            "text": "the drill fixture's committed reply"}])
                body = json.dumps({
                    "id": "msg_drill_fixture", "type": "message",
                    "role": "assistant", "model": "drill-fixture-mock",
                    "content": content,
                    "stop_reason": "tool_use" if writes else "end_turn",
                    "usage": {"input_tokens": 7, "output_tokens": 9},
                }, separators=(",", ":")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.requests = 0
        self.turn_messages = []
        super().__init__(("127.0.0.1", 0), Handler)

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.server_address[1]}/v1/messages"


def _free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _request(method, url, token, payload=None, timeout=5):
    data = None
    headers = {"Authorization": f"Bearer {token}",
               "X-Request-ID": "drill-fixture-turn"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.load(response)


def _schedule_turn_evidence(requests, conversation):
    """Each schedule reached the provider as a new turn and committed its reply."""
    found = []
    for schedule in MIGRATION_SCHEDULES:
        message = schedule["message"]
        calls = sum(isinstance(held, list) and bool(held)
                    and _role(held[-1]) == "user" and _text(held[-1]) == message
                    for held in requests)
        committed = any(_role(user) == "user" and _text(user) == message
                        and _role(reply) == "assistant"
                        and _text(reply) == "the drill fixture's committed reply"
                        for user, reply in zip(conversation or [], (conversation or [])[1:]))
        if not calls or not committed:
            raise DrillFixtureError("scheduled turn lacks provider input or committed reply: "
                                    + schedule["name"])
        found.append({"name": schedule["name"], "message": message,
                      "provider_requests": calls, "committed_reply": True})
    return found


def produce_backup(*, artifact, output, audit_key_file, work_dir,
                   timeout_s=DEFAULT_PROBE_TIMEOUT, migration_state=False):
    """Boot the release in ``artifact``, commit one real turn, drain, back up.

    Returns evidence: the backup digest, the number of committed session files,
    and the timestamps an auditor needs to see that nothing was pre-baked.
    """
    output = Path(output).resolve()
    if output.exists() or output.is_symlink():
        raise DrillFixtureError("refusing to overwrite an existing backup")
    # Same bar as the drill (same helper): a key the drill would refuse is a
    # wasted boot, so refuse it before doing any work — in this module's own
    # error type, since the caller is dealing with the fixture, not the drill.
    try:
        audit_key = _read_private_key(audit_key_file)
    except ReleaseDrillError as error:
        raise DrillFixtureError(str(error)) from error
    # RESOLVED, because the launcher is exec'd with cwd=release_root and a
    # program path derived from here: a relative work dir would be re-resolved
    # against that new cwd and vanish (CI, 2026-08-25).
    work_dir = Path(work_dir).resolve()
    if work_dir.exists():
        raise DrillFixtureError("work directory must not already exist")
    work_dir.mkdir(parents=True)
    started_unix = int(time.time())

    installed = _install_artifact(Path(artifact).resolve(), work_dir / "releases", "fixture")
    release_root = installed["root"]
    state_dir = work_dir / "state"

    token = secrets.token_urlsafe(32)
    auth_file = work_dir / "auth.json"
    now = int(time.time())
    # Private from the moment it exists: it holds a chat credential.
    _create_private(auth_file, json.dumps({"tokens": [{
        "sha256": hashlib.sha256(token.encode()).hexdigest(),
        "principal": "drill-fixture",
        "tenant": FIXTURE_TENANT,
        "scopes": ["chat", "ops:read"] + (
            ["schedules:read", "schedules:write"] if migration_state else []),
        "tools": ["write_file"] if migration_state else [],
        "not_before_unix": now - 60,
        "expires_unix": now + 3600,
    }]}, sort_keys=True, separators=(",", ":")))

    provider = _MockProvider(migration_state=migration_state)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    port = _free_port()
    # The same deliberately-minimal environment the drill's probe builds, so
    # the state this fixture commits comes from a service configured the way
    # the drill will configure it — plus the mock provider endpoint. The
    # settings beyond the wiring are the drill's own (SERVICE_SETTINGS) and
    # are recorded in the evidence; the timers are the release's.
    environment = {
        key: value for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "PYTHONPATH"}
    }
    environment.update({
        "ANTHROPIC_API_KEY": "drill-fixture-provider-placeholder",
        "PI_ENDPOINT": provider.endpoint,
        "PI_AUDIT_KEY": audit_key,
        "PI_AUTH_FILE": str(auth_file),
        "PI_STATE": str(state_dir),
        "PI_HOST": "127.0.0.1",
        "PI_PORT": str(port),
        **SERVICE_SETTINGS,
        "PI_SYSTEM_FILE": str(work_dir / "no-system-prompt"),
    })
    log_path = work_dir / "service.log"
    turn_reply = None
    with log_path.open("w+") as log:
        process = subprocess.Popen(
            [str(release_root / "bin" / "sigil-pi")], cwd=release_root,
            env=environment, stdout=log, stderr=log, text=True)
        try:
            deadline = time.monotonic() + timeout_s
            base = f"http://127.0.0.1:{port}"
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                try:
                    status, ready = _request("GET", f"{base}/v1/ready", token,
                                             timeout=2)
                    if status == 200 and ready.get("status") == "ready":
                        break
                except (OSError, ValueError, urllib.error.URLError,
                        json.JSONDecodeError):
                    time.sleep(0.05)
            else:
                raise DrillFixtureError(
                    "fixture service never became ready: "
                    + log_path.read_text()[-3000:])
            if process.poll() is not None:
                raise DrillFixtureError(
                    "fixture service exited during startup: "
                    + log_path.read_text()[-3000:])
            status, turn = _request(
                "POST", f"{base}/v1/chat", token,
                payload={"session": FIXTURE_SESSION,
                         "message": "commit one real turn for the drill"},
                timeout=TURN_WAIT_SECONDS)
            if status != 200 or not turn.get("reply"):
                raise DrillFixtureError(f"fixture turn failed: {status} {turn}")
            turn_reply = turn["reply"]
            if provider.requests == 0:
                raise DrillFixtureError(
                    "the turn never reached the provider — nothing real was committed")
            if migration_state:
                for schedule in MIGRATION_SCHEDULES:
                    status, created = _request("POST", f"{base}/v1/schedules", token,
                                               payload=schedule)
                    if status != 201 or created.get("created") is not True:
                        raise DrillFixtureError(f"fixture schedule failed: {status} {created}")
                # A new schedule is immediately due. Wait for its real first
                # run; the long intervals then keep this metadata stable during
                # recovery, rather than relying on a scheduler timing race.
                deadline = time.monotonic() + TURN_WAIT_SECONDS
                while time.monotonic() < deadline:
                    _, held = _request("GET", f"{base}/v1/schedules", token)
                    entries = held.get("schedules", [])
                    if (len(entries) == len(MIGRATION_SCHEDULES)
                            and all(e.get("last_status") == "ok" and e.get("last_run")
                                    for e in entries)):
                        break
                    time.sleep(0.1)
                else:
                    raise DrillFixtureError("fixture schedules did not complete successfully")
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=DRAIN_WAIT_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            provider.shutdown()
            provider.server_close()
    if process.returncode != 0:
        raise DrillFixtureError(
            "fixture service did not drain cleanly: " + log_path.read_text()[-3000:])
    marker = state_dir / ".product-clean-shutdown.json"
    if not marker.is_file():
        raise DrillFixtureError("no clean-shutdown marker; the backup would be refused")

    # The OLD release's own tool, as docs/state-compatibility.md requires.
    tool_env = dict(os.environ)
    tool_env["PI_AUDIT_KEY"] = audit_key
    result = subprocess.run(
        [sys.executable, str(release_root / "app" / "state_tool.py"), "backup",
         "--state", str(state_dir), "--output", str(output)],
        cwd=release_root / "app", env=tool_env, capture_output=True, text=True)
    if result.returncode:
        raise DrillFixtureError(
            "state_tool backup failed: " + (result.stderr or result.stdout)[-2000:])

    manifest = json.loads(result.stdout) if result.stdout.strip() else {}
    committed = [entry for entry in manifest.get("files", [])
                 if str(entry.get("path", "")).startswith("sessions/")]
    if not committed:
        raise DrillFixtureError("backup carries no committed session")
    evidence = {
        "artifact_sha256": installed["sha256"],
        "artifact_version": installed["version"],
        "backup": str(output),
        "backup_sha256": _sha256(output),
        "committed_session_files": len(committed),
        # Whose conversation the turn was committed to: the one the drill
        # reads and continues in every phase.
        "tenant": FIXTURE_TENANT,
        "session": FIXTURE_SESSION,
        "turn_reply": turn_reply,
        "configuration": {
            **service_configuration(),
            "provider": "a stand-in for the model provider, on loopback",
        },
        "started_unix": started_unix,
        "completed_unix": int(time.time()),
    }
    if migration_state:
        from scripts.verify_recovery_state import archive_snapshot
        held = archive_snapshot(output, FIXTURE_TENANT, FIXTURE_SESSION)
        files = {name: {"size": len(body.encode()),
                        "sha256": hashlib.sha256(body.encode()).hexdigest()}
                 for name, body in MIGRATION_FILES.items()}
        if held["sandbox_files"] != files:
            raise DrillFixtureError("the release did not persist the requested sandbox bytes")
        schedules = list(held["schedules"].values())
        wanted = {item["name"]: item for item in MIGRATION_SCHEDULES}
        if (len(schedules) != len(wanted)
                or {item["name"] for item in schedules} != set(wanted)
                or any(any(item[field] != value for field, value in wanted[item["name"]].items())
                       or item["last_status"] != "ok" or not item["last_run"]
                       or item["lease_owner"] is not None or item["lease_until"] is not None
                       for item in schedules)):
            raise DrillFixtureError("the release did not persist the completed schedules")
        evidence["migration_state"] = held
        _, payloads = _backup_payloads(output)
        conversation = _messages(payloads[_session_files(FIXTURE_TENANT, FIXTURE_SESSION)[0]])
        evidence["scheduled_turns"] = _schedule_turn_evidence(provider.turn_messages, conversation)
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True,
                        help="the OLD release archive to boot and back up")
    parser.add_argument("--output", required=True)
    parser.add_argument("--audit-key-file", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--migration-state", action="store_true",
                        help="commit two sandbox files and two completed schedules through the release")
    args = parser.parse_args()
    try:
        evidence = produce_backup(
            artifact=args.artifact, output=args.output,
            audit_key_file=args.audit_key_file, work_dir=args.work_dir,
            migration_state=args.migration_state)
    except (OSError, DrillFixtureError) as error:
        parser.exit(2, f"drill fixture failed: {error}\n")
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    main()
