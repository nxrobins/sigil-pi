"""Read a running native store's records without being able to stop it.

THE HAZARD. native/store holds one SQLite connection with a zero busy timeout
and treats a failed commit as fatal: it marks itself poisoned and refuses all
further work until it is reopened. Any ordinary SQLite reader, read-only or
not, holds a SHARED lock on the file while a statement runs, and a SHARED lock
is enough to make the owner's COMMIT fail with "database is locked". A test
that polls the database through `?mode=ro` can therefore stop the service it
is waiting on. The turn never advances, and a hundred seconds later the test
reports that the service did not commit.

observe() takes no lock. `immutable=1` tells SQLite the file cannot change, so
SQLite neither locks it nor checks it for changes. The file does change, so a
read is kept only if it provably saw one committed state:

    header    journal?    READ    journal?    header

The store uses a rollback journal: it rewrites the database only while
`records.sqlite-journal` exists, and every commit changes the file header (the
change counter at offset 24). A commit that touches the file during READ
either still has its journal at one of the two inner probes, or began after
the first of them and finished before the second, which puts its header write
between the two outer ones. The order is the argument: read the header inside
the journal probe on either side and a commit can slip through unseen.
test_store_observer.py runs every interleaving against this code.
"""
import os
import sqlite3
import time
from contextlib import closing
from urllib.parse import quote

_MOVED = object()
_PAUSE = 0.002


def _open(database):
    return sqlite3.connect(f"file:{quote(str(database))}?mode=ro&immutable=1", uri=True)


def _header(database):
    """The 100-byte file header as a fresh lock-free connection reads it, or the
    error it gets instead.

    Deliberately not open(database).read(100). POSIX drops every lock a process
    holds on a file when that process closes any descriptor for it, so a plain
    read here would silently release the lock of a test that holds one on
    purpose. SQLite keeps its own descriptors open until closing them is safe.
    """
    try:
        with closing(_open(database)) as db:
            return bytes(db.serialize()[:100])
    except sqlite3.Error as error:
        return repr(error)


def _journal(database):
    return os.path.exists(f"{database}-journal")


def _rollback_journal_only(header):
    """The argument above is about a rollback journal. In WAL mode commits go to
    another file, which an immutable reader never opens: it would see stale rows
    and call them current."""
    if isinstance(header, bytes) and len(header) == 100 and header.startswith(b"SQLite format 3\x00"):
        assert header[18:20] == b"\x01\x01", (
            "the store's database is not using a rollback journal (file format "
            f"versions {header[18]}/{header[19]}); store_observer cannot tell a "
            "committed state from a stale one in that mode and must be rewritten")


def _attempt(database, read):
    """read(connection), or _MOVED if the file may have changed underneath it."""
    before = _header(database)
    if _journal(database):
        return _MOVED
    try:
        _rollback_journal_only(before)
        with closing(_open(database)) as db:
            value, failure = read(db), None
    except Exception as caught:  # a torn read can fail in any way at all; judged below
        value, failure = None, caught
    if _journal(database) or _header(database) != before:
        return _MOVED
    if failure is not None:
        raise failure
    return value


def observe(root, read, *, patience=30.0):
    """The result of read(connection) against one committed state of the store at `root`.

    `read` may run several times and must return plain values, not a cursor:
    the connection is closed before its result is judged. An exception it
    raises against a state that held still is the caller's own and propagates
    at once; one raised while the file was moving is discarded with the read.
    """
    database = root / "records" / "records.sqlite"
    deadline = time.monotonic() + patience
    while True:
        seen = _attempt(database, read)
        if seen is not _MOVED:
            return seen
        if time.monotonic() >= deadline:
            state = ("a write is in progress: its rollback journal exists. If the store is "
                     "not running, it died mid-commit and only the store can roll that back"
                     if _journal(database) else "it was rewritten during every attempt")
            raise AssertionError(f"{database} did not hold still for one read in {patience:g}s; {state}")
        time.sleep(_PAUSE)
