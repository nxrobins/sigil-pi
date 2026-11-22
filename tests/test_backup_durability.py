"""Backups are durable before anything depends on them, and are streamed.

Found by the 2026-11-10 product audit, F-12:

- `backup-cycle` published a new archive, deleted the expired ones and synced
  only the deletions. The new archive's bytes and name were never synced, so a
  power loss could leave the old backups gone and the new one incomplete: the
  cycle could lose the only good copy. `backup` and `restore` synced nothing
  either; a restored tree could be installed under its final name with its
  files still in flight.
- `backup` read every state file into memory before writing the archive, up
  to the 10 GiB default limit, and `restore` read each member whole.

Pinned here, on the real `state_tool`:

- a cycle deletes an expired archive only once its new archive would survive a
  power loss: the bytes synced, then the name synced into the directory,
  whether the cycle created that archive or found it already there; and a
  sync that fails stops the cycle with every older archive in place;
- `backup` syncs the archive before publishing its name and the name after;
  `restore` syncs every file and directory of the staged tree before installing
  it and the install after; each syncs any directory it creates into its
  parent; neither publishes anything on the strength of a sync that failed;
- backup and restore hold a bounded amount of memory, however large the state;
- streaming changed nothing else: the archive is byte for byte the one the
  buffered writer produced, the size limit is still on the whole state,
  archives and restored trees are still private, and a file that changes
  while the archive is written fails the backup instead of corrupting it;
- a sandbox file is backed up as a tenant's bytes whatever it is called
  (found while fixing the above: one named like the quota ledger was opened
  as the ledger).
"""
import ast
import errno
import gzip
import hashlib
import hmac
import io
import json
import os
import random
import shutil
import sqlite3
import stat
import string
import sys
import tarfile
import tempfile
import tracemalloc
from contextlib import closing
from pathlib import Path

import pytest
from hypothesis import example, given, settings, strategies as st

from conftest import PI_ROOT

from agent import AuditLog, SessionStore
from product_service import DurableQuotaStore, Principal, ProductScheduleStore, _internal_session
import state_tool
from state_tool import (
    CLEAN_MARKER,
    StateToolError,
    create_backup,
    restore_backup,
    run_managed_backup_cycle,
)

AUDIT_KEY = b"backup-durability-test-key-0123456789abcdef"
MiB = 1024 * 1024


def quota_store(path):
    return DurableQuotaStore(
        path, max_concurrent_turns=10,
        tokens_per_day=1000, token_reservation_per_turn=10,
        storage_bytes_per_tenant=1000, storage_reservation_per_turn=10,
        audit_bytes_per_tenant=1000, audit_reservation_per_turn=10,
        clock=lambda: 100.0)


def write_content(path, content):
    """`content` is bytes, or a size to fill with a repeating pattern written
    a block at a time, so building a large state holds no large buffer."""
    if isinstance(content, bytes):
        path.write_bytes(content)
        return
    block = hashlib.sha256(path.name.encode()).digest() * (MiB // 32)
    with path.open("wb") as stream:
        for _ in range(content // MiB):
            stream.write(block)
        stream.write(block[:content % MiB])


def build_state(root, *, sessions=("project",), files=(), schedules=True):
    """A cleanly drained product state for tenant-a: per session a
    conversation, a sandbox holding data.bin, a signed audit record and a
    quota registry row; a schedule store; the clean-shutdown marker. `files`
    adds (session index, path parts inside its sandbox, content) entries; one
    whose name collides with another on a case-insensitive filesystem is
    skipped."""
    state = root / "state"
    store = SessionStore(state / "sessions")
    audit = AuditLog(state / "audit", key=AUDIT_KEY)
    quota = quota_store(state / "product-quotas.sqlite3")
    sandboxes = []
    for name in sessions:
        internal = _internal_session("tenant-a", name)
        store.save(internal, [{"role": "user", "content": f"committed in {name}"}])
        sandbox = state / "sandboxes" / hashlib.sha256(internal.encode()).hexdigest()
        sandbox.mkdir(parents=True)
        (sandbox / "data.bin").write_bytes(b"sandbox-data")
        audit.record(internal, "tool", "source", "input", "output", None, None, 1)
        quota.acquire_turn("tenant-a").release(
            usage={"input_tokens": 2, "output_tokens": 3},
            session_id=internal, storage_bytes=100, audit_bytes=50)
        sandboxes.append(sandbox)
    for index, parts, content in files:
        path = sandboxes[index % len(sandboxes)].joinpath(*parts)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                continue
            write_content(path, content)
        except (FileExistsError, NotADirectoryError, IsADirectoryError):
            continue
    if schedules:
        # A schedule as the product stores it: rows of the ledger, and the
        # schema-2 marker in product-schedules.json (audit F-27).
        ProductScheduleStore(state / "product-schedules.json", clock=lambda: 100.0).put(
            Principal("planner", "tenant-a", frozenset({"schedules:write", "chat"}),
                      frozenset()),
            name="daily", session="work", message="summarise the day", every_ms=60_000)
    (state / CLEAN_MARKER).write_text(json.dumps({
        "schema_version": 1, "product_version": "1.0.0", "clean_shutdown_unix": 100}))
    return state


def make_state(root, sandbox_files=()):
    """One session, plus (name, size) pattern files in its sandbox."""
    return build_state(root, files=[(0, (name,), size) for name, size in sandbox_files])


def identity(path):
    info = os.lstat(path)
    return info.st_dev, info.st_ino


class Syscalls:
    """Records, in order, the calls that decide what survives a power loss:
    every fsync (directory or file, its identity, its size at the time), every
    call that gives a file a name (link, rename, replace) and every unlink.
    Each call still happens; nothing is faked. With `fail_fsync=n`, the n-th
    fsync (from 0) does not happen: it fails as a dying disk fails it, with
    EIO, and is recorded as "fsync-failed", which counts as no sync at all."""

    def __init__(self, monkeypatch, fail_fsync=None):
        self.events = []
        real = {name: getattr(os, name)
                for name in ("fsync", "link", "replace", "rename", "unlink")}

        def fsync(fd):
            info = os.fstat(fd)
            kind = "dir" if stat.S_ISDIR(info.st_mode) else "file"
            asked = sum(1 for e in self.events if e[0] in ("fsync", "fsync-failed"))
            if asked == fail_fsync:
                self.events.append(("fsync-failed", kind, (info.st_dev, info.st_ino), info.st_size))
                raise OSError(errno.EIO, "injected: the device failed this sync")
            real["fsync"](fd)
            self.events.append(("fsync", kind, (info.st_dev, info.st_ino), info.st_size))

        def namer(name):
            def call(src, dst, *args, **kwargs):
                real[name](src, dst, *args, **kwargs)
                self.events.append((name, Path(os.fsdecode(src)), Path(os.fsdecode(dst))))
            return call

        def unlink(path, *args, **kwargs):
            real["unlink"](path, *args, **kwargs)
            self.events.append(("unlink", Path(os.fsdecode(path))))

        monkeypatch.setattr(os, "fsync", fsync)
        for name in ("link", "replace", "rename"):
            monkeypatch.setattr(os, name, namer(name))
        monkeypatch.setattr(os, "unlink", unlink)

    def index(self, event):
        return self.events.index(event)


def not_durable(events, path, upto):
    """Why `path`, as it is now, would not survive a power loss right before
    events[upto]; None when it would. Durable means: its bytes synced (an
    fsync of its inode at its current size) before the name was given to it,
    and its directory synced after the last call that gave it its name. With
    no such call in `events`, the name predates them, and any sync of the
    directory before `upto` makes it durable."""
    window = events[:upto]
    inode, size = identity(path), path.stat().st_size
    named = [i for i, e in enumerate(window)
             if e[0] in ("link", "rename", "replace") and e[2] == path]
    data_by = named[-1] if named else upto
    if not any(e[:4] == ("fsync", "file", inode, size) for e in window[:data_by]):
        return (f"{path.name}'s bytes were not synced before "
                + ("its name was published" if named else "this point"))
    start = named[-1] + 1 if named else 0
    if not any(e[:3] == ("fsync", "dir", identity(path.parent)) for e in window[start:]):
        return f"{path.name}'s name was not synced into its directory"
    return None


# ── the managed cycle: durable before it deletes ─────────────────────────


def test_an_expired_archive_is_deleted_only_once_the_new_one_is_durable(
        tmp_path, monkeypatch):
    state = make_state(tmp_path)
    backups = tmp_path.resolve() / "managed"
    first = run_managed_backup_cycle(
        state, backups, audit_key=AUDIT_KEY, retention_s=100, now=1000)
    expired = backups / first["created"]["name"]
    calls = Syscalls(monkeypatch)
    second = run_managed_backup_cycle(
        state, backups, audit_key=AUDIT_KEY, retention_s=100, now=1200)
    assert second["created"]["created_new_file"] is True
    assert [item["name"] for item in second["deleted"]] == [expired.name]
    final = backups / second["created"]["name"]
    deletion = calls.index(("unlink", expired))
    reason = not_durable(calls.events, final, deletion)
    assert reason is None, (
        f"the cycle deleted the expired archive {expired.name} while {reason}: a "
        f"power loss at that moment leaves the old backups gone and the new one "
        f"incomplete, so the cycle can lose the only good copy (audit F-12)")
    assert ("fsync", "dir", identity(backups)) in [e[:3] for e in calls.events[deletion + 1:]], (
        "the deletion itself is no longer synced, as it was before F-12 was fixed")


def test_an_archive_the_cycle_finds_already_there_is_synced_before_it_deletes(
        tmp_path, monkeypatch):
    """A second cycle in the same second finds its archive already published
    (same state, same time, same digest) and creates nothing. It must still
    not delete anything on the strength of an archive it has not synced."""
    state = make_state(tmp_path)
    backups = tmp_path.resolve() / "managed"
    first = run_managed_backup_cycle(
        state, backups, audit_key=AUDIT_KEY, retention_s=10_000, now=1000)
    second = run_managed_backup_cycle(
        state, backups, audit_key=AUDIT_KEY, retention_s=10_000, now=1200)
    assert second["deleted"] == []
    expired = backups / first["created"]["name"]
    calls = Syscalls(monkeypatch)
    third = run_managed_backup_cycle(
        state, backups, audit_key=AUDIT_KEY, retention_s=100, now=1200)
    assert third["created"]["created_new_file"] is False
    assert third["created"]["name"] == second["created"]["name"]
    assert [item["name"] for item in third["deleted"]] == [expired.name]
    final = backups / third["created"]["name"]
    reason = not_durable(calls.events, final, calls.index(("unlink", expired)))
    assert reason is None, (
        f"the cycle deleted the expired archive {expired.name} while {reason}: an "
        f"archive found already published is only as durable as the cycle that "
        f"wrote it (audit F-12)")


def test_a_cycle_returns_only_once_its_archive_and_new_directories_are_durable(
        tmp_path, monkeypatch):
    """The first cycle into a new backup directory deletes nothing, but it
    reports success: the archive it names, and the directories that hold it,
    must survive a power loss from then on."""
    state = make_state(tmp_path)
    backups = tmp_path.resolve() / "fresh" / "managed"
    calls = Syscalls(monkeypatch)
    result = run_managed_backup_cycle(
        state, backups, audit_key=AUDIT_KEY, retention_s=100, now=1000)
    final = backups / result["created"]["name"]
    reason = not_durable(calls.events, final, len(calls.events))
    assert reason is None, f"the cycle returned while {reason} (audit F-12)"
    synced = {e[2] for e in calls.events if e[:2] == ("fsync", "dir")}
    missing = [p.name for p in (tmp_path, tmp_path / "fresh") if identity(p) not in synced]
    assert missing == [], (
        f"the cycle created {backups.relative_to(tmp_path)} but never synced "
        f"{missing}, so a power loss can take the new directories and the archive "
        f"in them with it")


def one_expiring_archive(root):
    """A state, and a managed directory holding one archive made at now=1000
    that a cycle at now=1200 with retention 100 will expire."""
    state = make_state(root)
    backups = root.resolve() / "managed"
    first = run_managed_backup_cycle(
        state, backups, audit_key=AUDIT_KEY, retention_s=100, now=1000)
    return state, backups, backups / first["created"]["name"]


def test_a_sync_that_fails_stops_the_cycle_before_it_deletes(tmp_path):
    """Durable means the sync succeeded. If the disk refuses any one of the
    syncs the deletion stands on, the cycle stops there with that error and
    every older archive still in place; a cycle that carried on would delete
    on the strength of bytes that never reached the disk."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        state, backups, expired = one_expiring_archive(tmp_path / "clean")
        calls = Syscalls(monkeypatch)
        run_managed_backup_cycle(
            state, backups, audit_key=AUDIT_KEY, retention_s=100, now=1200)
    before = calls.events[:calls.index(("unlink", expired))]
    syncs = sum(1 for e in before if e[0] == "fsync")
    assert syncs >= 2, (
        f"the cycle made {syncs} sync(s) before it deleted the expired archive: "
        f"it deletes on the strength of an archive it never synced (audit F-12)")
    for failing in range(syncs):
        with pytest.MonkeyPatch.context() as monkeypatch:
            state, backups, expired = one_expiring_archive(tmp_path / f"fail-{failing}")
            Syscalls(monkeypatch, fail_fsync=failing)
            with pytest.raises(OSError) as error:
                run_managed_backup_cycle(
                    state, backups, audit_key=AUDIT_KEY, retention_s=100, now=1200)
        assert error.value.errno == errno.EIO, f"sync {failing}: {error.value!r}"
        assert expired.is_file(), (
            f"sync {failing} of {syncs} failed, and the cycle deleted the expired "
            f"archive all the same")
        stray = sorted(p.name for p in backups.iterdir()
                       if not MANAGED_NAME.fullmatch(p.name)
                       and p.name != state_tool.BACKUP_LOCK_NAME)
        assert stray == [], f"sync {failing}: the failed cycle left {stray} behind"


def test_the_command_reports_a_failed_sync_as_a_failed_operation(
        tmp_path, monkeypatch, capsys):
    """What the deployment scheduler sees: exit status 2 and the one-line
    error every other failed state operation gives, with nothing deleted."""
    state, backups, expired = one_expiring_archive(tmp_path)
    monkeypatch.setenv("PI_AUDIT_KEY", AUDIT_KEY.decode())
    monkeypatch.setattr(sys, "argv", [
        "state_tool.py", "backup-cycle", "--state", str(state),
        "--backup-dir", str(backups), "--retention-days", "1"])
    Syscalls(monkeypatch, fail_fsync=0)
    with pytest.raises(SystemExit) as error:
        state_tool.main()
    captured = capsys.readouterr()
    assert error.value.code == 2 and captured.out == ""
    assert captured.err.startswith("state operation failed: ")
    assert captured.err.count("\n") == 1 and "Traceback" not in captured.err
    assert expired.is_file(), (
        "the first sync the cycle asked for failed, after it had already "
        "deleted the expired archive (audit F-12)")


def test_a_link_under_the_cycles_own_archive_name_is_refused_as_it_always_was(tmp_path):
    """The cycle now syncs the archive it stands on before it validates the
    managed set. When that name is a link someone planted to an identical
    archive elsewhere, the cycle must still end as it did before: with the
    validation's refusal, and nothing deleted."""
    state = make_state(tmp_path)
    twin = run_managed_backup_cycle(
        state, tmp_path / "twin", audit_key=AUDIT_KEY, retention_s=100, now=1200)
    outside = tmp_path / "outside.tar.gz"
    shutil.copyfile(tmp_path / "twin" / twin["created"]["name"], outside)
    backups = tmp_path / "managed"
    first = run_managed_backup_cycle(
        state, backups, audit_key=AUDIT_KEY, retention_s=100, now=1000)
    (backups / twin["created"]["name"]).symlink_to(outside)
    with pytest.raises(StateToolError, match="is not one private regular file"):
        run_managed_backup_cycle(
            state, backups, audit_key=AUDIT_KEY, retention_s=100, now=1200)
    assert (backups / first["created"]["name"]).is_file()
    assert (backups / twin["created"]["name"]).is_symlink()
    assert hashlib.sha256(outside.read_bytes()).hexdigest() == twin["created"]["sha256"]


# ── backup and restore: synced before they are published ────────────────


def test_a_backup_is_synced_before_its_name_is_published_and_the_name_after(
        tmp_path, monkeypatch):
    state = make_state(tmp_path)
    output = tmp_path / "new" / "nested" / "state.tar.gz"
    calls = Syscalls(monkeypatch)
    create_backup(state, output, audit_key=AUDIT_KEY, now=200)
    reason = not_durable(calls.events, output, len(calls.events))
    assert reason is None, (
        f"`state_tool.py backup` returned while {reason}: the archive it reports "
        f"can be missing or incomplete after a power loss (audit F-12)")
    synced = {e[2] for e in calls.events if e[:2] == ("fsync", "dir")}
    assert {identity(tmp_path), identity(tmp_path / "new")} <= synced, (
        "the backup created new/nested/ for its output but did not sync the new "
        "directories into their parents")


def test_a_restore_syncs_the_tree_before_installing_it_and_the_install_after(
        tmp_path, monkeypatch):
    state = make_state(tmp_path, [("nested-a.bin", 4096)])
    backup = tmp_path / "state.tar.gz"
    manifest = create_backup(state, backup, audit_key=AUDIT_KEY, now=200)
    target = tmp_path.resolve() / "restore-parent" / "restored"
    calls = Syscalls(monkeypatch)
    restore_backup(backup, target, audit_key=AUDIT_KEY)
    installs = [i for i, e in enumerate(calls.events)
                if e[0] in ("rename", "replace") and e[2] == target]
    assert len(installs) == 1, f"expected one install of the tree, saw {installs}"
    at = installs[0]
    before = calls.events[:at]
    # The files restore writes: every declared file and the restored marker.
    # Validating the ledger read-only also leaves SQLite's own empty -wal and
    # its -shm index beside it; they hold no state, and SQLite rebuilds both.
    written = {entry["path"] for entry in manifest["files"]} | {CLEAN_MARKER}
    present = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
    assert present - written <= {"product-quotas.sqlite3-wal", "product-quotas.sqlite3-shm"}
    assert written <= present and len(written) > 5
    unsynced_files = sorted(
        name for name in written
        if ("fsync", "file", identity(target / name), (target / name).stat().st_size)
        not in [e[:4] for e in before])
    assert unsynced_files == [], (
        f"restore installed the tree with {len(unsynced_files)} file(s) never "
        f"synced, e.g. {unsynced_files[:3]}: after a power loss the restored "
        f"state can hold empty or partial files under its final name (audit F-12)")
    unsynced_dirs = sorted(
        str(p.relative_to(target)) for p in [target, *target.rglob("*")] if p.is_dir()
        and ("fsync", "dir", identity(p)) not in [e[:3] for e in before])
    assert unsynced_dirs == [], (
        f"restore installed the tree with directories whose entries were never "
        f"synced: {unsynced_dirs}")
    assert ("fsync", "dir", identity(target.parent)) in [e[:3] for e in calls.events[at + 1:]], (
        "restore never synced the directory it installed the tree into, so the "
        "install itself can be lost")
    assert identity(tmp_path) in {e[2] for e in calls.events if e[:2] == ("fsync", "dir")}, (
        "restore created restore-parent/ but did not sync it into its parent")


def test_a_backup_that_cannot_be_synced_is_never_published(tmp_path, monkeypatch):
    state = make_state(tmp_path)
    output = tmp_path / "out" / "state.tar.gz"
    output.parent.mkdir()
    calls = Syscalls(monkeypatch, fail_fsync=0)
    failure = None
    try:
        create_backup(state, output, audit_key=AUDIT_KEY, now=200)
    except OSError as error:
        failure = error
    assert failure is not None and failure.errno == errno.EIO, (
        f"the backup returned normally ({failure!r}) although the first sync it "
        f"asked for failed, or it asked for none (audit F-12)")
    assert [e[:2] for e in calls.events if e[0].startswith("fsync")] == [
        ("fsync-failed", "file")], "anti-vacuity: the sync that failed was the archive's"
    assert list(output.parent.iterdir()) == [], (
        "a backup whose bytes could not be synced was published, or left its "
        "scratch file behind")


def test_a_restore_that_cannot_sync_its_tree_installs_nothing(tmp_path):
    """Whichever sync before the install the disk refuses, restore fails with
    that error, the target does not exist and the staged tree is gone."""
    state = make_state(tmp_path)
    backup = tmp_path / "state.tar.gz"
    create_backup(state, backup, audit_key=AUDIT_KEY, now=200)
    root = tmp_path.resolve()
    with pytest.MonkeyPatch.context() as monkeypatch:
        calls = Syscalls(monkeypatch)
        restore_backup(backup, root / "clean" / "restored", audit_key=AUDIT_KEY)
    install = next(i for i, e in enumerate(calls.events)
                   if e[0] in ("rename", "replace") and e[2] == root / "clean" / "restored")
    syncs = sum(1 for e in calls.events[:install] if e[0] == "fsync")
    assert syncs >= 2, (
        f"restore made {syncs} sync(s) before installing the tree (audit F-12)")
    for failing in range(syncs):
        parent = root / f"fail-{failing}"
        with pytest.MonkeyPatch.context() as monkeypatch:
            Syscalls(monkeypatch, fail_fsync=failing)
            with pytest.raises(OSError) as error:
                restore_backup(backup, parent / "restored", audit_key=AUDIT_KEY)
        assert error.value.errno == errno.EIO, f"sync {failing}: {error.value!r}"
        left = sorted(p.name for p in parent.iterdir())
        assert left == [], (
            f"sync {failing} of {syncs} failed, and restore left {left} in the "
            f"target's directory")


# ── memory: streamed, not buffered ───────────────────────────────────────

# Six sandbox files: 48 MiB of state, the largest 24 MiB. Holding the state, or
# any one large file, shows up far above this bound; streaming needs a few
# copy buffers and the compressor's own state.
LARGE_STATE = [("large.bin", 24 * MiB), *[(f"part-{n}.bin", 6 * MiB) for n in range(4)]]
MEMORY_BOUND = 8 * MiB


def traced_peak(function):
    """The most Python memory `function` held at once, above what was already
    allocated when it started. Tracing is left as it was found, so a run under
    PYTHONTRACEMALLOC neither inflates the figure nor loses its tracing."""
    already_tracing = tracemalloc.is_tracing()
    if not already_tracing:
        tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        before = tracemalloc.get_traced_memory()[0]
        function()
        return tracemalloc.get_traced_memory()[1] - before
    finally:
        if not already_tracing:
            tracemalloc.stop()


@pytest.fixture(scope="module")
def large_backup(tmp_path_factory):
    root = tmp_path_factory.mktemp("large")
    state = make_state(root, LARGE_STATE)
    total = sum(p.stat().st_size for p in state.rglob("*") if p.is_file())
    assert total > 48 * MiB  # anti-vacuity: the state is as large as claimed
    backup = root / "large.tar.gz"
    peak = traced_peak(lambda: create_backup(state, backup, audit_key=AUDIT_KEY, now=200))
    return root, state, backup, peak


def test_a_backup_never_holds_the_state_in_memory(large_backup):
    _, _, backup, peak = large_backup
    assert backup.is_file()
    assert peak < MEMORY_BOUND, (
        f"backing up 48 MiB of state peaked at {peak / MiB:.1f} MiB of Python memory "
        f"(bound {MEMORY_BOUND // MiB} MiB): the archive is built from buffered "
        f"copies of the state, which the 10 GiB default limit lets reach 10 GiB "
        f"(audit F-12)")


def test_a_restore_never_holds_a_whole_member_in_memory(large_backup):
    root, state, backup, _ = large_backup
    target = root / "restored"
    peak = traced_peak(lambda: restore_backup(backup, target, audit_key=AUDIT_KEY))
    large = next((target / "sandboxes").glob("*/large.bin"))
    assert large.stat().st_size == 24 * MiB
    assert peak < MEMORY_BOUND, (
        f"restoring a backup whose largest member is 24 MiB peaked at "
        f"{peak / MiB:.1f} MiB of Python memory (bound {MEMORY_BOUND // MiB} MiB): "
        f"each member is read whole before it is written")


# ── what streaming must leave as it was ─────────────────────────────────


def test_the_size_limit_still_bounds_the_whole_state_not_each_file(tmp_path):
    """The limit is on the sum. The streamed writer counts as it reads, one
    file at a time; a limit applied to each file alone would let a state of
    many files through at any size."""
    state = make_state(tmp_path, [("a.bin", 3000), ("b.bin", 3000), ("c.bin", 3000)])
    manifest = create_backup(state, tmp_path / "all.tar.gz", audit_key=AUDIT_KEY, now=200)
    sizes = [entry["size"] for entry in manifest["files"]]
    total = sum(sizes)
    assert len(sizes) > 3 and max(sizes) < total - 1, "anti-vacuity: no one file is the sum"
    with pytest.raises(StateToolError, match="state exceeds the configured backup size limit"):
        create_backup(state, tmp_path / "over.tar.gz", audit_key=AUDIT_KEY, now=200,
                      max_bytes=total - 1)
    assert not (tmp_path / "over.tar.gz").exists()
    exact = create_backup(state, tmp_path / "exact.tar.gz", audit_key=AUDIT_KEY, now=200,
                          max_bytes=total)
    assert exact == manifest


def test_archives_and_restored_trees_are_private_whatever_the_umask(tmp_path):
    """The archive is 0600; every restored file is 0600 and every restored
    directory 0700, also under a umask that would allow more."""
    state = make_state(tmp_path, [("nested-a.bin", 10)])
    backup = tmp_path / "state.tar.gz"
    restored = tmp_path / "restored"
    previous = os.umask(0)
    try:
        create_backup(state, backup, audit_key=AUDIT_KEY, now=200)
        restore_backup(backup, restored, audit_key=AUDIT_KEY)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(backup.lstat().st_mode) == 0o600
    modes = {p.relative_to(tmp_path).as_posix(): stat.S_IMODE(p.lstat().st_mode)
             for p in [restored, *restored.rglob("*")]}
    assert len(modes) > 8
    wrong = {name: oct(mode) for name, mode in modes.items()
             if mode != (0o700 if (tmp_path / name).is_dir() else 0o600)}
    assert wrong == {}, f"restored with modes other than 0700/0600: {wrong}"


def test_a_restore_that_fails_a_checksum_leaves_nothing_behind(tmp_path):
    """Restore writes a member as it reads it and checks the digest after,
    where it used to hold the member in memory until checked. Bytes that fail
    the check must not outlive the failure."""
    state = make_state(tmp_path)
    original = tmp_path / "original.tar.gz"
    create_backup(state, original, audit_key=AUDIT_KEY, now=200)
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(original, "r:gz") as source, tarfile.open(tampered, "w:gz") as target:
        for member in source.getmembers():
            data = source.extractfile(member).read()
            if member.name.startswith("data/sandboxes/"):
                data = bytes(reversed(data))  # the declared size, other bytes
            target.addfile(member, io.BytesIO(data))
    parent = tmp_path / "parent"
    with pytest.raises(StateToolError, match="backup checksum failed: sandboxes/"):
        restore_backup(tampered, parent / "restored", audit_key=AUDIT_KEY)
    assert list(parent.iterdir()) == [], "a restore that failed its checksum left files behind"


def test_a_file_that_appears_at_the_output_while_the_archive_is_written_is_kept(
        tmp_path, monkeypatch):
    """The archive is published by a link, which cannot replace a file. One
    that appears at the output after the first check, while the archive is
    being written, is left as it is; the backup fails and removes its
    scratch file."""
    state = make_state(tmp_path)
    output = tmp_path / "out" / "state.tar.gz"
    real_addfile = tarfile.TarFile.addfile

    def addfile(archive, tarinfo, fileobj=None, *args, **kwargs):
        if tarinfo.name == state_tool.MANIFEST_NAME:
            output.write_bytes(b"someone else's file")
        return real_addfile(archive, tarinfo, fileobj, *args, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, "addfile", addfile)
    with pytest.raises(StateToolError, match="backup output already exists; refusing to overwrite"):
        create_backup(state, output, audit_key=AUDIT_KEY, now=200)
    assert output.read_bytes() == b"someone else's file"
    assert [p.name for p in output.parent.iterdir()] == [output.name], (
        "the failed backup left its scratch file behind")


def test_the_cycle_refuses_a_different_archive_under_its_own_name(tmp_path):
    """Two archives, one managed name: the cycle keeps the one that was
    there, publishes nothing and deletes nothing."""
    state, backups, expired = one_expiring_archive(tmp_path)
    twin = run_managed_backup_cycle(
        state, tmp_path / "twin", audit_key=AUDIT_KEY, retention_s=100, now=1200)
    squatter = backups / twin["created"]["name"]
    squatter.write_bytes(b"not that archive")
    squatter.chmod(0o600)
    with pytest.raises(StateToolError, match="managed backup name collision"):
        run_managed_backup_cycle(
            state, backups, audit_key=AUDIT_KEY, retention_s=100, now=1200)
    assert squatter.read_bytes() == b"not that archive"
    assert sorted(p.name for p in backups.iterdir()) == sorted(
        [state_tool.BACKUP_LOCK_NAME, expired.name, squatter.name])


def test_the_arguments_that_were_refused_still_are(tmp_path):
    state = make_state(tmp_path)
    with pytest.raises(StateToolError, match="managed backup time must be non-negative"):
        run_managed_backup_cycle(
            state, tmp_path / "managed", audit_key=AUDIT_KEY, retention_s=100, now=-1)
    backup = tmp_path / "state.tar.gz"
    create_backup(state, backup, audit_key=AUDIT_KEY, now=200)
    with pytest.raises(StateToolError, match="max_bytes must be positive"):
        restore_backup(backup, tmp_path / "restored", audit_key=AUDIT_KEY, max_bytes=0)
    with pytest.raises(StateToolError, match="an audit verification key is required"):
        restore_backup(backup, tmp_path / "restored", audit_key=b"")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state", "state.tar.gz"], (
        "a refused call created something")


# ── a sandbox file is a tenant's bytes, whatever it is called ───────────


def test_a_sandbox_file_named_like_the_quota_database_is_backed_up_as_bytes(tmp_path):
    """Only the ledger at the state root is a SQLite database to snapshot. A
    tool can give a sandbox file any name; one named product-quotas.sqlite3
    was opened as the ledger, so a single tenant could fail every backup."""
    state = make_state(tmp_path)
    sandbox = next((state / "sandboxes").iterdir())
    planted = sandbox / "product-quotas.sqlite3"
    planted.write_bytes(b"a tenant's own file, not a database")
    backup = tmp_path / "state.tar.gz"
    manifest = create_backup(state, backup, audit_key=AUDIT_KEY, now=200)
    relative = planted.relative_to(state).as_posix()
    entry = next(e for e in manifest["files"] if e["path"] == relative)
    assert entry["sha256"] == hashlib.sha256(planted.read_bytes()).hexdigest()
    restored = tmp_path / "restored"
    restore_backup(backup, restored, audit_key=AUDIT_KEY)
    assert (restored / relative).read_bytes() == planted.read_bytes()


def test_the_root_ledger_is_captured_with_the_rows_still_in_its_wal(tmp_path):
    """The other side of the test above: the ledger at the root is still
    captured through SQLite's backup API, not copied as a file, so committed
    rows that are still in its write-ahead log reach the archive."""
    state = make_state(tmp_path)
    ledger = state / "product-quotas.sqlite3"
    holder = sqlite3.connect(ledger)  # an open reader keeps the WAL from being folded in
    try:
        holder.execute("SELECT COUNT(*) FROM session_usage").fetchall()
        quota_store(ledger).acquire_turn("tenant-a").release(
            usage={"input_tokens": 7, "output_tokens": 0},
            session_id=_internal_session("tenant-a", "project"),
            storage_bytes=100, audit_bytes=50)
        assert (state / "product-quotas.sqlite3-wal").stat().st_size > 0
        (tmp_path / "raw-copy.sqlite3").write_bytes(ledger.read_bytes())
        with closing(sqlite3.connect(tmp_path / "raw-copy.sqlite3")) as raw:
            raw_tokens = raw.execute("SELECT SUM(tokens) FROM token_windows").fetchone()[0]
        assert raw_tokens == 5, "anti-vacuity: the main file alone lacks the new rows"
        backup = tmp_path / "state.tar.gz"
        create_backup(state, backup, audit_key=AUDIT_KEY, now=200)
    finally:
        holder.close()
    restored = tmp_path / "restored"
    restore_backup(backup, restored, audit_key=AUDIT_KEY)
    with closing(sqlite3.connect(restored / "product-quotas.sqlite3")) as db:
        assert db.execute("SELECT SUM(tokens) FROM token_windows").fetchone()[0] == 12


# ── guards: the format is unchanged, a changing file is caught, and the ──
# ── power-loss order holds for any sequence of cycles ───────────────────


def buffered_reference_archive(state_dir, *, audit_key, now):
    """The manifest and the archive bytes that the buffered `create_backup`
    (a004862, and every release before it) wrote for this state: every file
    read into memory first, then the manifest and the files. Streaming must
    not change one byte: older releases restore these archives, and changing
    the format is decision D5's. The one change D5 (a) allowed since (audit
    F-12, node B2) is the manifest's authentication tag, a pax header on the
    manifest member that older releases ignore; it is computed here as
    tests/test_backup_authentication.py specifies it. Only the ledger at the
    root is a database snapshot here, as in the fixed code (the buffered one
    also snapshotted any sandbox file of that name, the defect pinned
    above)."""
    state_dir = Path(state_dir).resolve()
    state_files = state_tool._regular_files(state_dir)
    marker, audit_report = state_tool._validate_state(state_dir, audit_key)
    entries, payloads = [], []
    for path in state_files:
        relative = path.relative_to(state_dir).as_posix()
        if relative == "product-quotas.sqlite3":
            with tempfile.TemporaryDirectory() as temp:
                snapshot = Path(temp) / "quota.sqlite3"
                with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as source:
                    with closing(sqlite3.connect(snapshot)) as destination:
                        source.backup(destination)
                data = snapshot.read_bytes()
        else:
            data = path.read_bytes()
        entries.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest(),
                        "size": len(data)})
        payloads.append((f"data/{relative}", data))
    manifest = {
        "schema_version": state_tool.SCHEMA_VERSION,
        "product_version": marker.get("product_version"),
        "created_unix": int(now),
        "clean_shutdown_unix": marker.get("clean_shutdown_unix"),
        "audit_chains": audit_report["chains"],
        "audit_records": audit_report["records"],
        "files": entries,
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    key = hmac.new(audit_key, b"sigil-pi backup manifest v1", hashlib.sha256).digest()
    tag = "v1:" + hmac.new(key, manifest_bytes, hashlib.sha256).hexdigest()
    raw = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name, data in [(state_tool.MANIFEST_NAME, manifest_bytes), *payloads]:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o600
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                info.mtime = 0
                if name == state_tool.MANIFEST_NAME:
                    info.pax_headers = {"SIGILPI.manifest_tag": tag}
                archive.addfile(info, io.BytesIO(data))
    return manifest, raw.getvalue()


# Sizes either side of a tar block (512), the tar copy buffer (16 KiB) and the
# streaming chunk (1 MiB); names short, over the 100-byte ustar limit (a pax
# header), and non-ASCII.
SIZES = (0, 1, 511, 512, 513, 16 * 1024 - 1, 16 * 1024, 16 * 1024 + 1, 64 * 1024 + 7, MiB + 3)
NAME = st.builds(
    lambda stem, tail: stem + tail,
    st.text(string.ascii_letters + string.digits + "-_.", min_size=1, max_size=110),
    st.sampled_from(["", "é", "日本", "ü-ß"]),
).filter(lambda name: name not in (".", ".."))
CONTENT = st.builds(
    lambda size, seed, noisy: random.Random(seed).randbytes(size) if noisy
    else (bytes(range(256)) * (size // 256 + 1))[:size],
    st.sampled_from(SIZES), st.integers(0, 2**32), st.booleans())
SANDBOX_FILE = st.tuples(
    st.integers(0, 1),
    st.builds(lambda dirs, name: (*dirs, name), st.lists(NAME, max_size=2), NAME),
    CONTENT)


@settings(max_examples=25, deadline=None)
@given(files=st.lists(SANDBOX_FILE, max_size=6), sessions=st.integers(1, 2),
       schedules=st.booleans())
@example(files=[(0, ("x" * 101 + "日本",), MiB + 3), (1, ("deep", "er", "z"), b"")],
         sessions=2, schedules=False)
def test_the_streamed_archive_is_byte_for_byte_the_buffered_one(files, sessions, schedules):
    """THE FORMAT GUARD: for any state, the streamed backup's manifest and
    bytes equal the buffered writer's."""
    with tempfile.TemporaryDirectory(prefix="b1-format-") as temp:
        root = Path(temp)
        state = build_state(root, sessions=[f"s{n}" for n in range(sessions)],
                            files=files, schedules=schedules)
        output = root / "streamed.tar.gz"
        manifest = create_backup(state, output, audit_key=AUDIT_KEY, now=1234)
        expected_manifest, expected = buffered_reference_archive(
            state, audit_key=AUDIT_KEY, now=1234)
        assert manifest == expected_manifest
        actual = output.read_bytes()
        assert actual == expected, (
            f"the streamed archive differs from the buffered one ({len(actual)} vs "
            f"{len(expected)} bytes): the backup format changed")


MUTATIONS = {
    "grown": lambda data: data + b"+",
    "shrunk": lambda data: data[:-1],
    "rewritten": lambda data: bytes([data[0] ^ 0xFF]) + data[1:],
}


def _state_file(state, which):
    return {
        "sandbox": lambda: next((state / "sandboxes").glob("*/data.bin")),
        "session": lambda: next((state / "sessions").glob("*.kv")),
        "audit": lambda: next((state / "audit").glob("*.jsonl")),
        "schedule": lambda: state / "product-schedules.json",
    }[which]()


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
@pytest.mark.parametrize("which", ["sandbox", "session", "audit", "schedule"])
def test_a_file_that_changes_while_the_archive_is_written_never_yields_a_wrong_archive(
        tmp_path, monkeypatch, which, mutation):
    """Streaming reads each file twice: once for the manifest, once into the
    archive. A file that changes in between must fail the backup, publishing
    nothing and leaving no scratch, rather than produce an archive whose
    bytes its own manifest contradicts, which restore would refuse only when
    it is needed. A writer that captured the file once may succeed instead,
    and then its archive must restore what it captured."""
    state = make_state(tmp_path)
    path = _state_file(state, which)
    relative = path.relative_to(state).as_posix()
    captured = path.read_bytes()
    real_addfile = tarfile.TarFile.addfile

    def addfile(archive, tarinfo, fileobj=None, *args, **kwargs):
        if tarinfo.name == state_tool.MANIFEST_NAME:
            path.write_bytes(MUTATIONS[mutation](captured))
        return real_addfile(archive, tarinfo, fileobj, *args, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, "addfile", addfile)
    output = tmp_path / "out" / "state.tar.gz"
    try:
        create_backup(state, output, audit_key=AUDIT_KEY, now=200)
    except StateToolError as error:
        assert str(error) == f"state changed while the backup was written: {relative}"
        assert list(output.parent.iterdir()) == [], "a failed backup left files behind"
        return
    monkeypatch.undo()
    restored = tmp_path / "restored"
    restore_backup(output, restored, audit_key=AUDIT_KEY)
    assert (restored / relative).read_bytes() == captured


@settings(max_examples=12, deadline=None)
@given(steps=st.lists(st.tuples(st.integers(0, 300), st.integers(1, 400)),
                      min_size=2, max_size=4))
def test_no_cycle_deletes_on_the_strength_of_an_archive_that_is_not_durable(steps):
    """THE POWER-LOSS GUARD, over any sequence of cycles (same-second reruns,
    several expiries at once, nothing to expire): before each deletion the
    cycle's archive is durable, and when the cycle returns its archive and
    its deletions are."""
    with tempfile.TemporaryDirectory(prefix="b1-cycles-") as temp, \
            pytest.MonkeyPatch.context() as monkeypatch:
        root = Path(temp).resolve()
        state = make_state(root)
        backups = root / "managed"
        calls = Syscalls(monkeypatch)
        now = 1000
        for advance, retention in steps:
            now += advance
            start = len(calls.events)
            result = run_managed_backup_cycle(
                state, backups, audit_key=AUDIT_KEY, retention_s=retention, now=now)
            events = calls.events[start:]
            final = backups / result["created"]["name"]
            for item in result["deleted"]:
                at = events.index(("unlink", backups / item["name"]))
                reason = not_durable(events, final, at)
                assert reason is None, f"deleted {item['name']} while {reason}"
            assert not_durable(events, final, len(events)) is None
            if result["deleted"]:
                last = max(i for i, e in enumerate(events) if e[0] == "unlink"
                           and MANAGED_NAME.fullmatch(e[1].name))
                assert ("fsync", "dir", identity(backups)) in [e[:3] for e in events[last:]]


MANAGED_NAME = state_tool.MANAGED_BACKUP_RE


# ── source guards: no whole-state buffers, no unsynced publication ──────


def _functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def whole_reads(source):
    """(function, line) of every call that reads a whole file or archive
    member into memory: read() with no size, read_bytes, read_text,
    readlines, json.load."""
    found = []
    for function in _functions(ast.parse(source)):
        for call in ast.walk(function):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
                continue
            name, owner = call.func.attr, ast.unparse(call.func.value)
            if ((name == "read" and not call.args and not call.keywords)
                    or name in ("read_bytes", "read_text", "readlines")
                    or (owner == "json" and name == "load")):
                found.append((function.name, call.lineno))
    return found


def publications_without_a_later_sync(source):
    """(function, line) of every call that gives a file or tree a published
    name (os.link, os.replace, os.rename, a one-argument Path.rename or
    replace, shutil.move) with no sync_directory call after it in the same
    function."""
    found = []
    for function in _functions(ast.parse(source)):
        calls = [n for n in ast.walk(function) if isinstance(n, ast.Call)]
        syncs = [c.lineno for c in calls if ast.unparse(c.func) == "sync_directory"]
        for call in calls:
            if not isinstance(call.func, ast.Attribute):
                continue
            owner, name = ast.unparse(call.func.value), call.func.attr
            if ((owner == "os" and name in ("link", "replace", "rename", "renames"))
                    or (owner == "shutil" and name == "move")
                    or (name in ("rename", "replace", "hardlink_to")
                        and len(call.args) == 1 and not call.keywords)):
                if not any(line > call.lineno for line in syncs):
                    found.append((function.name, call.lineno))
    return found


def test_the_source_guards_fire_on_every_shape_they_forbid():
    """Anti-vacuity for the two guards below."""
    reads = {
        "def a(p):\n    return p.read_bytes()\n": [("a", 2)],
        "def b(p):\n    return p.read_text()\n": [("b", 2)],
        "def c(s):\n    return s.read()\n": [("c", 2)],
        "def d(s):\n    return json.load(s)\n": [("d", 2)],
        "def e(s):\n    return s.readlines()\n": [("e", 2)],
        "def f(s):\n    return s.read(4096) + s.read(n)\n": [],
    }
    for source, expected in reads.items():
        assert whole_reads(source) == expected, source
    publications = {
        "def a():\n    os.link(t, p)\n": [("a", 2)],
        "def b():\n    os.replace(t, p)\n": [("b", 2)],
        "def c():\n    Path(t).rename(p)\n": [("c", 2)],
        "def d():\n    shutil.move(t, p)\n": [("d", 2)],
        "def e():\n    sync_directory(d)\n    os.link(t, p)\n": [("e", 3)],
        "def f():\n    os.link(t, p)\n    sync_directory(d)\n": [],
        "def g():\n    s.replace('a', 'b')\n": [],
    }
    for source, expected in publications.items():
        assert publications_without_a_later_sync(source) == expected, source


# The whole reads state_tool.py may make, each bounded by something other than
# the size of the state: the marker; one conversation at a time and the
# schedule store, which the product itself parses whole to serve a turn or a
# tick; and an archive's manifest, checked to be at most 4 MiB first.
WHOLE_READS_ALLOWED = {"_validate_state": 3, "_managed_backup_metadata": 1, "restore_backup": 1}


def test_state_tool_reads_no_whole_file_beyond_the_bounded_ones():
    """THE STREAMING SOURCE GUARD: a new whole read of a state file or archive
    member is the F-12 buffer coming back; add it here only with its bound."""
    found = {}
    for function, line in whole_reads((PI_ROOT / "state_tool.py").read_text()):
        found.setdefault(function, []).append(line)
    assert found.get("_validate_state"), "anti-vacuity: the known reads are seen"
    extra = {name: lines for name, lines in found.items()
             if len(lines) > WHOLE_READS_ALLOWED.get(name, 0)}
    assert extra == {}, (
        f"state_tool.py reads whole files or members in {extra}: stream them, "
        f"as create_backup and restore_backup do (audit F-12)")


def test_every_name_state_tool_publishes_is_synced_after_it():
    """THE PUBLICATION SOURCE GUARD: every link or rename that publishes an
    archive or a restored tree is followed, in the same function, by a sync
    of the directory that now holds the name."""
    source = (PI_ROOT / "state_tool.py").read_text()
    published = [c for c in ast.walk(ast.parse(source)) if isinstance(c, ast.Call)
                 and ast.unparse(c.func) in ("os.link", "os.replace")]
    assert len(published) >= 3, "anti-vacuity: backup, cycle and restore publish"
    assert publications_without_a_later_sync(source) == [], (
        "state_tool.py publishes a name it never syncs afterwards: a power loss "
        "can take the name back after the caller was told it exists (audit F-12)")
