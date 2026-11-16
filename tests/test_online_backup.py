"""Backups are taken while the service runs, one session at a time under its
own lock.

Found by the 2026-11-10 product audit, F-03. `state_tool.py backup` refused a
state without the clean-shutdown marker, so every backup needed the service
stopped. The state-compatibility document asks for production backups no more
than 15 minutes apart, and the readiness evidence for 99.9% availability on
one worker. Thirty days of 15-minute backups is 2,880 stops; 99.9% allows 43.2
minutes of downtime in 30 days: 0.9 seconds a stop, for a drain that may wait
120 seconds for one turn. The three could not hold together.

`backup --online` and `backup-cycle --online` need no marker and no stop
(decision D2). The service serializes every operation on a session behind a
lock file under `locks/`, named from the session's internal id, and the
schedule store behind its own; another process can take the same locks.
Pinned here, on the real `state_tool`, the product's real stores, registry and
locks, and a stand-in for the agent that writes what a turn writes:

- a serving state is backed up, by another process, with no marker and no
  stop, and the archive restores, validates and boots ready;
- every byte of a session is read while that session's lock is held, one
  session at a time; the schedule store is read under its own lock; the
  ledger is captured last, through SQLite; a busy lock is queued for, not
  polled; a lock that cannot be taken within the wait fails the whole backup,
  and nothing is published;
- the ledger copy agrees with what was copied: a session created meanwhile is
  left out with its row, a session deleted meanwhile is whole or absent, turn
  leases are dropped, and each row records the bytes that were captured, so
  the restored tree's startup report stays quiet, while files that were
  already lost are still reported;
- nothing is silently left out: state no registry row attributes fails the
  backup, as offline, but a turn in flight whose row a deletion removed does
  not; scratch and lock files are never archived as state; and a backup
  directory that cannot keep two state file names apart fails the backup;
- the private copy the archive is made from is beside the output, private,
  never synced under a lock, and removed however the backup ends;
- the archive is schema 1: an online manifest only adds fields, and the
  offline one gains none;
- for any interleaving of turns, deletions, new sessions and schedule changes
  with a backup, the archive restores, boots ready with no critical event,
  every session in it is one the service really held at a turn boundary, and
  the service stays ready throughout.
"""
import collections
import contextlib
import errno
import fcntl
import hashlib
import io
import itertools
import json
import os
import random
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import example, given, settings, strategies as st

from conftest import PI_ROOT
from test_backup_durability import Syscalls, not_durable
from test_product_main import _install_happy_runtime

import product_main
import state_tool
from agent import AuditLog, SessionStore, ensure_directory, verify_audit_dir
from product_service import (
    AuditVerificationMonitor,
    AuthRegistry,
    DurableQuotaStore,
    ProductDataManager,
    ProductRetentionMonitor,
    ProductScheduleStore,
    ProductScheduler,
    ProductService,
    SessionOperationLocks,
    _internal_session,
)
from scripts.release_drill import _backup_payloads
from state_tool import (
    CLEAN_MARKER,
    StateToolError,
    create_backup,
    restore_backup,
    run_managed_backup_cycle,
)

AUDIT_KEY = b"online-backup-audit-key-0123456789abcdef"
A, B = TENANTS = ("tenant-a", "tenant-b")
TOKENS = {tenant: f"online-backup-token-for-{tenant}-with-enough-entropy" for tenant in TENANTS}
AUTH_ENTRIES = [{
    "sha256": hashlib.sha256(TOKENS[tenant].encode()).hexdigest(),
    "principal": f"{tenant}-user", "tenant": tenant,
    "scopes": ["chat", "sessions:read", "sessions:delete", "schedules:read",
               "schedules:write", "ops:read"],
    "tools": []} for tenant in TENANTS]
# The keys of every schema-1 manifest, and the ones an online backup adds.
SCHEMA_1_KEYS = {"schema_version", "product_version", "created_unix", "clean_shutdown_unix",
                 "audit_chains", "audit_records", "files"}
ONLINE_KEYS = {"mode", "online"}
CAPTURE_KEYS = {"sessions", "sessions_created_meanwhile", "sessions_deleted_meanwhile",
                "max_session_lock_hold_ms", "capture_seconds"}
SCHEDULE_LOCK = "product-schedules.json.lock"
UNATTRIBUTED = "customer state is missing quota registry entries"


# ── a product service that is serving ────────────────────────────────────


def session(external, tenant=A):
    """The names one product session goes by on disk."""
    internal = _internal_session(tenant, external)
    digest = hashlib.sha256(internal.encode()).hexdigest()
    return SimpleNamespace(external=external, tenant=tenant, internal=internal,
                           digest=digest, lock=f"{digest}.lock")


def session_files(tree, digest):
    """Every file of the session `digest` in `tree`, by relative path: its
    conversation, its audit chain and the files in its sandbox."""
    tree = Path(tree)
    found = {}
    for relative in (f"sessions/{digest}.kv", f"audit/{digest}.jsonl"):
        if (tree / relative).is_file():
            found[relative] = (tree / relative).read_bytes()
    sandbox = tree / "sandboxes" / digest
    if sandbox.is_dir():
        for path in sorted(sandbox.rglob("*")):
            if path.is_file():
                found[path.relative_to(tree).as_posix()] = path.read_bytes()
    return found


def turns_in(files, digest):
    """How many turns each surface of a session records: (conversation, audit
    chain, the sandbox's counter, the sandbox's notes). TurnAgent keeps them
    equal at every turn boundary, and only there."""
    conversation = json.loads(files.get(f"sessions/{digest}.kv", b"[]"))
    return (len(conversation) // 2,
            files.get(f"audit/{digest}.jsonl", b"").count(b"\n"),
            int(files.get(f"sandboxes/{digest}/turns.txt", b"0")),
            sum(1 for name in files if "/notes/" in name))


class TurnAgent:
    """Stands in for PiAgent at the product boundary, over the real stores.

    A turn does what a real one does to the session's files, in the same
    order: its sandbox directory first, then a signed audit record (a forge),
    then the sandbox's files (a tool: one rewritten, one added), and the
    conversation last. Between the first of those and the last the surfaces
    disagree, so a copy taken without the session's lock can be told from one
    taken at a turn boundary. `history` keeps every state a session was left
    in by a completed turn, read while the turn still holds the session's
    lock."""

    manifest = {}
    memory = None
    _mcp = object()

    def __init__(self, state):
        self.state = Path(state)
        self.store = SessionStore(self.state / "sessions")
        self.sandbox_root = ensure_directory(self.state / "sandboxes")
        self.audit = AuditLog(self.state / "audit", key=AUDIT_KEY)
        self.history = collections.defaultdict(list)
        self._pauses = {}
        self._guard = threading.Lock()

    def pause_next_turn(self, internal, at="midway"):
        """Make the next turn on `internal` stop partway: "started", with
        only its sandbox directory made, or "midway", with its audit record
        and its sandbox written and its conversation not yet saved. Returns
        (reached, proceed): the first is set when the turn has stopped, and
        setting the second lets it finish."""
        reached, proceed = threading.Event(), threading.Event()
        self._pauses[internal] = (at, reached, proceed)
        return reached, proceed

    def _pause(self, internal, point):
        if self._pauses.get(internal, ("",))[0] == point:
            _, reached, proceed = self._pauses.pop(internal)
            reached.set()
            assert proceed.wait(60), "a paused turn was never released"

    def turn_with_usage(self, internal, message, *, allowed_tools=None,
                        deadline_monotonic=None):
        digest = hashlib.sha256(internal.encode()).hexdigest()
        sandbox = self.sandbox_root / digest
        sandbox.mkdir(exist_ok=True)          # PiAgent.sandbox_for, as a turn begins
        self._pause(internal, "started")
        conversation = self.store.load(internal)
        turn = len(conversation) // 2 + 1
        self.audit.record(internal, "llm", "source", message, f"reply {turn}", None, None, 1)
        (sandbox / "notes").mkdir(exist_ok=True)
        (sandbox / "turns.txt").write_text(str(turn))
        (sandbox / "notes" / f"turn-{turn}.txt").write_text(message)
        self._pause(internal, "midway")
        self.store.save(internal, [*conversation, {"role": "user", "content": message},
                                   {"role": "assistant", "content": f"reply {turn}"}])
        with self._guard:
            self.history[digest].append(session_files(self.state, digest))
        return f"reply {turn}", {"input_tokens": 3, "output_tokens": 2}

    def turn_telemetry(self):
        return {}


class Serving:
    """A product service over real stores in `state`, wired as product_main
    wires them: the session store, sandboxes and signed audit log, the durable
    quota registry, the schedule store, and the session locks under `locks/`.
    TurnAgent stands in for the agent. Nothing is started: a test drives it
    through `dispatch`, from whichever thread it wants a request on. No
    clean-shutdown marker is ever written: this state is serving."""

    def __init__(self, state):
        self.state = Path(state)
        self.agent = TurnAgent(self.state)
        self.quota = DurableQuotaStore(
            self.state / "product-quotas.sqlite3", requests_per_minute=10**6,
            max_concurrent_turns=64, tokens_per_day=10**9, token_reservation_per_turn=1000,
            storage_bytes_per_tenant=1 << 30, storage_reservation_per_turn=1 << 20,
            audit_bytes_per_tenant=1 << 28, audit_reservation_per_turn=1 << 16)
        self.schedules = ProductScheduleStore(self.state / "product-schedules.json")
        self.locks = SessionOperationLocks(self.state / "locks")
        self.manager = ProductDataManager(
            self.agent, self.schedules, self.locks, quota_store=self.quota)
        self.auth = AuthRegistry(AUTH_ENTRIES)
        self.audit_monitor = AuditVerificationMonitor(
            lambda: verify_audit_dir(self.state / "audit", key=AUDIT_KEY),
            log_sink=lambda record: None)
        self.service = ProductService(
            self.agent, self.auth, quota_store=self.quota, session_locks=self.locks,
            data_manager=self.manager, schedule_store=self.schedules,
            audit_monitor=self.audit_monitor, turn_deadline_s=60,
            log_sink=lambda record: None)
        self.scheduler = ProductScheduler(
            self.schedules, self.agent, self.auth, session_locks=self.locks,
            quota_store=self.quota, data_manager=self.manager, turn_deadline_s=60,
            log_sink=lambda record: None)
        self._messages = itertools.count(1)
        # Every content the schedule store has held, as its saves left it.
        self.schedule_versions = []
        save = self.schedules._save

        def recording_save(data):
            save(data)
            self.schedule_versions.append(self.schedules.path.read_bytes())

        self.schedules._save = recording_save
        assert self.audit_monitor.check()

    def _dispatch(self, method, path, tenant, body=None):
        return self.service.dispatch(
            method, path, {"Authorization": f"Bearer {TOKENS[tenant]}"},
            b"" if body is None else json.dumps(body).encode())

    def chat(self, external, tenant=A):
        return self._dispatch("POST", "/v1/chat", tenant, {
            "session": external,
            "message": f"message {next(self._messages)} for {external}"})[0]

    def delete(self, external, tenant=A):
        return self._dispatch("DELETE", f"/v1/sessions/{external}", tenant)[0]

    def schedule(self, name, external, tenant=A):
        return self._dispatch("POST", "/v1/schedules", tenant, {
            "name": name, "session": external, "message": f"scheduled by {name}",
            "every_ms": 3_600_000})[0]

    def unschedule(self, name, tenant=A):
        return self._dispatch("DELETE", f"/v1/schedules/{name}", tenant)[0]

    def ready(self, verify=True):
        """Whether GET /v1/ready answers 200. `verify` first runs the audit
        verification the product runs on a timer, so that readiness reflects
        the chains as they are now."""
        if verify:
            self.audit_monitor.check()
        return self._dispatch("GET", "/v1/ready", A)[0] == 200

    def registered(self):
        return {row[1] for row in self.quota.registered_sessions()}


def online_backup(state, output, **options):
    """What an operator runs while the service serves."""
    return create_backup(state, output, audit_key=AUDIT_KEY, online=True, **options)


def cli(*arguments):
    """state_tool's command line, run in this process with the audit key in
    its environment: (exit status, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PI_AUDIT_KEY", AUDIT_KEY.decode())
        patch.setattr(sys, "argv", ["state_tool.py", *map(str, arguments)])
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                state_tool.main()
                status = 0
            except SystemExit as stopped:
                status = stopped.code
    return status, out.getvalue(), err.getvalue()


def ledger_rows(tree):
    """{session digest: (storage bytes, audit bytes)} as the registry in
    `tree` records them."""
    path = Path(tree) / "product-quotas.sqlite3"
    if not path.exists():
        return {}
    with closing(sqlite3.connect(path)) as db:
        return {digest: (storage, audit) for digest, storage, audit in db.execute(
            "SELECT session, storage_bytes, audit_bytes FROM session_usage")}


def ledger_count(tree, table):
    with closing(sqlite3.connect(Path(tree) / "product-quotas.sqlite3")) as db:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def members(archive):
    """{member name: bytes} of a backup archive, in archive order."""
    with tarfile.open(archive, "r:gz") as opened:
        return {member.name: opened.extractfile(member).read()
                for member in opened.getmembers()}


def boot(state):
    """Start the product on `state` through product_main.run(), over
    test_product_main's stand-ins for the compiler, the agent, the scheduler
    and the listener, with the real startup reconciliation, audit
    verification and retention sweep. Returns (the structured events it
    printed, the readiness dependencies when it began to serve)."""
    out = io.StringIO()
    with pytest.MonkeyPatch.context() as patch, tempfile.TemporaryDirectory() as config:
        auth = Path(config) / "auth.json"
        auth.write_text(json.dumps({"tokens": [
            dict(entry, not_before_unix=int(time.time()) - 60,
                 expires_unix=int(time.time()) + 86400) for entry in AUTH_ENTRIES]}))
        patch.setenv("ANTHROPIC_API_KEY", "provider-test-key")
        patch.setenv("PI_AUDIT_KEY", AUDIT_KEY.decode())
        patch.setenv("PI_AUTH_FILE", str(auth))
        patch.setenv("PI_STATE", str(state))
        happened = _install_happy_runtime(patch)
        patch.setattr(product_main, "ProductRetentionMonitor", ProductRetentionMonitor)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            product_main.run()
    events = [json.loads(line) for line in out.getvalue().splitlines()
              if line.startswith("{")]
    return events, dict(happened)["readiness_dependencies"]


def boots_quietly(restored):
    """Boot the product on a restored tree. It must come up ready, report no
    critical event, and find the ledger already in agreement with the files:
    startup sets every row to what is on disk, so a row it had to change is a
    row the backup got wrong. Returns the reconciliation event."""
    before = ledger_rows(restored)
    events, dependencies = boot(restored)
    critical = [event for event in events if event.get("severity") == "critical"]
    assert critical == [], (
        f"the product started on the restored tree with critical events: {critical}")
    reconciliation = [event for event in events if event.get("event") == "state_reconciliation"]
    assert [event["status"] for event in reconciliation] == ["ok"], reconciliation
    assert dependencies and all(dependencies.values()), (
        f"the product was not ready on the restored tree: {dependencies}")
    assert ledger_rows(restored) == before, (
        "startup had to correct the restored ledger: the rows in the archive did "
        "not record the bytes of the files in the archive")
    return reconciliation[0]


def free(lock_path):
    """Whether another process could take the advisory lock on `lock_path`
    now. Taken and released at once when it could."""
    with Path(lock_path).open("a+b") as probe:
        try:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        return True


def becomes_free(lock_path, within=30.0):
    """Whether the lock is free, now or within `within` seconds. A backup
    that gave up waiting for a lock was queued for it: once the holder lets
    go, the queued request is granted the lock and gives it straight back,
    so for an instant after a timeout the lock can still be seen taken."""
    deadline = time.monotonic() + within
    while not free(lock_path):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.005)
    return True


class Interleave:
    """Lets a test act on the live service at the points of an online backup
    where another client could: around each lock the backup takes, and around
    its snapshot of the ledger. The backup runs on the calling thread.

    `before`, `held` and `after` map a lock (its position among the locks
    taken, or its file name) to callables run on the backup's own thread:
    before it starts waiting for the lock, while it holds it, and once it has
    let go. `during` callables are each started on a new thread while the
    lock is held and joined once it is released, so they may block on that
    lock, as a client's request would. `before_snapshot` and `after_snapshot`
    run either side of the ledger's capture; callables whose position no lock
    reached run before the snapshot too. `at_every_point(phase)` is called
    at each of those points, on a thread of its own for "during". `log`
    records the order of it all."""

    def __init__(self, patch, state, at_every_point=None):
        self.state = Path(state).resolve()
        self.at_every_point = at_every_point or (lambda phase: None)
        self.before, self.held, self.during, self.after = (
            collections.defaultdict(list) for _ in range(4))
        self.before_snapshot, self.after_snapshot = [], []
        self.locks, self.log, self.failures = [], [], []
        real_lock = state_tool._product_lock
        real_snapshot = state_tool._snapshot_quota_database

        @contextmanager
        def lock(path, wait_s):
            name = Path(path).name
            position = len(self.locks)
            self.locks.append(name)
            self.at_every_point("before")
            self._run(self.before, position, name)
            started = []
            try:
                with real_lock(path, wait_s):
                    self.log.append(("lock", name))
                    self._run(self.held, position, name)
                    started = [self._start(action) for action in (
                        lambda: self.at_every_point("during"),
                        *self._take(self.during, position, name))]
                    yield
                    self.log.append(("unlock", name))
            finally:
                for thread in started:
                    thread.join(60)
                    assert not thread.is_alive(), "a client thread never finished"
            self._run(self.after, position, name)
            self.at_every_point("after")

        def snapshot(path, destination):
            if Path(path).parent != self.state:
                return real_snapshot(path, destination)     # the staged copy's own
            for pending in (self.before, self.held, self.during, self.after):
                for key in sorted(k for k in pending if isinstance(k, int)):
                    for action in pending.pop(key):
                        action()
            for action in self.before_snapshot:
                action()
            self.at_every_point("before")
            real_snapshot(path, destination)
            self.log.append(("snapshot",))
            self.at_every_point("after")
            for action in self.after_snapshot:
                action()

        patch.setattr(state_tool, "_product_lock", lock)
        patch.setattr(state_tool, "_snapshot_quota_database", snapshot)

    @staticmethod
    def _take(actions, position, name):
        return actions.pop(position, []) + actions.pop(name, [])

    def _run(self, actions, position, name):
        for action in self._take(actions, position, name):
            action()

    def _start(self, action):
        def run():
            try:
                action()
            except BaseException as error:  # noqa: BLE001 - reported by the test
                self.failures.append(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread


def admit_and_wait(serving, external, tenant=A):
    """Start a chat turn and stop it where a turn stops for an instant in the
    product: admitted (its registry row and its lease exist) and not yet at
    its session lock. Returns (thread, statuses, release); the row is there
    when this returns."""
    waiting, go = threading.Event(), threading.Event()
    real = serving.locks.hold
    statuses = []

    @contextmanager
    def hold(internal, **options):
        if threading.current_thread() is thread:
            waiting.set()
            assert go.wait(60), "an admitted turn was never released"
        with real(internal, **options):
            yield

    thread = threading.Thread(
        target=lambda: statuses.append(serving.chat(external, tenant)), daemon=True)
    serving.locks.hold = hold
    thread.start()
    assert waiting.wait(30), "the turn never reached its session lock"

    def release():
        go.set()
        thread.join(60)
        assert not thread.is_alive() and statuses == [200], statuses

    return thread, statuses, release


def populated(root, sessions=("project", "notes", "plans")):
    """A serving state with a schedule and two completed turns in each of
    `sessions` for tenant A, and one session for tenant B."""
    serving = Serving(Path(root) / "state")
    for external in sessions:
        assert serving.chat(external) == 200 and serving.chat(external) == 200
    assert serving.chat("theirs", B) == 200
    assert serving.schedule("daily", sessions[0]) == 201
    return serving


def by_digest(serving, externals, tenant=A):
    """The sessions named `externals`, in the order of their digests, which
    is the order a backup reaches them in."""
    return sorted((session(external, tenant) for external in externals),
                  key=lambda item: item.digest)


# ── the defect: a backup needed the service stopped (audit F-03) ─────────


def test_a_serving_state_is_backed_up_without_a_stop_and_restores(tmp_path):
    """The whole of F-03's direction, through the command an operator runs:
    no clean-shutdown marker, a turn's lease still in the ledger, and the
    archive restores, validates and boots ready."""
    serving = populated(tmp_path)
    lease = serving.quota.acquire_turn(A)        # a turn admitted and still in flight
    assert not (serving.state / CLEAN_MARKER).exists()
    archive = tmp_path / "backups" / "online.tar.gz"
    status, out, err = cli("backup", "--state", serving.state, "--output", archive, "--online")
    assert status == 0, (
        f"`state_tool.py backup --online` on the state of a service that is "
        f"serving exited {status}: {err.strip()!r}. Backups still need the service "
        f"stopped, and the 15-minute backup interval and the 99.9% availability "
        f"target cannot both survive that (audit F-03)")
    lease.release()
    manifest = json.loads(out)
    assert manifest["schema_version"] == 1 and manifest["mode"] == "online"
    assert manifest["online"]["sessions"] == 4
    assert serving.ready(), "the service stopped being ready"
    restored = tmp_path / "restored"
    assert restore_backup(archive, restored, audit_key=AUDIT_KEY) == manifest
    for item in (*by_digest(serving, ("project", "notes", "plans")), session("theirs", B)):
        files = session_files(restored, item.digest)
        assert files == session_files(serving.state, item.digest) and len(files) >= 4
    assert (restored / "product-schedules.json").read_bytes() == (
        serving.state / "product-schedules.json").read_bytes()
    assert boots_quietly(restored)["sessions"] == 4


def test_without_the_online_flag_a_serving_state_is_still_refused(tmp_path):
    """The offline path is unchanged and stays for upgrades: it accepts only
    a state a clean drain vouches for."""
    serving = populated(tmp_path)
    archive = tmp_path / "offline.tar.gz"
    with pytest.raises(StateToolError, match="no clean-shutdown marker"):
        create_backup(serving.state, archive, audit_key=AUDIT_KEY)
    status, out, err = cli("backup", "--state", serving.state, "--output", archive)
    assert (status, out) == (2, "") and "no clean-shutdown marker" in err
    assert not archive.exists()


def test_an_external_process_backs_up_a_service_that_serves_over_its_socket(
        tmp_path, product_server):
    """As deployed: the service answers on its listener, a client keeps
    running turns, and `state_tool.py backup --online` runs as another
    process, which is what makes the lock files the only thing the two
    share. Every request made meanwhile is answered, readiness included."""
    serving = populated(tmp_path, sessions=("s0", "s1", "s2"))
    server = product_server(serving.service)
    headers = {"Authorization": f"Bearer {TOKENS[A]}", "Content-Type": "application/json"}
    stop = threading.Event()
    chats, readies = [], []

    def client():
        for n in itertools.count():
            if stop.is_set():
                return
            body = json.dumps({"session": f"s{n % 3}", "message": f"turn {n}"}).encode()
            try:
                chats.append(server.request(
                    "POST", "/v1/chat", headers=headers, body=body, timeout=60).status)
            except OSError as error:
                chats.append(repr(error))

    def prober():
        while not stop.is_set():
            try:
                readies.append(server.request(
                    "GET", "/v1/ready", headers=headers, timeout=60).status)
            except OSError as error:
                readies.append(repr(error))
            time.sleep(0.005)

    threads = [threading.Thread(target=target, daemon=True) for target in (client, prober)]
    for thread in threads:
        thread.start()
    archive = tmp_path / "backups" / "online.tar.gz"
    try:
        done = subprocess.run(
            [sys.executable, str(PI_ROOT / "state_tool.py"), "backup", "--online",
             "--state", str(serving.state), "--output", str(archive)],
            env={"PI_AUDIT_KEY": AUDIT_KEY.decode(), "PATH": os.environ["PATH"]},
            capture_output=True, text=True, timeout=120)
    finally:
        stop.set()
        for thread in threads:
            thread.join(60)
    assert done.returncode == 0, (
        f"`state_tool.py backup --online`, run as another process against a "
        f"service that was serving, exited {done.returncode}: "
        f"{done.stderr.strip()!r} (audit F-03)")
    assert chats and set(chats) == {200}, f"turns failed during the backup: {set(chats)}"
    assert readies and set(readies) == {200}, (
        f"the service was not ready throughout the backup: {collections.Counter(readies)}")
    manifest = json.loads(done.stdout)
    assert manifest["mode"] == "online" and manifest["online"]["sessions"] == 4
    restored = tmp_path / "restored"
    restore_backup(archive, restored, audit_key=AUDIT_KEY)
    for name in ("s0", "s1", "s2"):
        digest = session(name).digest
        files = session_files(restored, digest)
        assert files in serving.agent.history[digest], (
            f"session {name} was restored as {turns_in(files, digest)} turns "
            f"(conversation, chain, counter, notes): not a state it was ever left "
            f"in by a completed turn")
    boots_quietly(restored)


def test_the_managed_cycle_backs_up_a_serving_state(tmp_path):
    serving = populated(tmp_path)
    backups = tmp_path / "managed"
    status, out, err = cli("backup-cycle", "--state", serving.state, "--backup-dir", backups,
                           "--retention-days", "1", "--online")
    assert status == 0, (
        f"`state_tool.py backup-cycle --online` on a serving state exited "
        f"{status}: {err.strip()!r} (audit F-03)")
    result = json.loads(out)
    assert result["mode"] == "online" and set(result["online"]) == CAPTURE_KEYS
    name = result["created"]["name"]
    assert state_tool.MANAGED_BACKUP_RE.fullmatch(name)
    assert name == f"sigil-pi-state-{result['created']['created_unix']:010d}-" \
                   f"{hashlib.sha256((backups / name).read_bytes()).hexdigest()}.tar.gz"
    assert sorted(p.name for p in backups.iterdir()) == sorted(
        [name, state_tool.BACKUP_LOCK_NAME]), "the cycle left something else behind"
    restored = tmp_path / "restored"
    manifest = restore_backup(backups / name, restored, audit_key=AUDIT_KEY)
    assert manifest["online"] == result["online"]
    assert manifest["created_unix"] == result["created"]["created_unix"]
    boots_quietly(restored)


def test_an_online_cycle_deletes_only_once_its_archive_is_durable(tmp_path, monkeypatch):
    """B1's order holds in the online mode: the expired archive goes only
    after the new one's bytes and name are synced."""
    serving = populated(tmp_path)
    backups = tmp_path.resolve() / "managed"
    first = run_managed_backup_cycle(serving.state, backups, audit_key=AUDIT_KEY,
                                     retention_s=100, now=1000, online=True)
    expired = backups / first["created"]["name"]
    calls = Syscalls(monkeypatch)
    second = run_managed_backup_cycle(serving.state, backups, audit_key=AUDIT_KEY,
                                      retention_s=100, now=1200, online=True)
    assert [item["name"] for item in second["deleted"]] == [expired.name]
    final = backups / second["created"]["name"]
    assert not_durable(calls.events, final, calls.index(("unlink", expired))) is None
    assert sorted(p.name for p in backups.iterdir()) == sorted(
        [final.name, state_tool.BACKUP_LOCK_NAME])


# ── one session at a time, each under its own lock ───────────────────────


def test_every_byte_of_live_state_is_read_under_the_lock_that_guards_it(
        tmp_path, monkeypatch):
    """THE LOCK GUARD. Every file of the live state the backup opens is a
    session's, opened while that session's lock is held, or the schedule
    store, opened under the store's lock. Nothing else in the live state is
    opened at all: the validation and the archive read the private copy.
    (The ledger is SQLite's to open, and is not seen here.)"""
    serving = populated(tmp_path)
    state = serving.state.resolve()
    registered = sorted(serving.registered())
    probing = threading.local()
    opened = []

    def guard_of(relative):
        top, name = relative.parts[0], relative.name
        if top == "sessions" and name.endswith(".kv"):
            return state / "locks" / f"{name[:-3]}.lock"
        if top == "audit" and name.endswith(".jsonl"):
            return state / "locks" / f"{name[:-6]}.lock"
        if top == "sandboxes" and len(relative.parts) > 2:
            owner = [digest for digest in registered if digest == relative.parts[1]]
            return state / "locks" / f"{owner[0]}.lock" if owner else None
        if relative.as_posix() == "product-schedules.json":
            return state / SCHEDULE_LOCK
        return None

    def note(path):
        if isinstance(path, int) or getattr(probing, "active", False):
            return
        try:
            relative = Path(os.path.abspath(os.fsdecode(path))).relative_to(state)
        except ValueError:
            return
        if relative.parts[0] == "locks" or relative.as_posix() == SCHEDULE_LOCK:
            return
        guard = guard_of(relative)
        probing.active = True
        try:
            opened.append((relative.as_posix(), None if guard is None else not free(guard)))
        finally:
            probing.active = False

    def noting(real):
        def call(path, *args, **kwargs):
            note(path)
            return real(path, *args, **kwargs)
        return call

    monkeypatch.setattr(os, "open", noting(os.open))
    monkeypatch.setattr(io, "open", noting(io.open))
    monkeypatch.setattr("builtins.open", noting(open))
    online_backup(serving.state, tmp_path / "online.tar.gz")
    monkeypatch.undo()
    unlocked = sorted(name for name, locked in opened if locked is False)
    assert unlocked == [], (
        f"the backup read live state without the lock that guards it: {unlocked}")
    unknown = sorted(name for name, locked in opened if locked is None)
    assert unknown == [], f"the backup opened live files that are not state: {unknown}"
    read = {name for name, locked in opened}
    for digest in registered:
        assert {f"sessions/{digest}.kv", f"audit/{digest}.jsonl",
                f"sandboxes/{digest}/turns.txt"} <= read, "anti-vacuity"
    assert "product-schedules.json" in read, "anti-vacuity"


def test_one_session_is_locked_at_a_time_with_the_lock_its_turns_take(tmp_path):
    """While the backup copies a session, a turn on it waits (the product's
    own lock table, as another worker would hold it, times out), and every
    other session's lock is free."""
    serving = populated(tmp_path)
    sessions = by_digest(serving, ("project", "notes", "plans")) + [session("theirs", B)]
    worker = SessionOperationLocks(serving.state / "locks")
    seen = {}

    def probe(holding):
        def run():
            taken = []
            for item in sessions:
                try:
                    with worker.hold(item.internal,
                                     deadline_monotonic=time.monotonic() + 0.05):
                        pass
                except TimeoutError:
                    taken.append(item.external)
            seen[holding.external] = taken
        return run

    with pytest.MonkeyPatch.context() as patch:
        steps = Interleave(patch, serving.state)
        for item in sessions:
            steps.held[item.lock].append(probe(item))
        online_backup(serving.state, tmp_path / "online.tar.gz")
    assert seen == {item.external: [item.external] for item in sessions}, (
        f"while the backup held each session's lock, the product's lock table "
        f"found these sessions locked: {seen}")
    assert steps.locks == sorted(item.lock for item in sessions) + [SCHEDULE_LOCK]


def test_a_turn_in_flight_is_waited_for_and_captured_whole(tmp_path):
    """A turn holds its session's lock from its first write to its last. The
    backup waits for it, and what it archives is the session as the turn left
    it, never the torn state in between."""
    serving = Serving(tmp_path / "state")
    item = session("project")
    assert serving.chat("project") == 200
    reached, proceed = serving.agent.pause_next_turn(item.internal)
    turn = threading.Thread(target=serving.chat, args=("project",), daemon=True)
    turn.start()
    assert reached.wait(30)
    torn = turns_in(session_files(serving.state, item.digest), item.digest)
    assert torn == (1, 2, 2, 2), f"anti-vacuity: the turn is midway, saw {torn}"
    waiting = threading.Event()
    result = {}

    def backup():
        try:
            with pytest.MonkeyPatch.context() as patch:
                Interleave(patch, serving.state).before[item.lock].append(waiting.set)
                result["manifest"] = online_backup(
                    serving.state, tmp_path / "online.tar.gz", lock_wait_s=60)
        except BaseException as error:  # noqa: BLE001 - reported by the assertions below
            result["error"] = error

    runner = threading.Thread(target=backup, daemon=True)
    runner.start()
    try:
        while not waiting.wait(0.02):
            assert runner.is_alive(), f"the backup ended before it reached the session: {result}"
        runner.join(0.3)
        assert runner.is_alive(), (
            f"the backup did not wait for the turn in flight: {result}")
    finally:
        proceed.set()
        turn.join(60)
    runner.join(60)
    assert not runner.is_alive() and "manifest" in result, result
    restored = tmp_path / "restored"
    restore_backup(tmp_path / "online.tar.gz", restored, audit_key=AUDIT_KEY)
    files = session_files(restored, item.digest)
    assert turns_in(files, item.digest) == (2, 2, 2, 2)
    assert files == serving.agent.history[item.digest][-1]
    assert result["manifest"]["online"]["max_session_lock_hold_ms"] > 0
    boots_quietly(restored)


def test_a_session_lock_that_cannot_be_taken_fails_the_whole_backup(tmp_path):
    """The wait is bounded, and a session is never skipped: with one session's
    lock out of reach there is no archive at all, and no copy left behind."""
    serving = populated(tmp_path)
    busy = session("notes")
    output = tmp_path / "backups" / "online.tar.gz"
    output.parent.mkdir()
    with serving.locks.hold(busy.internal):
        started = time.monotonic()
        with pytest.raises(StateToolError) as refused:
            online_backup(serving.state, output, lock_wait_s=0.2)
        waited = time.monotonic() - started
        assert str(refused.value) == (
            f"could not lock {busy.lock} within 0.2 seconds; the backup was not taken")
        assert 0.2 <= waited < 20, f"the backup waited {waited:.2f}s for a 0.2s bound"
        assert list(output.parent.iterdir()) == [], (
            "a backup that could not lock a session published or left something")
        status, out, err = cli("backup", "--state", serving.state, "--output", output,
                               "--online", "--lock-wait-seconds", "0.05")
        assert (status, out) == (2, "") and err == (
            f"state operation failed: could not lock {busy.lock} within 0.05 seconds; "
            f"the backup was not taken\n")
        assert list(output.parent.iterdir()) == []
    # Each of those waits was queued for the lock when it gave up. Now that the
    # turn has let go, each is granted the lock in its turn, and must give it
    # straight back: a wait that kept it would hold the session for good.
    assert becomes_free(serving.state / "locks" / busy.lock), (
        "a wait that was given up holds the lock")
    assert serving.chat("notes") == 200
    assert online_backup(serving.state, output, lock_wait_s=5)["online"]["sessions"] == 4


def test_a_busy_lock_is_queued_for_once_and_never_polled(tmp_path, monkeypatch):
    """The product hands a busy session's lock from one queued turn to the
    next within microseconds. A backup that polled for it would have to land
    in that gap: against three clients on one session, a 10 ms poll took
    seconds, or failed, where a queued request is granted the lock at the
    first handoff (measured; see the audit ledger). So a busy lock is asked
    for once without waiting and then once more, in the kernel's queue."""
    serving = populated(tmp_path)
    busy = session("notes")
    real = fcntl.flock
    asked = []

    def flock(descriptor, operation):
        try:
            real(descriptor, operation)
        except BlockingIOError:
            asked.append((operation, "busy"))
            raise
        asked.append((operation, "done"))

    done = {}

    def backup():
        done["manifest"] = online_backup(
            serving.state, tmp_path / "online.tar.gz", lock_wait_s=60)

    with serving.locks.hold(busy.internal):
        monkeypatch.setattr(fcntl, "flock", flock)
        runner = threading.Thread(target=backup, daemon=True)
        runner.start()
        deadline = time.monotonic() + 30
        while (fcntl.LOCK_EX | fcntl.LOCK_NB, "busy") not in asked:
            assert time.monotonic() < deadline and runner.is_alive(), asked
            time.sleep(0.005)
        time.sleep(0.3)          # a poll would ask again, many times, in here
        waiting = list(asked)
    runner.join(60)
    monkeypatch.undo()
    assert done["manifest"]["online"]["sessions"] == 4
    assert waiting.count((fcntl.LOCK_EX | fcntl.LOCK_NB, "busy")) == 1, (
        f"the backup polled a busy lock: {waiting}")
    assert asked.count((fcntl.LOCK_EX, "done")) == 1, (
        f"the busy lock was not waited for with one blocking request: {asked}")


def test_the_managed_cycle_waits_no_longer_for_a_lock_than_it_is_told(tmp_path):
    serving = populated(tmp_path)
    busy = session("notes")
    backups = tmp_path / "managed"
    with serving.locks.hold(busy.internal):
        with pytest.raises(StateToolError, match=f"could not lock {busy.lock} within 0.1 s"):
            run_managed_backup_cycle(serving.state, backups, audit_key=AUDIT_KEY,
                                     retention_s=100, online=True, lock_wait_s=0.1)
        status, out, err = cli("backup-cycle", "--state", serving.state, "--backup-dir", backups,
                               "--retention-days", "1", "--online",
                               "--lock-wait-seconds", "0.05")
        assert (status, out) == (2, "") and err == (
            f"state operation failed: could not lock {busy.lock} within 0.05 seconds; "
            f"the backup was not taken\n")
    assert [p.name for p in backups.iterdir()] == [state_tool.BACKUP_LOCK_NAME]


def test_the_schedule_store_is_copied_under_the_stores_own_lock(tmp_path):
    """While the backup copies the schedule store, the store's own
    operations wait; and with the store's lock out of reach the backup
    fails, like a session's."""
    serving = populated(tmp_path)
    seen = {}

    def read_store():
        seen["entries"] = len(serving.schedules.entries())
        seen["after"] = steps.log[-1]

    with pytest.MonkeyPatch.context() as patch:
        steps = Interleave(patch, serving.state)
        steps.held[SCHEDULE_LOCK].append(
            lambda: seen.update(free=free(serving.schedules._lock_path)))
        steps.during[SCHEDULE_LOCK].append(read_store)
        online_backup(serving.state, tmp_path / "online.tar.gz")
    assert steps.failures == []
    assert seen == {"free": False, "entries": 1, "after": ("unlock", SCHEDULE_LOCK)}, (
        f"the store's own lock was not held while the schedule store was copied: {seen}")
    output = tmp_path / "backups" / "again.tar.gz"
    with serving.schedules._locked():
        with pytest.raises(StateToolError, match=(
                f"could not lock {SCHEDULE_LOCK} within 0.1 seconds")):
            online_backup(serving.state, output, lock_wait_s=0.1)
    assert list(output.parent.iterdir()) == []


def test_sessions_first_then_the_schedule_store_and_the_ledger_last(tmp_path):
    """The order the brief gives, and what it buys: a turn that completes
    after its session was copied and before the ledger is captured is in the
    ledger's tenant totals, while its session is archived as it was copied,
    with a row that records those bytes."""
    serving = populated(tmp_path)
    first = by_digest(serving, ("project", "notes", "plans"))[0]
    tokens_before = serving.quota.tenant_usage(A)["tokens_today"]
    captured = {}

    def turn_after_its_copy():
        captured.update(session_files(serving.state, first.digest))
        assert serving.chat(first.external) == 200

    with pytest.MonkeyPatch.context() as patch:
        steps = Interleave(patch, serving.state)
        steps.before_snapshot.append(turn_after_its_copy)
        online_backup(serving.state, tmp_path / "online.tar.gz")
    kinds = [entry[0] for entry in steps.log]
    assert kinds == ["lock", "unlock"] * 5 + ["snapshot"], steps.log
    assert steps.log[-3:-1] == [("lock", SCHEDULE_LOCK), ("unlock", SCHEDULE_LOCK)]
    restored = tmp_path / "restored"
    restore_backup(tmp_path / "online.tar.gz", restored, audit_key=AUDIT_KEY)
    assert session_files(restored, first.digest) == captured
    live_rows, rows = ledger_rows(serving.state), ledger_rows(restored)
    assert rows[first.digest] != live_rows[first.digest], (
        "anti-vacuity: the live row moved on with the turn")
    with closing(sqlite3.connect(restored / "product-quotas.sqlite3")) as db:
        tokens = db.execute("SELECT SUM(tokens) FROM token_windows").fetchone()[0]
    assert tokens == serving.quota.aggregate_usage()["tokens_today"] > tokens_before + 4
    boots_quietly(restored)


def test_the_ledger_is_captured_through_sqlite_with_what_is_still_in_its_wal(tmp_path):
    """A serving ledger keeps its newest commits in its write-ahead log. They
    are captured through SQLite's backup API, as offline; a copy of the
    database file alone would miss them."""
    serving = populated(tmp_path)
    ledger = serving.state / "product-quotas.sqlite3"
    holder = sqlite3.connect(ledger)  # an open reader keeps the WAL from being folded in
    try:
        holder.execute("SELECT COUNT(*) FROM session_usage").fetchall()
        assert serving.chat("project") == 200
        assert (serving.state / "product-quotas.sqlite3-wal").stat().st_size > 0
        (tmp_path / "raw-copy.sqlite3").write_bytes(ledger.read_bytes())
        with closing(sqlite3.connect(tmp_path / "raw-copy.sqlite3")) as raw:
            raw_tokens = raw.execute("SELECT SUM(tokens) FROM token_windows").fetchone()[0]
        tokens = serving.quota.aggregate_usage()["tokens_today"]
        assert raw_tokens < tokens, "anti-vacuity: the main file alone lacks the turn"
        online_backup(serving.state, tmp_path / "online.tar.gz")
    finally:
        holder.close()
    restored = tmp_path / "restored"
    restore_backup(tmp_path / "online.tar.gz", restored, audit_key=AUDIT_KEY)
    with closing(sqlite3.connect(restored / "product-quotas.sqlite3")) as db:
        assert db.execute("SELECT SUM(tokens) FROM token_windows").fetchone()[0] == tokens
    assert ledger_rows(restored) == ledger_rows(serving.state)
    boots_quietly(restored)


def test_the_private_copy_is_beside_the_output_private_and_never_synced_under_a_lock(
        tmp_path, monkeypatch):
    """The copy the archive is made from holds every conversation in the
    clear. It lives where the archive will, in a directory only its owner can
    enter, and holds the state's committed files and nothing else. It is
    scratch, so no file in it is synced: a sync per file would be paid while
    the session's lock is held, by the turn waiting for it."""
    serving = populated(tmp_path)
    output = tmp_path / "backups" / "online.tar.gz"
    seen, synced_under_a_lock = {}, []
    steps = Interleave(monkeypatch, serving.state)
    real_fsync = os.fsync

    def fsync(descriptor):
        if steps.log and steps.log[-1][0] == "lock":
            synced_under_a_lock.append(steps.log[-1][1])
        return real_fsync(descriptor)

    def look():
        entries = list(output.parent.iterdir())
        seen["names"] = [entry.name for entry in entries]
        seen["modes"] = [stat.S_IMODE(entry.lstat().st_mode) for entry in entries]
        seen["files"] = {path.relative_to(entries[0]).as_posix(): path.read_bytes()
                         for path in entries[0].rglob("*") if path.is_file()}

    steps.before_snapshot.append(look)
    monkeypatch.setattr(os, "fsync", fsync)
    previous = os.umask(0)
    try:
        online_backup(serving.state, output)
    finally:
        os.umask(previous)
    assert len(seen["names"]) == 1 and re.fullmatch(
        r"\.online\.tar\.gz\.[a-z0-9_]{8}\.online", seen["names"][0]), seen["names"]
    assert seen["modes"] == [0o700]
    expected = {"product-schedules.json": (serving.state / "product-schedules.json").read_bytes()}
    for digest in serving.registered():
        expected.update(session_files(serving.state, digest))
    assert seen["files"] == expected and len(expected) == 20
    assert synced_under_a_lock == [], (
        f"files were synced while these locks were held: {synced_under_a_lock}")
    assert [entry.name for entry in output.parent.iterdir()] == [output.name]


# ── the ledger copy agrees with what was copied ──────────────────────────


def test_a_session_created_meanwhile_is_left_out_with_its_row(tmp_path):
    serving = populated(tmp_path)
    new = session("arrived-during-the-backup")
    with pytest.MonkeyPatch.context() as patch:
        steps = Interleave(patch, serving.state)
        steps.after[0].append(lambda: serving.chat(new.external))
        manifest = online_backup(serving.state, tmp_path / "online.tar.gz")
    assert new.digest in ledger_rows(serving.state), "anti-vacuity: it is registered"
    assert manifest["online"]["sessions"] == 4
    assert manifest["online"]["sessions_created_meanwhile"] == 1
    assert not any(new.digest[:16] in name for name in members(tmp_path / "online.tar.gz"))
    restored = tmp_path / "restored"
    restore_backup(tmp_path / "online.tar.gz", restored, audit_key=AUDIT_KEY)
    assert new.digest not in ledger_rows(restored)
    assert session_files(restored, new.digest) == {}
    assert boots_quietly(restored)["sessions"] == 4


@pytest.mark.parametrize("when", [
    "before-it-is-copied", "while-it-is-copied", "after-it-is-copied", "after-the-ledger"])
def test_a_session_deleted_meanwhile_is_whole_or_absent(tmp_path, when):
    """A deletion holds the session's lock from its first removal to the
    removal of its registry row, so the backup sees it before or after, never
    partway. One the ledger no longer lists is left out, files and row; one
    deleted after the ledger was captured is in the archive as it was
    copied. Its schedule goes with it when the store is copied later."""
    serving = populated(tmp_path)
    first, second, third = by_digest(serving, ("project", "notes", "plans"))
    victim = {"before-it-is-copied": second}.get(when, first)
    assert serving.schedule("victims", victim.external) == 201
    whole = session_files(serving.state, victim.digest)
    statuses = []

    def delete():
        statuses.append(serving.delete(victim.external))

    with pytest.MonkeyPatch.context() as patch:
        steps = Interleave(patch, serving.state)
        {"before-it-is-copied": steps.after[first.lock],
         "while-it-is-copied": steps.during[first.lock],
         "after-it-is-copied": steps.after[first.lock],
         "after-the-ledger": steps.after_snapshot}[when].append(delete)
        manifest = online_backup(serving.state, tmp_path / "online.tar.gz")
    assert steps.failures == [] and statuses == [200]
    assert session_files(serving.state, victim.digest) == {}, "anti-vacuity: it is deleted"
    restored = tmp_path / "restored"
    restore_backup(tmp_path / "online.tar.gz", restored, audit_key=AUDIT_KEY)
    files, rows = session_files(restored, victim.digest), ledger_rows(restored)
    schedules = json.loads((restored / "product-schedules.json").read_bytes())
    scheduled = sorted(entry["name"] for entry in schedules.values())
    if when == "after-the-ledger":
        assert files == whole and victim.digest in rows
        assert scheduled == ["daily", "victims"]
        assert manifest["online"]["sessions_deleted_meanwhile"] == 0
    else:
        assert files == {} and victim.digest not in rows, (
            f"a session deleted {when} was archived in part: "
            f"{sorted(files)}, row {rows.get(victim.digest)}")
        assert scheduled == ["daily"]
        assert manifest["online"]["sessions_deleted_meanwhile"] == 1
    assert manifest["online"]["sessions"] + manifest["online"][
        "sessions_deleted_meanwhile"] == 4, "a registered session went uncounted"
    for kept in (first, second, third, session("theirs", B)):
        if kept is not victim:
            assert session_files(restored, kept.digest) == session_files(
                serving.state, kept.digest) != {}
    boots_quietly(restored)


def test_turn_leases_are_not_carried_into_the_archive(tmp_path):
    """A serving ledger holds the leases of turns in flight, which the
    offline validator rejects and a restored service would have to wait out.
    The archive's ledger has none; the live one keeps its own."""
    serving = populated(tmp_path)
    leases = []
    with pytest.MonkeyPatch.context() as patch:
        steps = Interleave(patch, serving.state)
        steps.before_snapshot.append(lambda: leases.append(serving.quota.acquire_turn(A)))
        online_backup(serving.state, tmp_path / "online.tar.gz")
    assert ledger_count(serving.state, "turn_leases") == 1, "anti-vacuity"
    restored = tmp_path / "restored"
    restore_backup(tmp_path / "online.tar.gz", restored, audit_key=AUDIT_KEY)
    assert ledger_count(restored, "turn_leases") == 0
    assert ledger_count(restored, "token_windows") == 2, "the tenants' usage is kept"
    leases[0].release()
    boots_quietly(restored)


def test_a_first_turn_that_settles_meanwhile_is_not_restored_as_lost_files(tmp_path):
    """A turn is admitted, which creates its session's registry row, before
    it takes the session lock. A backup can copy the still-empty session in
    between; the turn then settles, and the ledger, captured last, records
    bytes for files the archive does not hold. Restored as captured, that row
    is what lost files look like to startup. The row in the archive records
    what was copied instead: nothing."""
    serving = populated(tmp_path)
    new = session("first-turn")
    _, _, release = admit_and_wait(serving, new.external)
    assert ledger_rows(serving.state)[new.digest] == (0, 0)
    with pytest.MonkeyPatch.context() as patch:
        steps = Interleave(patch, serving.state)
        steps.after[new.lock].append(release)
        manifest = online_backup(serving.state, tmp_path / "online.tar.gz")
    live = ledger_rows(serving.state)[new.digest]
    assert live[0] > 0 and live[1] > 0, f"anti-vacuity: the turn settled {live}"
    assert manifest["online"]["sessions"] == 5
    restored = tmp_path / "restored"
    restore_backup(tmp_path / "online.tar.gz", restored, audit_key=AUDIT_KEY)
    assert session_files(restored, new.digest) == {}
    assert ledger_rows(restored)[new.digest] == (0, 0), (
        f"the archive's ledger records {ledger_rows(restored)[new.digest]} bytes "
        f"for a session of which the archive holds no file: startup on the "
        f"restored tree reports it as lost")
    event = boots_quietly(restored)
    assert (event["sessions_missing_storage"], event["sessions_missing_audit"]) == (0, 0)


def test_files_that_were_already_lost_are_still_reported_after_a_restore(tmp_path):
    """The other side of the test above. A row that records bytes for files
    that are gone from the live state is a loss, and the backup must not
    launder it: restored, it is reported exactly as on the live state."""
    serving = populated(tmp_path)
    lost = session("notes")
    for path in (serving.state / "sessions" / f"{lost.digest}.kv",
                 serving.state / "audit" / f"{lost.digest}.jsonl",
                 serving.state / "sandboxes" / lost.digest / "turns.txt",
                 *(serving.state / "sandboxes" / lost.digest / "notes").iterdir()):
        path.unlink()
    recorded = ledger_rows(serving.state)[lost.digest]
    assert recorded[0] > 0 and recorded[1] > 0
    online_backup(serving.state, tmp_path / "online.tar.gz")
    restored = tmp_path / "restored"
    restore_backup(tmp_path / "online.tar.gz", restored, audit_key=AUDIT_KEY)
    assert ledger_rows(restored)[lost.digest] == recorded
    events, _ = boot(restored)
    critical = [event for event in events if event.get("severity") == "critical"]
    assert [(event["event"], event["sessions_missing_storage"],
             event["sessions_missing_audit"]) for event in critical] == [
                 ("state_reconciliation", 1, 1)], critical


# ── nothing is silently left out, and nothing that is not state gets in ──


def test_scratch_and_lock_files_are_never_archived(tmp_path):
    """What P2 left on disk around a save: scratch files are never state.
    Neither are the lock files, SQLite's own files, or a marker's scratch."""
    serving = populated(tmp_path)
    item = session("project")
    canary = b"SCRATCH-CANARY-not-committed-state"
    planted = [
        serving.state / "sessions" / f".{item.digest}.kv.0123456789abcdef.tmp",
        serving.state / "sessions" / f"{item.digest}.kv.tmp",
        serving.state / "sessions" / f".{'f' * 64}.kv.0123456789abcdef.tmp",
        serving.state / ".product-schedules.json.0123456789abcdef.tmp",
        serving.state / ".product-schedules.json.4242.tmp",
        serving.state / f".{CLEAN_MARKER}.0123456789abcdef.tmp",
    ]
    for path in planted:
        path.write_bytes(canary)
    archive = tmp_path / "online.tar.gz"
    manifest = online_backup(serving.state, archive)
    archived = members(archive)
    assert not any(canary in data for data in archived.values()), (
        "a scratch file's bytes are in the archive")
    names = set(archived) - {"manifest.json"}
    assert names == {f"data/{entry['path']}" for entry in manifest["files"]}
    for name in names:
        relative = Path(name).relative_to("data")
        assert (relative.parts[0] in ("sessions", "sandboxes", "audit")
                or name in ("data/product-schedules.json", "data/product-quotas.sqlite3")), name
        assert not (relative.parts[0] == "sessions" and relative.suffix != ".kv"), name
        assert not name.endswith((".lock", ".tmp", "-wal", "-shm")), name
    assert all(path.read_bytes() == canary for path in planted), (
        "the backup touched the service's scratch files")
    restored = tmp_path / "restored"
    restore_backup(archive, restored, audit_key=AUDIT_KEY)
    assert boots_quietly(restored)["scratch_files_removed"] == 0


@pytest.mark.parametrize("kind", [
    "conversation", "chain", "sandbox", "sandbox-of-a-session-that-once-ran",
    "sandbox-by-another-name", "a-name-that-is-no-session", "no-ledger"])
def test_state_no_registry_row_attributes_fails_the_backup(tmp_path, kind):
    """The offline backup refuses customer files the registry cannot
    attribute. Copying only registered sessions would leave them out without
    a word, so the online backup refuses them too, with the same message."""
    serving = populated(tmp_path)
    stray = "e" * 64
    if kind == "conversation":
        (serving.state / "sessions" / f"{stray}.kv").write_text("[]")
    elif kind == "chain":
        (serving.state / "audit" / f"{stray}.jsonl").write_text("")
    elif kind == "sandbox":
        (serving.state / "sandboxes" / stray).mkdir()
    elif kind == "sandbox-of-a-session-that-once-ran":
        # Its lock file is still there, as lock files are; its row is not.
        (serving.state / "locks" / f"{stray}.lock").touch()
        (serving.state / "sandboxes" / stray).mkdir()
        (serving.state / "sandboxes" / stray / "left.txt").write_text("unattributed")
    elif kind == "sandbox-by-another-name":
        (serving.state / "sandboxes" / "not-a-session").mkdir()
    elif kind == "a-name-that-is-no-session":
        (serving.state / "sessions" / "notes.kv").write_text("[]")
    else:
        for name in ("product-quotas.sqlite3", "product-quotas.sqlite3-wal",
                     "product-quotas.sqlite3-shm"):
            (serving.state / name).unlink(missing_ok=True)
    output = tmp_path / "backups" / "online.tar.gz"
    expected = ("customer state has no durable quota registry" if kind == "no-ledger"
                else UNATTRIBUTED)
    with pytest.raises(StateToolError) as refused:
        online_backup(serving.state, output)
    assert str(refused.value) == expected
    assert list(output.parent.iterdir()) == []


@pytest.mark.parametrize("stopped", ["started", "midway"])
def test_a_turn_whose_admission_a_deletion_removed_is_copied_not_refused(tmp_path, stopped):
    """Files with no registry row are not yet state nothing attributes. A
    turn is admitted, which writes its session's row, before it takes the
    session lock. A deletion that holds the lock meanwhile removes that row
    with the session. The turn then runs, and restores the row only when it
    settles: until then the session has files and no row, for as long as a
    turn takes. The backup must decide under the session's lock, where it
    finds the turn finished and the row back. A sandbox directory, which is
    all a turn has made when it starts, names its session only through the
    lock file the turn holds."""
    serving = populated(tmp_path)
    item = session("notes")
    _, _, release = admit_and_wait(serving, "notes")
    assert serving.delete("notes") == 200
    reached, proceed = serving.agent.pause_next_turn(item.internal, at=stopped)
    finishing = threading.Thread(target=release, daemon=True)
    finishing.start()
    assert reached.wait(30)
    present = sorted(Path(name).parts[0] for name in session_files(serving.state, item.digest))
    assert item.digest not in ledger_rows(serving.state), "anti-vacuity: no row"
    assert (serving.state / "sandboxes" / item.digest).is_dir()
    assert present == ([] if stopped == "started" else ["audit", "sandboxes", "sandboxes"])
    threading.Timer(0.05, proceed.set).start()
    try:
        manifest = online_backup(serving.state, tmp_path / "online.tar.gz", lock_wait_s=60)
    finally:
        proceed.set()
        finishing.join(60)
    assert manifest["online"]["sessions"] == 4
    assert manifest["online"]["sessions_deleted_meanwhile"] == 0
    restored = tmp_path / "restored"
    restore_backup(tmp_path / "online.tar.gz", restored, audit_key=AUDIT_KEY)
    files = session_files(restored, item.digest)
    assert turns_in(files, item.digest) == (1, 1, 1, 1)
    assert files == serving.agent.history[item.digest][-1]
    boots_quietly(restored)


def test_a_session_that_arrives_between_the_registry_read_and_the_listing_is_copied(
        tmp_path, monkeypatch):
    """The backup reads the registry and then lists the files on disk. A
    session whose first turn completes in between has files and is not in
    the list it read. That is not state nothing attributes either: under its
    lock it has its row, and it is copied like any other."""
    serving = populated(tmp_path)
    new = session("arrived-in-between")
    real = state_tool._registered_sessions

    def registered(ledger):
        listed = real(ledger)
        assert serving.chat(new.external) == 200
        return listed

    monkeypatch.setattr(state_tool, "_registered_sessions", registered)
    manifest = online_backup(serving.state, tmp_path / "online.tar.gz")
    assert manifest["online"]["sessions"] == 5
    assert manifest["online"]["sessions_created_meanwhile"] == 0
    restored = tmp_path / "restored"
    restore_backup(tmp_path / "online.tar.gz", restored, audit_key=AUDIT_KEY)
    assert session_files(restored, new.digest) == serving.agent.history[new.digest][-1]
    boots_quietly(restored)


def test_a_turn_between_the_registry_read_and_its_copy_is_archived_with_its_row(tmp_path):
    """The row is read again under the session's lock, not taken from the
    list the backup began with: the bytes it records are those of the files
    as they are copied."""
    serving = populated(tmp_path)
    last = by_digest(serving, ("project", "notes", "plans"))[-1]
    listed = ledger_rows(serving.state)[last.digest]
    with pytest.MonkeyPatch.context() as patch:
        Interleave(patch, serving.state).before[0].append(lambda: serving.chat(last.external))
        online_backup(serving.state, tmp_path / "online.tar.gz")
    restored = tmp_path / "restored"
    restore_backup(tmp_path / "online.tar.gz", restored, audit_key=AUDIT_KEY)
    assert session_files(restored, last.digest) == serving.agent.history[last.digest][-1]
    assert ledger_rows(restored)[last.digest] == ledger_rows(serving.state)[last.digest] != listed
    boots_quietly(restored)


def test_a_file_that_appears_under_a_session_with_no_row_fails_the_backup(tmp_path):
    """Under a session's lock its files and its row agree: a deletion removes
    the files before the row. Files with no row are state nothing attributes,
    whenever they are found."""
    serving = populated(tmp_path)
    first = by_digest(serving, ("project", "notes", "plans"))[0]
    output = tmp_path / "backups" / "online.tar.gz"

    def drop_the_row():
        with closing(sqlite3.connect(serving.state / "product-quotas.sqlite3")) as db:
            db.execute("DELETE FROM session_usage WHERE session=?", (first.digest,))
            db.commit()

    with pytest.MonkeyPatch.context() as patch:
        Interleave(patch, serving.state).before[first.lock].append(drop_the_row)
        with pytest.raises(StateToolError) as refused:
            online_backup(serving.state, output)
    assert str(refused.value) == UNATTRIBUTED
    assert list(output.parent.iterdir()) == []


@pytest.mark.parametrize("kind", ["link-in-a-sandbox", "special-file-in-a-sandbox",
                                  "linked-conversation", "linked-surface",
                                  "linked-schedule-store", "linked-ledger"])
def test_what_an_offline_backup_refuses_to_read_the_online_one_refuses_too(tmp_path, kind):
    serving = populated(tmp_path)
    item = session("project")
    outside = tmp_path / "outside-secret"
    outside.write_text("must-not-be-backed-up")
    sandbox = serving.state / "sandboxes" / item.digest
    if kind == "link-in-a-sandbox":
        (sandbox / "notes" / "link").symlink_to(outside)
        expected = f"state backup refuses symlink: sandboxes/{item.digest}/notes/link"
    elif kind == "special-file-in-a-sandbox":
        os.mkfifo(sandbox / "pipe")
        expected = f"state backup refuses special file: sandboxes/{item.digest}/pipe"
    elif kind == "linked-conversation":
        conversation = serving.state / "sessions" / f"{item.digest}.kv"
        conversation.unlink()
        conversation.symlink_to(outside)
        expected = f"state backup refuses symlink: sessions/{item.digest}.kv"
    elif kind == "linked-surface":
        (serving.state / "audit").rename(tmp_path / "elsewhere")
        (serving.state / "audit").symlink_to(tmp_path / "elsewhere")
        expected = "state surface is not a real directory: audit"
    elif kind == "linked-schedule-store":
        store = serving.state / "product-schedules.json"
        store.rename(tmp_path / "schedules-elsewhere")
        store.symlink_to(tmp_path / "schedules-elsewhere")
        expected = "product-schedules.json must be a regular file"
    else:
        ledger = serving.state / "product-quotas.sqlite3"
        ledger.rename(tmp_path / "ledger-elsewhere")
        ledger.symlink_to(tmp_path / "ledger-elsewhere")
        expected = "product-quotas.sqlite3 must be a regular file"
    output = tmp_path / "backups" / "online.tar.gz"
    with pytest.raises(StateToolError) as refused:
        online_backup(serving.state, output)
    assert str(refused.value) == expected
    assert list(output.parent.iterdir()) == []


@pytest.mark.parametrize("taken", ["a-file", "a-directory"])
def test_a_name_the_private_copy_cannot_keep_apart_fails_the_backup(tmp_path, taken):
    """Found by this node's sweep, in a Linux container whose backup
    directory was on a volume that ignores case: a sandbox holding Note.txt
    and note.txt was archived as one file, without a word. The private copy
    is the backup's own directory, so a name that is already taken in it,
    which the backup did not make, is another state file's. Simulated here
    as such a volume shows it: the name is there before the copy writes it."""
    serving = populated(tmp_path)
    last = by_digest(serving, ("project", "notes", "plans"))[-1]
    output = tmp_path / "backups" / "online.tar.gz"
    name = {"a-file": f"sessions/{last.digest}.kv",
            "a-directory": f"sandboxes/{last.digest}/notes"}[taken]

    def take_the_name():
        (copy,) = output.parent.iterdir()
        if taken == "a-file":
            (copy / name).write_bytes(b"another file, under a name that differs only in case")
        else:
            (copy / name).mkdir(parents=True)

    with pytest.MonkeyPatch.context() as patch:
        Interleave(patch, serving.state).before[last.lock].append(take_the_name)
        with pytest.raises(StateToolError) as refused:
            online_backup(serving.state, output)
    collided = name if taken == "a-file" else f"sandboxes/{last.digest}/"
    assert str(refused.value).startswith(
        f"the backup directory cannot keep this state file's name apart from "
        f"another's: {collided}")
    assert list(output.parent.iterdir()) == []


@pytest.mark.parametrize("spread", ["within-one-sandbox", "across-sessions"])
def test_the_size_limit_stops_the_copy_before_it_passes_it(tmp_path, monkeypatch, spread):
    """The limit bounds the private copy as it is made, not only the archive
    afterwards: a state over the limit is not first copied whole. It is on
    the sum, as offline: no one file here reaches it, and neither does any
    one session when the files are spread across three."""
    serving = populated(tmp_path)
    holders = by_digest(serving, ("project", "notes", "plans"))
    for n in range(3):
        holder = holders[0] if spread == "within-one-sandbox" else holders[n]
        (serving.state / "sandboxes" / holder.digest / f"part-{n}.bin").write_bytes(
            bytes([n]) * 100_000)
    total = sum(len(data) for digest in serving.registered()
                for data in session_files(serving.state, digest).values())
    written = []
    real = state_tool._write_private

    def counting(path, chunks, **options):
        def counted():
            for chunk in chunks:
                written.append(len(chunk))
                yield chunk
        return real(path, counted(), **options)

    monkeypatch.setattr(state_tool, "_write_private", counting)
    output = tmp_path / "backups" / "online.tar.gz"
    limit = 250_000
    with pytest.raises(StateToolError, match="state exceeds the configured backup size limit"):
        online_backup(serving.state, output, max_bytes=limit)
    assert total > limit and 200_000 <= sum(written) <= limit, (
        f"{sum(written)} bytes of a {total}-byte state were copied under a limit of {limit}")
    assert list(output.parent.iterdir()) == []
    monkeypatch.undo()
    whole = online_backup(serving.state, output, max_bytes=10 * 1024 * 1024)
    assert sum(entry["size"] for entry in whole["files"]) > total


def test_a_ledger_that_cannot_be_read_fails_the_backup_as_a_state_error(tmp_path):
    """No SQLite error escapes as a traceback: the command reports it as
    every other failed state operation."""
    serving = populated(tmp_path)
    ledger = serving.state / "product-quotas.sqlite3"
    for name in ("product-quotas.sqlite3-wal", "product-quotas.sqlite3-shm"):
        (serving.state / name).unlink(missing_ok=True)
    ledger.write_bytes(b"not a database, though it has the ledger's name")
    output = tmp_path / "backups" / "online.tar.gz"
    status, out, err = cli("backup", "--state", serving.state, "--output", output, "--online")
    assert (status, out) == (2, "")
    assert err == "state operation failed: quota database is corrupt or cannot be read\n"
    assert list(output.parent.iterdir()) == []


def test_a_registry_row_that_names_no_session_fails_the_backup(tmp_path):
    """A row's session digest names the lock and the files. One that is not a
    digest is never used as a path: no lock file is created for it, inside
    the state or, as this one would have it, outside."""
    serving = populated(tmp_path)
    with closing(sqlite3.connect(serving.state / "product-quotas.sqlite3")) as db:
        db.execute("UPDATE session_usage SET session='../../outside' WHERE session=?",
                   (session("notes").digest,))
        db.commit()
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    output = tmp_path / "backups" / "online.tar.gz"
    with pytest.raises(StateToolError, match="quota database contains invalid usage records"):
        online_backup(serving.state, output)
    created = sorted(set(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")
                         if not p.name.endswith(("-wal", "-shm"))) - set(before))
    assert created == ["backups"], f"a backup refused for a bad registry row created {created}"


def test_the_arguments_an_online_backup_refuses(tmp_path):
    serving = populated(tmp_path)
    output = tmp_path / "backups" / "online.tar.gz"
    for wait in (-1, float("nan")):
        with pytest.raises(StateToolError, match="the lock wait must be zero or more seconds"):
            online_backup(serving.state, output, lock_wait_s=wait)
    with pytest.raises(StateToolError, match="an audit verification key is required"):
        create_backup(serving.state, output, audit_key=b"", online=True)
    with pytest.raises(StateToolError, match="max_bytes must be positive"):
        online_backup(serving.state, output, max_bytes=0)
    with pytest.raises(StateToolError, match="state directory does not exist"):
        online_backup(tmp_path / "missing", output)
    assert not output.parent.exists(), "a refused call created something"
    output.parent.mkdir()
    output.write_bytes(b"preserve-me")
    with pytest.MonkeyPatch.context() as patch:
        steps = Interleave(patch, serving.state)
        with pytest.raises(StateToolError, match="backup output already exists; refusing"):
            online_backup(serving.state, output)
    assert steps.locks == [], "an output that is already there is refused before any copy"
    assert [p.name for p in output.parent.iterdir()] == [output.name]
    assert output.read_bytes() == b"preserve-me"


def test_a_state_that_has_never_served_is_backed_up_online_too(tmp_path):
    """No ledger and no customer state: an empty archive, as offline. And a
    restored tree, which has no lock directory until a service starts on it,
    is backed up online like any other."""
    empty = tmp_path / "empty"
    empty.mkdir()
    manifest = online_backup(empty, tmp_path / "empty.tar.gz")
    assert manifest["files"] == [] and manifest["online"]["sessions"] == 0
    serving = populated(tmp_path)
    online_backup(serving.state, tmp_path / "first.tar.gz")
    restored = tmp_path / "restored"
    restore_backup(tmp_path / "first.tar.gz", restored, audit_key=AUDIT_KEY)
    assert not (restored / "locks").exists()
    again = online_backup(restored, tmp_path / "second.tar.gz")
    assert again["online"]["sessions"] == 4
    assert {entry["path"]: entry["sha256"] for entry in again["files"]
            if not entry["path"].endswith(".sqlite3")} == {
                entry["path"]: entry["sha256"]
                for entry in json.loads(members(tmp_path / "first.tar.gz")["manifest.json"])["files"]
                if not entry["path"].endswith(".sqlite3")}


# ── the archive is schema 1, and the offline one is unchanged ────────────


def test_an_online_archive_is_a_schema_1_archive_whose_manifest_only_adds_fields(tmp_path):
    """Decision D5 is open, so nothing an older reader refuses is added. The
    manifest keeps every schema-1 field and adds two; the members are the
    manifest and the declared files; and the recovery drill's own reader of
    schema 1, which shares no code with state_tool, reads it."""
    serving = populated(tmp_path)
    archive = tmp_path / "online.tar.gz"
    manifest = online_backup(serving.state, archive, now=1234)
    assert set(manifest) == SCHEMA_1_KEYS | ONLINE_KEYS
    assert manifest["schema_version"] == state_tool.SCHEMA_VERSION == 1
    assert manifest["mode"] == "online" and set(manifest["online"]) == CAPTURE_KEYS
    assert manifest["created_unix"] == 1234
    assert manifest["clean_shutdown_unix"] is None and manifest["product_version"] is None
    assert (manifest["audit_chains"], manifest["audit_records"]) == (4, 7)
    archived = members(archive)
    assert list(archived)[0] == "manifest.json"
    assert json.loads(archived["manifest.json"]) == manifest
    assert set(archived) == {"manifest.json"} | {
        f"data/{entry['path']}" for entry in manifest["files"]}
    for entry in manifest["files"]:
        assert set(entry) == {"path", "sha256", "size"}
        data = archived[f"data/{entry['path']}"]
        assert (hashlib.sha256(data).hexdigest(), len(data)) == (entry["sha256"], entry["size"])
    with tarfile.open(archive, "r:gz") as opened:
        assert {(m.mode, m.uid, m.gid, m.uname, m.gname, m.mtime, m.type)
                for m in opened.getmembers()} == {
                    (0o600, 0, 0, "root", "root", 0, tarfile.REGTYPE)}
    assert archive.stat().st_mode & 0o777 == 0o600
    drill_manifest, payloads = _backup_payloads(archive)
    assert drill_manifest == manifest
    assert set(payloads) == {entry["path"] for entry in manifest["files"]}


def test_an_offline_backup_is_what_it_was(tmp_path):
    """The marker is still required, the manifest gains no field, and a
    drained state gives the same files either way."""
    serving = populated(tmp_path)
    (serving.state / CLEAN_MARKER).write_text(json.dumps({
        "schema_version": 1, "product_version": "9.9.9", "clean_shutdown_unix": 100}))
    offline = create_backup(serving.state, tmp_path / "offline.tar.gz",
                            audit_key=AUDIT_KEY, now=1234)
    assert set(offline) == SCHEMA_1_KEYS
    assert (offline["product_version"], offline["clean_shutdown_unix"]) == ("9.9.9", 100)
    online = online_backup(serving.state, tmp_path / "online.tar.gz", now=1234)
    assert online["files"] == offline["files"], (
        "with nothing in flight, an online backup archives the files an offline one does")
    cycle = run_managed_backup_cycle(serving.state, tmp_path / "managed",
                                     audit_key=AUDIT_KEY, retention_s=100, now=1234)
    assert set(cycle) == {"schema_version", "created", "deleted", "retention_seconds"}


# ── the semantics ────────────────────────────────────────────────────────


def test_backup_age_counts_from_the_start(tmp_path, monkeypatch):
    """Every session in the archive is at least as new as the moment the
    backup began, and no newer bound holds for all of them. `created_unix`,
    which the recovery objective is measured from, is that moment, not the
    time the copy finished."""
    serving = populated(tmp_path)
    clock = itertools.count(5000, 100)
    monkeypatch.setattr(time, "time", lambda: float(next(clock)))
    manifest = online_backup(serving.state, tmp_path / "online.tar.gz")
    assert manifest["created_unix"] == 5000, (
        f"created_unix is {manifest['created_unix']}: the clock read 5000 when the "
        f"backup began and later with every reading after it")
    monkeypatch.undo()
    result = run_managed_backup_cycle(serving.state, tmp_path / "managed",
                                      audit_key=AUDIT_KEY, retention_s=100, now=777,
                                      online=True)
    assert result["created"]["created_unix"] == 777
    assert result["created"]["name"].startswith("sigil-pi-state-0000000777-")


def test_the_capture_is_reported_and_nothing_else(tmp_path):
    """The longest one session's lock was held is in the manifest, where a
    recovery drill reads it, with the counts that account for every
    registered session. Counts and times only."""
    serving = populated(tmp_path)
    manifest = online_backup(serving.state, tmp_path / "online.tar.gz")
    capture = manifest["online"]
    assert capture["sessions"] == 4
    assert (capture["sessions_created_meanwhile"], capture["sessions_deleted_meanwhile"]) == (0, 0)
    assert 0 < capture["max_session_lock_hold_ms"] < 60_000
    assert 0 < capture["capture_seconds"] < 60
    assert capture["max_session_lock_hold_ms"] <= capture["capture_seconds"] * 1000
    rendered = json.dumps({key: value for key, value in manifest.items() if key != "files"})
    for private in ("project", "tenant-a", session("project").digest[:16], str(tmp_path)):
        assert private not in rendered


def test_the_backup_leaves_the_live_state_as_it_found_it(tmp_path):
    """It reads. The only files it may add are lock files the service would
    create itself, and SQLite's own beside the ledger."""
    serving = populated(tmp_path)

    def snapshot():
        files = {path.relative_to(serving.state).as_posix(): path.read_bytes()
                 for path in serving.state.rglob("*") if path.is_file()
                 and not path.name.endswith(("-wal", "-shm", ".sqlite3"))}
        with closing(sqlite3.connect(serving.state / "product-quotas.sqlite3")) as db:
            tables = {table: db.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
                      for table in ("session_usage", "token_windows", "turn_leases")}
        return files, tables

    lease = serving.quota.acquire_turn(A)
    before = snapshot()
    online_backup(serving.state, tmp_path / "online.tar.gz")
    after = snapshot()
    lease.release()
    assert after == before
    assert len(before[0]) > 20 and len(before[1]["turn_leases"]) == 1, "anti-vacuity"
    held = [lock.name for lock in (*(serving.state / "locks").iterdir(),
                                   serving.state / SCHEDULE_LOCK) if not free(lock)]
    assert held == [] and len(list((serving.state / "locks").iterdir())) == 4, (
        f"the backup returned with these locks still held: {held}")


@pytest.mark.parametrize("failure", ["a-lock-out-of-reach", "a-chain-that-fails-verification",
                                     "an-interrupt", "a-full-disk"])
def test_a_backup_that_fails_leaves_no_copy_of_the_state_behind(tmp_path, failure):
    """The private copy holds every conversation in the clear. Whatever ends
    the backup, a signal included, it is removed, and nothing is published."""
    serving = populated(tmp_path)
    output = tmp_path / "backups" / "online.tar.gz"
    target = by_digest(serving, ("project", "notes", "plans"))[1]
    chain = serving.state / "audit" / f"{target.digest}.jsonl"
    signed = chain.read_text()
    with pytest.MonkeyPatch.context() as patch:
        steps = Interleave(patch, serving.state)
        if failure == "a-lock-out-of-reach":
            with serving.locks.hold(target.internal):
                with pytest.raises(StateToolError, match="could not lock"):
                    online_backup(serving.state, output, lock_wait_s=0.05)
        elif failure == "a-chain-that-fails-verification":
            chain.write_text(signed.replace("source", "tampered"))
            with pytest.raises(StateToolError, match=r"audit verification failed for 1 chain"):
                online_backup(serving.state, output)
            chain.write_text(signed)
        elif failure == "an-interrupt":
            def interrupt():
                raise KeyboardInterrupt
            steps.held[target.lock].append(interrupt)
            with pytest.raises(KeyboardInterrupt):
                online_backup(serving.state, output)
        else:
            def full():
                raise OSError(errno.ENOSPC, "No space left on device")
            steps.before_snapshot.append(full)
            with pytest.raises(OSError) as refused:
                online_backup(serving.state, output)
            assert refused.value.errno == errno.ENOSPC
    assert len(steps.locks) >= 2, "anti-vacuity: the copy had begun"
    assert list(output.parent.iterdir()) == [], (
        f"a backup ended by {failure} left {[p.name for p in output.parent.iterdir()]}")
    assert becomes_free(serving.state / "locks" / target.lock), "a lock was left held"
    assert serving.ready()


KILLED_MIDWAY = r"""
import os, signal, sys
sys.path.insert(0, sys.argv[1])
import state_tool


def die(path, destination):
    os.kill(os.getpid(), signal.SIGKILL)


state_tool._snapshot_quota_database = die
state_tool.run_managed_backup_cycle(
    sys.argv[2], sys.argv[3], audit_key=sys.argv[4].encode(), retention_s=86400, online=True)
"""


def test_the_copy_a_killed_cycle_leaves_is_removed_by_the_next_cycle(tmp_path):
    """A kill is the one end that cleans nothing up. What it leaves in the
    backup directory is a plain copy of the state; the next cycle, which
    holds the directory's lock and so knows no other cycle owns it, removes
    it. Nothing else in the directory is touched."""
    serving = populated(tmp_path)
    backups = tmp_path / "managed"
    child = subprocess.run(
        [sys.executable, "-c", KILLED_MIDWAY, str(PI_ROOT), str(serving.state),
         str(backups), AUDIT_KEY.decode()],
        capture_output=True, text=True, timeout=120)
    assert child.returncode == -signal.SIGKILL, (child.returncode, child.stderr)
    left = sorted(p.name for p in backups.iterdir() if p.name != state_tool.BACKUP_LOCK_NAME)
    assert len(left) == 1 and (backups / left[0]).is_dir(), (
        f"anti-vacuity: a killed online cycle should leave its copy; found {left}")
    conversation = serving.state / "sessions" / f"{session('project').digest}.kv"
    copies = [p for p in (backups / left[0]).rglob("*.kv")
              if p.read_bytes() == conversation.read_bytes()]
    assert len(copies) == 1, "anti-vacuity: the copy holds a conversation in the clear"
    bystander = backups / "operators-notes.online"
    bystander.mkdir()
    (bystander / "keep").write_text("not the cycle's")
    result = run_managed_backup_cycle(serving.state, backups, audit_key=AUDIT_KEY,
                                      retention_s=86400, online=True)
    assert sorted(p.name for p in backups.iterdir()) == sorted(
        [state_tool.BACKUP_LOCK_NAME, result["created"]["name"], bystander.name]), (
        "the next cycle did not remove the copy the killed one left, or removed more")
    assert (bystander / "keep").read_text() == "not the cycle's"


# ── the property: any interleaving restores and boots quietly ───────────

SLOTS = [(tenant, f"s{n}") for tenant in TENANTS for n in range(3)]
POINT = st.tuples(st.sampled_from(["before", "during", "after"]), st.integers(0, 8))
OPERATION = st.one_of(
    st.tuples(st.sampled_from(["turn", "turn", "delete", "schedule", "unschedule"]),
              st.sampled_from(SLOTS)),
    st.tuples(st.sampled_from(["tick", "lease"]), st.none()))


# A turn that holds its session's lock when the backup starts: the session,
# where the turn has stopped, and whether a deletion removed the row its
# admission wrote (see the test of that above).
IN_FLIGHT = st.tuples(st.sampled_from(SLOTS), st.sampled_from(["started", "midway"]),
                      st.booleans())


@settings(max_examples=40, deadline=None)
@given(
    initial=st.lists(st.tuples(st.sampled_from(SLOTS), st.integers(0, 2), st.booleans()),
                     max_size=6),
    steps=st.lists(st.tuples(POINT, OPERATION), max_size=8),
    in_flight=st.one_of(st.none(), IN_FLIGHT),
    admitted=st.lists(st.tuples(st.sampled_from(SLOTS), POINT), max_size=2,
                      unique_by=lambda pair: pair[0]))
@example(  # a first turn settles after its empty session was copied
    initial=[((A, "s0"), 1, False)], steps=[], in_flight=None,
    admitted=[((A, "s1"), ("after", 0)), ((B, "s0"), ("before", 9))])
@example(  # deleted after its copy, recreated, and a schedule run meanwhile
    initial=[((A, "s0"), 2, True), ((A, "s1"), 1, True), ((B, "s2"), 0, True)],
    steps=[(("after", 0), ("delete", (A, "s0"))), (("after", 0), ("delete", (A, "s1"))),
           (("before", 1), ("turn", (A, "s0"))), (("during", 1), ("tick", None)),
           (("after", 1), ("lease", None)), (("during", 2), ("delete", (B, "s2")))],
    in_flight=((A, "s1"), "midway", False), admitted=[])
@example(  # a turn in flight whose row a deletion removed, then deleted again
    initial=[((A, "s0"), 1, False), ((B, "s1"), 2, True)],
    steps=[(("during", 0), ("turn", (A, "s0"))), (("after", 1), ("delete", (A, "s0")))],
    in_flight=((A, "s0"), "started", True), admitted=[((B, "s1"), ("during", 1))])
def test_any_interleaving_yields_an_archive_that_restores_and_boots_quietly(
        initial, steps, in_flight, admitted):
    """THE PROPERTY. Turns, deletions, new sessions, schedule changes,
    scheduled runs and admitted turns, at any point of a backup: before a
    lock is taken, while it is held (then they wait for it, as clients do),
    after it is released, and around the ledger's capture; with a turn in
    flight when the backup starts, and turns admitted but not yet at their
    lock. Whatever the interleaving:

    - the backup succeeds, the archive restores (which validates it as an
      offline archive is validated), and the product boots ready on it with
      no critical event and no row to correct;
    - every session that was registered or had a turn in flight when the
      backup began is in the archive in a state a completed turn left it in,
      no older than at the start, or was deleted meanwhile and is whole or
      absent; no other session is in it;
    - every such session is archived or counted as deleted;
    - the schedule store in the archive is one the service saved;
    - the service is ready at every point, and no request fails."""
    with tempfile.TemporaryDirectory(prefix="b3-property-") as temp, \
            pytest.MonkeyPatch.context() as patch:
        root = Path(temp)
        serving = Serving(root / "state")
        names = {slot: session(slot[1], slot[0]) for slot in SLOTS}
        for slot, turns, scheduled in initial:
            for _ in range(turns):
                assert serving.chat(slot[1], slot[0]) == 200
            if scheduled:
                assert serving.schedule(f"plan-{slot[1]}", slot[1], slot[0]) == 201
        deleted, not_ready, leases, finish = set(), [], [], []

        def check_ready(phase):
            # The audit chains are verified again only where no turn of this
            # test can be mid-append; in between, readiness is as last found.
            if not serving.ready(verify=phase != "during"):
                not_ready.append((phase, len(interleave.log)))

        def operation(kind, slot):
            def run():
                if kind == "turn":
                    assert serving.chat(slot[1], slot[0]) == 200
                elif kind == "delete":
                    if serving.delete(slot[1], slot[0]) == 200:
                        deleted.add(names[slot].digest)
                elif kind == "schedule":
                    assert serving.schedule(f"plan-{slot[1]}", slot[1], slot[0]) == 201
                elif kind == "unschedule":
                    assert serving.unschedule(f"plan-{slot[1]}", slot[0]) in (200, 404)
                elif kind == "tick":
                    serving.scheduler.tick()
                else:
                    leases.append(serving.quota.acquire_turn(A))
            return run

        interleave = Interleave(patch, serving.state, at_every_point=check_ready)
        for (phase, position), (kind, slot) in steps:
            getattr(interleave, phase)[position].append(operation(kind, slot))
        for slot, (phase, position) in admitted:
            _, _, release = admit_and_wait(serving, slot[1], slot[0])
            finish.append(release)
            getattr(interleave, phase)[position].append(release)
        midway = None
        if in_flight is not None:
            slot, stopped, orphaned = in_flight
            midway = names[slot].digest
            if orphaned:
                _, _, admitted_turn = admit_and_wait(serving, slot[1], slot[0])
                serving.delete(slot[1], slot[0])
                turn = threading.Thread(target=admitted_turn, daemon=True)
            else:
                turn = threading.Thread(
                    target=serving.chat, args=(slot[1], slot[0]), daemon=True)
            reached, proceed = serving.agent.pause_next_turn(names[slot].internal, at=stopped)
            turn.start()
            assert reached.wait(30)
            finish.append(lambda: (proceed.set(), turn.join(60)))

        # The sessions the backup owes an account of: the registered ones and
        # the one with a turn in flight, whose row a deletion may have taken.
        registered = serving.registered() | ({midway} if midway else set())
        # What each holds now, except the one whose turn is in flight: its
        # files are torn until that turn completes.
        at_start = {digest: session_files(serving.state, digest)
                    for digest in registered if digest != midway}
        seen = {digest: len(serving.agent.history[digest]) for digest in registered}
        store_at_start = (serving.schedules.path.read_bytes()
                          if serving.schedules.path.exists() else None)
        if in_flight is not None:
            threading.Timer(0.03, proceed.set).start()
        archive = root / "backups" / "online.tar.gz"
        manifest = online_backup(serving.state, archive, lock_wait_s=60)
        for last in finish:
            last()
        for lease in leases:
            lease.release()
        assert interleave.failures == [], interleave.failures
        assert not_ready == [], f"the service was not ready at {not_ready}"
        assert [p.name for p in archive.parent.iterdir()] == [archive.name]

        restored = root / "restored"
        assert restore_backup(archive, restored, audit_key=AUDIT_KEY) == manifest
        rows = ledger_rows(restored)
        assert set(rows) <= registered, (
            "a session that did not exist when the backup began is in the archive")
        for digest in registered:
            files = session_files(restored, digest)
            if digest not in rows:
                assert digest in deleted and files == {}, (
                    f"a registered session is missing from the archive, or is there "
                    f"without its row: files {sorted(files)}")
                continue
            allowed = serving.agent.history[digest][seen[digest]:]
            if digest in at_start:
                allowed.append(at_start[digest])
            if digest in deleted:
                allowed.append({})
            assert files in allowed, (
                f"a session was archived as {turns_in(files, digest)} turns "
                f"(conversation, chain, counter, notes): not a state a completed "
                f"turn left it in since the backup began")
        for digest in {names[slot].digest for slot in SLOTS} - registered:
            assert session_files(restored, digest) == {}
        capture = manifest["online"]
        assert capture["sessions"] == len(rows)
        assert capture["sessions"] + capture["sessions_deleted_meanwhile"] == len(registered)
        assert ledger_count(restored, "turn_leases") == 0
        store = restored / "product-schedules.json"
        archived_store = store.read_bytes() if store.exists() else None
        assert archived_store in [store_at_start, *serving.schedule_versions], (
            "the archive's schedule store is not one the service saved")
        boots_quietly(restored)


def test_backups_under_free_running_load_restore_and_boot_quietly(tmp_path):
    """The same invariants with nothing arranged: three clients run turns,
    deletions and schedule changes as fast as they can, a prober asks for
    readiness, and backups are taken one after another meanwhile."""
    serving = Serving(tmp_path / "state")
    names = {slot: session(slot[1], slot[0]) for slot in SLOTS}
    stop = threading.Event()
    problems, readies = [], []

    def client(seed):
        choose = random.Random(seed)
        while not stop.is_set():
            tenant, name = choose.choice(SLOTS)
            roll = choose.random()
            try:
                if roll < 0.70:
                    status, fine = serving.chat(name, tenant), (200,)
                elif roll < 0.85:
                    status, fine = serving.delete(name, tenant), (200, 404)
                elif roll < 0.95:
                    status, fine = serving.schedule(f"plan-{name}", name, tenant), (201,)
                else:
                    status, fine = serving.unschedule(f"plan-{name}", tenant), (200, 404)
                if status not in fine:
                    problems.append((tenant, name, status))
            except BaseException as error:  # noqa: BLE001 - reported below
                problems.append(error)

    def prober():
        while not stop.is_set():
            readies.append(serving.ready(verify=False))
            time.sleep(0.002)

    threads = [threading.Thread(target=client, args=(seed,), daemon=True) for seed in range(3)]
    threads.append(threading.Thread(target=prober, daemon=True))
    for thread in threads:
        thread.start()
    archives = []
    try:
        for n in range(4):
            archive = tmp_path / "backups" / f"online-{n}.tar.gz"
            archives.append((archive, online_backup(serving.state, archive, lock_wait_s=60)))
    finally:
        stop.set()
        for thread in threads:
            thread.join(60)
    assert problems == [], problems[:3]
    assert readies and all(readies), "the service was not ready throughout"
    assert sorted(p.name for p in (tmp_path / "backups").iterdir()) == sorted(
        archive.name for archive, _ in archives)
    for n, (archive, manifest) in enumerate(archives):
        restored = tmp_path / f"restored-{n}"
        assert restore_backup(archive, restored, audit_key=AUDIT_KEY) == manifest
        rows = ledger_rows(restored)
        assert manifest["online"]["sessions"] == len(rows)
        for digest in {item.digest for item in names.values()}:
            files = session_files(restored, digest)
            if digest in rows:
                assert files == {} or files in serving.agent.history[digest], (
                    f"backup {n} archived a session as {turns_in(files, digest)} turns "
                    f"(conversation, chain, counter, notes): not a turn boundary")
            else:
                assert files == {}
        boots_quietly(restored)
