"""The deadline loop on the cross-worker session lock.

The product's workers serialize the operations on one session through a
lock file per session (SessionOperationLocks with a lock directory): every
chat turn takes it. A turn that has a deadline polls for that file lock and
gives up when its deadline passes. No test ran that loop (audit 2026-11-10,
F-17), although production takes it on every chat turn. These tests run it
against another worker that is another process, holding the session through
its own SessionOperationLocks:

- the deadline ends the wait, after the deadline and not long after it, and
  the waiting worker is left holding nothing;
- a worker that is still within its deadline gets the session as soon as the
  other worker lets it go, and one whose deadline has already passed takes
  nothing;
- a chat turn whose deadline passes there is answered 504 without reaching
  the agent, and the product serves the session again afterwards.
"""

import hashlib
import json
import subprocess
import sys
import threading
import time

import pytest

from conftest import PI_ROOT
from product_service import AuthRegistry, ProductService, SessionOperationLocks, _internal_session

SESSION = "tenant-a:shared-session"
TOKEN = "cross-worker-lock-chat-token-with-enough-entropy-for-a-test"

_OTHER_WORKER = """
import sys
sys.path.insert(0, sys.argv[1])
from product_service import SessionOperationLocks
with SessionOperationLocks(sys.argv[2]).hold(sys.argv[3]):
    print("held", flush=True)
    sys.stdin.readline()
print("released", flush=True)
"""


class OtherWorker:
    """Another product worker, in its own process, holding one session."""

    def __init__(self, lock_dir, session):
        self.process = subprocess.Popen(
            [sys.executable, "-c", _OTHER_WORKER, str(PI_ROOT), str(lock_dir), session],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        line = self.process.stdout.readline()
        assert line == "held\n", f"the other worker did not take the session: {line!r}"

    def release(self):
        if self.process.poll() is None:
            self.process.stdin.write("\n")
            self.process.stdin.flush()
            assert self.process.stdout.readline() == "released\n"
        self.process.wait(timeout=30)


@pytest.fixture()
def other_worker(tmp_path):
    started = []

    def start(session=SESSION):
        worker = OtherWorker(tmp_path / "locks", session)
        started.append(worker)
        return worker

    yield start
    for worker in started:
        worker.release()


def _hold_in_thread(locks, session, deadline):
    """Run `locks.hold(session, deadline_monotonic=deadline)` on a thread;
    returns the thread and a dict that ends up with what happened."""
    outcome = {}

    def run():
        started = time.monotonic()
        try:
            with locks.hold(session, deadline_monotonic=deadline):
                outcome["held_after"] = time.monotonic() - started
        except BaseException as raised:  # noqa: BLE001 - reported below
            raised_at = time.monotonic()
            outcome["raised"] = raised
            outcome["raised_at"] = raised_at
            outcome["raised_after"] = raised_at - started

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def test_the_deadline_ends_the_wait_for_a_session_another_worker_holds(
        tmp_path, other_worker):
    worker = other_worker()
    locks = SessionOperationLocks(tmp_path / "locks")
    budget = 0.3

    deadline = time.monotonic() + budget
    thread, outcome = _hold_in_thread(locks, SESSION, deadline)
    thread.join(budget + 10)
    if thread.is_alive():
        worker.release()
        thread.join(10)
        pytest.fail("the deadline did not end the wait; the other worker had to let go")

    assert "held_after" not in outcome, "took a session another worker holds"
    raised = outcome["raised"]
    assert isinstance(raised, TimeoutError), repr(raised)
    assert "cross-worker session lock" in str(raised), str(raised)
    # Against the deadline itself: the thread starts after the deadline is set,
    # so time measured from the thread's start can fall short of the budget by
    # the start-up latency although the wait ended on time.
    assert outcome["raised_at"] >= deadline, (
        f"gave up {deadline - outcome['raised_at']:.6f}s before its deadline")
    assert outcome["raised_after"] < budget + 5, outcome["raised_after"]
    # Nothing is left held: no registry entry, and the in-process lock was
    # released with the file, so this worker takes the session at once when
    # the other one has let go.
    assert locks.size() == 0
    worker.release()
    thread, outcome = _hold_in_thread(locks, SESSION, time.monotonic() + 5)
    thread.join(10)
    assert "held_after" in outcome, outcome
    assert locks.size() == 0


def test_a_worker_within_its_deadline_gets_the_session_when_the_other_lets_go(
        tmp_path, other_worker):
    worker = other_worker()
    locks = SessionOperationLocks(tmp_path / "locks")

    thread, outcome = _hold_in_thread(locks, SESSION, time.monotonic() + 30)
    time.sleep(0.2)
    assert thread.is_alive() and not outcome, "did not wait for the other worker"
    worker.release()
    thread.join(30)

    assert "raised" not in outcome, repr(outcome.get("raised"))
    assert outcome["held_after"] >= 0.2
    assert locks.size() == 0


def test_a_deadline_that_has_already_passed_takes_nothing(tmp_path):
    """The other end of the same deadline: a session that nobody holds is
    still refused once the deadline has passed, before any lock is tried,
    and the refusal leaves no entry behind."""
    locks = SessionOperationLocks(tmp_path / "locks")
    with pytest.raises(TimeoutError, match="waiting for session operation"):
        with locks.hold(SESSION, deadline_monotonic=time.monotonic() - 1):
            pytest.fail("held a session after its deadline had passed")
    assert locks.size() == 0
    with locks.hold(SESSION, deadline_monotonic=time.monotonic() + 5):
        assert locks.size() == 1
    assert locks.size() == 0


def test_a_chat_turn_whose_deadline_passes_on_another_workers_lock_gets_504(
        tmp_path, other_worker):
    class Agent:
        manifest = {}
        memory = None
        _mcp = object()

        def __init__(self):
            self.calls = []

        def turn_with_usage(self, session, message, **kwargs):
            self.calls.append(session)
            return "served", {"input_tokens": 1, "output_tokens": 1}

        def turn_telemetry(self):
            return {"forge_queue_wait_ms": 0.0, "tool_calls": 0, "retries": 0}

    agent = Agent()
    auth = AuthRegistry([{
        "sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
        "principal": "chat-user", "tenant": "tenant-a", "scopes": ["chat"], "tools": [],
    }])
    locks = SessionOperationLocks(tmp_path / "locks")
    service = ProductService(agent, auth, session_locks=locks, turn_deadline_s=0.3,
                             log_sink=lambda record: None)
    internal = _internal_session("tenant-a", "shared")
    worker = other_worker(internal)
    headers = {"Authorization": f"Bearer {TOKEN}", "X-Request-ID": "cross-worker-1"}
    body = json.dumps({"session": "shared", "message": "hello"}).encode()

    answered = {}
    thread = threading.Thread(daemon=True, target=lambda: answered.update(
        zip(("status", "headers", "reply"),
            service.dispatch("POST", "/v1/chat", headers, body))))
    thread.start()
    thread.join(10)
    if thread.is_alive():
        worker.release()
        thread.join(10)
        pytest.fail("the turn's deadline did not end its wait for the session")
    status, reply = answered["status"], answered["reply"]

    assert status == 504, reply
    assert reply["error"]["code"] == "turn_deadline_exceeded"
    assert agent.calls == [], "the turn reached the agent without the session"
    assert locks.size() == 0
    assert service.metrics.snapshot()["active_turns"] == 0

    worker.release()
    status, _, reply = service.dispatch("POST", "/v1/chat", headers, body)
    assert status == 200, reply
    assert agent.calls == [internal]
