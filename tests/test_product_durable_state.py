"""The product's state: synced writes, deletion that takes every file of a
session, and a startup that clears orphaned scratch and reports lost files.

Found by the 2026-11-10 product audit. F-13: the product's schedule store and
its clean-shutdown marker were written by write-then-rename with no `fsync`,
and deleting a session synced nothing, so after a power loss or a kernel
crash the synced quota ledger and the files could disagree in either
direction. Startup checked only for files without ledger rows; a row whose
files were gone was quietly set to zero, so a lost conversation came back as
an empty session with no error. F-30: a save killed between its write and its
rename leaves a scratch file holding the conversation; the product's DELETE
and retention removed the committed file, the sandbox and the audit chain,
and nothing else.

tests/test_durable_state.py pins agent.py's stores (node A3). This file pins
the product's own:

- every schedule-store write, and the clean-shutdown marker, is synced before
  its rename and its directory after it, and a failed one leaves no scratch;
  the previous marker's removal is synced before any state is opened;
- DELETE and retention remove every file that held the session's content,
  scratch included, and nothing of another session, and sync each removal
  before the ledger row goes; they zero the row's bytes before removing any
  file, so an interrupted deletion is not mistaken for lost files;
- a chat turn settles its quota while it still holds the session lock, so a
  DELETE cannot slip between and have its row re-created with stale bytes;
- startup creates its directories durably, removes every orphaned scratch
  file (under the lock that a save in flight would hold), and reports any row
  whose recorded bytes are non-zero but whose files are gone, with one
  content-free critical event;
- by syntax: the product modules write files only through
  agent.write_durably, except the sites listed below.
"""
import ast
import errno
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import example, given, settings, strategies as st

from conftest import PI_ROOT
from test_durable_state import Syscalls, _scopes, assert_durable_replace, identity
from test_product_main import _base_env, _install_happy_runtime

import product_main
import product_service
from agent import AuditLog, SessionStore
from product_service import (
    AuthRegistry,
    DurableQuotaStore,
    ProductDataManager,
    ProductError,
    ProductRetentionMonitor,
    ProductScheduleStore,
    ProductService,
    SessionOperationLocks,
    _internal_session,
)
from state_tool import CLEAN_MARKER, SCHEMA_VERSION

TENANT = "tenant-a"
TOKEN = "durable-product-token-with-enough-entropy-for-a-test-only"
AUDIT_KEY = b"durable-product-audit-key-0123456789"
SECRET = "my secret plan"
TOKEN_HEX = "0123456789abcdef"


def _auth():
    return AuthRegistry([{
        "sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
        "principal": "alice", "tenant": TENANT,
        "scopes": ["chat", "sessions:read", "sessions:delete",
                   "schedules:read", "schedules:write"],
        "tools": []}])


def _principal():
    return _auth().authenticate(f"Bearer {TOKEN}")


def _headers():
    return {"Authorization": f"Bearer {TOKEN}"}


def _agent(root, key=AUDIT_KEY):
    """The stores PiAgent holds, on disk, as ProductDataManager reaches them."""
    return SimpleNamespace(
        manifest={}, memory=None, _mcp=object(),
        store=SessionStore(root / "sessions"),
        sandbox_root=root / "sandboxes",
        audit=AuditLog(root / "audit", key=key))


def _quota(root, clock=None):
    return DurableQuotaStore(
        root / "product-quotas.sqlite3", clock=clock, max_concurrent_turns=10,
        tokens_per_day=10_000_000, token_reservation_per_turn=1000,
        storage_bytes_per_tenant=1 << 30, storage_reservation_per_turn=1 << 20,
        audit_bytes_per_tenant=1 << 28, audit_reservation_per_turn=1 << 16)


def _manager(root, agent, quota, clock=None):
    return ProductDataManager(
        agent, ProductScheduleStore(root / "product-schedules.json", clock=clock),
        SessionOperationLocks(root / "locks"), quota_store=quota)


class Session:
    """Every path that can hold one product session's content."""

    def __init__(self, agent, external, tenant=TENANT):
        self.external = external
        self.internal = _internal_session(tenant, external)
        self.digest = hashlib.sha256(self.internal.encode()).hexdigest()
        self.conversation = agent.store._path(self.internal)
        self.new_scratch = self.conversation.with_name(
            f".{self.conversation.name}.{TOKEN_HEX}.tmp")
        self.legacy_scratch = self.conversation.with_suffix(".kv.tmp")
        self.sandbox = Path(agent.sandbox_root) / self.digest[:16]
        self.chain = agent.audit._path(self.internal)

    def paths(self):
        return {"conversation": self.conversation, "new_scratch": self.new_scratch,
                "legacy_scratch": self.legacy_scratch, "sandbox": self.sandbox,
                "chain": self.chain}


KINDS = ("conversation", "new_scratch", "legacy_scratch", "sandbox", "chain")
COMMITTED = {"conversation", "sandbox", "chain"}


def _plant(agent, session, kinds, text):
    """Give `session` the files of `kinds`, each holding `text`."""
    encoded = json.dumps([{"role": "user", "content": text}]).encode()
    if "conversation" in kinds:
        agent.store.save(session.internal, [{"role": "user", "content": text}])
    if "new_scratch" in kinds:
        session.new_scratch.write_bytes(encoded)
    if "legacy_scratch" in kinds:
        session.legacy_scratch.write_bytes(encoded)
    if "sandbox" in kinds:
        session.sandbox.mkdir(parents=True, exist_ok=True)
        (session.sandbox / "note.txt").write_text(text)
    if "chain" in kinds:
        agent.audit.record(session.internal, "test", "source", text, "output",
                           None, None, 10)


def _register(quota, session, *, storage_bytes, audit_bytes):
    """A settled turn's registry row: what the ledger believes is on disk."""
    lease = quota.acquire_turn(TENANT, session_id=session.internal)
    lease.release(session_id=session.internal, storage_bytes=storage_bytes,
                  audit_bytes=audit_bytes)


def _register_footprint(quota, manager, session):
    footprint = manager.footprint(session.internal)
    _register(quota, session, storage_bytes=footprint["storage_bytes"],
              audit_bytes=footprint["audit_bytes"])
    return footprint


def _holding(directory, needle):
    return sorted(str(path.relative_to(directory)) for path in Path(directory).rglob("*")
                  if path.is_file() and needle.encode() in path.read_bytes())


def _charged(quota, tenant=TENANT):
    usage = quota.tenant_usage(tenant)
    return usage["storage_bytes"], usage["audit_bytes"]


def _retention(quota, manager, now):
    return ProductRetentionMonitor(
        quota, manager, manager.schedule_store, manager.session_locks,
        retention_s=500, clock=lambda: now[0], log_sink=lambda record: None)


# ── F-13: every schedule-store write is synced ───────────────────────────


def _schedule_operations():
    """Each ProductScheduleStore operation that rewrites the store, as
    (name, prepare, run): `prepare` puts the store in a state where the
    operation has something to change."""
    principal = _principal()
    key = ProductScheduleStore._key(TENANT, "daily")
    internal = _internal_session(TENANT, "work")
    tenant_hash = hashlib.sha256(TENANT.encode()).hexdigest()

    def put(store):
        store.put(principal, name="daily", session="work", message=SECRET,
                  every_ms=60_000)

    return [
        ("put", lambda store: None, put),
        ("remove", put, lambda store: store.remove(TENANT, "daily")),
        ("remove_session", put, lambda store: store.remove_session(TENANT, "work")),
        ("remove_internal", put,
         lambda store: store.remove_internal(tenant_hash, internal)),
        ("remove_inactive", put, lambda store: store.remove_inactive(10**10, 10**10)),
        ("mark_run", put, lambda store: store.mark_run(key, 123.0, "ok")),
        ("claim_due", put, lambda store: store.claim_due(key, 10**6, "worker", 300)),
        ("complete_claim",
         lambda store: (put(store), store.claim_due(key, 10**6, "worker", 300)),
         lambda store: store.complete_claim(key, "worker", 10**6, "ok")),
    ]


@pytest.mark.parametrize("operation", _schedule_operations(), ids=lambda op: op[0])
def test_every_schedule_store_write_syncs_its_bytes_then_renames_then_syncs_the_directory(
        tmp_path, monkeypatch, operation):
    _, prepare, run = operation
    store = ProductScheduleStore(tmp_path / "product-schedules.json")
    prepare(store)
    calls = Syscalls(monkeypatch)
    run(store)
    assert_durable_replace(calls.events, store.path, store.path.read_bytes())


_SCHEDULE_CHILD = r"""
import hashlib, json, os, resource, signal, sys
sys.path.insert(0, sys.argv[1])
sys.dont_write_bytecode = True
from product_service import AuthRegistry, ProductScheduleStore
token = sys.argv[3]
auth = AuthRegistry([{"sha256": hashlib.sha256(token.encode()).hexdigest(),
                      "principal": "alice", "tenant": "tenant-a",
                      "scopes": ["schedules:write"], "tools": []}])
principal = auth.authenticate("Bearer " + token)
store = ProductScheduleStore(sys.argv[2])
store.put(principal, name="daily", session="work", message="committed", every_ms=60000)
"""

SCHEDULE_SAVE_PAST_THE_FILE_SIZE_LIMIT = _SCHEDULE_CHILD + r"""
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
resource.setrlimit(resource.RLIMIT_FSIZE,
                   (4096, resource.getrlimit(resource.RLIMIT_FSIZE)[1]))
try:
    store.put(principal, name="other", session="work",
              message="my secret plan " + "x" * 65536, every_ms=60000)
except OSError as error:
    print(json.dumps({"errno": error.errno}))
else:
    print(json.dumps({"errno": None}))
"""

SCHEDULE_SAVE_KILLED_AT_ITS_RENAME = _SCHEDULE_CHILD + r"""
def killed(*args, **kwargs):
    os.kill(os.getpid(), signal.SIGKILL)
os.replace = os.rename = killed
store.put(principal, name="other", session="work", message="my secret plan",
          every_ms=60000)
"""


def _schedule_child(code, path):
    return subprocess.run(
        [sys.executable, "-c", code, str(PI_ROOT), str(path), TOKEN],
        capture_output=True, text=True, timeout=60)


def test_a_schedule_save_that_fails_partway_through_its_write_leaves_no_scratch(tmp_path):
    """A real write failure: the child's file size limit stops the save at 4
    KiB of a 64 KiB schedule, as a filling disk does, and the kernel fails
    the write with EFBIG. The schedule message is customer content."""
    path = tmp_path / "product-schedules.json"
    child = _schedule_child(SCHEDULE_SAVE_PAST_THE_FILE_SIZE_LIMIT, path)
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout) == {"errno": errno.EFBIG}, child.stdout
    left = sorted(os.listdir(tmp_path))
    assert left == [path.name, f"{path.name}.lock"], (
        f"a failed schedule save left scratch behind: {left}; holding the "
        f"message: {_holding(tmp_path, SECRET)}")
    assert [e["message"] for _, e in ProductScheduleStore(path).entries()] == ["committed"]


@pytest.mark.parametrize("fault", [
    lambda: OSError(errno.EIO, "injected I/O error at the rename"),
    lambda: KeyboardInterrupt(),
], ids=["io-error", "interrupt"])
def test_a_schedule_save_that_fails_at_its_rename_leaves_no_scratch(
        tmp_path, monkeypatch, fault):
    store = ProductScheduleStore(tmp_path / "product-schedules.json")
    principal = _principal()
    store.put(principal, name="daily", session="work", message="committed",
              every_ms=60_000)
    injected = fault()
    real = os.replace

    def replace(src, dst, *args, **kwargs):
        if Path(os.fsdecode(dst)) == store.path:
            raise injected
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(type(injected)) as raised:
        store.put(principal, name="other", session="work", message=SECRET,
                  every_ms=60_000)
    assert raised.value is injected, "the save must re-raise the failure unchanged"
    monkeypatch.undo()
    left = sorted(os.listdir(tmp_path))
    assert left == [store.path.name, f"{store.path.name}.lock"], (
        f"a failed schedule save left scratch behind: {left}")
    assert [e["name"] for _, e in store.entries()] == ["daily"]


def test_a_killed_schedule_save_leaves_scratch_that_the_store_removes(tmp_path):
    """Only a killed process leaves scratch now. The child is killed at the
    rename, after the whole schedule, message included, was written; the
    store's sweep, which startup runs, then removes exactly that file."""
    path = tmp_path / "product-schedules.json"
    child = _schedule_child(SCHEDULE_SAVE_KILLED_AT_ITS_RENAME, path)
    assert child.returncode == -signal.SIGKILL, (child.returncode, child.stderr)
    scratch = sorted(set(os.listdir(tmp_path)) - {path.name, f"{path.name}.lock"})
    assert len(scratch) == 1 and _holding(tmp_path, SECRET) == scratch, (
        f"anti-vacuity: the killed save should leave one scratch file holding "
        f"the message; found {scratch}, holding it: {_holding(tmp_path, SECRET)}")
    store = ProductScheduleStore(path)
    assert [e["message"] for _, e in store.entries()] == ["committed"], (
        "the uncommitted save was adopted")
    assert store.remove_scratch() == 1
    assert sorted(os.listdir(tmp_path)) == [path.name, f"{path.name}.lock"]
    assert [e["message"] for _, e in store.entries()] == ["committed"]


@pytest.mark.parametrize("kind", ["schedules", "quota"])
def test_a_product_store_that_creates_its_directories_syncs_each_into_its_parent(
        tmp_path, monkeypatch, kind):
    """A synced file in a directory that a power loss can take away is not
    durable. SQLite syncs the directory that holds the ledger, not that
    directory's own entry in its parent."""
    calls = Syscalls(monkeypatch)
    nested = tmp_path / "state" / "nested"
    if kind == "schedules":
        ProductScheduleStore(nested / "product-schedules.json")
    else:
        _quota(nested)
    synced = {event[2] for event in calls.events if event[:2] == ("fsync", "dir")}
    made = [tmp_path / "state", nested]
    unsynced = [str(d.relative_to(tmp_path)) for d in made if identity(d.parent) not in synced]
    assert unsynced == [], f"created but never synced into its parent: {unsynced}"


@pytest.mark.parametrize("existing", [(), ("sessions", "audit")],
                         ids=["a-new-deployment", "only-the-sandbox-root-is-missing"])
def test_startup_creates_its_state_directories_durably(tmp_path, monkeypatch, existing):
    """Every directory product startup creates for state is synced into its
    parent after it is created and before the service serves. (locks/ holds
    only empty lock files; a lost one is recreated at the next start.)"""
    _base_env(tmp_path, monkeypatch)
    state = tmp_path / "deploy" / "state"
    monkeypatch.setenv("PI_STATE", str(state))
    for name in existing:
        (state / name).mkdir(parents=True)
    _install_happy_runtime(monkeypatch)
    calls = Syscalls(monkeypatch)
    real_mkdir, serve = os.mkdir, product_main.serve_product

    def mkdir(path, *args, **kwargs):
        real_mkdir(path, *args, **kwargs)
        calls.events.append(("mkdir", Path(os.fsdecode(path))))

    def serving(*args, **kwargs):
        calls.events.append(("serving",))
        return serve(*args, **kwargs)

    monkeypatch.setattr(os, "mkdir", mkdir)
    monkeypatch.setattr(product_main, "serve_product", serving)
    product_main.run()
    expected = ([state / "sandboxes"] if existing else
                [tmp_path / "deploy", state, state / "sessions", state / "sandboxes",
                 state / "audit"])
    made = [event[1] for event in calls.events if event[0] == "mkdir"]
    assert set(expected) <= set(made), f"anti-vacuity: startup created {made}"
    serving_at = calls.events.index(("serving",))
    unsynced = []
    for directory in expected:
        at = calls.events.index(("mkdir", directory))
        if not any(event[:3] == ("fsync", "dir", identity(directory.parent))
                   for event in calls.events[at + 1:serving_at]):
            unsynced.append(str(directory.relative_to(tmp_path)))
    assert unsynced == [], (
        f"product startup created state directories and began serving without "
        f"syncing them into their parents: {unsynced}")


# ── F-13: the clean-shutdown marker ──────────────────────────────────────


def _note_when_state_is_opened(monkeypatch, note):
    """Call `note()` when startup constructs its first store: everything
    before that happens before any state is opened."""
    real = product_main.SessionStore

    def opening(directory):
        note()
        return real(directory)

    monkeypatch.setattr(product_main, "SessionStore", opening)


def test_the_clean_shutdown_marker_is_synced_before_a_backup_can_trust_it(
        tmp_path, monkeypatch):
    """The marker is what lets an offline backup trust the state. It was a
    bare write-then-rename: a power loss after a clean stop could leave it
    missing, empty or partial, and a backup of cleanly drained state was
    then refused."""
    _base_env(tmp_path, monkeypatch)
    _install_happy_runtime(monkeypatch)
    calls = Syscalls(monkeypatch)
    product_main.run()
    state = tmp_path / "state"
    marker = state / CLEAN_MARKER
    assert_durable_replace(calls.events, marker, marker.read_bytes())
    assert json.loads(marker.read_text())["schema_version"] == SCHEMA_VERSION
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert [p.name for p in state.iterdir() if p.name.endswith(".tmp")] == []


def test_startup_makes_the_old_markers_removal_durable_before_it_opens_state(
        tmp_path, monkeypatch):
    """Startup removes the previous marker so that a crash from here on
    cannot be backed up as if it were a clean stop. An unlink that is never
    synced can be undone by a power loss, and the marker then vouches for
    state that was not drained."""
    _base_env(tmp_path, monkeypatch)
    _install_happy_runtime(monkeypatch)
    product_main.run()                      # a clean stop: the marker exists
    state = tmp_path / "state"
    marker = state / CLEAN_MARKER
    assert marker.is_file()
    calls = Syscalls(monkeypatch)
    _record_removals(monkeypatch, calls)
    _note_when_state_is_opened(
        monkeypatch, lambda: calls.events.append(("state opened",)))
    product_main.run()
    events = calls.events
    removed, opened = events.index(("removed", marker)), events.index(("state opened",))
    assert removed < opened
    assert any(event[:3] == ("fsync", "dir", identity(state))
               for event in events[removed + 1:opened]), (
        "startup removed the previous clean-shutdown marker and opened state "
        "without syncing the removal: a power loss can bring the marker back")


def test_startup_removes_what_a_killed_marker_write_left(tmp_path, monkeypatch):
    """A stop killed while it wrote the marker leaves the marker's scratch:
    `.product-clean-shutdown.tmp` from releases before 2026-11-10, and
    write_durably's name since. Neither is ever read; startup removes both
    before it opens state, and nothing that only resembles them."""
    _base_env(tmp_path, monkeypatch)
    _install_happy_runtime(monkeypatch)
    state = tmp_path / "state"
    state.mkdir()
    marker = state / CLEAN_MARKER
    leftovers = [marker.with_suffix(".tmp"),
                 marker.with_name(f".{marker.name}.{TOKEN_HEX}.tmp")]
    resembling = [marker.with_name(f".{marker.name}.{TOKEN_HEX[:15]}.tmp"),
                  marker.with_name(f"{marker.name}.bak")]
    for path in leftovers + resembling:
        path.write_text("{}")
    seen = {}
    _note_when_state_is_opened(monkeypatch, lambda: seen.update(
        left=[p.name for p in leftovers if p.exists()],
        kept=[p.name for p in resembling if p.exists()]))
    product_main.run()
    assert seen["left"] == [], (
        f"state was opened with a killed marker write's scratch still there: "
        f"{seen['left']}")
    assert seen["kept"] == [p.name for p in resembling]
    assert all(p.exists() for p in resembling) and not any(p.exists() for p in leftovers)


# ── F-30: deletion takes every file of the session, durably ──────────────


@settings(max_examples=80, deadline=None)
@given(deleted=st.sets(st.sampled_from(KINDS)), kept=st.sets(st.sampled_from(KINDS)),
       path=st.sampled_from(("api", "retention")))
@example(deleted={"conversation", "legacy_scratch", "sandbox", "chain"}, kept=set(),
         path="api")     # audit/repro/r11_orphaned_scratch.py
@example(deleted={"new_scratch"}, kept=set(KINDS), path="api")
@example(deleted={"new_scratch", "legacy_scratch"}, kept=set(KINDS), path="retention")
@example(deleted=set(KINDS), kept=set(KINDS), path="retention")
@example(deleted=set(), kept=set(), path="retention")
def test_deletion_takes_every_file_of_the_session_and_nothing_else(
        tmp_path_factory, deleted, kept, path):
    """THE GUARD for F-30 at the product boundary. Whatever mix of committed
    files and scratch two sessions of one tenant hold, deleting one (by
    DELETE or by retention) leaves none of its files, and every file of the
    other exactly as it was. A session with nothing but scratch is still
    "not found" to DELETE, and its scratch is removed all the same."""
    root = tmp_path_factory.mktemp("delete")
    now = [100.0]
    agent = _agent(root)
    quota = _quota(root, clock=lambda: now[0])
    manager = _manager(root, agent, quota, clock=lambda: now[0])
    gone, stays = Session(agent, "deleted"), Session(agent, "kept")
    _plant(agent, gone, deleted, "the deleted session's secret")
    _plant(agent, stays, kept, "the kept session's words")
    _register(quota, gone, storage_bytes=1, audit_bytes=1)
    now[0] = 900.0
    _register(quota, stays, storage_bytes=1, audit_bytes=1)
    before = {kind: (p.read_bytes() if p.is_file() else sorted(os.listdir(p)))
              for kind, p in stays.paths().items() if p.exists()}
    now[0] = 1000.0
    if path == "api":
        try:
            answer = manager.delete(_principal(), gone.external)
        except ProductError as error:
            assert error.code == "session_not_found"
            assert not deleted & COMMITTED
        else:
            assert answer == {"conversation": "conversation" in deleted,
                              "sandbox": "sandbox" in deleted,
                              "audit": "chain" in deleted, "schedules": 0}
            assert deleted & COMMITTED
    else:
        assert _retention(quota, manager, now).tick() is True
    left = sorted(kind for kind, p in gone.paths().items() if p.exists())
    assert left == [], (
        f"deleting the session by {path} left files that held its content: {left}")
    assert _holding(root, "the deleted session's secret") == []
    after = {kind: (p.read_bytes() if p.is_file() else sorted(os.listdir(p)))
             for kind, p in stays.paths().items() if p.exists()}
    assert after == before, "deleting one session touched another's files"
    assert _charged(quota) == (1, 1), (
        "the deleted session is charged nothing, and the other session's row "
        "is as it was")


def _record_removals(monkeypatch, calls):
    """Add each unlink of a path and each tree removal to `calls.events`, in
    order with its syncs."""
    real_unlink, real_rmtree = os.unlink, shutil.rmtree

    def unlink(path, *args, **kwargs):
        real_unlink(path, *args, **kwargs)
        if "dir_fd" not in kwargs:
            calls.events.append(("removed", Path(os.fsdecode(path))))

    def rmtree(path, *args, **kwargs):
        real_rmtree(path, *args, **kwargs)
        calls.events.append(("removed", Path(os.fsdecode(path))))

    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(shutil, "rmtree", rmtree)


@pytest.mark.parametrize("path", ["api", "retention"])
def test_a_deletion_is_synced_before_its_registry_row_goes(tmp_path, monkeypatch, path):
    """The ledger row is removed last and is synced (SQLite synchronous=FULL).
    If the unlinks before it are not, a power loss brings the files back with
    no row, and startup then refuses the whole state ("customer state lacks
    durable quota registry entries")."""
    now = [100.0]
    agent = _agent(tmp_path)
    quota = _quota(tmp_path, clock=lambda: now[0])
    manager = _manager(tmp_path, agent, quota, clock=lambda: now[0])
    gone = Session(agent, "deleted")
    _plant(agent, gone, KINDS, SECRET)
    _register(quota, gone, storage_bytes=1, audit_bytes=1)
    now[0] = 1000.0
    calls = Syscalls(monkeypatch)
    _record_removals(monkeypatch, calls)
    remover = "remove_session_usage" if path == "api" else "remove_registered_session"
    real_remove = getattr(quota, remover)

    def remove(*args, **kwargs):
        calls.events.append(("row removed",))
        return real_remove(*args, **kwargs)

    monkeypatch.setattr(quota, remover, remove)
    if path == "api":
        manager.delete(_principal(), gone.external)
    else:
        assert _retention(quota, manager, now).tick() is True
    events = calls.events
    at = events.index(("row removed",))
    unsynced = []
    for directory in (agent.store.dir, agent.audit.dir, Path(agent.sandbox_root)):
        removals = [i for i, event in enumerate(events[:at])
                    if event[0] == "removed" and event[1].parent == directory]
        assert removals, f"anti-vacuity: nothing was removed from {directory.name}"
        if not any(event[:3] == ("fsync", "dir", identity(directory))
                   for event in events[removals[-1] + 1:at]):
            unsynced.append(directory.name)
    assert unsynced == [], (
        f"the {path} deletion removed files from {unsynced} without syncing the "
        f"directory before the registry row was removed: a power loss can bring "
        f"them back with no row, and startup then refuses the whole state")


@pytest.mark.parametrize("path", ["api", "retention"])
def test_an_interrupted_deletion_is_not_reported_as_lost_files(tmp_path, monkeypatch, path):
    """A deletion removes the files, then the row; a process that dies between
    the two leaves a row whose files are gone, which startup retries rather
    than reports. Deletion therefore zeroes the row's bytes before it removes
    any file: the next startup sees nothing recorded for the missing files."""
    now = [100.0]
    agent = _agent(tmp_path)
    quota = _quota(tmp_path, clock=lambda: now[0])
    manager = _manager(tmp_path, agent, quota, clock=lambda: now[0])
    gone = Session(agent, "deleted")
    _plant(agent, gone, KINDS, SECRET)
    recorded = _register_footprint(quota, manager, gone)
    assert recorded["storage_bytes"] > 0 and recorded["audit_bytes"] > 0
    now[0] = 1000.0
    remover = "remove_session_usage" if path == "api" else "remove_registered_session"

    def dies_here(*args, **kwargs):
        raise OSError(errno.EIO, "injected: the host died before the row was removed")

    monkeypatch.setattr(quota, remover, dies_here)
    if path == "api":
        with pytest.raises(OSError, match="injected"):
            manager.delete(_principal(), gone.external)
    else:
        assert _retention(quota, manager, now).tick() is False
    monkeypatch.undo()
    assert [kind for kind, p in gone.paths().items() if p.exists()] == []
    assert [row[2] for row in quota.registered_sessions()] == [gone.internal], (
        "anti-vacuity: the row must outlive the interrupted deletion")
    events = []
    manager.reconcile_at_startup(log_sink=events.append)
    assert [event for event in events if event["severity"] == "critical"] == [], (
        f"an interrupted {path} deletion was reported as lost files: {events}")


@pytest.mark.parametrize("path", ["api", "retention"])
@pytest.mark.parametrize("step", ["sandbox", "chain"])
def test_a_deletion_that_fails_partway_is_not_reported_as_lost_files(
        tmp_path, monkeypatch, path, step):
    """The conversation goes first. A deletion that then fails, at the
    sandbox or at the audit chain, has removed files whose bytes the row
    would still record had it not been zeroed before the first file went.
    What is left is still attributed, and counted again at the next start."""
    now = [100.0]
    agent = _agent(tmp_path)
    quota = _quota(tmp_path, clock=lambda: now[0])
    manager = _manager(tmp_path, agent, quota, clock=lambda: now[0])
    gone = Session(agent, "deleted")
    _plant(agent, gone, ("conversation", "chain"), SECRET)
    gone.sandbox.mkdir(parents=True)       # no file in it: storage is the conversation
    recorded = _register_footprint(quota, manager, gone)
    assert recorded["storage_bytes"] > 0 and recorded["audit_bytes"] > 0
    now[0] = 1000.0
    real_unlink, real_rmtree = os.unlink, shutil.rmtree

    def unlink(target, *args, **kwargs):
        if step == "chain" and Path(os.fsdecode(target)) == gone.chain:
            raise OSError(errno.EIO, "injected: the audit chain could not be removed")
        return real_unlink(target, *args, **kwargs)

    def rmtree(target, *args, **kwargs):
        if step == "sandbox":
            raise OSError(errno.EIO, "injected: the sandbox could not be removed")
        return real_rmtree(target, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(shutil, "rmtree", rmtree)
    if path == "api":
        with pytest.raises(OSError, match="injected"):
            manager.delete(_principal(), gone.external)
    else:
        assert _retention(quota, manager, now).tick() is False
    monkeypatch.undo()
    assert not gone.conversation.exists() and gone.chain.is_file(), (
        "anti-vacuity: the deletion stopped after the conversation, before the chain")
    assert [row[2] for row in quota.registered_sessions()] == [gone.internal]
    events = []
    manager.reconcile_at_startup(log_sink=events.append)
    assert [event["severity"] for event in events] == ["info"], (
        f"a {path} deletion that failed at the {step} was reported as lost files: "
        f"{events}")
    assert _charged(quota) == (0, gone.chain.stat().st_size)


def test_a_session_that_wakes_as_retention_deletes_it_is_charged_for_nothing(
        tmp_path, monkeypatch):
    """A turn admitted while the sweep holds the session's lock refreshes the
    row, and the sweep then keeps it (tests/test_product_service.py pins
    that). The kept row described files the sweep had just removed: the
    tenant stayed charged for them until the turn settled, and a crash before
    that left a row that the next startup would report as lost files."""
    now = [100.0]
    agent = _agent(tmp_path)
    quota = _quota(tmp_path, clock=lambda: now[0])
    manager = _manager(tmp_path, agent, quota, clock=lambda: now[0])
    woken = Session(agent, "woken")
    _plant(agent, woken, KINDS, SECRET)
    _register_footprint(quota, manager, woken)
    now[0] = 1000.0
    real_remove = quota.remove_registered_session

    def refresh_then_remove(tenant, session, *, cutoff_unix=None):
        quota.acquire_turn(TENANT, session_id=woken.internal).release()
        return real_remove(tenant, session, cutoff_unix=cutoff_unix)

    monkeypatch.setattr(quota, "remove_registered_session", refresh_then_remove)
    assert _retention(quota, manager, now).tick() is True
    assert [kind for kind, p in woken.paths().items() if p.exists()] == []
    assert [row[2] for row in quota.registered_sessions()] == [woken.internal], (
        "anti-vacuity: the refreshed row must be kept")
    assert _charged(quota) == (0, 0), (
        f"retention removed the session's files and left the tenant charged "
        f"{_charged(quota)} (storage, audit) bytes for them")
    events = []
    manager.reconcile_at_startup(log_sink=events.append)
    assert [event["severity"] for event in events] == ["info"]


# ── a chat turn settles under the session lock ───────────────────────────


class SavingAgent:
    """Stands in for PiAgent at the product boundary. Each turn saves the
    conversation and appends an audit record, as a real turn does; a failing
    one raises after that, having counted its tokens."""
    manifest = {}
    memory = None
    _mcp = object()

    def __init__(self, root, fails=False):
        self.store = SessionStore(root / "sessions")
        self.sandbox_root = root / "sandboxes"
        self.sandbox_root.mkdir()
        self.audit = AuditLog(root / "audit", key=AUDIT_KEY)
        self.fails = fails

    def turn_with_usage(self, session, message, *, allowed_tools=None,
                        deadline_monotonic=None):
        self.store.save(session, [{"role": "user", "content": message}])
        self.audit.record(session, "llm", "source", message, "output", None, None, 1)
        if self.fails:
            raise RuntimeError("the provider failed after the turn wrote its state")
        return "noted", {"input_tokens": 1, "output_tokens": 1}

    def turn_telemetry(self):
        return {"input_tokens": 7, "output_tokens": 5} if self.fails else {}


def _chat_service(root, fails=False):
    agent = SavingAgent(root, fails=fails)
    quota = _quota(root)
    manager = _manager(root, agent, quota)
    service = ProductService(
        agent, _auth(), quota_store=quota, session_locks=manager.session_locks,
        data_manager=manager, schedule_store=manager.schedule_store,
        log_sink=lambda record: None)
    return service, agent, quota


def _session_lock_is_free(root, internal):
    """Whether another worker could take `internal`'s session lock now."""
    try:
        with SessionOperationLocks(root / "locks").hold(
                internal, deadline_monotonic=time.monotonic() + 0.05):
            return True
    except TimeoutError:
        return False


CHAT = json.dumps({"session": "s", "message": SECRET}).encode()


@pytest.mark.parametrize("fails", [False, True], ids=["turn-succeeds", "turn-fails"])
def test_a_chat_turn_settles_its_quota_while_it_holds_the_session_lock(
        tmp_path, monkeypatch, fails):
    """A scheduled turn already settles inside its session lock. A chat turn
    measured its files under the lock and settled after releasing it, so a
    DELETE could take the lock in between (see the next test)."""
    service, _, quota = _chat_service(tmp_path, fails=fails)
    internal = _internal_session(TENANT, "s")
    lock_free_at_settlement = []
    real = quota.settle_turn

    def settle(lease_id, **kwargs):
        lock_free_at_settlement.append(_session_lock_is_free(tmp_path, internal))
        return real(lease_id, **kwargs)

    monkeypatch.setattr(quota, "settle_turn", settle)
    status = service.dispatch("POST", "/v1/chat", _headers(), CHAT)[0]
    assert status == (502 if fails else 200)
    assert lock_free_at_settlement == [False], (
        "the chat turn released the session lock before settling its quota: "
        f"{lock_free_at_settlement}")
    footprint = service.data_manager.footprint(internal)
    usage = quota.tenant_usage(TENANT)
    assert _charged(quota) == (footprint["storage_bytes"], footprint["audit_bytes"])
    assert usage["tokens_today"] == (12 if fails else 2)
    assert usage["reserved_tokens"] == usage["reserved_storage_bytes"] == 0


def test_a_delete_that_lands_as_a_chat_turn_ends_leaves_nothing_charged(
        tmp_path, monkeypatch):
    """A DELETE that takes the session lock after the turn released it but
    before the turn settled: the settlement re-created the registry row the
    DELETE removed, with the bytes of files that no longer exist. The tenant
    stayed charged for them, and the next startup would report them lost."""
    service, agent, quota = _chat_service(tmp_path)
    internal = _internal_session(TENANT, "s")
    outcome, workers = {}, []
    real = quota.settle_turn

    def delete():
        outcome["delete"] = service.dispatch("DELETE", "/v1/sessions/s", _headers())[0]

    def settle(lease_id, **kwargs):
        if not outcome and not workers:
            if _session_lock_is_free(tmp_path, internal):
                delete()        # the window is open: run the DELETE inside it
            else:
                worker = threading.Thread(target=delete)
                worker.start()  # it waits for the lock, as a real DELETE would
                workers.append(worker)
        return real(lease_id, **kwargs)

    monkeypatch.setattr(quota, "settle_turn", settle)
    assert service.dispatch("POST", "/v1/chat", _headers(), CHAT)[0] == 200
    for worker in workers:
        worker.join(10)
        assert not worker.is_alive()
    assert outcome == {"delete": 200}
    assert not agent.store._path(internal).exists()
    assert _charged(quota) == (0, 0), (
        f"DELETE returned 200, yet the tenant is still charged {_charged(quota)} "
        f"(storage, audit) bytes for files that no longer exist: the turn's "
        f"settlement re-created the row the deletion removed")
    events = []
    service.data_manager.reconcile_at_startup(log_sink=events.append)
    assert [event["severity"] for event in events] == ["info"]


def test_a_turn_that_never_reaches_its_session_frees_its_reservation(tmp_path):
    """The other way out of a chat turn: the deadline passes while another
    worker holds the session. Nothing ran, so nothing is recorded for the
    session, and the reservation taken at admission is released."""
    service, _, quota = _chat_service(tmp_path)
    service.turn_deadline_s = 0.05
    internal = _internal_session(TENANT, "s")
    with SessionOperationLocks(tmp_path / "locks").hold(internal):
        assert service.dispatch("POST", "/v1/chat", _headers(), CHAT)[0] == 504
    usage = quota.tenant_usage(TENANT)
    assert _charged(quota) == (0, 0)
    assert (usage["tokens_today"], usage["reserved_tokens"],
            usage["reserved_storage_bytes"], usage["reserved_audit_bytes"]) == (0, 0, 0, 0)


def test_deleting_a_session_removes_the_scratch_an_interrupted_save_left(tmp_path):
    """audit/repro/r11_orphaned_scratch.py through the product's dispatch: a
    turn, then the scratch file an interrupted save of that session leaves
    (the name releases before 2026-11-10 used, and write_durably's), then
    DELETE. It returned 200 and the scratch still held the message."""
    service, agent, quota = _chat_service(tmp_path)
    assert service.dispatch("POST", "/v1/chat", _headers(), CHAT)[0] == 200
    session = Session(agent, "s")
    for scratch in (session.legacy_scratch, session.new_scratch):
        scratch.write_bytes(session.conversation.read_bytes())
    status, _, body = service.dispatch("DELETE", "/v1/sessions/s", _headers())
    assert status == 200 and body["deleted"]["conversation"] is True
    assert _holding(tmp_path, SECRET) == [], (
        f"DELETE returned 200 and these files still hold the message: "
        f"{_holding(tmp_path, SECRET)}")
    assert _charged(quota) == (0, 0)


# ── startup: orphaned scratch and lost files ─────────────────────────────


def _boot(capsys):
    """Run the product entry point once, over test_product_main's fakes, and
    return the structured events it printed."""
    capsys.readouterr()
    product_main.run()
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


def _state_with_one_session(tmp_path, monkeypatch, capsys, external):
    """A booted product state holding one registered session of the auth
    file's tenant, with every file, signed with the product's audit key."""
    _base_env(tmp_path, monkeypatch)
    _install_happy_runtime(monkeypatch)
    _boot(capsys)
    state = tmp_path / "state"
    agent = _agent(state, key=os.environ["PI_AUDIT_KEY"].encode())
    session = Session(agent, external, tenant="tenant")
    _plant(agent, session, COMMITTED, SECRET)
    quota = _quota(state)
    lease = quota.acquire_turn("tenant", session_id=session.internal)
    footprint = ProductDataManager(
        agent, ProductScheduleStore(state / "product-schedules.json"),
        SessionOperationLocks()).footprint(session.internal)
    lease.release(session_id=session.internal, **footprint)
    return state, agent, session, quota


def test_startup_reports_a_registry_row_whose_files_are_gone(tmp_path, monkeypatch, capsys):
    """F-13's impact, end to end through product_main.run(). The ledger
    recorded a turn's bytes; a power loss took the files. Startup used to set
    the row to zero and say nothing, so the tenant found an empty session."""
    state, _, session, quota = _state_with_one_session(
        tmp_path, monkeypatch, capsys, "lost-on-power-loss")
    recorded = quota.tenant_usage("tenant")
    assert recorded["storage_bytes"] > 0 and recorded["audit_bytes"] > 0
    session.conversation.unlink()
    shutil.rmtree(session.sandbox)
    session.chain.unlink()
    events = _boot(capsys)
    critical = [event for event in events if event.get("severity") == "critical"]
    assert critical == [{
        "event": "state_reconciliation", "severity": "critical", "status": "failed",
        "sessions": 1, "sessions_missing_storage": 1, "sessions_missing_audit": 1,
        "scratch_files_removed": 0,
    }], (f"the registry recorded {recorded['storage_bytes']} storage and "
         f"{recorded['audit_bytes']} audit bytes for a session whose files are gone, "
         f"and startup reported: {critical or 'nothing'}")
    rendered = json.dumps(events)
    for private in ("lost-on-power-loss", session.internal, session.digest,
                    session.digest[:16], str(state), SECRET):
        assert private not in rendered, f"a startup event carried {private!r}"
    assert _charged(quota, "tenant") == (0, 0), (
        "after the report the registry must agree with the files")
    again = [event for event in _boot(capsys)
             if event.get("event") == "state_reconciliation"]
    assert [event["severity"] for event in again] == ["info"], (
        "the loss is reported by the startup that finds it, once")


def test_startup_removes_orphaned_scratch_of_every_kind(tmp_path, monkeypatch, capsys):
    """F-30's leftovers, end to end through product_main.run(). A process
    killed between a write and its rename leaves scratch holding customer
    content; nothing removed it. Startup runs before this worker serves
    anything, so every scratch file is orphaned. Names that only resemble
    scratch stay."""
    state, agent, session, _ = _state_with_one_session(
        tmp_path, monkeypatch, capsys, "kept")
    schedules = state / "product-schedules.json"
    orphaned = [
        session.new_scratch, session.legacy_scratch,
        Session(agent, "never-committed", tenant="tenant").new_scratch,
        schedules.with_name(f".{schedules.name}.{TOKEN_HEX}.tmp"),
        schedules.with_name(f".{schedules.name}.{os.getpid()}.tmp"),
    ]
    resembling = [
        session.conversation.with_name(f".{session.conversation.name}.{TOKEN_HEX[:15]}.tmp"),
        session.conversation.with_name(f"{session.conversation.name}.bak"),
        schedules.with_name(f".{schedules.name}.tmp"),
        schedules.with_name(f"{schedules.name}.{TOKEN_HEX}.tmp"),
    ]
    for path in orphaned + resembling:
        path.write_bytes(SECRET.encode())
    events = _boot(capsys)
    left = [str(p.relative_to(state)) for p in orphaned if p.exists()]
    assert left == [], f"startup left orphaned scratch holding customer content: {left}"
    assert all(p.exists() for p in resembling), "startup removed a file that is not scratch"
    assert agent.store.load(session.internal) == [{"role": "user", "content": SECRET}]
    reconciliation = [e for e in events if e.get("event") == "state_reconciliation"]
    assert reconciliation == [{
        "event": "state_reconciliation", "severity": "info", "status": "ok",
        "sessions": 1, "sessions_missing_storage": 0, "sessions_missing_audit": 0,
        "scratch_files_removed": len(orphaned),
    }]


TURN_KILLED_AT_ITS_SAVE = r"""
import hashlib, json, os, signal, sys
sys.path.insert(0, sys.argv[1])
sys.dont_write_bytecode = True
from pathlib import Path
from agent import AuditLog, SessionStore
from product_service import (AuthRegistry, DurableQuotaStore, ProductDataManager,
                             ProductScheduleStore, ProductService, SessionOperationLocks)
root, token, key = Path(sys.argv[2]), sys.argv[3], sys.argv[4].encode()


class Agent:
    manifest = {}
    memory = None
    _mcp = object()
    store = SessionStore(root / "sessions")
    sandbox_root = root / "sandboxes"
    audit = AuditLog(root / "audit", key=key)

    def turn_with_usage(self, session, message, *, allowed_tools=None,
                        deadline_monotonic=None):
        self.audit.record(session, "llm", "source", message, "output", None, None, 1)
        self.store.save(session, [{"role": "user", "content": message}])
        return "noted", {"input_tokens": 1, "output_tokens": 1}

    def turn_telemetry(self):
        return {}


agent = Agent()
auth = AuthRegistry([{"sha256": hashlib.sha256(token.encode()).hexdigest(),
                      "principal": "alice", "tenant": "tenant-a",
                      "scopes": ["chat"], "tools": []}])
quota = DurableQuotaStore(
    root / "product-quotas.sqlite3", max_concurrent_turns=10,
    tokens_per_day=10_000_000, token_reservation_per_turn=1000,
    storage_bytes_per_tenant=1 << 30, storage_reservation_per_turn=1 << 20,
    audit_bytes_per_tenant=1 << 28, audit_reservation_per_turn=1 << 16)
locks = SessionOperationLocks(root / "locks")
schedules = ProductScheduleStore(root / "product-schedules.json")
service = ProductService(
    agent, auth, quota_store=quota, session_locks=locks, schedule_store=schedules,
    data_manager=ProductDataManager(agent, schedules, locks, quota_store=quota),
    log_sink=lambda record: None)
real = os.replace


def replace(src, dst, *args, **kwargs):
    if Path(os.fsdecode(dst)).parent == agent.store.dir:
        os.kill(os.getpid(), signal.SIGKILL)
    return real(src, dst, *args, **kwargs)


os.replace = replace
service.dispatch("POST", "/v1/chat", {"Authorization": "Bearer " + token},
                 json.dumps({"session": "s", "message": "my secret plan"}).encode())
"""


def test_a_turn_killed_at_its_save_leaves_scratch_that_the_next_startup_removes(tmp_path):
    """The whole of F-30 with a real kill: the product process dies between
    writing the conversation and renaming it. The scratch file it leaves is
    whatever name the save really used; the next startup removes it, and
    reports nothing lost, because the turn never settled any bytes."""
    child = subprocess.run(
        [sys.executable, "-c", TURN_KILLED_AT_ITS_SAVE, str(PI_ROOT), str(tmp_path),
         TOKEN, AUDIT_KEY.decode()],
        capture_output=True, text=True, timeout=60)
    assert child.returncode == -signal.SIGKILL, (child.returncode, child.stderr)
    agent = _agent(tmp_path)
    session = Session(agent, "s")
    scratch = [str(p.relative_to(tmp_path)) for p in agent.store.scratch_files()]
    assert len(scratch) == 1 and _holding(tmp_path, SECRET) == scratch, (
        f"anti-vacuity: the killed turn should leave one scratch file holding the "
        f"message; found {scratch}, holding it: {_holding(tmp_path, SECRET)}")
    assert not session.conversation.exists() and session.chain.is_file()
    quota = _quota(tmp_path)
    manager = _manager(tmp_path, agent, quota)
    events = []
    manager.reconcile_at_startup(log_sink=events.append)
    assert events == [{
        "event": "state_reconciliation", "severity": "info", "status": "ok",
        "sessions": 1, "sessions_missing_storage": 0, "sessions_missing_audit": 0,
        "scratch_files_removed": 1,
    }]
    assert _holding(tmp_path, SECRET) == []
    assert _charged(quota) == (0, session.chain.stat().st_size)


def test_startup_syncs_the_scratch_it_removes(tmp_path, monkeypatch):
    """Removal is a privacy operation: a power loss after startup must not
    bring the content back."""
    agent = _agent(tmp_path)
    manager = _manager(tmp_path, agent, _quota(tmp_path))
    session = Session(agent, "s")
    _plant(agent, session, ("new_scratch",), SECRET)
    schedule_scratch = manager.schedule_store.path.with_name(
        f".{manager.schedule_store.path.name}.{TOKEN_HEX}.tmp")
    schedule_scratch.write_bytes(SECRET.encode())
    calls = Syscalls(monkeypatch)
    _record_removals(monkeypatch, calls)
    manager.reconcile_at_startup(log_sink=lambda record: None)
    events = calls.events
    for directory in (agent.store.dir, tmp_path):
        removals = [i for i, e in enumerate(events)
                    if e[0] == "removed" and e[1].parent == directory]
        assert removals, f"anti-vacuity: nothing removed from {directory}"
        assert any(e[:3] == ("fsync", "dir", identity(directory))
                   for e in events[removals[-1] + 1:]), (
            f"startup removed scratch from {directory.name} without syncing it")


def test_the_schedule_store_clears_scratch_only_under_its_lock(tmp_path):
    """The store's lock serializes its writers across product workers. A
    second worker's sweep must not remove a scratch file whose save is in
    flight under that lock."""
    path = tmp_path / "product-schedules.json"
    first, second = ProductScheduleStore(path), ProductScheduleStore(path)
    in_flight = path.with_name(f".{path.name}.{TOKEN_HEX}.tmp")
    removed = {}
    sweeper = threading.Thread(
        target=lambda: removed.setdefault("count", second.remove_scratch()))
    with first._locked():
        in_flight.write_bytes(b"{}")
        sweeper.start()
        sweeper.join(0.3)
        assert sweeper.is_alive() and in_flight.exists(), (
            "a sweep removed scratch while another worker held the store's lock")
    sweeper.join(10)
    assert removed == {"count": 1} and not in_flight.exists()


def test_the_schedule_store_removes_its_own_scratch_and_nothing_else(
        tmp_path, monkeypatch):
    """Its scratch is write_durably's `.<name>.<16 hex>.tmp` and, from
    releases before 2026-11-10, `.<name>.<pid>.tmp`. Near-misses, another
    file's scratch and the store's own files stay, and a sweep that finds
    nothing costs no sync."""
    path = tmp_path / "product-schedules.json"
    store = ProductScheduleStore(path)
    store.put(_principal(), name="daily", session="work", message="committed",
              every_ms=60_000)
    kept = [path.name, f"{path.name}.lock",
            f".{path.name}.tmp", f"{path.name}.{TOKEN_HEX}.tmp",
            f".{path.name}.{TOKEN_HEX[:15]}.tmp", f".{path.name}.FEDCBA9876543210.tmp",
            f".{path.name}.12a4.tmp", f".{path.name}.4242.tmp.bak",
            f".other.json.{TOKEN_HEX}.tmp", ".other.json.4242.tmp"]
    scratch = [f".{path.name}.{TOKEN_HEX}.tmp", f".{path.name}.4242.tmp",
               f".{path.name}.1.tmp"]
    for name in kept[2:] + scratch:
        (tmp_path / name).write_bytes(SECRET.encode())
    assert store.remove_scratch() == len(scratch)
    assert sorted(os.listdir(tmp_path)) == sorted(kept)
    assert [e["message"] for _, e in store.entries()] == ["committed"]
    calls = Syscalls(monkeypatch)
    assert store.remove_scratch() == 0
    assert calls.events == []


def test_startup_leaves_the_scratch_of_a_session_whose_lock_another_worker_holds(tmp_path):
    """One worker is the supported topology, and the locks are kept safe for
    more. A save in flight belongs to a worker that holds its session's lock,
    so the scratch of a locked session is not orphaned: removing it would
    fail that worker's rename, and its turn. It is left for the next start."""
    agent = _agent(tmp_path)
    manager = _manager(tmp_path, agent, _quota(tmp_path))
    busy, idle = Session(agent, "busy"), Session(agent, "idle")
    _plant(agent, busy, ("new_scratch", "legacy_scratch"), SECRET)
    _plant(agent, idle, ("new_scratch",), SECRET)
    with SessionOperationLocks(tmp_path / "locks").hold(busy.internal):
        assert manager.remove_orphaned_scratch() == 1
        assert busy.new_scratch.exists() and busy.legacy_scratch.exists(), (
            "a sweep removed the scratch of a session whose lock was held")
        assert not idle.new_scratch.exists()
    assert manager.remove_orphaned_scratch() == 2
    assert not busy.new_scratch.exists() and not busy.legacy_scratch.exists()
    assert manager.session_locks.size() == 0


def test_a_lock_taken_by_digest_excludes_the_sessions_own_operations(tmp_path):
    """Startup knows a scratch file's session only by the digest in its name,
    which is also the name of that session's lock file. Taking the lock by
    digest must exclude, and be excluded by, a turn's `hold`, in this worker
    and in another."""
    locks = SessionOperationLocks(tmp_path / "locks")
    internal = _internal_session(TENANT, "s")
    digest = hashlib.sha256(internal.encode()).hexdigest()
    with locks.hold_if_free(digest) as free:
        assert free is True
        for holder in (locks, SessionOperationLocks(tmp_path / "locks")):
            with pytest.raises(TimeoutError):
                with holder.hold(internal, deadline_monotonic=time.monotonic() + 0.05):
                    pass
    for holder in (locks, SessionOperationLocks(tmp_path / "locks")):
        with holder.hold(internal, deadline_monotonic=time.monotonic() + 5):
            with locks.hold_if_free(digest) as free:
                assert free is False
    with locks.hold_if_free(digest) as free:
        assert free is True, "the lock was not released"
    with SessionOperationLocks().hold_if_free(digest) as free:
        assert free is True, "without a lock directory there is nothing to take"
    assert locks.size() == 0


def test_a_scratch_file_that_is_gone_when_its_lock_is_taken_is_not_counted(
        tmp_path, monkeypatch):
    """Another worker's save can finish between the listing and the lock: its
    scratch was renamed into place, so nothing was removed."""
    agent = _agent(tmp_path)
    manager = _manager(tmp_path, agent, _quota(tmp_path))
    session = Session(agent, "s")
    monkeypatch.setattr(agent.store, "scratch_files", lambda: [session.new_scratch])
    calls = Syscalls(monkeypatch)
    assert manager.remove_orphaned_scratch() == 0
    assert calls.events == [], "nothing was removed, so nothing needs a sync"


CONVERSATION_STATES = ("absent", "empty", "present")
SANDBOX_STATES = ("absent", "empty-directory", "file")
CHAIN_STATES = ("absent", "empty", "present")


@settings(max_examples=60, deadline=None)
@given(rows=st.lists(st.tuples(
    st.booleans(), st.booleans(), st.sampled_from(CONVERSATION_STATES),
    st.sampled_from(SANDBOX_STATES), st.sampled_from(CHAIN_STATES)),
    min_size=1, max_size=4))
@example(rows=[(True, True, "absent", "absent", "absent")])
@example(rows=[(True, True, "present", "file", "present"),
               (True, False, "empty", "empty-directory", "absent"),
               (False, True, "absent", "absent", "empty"),
               (True, True, "absent", "file", "present")])
def test_the_startup_report_counts_exactly_the_rows_whose_recorded_bytes_are_gone(
        tmp_path_factory, rows):
    """THE GUARD for the report. Each row records storage and audit bytes or
    not; each surface's files are absent, empty or present. A surface is lost
    when its row recorded bytes for it and no byte of it is left: a missing or
    zero-length conversation with no sandbox file, a missing or zero-length
    chain. The event counts exactly those, is critical exactly when there is
    one, carries nothing but counts, and the registry then agrees with the
    files."""
    root = tmp_path_factory.mktemp("startup")
    agent = _agent(root)
    quota = _quota(root)
    manager = _manager(root, agent, quota)
    lost_storage = lost_audit = 0
    for n, (records_storage, records_audit, conversation, sandbox, chain) in enumerate(rows):
        session = Session(agent, f"session-{n}")
        if conversation == "present":
            agent.store.save(session.internal, [{"role": "user", "content": SECRET}])
        elif conversation == "empty":
            session.conversation.write_bytes(b"")
        if sandbox != "absent":
            session.sandbox.mkdir(parents=True)
            if sandbox == "file":
                (session.sandbox / "note.txt").write_text(SECRET)
        if chain == "present":
            agent.audit.record(session.internal, "test", "source", SECRET, "output",
                               None, None, 10)
        elif chain == "empty":
            session.chain.write_bytes(b"")
        _register(quota, session, storage_bytes=1000 if records_storage else 0,
                  audit_bytes=1000 if records_audit else 0)
        lost_storage += (records_storage and conversation != "present"
                         and sandbox != "file")
        lost_audit += records_audit and chain != "present"
    events = []
    assert manager.reconcile_at_startup(log_sink=events.append) is not bool(
        lost_storage or lost_audit)
    lost = bool(lost_storage or lost_audit)
    assert events == [{
        "event": "state_reconciliation",
        "severity": "critical" if lost else "info",
        "status": "failed" if lost else "ok",
        "sessions": len(rows),
        "sessions_missing_storage": lost_storage,
        "sessions_missing_audit": lost_audit,
        "scratch_files_removed": 0,
    }]
    measured = [manager.footprint(Session(agent, f"session-{n}").internal)
                for n in range(len(rows))]
    assert _charged(quota) == (
        sum(m["storage_bytes"] for m in measured), sum(m["audit_bytes"] for m in measured)), (
        "after the report the registry must agree with the files")


def test_startup_still_refuses_files_the_registry_cannot_attribute(tmp_path):
    """And refuses before it removes anything: state it will not open is left
    exactly as it was found."""
    agent = _agent(tmp_path)
    manager = _manager(tmp_path, agent, _quota(tmp_path))
    unregistered = Session(agent, "unregistered")
    _plant(agent, unregistered, ("conversation", "new_scratch"), SECRET)
    with pytest.raises(product_service.ConfigError, match="supported state migration"):
        manager.reconcile_at_startup(log_sink=lambda record: None)
    assert unregistered.new_scratch.exists()


def test_startup_still_refuses_a_registry_row_without_its_session_identity(tmp_path):
    """A row written before the registry recorded internal session ids names
    no files, so nothing on disk can be measured against it."""
    agent = _agent(tmp_path)
    quota = _quota(tmp_path)
    manager = _manager(tmp_path, agent, quota)
    with closing(sqlite3.connect(quota.path)) as db:
        db.execute(
            "INSERT INTO session_usage(tenant, session, internal_session, "
            "storage_bytes, audit_bytes) VALUES(?,?,NULL,1,1)", ("a" * 64, "b" * 64))
        db.commit()
    for refused in (lambda: manager.reconcile_at_startup(log_sink=lambda record: None),
                    quota.registered_sessions,
                    lambda: quota.reconcile_registered(manager.footprint)):
        with pytest.raises(product_service.ConfigError, match="unmigrated session"):
            refused()


def test_startup_reconciliation_needs_the_registry(tmp_path):
    manager = ProductDataManager(
        _agent(tmp_path), ProductScheduleStore(tmp_path / "product-schedules.json"),
        SessionOperationLocks())
    with pytest.raises(product_service.ConfigError, match="quota registry"):
        manager.reconcile_at_startup(log_sink=lambda record: None)


def test_the_startup_report_is_printed_as_one_line_of_json(tmp_path, capsys):
    """With no sink given, the event goes where the monitors' events go."""
    agent = _agent(tmp_path)
    assert _manager(tmp_path, agent, _quota(tmp_path)).reconcile_at_startup() is True
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [{
        "event": "state_reconciliation", "severity": "info", "status": "ok",
        "sessions": 0, "sessions_missing_storage": 0, "sessions_missing_audit": 0,
        "scratch_files_removed": 0,
    }]


# ── guards for the class ─────────────────────────────────────────────────

PRODUCT_MODULES = ("product_service.py", "product_main.py")

# Where a product module may write a file other than through
# agent.write_durably, and why. A new entry needs a reason as good as these.
OTHER_WRITES = {
    ("product_service.py", "SessionOperationLocks.hold"):
        "opens the session's lock file, which holds no bytes",
    ("product_service.py", "SessionOperationLocks.hold_if_free"):
        "opens the session's lock file, which holds no bytes",
    ("product_service.py", "ProductScheduleStore._locked"):
        "opens the schedule store's lock file, which holds no bytes",
    ("product_service.py", "serve_product.Handler._write"):
        "writes a response to the client's socket",
}


def file_writes(source):
    """(scope, line) of every call that writes bytes to a file or opens one
    for writing: write/write_bytes/write_text/writelines/touch, os.write,
    os.open, os.fdopen, tempfile's creators, and open() or Path.open() with
    a mode that writes."""
    found = []
    for scope, node in _scopes(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        function = ast.unparse(node.func)
        attribute = node.func.attr if isinstance(node.func, ast.Attribute) else function
        if (attribute in ("write", "write_bytes", "write_text", "writelines", "touch")
                or function in ("os.write", "os.open", "os.fdopen", "tempfile.mkstemp",
                                "tempfile.NamedTemporaryFile", "tempfile.mkdtemp")):
            found.append((scope, node.lineno))
        elif attribute == "open" or function == "open":
            modes = [a.value for a in node.args
                     if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            modes += [k.value.value for k in node.keywords
                      if k.arg == "mode" and isinstance(k.value, ast.Constant)]
            if any(set(mode) & set("wax+") for mode in modes):
                found.append((scope, node.lineno))
    return found


def test_the_write_detector_fires_on_every_shape_it_forbids():
    shapes = {
        "def a():\n    p.write_bytes(b)\n": [("a", 2)],
        "def b():\n    p.write_text(t)\n": [("b", 2)],
        "def c():\n    open(p, 'w')\n": [("c", 2)],
        "def d():\n    p.open(mode='ab')\n": [("d", 2)],
        "def e():\n    os.open(p, flags)\n": [("e", 2)],
        "class F:\n    def g(self):\n        self.stream.write(b)\n": [("F.g", 3)],
        "def h():\n    tempfile.mkstemp()\n": [("h", 2)],
        "def i():\n    open(p); p.open('rb'); p.read_bytes(); print(x)\n": [],
    }
    for source, expected in shapes.items():
        assert file_writes(source) == expected, source


def test_the_product_writes_files_only_through_write_durably():
    """THE SOURCE GUARD for F-13 in the product modules: a file written any
    other way is unsynced, and its scratch survives a failure. A new state
    file belongs in agent.write_durably; the sites listed above are not
    state. (tests/test_durable_state.py forbids a rename into place outside
    write_durably in the same modules.)"""
    found = {}
    for module in PRODUCT_MODULES:
        for scope, line in file_writes((PI_ROOT / module).read_text()):
            found.setdefault((module, scope), []).append(line)
    extra = {site: lines for site, lines in found.items() if site not in OTHER_WRITES}
    assert extra == {}, (
        f"a product module writes a file outside agent.write_durably: {extra}")
    stale = sorted(site for site in OTHER_WRITES if site not in found)
    assert stale == [], f"listed as writing a file, and no longer does: {stale}"
    for module, scope in (("product_service.py", "ProductScheduleStore"),
                          ("product_main.py", "run")):
        tree = ast.parse((PI_ROOT / module).read_text())
        called = {ast.unparse(node.func) for inner, node in _scopes(tree)
                  if isinstance(node, ast.Call)
                  and (inner == scope or inner.startswith(scope + "."))}
        assert "write_durably" in called, (
            f"{module}:{scope} no longer commits through write_durably")
