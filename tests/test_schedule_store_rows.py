"""The schedule store, schema 2 (audit 2026-11-10, F-27; decision D5 (a)).

Schema 1 kept every tenant's schedules in one JSON document,
product-schedules.json. Every operation read it whole, and every claim,
completion and release rewrote it whole, under one exclusive lock that the
scheduler took once a second: one tenant's claim cost time in proportion to
every other tenant's schedules (r8: 530 ms with twenty tenants holding 8 MiB
each). C4 bounded what one tenant may hold; this file pins the other half.

Schema 2 keeps each schedule as rows of the quota ledger,
product-quotas.sqlite3: a claim, a completion and a release change their own
schedule's row and nothing else, and the scheduler's look reads no message.
product-schedules.json holds only a marker that a release before schema 2
cannot read as a store, so such a release refuses the state instead of
serving it with no schedules. A schema-1 document is converted only by
`state_tool.py migrate`, as a second versioned step after the sandbox step
(state layout 2), and never implicitly: the service, `backup` and
`backup-cycle` refuse it, naming the command. The guards P2 gave the
document's writes have their equivalents for the rows here: a commit that a
crash cannot tear, a full disk that changes nothing, every commit synced, and
a deleted or replaced message left in no file (F-13, F-30).
"""

import errno
import hashlib
import io
import json
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import tarfile
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from conftest import PI_ROOT
from test_product_main import _base_env, _install_happy_runtime

import product_main
import product_service
import state_tool
from agent import AuditLog, SessionStore
from product_service import (
    ConfigError,
    DurableQuotaStore,
    Principal,
    ProductError,
    ProductScheduleStore,
    _internal_session,
)
from state_tool import CLEAN_MARKER, StateToolError, create_backup, restore_backup

KEY = b"schedule-store-rows-audit-key-0123456789"
JOURNAL = ".state-migration.json"
MIGRATE = r"state_tool\.py migrate"
TOKEN = "QZXJ-SCHEDULE-TOKEN"
# A message that spills over several ledger pages, the token in each.
SPREAD = (TOKEN + "-" * 1005) * 64
# A Python-level read of the store's own file larger than this is a read of
# its schedules: the schema-2 marker is far smaller.
MARKER_READ = 1024


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _principal(tenant, principal="planner"):
    return Principal(principal, tenant, frozenset({"schedules:write", "chat"}), frozenset())


def _quota(root, clock=None):
    return DurableQuotaStore(
        root / "product-quotas.sqlite3", clock=clock, max_concurrent_turns=10,
        tokens_per_day=10_000_000, token_reservation_per_turn=1000,
        storage_bytes_per_tenant=1 << 30, storage_reservation_per_turn=1 << 20,
        audit_bytes_per_tenant=1 << 28, audit_reservation_per_turn=1 << 16)


def _store(root, **options):
    """The product's schedule store in `root`, beside its quota ledger, as
    product_main builds them."""
    root.mkdir(parents=True, exist_ok=True)
    _quota(root)
    return ProductScheduleStore(root / "product-schedules.json", **options)


def _fill(store, tenants, schedules, size):
    for tenant in range(tenants):
        principal = _principal(f"tenant-{tenant}")
        for index in range(schedules):
            store.put(principal, name=f"job{index}", session=f"s{index}",
                      message=f"{tenant}/{index}/" + "m" * size, every_ms=60_000)


def _checkpoint(root):
    """Copy the ledger's log into the ledger, as SQLite does by itself once
    the log is long: the rows are then in the ledger's own file."""
    with closing(sqlite3.connect(root / "product-quotas.sqlite3")) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()


def _holding(root, needle=TOKEN):
    return sorted(path.relative_to(root).as_posix() for path in Path(root).rglob("*")
                  if path.is_file() and needle.encode() in path.read_bytes())


def _rows(root):
    """Every schedule row and message in the ledger, by key."""
    with closing(sqlite3.connect(f"file:{root / 'product-quotas.sqlite3'}?mode=ro",
                                 uri=True)) as db:
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "schedules" not in tables:
            return {}
        columns = [row[1] for row in db.execute("PRAGMA table_info(schedules)")]
        rows = {row[0]: dict(zip(columns, row))
                for row in db.execute("SELECT * FROM schedules ORDER BY key")}
        for key, message in db.execute("SELECT key, message FROM schedule_messages"):
            rows.setdefault(key, {})["message"] = message
        return rows


def _usage(root):
    with closing(sqlite3.connect(f"file:{root / 'product-quotas.sqlite3'}?mode=ro",
                                 uri=True)) as db:
        return db.execute("SELECT * FROM session_usage ORDER BY session").fetchall()


def _tree(root):
    """Every entry under `root` but the ledger, which `_rows` and `_usage`
    compare by content: SQLite may rewrite pages without changing them."""
    return {path.relative_to(root).as_posix(): (path.read_bytes() if path.is_file() else None)
            for path in sorted(Path(root).rglob("*"))
            if not path.name.startswith("product-quotas.sqlite3")}


# ── the finding: a claim's cost grew with every other tenant's schedules ──


def _files(directory):
    found = {}
    for path in Path(directory).iterdir():
        if path.is_file():
            status = path.stat()
            found[path.name] = (status.st_ino, status.st_size)
    return found


@contextmanager
def _log_held(directory):
    """A reader holds the ledger at its current state, so that SQLite cannot
    restart its write-ahead log while an operation runs: what the operation
    writes is appended, and counted."""
    ledger = Path(directory) / "product-quotas.sqlite3"
    with closing(sqlite3.connect(f"file:{ledger}?mode=ro", uri=True,
                                 isolation_level=None)) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        yield
        reader.execute("COMMIT")


def _bytes_written(directory, operation):
    """What `operation` wrote to the files of `directory`: a file that is new,
    or that took a new inode (a replaced file), whole; a file that grew, by
    what it grew."""
    with _log_held(directory):
        before = _files(directory)
        operation()
        after = _files(directory)
    written = 0
    for name, (inode, size) in after.items():
        if name not in before or before[name][0] != inode:
            written += size
        else:
            written += max(0, size - before[name][1])
    return written


class _Counted:
    def __init__(self, stream, tally):
        self._stream, self._tally = stream, tally

    def read(self, *args):
        data = self._stream.read(*args)
        self._tally.append(len(data))
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._stream.close()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _watch(directory, monkeypatch):
    """Count what Python reads of the files in `directory`, and record every
    SQL statement any connection runs, as SQLite ran it (parameters bound)."""
    tally, statements = [], []
    real_open, real_connect = Path.open, sqlite3.connect
    root = Path(directory).resolve()

    def counted_open(self, *args, **kwargs):
        stream = real_open(self, *args, **kwargs)
        if Path(self).resolve().parent == root:
            return _Counted(stream, tally)
        return stream

    def traced_connect(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(Path, "open", counted_open)
    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    return tally, statements


def _traced(statements, connect=sqlite3.connect):
    def traced_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db
    return traced_connect


def _keyed(statements, key):
    """The statements that read or change schedules, each of which must name
    `key`; and those that name a message."""
    data = [s for s in statements
            if not s.lstrip().upper().startswith(("PRAGMA", "BEGIN", "COMMIT", "ROLLBACK",
                                                  "CREATE"))]
    stray = [s for s in data if f"key='{key}'" not in s]
    messages = [s for s in data if "schedule_messages" in s]
    return stray, messages


def _claimed(store, key):
    assert store.claim_due(key, 10**7, "worker", 300) is not None


OPERATIONS = {
    # name: (prepare, run, what it must have done)
    "claim_due": (lambda store, key: None,
                  lambda store, key: store.claim_due(key, 10**7, "worker", 300),
                  lambda entry: entry["lease_owner"] == "worker"),
    "complete_claim": (_claimed,
                       lambda store, key: store.complete_claim(key, "worker", 10**7, "ok"),
                       lambda entry: (entry["last_status"], "lease_owner" in entry)
                       == ("ok", False)),
    "release_claim": (_claimed,
                      lambda store, key: store.release_claim(key, "worker"),
                      lambda entry: "lease_owner" not in entry and entry["last_run"] is None),
    "mark_run": (lambda store, key: None,
                 lambda store, key: store.mark_run(key, 10**7, "failed"),
                 lambda entry: entry["last_status"] == "failed"),
}


@pytest.mark.parametrize("operation", OPERATIONS)
def test_a_claim_completion_or_release_writes_only_its_own_schedule(tmp_path, operation):
    """THE FINDING. Eight tenants hold four schedules of 48 KiB each. A claim,
    a completion, a release or a recorded run writes its own schedule's row,
    a few KiB, whatever the other tenants hold; schema 1 rewrote every byte of
    every tenant's schedules each time."""
    prepare, run, done = OPERATIONS[operation]
    store = _store(tmp_path)
    _fill(store, tenants=8, schedules=4, size=48 * 1024)
    key = ProductScheduleStore._key("tenant-3", "job2")
    prepare(store, key)
    _checkpoint(tmp_path)
    others = sum(len(entry["message"]) for tenant in range(8)
                 for entry in store.for_tenant(f"tenant-{tenant}")) - len(
                     store.for_tenant("tenant-3")[2]["message"])
    written = _bytes_written(tmp_path, lambda: run(store, key))
    assert written <= 64 * 1024, (
        f"one {operation} wrote {written} bytes to the store's directory while other "
        f"tenants' schedules held {others}: it rewrote them (audit F-27)")
    assert done(store.get(key)), f"the {operation} did not do its work"


@pytest.mark.parametrize("operation", OPERATIONS)
def test_a_claim_completion_or_release_reads_no_other_schedule(
        tmp_path, monkeypatch, operation):
    """THE FINDING, the read half: schema 1 parsed every tenant's schedules
    for each of these. Schema 2 reads its own schedule's row: every statement
    it runs names its key, and none reads a message."""
    prepare, run, done = OPERATIONS[operation]
    store = _store(tmp_path)
    _fill(store, tenants=8, schedules=4, size=48 * 1024)
    key = ProductScheduleStore._key("tenant-3", "job2")
    prepare(store, key)
    tally, statements = _watch(tmp_path, monkeypatch)
    run(store, key)
    monkeypatch.undo()
    assert sum(tally) <= MARKER_READ, (
        f"one {operation} read {sum(tally)} bytes of the schedule store: every "
        f"tenant's schedules (audit F-27)")
    stray, messages = _keyed(statements, key)
    assert stray == [] and messages == [], (
        f"one {operation} ran statements beyond its own schedule's row: {stray + messages}")
    assert done(store.get(key)), f"the {operation} did not do its work"


def test_the_scheduler_s_look_reads_no_message(tmp_path, monkeypatch):
    """The scheduler looks at every schedule once a second. Schema 1 read
    every message to do it; the look needs none."""
    store = _store(tmp_path)
    _fill(store, tenants=4, schedules=4, size=48 * 1024)
    tally, statements = _watch(tmp_path, monkeypatch)
    listing = store.entries()
    monkeypatch.undo()
    assert sum(tally) <= MARKER_READ, (
        f"the scheduler's look read {sum(tally)} bytes of the store: every message of "
        f"every tenant, once a second (audit F-27)")
    assert len(listing) == 16 and all("message" not in entry for _, entry in listing)
    assert not [s for s in statements if "schedule_messages" in s]


def test_a_turn_reads_its_own_message_and_no_other(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _fill(store, tenants=3, schedules=2, size=1024)
    key = ProductScheduleStore._key("tenant-1", "job1")
    tally, statements = _watch(tmp_path, monkeypatch)
    entry = store.get(key, with_message=True)
    monkeypatch.undo()
    assert entry["message"] == "1/1/" + "m" * 1024
    assert sum(tally) <= MARKER_READ
    stray, _ = _keyed(statements, key)
    assert stray == []
    del statements[:]
    monkeypatch.setattr(sqlite3, "connect", _traced(statements))
    plain = store.get(key)
    monkeypatch.undo()
    stray, messages = _keyed(statements, key)
    assert "message" not in plain and stray == [] and messages == [], (
        f"get() without with_message read a message: {messages}")


# ── the store's semantics are schema 1's ───────────────────────────────────


class _Model:
    """What each operation did to the schema-1 document, on a dict: the rows
    must answer every question the same way."""

    def __init__(self, max_per_tenant, max_bytes):
        self.data, self.max_per_tenant, self.max_bytes = {}, max_per_tenant, max_bytes

    def put(self, tenant, name, session, message, every_ms, now):
        key = ProductScheduleStore._key(tenant, name)
        tenant_hash = _digest(tenant)
        mine = {k: e for k, e in self.data.items() if e["tenant_sha256"] == tenant_hash}
        if key not in self.data and len(mine) >= self.max_per_tenant:
            return "tenant schedule quota reached"
        added = ProductScheduleStore._stored_bytes(message)
        held = sum(ProductScheduleStore._stored_bytes(e["message"])
                   for k, e in mine.items() if k != key)
        prior = self.data.get(key, {})
        if held + added > self.max_bytes and added > ProductScheduleStore._stored_bytes(
                prior.get("message", "")):
            return "tenant schedule byte quota reached"
        self.data[key] = {
            "tenant_sha256": tenant_hash, "principal": "planner", "name": name,
            "session": session, "internal_session": _internal_session(tenant, session),
            "message": message, "every_ms": every_ms, "last_run": prior.get("last_run"),
            "last_status": prior.get("last_status"),
            "created_unix": prior.get("created_unix", now), "updated_unix": now}
        return None

    def remove(self, tenant, name):
        return self.data.pop(ProductScheduleStore._key(tenant, name), None) is not None

    def remove_session(self, tenant, session):
        doomed = [k for k, e in self.data.items()
                  if e["tenant_sha256"] == _digest(tenant) and e["session"] == session]
        for key in doomed:
            del self.data[key]
        return len(doomed)

    def claim(self, key, now, owner, lease_s):
        entry = self.data.get(key)
        if entry is None or not (entry["last_run"] is None
                                 or (now - entry["last_run"]) * 1000.0 >= entry["every_ms"]):
            return None
        if entry.get("lease_until", 0) > now:
            return None
        entry.update(lease_owner=owner, lease_until=now + lease_s)
        return {k: v for k, v in entry.items() if k != "message"}

    def complete(self, key, owner, when, status):
        entry = self.data.get(key)
        if entry is None or entry.get("lease_owner") != owner:
            return False
        entry.update(last_run=when, last_status=status, updated_unix=when)
        entry.pop("lease_owner")
        entry.pop("lease_until")
        return True

    def release(self, key, owner):
        entry = self.data.get(key)
        if entry is None or entry.get("lease_owner") != owner:
            return False
        entry.pop("lease_owner")
        entry.pop("lease_until")
        return True

    def mark_run(self, key, when, status):
        if key in self.data:
            self.data[key].update(last_run=when, last_status=status, updated_unix=when)


TENANTS = ("tenant-a", "tenant-b", "tenant-c")
NAMES = ("daily", "weekly")
_operation = st.one_of(
    st.tuples(st.just("put"), st.sampled_from(TENANTS), st.sampled_from(NAMES),
              st.sampled_from(("work", "home")), st.text(min_size=1, max_size=12),
              st.sampled_from((1, 1000, 60_000))),
    st.tuples(st.just("remove"), st.sampled_from(TENANTS), st.sampled_from(NAMES)),
    st.tuples(st.just("remove_session"), st.sampled_from(TENANTS),
              st.sampled_from(("work", "home"))),
    st.tuples(st.just("claim"), st.sampled_from(TENANTS), st.sampled_from(NAMES),
              st.sampled_from(("w1", "w2"))),
    st.tuples(st.just("complete"), st.sampled_from(TENANTS), st.sampled_from(NAMES),
              st.sampled_from(("w1", "w2"))),
    st.tuples(st.just("release"), st.sampled_from(TENANTS), st.sampled_from(NAMES),
              st.sampled_from(("w1", "w2"))),
    st.tuples(st.just("mark_run"), st.sampled_from(TENANTS), st.sampled_from(NAMES)),
    st.tuples(st.just("tick"), st.integers(min_value=0, max_value=400)),
)


@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(operations=st.lists(_operation, max_size=24))
def test_every_operation_answers_as_the_schema_one_store_did(tmp_path_factory, operations):
    """A property, the class: for any sequence of puts, removals, claims,
    completions, releases and recorded runs across three tenants, with
    quotas small enough to be met, every answer and every listing the rows
    give is the one schema 1's document gave."""
    root = tmp_path_factory.mktemp("model")
    now = [1000.0]
    store = _store(root, clock=lambda: now[0], max_per_tenant=2, max_bytes_per_tenant=20)
    model = _Model(max_per_tenant=2, max_bytes=20)
    for operation in operations:
        kind = operation[0]
        if kind == "tick":
            now[0] += operation[1]
            continue
        if kind == "put":
            _, tenant, name, session, message, every_ms = operation
            expected = model.put(tenant, name, session, message, every_ms, now[0])
            try:
                store.put(_principal(tenant), name=name, session=session, message=message,
                          every_ms=every_ms)
                got = None
            except ProductError as error:
                assert error.status == 409, error.message
                got = error.message
            assert got == expected, (operation, got, expected)
            continue
        if kind == "remove_session":
            assert store.remove_session(operation[1], operation[2]) == model.remove_session(
                operation[1], operation[2])
            continue
        key = ProductScheduleStore._key(operation[1], operation[2])
        if kind == "remove":
            assert store.remove(operation[1], operation[2]) is model.remove(
                operation[1], operation[2])
        elif kind == "claim":
            assert store.claim_due(key, now[0], operation[3], 30) == model.claim(
                key, now[0], operation[3], 30)
        elif kind == "complete":
            assert store.complete_claim(key, operation[3], now[0], "ok") is model.complete(
                key, operation[3], now[0], "ok")
        elif kind == "release":
            assert store.release_claim(key, operation[3]) is model.release(key, operation[3])
        else:
            store.mark_run(key, now[0], "failed")
            model.mark_run(key, now[0], "failed")
    assert [key for key, _ in store.entries()] == sorted(model.data)
    for key, entry in store.entries():
        assert entry == {k: v for k, v in model.data[key].items() if k != "message"}
        assert store.get(key, with_message=True) == model.data[key]
    for tenant in TENANTS:
        assert store.for_tenant(tenant) == [
            {k: e[k] for k in ("name", "session", "message", "every_ms", "last_run",
                               "last_status")}
            for e in sorted((e for e in model.data.values()
                             if e["tenant_sha256"] == _digest(tenant)),
                            key=lambda e: e["name"])]


# ── the format: a marker a release before schema 2 refuses ─────────────────


def test_a_new_store_is_marked_as_schema_two_before_it_holds_anything(tmp_path):
    store = _store(tmp_path)
    marker = (tmp_path / "product-schedules.json").read_bytes()
    assert marker == product_service.SCHEDULE_STORE_MARKER
    assert not isinstance(json.loads(marker), dict), (
        "a release before schema 2 would read the marker as an empty store")
    assert store.database == tmp_path / "product-quotas.sqlite3"
    store.put(_principal("tenant-a"), name="daily", session="work", message="m",
              every_ms=60_000)
    assert (tmp_path / "product-schedules.json").read_bytes() == marker
    assert [row["message"] for row in _rows(tmp_path).values()] == ["m"]


def test_a_schedule_replaced_while_it_is_claimed_is_unclaimed_as_it_always_was(tmp_path):
    """Schema 1's put wrote a new entry without the lease, so a schedule its
    owner replaced while its run was in flight was unclaimed, and that run's
    completion found no lease of its own and recorded nothing. The rows keep
    that."""
    store = _store(tmp_path)
    principal = _principal("tenant-a")
    store.put(principal, name="daily", session="work", message="first", every_ms=60_000)
    key = ProductScheduleStore._key("tenant-a", "daily")
    assert store.claim_due(key, 10**7, "worker", 300)["lease_owner"] == "worker"
    store.put(principal, name="daily", session="work", message="second", every_ms=60_000)
    entry = store.get(key, with_message=True)
    assert ("lease_owner" in entry, "lease_until" in entry, entry["message"]) == (
        False, False, "second")
    assert store.complete_claim(key, "worker", 10**7, "ok") is False
    assert store.get(key)["last_run"] is None


def test_a_store_whose_marker_is_gone_marks_itself_again_before_its_next_row(tmp_path):
    """Rows the marker does not announce are schedules a release before
    schema 2 would not see. A store that finds its marker gone writes it
    again before it writes a row."""
    store = _store(tmp_path)
    (tmp_path / "product-schedules.json").unlink()
    assert store.entries() == [], "a store with no marker and no row is an empty one"
    store.put(_principal("tenant-a"), name="daily", session="work", message="m",
              every_ms=60_000)
    assert (tmp_path / "product-schedules.json").read_bytes() == (
        product_service.SCHEDULE_STORE_MARKER)
    assert [row["name"] for row in _rows(tmp_path).values()] == ["daily"]


def _schema_one_entry(tenant, name, session, message, **fields):
    tenant_hash = _digest(tenant)
    entry = {"tenant_sha256": tenant_hash, "principal": "planner", "name": name,
             "session": session, "internal_session": _internal_session(tenant, session),
             "message": message, "every_ms": 3_600_000, "last_run": None,
             "last_status": None, "created_unix": 100.0, "updated_unix": 100.0}
    entry.update(fields)
    return f"{tenant_hash}:{name}", entry


def _schema_one_document(entries):
    return json.dumps(dict(entries), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode()


SCHEMA_ONE = [
    _schema_one_entry("tenant-a", "daily", "project", "summarise the day", last_run=900.0,
                      last_status="ok", updated_unix=900.0),
    _schema_one_entry("tenant-a", "weekly", "notes", "plan the week é\n\"quoted\""),
    _schema_one_entry("tenant-b", "daily", "project", SPREAD,
                      lease_owner="123-worker", lease_until=1300.0),
]


def _drain(root):
    (root / CLEAN_MARKER).write_text(json.dumps({
        "schema_version": 1, "product_version": "0.4.0", "clean_shutdown_unix": 100}))


def _schema_one_state(root, *, key=KEY, legacy_sandboxes=False, entries=SCHEMA_ONE):
    """A drained state as a release before schema 2 left it: three sessions
    with their files and registry rows, and their schedules in a schema-1
    document. With `legacy_sandboxes` the sandboxes are named as releases
    before state layout 2 named them (audit F-23)."""
    sessions = SessionStore(root / "sessions")
    audit = AuditLog(root / "audit", key=key)
    quota = _quota(root)
    for tenant, external in (("tenant-a", "project"), ("tenant-a", "notes"),
                             ("tenant-b", "project")):
        internal = _internal_session(tenant, external)
        digest = _digest(internal)
        sessions.save(internal, [{"role": "user", "content": f"{tenant}/{external}"}])
        audit.record(internal, "write_file", "source", "input", "output", None, None, 1)
        sandbox = root / "sandboxes" / (digest[:16] if legacy_sandboxes else digest)
        sandbox.mkdir(parents=True)
        (sandbox / "note.txt").write_text(f"{tenant}/{external}")
        if not legacy_sandboxes:
            (root / "locks").mkdir(exist_ok=True)
            (root / "locks" / f"{digest}.lock").touch()
        lease = quota.acquire_turn(tenant, session_id=internal)
        lease.release(session_id=internal, storage_bytes=1, audit_bytes=1)
    (root / "product-schedules.json").write_bytes(_schema_one_document(entries))
    (root / "product-schedules.json.lock").touch()     # what the old store locked
    _drain(root)
    return root


def test_startup_refuses_a_schema_one_store_before_anything_changes(tmp_path, monkeypatch):
    """The real startup. It refuses before it removes the clean-shutdown
    marker, which the migration needs, and names the command."""
    _base_env(tmp_path, monkeypatch)
    _install_happy_runtime(monkeypatch)
    state = _schema_one_state(tmp_path / "state", key=b"a" * 32)
    before, rows = _tree(state), _usage(state)
    with pytest.raises(ConfigError, match=rf"schema 1.*{MIGRATE}"):
        product_main.run()
    assert _tree(state) == before and _usage(state) == rows, (
        "startup changed a state it refused to open")


def test_startup_refuses_a_schedule_store_that_is_not_one(tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    _install_happy_runtime(monkeypatch)
    state = tmp_path / "state"
    (state / "product-schedules.json").mkdir(parents=True)
    _drain(state)
    with pytest.raises(ConfigError, match="product schedule store is corrupt"):
        product_main.run()
    assert (state / CLEAN_MARKER).exists(), "startup took the marker of a state it refused"


@pytest.mark.parametrize("content", [b"{}", b"", _schema_one_document(SCHEMA_ONE)],
                         ids=["empty-object", "empty-file", "entries"])
def test_a_schema_one_document_is_refused_never_read_or_converted(tmp_path, content):
    path = tmp_path / "product-schedules.json"
    _quota(tmp_path)
    path.write_bytes(content)
    store = ProductScheduleStore(path)
    principal = _principal("tenant-a")
    for call in (store.entries, lambda: store.for_tenant("tenant-a"),
                 lambda: store.get("any"), lambda: store.remove("tenant-a", "daily"),
                 lambda: store.remove_inactive(10**10, 10**10),
                 lambda: store.put(principal, name="daily", session="work", message="m",
                                   every_ms=60_000)):
        with pytest.raises(RuntimeError, match=rf"schema 1.*{MIGRATE}"):
            call()
    assert store.is_healthy() is False
    assert path.read_bytes() == content and _rows(tmp_path) == {}, (
        "the store changed a schema-1 document it must leave to the migration")


@pytest.mark.parametrize("online", [False, True], ids=["offline", "online"])
def test_a_backup_refuses_a_schema_one_store_naming_the_command(tmp_path, online):
    state = _schema_one_state(tmp_path / "state")
    before = _tree(state)
    with pytest.raises(StateToolError, match=rf"schema 1.*{MIGRATE}"):
        create_backup(state, tmp_path / "out.tar.gz", audit_key=KEY, online=online)
    assert _tree(state) == before and sorted(os.listdir(tmp_path)) == ["state"]


def _old_release(tmp_path, release="v0.4.0"):
    """The modules of a published release, from its git tag, in a directory
    of their own. Skips where the tags are absent (CI's shallow checkout)."""
    try:
        listed = subprocess.run(["git", "-C", str(PI_ROOT), "ls-tree", "--name-only", release],
                                capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        pytest.skip(f"git is not usable here: {error}")
    if listed.returncode:
        pytest.skip(f"the {release} release tag is not available here (a shallow clone, or "
                    "a tree without its git history)")
    modules = [name for name in listed.stdout.decode().split() if name.endswith(".py")]
    exported = subprocess.run(["git", "-C", str(PI_ROOT), "archive", "--format=tar", release,
                               "--", *modules], capture_output=True, timeout=60)
    assert exported.returncode == 0, exported.stderr
    tree = tmp_path / release
    tree.mkdir()
    with tarfile.open(fileobj=io.BytesIO(exported.stdout)) as archive:
        for member in archive.getmembers():
            (tree / member.name).write_bytes(archive.extractfile(member).read())
    return tree


_OLD_STORE = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
sys.dont_write_bytecode = True
from product_service import ProductScheduleStore
store = ProductScheduleStore(sys.argv[2])
try:
    listed = sorted(entry["name"] for _, entry in store.entries())
except RuntimeError as error:
    listed = str(error)
print(json.dumps({"healthy": store.is_healthy(), "entries": listed}))
"""


def _old_store_sees(tree, path):
    child = subprocess.run([sys.executable, "-c", _OLD_STORE, str(tree), str(path)],
                           capture_output=True, text=True, timeout=120, cwd=tree)
    assert child.returncode == 0, child.stderr[-800:]
    return json.loads(child.stdout)


def _old_tool(tree, *arguments):
    return subprocess.run(
        [sys.executable, str(tree / "state_tool.py"), *arguments],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PI_AUDIT_KEY": KEY.decode()},
        capture_output=True, text=True, cwd=tree, timeout=120)


def test_a_release_before_schema_two_refuses_a_state_this_one_opened(tmp_path):
    """The rollback rule: the old release never opens migrated state. Its
    store reads the marker as a corrupt store, so its readiness fails and
    every tick fails, instead of serving with no schedules; its state_tool
    refuses to back the state up."""
    tree = _old_release(tmp_path)
    fresh = tmp_path / "fresh"
    _store(fresh)
    assert _old_store_sees(tree, fresh / "product-schedules.json") == {
        "healthy": False, "entries": "product schedule store is corrupt"}
    state = _schema_one_state(tmp_path / "state", legacy_sandboxes=True)
    assert _old_store_sees(tree, state / "product-schedules.json")["healthy"] is True
    state_tool.migrate_state(state, tmp_path / "pre-migration.tar.gz", audit_key=KEY)
    assert _old_store_sees(tree, state / "product-schedules.json") == {
        "healthy": False, "entries": "product schedule store is corrupt"}
    refused = _old_tool(tree, "backup", "--state", str(state),
                        "--output", str(tmp_path / "old.tar.gz"))
    assert refused.returncode == 2 and "schedule state is not an object" in refused.stderr, (
        refused.stderr[-800:])


def test_the_rollback_restores_the_pre_migration_backup_for_the_old_release(tmp_path):
    """The documented rollback path, with the old release's own code: its
    state_tool restores the migration's pre-migration backup into a new
    path, and its store there holds every schedule as it was."""
    tree = _old_release(tmp_path)
    state = _schema_one_state(tmp_path / "state", legacy_sandboxes=True)
    document = (state / "product-schedules.json").read_bytes()
    backup = tmp_path / "pre-migration.tar.gz"
    state_tool.migrate_state(state, backup, audit_key=KEY)
    restored = tmp_path / "restored"
    result = _old_tool(tree, "restore", "--backup", str(backup), "--state", str(restored))
    assert result.returncode == 0, result.stderr[-800:]
    assert (restored / "product-schedules.json").read_bytes() == document
    assert _old_store_sees(tree, restored / "product-schedules.json") == {
        "healthy": True, "entries": ["daily", "daily", "weekly"]}


# ── backups hold the rows, authenticated like every other file ─────────────


def _product_state(root, clock=None):
    """A drained state the product wrote: schedules of two tenants, one of
    them claimed, the other with a run recorded."""
    store = _store(root, clock=clock)
    for tenant in ("tenant-a", "tenant-b"):
        principal = _principal(tenant)
        store.put(principal, name="daily", session="work", message=f"{tenant} {SPREAD}",
                  every_ms=60_000)
        store.put(principal, name="weekly", session="home", message=f"{tenant} weekly",
                  every_ms=600_000)
    key = ProductScheduleStore._key("tenant-a", "daily")
    assert store.claim_due(key, 10**7, "worker", 300) is not None
    store.mark_run(ProductScheduleStore._key("tenant-b", "weekly"), 10**7, "ok")
    _drain(root)
    return store


@pytest.mark.parametrize("online", [False, True], ids=["offline", "online"])
def test_a_backup_round_trips_every_schedule_row(tmp_path, online):
    state = tmp_path / "state"
    store = _product_state(state)
    listing = store.entries()
    messages = {tenant: store.for_tenant(tenant) for tenant in ("tenant-a", "tenant-b")}
    backup = tmp_path / "state.tar.gz"
    manifest = create_backup(state, backup, audit_key=KEY, online=online)
    assert {"product-schedules.json", "product-quotas.sqlite3"} <= {
        entry["path"] for entry in manifest["files"]}
    restored = tmp_path / "restored"
    restore_backup(backup, restored, audit_key=KEY)
    again = ProductScheduleStore(restored / "product-schedules.json")
    assert (restored / "product-schedules.json").read_bytes() == (
        product_service.SCHEDULE_STORE_MARKER)
    assert again.entries() == listing
    assert {tenant: again.for_tenant(tenant) for tenant in messages} == messages


def _plant(root, sql, *parameters):
    with closing(sqlite3.connect(root / "product-quotas.sqlite3")) as db:
        db.execute(sql, parameters)
        db.commit()


KEY_A = f"{_digest('tenant-a')}:daily"
ROW_DAMAGE = {
    "a-tenant-that-is-not-a-digest": (
        "UPDATE schedules SET tenant_sha256='tenant-a' WHERE key=?", KEY_A),
    "a-key-that-is-not-its-tenant-and-name": (
        "UPDATE schedules SET name='other' WHERE key=?", KEY_A),
    "another-tenant-s-session": (
        "UPDATE schedules SET internal_session=? WHERE key=?",
        _internal_session("tenant-b", "work"), KEY_A),
    "an-interval-that-is-not-positive": (
        "UPDATE schedules SET every_ms=0 WHERE key=?", KEY_A),
    "a-message-byte-count-that-is-not-its-message-s": (
        "UPDATE schedules SET message_bytes=message_bytes-1 WHERE key=?", KEY_A),
    "a-schedule-without-its-message": (
        "DELETE FROM schedule_messages WHERE key=?", KEY_A),
    "a-message-without-its-schedule": (
        "INSERT INTO schedule_messages(key, message) VALUES(?, 'stray')",
        f"{_digest('tenant-c')}:daily"),
    "a-lease-without-its-end": (
        "UPDATE schedules SET lease_until=NULL WHERE key=?", KEY_A),
}


@pytest.mark.parametrize("damage", ROW_DAMAGE)
def test_a_backup_refuses_schedule_rows_no_release_wrote(tmp_path, damage):
    state = tmp_path / "state"
    _product_state(state)
    _plant(state, *ROW_DAMAGE[damage])
    with pytest.raises(StateToolError, match="schedule state"):
        create_backup(state, tmp_path / "out.tar.gz", audit_key=KEY)
    assert not (tmp_path / "out.tar.gz").exists()


def test_a_backup_refuses_schedule_rows_without_the_marker(tmp_path):
    """Rows the marker does not announce are schedules a release before
    schema 2 would not see: refused, not archived."""
    state = tmp_path / "state"
    _product_state(state)
    (state / "product-schedules.json").unlink()
    with pytest.raises(StateToolError, match="schedule state"):
        create_backup(state, tmp_path / "out.tar.gz", audit_key=KEY)


# ── the migration: the schedule store's step ───────────────────────────────


def _expected_rows(entries, legacy_unix=None):
    rows = {}
    for key, entry in entries:
        row = {"key": key, "tenant_sha256": entry["tenant_sha256"],
               "principal": entry["principal"], "name": entry["name"],
               "session": entry["session"], "internal_session": entry["internal_session"],
               "every_ms": entry["every_ms"],
               "message_bytes": ProductScheduleStore._stored_bytes(entry["message"]),
               "created_unix": entry.get("created_unix", legacy_unix),
               "updated_unix": entry.get("updated_unix", legacy_unix),
               "last_run": entry.get("last_run"), "last_status": entry.get("last_status"),
               "lease_owner": entry.get("lease_owner"),
               "lease_until": entry.get("lease_until"), "message": entry["message"]}
        rows[key] = row
    return rows


def test_a_dry_run_reports_the_schedule_step_and_changes_nothing(tmp_path):
    state = _schema_one_state(tmp_path / "state")
    before, usage = _tree(state), _usage(state)
    report = state_tool.migrate_state(state, None, audit_key=KEY, dry_run=True)
    assert report["steps"] == ["schedule-store-rows"]
    assert report["schedule_store"] == {
        "migration": "schedule-store-rows", "from_schema": 1, "to_schema": 2,
        "schedules": 3}
    assert (report["from_layout"], report["to_layout"], report["moves"]) == (2, 2, [])
    assert report["dry_run"] is True and report["backup"] is None
    assert _tree(state) == before and _usage(state) == usage and _rows(state) == {}
    assert sorted(os.listdir(tmp_path)) == ["state"], "a dry run wrote a file"


def test_the_migration_moves_every_schedule_into_rows_and_marks_the_store(tmp_path):
    state = _schema_one_state(tmp_path / "state")
    document = (state / "product-schedules.json").read_bytes()
    before, usage = _tree(state), _usage(state)
    backup = tmp_path / "pre-migration.tar.gz"
    report = state_tool.migrate_state(state, backup, audit_key=KEY)
    assert (report["steps"], report["dry_run"], report["resumed"]) == (
        ["schedule-store-rows"], False, False)
    assert report["schedule_store"]["schedules"] == 3
    assert _rows(state) == _expected_rows(SCHEMA_ONE), "a schedule changed on its way"
    assert (state / "product-schedules.json").read_bytes() == (
        product_service.SCHEDULE_STORE_MARKER)
    expected = dict(before, **{"product-schedules.json": product_service.SCHEDULE_STORE_MARKER})
    assert _tree(state) == expected and _usage(state) == usage, (
        "the migration did more than move the schedules into rows")
    assert not (state / JOURNAL).exists()
    assert stat.S_IMODE(backup.stat().st_mode) == 0o400
    with tarfile.open(backup) as archive:
        assert archive.extractfile("data/product-schedules.json").read() == document, (
            "the pre-migration backup does not hold the schema-1 document")
    # The output is what this release opens and backs up.
    assert product_service.unmigrated_schedule_store(state) is None
    store = ProductScheduleStore(state / "product-schedules.json")
    assert [e["message"] for e in store.for_tenant("tenant-b")] == [SPREAD]
    assert store.get(SCHEMA_ONE[2][0])["lease_owner"] == "123-worker"
    create_backup(state, tmp_path / "after.tar.gz", audit_key=KEY)


def test_a_layout_one_state_runs_both_steps_in_order(tmp_path):
    state = _schema_one_state(tmp_path / "state", legacy_sandboxes=True)
    report = state_tool.migrate_state(state, tmp_path / "pre-migration.tar.gz", audit_key=KEY)
    assert report["steps"] == ["sandbox-full-digest", "schedule-store-rows"]
    assert (report["migration"], report["from_layout"], report["to_layout"]) == (
        "sandbox-full-digest", 1, 2)
    assert len(report["moves"]) == 3
    assert report["schedule_store"]["from_schema"] == 1
    assert _rows(state) == _expected_rows(SCHEMA_ONE)
    assert product_service.unmigrated_schedule_store(state) is None
    create_backup(state, tmp_path / "after.tar.gz", audit_key=KEY)


def test_a_second_migration_finds_nothing_to_do_and_writes_nothing(tmp_path):
    state = _schema_one_state(tmp_path / "state")
    state_tool.migrate_state(state, tmp_path / "first.tar.gz", audit_key=KEY)
    after, rows = _tree(state), _rows(state)
    report = state_tool.migrate_state(state, tmp_path / "second.tar.gz", audit_key=KEY)
    assert report["steps"] == [] and report["backup"] is None
    assert report["schedule_store"] == {
        "migration": "schedule-store-rows", "from_schema": 2, "to_schema": 2,
        "schedules": 0}
    assert not (tmp_path / "second.tar.gz").exists()
    assert _tree(state) == after and _rows(state) == rows


def test_a_legacy_entry_without_timestamps_keeps_the_document_s_boundary(tmp_path):
    """A release before retention wrote no per-entry timestamps, and the
    store used its file's mtime as each such entry's activity, so that the
    first sweep after an upgrade kept it. The migration writes that boundary
    into the row: retention then keeps the entry while the boundary is
    recent, and removes it once it is not."""
    legacy = []
    for name, mtime in (("recent", 800.0), ("stale", 100.0)):
        key, entry = _schema_one_entry("tenant-a", "daily", "project", "m")
        del entry["created_unix"], entry["updated_unix"]
        legacy.append((key, entry))
        state = _schema_one_state(tmp_path / name, entries=[(key, entry)])
        os.utime(state / "product-schedules.json", (mtime, mtime))
        state_tool.migrate_state(state, tmp_path / f"{name}.tar.gz", audit_key=KEY)
        assert _rows(state) == _expected_rows([(key, entry)], legacy_unix=mtime)
        store = ProductScheduleStore(state / "product-schedules.json")
        assert store.remove_inactive(500.0, 1000.0) == (name == "stale")


_KILLED = r'''
import os, signal, sys
sys.path.insert(0, sys.argv[1])
sys.dont_write_bytecode = True
import state_tool
point, state, backup = sys.argv[2], sys.argv[3], sys.argv[4]

def kill_on(nth, original):
    calls = []
    def wrapper(*args, **kwargs):
        calls.append(args)
        if len(calls) == nth:
            os.kill(os.getpid(), signal.SIGKILL)
        return original(*args, **kwargs)
    return wrapper

if point == "before-the-journal":
    state_tool.write_durably = kill_on(1, state_tool.write_durably)
elif point == "before-the-rows":
    state_tool._commit_schedule_rows = kill_on(1, state_tool._commit_schedule_rows)
elif point == "before-the-marker":
    state_tool.write_durably = kill_on(2, state_tool.write_durably)
else:
    state_tool._finish_migration = kill_on(1, state_tool._finish_migration)
state_tool.migrate_state(state, backup, audit_key=os.environ["PI_AUDIT_KEY"].encode())
print("the migration finished without reaching its kill point")
'''


@pytest.mark.parametrize("point", ["before-the-journal", "before-the-rows",
                                   "before-the-marker", "before-the-journal-is-removed"])
def test_a_migration_killed_at_any_point_of_the_schedule_step_finishes_when_run_again(
        tmp_path, point):
    """A real kill in a child process at each step: nothing done yet but the
    backup; the journal written; the rows committed and the document still
    there; the marker written. The same command run again finishes, with
    the backup it took, and leaves exactly what an uninterrupted run does."""
    state = _schema_one_state(tmp_path / "state")
    backup = tmp_path / "pre-migration.tar.gz"
    child = subprocess.run(
        [sys.executable, "-c", _KILLED, str(PI_ROOT), point, str(state), str(backup)],
        env={**os.environ, "PI_AUDIT_KEY": KEY.decode()}, capture_output=True, text=True,
        timeout=120)
    assert child.returncode == -signal.SIGKILL, (child.returncode, child.stdout,
                                                 child.stderr[-800:])
    if point == "before-the-journal":
        assert not (state / JOURNAL).exists() and backup.exists()
        backup = tmp_path / "second-backup.tar.gz"     # a backup is never overwritten
    else:
        assert (state / JOURNAL).exists()
        assert "state migration is incomplete" in product_main.unmigrated_state(state)
        with pytest.raises(StateToolError, match="state migration is incomplete"):
            create_backup(state, tmp_path / "meanwhile.tar.gz", audit_key=KEY)
    taken = hashlib.sha256(backup.read_bytes()).hexdigest() if backup.exists() else None
    report = state_tool.migrate_state(state, backup, audit_key=KEY)
    assert report["resumed"] is (point != "before-the-journal")
    assert _rows(state) == _expected_rows(SCHEMA_ONE)
    assert (state / "product-schedules.json").read_bytes() == (
        product_service.SCHEDULE_STORE_MARKER)
    assert not (state / JOURNAL).exists()
    if taken is not None:
        assert hashlib.sha256(backup.read_bytes()).hexdigest() == taken, (
            "a resumed migration rewrote its backup")
    create_backup(state, tmp_path / "after.tar.gz", audit_key=KEY)


def test_the_journal_names_its_steps_and_a_first_migration_s_journal_still_resumes(
        tmp_path):
    """This release's journal names the steps its run takes. A journal the
    first migration wrote (schema 1, the sandbox step alone) still resumes,
    and the run then takes every step the state needs."""
    state = _schema_one_state(tmp_path / "state", legacy_sandboxes=True)
    backup = tmp_path / "pre-migration.tar.gz"
    child = subprocess.run(
        [sys.executable, "-c", _KILLED, str(PI_ROOT), "before-the-rows", str(state),
         str(backup)], env={**os.environ, "PI_AUDIT_KEY": KEY.decode()},
        capture_output=True, text=True, timeout=120)
    assert child.returncode == -signal.SIGKILL, child.stderr[-800:]
    journal = json.loads((state / JOURNAL).read_text())
    assert (journal["schema_version"], journal["steps"]) == (
        2, ["sandbox-full-digest", "schedule-store-rows"])
    (state / JOURNAL).write_text(json.dumps({
        "schema_version": 1, "migration": "sandbox-full-digest", "from_layout": 1,
        "to_layout": 2, "backup": journal["backup"],
        "backup_sha256": journal["backup_sha256"], "started_unix": 1}))
    report = state_tool.migrate_state(state, backup, audit_key=KEY)
    assert report["resumed"] is True
    assert report["steps"] == ["sandbox-full-digest", "schedule-store-rows"]
    assert _rows(state) == _expected_rows(SCHEMA_ONE)
    assert not (state / JOURNAL).exists()


RESUMED_AFTER = ("the-sandbox-directory-sync-failed", "it-stopped-before-its-journal-went")


@pytest.mark.parametrize("interruption", RESUMED_AFTER)
def test_a_resumed_migration_makes_every_step_durable_before_its_journal_goes(
        tmp_path, monkeypatch, interruption):
    """Review R2 (2026-11-17). A run that stopped after its last rename leaves
    the next nothing to move, and one that stopped after the store's marker
    leaves it nothing to write. Neither tells the next run whether the
    directory entries it made reached the disk: the sync may have failed, or
    the run may have died before it. The resumed run used to skip both syncs
    and remove its journal, so a power cut could bring back the old names or
    the old document after the migration had said it was done. Now it syncs
    every directory a step of the migration writes, and then removes it."""
    state = _schema_one_state(tmp_path / "state", legacy_sandboxes=True)
    backup = tmp_path / "pre-migration.tar.gz"
    sandboxes, root = (state / "sandboxes").resolve(), state.resolve()
    real_sync, real_finish = state_tool.sync_directory, state_tool._finish_migration
    with monkeypatch.context() as first_run:
        if interruption == "the-sandbox-directory-sync-failed":
            def sync_directory(path):
                if Path(path).resolve() == sandboxes:
                    raise OSError(errno.EIO, "the sandbox directory's sync failed")
                return real_sync(path)
            first_run.setattr(state_tool, "sync_directory", sync_directory)
        else:
            def stop(state_dir, journal_path):
                raise OSError(errno.EIO, "the run stopped before its journal went")
            first_run.setattr(state_tool, "_finish_migration", stop)
        with pytest.raises(OSError):
            state_tool.migrate_state(state, backup, audit_key=KEY)
    assert (state / JOURNAL).is_file()
    assert state_tool.legacy_sandbox_directories(state / "sandboxes") == []

    events = []

    def observed_sync(path):
        events.append(Path(path).resolve())
        return real_sync(path)

    def observed_finish(state_dir, journal_path):
        events.append("the journal goes")
        return real_finish(state_dir, journal_path)

    monkeypatch.setattr(state_tool, "sync_directory", observed_sync)
    monkeypatch.setattr(state_tool, "_finish_migration", observed_finish)
    report = state_tool.migrate_state(state, backup, audit_key=KEY)
    assert report["resumed"] is True and report["moves"] == []
    synced = set(events[:events.index("the journal goes")])
    assert {sandboxes, root} <= synced, (
        f"the resumed migration removed its journal having synced only "
        f"{sorted(str(path) for path in synced)}")
    assert not (state / JOURNAL).exists()
    assert _rows(state) == _expected_rows(SCHEMA_ONE)


def _damaged(entries, change):
    key, entry = entries[0]
    entry = dict(entry)
    key = change(entry) or key
    return [(key, entry), *entries[1:]]


SCHEDULE_DAMAGE = {
    "not-an-object": (b"[]", "schedule state is not an object"),
    "not-json": (b'{"half', "schedule state is corrupt"),
    "an-entry-that-is-not-an-object": (
        json.dumps({SCHEMA_ONE[0][0]: "daily"}).encode(), "not an object"),
    "a-key-that-is-not-its-tenant-and-name": (
        lambda entry: "tenant-a:daily", "key"),
    "a-tenant-that-is-not-a-digest": (
        lambda entry: entry.update(tenant_sha256="tenant-a"), "tenant"),
    "a-name-no-release-accepts": (
        lambda entry: (entry.update(name="../daily"),
                       f"{entry['tenant_sha256']}:../daily")[1], "name"),
    "a-missing-field": (lambda entry: (entry.pop("principal"), None)[1], "principal"),
    "an-unknown-field": (lambda entry: entry.update(prompt="planted"), "prompt"),
    "an-interval-of-zero": (lambda entry: entry.update(every_ms=0), "every_ms"),
    "an-interval-that-is-a-flag": (lambda entry: entry.update(every_ms=True), "every_ms"),
    "a-message-that-is-not-text": (lambda entry: entry.update(message=["m"]), "message"),
    "a-message-that-is-not-valid-text": (
        lambda entry: entry.update(message="\ud800"), "message"),
    "another-session-s-internal-name": (
        lambda entry: entry.update(internal_session=_internal_session("tenant-b", "x")),
        "internal session"),
    "a-lease-without-its-end": (
        lambda entry: entry.update(lease_owner="worker"), "lease"),
    "a-run-that-is-not-a-time": (lambda entry: entry.update(last_run="yesterday"),
                                 "last_run"),
    # Releases before schema 2 stored int(every_ms) unbounded in the document,
    # so an interval of 1e30 was kept (and never ran); a row holds a signed
    # 64-bit integer. The check refuses such a value, naming it, before the
    # migration writes its backup or its journal: let through, the real run
    # died at the rows' commit with an OverflowError, every run after it the
    # same, and the state stayed "state migration is incomplete".
    "an-interval-past-a-64-bit-integer": (
        lambda entry: entry.update(every_ms=2 ** 63), "every_ms"),
    "an-interval-an-earlier-release-kept-from-1e30": (
        lambda entry: entry.update(every_ms=int(1e30)), "every_ms"),
    "a-time-past-a-64-bit-integer": (
        lambda entry: entry.update(last_run=2 ** 63), "last_run"),
}


@pytest.mark.parametrize("damage", SCHEDULE_DAMAGE)
def test_corrupt_schedule_input_is_refused_before_anything_changes(tmp_path, damage):
    change, refusal = SCHEDULE_DAMAGE[damage]
    state = _schema_one_state(tmp_path / "state")
    if isinstance(change, bytes):
        content = change
    else:
        content = json.dumps(dict(_damaged(SCHEMA_ONE, change)), ensure_ascii=False,
                             sort_keys=True).encode("utf-8", "surrogatepass")
    (state / "product-schedules.json").write_bytes(content)
    before, files = _tree(state), sorted(os.listdir(tmp_path))
    for dry_run in (True, False):
        with pytest.raises(StateToolError, match=refusal):
            state_tool.migrate_state(state, None if dry_run else tmp_path / "pre.tar.gz",
                                     audit_key=KEY, dry_run=dry_run)
        assert _tree(state) == before and sorted(os.listdir(tmp_path)) == files
        assert _rows(state) == {}


@pytest.mark.parametrize("every_ms", [2 ** 63, int(1e30), 1e30, float("inf")],
                         ids=["2**63", "int-1e30", "json-1e30", "infinity"])
def test_an_interval_a_row_cannot_hold_is_refused_with_400_and_writes_nothing(
        tmp_path, every_ms):
    """A JSON body's 1e30 reaches put() as a float and 1e400 as infinity.
    Schema 1 kept int(1e30) in the document; a row holds a signed 64-bit
    integer, and infinity has no int at all. Each is refused as every other
    invalid interval is, 400 invalid_interval, not raised as an OverflowError
    (a 500), and nothing is written (audit F-27)."""
    store = _store(tmp_path / "live")
    with pytest.raises(ProductError) as error:
        store.put(_principal("tenant-a"), name="daily", session="work", message="m",
                  every_ms=every_ms)
    assert (error.value.status, error.value.code) == (400, "invalid_interval")
    assert _rows(tmp_path / "live") == {}


def test_the_largest_interval_a_row_holds_is_kept_as_written(tmp_path):
    store = _store(tmp_path / "live")
    store.put(_principal("tenant-a"), name="daily", session="work", message="m",
              every_ms=2 ** 63 - 1)
    assert [entry["every_ms"] for _, entry in store.entries()] == [2 ** 63 - 1]


def test_rows_that_are_not_the_document_s_are_never_merged(tmp_path):
    """Rows already in the ledger beside a schema-1 document are a run that
    stopped after its commit only if they are exactly the document's. Any
    others are refused before anything changes."""
    state = _schema_one_state(tmp_path / "state")
    with closing(sqlite3.connect(state / "product-quotas.sqlite3")) as db:
        for statement in product_service.SCHEDULE_TABLES:
            db.execute(statement)
        rows = _expected_rows(SCHEMA_ONE[:1])
        for row in rows.values():
            product_service.write_schedule_row(db, dict(
                row, message="another message",
                message_bytes=ProductScheduleStore._stored_bytes("another message")))
        db.commit()
    before, held = _tree(state), _rows(state)
    for dry_run in (True, False):
        with pytest.raises(StateToolError, match="never merge"):
            state_tool.migrate_state(state, None if dry_run else tmp_path / "pre.tar.gz",
                                     audit_key=KEY, dry_run=dry_run)
    assert _tree(state) == before and _rows(state) == held
    assert sorted(os.listdir(tmp_path)) == ["state"]


def test_restore_restores_a_schema_one_archive_and_says_it_needs_migrating(tmp_path):
    """The pre-upgrade backup an older release took holds a schema-1
    document. This release restores it as it was, so that the upgrade path
    can begin from it, and says the state needs migrating."""
    state = _schema_one_state(tmp_path / "state")
    document = (state / "product-schedules.json").read_bytes()
    backup = tmp_path / "pre-migration.tar.gz"
    state_tool.migrate_state(state, backup, audit_key=KEY)
    restored = tmp_path / "restored"
    result = subprocess.run(
        [sys.executable, str(PI_ROOT / "state_tool.py"), "restore", "--backup", str(backup),
         "--state", str(restored)],
        env={**os.environ, "PI_AUDIT_KEY": KEY.decode()}, capture_output=True, text=True,
        timeout=120)
    assert result.returncode == 0, result.stderr[-800:]
    assert (restored / "product-schedules.json").read_bytes() == document
    assert re.search(rf"schema 1.*{MIGRATE}", result.stderr), result.stderr
    report = state_tool.migrate_state(restored, tmp_path / "again.tar.gz", audit_key=KEY)
    assert report["steps"] == ["schedule-store-rows"]
    assert _rows(restored) == _expected_rows(SCHEMA_ONE)


# ── P2's guards, for the rows: F-13 and F-30 ────────────────────────────────


class _Watched:
    """A connection the store opened, which reports its settings as it closes."""

    def __init__(self, db, seen):
        self._db, self._seen = db, seen

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        self._seen.append(tuple(self._db.execute(f"PRAGMA {name}").fetchone()[0]
                                for name in ("synchronous", "secure_delete", "journal_mode")))
        self._db.close()


def _every_operation(store, principal):
    key = ProductScheduleStore._key(principal.tenant_id, "daily")
    internal = _internal_session(principal.tenant_id, "work")
    tenant_hash = _digest(principal.tenant_id)
    for call in (
            lambda: store.put(principal, name="daily", session="work", message="m",
                              every_ms=60_000),
            lambda: store.claim_due(key, 10**7, "worker", 300),
            lambda: store.complete_claim(key, "worker", 10**7, "ok"),
            lambda: store.claim_due(key, 2 * 10**7, "worker", 300),
            lambda: store.release_claim(key, "worker"),
            lambda: store.mark_run(key, 10**7, "ok"),
            lambda: store.remove(principal.tenant_id, "daily"),
            lambda: store.put(principal, name="daily", session="work", message="m",
                              every_ms=60_000),
            lambda: store.remove_session(principal.tenant_id, "work"),
            lambda: store.put(principal, name="daily", session="work", message="m",
                              every_ms=60_000),
            lambda: store.remove_internal(tenant_hash, internal),
            lambda: store.put(principal, name="daily", session="work", message="m",
                              every_ms=60_000),
            lambda: store.remove_inactive(10**10, 10**10)):
        yield call


def test_every_commit_the_store_makes_is_synced_through_the_log(tmp_path, monkeypatch):
    """F-13 for the rows. Every connection the store opens runs in the
    write-ahead log with synchronous FULL, so that a commit is on disk
    before it returns, and with secure_delete, so that a message it deletes
    is overwritten (F-30); every write is in a transaction, and so are the
    tables the first connection makes."""
    store = _store(tmp_path)
    principal = _principal("tenant-a")
    seen, statements = [], []
    real = sqlite3.connect

    def watched(*args, **kwargs):
        db = real(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return _Watched(db, seen)

    monkeypatch.setattr(sqlite3, "connect", watched)
    for call in _every_operation(store, principal):
        before = len(seen)
        call()
        assert len(seen) > before, "an operation opened no connection to the ledger"
    monkeypatch.undo()
    assert set(seen) == {(2, 1, "wal")}, f"connections the store opened ran with {set(seen)}"
    writes = [s for s in statements if s.split()[0].upper() in ("INSERT", "UPDATE", "DELETE")]
    assert writes, "anti-vacuity: no write was seen"
    open_transaction = False
    for statement in statements:
        word = statement.split()[0].upper()
        if word == "BEGIN":
            assert statement.upper() == "BEGIN IMMEDIATE", statement
            open_transaction = True
        elif word in ("COMMIT", "ROLLBACK"):
            open_transaction = False
        elif word in ("INSERT", "UPDATE", "DELETE"):
            assert open_transaction, f"a write outside a transaction: {statement}"
    # The first connection makes the tables, in one transaction: a backup's
    # capture of the ledger then holds both of them or neither.
    words = [statement.split()[0].upper() for statement in statements]
    creates = [i for i, word in enumerate(words) if word == "CREATE"]
    assert creates, "anti-vacuity: the store made no table"
    begin = max(i for i in range(creates[0]) if words[i] == "BEGIN")
    assert statements[begin] == "BEGIN IMMEDIATE" and "COMMIT" not in words[begin:creates[-1]], (
        "the tables were made one statement at a time: a capture can hold one without the other")


REMOVALS = {
    "remove": lambda store, now: store.remove("tenant-a", "doomed"),
    "remove_session": lambda store, now: store.remove_session("tenant-a", "doomed"),
    "remove_internal": lambda store, now: store.remove_internal(
        _digest("tenant-a"), _internal_session("tenant-a", "doomed")),
    "remove_inactive": lambda store, now: store.remove_inactive(5000.0, now[0]),
    "replaced": lambda store, now: store.put(
        _principal("tenant-a"), name="doomed", session="doomed", message="replaced",
        every_ms=60_000),
}


@pytest.mark.parametrize("removal", REMOVALS)
def test_a_deleted_or_replaced_message_is_left_in_no_file(tmp_path, removal):
    """F-30 for the rows. A message deleted with its schedule or its session,
    by retention, or replaced by its owner, was in the ledger's pages and in
    its log. Once the removal returns no file holds a byte of it; the other
    schedules are as they were. Another worker's connection stays open
    throughout, as the service's own do under load, so that SQLite does not
    empty the log as the last connection closes: the store must."""
    now = [100.0]
    store = _store(tmp_path, clock=lambda: now[0])
    principal = _principal("tenant-a")
    store.put(principal, name="doomed", session="doomed", message=SPREAD, every_ms=60_000)
    now[0] = 10_000.0
    store.put(principal, name="kept", session="work", message="kept " * 200, every_ms=60_000)
    _checkpoint(tmp_path)
    with closing(sqlite3.connect(tmp_path / "product-quotas.sqlite3")) as worker:
        worker.execute("SELECT COUNT(*) FROM schedules").fetchone()
        store.mark_run(ProductScheduleStore._key("tenant-a", "kept"), 10_001.0, "ok")
        assert _holding(tmp_path), "anti-vacuity: the message was in no file to begin with"
        REMOVALS[removal](store, now)
        assert _holding(tmp_path) == [], (
            f"after {removal}, these files still hold the message: {_holding(tmp_path)}")
        assert [e["message"] for e in store.for_tenant("tenant-a")
                if e["name"] == "kept"] == ["kept " * 200]


_TORN = r"""
import os, resource, signal, sqlite3, sys
sys.path.insert(0, sys.argv[1])
sys.dont_write_bytecode = True
from product_service import DurableQuotaStore, Principal, ProductScheduleStore
root = sys.argv[2]
DurableQuotaStore(os.path.join(root, "product-quotas.sqlite3"))
store = ProductScheduleStore(os.path.join(root, "product-schedules.json"))
principal = Principal("planner", "tenant-a", frozenset({"schedules:write", "chat"}), frozenset())
store.put(principal, name="daily", session="work", message="committed", every_ms=60000)
keeper = sqlite3.connect(os.path.join(root, "product-quotas.sqlite3"))   # keeps the log
keeper.execute("SELECT COUNT(*) FROM schedules").fetchone()   # opens the log and its index
page = keeper.execute("PRAGMA page_size").fetchone()[0]
wal = os.path.join(root, "product-quotas.sqlite3-wal")
log = os.path.getsize(wal) if os.path.exists(wal) else 0
signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
resource.setrlimit(resource.RLIMIT_FSIZE,
                   (log + 10 * (page + 24), resource.getrlimit(resource.RLIMIT_FSIZE)[1]))
store.put(principal, name="other", session="work", message=sys.argv[3], every_ms=60000)
print("the commit was not torn")
"""


def test_a_removal_s_checkpoint_holds_up_no_other_writer_of_the_ledger(tmp_path):
    """After a removal the store empties the log (F-30) with a TRUNCATE
    checkpoint, which waits for every reader of an older snapshot, such as an
    online backup's capture of the ledger, and while it waits no other writer
    of the ledger can commit: a quota write waited for the whole capture, and
    one that waits past its own 5 s busy timeout fails as "database is
    locked". The checkpoint waits briefly and leaves the log to the next one
    instead; the capture holds the message from before the removal anyway."""
    principal = _principal("tenant-a")
    store = _store(tmp_path)
    store.put(principal, name="doomed", session="work", message=SPREAD, every_ms=60_000)
    quota = _quota(tmp_path)
    capture = sqlite3.connect(tmp_path / "product-quotas.sqlite3", isolation_level=None,
                              check_same_thread=False)
    capture.execute("BEGIN")
    capture.execute("SELECT COUNT(*) FROM schedules").fetchone()
    remover = threading.Thread(target=store.remove, args=("tenant-a", "doomed"))
    remover.start()
    try:
        time.sleep(0.5)     # the removal has committed; its checkpoint is pending
        started = time.monotonic()
        quota.acquire_turn("tenant-a").release(
            usage={"input_tokens": 1, "output_tokens": 1},
            session_id=_internal_session("tenant-a", "work"), storage_bytes=0, audit_bytes=0)
        waited = time.monotonic() - started
    finally:
        time.sleep(0.2)
        capture.execute("COMMIT")
        capture.close()
        remover.join(30)
    assert not remover.is_alive()
    assert store.entries() == []
    assert waited < 1.5, (
        f"a quota write waited {waited:.2f} s behind the schedule store's checkpoint while "
        "an older snapshot was being read")


def test_a_commit_a_crash_tears_leaves_the_committed_schedules(tmp_path):
    """A real crash in the middle of a commit: the kernel kills the child
    (SIGXFSZ) when its write of the log's eleventh new page crosses the file
    size limit, ten pages of the new schedule already written. The ledger
    holds the committed schedule and nothing of the torn one, and the
    store's startup sweep leaves the torn bytes in no file."""
    child = subprocess.run([sys.executable, "-c", _TORN, str(PI_ROOT), str(tmp_path), SPREAD],
                           capture_output=True, text=True, timeout=120)
    assert child.returncode == -signal.SIGXFSZ, (child.returncode, child.stdout,
                                                 child.stderr[-800:])
    assert _holding(tmp_path), "anti-vacuity: the crash tore nothing: the log holds no page"
    # Another worker's connection: SQLite then leaves the log as it is when
    # this one's close, which would otherwise empty it, and the sweep must.
    with closing(sqlite3.connect(tmp_path / "product-quotas.sqlite3")) as worker:
        worker.execute("SELECT COUNT(*) FROM schedules").fetchone()
        store = ProductScheduleStore(tmp_path / "product-schedules.json")
        assert [e["message"] for e in store.for_tenant("tenant-a")] == ["committed"]
        assert worker.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert _holding(tmp_path), "anti-vacuity: the log was emptied before the sweep"
        store.remove_scratch()
        assert _holding(tmp_path) == [], f"the torn commit's bytes are in {_holding(tmp_path)}"
        assert [e["message"] for e in store.for_tenant("tenant-a")] == ["committed"]


def test_a_full_disk_at_the_commit_changes_nothing(tmp_path, monkeypatch):
    """A fault injected into the store's write: SQLite's own full-disk error
    (SQLITE_FULL), raised by the ledger's page limit. The store reports it as
    the full disk it is, and the committed schedule is untouched."""
    store = _store(tmp_path)
    principal = _principal("tenant-a")
    store.put(principal, name="daily", session="work", message="committed", every_ms=60_000)
    real = sqlite3.connect

    def full(*args, **kwargs):
        db = real(*args, **kwargs)
        pages = db.execute("PRAGMA page_count").fetchone()[0]
        db.execute(f"PRAGMA max_page_count={max(pages, 1)}")
        return db

    monkeypatch.setattr(sqlite3, "connect", full)
    with pytest.raises(OSError) as error:
        store.put(principal, name="daily", session="work", message=SPREAD, every_ms=60_000)
    monkeypatch.undo()
    assert error.value.errno == errno.ENOSPC
    assert [e["message"] for e in store.for_tenant("tenant-a")] == ["committed"]


def test_a_ledger_that_is_not_a_database_is_a_corrupt_store(tmp_path):
    store = _store(tmp_path)
    (tmp_path / "product-quotas.sqlite3").write_bytes(b"not a database" * 512)
    for name in ("product-quotas.sqlite3-wal", "product-quotas.sqlite3-shm"):
        (tmp_path / name).unlink(missing_ok=True)
    fresh = ProductScheduleStore(tmp_path / "product-schedules.json")
    with pytest.raises(RuntimeError, match="product schedule store is corrupt"):
        fresh.entries()
    assert fresh.is_healthy() is False
    assert store.is_healthy() is False


def test_a_schedule_whose_message_row_is_gone_is_a_corrupt_store(tmp_path):
    store = _store(tmp_path)
    store.put(_principal("tenant-a"), name="daily", session="work", message="m",
              every_ms=60_000)
    _plant(tmp_path, "DELETE FROM schedule_messages")
    key = ProductScheduleStore._key("tenant-a", "daily")
    for read in (lambda: store.for_tenant("tenant-a"),
                 lambda: store.get(key, with_message=True)):
        with pytest.raises(RuntimeError, match="product schedule store is corrupt"):
            read()
    assert store.get(key)["name"] == "daily"

