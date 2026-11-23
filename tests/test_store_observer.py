"""A test may watch a running native store only if watching cannot stop it.

These need no toolchain. The owner is a second process holding one SQLite
connection configured as native/store/src/store.rs configures its own, because
the hazard is a lock held across processes.
"""
import itertools
import sqlite3
import subprocess
import sys
import time
import types
from contextlib import closing

import pytest

import store_observer
from store_observer import observe

ROWS = "SELECT key,revision FROM records ORDER BY key"

OWNER = r"""
import sqlite3, sys
db = sqlite3.connect(sys.argv[1], isolation_level=None)
print(db.execute("PRAGMA journal_mode=DELETE").fetchone()[0], flush=True)
db.execute("PRAGMA synchronous=EXTRA")
db.execute("PRAGMA busy_timeout=0")
for line in sys.stdin:
    try:
        db.execute(line)
        print("ok", flush=True)
    except sqlite3.Error as error:
        print(f"error: {error}", flush=True)
"""


class Owner:
    """The store's side: one connection, rollback journal, no busy timeout."""

    def __init__(self, database):
        self.process = subprocess.Popen([sys.executable, "-c", OWNER, str(database)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        assert self.process.stdout.readline() == "delete\n"

    def run(self, statement):
        self.process.stdin.write(statement + "\n")
        self.process.stdin.flush()
        return self.process.stdout.readline().strip()

    def advance(self):
        """One whole transaction; the reply is the commit's."""
        assert self.run("BEGIN IMMEDIATE") == "ok"
        assert self.run("UPDATE records SET revision=revision+1") == "ok"
        return self.run("COMMIT")

    def close(self):
        self.process.stdin.close()
        assert self.process.wait(30) == 0


@pytest.fixture
def owner(tmp_path):
    (tmp_path / "records").mkdir()
    owner = Owner(tmp_path / "records/records.sqlite")
    try:
        assert owner.run("CREATE TABLE records(key TEXT, revision INTEGER)") == "ok"
        assert owner.run("INSERT INTO records VALUES('turn', 1)") == "ok"
        yield owner
    finally:
        owner.close()


def rows(db):
    return db.execute(ROWS).fetchall()


def test_an_ordinary_read_only_connection_fails_the_owners_commit(owner, tmp_path):
    """The hazard itself, so that this file fails if it ever stops being true.

    The store refuses all further work after a failed commit, so this one
    refused commit is a service that never advances again."""
    database = tmp_path / "records/records.sqlite"
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True, isolation_level=None)) as reader:
        reader.execute("BEGIN")
        assert rows(reader) == [("turn", 1)]
        assert owner.advance() == "error: database is locked"
        assert owner.run("ROLLBACK") == "ok"
    assert owner.advance() == "ok"


def test_observe_cannot_fail_a_commit_and_does_not_return_the_read_it_overlapped(owner, tmp_path):
    reads = []

    def read(db):
        db.execute("BEGIN")
        seen = rows(db)
        if not reads:
            # The owner commits while this connection is in the middle of a read.
            assert owner.advance() == "ok"
        reads.append(seen)
        return seen

    assert observe(tmp_path, read) == [("turn", 2)]
    # The first read saw the old row and was discarded, not handed back.
    assert reads == [[("turn", 1)], [("turn", 2)]]


def test_a_failure_from_a_read_the_file_moved_under_is_discarded_with_the_read(owner, tmp_path):
    """SQLite promises nothing about a file that changes under an immutable
    reader: wrong rows or an error. Neither may reach the caller."""
    reads = []

    def read(db):
        reads.append(rows(db))
        if len(reads) == 1:
            assert owner.advance() == "ok"
            raise sqlite3.DatabaseError("database disk image is malformed")
        return reads[-1]

    assert observe(tmp_path, read) == [("turn", 2)]
    assert len(reads) == 2


def test_observe_does_not_read_while_a_write_is_in_progress(owner, tmp_path):
    assert owner.run("BEGIN IMMEDIATE") == "ok"
    assert owner.run("UPDATE records SET revision=7") == "ok"
    reads = []
    with pytest.raises(AssertionError, match="write is in progress"):
        observe(tmp_path, lambda db: reads.append(rows(db)), patience=0.2)
    assert reads == [], "it must not have read at all"
    assert owner.run("COMMIT") == "ok"
    assert observe(tmp_path, rows) == [("turn", 7)]


def test_observe_does_not_release_a_lock_this_process_holds_on_purpose(owner, tmp_path):
    """POSIX drops every lock a process holds on a file when the process closes
    any descriptor for it. Two tests lock the database themselves to make the
    store busy, and an observer that read the header with a plain open() would
    release their lock without an error."""
    with closing(sqlite3.connect(tmp_path / "records/records.sqlite", isolation_level=None)) as mine:
        mine.execute("BEGIN EXCLUSIVE")
        assert owner.run("BEGIN IMMEDIATE") == "error: database is locked"
        assert observe(tmp_path, rows) == [("turn", 1)]
        assert owner.run("BEGIN IMMEDIATE") == "error: database is locked", "observing released this lock"
        mine.execute("ROLLBACK")
    assert owner.advance() == "ok"


def test_the_file_changes_only_under_a_journal_and_every_commit_moves_the_header(owner, tmp_path):
    """The two facts about SQLite that the observer's argument stands on."""
    database = tmp_path / "records/records.sqlite"
    journal = tmp_path / "records/records.sqlite-journal"
    committed = database.read_bytes()
    assert not journal.exists()
    assert owner.run("BEGIN IMMEDIATE") == "ok"
    assert owner.run("UPDATE records SET revision=revision+1") == "ok"
    assert journal.exists(), "an uncommitted change must already have its journal"
    assert database.read_bytes() == committed, "and must not have reached the file"
    assert owner.run("COMMIT") == "ok"
    assert not journal.exists()
    assert database.read_bytes()[:100] != committed[:100], "a commit must change the header"
    # A change that is rolled back leaves no trace for the header to report.
    committed = database.read_bytes()
    assert owner.run("BEGIN IMMEDIATE") == "ok"
    assert owner.run("UPDATE records SET revision=revision+1") == "ok"
    assert owner.run("ROLLBACK") == "ok"
    assert not journal.exists() and database.read_bytes() == committed


def test_a_mistake_in_the_read_is_raised_at_once_not_waited_out(owner, tmp_path):
    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        observe(tmp_path, lambda db: db.execute("SELECT * FROM absent").fetchall(), patience=30)
    assert time.monotonic() - started < 5


def test_a_missing_database_is_the_error_it_always_was(tmp_path):
    with pytest.raises(sqlite3.OperationalError, match="unable to open"):
        observe(tmp_path, rows, patience=30)


def test_observe_refuses_a_store_that_does_not_use_a_rollback_journal(tmp_path):
    """In WAL mode commits land in another file, which an immutable reader never
    opens. It would return stale rows as current, so it must not return at all."""
    (tmp_path / "records").mkdir()
    with closing(sqlite3.connect(tmp_path / "records/records.sqlite")) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        db.execute("CREATE TABLE records(key TEXT, revision INTEGER)")
        db.commit()
    with pytest.raises(AssertionError, match="not using a rollback journal"):
        observe(tmp_path, rows, patience=1)


class Commit:
    """A file that one rollback-journal commit happens to, one step at a time.

    The journal appears, the three pages are rewritten in the given order, the
    journal is deleted. `schedule[i]` is the observer event before which step
    i happens; every probe and page read the observer makes is one event."""

    def __init__(self, order, schedule):
        self.steps = [("journal", True), *((page, "new") for page in order), ("journal", False)]
        self.schedule, self.done, self.events = schedule, 0, 0
        self.journal, self.header, self.a, self.b = False, "old", "old", "old"

    def see(self, name):
        while self.done < len(self.steps) and self.schedule[self.done] <= self.events:
            setattr(self, *self.steps[self.done])
            self.done += 1
        self.events += 1
        return getattr(self, name)


def test_no_interleaving_of_a_commit_gets_a_torn_or_uncommitted_read_past_the_observer(monkeypatch, tmp_path):
    """Every way one commit can fall across the observer's first two attempts.

    The code under test is the real observe(): only the file it probes and the
    connection it opens are replaced. The pages are rewritten in every order,
    because SQLite promises none: it usually writes page 1 first, and writes a
    spilled page before it."""
    monkeypatch.setattr(store_observer, "_PAUSE", 0)
    monkeypatch.setattr(store_observer, "_open", lambda _: types.SimpleNamespace(close=lambda: None))
    tried = 0
    for order in itertools.permutations(("header", "a", "b")):
        # An attempt is at most six events, so 0..12 spans two of them and "afterwards".
        for schedule in itertools.combinations_with_replacement(range(13), 5):
            file = Commit(order, schedule)
            monkeypatch.setattr(store_observer, "_header", lambda _: file.see("header"))
            monkeypatch.setattr(store_observer, "_journal", lambda _: file.see("journal"))
            seen = observe(tmp_path, lambda _: (file.see("a"), file.see("b")), patience=30)
            where = f"pages rewritten in order {order}, steps before observer events {schedule}"
            assert seen in (("old", "old"), ("new", "new")), f"torn read {seen}: {where}"
            assert seen == ("old", "old") or file.done == 5, f"returned a state not yet committed: {where}"
            tried += 1
    assert tried == 6 * 6188
