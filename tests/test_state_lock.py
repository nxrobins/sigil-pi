"""The single-instance guard (issue #20).

Every long-lived concern in this host is in-process and on local disk, and
every one of its safety properties is scoped to ONE process: the scheduler's
no-overlap guard is a per-instance set (`Scheduler._running`), the per-session
locks are a per-instance dict, and the audit chain is one writer's record. Two
hosts pointed at the same PI_STATE would each see the same due schedule
entries and each fire them — the tested promise that "a turn does not stack
with itself" (test_scheduler.py) holds within a host and silently not across
two.

The repo's instinct for this shape is a loud refusal at startup, not a
degraded run: an unconfigured secret is a -403, a flipped memory scope is a
startup error naming the migration. So: a second live host on the same state
directory is REFUSED, naming the holder.

flock, not O_EXCL: an advisory lock dies with its process, so a crashed host
leaves no stale lockfile to teach operators the delete-the-lock reflex —
which is how stale-lock schemes end up protecting nothing.
"""
import os
import subprocess
import sys

import pytest

from agent import StateLockHeld, acquire_state_lock
from conftest import PI_ROOT


def test_the_lock_is_exclusive_while_held(tmp_path):
    held = acquire_state_lock(tmp_path)
    try:
        with pytest.raises(StateLockHeld):
            acquire_state_lock(tmp_path)
    finally:
        held.close()


def test_refusal_names_the_holder_and_the_state_dir(tmp_path):
    """A refusal an operator cannot act on is a bug report, not a guard: it
    must say WHO holds the state (pid) and WHICH state, because the operator's
    next question is 'is that process real, and did I mean this directory'."""
    held = acquire_state_lock(tmp_path)
    try:
        with pytest.raises(StateLockHeld) as e:
            acquire_state_lock(tmp_path)
        assert str(os.getpid()) in str(e.value)
        assert str(tmp_path) in str(e.value)
    finally:
        held.close()


def test_release_frees_the_lock(tmp_path):
    acquire_state_lock(tmp_path).close()
    second = acquire_state_lock(tmp_path)  # must not raise
    second.close()


def test_a_crashed_holder_leaves_no_stale_lock(tmp_path):
    """THE reason this is flock and not an O_EXCL pidfile. A host that dies
    without cleanup must not brick its state directory: the kernel drops an
    advisory lock with the process, so the next start just works. A stale-file
    scheme fails here, and its workaround — operators deleting lockfiles on
    sight — is how two live hosts end up sharing state anyway."""
    code = (
        "import sys; sys.path.insert(0, sys.argv[2]);"
        "from agent import acquire_state_lock;"
        "acquire_state_lock(sys.argv[1]);"
        "import os; os._exit(1)"  # dies holding the lock, no cleanup
    )
    subprocess.run([sys.executable, "-c", code, str(tmp_path), str(PI_ROOT)],
                   check=False)
    held = acquire_state_lock(tmp_path)  # must not raise
    held.close()


def test_a_live_holder_in_another_process_is_refused(tmp_path):
    """The real deployment mistake, reproduced: a second host process on the
    same PI_STATE while the first is still alive."""
    code = (
        "import sys; sys.path.insert(0, sys.argv[2]);"
        "from agent import acquire_state_lock;"
        "lk = acquire_state_lock(sys.argv[1]);"
        "print('HELD', flush=True);"
        "sys.stdin.readline()"  # hold until we say so
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path), str(PI_ROOT)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "HELD"
        with pytest.raises(StateLockHeld):
            acquire_state_lock(tmp_path)
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
    held = acquire_state_lock(tmp_path)  # released with the process
    held.close()


def test_the_state_dir_is_created_if_missing(tmp_path):
    held = acquire_state_lock(tmp_path / "fresh" / "state")
    held.close()


def test_verify_audit_does_not_take_the_lock():
    """An auditor should be able to check the chains while the host is UP —
    --verify-audit is read-only file walking, and gating it on the lock would
    mean the one time you most want to verify (something looks wrong on a
    live host) is the one time you cannot. Structural: the verify branch must
    exit before the lock is acquired."""
    src = (PI_ROOT / "agent.py").read_text()
    body = src.split("def main():", 1)[1]
    verify_at = body.index("--verify-audit")
    lock_at = body.index("acquire_state_lock(")
    assert verify_at < lock_at, (
        "--verify-audit must be handled before the state lock is taken, so "
        "auditing works while a host is running")


def test_main_locks_before_touching_state():
    """The guard is worthless if any state consumer starts before it: the
    lock must be acquired before the SessionStore, the scheduler, or the
    audit log are constructed."""
    src = (PI_ROOT / "agent.py").read_text()
    body = src.split("def main():", 1)[1]
    lock_at = body.index("acquire_state_lock(")
    store_at = body.index("SessionStore(")
    assert lock_at < store_at, (
        "main() must hold the state lock before constructing state consumers")
