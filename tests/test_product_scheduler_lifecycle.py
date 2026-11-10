"""ProductScheduler on its real thread (audit 2026-11-10, F-17).

The scheduler tests drive ProductScheduler.tick() directly, so the code that
runs it in production, start() and stop() and the loop between them, ran in
no test at all: coverage of a full gate run showed both methods never
executed. These tests start the real thread through conftest's
product_scheduler harness, with an injected clock (ManualClock) deciding what
is due and a scripted agent (ScriptedTurnAgent) standing in for the turn, and
pin what the lifecycle does today, so that the scheduler changes planned next
(C1 to C3 of the remediation graph) are measured against it.

The guard at the end finds every class in the product host whose start()
creates a thread and fails until it is listed in BACKGROUND_LOOPS, where a
test starts its real thread and stops it.
"""

import ast
import threading
import time

import pytest

from conftest import PI_ROOT, SCHEDULER_TENANT, ScriptedTurnAgent

from product_service import (
    AuditVerificationMonitor,
    ProductRetentionMonitor,
    _internal_session,
)

HOUR_S = 3600
HOUR_MS = HOUR_S * 1000


def _lease_fields(entry):
    return {key: entry[key] for key in ("lease_owner", "lease_until") if key in entry}


# ── what one due entry does ─────────────────────────────────────────────


def test_a_due_entry_runs_once(product_scheduler):
    """A new entry is due at once. The thread runs its turn, stamps the run
    with the injected clock, completes its claim, and does not run it again on
    the ticks that follow, because that clock has not moved."""
    scheduler = product_scheduler()
    scheduler.add("daily", session="work", message="report", every_ms=HOUR_MS)
    test_thread = threading.current_thread()

    thread = scheduler.start()
    [turn] = scheduler.agent.wait_for_turns(1)
    scheduler.wait_for_ticks(5)

    runs = len(scheduler.agent.turns)
    assert runs == 1, f"the entry ran {runs} times on a clock that never moved"
    assert turn.session == _internal_session(SCHEDULER_TENANT, "work")
    assert turn.message == "report"
    assert turn.allowed_tools == {"read_file"}
    assert turn.deadline_monotonic is not None and turn.finished is not None
    assert turn.thread is not test_thread, "the turn ran on the test's own thread"
    assert thread.is_alive()
    listed = scheduler.schedule("daily")
    assert listed["last_status"] == "ok"
    assert listed["last_run"] == scheduler.clock.time()
    assert _lease_fields(scheduler.stored("daily")) == {}, "the claim was never completed"


def test_the_injected_clock_alone_decides_when_an_entry_is_due_again(product_scheduler):
    """The thread keeps looking in real time, but an entry runs again only
    when the injected clock reaches its interval, and once after a long gap,
    not once per interval missed."""
    scheduler = product_scheduler()
    scheduler.add("hourly", every_ms=HOUR_MS)
    scheduler.start()
    scheduler.agent.wait_for_turns(1)
    scheduler.wait_for_ticks(1)  # the run is recorded before the clock moves
    first_run = scheduler.schedule("hourly")["last_run"]
    assert first_run == scheduler.clock.time()

    scheduler.clock.advance(HOUR_S - 1)
    scheduler.wait_for_ticks(3)
    assert len(scheduler.agent.turns) == 1, "ran again a second before its interval"

    scheduler.clock.advance(1)
    scheduler.agent.wait_for_turns(2)
    scheduler.wait_for_ticks(3)
    assert len(scheduler.agent.turns) == 2
    assert scheduler.schedule("hourly")["last_run"] == first_run + HOUR_S

    scheduler.clock.advance(10 * HOUR_S)
    scheduler.agent.wait_for_turns(3)
    scheduler.wait_for_ticks(3)
    assert len(scheduler.agent.turns) == 3, "a long gap ran the entry more than once"
    assert scheduler.schedule("hourly")["last_run"] == first_run + 11 * HOUR_S


def test_a_failing_turn_is_recorded_without_its_text_and_the_thread_runs_on(product_scheduler):
    """A turn that raises is state, not a reason to stop the loop: the run is
    recorded as failed with no exception text in the store, the thread keeps
    ticking, and the entry's next due run goes ahead."""
    scheduler = product_scheduler()
    scheduler.agent.script(RuntimeError("private diagnostic text"))
    scheduler.add("flaky")
    thread = scheduler.start()
    scheduler.agent.wait_for_turns(1)
    scheduler.wait_for_ticks(2)

    assert scheduler.schedule("flaky")["last_status"] == "failed"
    assert b"private diagnostic text" not in scheduler.store.path.read_bytes()
    assert thread.is_alive() and scheduler.tick_errors == []

    scheduler.clock.advance(HOUR_S)
    scheduler.agent.wait_for_turns(2)
    scheduler.wait_for_ticks(1)
    assert scheduler.schedule("flaky")["last_status"] == "ok"


def test_a_turn_past_its_deadline_is_recorded_as_deadline_exceeded(product_scheduler):
    """The thread gives each turn turn_deadline_s from its claim; a turn still
    running at that deadline ends with TimeoutError, as PiAgent's does, and the
    run is recorded as deadline_exceeded."""
    scheduler = product_scheduler(turn_deadline_s=0.2)
    scheduler.agent.hold()
    scheduler.add("slow")
    thread = scheduler.start()
    [turn] = scheduler.agent.wait_for_turns(1)
    scheduler.wait_for_ticks(1)

    assert scheduler.schedule("slow")["last_status"] == "deadline_exceeded"
    assert turn.deadline_monotonic - turn.started <= 0.2 + 1e-6
    assert thread.is_alive()


def test_a_schedule_deleted_while_its_claim_waits_for_the_session_runs_no_turn(
        product_scheduler):
    """Deleting a session holds its operation lock while it removes the
    session's schedules. An entry the thread claimed just before is re-read
    under that lock once the deletion lets go, and runs no final turn."""
    scheduler = product_scheduler()
    scheduler.add("daily", session="work")
    owner = scheduler.scheduler.owner

    with scheduler.locks.hold(_internal_session(SCHEDULER_TENANT, "work")):
        scheduler.start()
        scheduler.wait_for(
            lambda: (scheduler.stored("daily") or {}).get("lease_owner") == owner,
            "the thread claimed the due entry")
        assert scheduler.store.remove_session(SCHEDULER_TENANT, "work") == 1
    scheduler.wait_for_ticks(2)

    assert scheduler.agent.turns == []
    assert scheduler.schedule("daily") is None
    assert scheduler.locks.size() == 0


# ── start and stop ──────────────────────────────────────────────────────


def test_stop_on_an_idle_scheduler_returns_true(product_scheduler):
    """With nothing due, the thread waits out its interval on the stop event.
    stop() wakes it, joins it and returns True at once. The interval here is
    an hour, so a stop that waited the interval out would return False."""
    scheduler = product_scheduler(interval_s=HOUR_S)
    thread = scheduler.start()
    scheduler.wait_for(lambda: scheduler.ticks == 1, "the first tick completed")

    started = time.monotonic()
    assert scheduler.stop(timeout_s=10) is True
    elapsed = time.monotonic() - started

    assert not thread.is_alive()
    assert elapsed < 5, f"stop() took {elapsed:.1f}s on an idle scheduler"
    assert scheduler.ticks == 1 and scheduler.agent.turns == []


def test_stop_waits_for_the_turn_in_flight_and_reports_whether_it_finished(
        product_scheduler):
    """stop() does not abandon a running turn. It returns False while the
    turn is still running when its timeout ends, and True once the turn has
    finished and its run is recorded."""
    scheduler = product_scheduler()
    scheduler.add("report")
    scheduler.agent.hold()
    thread = scheduler.start()
    [turn] = scheduler.agent.wait_for_turns(1)

    assert scheduler.stop(timeout_s=0.2) is False
    assert thread.is_alive() and turn.finished is None

    scheduler.agent.release()
    assert scheduler.stop(timeout_s=10) is True
    assert not thread.is_alive() and turn.finished is not None
    assert len(scheduler.agent.turns) == 1
    assert scheduler.schedule("report")["last_status"] == "ok"
    assert _lease_fields(scheduler.stored("report")) == {}, "the claim was never completed"


def test_starting_a_running_scheduler_returns_its_thread(product_scheduler):
    """A second start() while the thread runs returns that thread instead of
    starting a second loop over the same store."""
    scheduler = product_scheduler()
    first = scheduler.start()
    scheduler.wait_for_ticks(1)

    assert scheduler.scheduler.start() is first
    assert first.is_alive()


# ── the harness itself ──────────────────────────────────────────────────


def test_a_scheduler_that_will_not_stop_fails_teardown_instead_of_hanging(product_scheduler):
    """Teardown releases held turns, stops the scheduler and joins its thread
    within a bound. A thread that still does not stop fails the test, showing
    where it is, rather than hanging the suite."""
    entered, unstuck = threading.Event(), threading.Event()

    class Unreleasable(ScriptedTurnAgent):
        def turn_with_usage(self, session, message, **kwargs):
            entered.set()
            unstuck.wait(30)  # set by this test, never by the harness
            return "late", {}

    scheduler = product_scheduler(agent=Unreleasable())
    scheduler.add("stuck")
    thread = scheduler.start()
    assert entered.wait(5)
    started = time.monotonic()
    try:
        with pytest.raises(AssertionError, match=r"still running 0\.5s after") as failure:
            scheduler.close(timeout=0.5)
        assert time.monotonic() - started < 5
        assert "turn_with_usage" in str(failure.value), "the failure must say where it is"
    finally:
        unstuck.set()
        thread.join(5)
    assert not thread.is_alive()


def test_the_harness_fails_a_wait_that_is_never_satisfied_instead_of_hanging(
        product_scheduler):
    scheduler = product_scheduler()
    started = time.monotonic()
    with pytest.raises(AssertionError, match="never started"):
        scheduler.wait_for_ticks(1, timeout=0.2)
    scheduler.start()
    with pytest.raises(AssertionError, match=r"0 scheduled turn\(s\) started within 0\.2s"):
        scheduler.agent.wait_for_turns(1, timeout=0.2)
    with pytest.raises(AssertionError, match=r"not within 0\.2s: what never happens"):
        scheduler.wait_for(lambda: False, "what never happens", timeout=0.2)
    assert time.monotonic() - started < 5


# ── every background loop of the product host ───────────────────────────
#
# How to build each class in the product host whose start() creates a thread,
# and how to see that its loop has done one round of work. The guard below
# fails when such a class is missing here, so a new loop cannot ship with a
# thread that no test has started and stopped, which is how
# ProductScheduler's went unseen.

PRODUCT_HOST_MODULES = ("product_service.py", "product_main.py", "runtime_client.py",
                        "state_tool.py")


def _audit_monitor(build_scheduler):
    monitor = AuditVerificationMonitor(
        lambda: {"ok": True, "chains": 0, "records": 0},
        interval_s=0.01, log_sink=lambda record: None)
    return monitor, lambda: monitor.snapshot()["checks_total"] >= 1


def _retention_monitor(build_scheduler):
    # A failing precheck ends every sweep before it reads any store, so this
    # loop needs none, and each sweep still counts as a check.
    monitor = ProductRetentionMonitor(
        None, None, None, None, retention_s=1, interval_s=0.01,
        precheck=lambda: False, log_sink=lambda record: None)
    return monitor, lambda: monitor.snapshot()["checks_total"] >= 1


def _scheduler(build_scheduler):
    handle = build_scheduler()
    return handle.scheduler, lambda: handle.ticks >= 1


BACKGROUND_LOOPS = {
    "AuditVerificationMonitor": _audit_monitor,
    "ProductRetentionMonitor": _retention_monitor,
    "ProductScheduler": _scheduler,
}


@pytest.mark.parametrize("name", sorted(BACKGROUND_LOOPS))
def test_every_background_loop_runs_a_thread_that_stop_ends(product_scheduler, name):
    """stop() on a loop that never started has nothing to wait for. start()
    runs a thread that does the loop's work, and stop() ends every thread
    start() began, and says so."""
    build = BACKGROUND_LOOPS[name]
    never_started, _ = build(product_scheduler)
    assert never_started.stop(0) is True

    loop, worked = build(product_scheduler)
    before = set(threading.enumerate())
    loop.start()
    started = [thread for thread in threading.enumerate() if thread not in before]
    assert started, f"{name}.start() left no thread running"
    deadline = time.monotonic() + 5
    while not worked():
        assert time.monotonic() < deadline, f"{name}'s thread did no work within 5s"
        time.sleep(0.005)

    assert loop.stop(10) is True
    assert not [thread for thread in started if thread.is_alive()]


def _classes_that_start_a_thread(path):
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.ClassDef):
            continue
        for method in node.body:
            if not (isinstance(method, ast.FunctionDef) and method.name == "start"):
                continue
            for call in ast.walk(method):
                if isinstance(call, ast.Call) and (
                        (isinstance(call.func, ast.Attribute) and call.func.attr == "Thread")
                        or (isinstance(call.func, ast.Name) and call.func.id == "Thread")):
                    found.add(node.name)
    return found


def test_every_class_that_starts_a_thread_is_a_background_loop_under_test():
    found = set()
    for module in PRODUCT_HOST_MODULES:
        found |= _classes_that_start_a_thread(PI_ROOT / module)

    assert found, "the scan found no background loop at all; it no longer matches the code"
    assert found == set(BACKGROUND_LOOPS), (
        f"these product host classes start a thread: {sorted(found)}; "
        f"BACKGROUND_LOOPS builds {sorted(BACKGROUND_LOOPS)}. Add every new loop "
        f"there, so a test starts its real thread and stops it.")
