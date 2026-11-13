"""Release recovery drills are safe, digest-bound, and anti-vacuous.

This module also holds what the recovery-drill tests share: two release
archives, a pre-upgrade backup that holds a committed turn, and stand-in
releases (`_stand_ins`). A stand-in is the product's own HTTP service, auth
registry, quota store, session store and audit log on an agent that asks the
provider itself instead of through the compiler, so the drill's real probe
drives it over real HTTP, and each stand-in can be told what to get wrong.
tests/test_recovery_drill_real_bundle.py runs the drill against real bundles.
"""

import hashlib
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import tarfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from agent import AuditLog, SessionStore, ensure_directory, verify_audit_dir
from conftest import PI_ROOT
from product_service import (
    AuthRegistry,
    DurableQuotaStore,
    ProductDataManager,
    ProductError,
    ProductScheduleStore,
    ProductService,
    SessionOperationLocks,
    _internal_session,
    serve_product,
)
from scripts import release_drill
from scripts.build_release import APP_FILES, DOC_FILES, build_release
from scripts.release_drill import ReleaseDrillError, _install_artifact, run_drill
from state_tool import CLEAN_MARKER, create_backup


AUDIT_KEY = "release-drill-test-audit-key-with-sufficient-entropy"


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True,
        capture_output=True, text=True).stdout.strip()


def _fake_sigil(tmp_path):
    root = tmp_path / "SIGIL"
    (root / "crates").mkdir(parents=True)
    (root / "crates" / "runtime.txt").write_text("runtime source")
    stdlib = root / "stdlib" / "sigil"
    stdlib.mkdir(parents=True)
    for name in ("http", "json", "kv"):
        (stdlib / f"{name}.sigil").write_text(f"module sigil::{name};\n")
    binary = root / "target" / "release" / "sigil-mcp"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"fake immutable runtime")
    binary.chmod(0o755)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "crates", "stdlib"], check=True)
    subprocess.run([
        "git", "-C", str(root), "-c", "user.name=test", "-c",
        "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
    pin = tmp_path / "SIGIL_REV"
    pin.write_text(
        f"crates = {_git(root, 'rev-parse', 'HEAD:crates')}\n"
        f"stdlib = {_git(root, 'rev-parse', 'HEAD:stdlib')}\n"
        f"ref = {_git(root, 'rev-parse', 'HEAD')}\n")
    return root, pin


def _app_variant(tmp_path, name, version):
    root = tmp_path / name
    for relative in (
            *APP_FILES, *DOC_FILES,
            "config/auth.example.json", "config/alert-policy.json",
            "config/grafana-slo-dashboard.json"):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PI_ROOT / relative, destination)
    for source in (PI_ROOT / "tools").iterdir():
        if source.is_file() and source.suffix in (".sigil", ".json"):
            destination = root / "tools" / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    (root / "VERSION").write_text(version + "\n")
    return root


def _artifacts(tmp_path):
    sigil, pin = _fake_sigil(tmp_path)
    old = build_release(
        sigil_root=sigil, output_dir=tmp_path / "old-out",
        app_root=_app_variant(tmp_path, "old-app", "0.9.0"),
        pin_file=pin, build_runtime=False, platform_tag="test")[0]
    new = build_release(
        sigil_root=sigil, output_dir=tmp_path / "new-out",
        app_root=_app_variant(tmp_path, "new-app", "1.0.0"),
        pin_file=pin, build_runtime=False, platform_tag="test")[0]
    return old, new


# The tenant and the session whose conversation the drill reads and continues
# when it is told nothing else: the ones scripts/drill_fixture.py commits its
# turn in. Literals, so that a drill whose defaults moved fails a test.
TENANT = "drill-fixture-tenant"
SESSION = "drill-fixture-session"
COMMITTED = [
    {"role": "user", "content": "committed before upgrade"},
    {"role": "assistant",
     "content": [{"type": "text", "text": "the reply committed before upgrade"}]},
]
# A session of another tenant, which no phase addresses.
BYSTANDER = ("another-tenant", "bystander-session")
QUOTAS = dict(
    max_concurrent_turns=10, tokens_per_day=1000, token_reservation_per_turn=10,
    storage_bytes_per_tenant=1_000_000, storage_reservation_per_turn=10,
    audit_bytes_per_tenant=1_000_000, audit_reservation_per_turn=10)


def _backup(tmp_path, *, tenant=TENANT, session=SESSION, messages=COMMITTED):
    """A fresh backup of a drained state: `messages` committed in `session`
    of `tenant`, each with its audit chain and quota registry row, and one
    bystander session."""
    state = tmp_path / "source-state"
    store = SessionStore(state / "sessions")
    audit = AuditLog(state / "audit", key=AUDIT_KEY.encode())
    quota = DurableQuotaStore(state / "product-quotas.sqlite3", **QUOTAS)
    for owner, name, conversation in (
            (tenant, session, messages),
            (*BYSTANDER, [{"role": "user", "content": "a bystander's message"}])):
        internal = _internal_session(owner, name)
        store.save(internal, conversation)
        audit.record(internal, "tool", "source", "input", "output", None, None, 1)
        lease = quota.acquire_turn(owner)
        lease.release(
            session_id=internal, usage={"input_tokens": 1, "output_tokens": 1},
            storage_bytes=store._path(internal).stat().st_size,
            audit_bytes=audit._path(internal).stat().st_size)
    now = int(time.time())
    (state / CLEAN_MARKER).write_text(json.dumps({
        "schema_version": 1,
        "product_version": "0.9.0",
        "clean_shutdown_unix": now,
    }))
    backup = tmp_path / "state.tar.gz"
    create_backup(state, backup, audit_key=AUDIT_KEY.encode(), now=now)
    key = tmp_path / "audit-key"
    key.write_text(AUDIT_KEY)
    key.chmod(0o600)
    return backup, key, now


# ── stand-in releases ────────────────────────────────────────────────────

SLOW_TURN_SECONDS = 0.75
PHASES = ("clean_install", "restore_old", "upgrade_new", "rollback_old")
# What a stand-in can be told to get wrong. Each of TURN_FAULTS leaves a phase
# without a real turn in the session as it was committed, and each is caught
# by a different part of the drill's judgement.
TURN_FAULTS = (
    "forgets_history",   # reads every conversation as empty, as a release
                         # that cannot parse another release's sessions would
    "resets_sessions",   # empties every conversation it finds when it starts
    "hides_audit_record",  # serves every audit chain without its first record
    "silent_turn",       # answers the turn without asking the provider
    "asks_something_else",  # asks the provider, but not with the turn's message
    "sends_no_history",  # asks the provider with the new message alone
    "garbles_reply",     # returns and keeps a reply the provider did not give
    "fails_turn",        # the turn fails: 502 agent_failure
    "drops_committed",   # saves the conversation without its first message
    "alters_committed",  # saves the conversation with its first message changed
    "keeps_nothing",     # answers the turn and saves none of it
    "no_audit",          # commits the turn without an audit record
    "hides_session",     # the session's tenant is refused: 403
)
# Wrong only where there is a conversation to lose: not on a clean install.
NEEDS_HISTORY = {"resets_sessions", "hides_audit_record", "sends_no_history"}
ENDPOINT_FAULTS = ("metrics_fail", "version_fail")


class _StandInAgent:
    """Asks the provider itself, where the product's agent asks through the
    compiler, and keeps the conversation and its audit chain in the product's
    own stores."""

    manifest = {}
    memory = None
    _mcp = object()  # ProductService's readiness looks for a runtime here

    def __init__(self, state, key, endpoint, faults):
        self.store = SessionStore(state / "sessions")
        self.sandbox_root = ensure_directory(state / "sandboxes")
        self.audit = AuditLog(state / "audit", key=key)
        self.endpoint, self.faults = endpoint, faults
        if "forgets_history" in faults:
            self.store.load = lambda session: []
        if "resets_sessions" in faults:
            for path in sorted(self.store.dir.glob("*.kv")):
                path.write_bytes(b"[]")
        if "hides_audit_record" in faults:
            whole = self.audit.read
            self.audit.read = lambda session: whole(session)[1:]

    def turn_with_usage(self, session, message, **_):
        if "fails_turn" in self.faults:
            raise RuntimeError("the stand-in's turn failed")
        if "slow_turn" in self.faults:
            time.sleep(SLOW_TURN_SECONDS)
        messages = self.store.load(session) + [{"role": "user", "content": message}]
        if "silent_turn" in self.faults:
            reply = "a reply no provider gave"
        else:
            sent = messages[-1:] if "sends_no_history" in self.faults else messages
            if "asks_something_else" in self.faults:
                sent = messages[:-1] + [{"role": "user", "content": "something else"}]
            request = urllib.request.Request(
                self.endpoint, headers={"content-type": "application/json"},
                data=json.dumps({"model": "stand-in", "max_tokens": 64,
                                 "messages": sent}).encode())
            with urllib.request.urlopen(request, timeout=10) as response:
                reply = "".join(block["text"] for block in json.load(response)["content"]
                                if block.get("type") == "text")
        if "garbles_reply" in self.faults:
            reply += " (as the release retells it)"
        messages.append({"role": "assistant", "content": [{"type": "text", "text": reply}]})
        if "drops_committed" in self.faults:
            messages = messages[1:]
        if "alters_committed" in self.faults:
            messages[0] = {**messages[0], "content": "not what was committed"}
        if "volatile_turn" in self.faults:
            # Served from memory for as long as this start lasts; never written.
            on_disk = self.store.load
            self.store.load = lambda name: messages if name == session else on_disk(name)
        elif "keeps_nothing" not in self.faults:
            self.store.save(session, messages)
        if "no_audit" not in self.faults:
            self.audit.record(session, "llm", "stand-in", message, reply, None, None, 1)
        if "alters_bystander" in self.faults:
            for path in sorted(self.store.dir.glob("*.kv")):
                if path != self.store._path(session):
                    path.write_bytes(path.read_bytes().replace(b"bystander", b"bystandex"))
        return reply, {"input_tokens": 1, "output_tokens": 1}

    def turn_telemetry(self):
        return {}


class _StandInService(ProductService):
    """The product's service, with the two operational answers a stand-in can
    be told to fail. Each fails inside the real dispatch, which answers 500
    internal_error and logs the request as it does for any other."""

    faults = frozenset()

    def operational_metrics(self):
        if "metrics_fail" in self.faults:
            raise RuntimeError("the stand-in's metrics are broken")
        return super().operational_metrics()

    @property
    def version(self):
        if "version_fail" in self.faults:
            raise RuntimeError("the stand-in's version is broken")
        return self._version

    @version.setter
    def version(self, value):
        self._version = value


class _StandInBoot:
    """One start of a stand-in release, with what `subprocess.Popen` gives the
    drill: poll, send_signal, wait, kill and returncode. `requests` is the
    service's own log of every request it answered."""

    def __init__(self, faults, command, env, log):
        root = Path(command[0]).resolve().parent.parent
        self.version = (root / "app" / "VERSION").read_text().strip()
        self.faults, self.env, self.log = frozenset(faults), dict(env), log
        self.state = Path(env["PI_STATE"])
        self.requests, self.returncode, self.pid = [], None, os.getpid()
        key = env["PI_AUDIT_KEY"].encode()
        ensure_directory(self.state)
        (self.state / CLEAN_MARKER).unlink(missing_ok=True)
        if verify_audit_dir(self.state / "audit", key=key)["problems"]:
            self._say("existing audit chains failed signature verification")
            self.returncode = 1
            return
        agent = _StandInAgent(self.state, key, env.get("PI_ENDPOINT"), self.faults)
        schedules = ProductScheduleStore(self.state / "product-schedules.json")
        locks = SessionOperationLocks(self.state / "locks")
        quota = DurableQuotaStore(self.state / "product-quotas.sqlite3", **QUOTAS)
        data = ProductDataManager(agent, schedules, locks, quota_store=quota)
        if "hides_session" in self.faults:
            def refused(principal, session):
                raise ProductError(403, "forbidden", "the stand-in hides its sessions")
            data.export = refused
        if "no_export_route" in self.faults:
            def no_route(principal, session):
                raise ProductError(404, "not_found", "route not found")
            data.export = no_route
        polls = iter(range(10 ** 9))
        slow = 8 if "slow_ready" in self.faults else 0
        self.service = _StandInService(
            agent, AuthRegistry.from_file(env["PI_AUTH_FILE"]), version=self.version,
            schedule_store=schedules, session_locks=locks, quota_store=quota,
            data_manager=data, resource_root=self.state, log_sink=self.requests.append,
            readiness=lambda: "never_ready" not in self.faults and next(polls) >= slow,
            effective_settings=self._settings())
        self.service.faults = self.faults
        self.address = (env["PI_HOST"], int(env["PI_PORT"]))
        self.server = serve_product(self.service, host=self.address[0], port=self.address[1])
        self._say(f"stand-in {self.version} serving")

    def _settings(self):
        """What a release reports as in force, the way product_main builds
        it. Releases before audit 2026-11-10 report nothing."""
        if "reports_no_settings" in self.faults:
            return None
        given = {} if "ignores_settings" in self.faults else self.env
        return {
            "model": "stand-in",
            "endpoint_host": "127.0.0.1" if self.env.get("PI_ENDPOINT") else "api.anthropic.com",
            "turn_deadline_seconds": 120,
            "audit_verify_seconds": int(given.get("PI_AUDIT_VERIFY_SECONDS", 60)),
            "retention_days": int(given.get("PI_RETENTION_DAYS", 90)),
            "retention_interval_seconds": int(given.get("PI_RETENTION_INTERVAL_SECONDS", 3600)),
        }

    def _say(self, line):
        if hasattr(self.log, "write"):
            self.log.write(line + "\n")
            self.log.flush()

    def routes(self, tenant=None):
        """The routes this boot answered, in order: all of them, or those
        asked with a credential of `tenant`."""
        wanted = None if tenant is None else hashlib.sha256(tenant.encode()).hexdigest()
        return [f"{record['method']} {record['route']}" for record in self.requests
                if record.get("event") == "http_request"
                and (wanted is None or record["tenant_sha256"] == wanted)]

    def poll(self):
        return self.returncode

    def send_signal(self, signum):
        assert signum == signal.SIGTERM, signum
        if "slow_stop" in self.faults:
            time.sleep(SLOW_TURN_SECONDS)
        self.service.start_draining()
        # The listener looks for a shutdown request twice a second. A
        # connection wakes it, so that a test is not kept waiting for it.
        stopping = threading.Thread(target=self.server.shutdown)
        stopping.start()
        while stopping.is_alive():
            try:
                socket.create_connection(self.address, timeout=1).close()
            except OSError:
                pass
            stopping.join(0.01)
        drained = self.service.wait_for_drain(10)
        self.server.server_close()
        if not drained or "unclean_exit" in self.faults:
            self.returncode = 1
            return
        (self.state / CLEAN_MARKER).write_text(json.dumps({
            "schema_version": 1, "product_version": self.version,
            "clean_shutdown_unix": int(time.time())}))
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -signal.SIGKILL


class _StandIns:
    def __init__(self, faults):
        self.faults, self.boots = dict(faults), []

    def popen(self, command, **kwargs):
        boot = _StandInBoot(self.faults.get(len(self.boots), ()), command,
                            kwargs["env"], kwargs.get("stdout"))
        self.boots.append(boot)
        return boot


def _stand_ins(monkeypatch, faults=None):
    """Make every release the drill starts a stand-in. `faults` maps the
    ordinal of a start (0 is the clean install, 1 the restored old release, 2
    the candidate, 3 the rollback) to what that start gets wrong. Returns the
    record of every start."""
    stand_ins = _StandIns(faults or {})
    real = subprocess.Popen

    def popen(command, *args, **kwargs):
        if Path(command[0]).name == "sigil-pi":
            return stand_ins.popen(command, **kwargs)
        return real(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    return stand_ins


def _injected_probe(release, state, auth_file, token, audit_key, **kwargs):
    """The drill's own probe, handed to it as an injected one: the report
    must then say the probe was not the real service. On the way it checks
    what the drill hands every probe: a credentials file only its owner can
    read (it holds a chat credential in the customer's tenant), an operator
    token, and the audit key the drill was given."""
    assert Path(auth_file).stat().st_mode & 0o077 == 0
    assert token and audit_key == AUDIT_KEY
    return release_drill._probe_release(
        release, state, auth_file, token, audit_key, **kwargs)


def test_drill_installs_switches_restores_rolls_back_and_preserves_state(
        tmp_path, monkeypatch):
    _stand_ins(monkeypatch)
    old, new = _artifacts(tmp_path)
    backup, key, now = _backup(tmp_path)
    output = tmp_path / "report.json"
    report = run_drill(
        old_artifact=old, new_artifact=new, state_backup=backup,
        audit_key_file=key, output=output, work_dir=tmp_path / "work",
        probe=_injected_probe, now_fn=lambda: now)

    assert report["evaluation"]["mechanics_passed"] is True
    continuity = report["evaluation"]["continuity"]
    # Two sessions, each a conversation and an audit chain. The one the
    # phases continued grew and kept what was committed; the other is
    # byte-identical.
    assert {name: continuity[name] for name in (
        "committed_customer_files", "committed_session_files",
        "lost_or_changed", "preserved")} == {
        "committed_customer_files": 4,
        "committed_session_files": 2,
        "lost_or_changed": [],
        "preserved": True,
    }
    assert [phase["name"] for phase in report["phases"]] == [
        "clean_install", "restore_old", "upgrade_new", "rollback_old"]
    assert report["artifacts"]["old"]["version"] == "0.9.0"
    assert report["artifacts"]["new"]["version"] == "1.0.0"
    assert report["qualification_eligible"] is False
    assert report["qualification_failures"] == [
        "probe was injected rather than the real service"]
    assert json.loads(output.read_text()) == report
    assert output.stat().st_mode & 0o077 == 0
    assert (tmp_path / "work" / "current").resolve().name.startswith("old-")


def test_artifact_install_rejects_traversal_and_manifest_tampering(tmp_path):
    traversal = tmp_path / "traversal.tar.gz"
    with tarfile.open(traversal, "w:gz") as archive:
        data = b"bad"
        member = tarfile.TarInfo("../outside")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    with pytest.raises(ReleaseDrillError, match="unsafe archive path"):
        _install_artifact(traversal, tmp_path / "releases", "bad")
    assert not (tmp_path / "outside").exists()

    old, _ = _artifacts(tmp_path / "fixture")
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(old, "r:gz") as source, tarfile.open(tampered, "w:gz") as target:
        for member in source.getmembers():
            stream = source.extractfile(member) if member.isreg() else None
            data = stream.read() if stream is not None else None
            if member.name.endswith("/app/VERSION"):
                data = b"9.9.9\n"
                member.size = len(data)
            target.addfile(member, io.BytesIO(data) if data is not None else None)
    with pytest.raises(ReleaseDrillError, match="checksum failed"):
        _install_artifact(tampered, tmp_path / "releases", "tampered")


@pytest.mark.parametrize("mode,match", [
    (0o644, "group or other"),
    (0o600, "overwrite"),
])
def test_drill_fails_closed_on_private_input_or_output_contract(tmp_path, mode, match):
    old, new = _artifacts(tmp_path)
    backup, key, now = _backup(tmp_path)
    output = tmp_path / "report.json"
    if mode == 0o644:
        key.chmod(mode)
    else:
        output.write_text("existing evidence")
    with pytest.raises(ReleaseDrillError, match=match):
        run_drill(
            old_artifact=old, new_artifact=new, state_backup=backup,
            audit_key_file=key, output=output, probe=_injected_probe,
            now_fn=lambda: now)
