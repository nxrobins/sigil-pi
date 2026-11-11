"""Durable state writes: synced, atomic, and leaving no scratch behind.

Found by the 2026-11-10 product audit. F-13: conversations, schedules and audit
records were written by write-then-rename or by append with no `fsync`, so
after a power loss or a kernel crash the synced SQLite ledger could be ahead of
the files, and a lost conversation would come back as an empty session. F-30:
a save wrote `<hash>.kv.tmp` and renamed it; a failure or a kill between the
two left that scratch file, holding the conversation, until the session's
next save, and deleting the session did not remove it.

Pinned here, on the real stores in agent.py:

- every whole-file save syncs its data before the rename and the directory
  after it; every audit append is synced before it returns; a store that
  creates its directory syncs that directory into its parent;
- a save that fails, at any step and with any exception, leaves the committed
  file as it was and no scratch file; a killed save leaves its scratch, and the
  session store's `delete` removes it with the conversation;
- scratch names are unique, so concurrent writers never share one and a
  failing writer never removes another's;
- by syntax: no module that writes live service state renames a file into
  place except through `agent.write_durably` (two product sites remain,
  listed below for the nodes that own them), and the stores write nothing
  except through it and `agent.append_durably`.
"""
import ast
import errno
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from conftest import PI_ROOT

import agent
from agent import AuditLog, ScheduleStore, SessionStore

KEY = b"durable-state-test-key-0123456789abcdef"


def encoded(messages):
    """What SessionStore.save writes for `messages`."""
    return json.dumps(messages, ensure_ascii=False).encode()


def identity(path):
    info = os.stat(path)
    return info.st_dev, info.st_ino


class Syscalls:
    """Records the calls that decide durability, with what each one touched:
    every fsync (directory or file, its identity, its size at the time) and
    every rename (source, destination, the source's identity and size). Each
    call still happens; nothing is faked."""

    def __init__(self, monkeypatch):
        self.events = []
        real_fsync, real_replace, real_rename = os.fsync, os.replace, os.rename

        def fsync(fd):
            info = os.fstat(fd)
            kind = "dir" if stat.S_ISDIR(info.st_mode) else "file"
            real_fsync(fd)
            self.events.append(("fsync", kind, (info.st_dev, info.st_ino), info.st_size))

        def renamer(real):
            def rename(src, dst, *args, **kwargs):
                info = os.stat(src)
                real(src, dst, *args, **kwargs)
                self.events.append(("rename", Path(os.fsdecode(src)),
                                    Path(os.fsdecode(dst)),
                                    (info.st_dev, info.st_ino), info.st_size))
            return rename

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(os, "replace", renamer(real_replace))
        monkeypatch.setattr(os, "rename", renamer(real_rename))

    def mark(self):
        return len(self.events)

    def since(self, mark):
        return self.events[mark:]


def assert_durable_replace(events, target, expected):
    """`events` show `target` committed with `expected` the durable way: the
    new bytes synced under their scratch name, renamed over `target`, then
    `target`'s directory synced."""
    renames = [e for e in events if e[0] == "rename" and e[2] == target]
    assert len(renames) == 1, f"expected one rename onto {target.name}, saw {renames}"
    at = events.index(renames[0])
    _, source, _, inode, size = renames[0]
    assert source.parent == target.parent, "the scratch must sit beside its target"
    assert size == len(expected)
    synced = [e for e in events[:at] if e[:3] == ("fsync", "file", inode)]
    assert synced and synced[-1][3] == len(expected), (
        f"{target.name} was renamed into place before its new bytes were synced: "
        f"after a power loss the name can point at an empty or partial file")
    directory = identity(target.parent)
    assert any(e[:3] == ("fsync", "dir", directory) for e in events[at + 1:]), (
        f"the rename onto {target.name} was never made durable: its directory "
        f"was not synced afterwards, so a power loss can bring back the old version")
    assert identity(target) == inode and target.read_bytes() == expected


# ── F-13: every write is synced ──────────────────────────────────────────


def test_a_session_save_syncs_its_bytes_then_renames_then_syncs_the_directory(
        tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions")
    store.save("s", [{"role": "user", "content": "the committed turn"}])
    calls = Syscalls(monkeypatch)
    messages = [{"role": "user", "content": "the committed turn"},
                {"role": "assistant", "content": [{"type": "text", "text": "noted"}]}]
    store.save("s", messages)
    assert_durable_replace(calls.events, store._path("s"), encoded(messages))


def test_a_research_schedule_save_is_synced_the_same_way(tmp_path, monkeypatch):
    store = ScheduleStore(tmp_path / "schedules.json")
    store.put("daily", "s", "first", 60_000)
    calls = Syscalls(monkeypatch)
    store.mark_run("daily", 1234.5)
    expected = json.dumps({"daily": {"session": "s", "message": "first",
                                     "every_ms": 60_000, "last_run": 1234.5}},
                          ensure_ascii=False).encode()
    assert_durable_replace(calls.events, store.path, expected)


def test_every_audit_append_is_synced_before_it_returns(tmp_path, monkeypatch):
    """An append is durable when `append_or_raise` returns: the chain is
    synced at its new size, and the first append, which creates the chain,
    also syncs the directory that now names it."""
    log = AuditLog(tmp_path / "audit", key=KEY)
    chain = log._path("s")
    calls = Syscalls(monkeypatch)
    for n in range(3):
        mark = calls.mark()
        log.append_or_raise("s", "llm", "src", "in", "out", None, None, 1)
        made = calls.since(mark)
        assert ("fsync", "file", identity(chain), chain.stat().st_size) in made, (
            f"append {n} returned before its record was synced")
        if n == 0:
            assert any(e[:3] == ("fsync", "dir", identity(log.dir)) for e in made), (
                "the append that created the chain did not sync its directory")
    assert agent.verify_chain(log.read("s"), key=KEY) == (True, None)


@pytest.mark.parametrize("kind", ["sessions", "audit", "schedules"])
def test_a_store_that_creates_its_directories_syncs_each_into_its_parent(
        tmp_path, monkeypatch, kind):
    """A synced file in a directory that a power loss can take away is not
    durable. Every directory a store creates is synced into its parent."""
    calls = Syscalls(monkeypatch)
    state = tmp_path / "state"
    if kind == "sessions":
        SessionStore(state / "nested" / "sessions")
        made = [state, state / "nested", state / "nested" / "sessions"]
    elif kind == "audit":
        AuditLog(state / "nested" / "audit")
        made = [state, state / "nested", state / "nested" / "audit"]
    else:
        ScheduleStore(state / "nested" / "schedules.json")
        made = [state, state / "nested"]
    synced = {e[2] for e in calls.events if e[:2] == ("fsync", "dir")}
    unsynced = [str(d.relative_to(tmp_path)) for d in made
                if identity(d.parent) not in synced]
    assert unsynced == [], f"created but never synced into its parent: {unsynced}"


# ── F-30: a failed save leaves nothing behind ────────────────────────────


def _store(kind, directory):
    """A store of `kind` in `directory`, with a function that commits `text`
    through it and one that reads back what is committed."""
    if kind == "session":
        store = SessionStore(directory)
        return (store, store._path("s"),
                lambda text: store.save("s", [{"role": "user", "content": text}]),
                lambda: store.load("s")[0]["content"])
    store = ScheduleStore(Path(directory) / "schedules.json")
    return (store, store.path,
            lambda text: store.put("daily", "s", text, 60_000),
            lambda: store.entries()[0]["message"])


SAVE_PAST_THE_FILE_SIZE_LIMIT = r"""
import json, os, resource, signal, sys
sys.path.insert(0, sys.argv[1])
sys.dont_write_bytecode = True
from agent import ScheduleStore, SessionStore
kind, directory = sys.argv[2], sys.argv[3]
if kind == "session":
    store = SessionStore(directory)
    commit = lambda text: store.save("s", [{"role": "user", "content": text}])
else:
    store = ScheduleStore(os.path.join(directory, "schedules.json"))
    commit = lambda text: store.put("daily", "s", text, 60_000)
commit("committed")
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
resource.setrlimit(resource.RLIMIT_FSIZE,
                   (4096, resource.getrlimit(resource.RLIMIT_FSIZE)[1]))
try:
    commit("x" * 65536)
except OSError as error:
    print(json.dumps({"errno": error.errno}))
else:
    print(json.dumps({"errno": None}))
"""


@pytest.mark.parametrize("kind", ["session", "schedule"])
def test_a_save_that_fails_partway_through_its_write_leaves_no_scratch(tmp_path, kind):
    """A real write failure, not a mock: the child's file size limit stops the
    scratch file at 4 KiB of a 64 KiB save, as a filling disk does, and the
    kernel fails the next write with EFBIG."""
    directory = tmp_path / "state"
    child = subprocess.run(
        [sys.executable, "-c", SAVE_PAST_THE_FILE_SIZE_LIMIT, str(PI_ROOT),
         kind, str(directory)],
        capture_output=True, text=True, timeout=60)
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout) == {"errno": errno.EFBIG}, child.stdout
    _, committed, _, read = _store(kind, directory)
    assert sorted(os.listdir(directory)) == [committed.name], (
        f"a failed save left scratch behind: {sorted(os.listdir(directory))}")
    assert read() == "committed"


@pytest.mark.parametrize("fault", [
    lambda: OSError(errno.EIO, "injected I/O error at the rename"),
    lambda: KeyboardInterrupt(),
], ids=["io-error", "interrupt"])
@pytest.mark.parametrize("kind", ["session", "schedule"])
def test_a_save_that_fails_at_its_rename_leaves_no_scratch(
        tmp_path, monkeypatch, kind, fault):
    """The other failing step a write-then-rename has, with an ordinary error
    and with an exception that is not one (a signal arriving mid-save)."""
    directory = tmp_path / "state"
    _, committed, commit, read = _store(kind, directory)
    commit("committed")
    real = os.replace
    injected = fault()

    def replace(src, dst, *args, **kwargs):
        if Path(os.fsdecode(dst)) == committed:
            raise injected
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(type(injected)) as raised:
        commit("uncommitted")
    assert raised.value is injected, "the save must re-raise the failure unchanged"
    assert sorted(os.listdir(directory)) == [committed.name], (
        f"a failed save left scratch behind: {sorted(os.listdir(directory))}")
    assert read() == "committed"


def test_concurrent_saves_of_one_session_each_commit_their_own_bytes(
        tmp_path, monkeypatch):
    """Scratch names are unique. With one shared name, a second writer
    overwrites the first writer's scratch while the first is between its
    write and its rename: the first commits bytes it never wrote, and the
    second's rename finds nothing. Turns serialize per session today; the
    store must not depend on that, and removing scratch on failure is only
    safe when no other writer can be using the same file."""
    store = SessionStore(tmp_path / "sessions")
    target = store._path("s")
    first = [{"role": "user", "content": "from the first writer"}]
    second = [{"role": "user", "content": "from the second writer"}]
    at_rename = {"first": threading.Event(), "second": threading.Event()}
    first_done = threading.Event()
    moved, errors = {}, {}
    real = os.replace

    def replace(src, dst, *args, **kwargs):
        if Path(os.fsdecode(dst)) != target:
            return real(src, dst, *args, **kwargs)
        who = threading.current_thread().name
        at_rename[who].set()
        (at_rename["second"] if who == "first" else first_done).wait(5)
        try:
            moved[who] = Path(os.fsdecode(src)).read_bytes()
        except OSError as error:
            moved[who] = repr(error)
        try:
            return real(src, dst, *args, **kwargs)
        finally:
            if who == "first":
                first_done.set()

    def save(messages):
        try:
            store.save("s", messages)
        except Exception as error:  # noqa: BLE001 — recorded and asserted on
            errors[threading.current_thread().name] = repr(error)

    monkeypatch.setattr(os, "replace", replace)
    one = threading.Thread(target=save, args=(first,), name="first")
    two = threading.Thread(target=save, args=(second,), name="second")
    one.start()
    assert at_rename["first"].wait(5), "the first writer never reached its rename"
    two.start()
    one.join(10)
    two.join(10)
    assert errors == {}, f"a concurrent save of the same session failed: {errors}"
    assert moved == {"first": encoded(first), "second": encoded(second)}, (
        f"a writer renamed bytes it did not write: {moved}")
    assert store.load("s") == second
    assert sorted(os.listdir(store.dir)) == [target.name]


# ── F-30: what a killed save leaves, deletion removes ───────────────────


KILLED_MID_SAVE = r"""
import os, signal, sys
sys.path.insert(0, sys.argv[1])
sys.dont_write_bytecode = True
from agent import SessionStore
store = SessionStore(sys.argv[2])
store.save("kept", [{"role": "user", "content": "another session"}])
store.save("gone", [{"role": "user", "content": "committed"}])
target = os.fspath(store._path("gone"))
real = os.replace
def replace(src, dst, *args, **kwargs):
    if os.fspath(dst) == target:
        os.kill(os.getpid(), signal.SIGKILL)
    return real(src, dst, *args, **kwargs)
os.replace = replace
store.save("gone", [{"role": "user", "content": "my secret plan"}])
"""


def _holding(directory, needle):
    return sorted(p.name for p in Path(directory).iterdir()
                  if p.is_file() and needle in p.read_bytes())


def test_delete_removes_the_conversation_and_the_scratch_a_killed_save_left(tmp_path):
    """A process killed between its write and its rename cannot clean up.
    Its scratch holds the conversation; deleting the session must remove it
    too, and nothing of any other session."""
    directory = tmp_path / "sessions"
    child = subprocess.run(
        [sys.executable, "-c", KILLED_MID_SAVE, str(PI_ROOT), str(directory)],
        capture_output=True, text=True, timeout=60)
    assert child.returncode == -signal.SIGKILL, child.stderr
    store = SessionStore(directory)
    committed, kept = store._path("gone"), store._path("kept")
    left = _holding(directory, b"my secret plan")
    assert len(left) == 1 and left[0] not in (committed.name, kept.name), (
        "anti-vacuity: the kill did not land between the write and the rename")
    assert store.load("gone") == [{"role": "user", "content": "committed"}], (
        "an uncommitted save must never be adopted")
    assert store.delete("gone") is True
    assert sorted(os.listdir(directory)) == [kept.name]
    assert _holding(directory, b"my secret plan") == []
    assert store.load("kept") == [{"role": "user", "content": "another session"}]


def test_delete_removes_the_scratch_name_an_earlier_release_used(tmp_path):
    """Releases before this fix named the scratch `<hash>.kv.tmp`. State
    upgraded from one may hold such a file; deletion removes it too."""
    store = SessionStore(tmp_path / "sessions")
    store.save("gone", [{"role": "user", "content": "committed"}])
    legacy = store._path("gone").with_suffix(".kv.tmp")
    legacy.write_bytes(encoded([{"role": "user", "content": "my secret plan"}]))
    assert store.delete("gone") is True
    assert os.listdir(store.dir) == []


def test_delete_of_a_session_with_no_conversation_reports_false_and_still_cleans(
        tmp_path):
    """A killed FIRST save leaves scratch and no committed file. Deleting
    that session reports no conversation and removes the scratch."""
    store = SessionStore(tmp_path / "sessions")
    store.save("other", [{"role": "user", "content": "unrelated"}])
    legacy = store._path("gone").with_suffix(".kv.tmp")
    legacy.write_bytes(b"[]")
    assert store.delete("gone") is False
    assert store.delete("never-saved") is False
    assert sorted(os.listdir(store.dir)) == [store._path("other").name]


# ── guards for the class ─────────────────────────────────────────────────


def is_scratch_of(path, target):
    """A scratch file `agent.write_durably(target, ...)` creates."""
    path = Path(os.fsdecode(path))
    return (path.parent == target.parent
            and re.fullmatch(rf"\.{re.escape(target.name)}\.[0-9a-f]{{16}}\.tmp",
                             path.name) is not None)


class FailStep:
    """Fails exactly one step of `agent.write_durably(target, ...)` with
    `error`, at the OS call that performs it. Every other call, by this
    write or by anything else, goes through untouched. A failing close
    still releases its descriptor first, as close(2) does."""

    STEPS = ("create", "sync", "close", "rename", "open-dir", "sync-dir", "close-dir")

    def __init__(self, monkeypatch, target, step, error):
        self.step, self.error, self.fired = step, error, False
        fds = {}
        real = {name: getattr(os, name) for name in ("open", "fsync", "close", "replace")}

        def due(step):
            if step == self.step and not self.fired:
                self.fired = True
                return True
            return False

        def open_(path, flags, *args, **kwargs):
            path_ = Path(os.fsdecode(path))
            kind = ("scratch" if is_scratch_of(path_, target)
                    else "dir" if path_ == target.parent else None)
            if kind and due({"scratch": "create", "dir": "open-dir"}[kind]):
                raise self.error
            fd = real["open"](path, flags, *args, **kwargs)
            if kind:
                fds[fd] = kind
            return fd

        def fsync(fd):
            kind = fds.get(fd)
            if kind and due({"scratch": "sync", "dir": "sync-dir"}[kind]):
                raise self.error
            return real["fsync"](fd)

        def close(fd):
            kind = fds.pop(fd, None)
            real["close"](fd)
            if kind and due({"scratch": "close", "dir": "close-dir"}[kind]):
                raise self.error

        def replace(src, dst, *args, **kwargs):
            if Path(os.fsdecode(dst)) == target and due("rename"):
                raise self.error
            return real["replace"](src, dst, *args, **kwargs)

        for name, function in (("open", open_), ("fsync", fsync),
                               ("close", close), ("replace", replace)):
            monkeypatch.setattr(os, name, function)


ERRORS = {
    "full-disk": lambda: OSError(errno.ENOSPC, "injected: no space left"),
    "io-error": lambda: OSError(errno.EIO, "injected: I/O error"),
    "interrupt": lambda: KeyboardInterrupt(),
    "exit": lambda: SystemExit(3),
}


@settings(max_examples=200, deadline=None)
@given(step=st.sampled_from((None,) + FailStep.STEPS),
       error=st.sampled_from(sorted(ERRORS)),
       old=st.one_of(st.none(), st.binary(max_size=2048)),
       new=st.one_of(st.binary(max_size=2048), st.binary(min_size=65536, max_size=70000)))
def test_a_durable_write_failing_at_any_step_leaves_old_or_new_and_no_scratch(
        tmp_path_factory, step, error, old, new):
    """THE GUARD for both findings. Whatever step fails, with an error or an
    exception that is not one, the failure reaches the caller unchanged, the
    target holds exactly its old bytes (or is still absent) when the failure
    came before the rename and exactly the new ones after it, and no scratch
    file is left. With no failure, the new bytes are committed."""
    directory = tmp_path_factory.mktemp("durable")
    target = directory / "state.json"
    if old is not None:
        agent.write_durably(target, old)
    with pytest.MonkeyPatch.context() as patch:
        if step is None:
            agent.write_durably(target, new)
        else:
            injected = ERRORS[error]()
            fault = FailStep(patch, target, step, injected)
            with pytest.raises(type(injected)) as raised:
                agent.write_durably(target, new)
            assert raised.value is injected and fault.fired
    renamed = step is None or step.endswith("-dir")
    if renamed:
        assert target.read_bytes() == new
    elif old is not None:
        assert target.read_bytes() == old
    else:
        assert not target.exists()
    assert os.listdir(directory) == ([target.name] if renamed or old is not None else [])


def test_a_scratch_file_that_cannot_be_removed_does_not_hide_the_failure(
        tmp_path, monkeypatch):
    """Cleanup is best effort: when the scratch file cannot be removed (the
    disk has gone read-only, say) the save still raises its own failure, not
    the cleanup's, and the scratch it left is the kind `scratch_files`
    reports."""
    target = tmp_path / "state.json"
    agent.write_durably(target, b"old")
    real_unlink = os.unlink
    injected = OSError(errno.EIO, "injected: I/O error at the rename")

    def unlink(path, *args, **kwargs):
        if is_scratch_of(path, target):
            raise OSError(errno.EROFS, "injected: read-only file system")
        return real_unlink(path, *args, **kwargs)

    FailStep(monkeypatch, target, "rename", injected)
    monkeypatch.setattr(os, "unlink", unlink)
    with pytest.raises(OSError) as raised:
        agent.write_durably(target, b"new")
    assert raised.value is injected
    monkeypatch.undo()
    [left] = agent.scratch_files(target)
    assert left.read_bytes() == b"new" and target.read_bytes() == b"old"


SESSION_A, SESSION_B = "alpha", "beta"


@settings(max_examples=100, deadline=None)
@given(present=st.sets(st.sampled_from([
    "a.kv", "a.new", "a.legacy", "b.kv", "b.new", "b.legacy",
    "a.short-token", "a.upper-token", "a.no-dot", "a.kv.bak", "other.json",
    "other.json.new"])))
def test_scratch_listing_and_deletion_take_exactly_what_they_should(
        tmp_path_factory, present):
    """Deletion is a privacy operation: it must take every scratch file of
    the session it deletes, and nothing of any other session, whatever mix
    of files the directory holds, including near-misses of the naming."""
    store = SessionStore(tmp_path_factory.mktemp("sessions"))
    a, b = store._path(SESSION_A), store._path(SESSION_B)
    token = "0123456789abcdef"
    names = {
        "a.kv": a.name, "a.new": f".{a.name}.{token}.tmp", "a.legacy": f"{a.name}.tmp",
        "b.kv": b.name, "b.new": f".{b.name}.{token}.tmp", "b.legacy": f"{b.name}.tmp",
        "a.short-token": f".{a.name}.{token[:15]}.tmp",
        # Not token.upper(): on a case-insensitive filesystem (macOS by
        # default) that names the same file as "a.new".
        "a.upper-token": f".{a.name}.FEDCBA9876543210.tmp",
        "a.no-dot": f"{a.name}.{token}.tmp",
        "a.kv.bak": f"{a.name}.bak",
        "other.json": "other.json", "other.json.new": f".other.json.{token}.tmp",
    }
    for key in present:
        (store.dir / names[key]).write_bytes(key.encode())
    scratch = {"a.new", "a.legacy", "b.new", "b.legacy"}
    assert {p.name for p in store.scratch_files()} == {names[k] for k in present & scratch}
    assert {p.name for p in agent.scratch_files(a)} == {names[k] for k in present & {"a.new"}}
    assert {p.name for p in agent.scratch_files(store.dir / "other.json")} == {
        names[k] for k in present & {"other.json.new"}}
    assert store.delete(SESSION_A) is ("a.kv" in present)
    assert sorted(os.listdir(store.dir)) == sorted(
        names[k] for k in present - {"a.kv", "a.new", "a.legacy"})


def test_deletion_is_synced_into_the_directory(tmp_path, monkeypatch):
    """The product removes a session's quota row after deleting its files.
    If the unlinks are not durable, a power loss brings the files back with
    no row, and startup refuses state it cannot attribute."""
    store = SessionStore(tmp_path / "sessions")
    store.save("gone", [{"role": "user", "content": "committed"}])
    calls = Syscalls(monkeypatch)
    assert store.delete("gone") is True
    assert ("fsync", "dir", identity(store.dir)) in [e[:3] for e in calls.events]


def test_an_existing_directory_costs_no_sync(tmp_path, monkeypatch):
    calls = Syscalls(monkeypatch)
    assert agent.ensure_directory(tmp_path) == tmp_path
    SessionStore(tmp_path)
    assert calls.events == []


def test_a_relative_directory_is_created_and_synced_into_the_working_directory(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = Syscalls(monkeypatch)
    agent.ensure_directory(Path("state") / "sessions")
    assert (tmp_path / "state" / "sessions").is_dir()
    synced = {e[2] for e in calls.events if e[:2] == ("fsync", "dir")}
    assert {identity(tmp_path), identity(tmp_path / "state")} <= synced


def test_a_new_file_written_durably_keeps_the_mode_write_bytes_gave_it(tmp_path):
    """The scratch file is created 0o666 under the umask, exactly what
    Path.write_bytes created before, so a committed file's permissions do
    not change with this fix."""
    agent.write_durably(tmp_path / "durable", b"x")
    (tmp_path / "plain").write_bytes(b"x")
    assert (stat.S_IMODE((tmp_path / "durable").stat().st_mode)
            == stat.S_IMODE((tmp_path / "plain").stat().st_mode))


# The modules that write the live service state. state_tool.py is not one:
# it publishes backups with os.link, which refuses to overwrite, and installs
# a restore by renaming a whole staged tree; their durability is F-12's
# (remediation node B1), and write_durably's overwrite would be wrong there.
STATE_MODULES = ("agent.py", "product_service.py", "product_main.py")

# Bare renames-into-place that remain outside agent.py, with the most of them
# each function may hold. Each is the remediation node's that owns the file:
# P2 syncs schedule writes (F-13); the clean-shutdown marker was found by
# this node's sweep. The guard is one-sided, so the owner of an entry removes
# it by moving the site onto agent.write_durably, without editing this test.
REMAINING_BARE_RENAMES = {
    # None: node P2 moved both remaining sites, the product's schedule save
    # and its clean-shutdown marker, onto agent.write_durably.
}


def _scopes(tree):
    """(qualified name of the enclosing def or class, node) for every node."""
    def walk(node, scope):
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = f"{scope}.{child.name}" if scope else child.name
            yield inner, child
            yield from walk(child, inner)
    yield from walk(tree, "")


def renames_into_place(source):
    """(scope, line) of every call that publishes a file under a new name: a
    rename, a replace or a link, by `os`, `shutil` or a Path method. A
    one-argument `.replace(x)` or `.rename(x)` is pathlib's (str.replace
    needs two)."""
    found = []
    for scope, node in _scopes(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        owner, name = ast.unparse(node.func.value), node.func.attr
        if ((owner == "os" and name in ("replace", "rename", "renames", "link"))
                or (owner == "shutil" and name in ("move", "copyfile", "copy", "copy2"))
                or (name in ("replace", "rename", "hardlink_to")
                    and len(node.args) == 1 and not node.keywords)):
            found.append((scope, node.lineno))
    return found


def test_the_rename_detector_fires_on_every_shape_it_forbids():
    """Anti-vacuity: each forbidden shape is found, and the lookalikes that
    are not renames are not."""
    shapes = {
        "def a():\n    tmp.write_bytes(b); tmp.replace(path)\n": [("a", 2)],
        "def b():\n    os.replace(tmp, path)\n": [("b", 2)],
        "def c():\n    os.rename(tmp, path)\n": [("c", 2)],
        "def d():\n    Path(tmp).rename(path)\n": [("d", 2)],
        "def e():\n    os.link(tmp, path)\n": [("e", 2)],
        "def f():\n    shutil.move(tmp, path)\n": [("f", 2)],
        "class G:\n    def h(self):\n        self.tmp.replace(self.path)\n": [("G.h", 3)],
        "def i():\n    s.replace('a', 'b'); d.replace(year=1); s.replace('a', 'b', 1)\n": [],
    }
    for source, expected in shapes.items():
        assert renames_into_place(source) == expected, source


def test_state_is_renamed_into_place_only_by_write_durably():
    """THE SOURCE GUARD: a write-then-rename that bypasses write_durably is
    unsynced and leaves its scratch on failure, the F-13 and F-30 defect.
    In agent.py the only one is write_durably's own; elsewhere only the
    remaining sites listed above, each no more often than it is today."""
    found = {}
    for module in STATE_MODULES:
        for scope, line in renames_into_place((PI_ROOT / module).read_text()):
            found.setdefault((module, scope), []).append(line)
    assert ("agent.py", "write_durably") in found, "anti-vacuity: the helper's own rename"
    del found[("agent.py", "write_durably")]
    assert len(sum(found.values(), [])) <= sum(REMAINING_BARE_RENAMES.values())
    extra = {site: lines for site, lines in found.items()
             if len(lines) > REMAINING_BARE_RENAMES.get(site, 0)}
    assert extra == {}, (
        f"rename-into-place outside agent.write_durably: {extra}. Commit state "
        f"through write_durably, which syncs it and cleans up after a failure.")


def test_the_stores_write_only_through_the_durable_helpers():
    """The session, schedule and audit stores in agent.py write nothing
    except through write_durably and append_durably: no direct write, no
    write-mode open, no rename. Reads, listing and unlink are theirs."""
    tree = ast.parse((PI_ROOT / "agent.py").read_text())
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    helpers = {"SessionStore": "write_durably", "ScheduleStore": "write_durably",
               "AuditLog": "append_durably"}
    for name, helper in helpers.items():
        calls = [node for node in ast.walk(classes[name]) if isinstance(node, ast.Call)]
        called = {ast.unparse(call.func) for call in calls}
        assert helper in called, f"{name} no longer commits through {helper}"
        writes = []
        for call in calls:
            function = ast.unparse(call.func)
            attribute = call.func.attr if isinstance(call.func, ast.Attribute) else function
            if attribute in ("write", "write_bytes", "write_text", "writelines",
                             "touch", "rename", "replace", "link", "hardlink_to"):
                if not (attribute == "replace" and len(call.args) != 1):
                    writes.append(f"line {call.lineno}: {ast.unparse(call)[:60]}")
            if attribute == "open" or function == "open":
                modes = [a.value for a in call.args if isinstance(a, ast.Constant)
                         and isinstance(a.value, str)]
                modes += [k.value.value for k in call.keywords if k.arg == "mode"
                          and isinstance(k.value, ast.Constant)]
                if function == "os.open" or any(set(m) & set("wax+") for m in modes):
                    writes.append(f"line {call.lineno}: {ast.unparse(call)[:60]}")
        assert writes == [], f"{name} writes outside {helper}: {writes}"
