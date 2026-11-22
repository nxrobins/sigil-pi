"""A session's sandbox is named by the full digest of its id (audit
2026-11-10, F-23).

Conversations and audit chains were named by the full SHA-256 of the
session's id; its sandbox directory by the first 16 hex characters, 64 bits.
A tenant who knew another tenant's session could search, at about 2**64
hashes, for a session name of its own whose digest began the same way, and
its tools would then read and write the other tenant's files.

Sandboxes are now named by the whole digest (state layout 2). State that a
release before it wrote (layout 1) moves by `state_tool.py migrate`, which is
versioned, dry-runnable and restartable, takes an immutable backup first,
and reports, never merges, a directory two sessions share. The service and
`state_tool.py backup` refuse layout-1 state, naming the command, before
they change anything in it.
"""

import ast
import hashlib
import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import tarfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

from conftest import PI_ROOT, make_agent, msg, text, tool_use
from test_product_main import _base_env, _install_happy_runtime

import agent as agent_module
import product_main
import product_service
import state_tool
from agent import AuditLog, PiAgent, SessionStore
from product_service import (
    ConfigError,
    DurableQuotaStore,
    ProductDataManager,
    ProductRetentionMonitor,
    ProductScheduleStore,
    SessionOperationLocks,
    _internal_session,
)
from state_tool import CLEAN_MARKER, StateToolError, create_backup, restore_backup

KEY = b"sandbox-width-audit-key-0123456789abcdef"
JOURNAL = ".state-migration.json"
SESSIONS = (("tenant-a", "project"), ("tenant-a", "notes"), ("tenant-b", "project"))


def _digest(value):
    """The digest the product names a session's files by. Read through the
    agent module's hashlib, so a test that chooses digests sees its own."""
    return agent_module.hashlib.sha256(value.encode()).hexdigest()


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _quota(root, clock=None):
    return DurableQuotaStore(
        root / "product-quotas.sqlite3", clock=clock, max_concurrent_turns=10,
        tokens_per_day=10_000_000, token_reservation_per_turn=1000,
        storage_bytes_per_tenant=1 << 30, storage_reservation_per_turn=1 << 20,
        audit_bytes_per_tenant=1 << 28, audit_reservation_per_turn=1 << 16)


def _register(quota, tenant, internal):
    lease = quota.acquire_turn(tenant, session_id=internal)
    lease.release(session_id=internal, storage_bytes=1, audit_bytes=1)


def _drain(root):
    (root / CLEAN_MARKER).write_text(json.dumps({
        "schema_version": 1, "product_version": "0.4.0", "clean_shutdown_unix": 100}))


def _layout_one_state(root, sessions=SESSIONS, key=KEY):
    """A drained product state as a release before layout 2 left it: each
    session's sandbox is named by the first 16 hex characters of its digest.
    Returns {(tenant, session): digest}."""
    store = SessionStore(root / "sessions")
    audit = AuditLog(root / "audit", key=key)
    quota = _quota(root)
    digests = {}
    for tenant, external in sessions:
        internal = _internal_session(tenant, external)
        digest = _digest(internal)
        store.save(internal, [{"role": "user", "content": f"{tenant}/{external}"}])
        audit.record(internal, "write_file", "source", "input", "output", None, None, 1)
        sandbox = root / "sandboxes" / digest[:16]
        (sandbox / "nested").mkdir(parents=True, exist_ok=True)
        (sandbox / f"{tenant}.{external}.txt").write_text(f"{tenant}/{external} note")
        (sandbox / "nested" / f"{tenant}.{external}.bin").write_bytes(digest.encode())
        _register(quota, tenant, internal)
        digests[(tenant, external)] = digest
    _drain(root)
    return digests


def _tree(root):
    """Every entry under `root` but the quota ledger, by relative path: a
    file's bytes, or None for a directory. The ledger is compared by its rows
    (`_rows`): SQLite may rewrite its pages without changing them."""
    return {path.relative_to(root).as_posix(): (path.read_bytes() if path.is_file() else None)
            for path in sorted(Path(root).rglob("*"))
            if not path.name.startswith("product-quotas.sqlite3")}


def _rows(root):
    with closing(sqlite3.connect(f"file:{root / 'product-quotas.sqlite3'}?mode=ro",
                                 uri=True)) as db:
        return db.execute("SELECT * FROM session_usage ORDER BY session").fetchall()


def _moves(digests):
    return sorted(({"from": f"sandboxes/{digest[:16]}", "to": f"sandboxes/{digest}"}
                   for digest in digests.values()), key=lambda move: move["from"])


def _migrated(tree, digests):
    """A layout-1 snapshot as the migration must leave it: each sandbox
    under its full digest, every byte as it was, nothing else changed."""
    full = {digest[:16]: digest for digest in digests.values()}
    out = {}
    for path, content in tree.items():
        parts = path.split("/")
        if parts[0] == "sandboxes" and len(parts) > 1:
            parts[1] = full.get(parts[1], parts[1])
        out["/".join(parts)] = content
    return out


def _old_release_opens(root):
    """The attribution rule a release before layout 2 applies at startup
    (ProductDataManager.verify_quota_registry at c41f3e5): every sandbox
    directory must be the first 16 hex characters of a registered digest."""
    registered = {row[1] for row in _rows(root)}
    names = {path.name for path in (root / "sandboxes").iterdir() if path.is_dir()}
    return names <= {digest[:16] for digest in registered}


def _product(root, clock=None):
    """The real agent and data manager the product builds, without a compiler."""
    agent = PiAgent("http://127.0.0.1:9/v1/messages", "key",
                    store=SessionStore(root / "sessions"), sandbox_root=root / "sandboxes",
                    mcp=None, audit=AuditLog(root / "audit", key=KEY))
    quota = _quota(root, clock)
    manager = ProductDataManager(
        agent, ProductScheduleStore(root / "product-schedules.json", clock=clock),
        SessionOperationLocks(root / "locks"), quota_store=quota)
    return agent, quota, manager


class _Digests:
    """hashlib, except that each input listed hashes to the digest listed.

    Finding two session ids whose digests share 64 bits costs about 2**32
    hashes for some pair and 2**64 for a chosen target: too much for a test,
    so the test chooses the digest of one id instead."""

    def __init__(self, chosen):
        self.chosen = {value.encode(): digest for value, digest in chosen.items()}

    def sha256(self, data=b"", **kwargs):
        if isinstance(data, (bytes, bytearray)) and bytes(data) in self.chosen:
            digest = self.chosen[bytes(data)]
            return SimpleNamespace(hexdigest=lambda: digest,
                                   digest=lambda: bytes.fromhex(digest))
        return hashlib.sha256(data, **kwargs)

    def __getattr__(self, name):
        return getattr(hashlib, name)


def _collide(monkeypatch, victim, attacker):
    """Give `attacker` a digest whose first 64 bits are `victim`'s: what a
    tenant who searched 2**64 session names would hold. Returns it."""
    chosen = _digest(victim)[:16] + _digest(attacker)[16:]
    assert chosen != _digest(victim)
    shim = _Digests({attacker: chosen})
    for module in (agent_module, product_service, state_tool):
        monkeypatch.setattr(module, "hashlib", shim)
    return chosen


# ── the width: every surface of a session is named by its full digest ──────


@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(session=st.text(st.characters(blacklist_categories=("Cs",)), min_size=1,
                       max_size=80))
@example(session="v1:" + "0" * 64 + ":" + "f" * 64)
def test_every_surface_of_a_session_is_named_by_the_full_digest_of_its_id(
        tmp_path, session):
    """THE GUARD for F-23's width. The sandbox the agent hands a tool, the
    sandbox the product exports, deletes and measures, the conversation and
    the audit chain are all named by the same 64 hex characters."""
    agent = PiAgent("http://127.0.0.1:9/v1/messages", "key",
                    store=SessionStore(tmp_path / "sessions"),
                    sandbox_root=tmp_path / "sandboxes", mcp=None,
                    audit=AuditLog(tmp_path / "audit", key=KEY))
    manager = ProductDataManager(
        agent, ProductScheduleStore(tmp_path / "product-schedules.json"),
        SessionOperationLocks(tmp_path / "locks"))
    digest = hashlib.sha256(session.encode()).hexdigest()
    sandbox = agent.sandbox_for(session)
    assert sandbox == tmp_path / "sandboxes" / digest, (
        f"the sandbox is sandboxes/{sandbox.name}: named by {len(sandbox.name)} of the "
        f"64 hex characters of its session's digest, so two sessions whose digests "
        f"share them share one directory (audit F-23)")
    conversation, product_sandbox, chain = manager._paths(session)
    assert product_sandbox == sandbox, (
        "the product exports, deletes and measures a sandbox the agent never writes")
    assert (conversation.name, chain.name) == (f"{digest}.kv", f"{digest}.jsonl")


def test_a_tenant_whose_digest_shares_64_bits_with_another_sees_none_of_its_files(
        tmp_path, monkeypatch):
    """F-23's consequence at the product boundary. Tenant B holds a session
    whose digest begins with tenant A's 64 bits. B's export, its storage
    charge and its deletion must not reach A's files."""
    victim = _internal_session("tenant-a", "project")
    attacker = _internal_session("tenant-b", "chosen")
    _collide(monkeypatch, victim, attacker)
    agent, _, manager = _product(tmp_path / "state")
    (agent.sandbox_for(victim) / "a-secret.txt").write_text("tenant A's plans")
    (agent.sandbox_for(attacker) / "b-note.txt").write_text("tenant B's note")

    exported = manager.export(SimpleNamespace(tenant_id="tenant-b"), "chosen")
    assert [item["path"] for item in exported["files"]] == ["b-note.txt"], (
        "tenant B's export holds tenant A's sandbox file: their digests share 64 bits "
        "and the sandbox is named by those 64 bits (audit F-23)")
    assert manager.footprint(attacker)["storage_bytes"] == len("tenant B's note"), (
        "tenant B is charged for tenant A's files")
    manager.delete(SimpleNamespace(tenant_id="tenant-b"), "chosen")
    assert (agent.sandbox_for(victim) / "a-secret.txt").read_text() == "tenant A's plans", (
        "deleting tenant B's session removed tenant A's files")


def test_a_forge_tool_writes_at_full_width_and_a_colliding_session_cannot_read_it(
        scripted_llm, tmp_path, mcp, monkeypatch):
    """The same, on the real compiler: the filesystem grant a tool runs
    under is scoped to the session's sandbox, so the sandbox's name is the
    isolation boundary."""
    victim, attacker = "collision-victim", "collision-attacker"
    _collide(monkeypatch, victim, attacker)
    agent = make_agent(scripted_llm, tmp_path, mcp)
    scripted_llm.script = [
        msg([tool_use("t1", "write_file", {"path": "secret.txt", "content": "victim-only"})]),
        msg([text("written")]),
        msg([tool_use("t2", "read_file", {"path": "secret.txt"})]),
        msg([text("done")]),
    ]
    assert agent.turn(victim, "write my file") == "written"
    assert agent.turn(attacker, "read the same path") == "done"
    result = scripted_llm.requests[-1]["messages"][-1]["content"][0]
    assert result.get("is_error") is True and "victim-only" not in json.dumps(result), (
        f"a session whose digest shares 64 bits with another's read its file: {result}")
    written = sorted(path.relative_to(tmp_path / "sandboxes").as_posix()
                     for path in (tmp_path / "sandboxes").rglob("secret.txt"))
    assert written == [f"{_digest(victim)}/secret.txt"], written


# ── the product and the state tool find a sandbox at full width ────────────


def test_export_deletion_and_the_storage_quota_find_a_sandbox_at_full_width(tmp_path):
    root = tmp_path / "state"
    agent, quota, manager = _product(root)
    internal = _internal_session("tenant-a", "project")
    sandbox = agent.sandbox_for(internal)
    assert sandbox.name == _digest(internal), (
        f"the sandbox is named by {len(sandbox.name)} hex characters (audit F-23)")
    (sandbox / "report.txt").write_text("quarterly numbers")
    _register(quota, "tenant-a", internal)

    assert manager.footprint(internal)["storage_bytes"] == len("quarterly numbers")
    exported = manager.export(SimpleNamespace(tenant_id="tenant-a"), "project")
    assert [item["path"] for item in exported["files"]] == ["report.txt"]
    assert manager.delete(SimpleNamespace(tenant_id="tenant-a"), "project")["sandbox"] is True
    assert os.listdir(root / "sandboxes") == []


def test_retention_removes_a_sandbox_at_full_width(tmp_path):
    root = tmp_path / "state"
    now = [100.0]
    agent, quota, manager = _product(root, clock=lambda: now[0])
    internal = _internal_session("tenant-a", "project")
    sandbox = agent.sandbox_for(internal)
    assert sandbox.name == _digest(internal)
    (sandbox / "stale.txt").write_text("nobody has looked at this")
    _register(quota, "tenant-a", internal)
    now[0] = 1000.0
    monitor = ProductRetentionMonitor(
        quota, manager, manager.schedule_store, manager.session_locks, retention_s=500,
        clock=lambda: now[0], log_sink=lambda record: None)
    assert monitor.tick() is True
    assert os.listdir(root / "sandboxes") == []


def test_offline_and_online_backups_carry_a_sandbox_at_full_width(tmp_path):
    root = tmp_path / "state"
    agent, quota, _ = _product(root)
    internal = _internal_session("tenant-a", "project")
    digest = _digest(internal)
    sandbox = agent.sandbox_for(internal)
    assert sandbox.name == digest
    (sandbox / "report.txt").write_text("quarterly numbers")
    agent.store.save(internal, [{"role": "user", "content": "hello"}])
    _register(quota, "tenant-a", internal)

    create_backup(root, tmp_path / "online.tar.gz", audit_key=KEY, online=True)
    _drain(root)
    create_backup(root, tmp_path / "offline.tar.gz", audit_key=KEY)
    for archive in ("online", "offline"):
        with tarfile.open(tmp_path / f"{archive}.tar.gz") as opened:
            members = {member.name for member in opened.getmembers()}
        assert f"data/sandboxes/{digest}/report.txt" in members, (archive, sorted(members))
        restore_backup(tmp_path / f"{archive}.tar.gz", tmp_path / f"restored-{archive}",
                       audit_key=KEY)
        restored = tmp_path / f"restored-{archive}" / "sandboxes" / digest / "report.txt"
        assert restored.read_text() == "quarterly numbers"


# ── layout-1 state is refused, naming the command, before anything changes ──


def test_startup_refuses_a_state_root_that_holds_an_old_width_sandbox(
        tmp_path, monkeypatch):
    """The real startup. It must refuse before it removes the clean-shutdown
    marker: a migration runs only on a drained state, and a refusal that
    took the marker with it would leave the operator unable to migrate."""
    _base_env(tmp_path, monkeypatch)
    _install_happy_runtime(monkeypatch)
    state = tmp_path / "state"
    _layout_one_state(state, key=b"a" * 32)
    before, rows = _tree(state), _rows(state)
    with pytest.raises(ConfigError, match=r"state_tool\.py migrate"):
        product_main.run()
    assert _tree(state) == before and _rows(state) == rows, (
        "startup changed a state it refused to open")


def test_startup_refuses_a_state_whose_migration_has_not_finished(tmp_path, monkeypatch):
    """Every sandbox moved, but the migration's journal is still there: the
    command has not validated its output. Startup refuses until it has."""
    _base_env(tmp_path, monkeypatch)
    _install_happy_runtime(monkeypatch)
    state = tmp_path / "state"
    digests = _layout_one_state(state, key=b"a" * 32)
    for digest in digests.values():
        (state / "sandboxes" / digest[:16]).rename(state / "sandboxes" / digest)
    (state / JOURNAL).write_text("{}")
    before = _tree(state)
    with pytest.raises(ConfigError, match=r"state migration is incomplete.*state_tool\.py migrate"):
        product_main.run()
    assert _tree(state) == before


def test_the_registry_check_names_the_migration_for_an_old_width_sandbox(tmp_path):
    root = tmp_path / "state"
    digests = _layout_one_state(root)
    agent, quota, manager = _product(root)
    with pytest.raises(ConfigError, match=r"state needs migration.*state_tool\.py migrate"):
        manager.verify_quota_registry(quota.registered_sessions())
    for digest in digests.values():
        (root / "sandboxes" / digest[:16]).rename(root / "sandboxes" / digest)
    manager.verify_quota_registry(quota.registered_sessions())


@pytest.mark.parametrize("online", [False, True], ids=["offline", "online"])
def test_a_backup_refuses_an_old_width_state_and_names_the_migration(tmp_path, online):
    state = tmp_path / "state"
    _layout_one_state(state)
    before = _tree(state)
    with pytest.raises(StateToolError, match=r"state needs migration.*state_tool\.py migrate"):
        create_backup(state, tmp_path / "backup.tar.gz", audit_key=KEY, online=online)
    assert not (tmp_path / "backup.tar.gz").exists()
    assert {k: v for k, v in _tree(state).items() if not k.startswith("locks")} == before



def test_the_research_host_refuses_a_layout_one_sandbox_it_would_pass_over(
        tmp_path, monkeypatch):
    """The research host's state is never migrated (docs/state-compatibility.md),
    and it would not see a layout-1 sandbox: the session's turn gets a new,
    empty one. So it refuses to start on one, as the product does."""
    state = tmp_path / "research"
    old = state / "sandboxes" / hashlib.sha256(b"notebook").hexdigest()[:16]
    old.mkdir(parents=True)
    (old / "notes.txt").write_text("kept for months")
    agent = PiAgent("http://127.0.0.1:9/v1/messages", "key",
                    store=SessionStore(state / "sessions"), sandbox_root=state / "sandboxes",
                    mcp=None)
    assert not (agent.sandbox_for("notebook") / "notes.txt").exists(), (
        "anti-vacuity: a turn on this state would not see its own file")
    monkeypatch.setenv("PI_STATE", str(state))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["agent.py"])
    with pytest.raises(SystemExit) as refused:
        agent_module.main()
    assert "state layout 1" in str(refused.value.code), (
        f"the research host went on with a sandbox it passes over: {refused.value.code!r}")


@pytest.mark.parametrize("online", [False, True], ids=["offline", "online"])
def test_a_backup_still_refuses_a_layout_one_sandbox_that_its_first_check_missed(
        tmp_path, monkeypatch, online):
    """The second line of defense: a layout-1 directory that appears after a
    backup's first check (an old release still serving the state) is refused
    where the backup reads the sandboxes, with the same message."""
    state = tmp_path / "state"
    _layout_one_state(state)
    monkeypatch.setattr(state_tool, "unmigrated_state", lambda state_dir: None)
    with pytest.raises(StateToolError, match=r"state needs migration.*state_tool\.py migrate"):
        create_backup(state, tmp_path / "backup.tar.gz", audit_key=KEY, online=online)
    assert not (tmp_path / "backup.tar.gz").exists()

# ── the migration ───────────────────────────────────────────────────────────


def test_a_dry_run_lists_every_move_and_changes_nothing(tmp_path):
    state = tmp_path / "state"
    digests = _layout_one_state(state)
    before = _tree(state)
    report = state_tool.migrate_state(state, None, audit_key=KEY, dry_run=True)
    assert report["moves"] == _moves(digests) and report["problems"] == []
    assert (report["migration"], report["from_layout"], report["to_layout"]) == (
        "sandbox-full-digest", 1, 2)
    assert report["dry_run"] is True and report["backup"] is None
    assert _tree(state) == before
    assert sorted(os.listdir(tmp_path)) == ["state"], "a dry run wrote a file"


def test_a_migration_backs_up_the_state_then_moves_every_sandbox_to_its_full_digest(
        tmp_path):
    state = tmp_path / "state"
    digests = _layout_one_state(state)
    before, rows = _tree(state), _rows(state)
    backup = tmp_path / "pre-migration.tar.gz"
    report = state_tool.migrate_state(state, backup, audit_key=KEY)
    assert report["moves"] == _moves(digests) and report["problems"] == []
    assert (report["from_layout"], report["to_layout"], report["dry_run"],
            report["resumed"]) == (1, 2, False, False)
    assert _tree(state) == _migrated(before, digests), (
        "the migration did more than move each sandbox to its full digest")
    assert _rows(state) == rows
    assert report["backup"] == str(backup) and report["backup_sha256"] == _sha256(backup)
    assert stat.S_IMODE(backup.stat().st_mode) == 0o400, "the pre-migration backup is writable"
    with tarfile.open(backup) as opened:
        members = {member.name for member in opened.getmembers()}
    assert all(f"data/sandboxes/{digest[:16]}/{tenant}.{external}.txt" in members
               for (tenant, external), digest in digests.items()), (
        "the pre-migration backup does not hold the state as it was")
    # The output is what this release opens and backs up.
    assert agent_module.unmigrated_state(state) is None
    create_backup(state, tmp_path / "after.tar.gz", audit_key=KEY)
    _, quota, manager = _product(state)
    manager.verify_quota_registry(quota.registered_sessions())


def test_a_second_migration_finds_nothing_to_do_and_writes_nothing(tmp_path):
    state = tmp_path / "state"
    _layout_one_state(state)
    state_tool.migrate_state(state, tmp_path / "first.tar.gz", audit_key=KEY)
    after = _tree(state)
    report = state_tool.migrate_state(state, tmp_path / "second.tar.gz", audit_key=KEY)
    assert report["moves"] == [] and report["problems"] == []
    assert (report["from_layout"], report["backup"], report["resumed"]) == (2, None, False)
    assert not (tmp_path / "second.tar.gz").exists()
    assert _tree(state) == after
    assert state_tool.migrate_state(state, None, audit_key=KEY, dry_run=True)["moves"] == []


def test_a_state_written_at_full_width_needs_no_migration(tmp_path):
    root = tmp_path / "state"
    agent, quota, _ = _product(root)
    internal = _internal_session("tenant-a", "project")
    (agent.sandbox_for(internal) / "report.txt").write_text("numbers")
    _register(quota, "tenant-a", internal)
    _drain(root)
    before = _tree(root)
    report = state_tool.migrate_state(root, tmp_path / "unused.tar.gz", audit_key=KEY)
    assert (report["from_layout"], report["moves"], report["backup"]) == (2, [], None)
    assert _tree(root) == before and not (tmp_path / "unused.tar.gz").exists()



def test_a_state_with_no_customer_state_migrates_to_nothing(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    _drain(state)
    report = state_tool.migrate_state(state, tmp_path / "unused.tar.gz", audit_key=KEY)
    assert (report["from_layout"], report["moves"], report["backup"]) == (2, [], None)
    assert sorted(os.listdir(state)) == [CLEAN_MARKER]

KILL_POINTS = ("before-the-journal", "first-rename", "second-rename",
               "before-the-journal-is-removed")

_KILLED_MIGRATION = r'''
import os, signal, sys
sys.path.insert(0, sys.argv[1])
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
elif point == "first-rename":
    os.rename = kill_on(1, os.rename)
elif point == "second-rename":
    os.rename = kill_on(2, os.rename)
else:
    state_tool._finish_migration = kill_on(1, state_tool._finish_migration)
state_tool.migrate_state(state, backup, audit_key=os.environ["PI_AUDIT_KEY"].encode())
print("the migration finished without reaching its kill point")
'''


@pytest.mark.parametrize("point", KILL_POINTS)
def test_a_migration_killed_at_any_point_finishes_when_it_is_run_again(tmp_path, point):
    """A real kill in a child process, at each step the migration takes. A
    run that wrote its journal resumes from it with the backup it took;
    one killed before that starts again and never overwrites a backup."""
    state = tmp_path / "state"
    digests = _layout_one_state(state)
    before = _tree(state)
    backup = tmp_path / "pre-migration.tar.gz"
    child = subprocess.run(
        [sys.executable, "-c", _KILLED_MIGRATION, str(PI_ROOT), point, str(state),
         str(backup)],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PI_AUDIT_KEY": KEY.decode()})
    assert child.returncode == -signal.SIGKILL, (child.returncode, child.stdout, child.stderr)
    moved = {"before-the-journal": 0, "first-rename": 0, "second-rename": 1,
             "before-the-journal-is-removed": 3}[point]
    assert sum((state / "sandboxes" / d).is_dir() for d in digests.values()) == moved
    assert backup.is_file(), "the backup is taken before anything moves"
    taken = _sha256(backup)
    if point == "before-the-journal":
        assert not (state / JOURNAL).exists()
        with pytest.raises(StateToolError, match="refusing to overwrite"):
            state_tool.migrate_state(state, backup, audit_key=KEY)
        assert _sha256(backup) == taken
        backup = tmp_path / "second-attempt.tar.gz"
    else:
        assert (state / JOURNAL).is_file()
        problem = agent_module.unmigrated_state(state)
        assert "state migration is incomplete" in problem and "state_tool.py migrate" in problem
        with pytest.raises(StateToolError, match="state migration is incomplete"):
            create_backup(state, tmp_path / "refused.tar.gz", audit_key=KEY)
        with pytest.raises(StateToolError, match=str(backup)):
            state_tool.migrate_state(state, tmp_path / "other.tar.gz", audit_key=KEY)
        assert not (tmp_path / "other.tar.gz").exists()
        dry = state_tool.migrate_state(state, None, audit_key=KEY, dry_run=True)
        assert dry["resumed"] is True and len(dry["moves"]) == 3 - moved
    report = state_tool.migrate_state(state, backup, audit_key=KEY)
    assert report["resumed"] is (point != "before-the-journal")
    assert len(report["moves"]) == 3 - moved
    assert _tree(state) == _migrated(before, digests)
    if point != "before-the-journal":
        assert _sha256(backup) == taken, "a resumed migration rewrote its backup"
    assert agent_module.unmigrated_state(state) is None


def test_two_sessions_whose_digests_share_64_bits_are_reported_and_never_merged(
        tmp_path, monkeypatch):
    """The finding's own case, in layout-1 state: two sessions share one
    sandbox, and no tool can say whose each file is. The migration reports
    the directory with both sessions and moves nothing, not even the
    directories it could move; once the operator has taken the shared
    directory out of the state, it migrates the rest."""
    victim = _internal_session("tenant-a", "project")
    attacker = _internal_session("tenant-b", "chosen")
    chosen = _collide(monkeypatch, victim, attacker)
    state = tmp_path / "state"
    digests = _layout_one_state(state, sessions=(
        ("tenant-a", "project"), ("tenant-b", "chosen"), ("tenant-a", "notes")))
    shared = f"sandboxes/{chosen[:16]}"
    assert sorted(os.listdir(state / shared)) == [
        "nested", "tenant-a.project.txt", "tenant-b.chosen.txt"]
    before = _tree(state)

    report = state_tool.migrate_state(state, None, audit_key=KEY, dry_run=True)
    assert [{key: value for key, value in problem.items() if key != "reason"}
            for problem in report["problems"]] == [
        {"path": shared, "sessions": sorted([digests[("tenant-a", "project")], chosen])}]
    assert "never" in report["problems"][0]["reason"]
    assert report["moves"] == _moves({"notes": digests[("tenant-a", "notes")]})
    backup = tmp_path / "pre-migration.tar.gz"
    with pytest.raises(StateToolError, match=f"{shared}.*never merge"):
        state_tool.migrate_state(state, backup, audit_key=KEY)
    assert _tree(state) == before and not backup.exists(), (
        "a migration that found a shared sandbox changed the state")

    evidence = tmp_path / "evidence"
    (state / shared).rename(evidence)
    report = state_tool.migrate_state(state, backup, audit_key=KEY)
    assert report["moves"] == _moves({"notes": digests[("tenant-a", "notes")]})
    assert sorted(os.listdir(evidence)) == ["nested", "tenant-a.project.txt",
                                            "tenant-b.chosen.txt"]


def test_a_full_width_directory_that_already_exists_is_never_overwritten(tmp_path):
    state = tmp_path / "state"
    digests = _layout_one_state(state)
    digest = digests[("tenant-a", "project")]
    (state / "sandboxes" / digest).mkdir()
    (state / "sandboxes" / digest / "later.txt").write_text("written at full width")
    before = _tree(state)
    report = state_tool.migrate_state(state, None, audit_key=KEY, dry_run=True)
    assert [{key: value for key, value in problem.items() if key != "reason"}
            for problem in report["problems"]] == [
        {"path": f"sandboxes/{digest[:16]}", "target": f"sandboxes/{digest}"}]
    backup = tmp_path / "pre-migration.tar.gz"
    with pytest.raises(StateToolError, match=f"sandboxes/{digest[:16]}.*never merge"):
        state_tool.migrate_state(state, backup, audit_key=KEY)
    assert _tree(state) == before and not backup.exists()


def _break(state, tmp_path, how):
    """Damage a layout-1 state the way `how` names; returns the key to use."""
    if how == "no-clean-shutdown-marker":
        (state / CLEAN_MARKER).unlink()
    elif how == "corrupt-conversation":
        next((state / "sessions").glob("*.kv")).write_bytes(b"{not json")
    elif how == "audit-signed-with-another-key":
        return b"another-audit-key-that-is-long-enough-0123"
    elif how == "corrupt-quota-database":
        (state / "product-quotas.sqlite3").write_bytes(b"not a database" * 100)
    elif how == "unattributed-old-width-sandbox":
        (state / "sandboxes" / "0123456789abcdef").mkdir()
    elif how == "unreadable-journal":
        (state / JOURNAL).write_text("{not json")
    elif how == "journal-of-another-schema":
        (state / JOURNAL).write_text(json.dumps({
            "schema_version": 99, "migration": "sandbox-full-digest",
            "backup": str(tmp_path / "pre-migration.tar.gz"), "backup_sha256": "0" * 64}))
    elif how == "journal-whose-backup-changed":
        backup = tmp_path / "pre-migration.tar.gz"
        backup.write_bytes(b"a backup someone replaced")
        (state / JOURNAL).write_text(json.dumps({
            "schema_version": 1, "migration": "sandbox-full-digest",
            "from_layout": 1, "to_layout": 2, "backup": str(backup),
            "backup_sha256": "0" * 64, "started_unix": 1}))
    elif how == "journal-that-is-a-directory":
        (state / JOURNAL).mkdir()
    elif how == "journal-larger-than-any-journal":
        (state / JOURNAL).write_text(" " * (64 * 1024) + "{}")
    elif how == "journal-whose-backup-cannot-be-read":
        backup = tmp_path / "pre-migration.tar.gz"
        backup.write_bytes(b"unreadable")
        backup.chmod(0)
        (state / JOURNAL).write_text(json.dumps({
            "schema_version": 1, "migration": "sandbox-full-digest",
            "from_layout": 1, "to_layout": 2, "backup": str(backup),
            "backup_sha256": "0" * 64, "started_unix": 1}))
    elif how == "journal-whose-backup-is-gone":
        (state / JOURNAL).write_text(json.dumps({
            "schema_version": 1, "migration": "sandbox-full-digest",
            "from_layout": 1, "to_layout": 2,
            "backup": str(tmp_path / "pre-migration.tar.gz"),
            "backup_sha256": "0" * 64, "started_unix": 1}))
    return KEY


@pytest.mark.parametrize("how,refusal", [
    ("no-clean-shutdown-marker", "clean-shutdown marker"),
    ("corrupt-conversation", "conversation state is corrupt"),
    ("audit-signed-with-another-key", "audit verification failed"),
    ("corrupt-quota-database", "quota database"),
    ("unattributed-old-width-sandbox", "missing quota registry entries"),
    ("unreadable-journal", "state migration journal is invalid"),
    ("journal-of-another-schema", "state migration journal is invalid"),
    ("journal-that-is-a-directory", "state migration journal is invalid"),
    ("journal-larger-than-any-journal", "state migration journal is invalid"),
    ("journal-whose-backup-cannot-be-read", "pre-migration backup .* is missing or changed"),
    ("journal-whose-backup-changed", "pre-migration backup .* is missing or changed"),
    ("journal-whose-backup-is-gone", "pre-migration backup .* is missing or changed"),
])
def test_corrupt_input_is_refused_before_anything_changes(tmp_path, how, refusal):
    state = tmp_path / "state"
    _layout_one_state(state)
    key = _break(state, tmp_path, how)
    before, files = _tree(state), sorted(os.listdir(tmp_path))
    backup = tmp_path / "pre-migration.tar.gz"
    for dry_run in (True, False):
        with pytest.raises(StateToolError, match=refusal):
            state_tool.migrate_state(state, None if dry_run else backup, audit_key=key,
                                     dry_run=dry_run)
        assert _tree(state) == before and sorted(os.listdir(tmp_path)) == files


@pytest.mark.parametrize("arguments,refusal", [
    ({"state_dir": "missing"}, "state directory does not exist"),
    ({"audit_key": b""}, "an audit verification key is required"),
    ({"max_bytes": 0}, "max_bytes must be positive"),
    ({"backup": None}, "a migration needs --backup"),
])
def test_a_migration_refuses_arguments_it_cannot_run_with(tmp_path, arguments, refusal):
    state = tmp_path / "state"
    _layout_one_state(state)
    before = _tree(state)
    call = {"state_dir": state, "backup": tmp_path / "pre-migration.tar.gz",
            "audit_key": KEY, **arguments}
    if call["state_dir"] == "missing":
        call["state_dir"] = tmp_path / "missing"
    with pytest.raises(StateToolError, match=refusal):
        state_tool.migrate_state(call.pop("state_dir"), call.pop("backup"), **call)
    assert _tree(state) == before and sorted(os.listdir(tmp_path)) == ["state"]


def test_only_one_migration_runs_on_a_state_at_a_time(tmp_path):
    import fcntl
    state = tmp_path / "state"
    _layout_one_state(state)
    before = _tree(state)
    holder = os.open(state, os.O_RDONLY)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(StateToolError, match="another state migration is running"):
            state_tool.migrate_state(state, tmp_path / "pre-migration.tar.gz", audit_key=KEY)
    finally:
        os.close(holder)
    assert _tree(state) == before and not (tmp_path / "pre-migration.tar.gz").exists()


def test_a_directory_that_appears_at_a_target_mid_migration_is_never_overwritten(
        tmp_path, monkeypatch):
    """Nothing supported runs during a migration, but a name that appears
    between the plan and its rename is not overwritten either: the run
    stops with its journal, so the state stays refused until it is resolved."""
    state = tmp_path / "state"
    digests = _layout_one_state(state)
    digest = sorted(digests.values(), key=lambda d: d[:16])[0]
    real = state_tool.write_durably

    def journal_then_intruder(path, data):
        real(path, data)
        (state / "sandboxes" / digest).mkdir()
        (state / "sandboxes" / digest / "intruder.txt").write_text("arrived late")

    monkeypatch.setattr(state_tool, "write_durably", journal_then_intruder)
    with pytest.raises(StateToolError, match=f"sandboxes/{digest} appeared.*never merge"):
        state_tool.migrate_state(state, tmp_path / "pre-migration.tar.gz", audit_key=KEY)
    assert sorted(os.listdir(state / "sandboxes" / digest)) == ["intruder.txt"]
    assert (state / "sandboxes" / digest[:16]).is_dir()
    assert "state migration is incomplete" in agent_module.unmigrated_state(state)

def test_rollback_restores_the_pre_migration_backup_which_the_old_release_opens(tmp_path):
    """The rollback path docs/state-compatibility.md names: the old release
    never opens migrated state (its startup refuses a sandbox it cannot
    attribute); the pre-migration backup restores into a new path as the
    state the old release left, which it opens, and which this release
    would migrate again."""
    state = tmp_path / "state"
    digests = _layout_one_state(state)
    before, rows = _tree(state), _rows(state)
    assert _old_release_opens(state)
    backup = tmp_path / "pre-migration.tar.gz"
    state_tool.migrate_state(state, backup, audit_key=KEY)
    assert not _old_release_opens(state), "the old release would open migrated state"

    rolled_back = tmp_path / "rolled-back"
    restore_backup(backup, rolled_back, audit_key=KEY)
    without_marker = lambda tree: {k: v for k, v in tree.items() if k != CLEAN_MARKER}
    assert without_marker(_tree(rolled_back)) == without_marker(before)
    assert _rows(rolled_back) == rows
    assert _old_release_opens(rolled_back)
    assert "state needs migration" in agent_module.unmigrated_state(rolled_back)
    again = state_tool.migrate_state(rolled_back, tmp_path / "again.tar.gz", audit_key=KEY)
    assert again["moves"] == _moves(digests)


def _cli(*args, key=KEY):
    return subprocess.run(
        [sys.executable, str(PI_ROOT / "state_tool.py"), *args], capture_output=True,
        text=True, timeout=120, env={**os.environ, "PI_AUDIT_KEY": key.decode()})


def test_the_command_line_migrates_and_says_when_a_restored_state_needs_it(tmp_path):
    state = tmp_path / "state"
    digests = _layout_one_state(state)
    dry = _cli("migrate", "--state", str(state), "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert json.loads(dry.stdout)["moves"] == _moves(digests)
    no_backup = _cli("migrate", "--state", str(state))
    assert no_backup.returncode == 2 and "--backup" in no_backup.stderr

    backup = tmp_path / "pre-migration.tar.gz"
    done = _cli("migrate", "--state", str(state), "--backup", str(backup))
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["moves"] == _moves(digests)

    restored = _cli("restore", "--backup", str(backup), "--state", str(tmp_path / "restored"))
    assert restored.returncode == 0, restored.stderr
    assert "state_tool.py migrate" in restored.stderr, (
        "a restore of layout-1 state did not say that this release needs it migrated")

    digest = digests[("tenant-a", "project")]
    (tmp_path / "restored" / "sandboxes" / digest).mkdir()
    blocked = _cli("migrate", "--state", str(tmp_path / "restored"), "--dry-run")
    assert blocked.returncode == 2 and "1 problem" in blocked.stderr
    assert json.loads(blocked.stdout)["problems"][0]["target"] == f"sandboxes/{digest}"


# ── guards ──────────────────────────────────────────────────────────────────


_HEX = st.text("0123456789abcdef", min_size=64, max_size=64)


@settings(max_examples=200, deadline=None)
@given(registered=st.lists(_HEX, max_size=6, unique=True),
       twins=st.lists(st.tuples(st.integers(0, 5), _HEX), max_size=3),
       strays=st.lists(st.text("0123456789abcdef", min_size=16, max_size=16), max_size=2),
       data=st.data())
def test_the_plan_moves_each_directory_to_its_one_owner_and_reports_every_other(
        registered, twins, strays, data):
    """THE GUARD for the migration's plan. Whatever sessions share a 64-bit
    prefix, and whatever full-width names already exist: each old-width
    directory either moves to the one registered session it names, onto a
    name nothing holds, or is reported; never both, never two to one."""
    registered = list(registered)
    for index, tail in twins:  # a session whose digest shares another's 64 bits
        if index < len(registered):
            registered.append(registered[index][:16] + tail[16:])
    registered = sorted(set(registered))
    legacy = sorted({digest[:16] for digest in registered} | set(strays))
    present = set(data.draw(st.lists(st.sampled_from(registered), unique=True))
                  if registered else [])
    moves, problems = state_tool._plan_sandbox_moves(legacy, registered, present)
    planned = [move["from"] for move in moves] + [problem["path"] for problem in problems]
    assert sorted(planned) == [f"sandboxes/{name}" for name in legacy]
    targets = [move["to"] for move in moves]
    assert len(targets) == len(set(targets))
    for move in moves:
        owners = [d for d in registered if d.startswith(move["from"][len("sandboxes/"):])]
        assert owners == [move["to"][len("sandboxes/"):]]
        assert owners[0] not in present
    for problem in problems:
        name = problem["path"][len("sandboxes/"):]
        owners = [d for d in registered if d.startswith(name)]
        assert len(owners) != 1 or owners[0] in present, problem


# Truncating a digest is allowed only where the result names nothing a
# tenant owns. Each site, by module and function, with why.
TRUNCATED_DIGESTS = {
    ("agent.py", "hash_session"):
        "a 12-character tag that lets an operator correlate log lines; it names no "
        "file and separates nothing",
    ("state_tool.py", "_validate_quota_database"):
        "recognizes a sandbox of state layout 1 where layout 1 is an input (a restore, "
        "the migration); it names nothing, and everything else refuses such a sandbox",
    ("state_tool.py", "_plan_sandbox_moves"):
        "finds the registered sessions a layout-1 name could belong to, so that the "
        "migration moves a directory only to its one owner and reports a shared one",
}
GUARDED_MODULES = ("agent.py", "product_service.py", "product_main.py", "state_tool.py")


def _truncated_digests(source):
    """(innermost function, line) of each slice of a digest: of a
    hexdigest() or digest() result, or of a name that says it holds one."""
    found = []

    def visit(node, function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
            value = node.value
            if ((isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                 and value.func.attr in ("hexdigest", "digest"))
                    or (isinstance(value, ast.Name) and "digest" in value.id.lower())
                    or (isinstance(value, ast.Attribute)
                        and "digest" in value.attr.lower())):
                found.append((function, node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(ast.parse(source), "<module>")
    return found


def test_no_tenant_identifier_is_a_truncated_digest():
    """THE GUARD for F-23's class: a name derived from a digest is the whole
    digest. One that is not, in a module that names live state, must be
    listed above with the reason it isolates nothing."""
    unlisted = {}
    listed = set()
    for module in GUARDED_MODULES:
        for function, line in _truncated_digests((PI_ROOT / module).read_text()):
            if (module, function) in TRUNCATED_DIGESTS:
                listed.add((module, function))
            else:
                unlisted.setdefault(f"{module}:{function}", []).append(line)
    assert unlisted == {}, f"a digest is truncated outside the allowed sites: {unlisted}"
    assert listed == set(TRUNCATED_DIGESTS), "an allowed site no longer truncates: drop it"


def test_the_truncation_guard_fires_on_every_shape_it_forbids():
    shapes = [
        "def sandbox_for(self, session_id):\n"
        "    return self.root / hashlib.sha256(session_id.encode()).hexdigest()[:16]\n",
        "def surfaces(digest):\n    return f'sandboxes/{digest[:16]}'\n",
        "def short(entry):\n    return entry.digest[:16]\n",
        "def raw(data):\n    return hashlib.sha256(data).digest()[8:24]\n",
    ]
    for shape in shapes:
        assert len(_truncated_digests(shape)) == 1, shape
    assert _truncated_digests("def full(d):\n    return d.hexdigest()\n") == []
