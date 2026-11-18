"""The product's background loops survive errors, and the scheduler reports
its liveness (audit 2026-11-10, F-05).

``ProductScheduler.start()`` ran ``tick()`` with no exception guard, and
``store.entries()`` and the ``complete_claim()`` in ``tick()``'s finally block
sit outside ``tick()``'s per-entry handler. One ``OSError`` from either ended
the thread: every tenant's schedules stopped until the process was restarted,
and ``GET /v1/ready`` kept answering 200 with ``schedule_store: true``
(``audit/repro/r7_scheduler_lifecycle.py``, part b). The research scheduler in
``agent.py`` has had a guard around its tick all along.

The sweep found the same shape in the other two loops of the product host.
The audit and retention monitors call their log sink outside any handler, so
a sink that raised once (a full disk under stdout) ended their threads too,
and readiness went on reporting the last result each had recorded. A loop
started again after ``stop()`` ran nothing, and one started while its stop
was still in flight returned as if it had started.

These tests run every loop on its real thread: the scheduler through
conftest's ``product_scheduler`` harness, the monitors over scripted
collaborators.

- No error ends a loop: not one from the schedule store at any call, not one
  from a log sink, and not any sequence of them (a property test).
- A run whose completion could not be recorded keeps its lease and runs again
  when the lease expires. Before, its key stayed claimed in memory for good.
- A claim the store refuses fails its tick. Before, it failed nothing and
  recorded nothing, so a store that could be read but not written stopped
  every schedule while the loop looked idle.
- The scheduler reports its liveness: whether its thread is alive, when it
  last completed a tick, and whether it has made progress within its stall
  bound. Readiness, both metrics formats and the ``scheduler_not_running``
  alert read that one report, and ``product_main`` wires it into the service.
- The guard at the end holds every class of the product host that starts a
  thread to the same rules, and fails when a new one is not listed.
"""

import collections
import errno
import hashlib
import itertools
import json
import os
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import example, given, settings, strategies as st

from conftest import (PI_ROOT, SCHEDULER_TENANT, ManualClock,
                      ProductSchedulerHandle, ScriptedTurnAgent)
from test_product_scheduler_lifecycle import (PRODUCT_HOST_MODULES,
                                              _classes_that_start_a_thread)

import product_main
from product_service import (
    AuditVerificationMonitor,
    AuthRegistry,
    ProductRetentionMonitor,
    ProductScheduler,
    ProductScheduleStore,
    ProductService,
    _internal_session,
    render_prometheus_metrics,
)

HOUR_S = 3600
HOUR_MS = HOUR_S * 1000
OPS_TOKEN = "background-loop-operator-token-with-test-only-entropy"
PLANNER_TOKEN = "background-loop-planner-token-with-test-only-entropy"
PLANNER = "planner"

# What a schedule store can raise at the scheduler: the audit's own example
# (EMFILE), the full disk its impact paragraph names, the store's own
# corruption error, and an error that is neither OSError nor RuntimeError.
STORE_ERRORS = {
    "too many open files": OSError(errno.EMFILE, "Too many open files"),
    "full disk": OSError(errno.ENOSPC, "No space left on device"),
    "corrupt store": RuntimeError("product schedule store is corrupt"),
    "out of memory": MemoryError(),
}
SCHEDULER_REPORT = {
    "healthy", "thread_alive", "ticks_total", "tick_failures_total",
    "last_tick_unix", "last_tick_age_seconds", "last_progress_age_seconds",
    "stall_after_seconds",
}


@pytest.fixture(autouse=True)
def _a_thread_may_end_on_system_exit(monkeypatch):
    """SystemExit is how these tests end a loop's thread on purpose: it is the
    one thing no ``except Exception`` catches. Python itself ignores it in a
    thread; pytest reports every exception that ends one. Pass that one by,
    and leave every other exception that ends a thread reported."""
    reported = threading.excepthook

    def hook(args):
        if args.exc_type is not SystemExit:
            reported(args)

    monkeypatch.setattr(threading, "excepthook", hook)


def _entry(token, principal, tenant, scopes, tools=()):
    return {"sha256": hashlib.sha256(token.encode()).hexdigest(),
            "principal": principal, "tenant": tenant, "scopes": list(scopes),
            "tools": list(tools)}


def _auth():
    """The harness's scheduling principal, and an operator to ask readiness."""
    return AuthRegistry([
        _entry(PLANNER_TOKEN, PLANNER, SCHEDULER_TENANT,
               ["chat", "schedules:read", "schedules:write"], ["read_file"]),
        _entry(OPS_TOKEN, "operator", "operations", ["ops:read"]),
    ])


def _ops():
    return {"Authorization": f"Bearer {OPS_TOKEN}"}


def _ready(service):
    status, _, body = service.dispatch("GET", "/v1/ready", _ops())
    return status, body


def _metrics(service):
    """``GET /v1/metrics`` as a client receives it: through JSON."""
    status, _, body = service.dispatch("GET", "/v1/metrics", _ops())
    assert status == 200, body
    return json.loads(json.dumps(body))["metrics"]


def _prometheus(service):
    status, _, payload = service.dispatch("GET", "/v1/metrics/prometheus", _ops())
    assert status == 200, payload
    return payload.body


class _Failing:
    """Make ``target.<name>`` raise ``error`` on its next ``times`` calls
    (every call when ``times`` is None, until ``stop()``), then call through."""

    def __init__(self, target, name, error, times=1):
        self.left = float("inf") if times is None else times
        self.raised = 0
        real = getattr(target, name)

        def call(*args, **kwargs):
            if self.left > 0:
                self.left -= 1
                self.raised += 1
                raise error
            return real(*args, **kwargs)

        setattr(target, name, call)

    def stop(self):
        self.left = 0


def _wait(scheduler, thread, predicate, what, timeout=5.0):
    """Wait for predicate(). A scheduler thread that has ended fails the wait
    at once, with what escaped tick(): that is the defect, and waiting out
    the timeout would only hide it behind a slower message."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if not thread.is_alive():
            raise AssertionError(
                f"the scheduler thread ended before {what}. It had completed "
                f"{scheduler.ticks} tick(s), and these escaped tick(): "
                f"{scheduler.tick_errors!r}. One error must not stop every "
                f"tenant's schedules until the process restarts (audit F-05)")
        if time.monotonic() >= deadline:
            raise AssertionError(f"not within {timeout}s: {what}")
        time.sleep(0.005)


def _ticks(scheduler, thread, more):
    """The thread completes ``more`` further ticks."""
    target = scheduler.ticks + more
    _wait(scheduler, thread, lambda: scheduler.ticks >= target,
          f"{more} more tick(s) completed")


# ── one error does not end the scheduler ────────────────────────────────


@pytest.mark.parametrize("error", sorted(STORE_ERRORS))
def test_one_error_reading_the_schedule_store_does_not_end_the_scheduler(
        product_scheduler, error):
    """r7 part (b) on the harness: the tick's read of the store raises once.
    The thread runs on, and the due entry runs on the next tick."""
    scheduler = product_scheduler()
    scheduler.add("job", every_ms=HOUR_MS)
    failed = _Failing(scheduler.store, "entries", STORE_ERRORS[error])

    thread = scheduler.start()
    _wait(scheduler, thread, lambda: len(scheduler.agent.turns) == 1,
          "the due entry ran on a tick after the one that failed")
    _wait(scheduler, thread,
          lambda: (scheduler.schedule("job") or {}).get("last_status") == "ok",
          "the run was recorded")
    _ticks(scheduler, thread, 2)

    assert failed.raised == 1 and len(scheduler.tick_errors) == 1
    assert thread.is_alive()
    assert len(scheduler.agent.turns) == 1, "the entry ran more than once"


@pytest.mark.parametrize("error", sorted(STORE_ERRORS))
def test_a_run_that_could_not_be_recorded_keeps_its_lease_and_runs_again_after_it(
        product_scheduler, error):
    """complete_claim() rewrites the store after the turn, in tick()'s finally
    block, so a full disk during a scheduled turn lands here. The thread runs
    on. The run is not recorded, so its claim stands and nothing runs the
    entry twice while it does; when the lease expires the entry is due again
    and runs, in this same process. Before, the entry's key was never released
    in memory, so it could not run again even if the loop had survived."""
    scheduler = product_scheduler(lease_s=60)
    scheduler.add("job", every_ms=HOUR_MS)
    failed = _Failing(scheduler.store, "complete_claim", STORE_ERRORS[error])

    thread = scheduler.start()
    _wait(scheduler, thread, lambda: failed.raised == 1,
          "recording the first run had failed")
    _ticks(scheduler, thread, 3)
    stored = scheduler.stored("job")
    assert stored["last_run"] is None and stored["last_status"] is None
    assert stored["lease_owner"] == scheduler.scheduler.owner
    assert len(scheduler.agent.turns) == 1, "the entry ran again while still leased"

    scheduler.clock.advance(61)
    _wait(scheduler, thread, lambda: len(scheduler.agent.turns) == 2,
          "the entry ran again once its lease had expired")
    _wait(scheduler, thread,
          lambda: (scheduler.schedule("job") or {}).get("last_status") == "ok",
          "the second run was recorded")
    assert "lease_owner" not in scheduler.stored("job")
    assert thread.is_alive() and len(scheduler.tick_errors) == 1


def test_a_claim_the_store_refuses_fails_the_tick_but_not_the_other_entries(
        product_scheduler):
    """claim_due() rewrites the store, so a store that can be read but not
    written (a full disk again) fails here, for every due entry, on every
    tick. Nothing was claimed, so there is no run to record as state, and the
    failure left no trace at all: no status, no event, no error out of
    tick(). The loop survived it and reported nothing, which is the finding's
    second half. The tick now fails as a whole, but only after it has visited
    every entry, so an entry the store refuses does not hold up another's."""
    logs = []
    scheduler = product_scheduler()
    loop = scheduler.scheduler
    loop.log_sink = logs.append
    scheduler.add("refused", every_ms=HOUR_MS)      # the store lists it first
    scheduler.add("served", every_ms=HOUR_MS)
    refused_key = ProductScheduleStore._key(SCHEDULER_TENANT, "refused")
    claim = scheduler.store.claim_due
    refusals = []
    refusing = [True]

    def claim_due(key, *args, **kwargs):
        if refusing[0] and key == refused_key:
            refusals.append(key)
            raise OSError(errno.ENOSPC, "No space left on device")
        return claim(key, *args, **kwargs)

    scheduler.store.claim_due = claim_due
    thread = scheduler.start()
    _wait(scheduler, thread, lambda: len(refusals) >= 3,
          "the store had refused the claim on three ticks")
    _ticks(scheduler, thread, 1)

    served = [turn.session for turn in scheduler.agent.turns]
    assert served == [_internal_session(SCHEDULER_TENANT, "served")], (
        "the entry behind the refused one did not run exactly once")
    assert scheduler.schedule("served")["last_status"] == "ok"
    assert len(scheduler.tick_errors) >= 3, (
        f"the store refused a due entry's claim on {len(refusals)} ticks and "
        f"{len(scheduler.tick_errors)} of them failed: a claim that cannot be made "
        f"leaves no status, no event and no metric (audit F-05)")
    _wait(scheduler, thread, lambda: loop.snapshot()["tick_failures_total"] >= 3,
          "the scheduler had counted three failed ticks")
    assert loop.snapshot()["ticks_total"] == 0
    assert logs == [{"event": "scheduler_tick", "severity": "critical", "status": "failed"}]

    refusing[0] = False
    _wait(scheduler, thread, lambda: len(scheduler.agent.turns) == 2,
          "the refused entry ran once the store accepted its claim")
    _wait(scheduler, thread, lambda: len(logs) == 2, "the recovery was reported")
    assert scheduler.schedule("refused")["last_status"] == "ok"
    assert logs[1]["status"] == "ok" and logs[1]["failed_ticks"] >= 3
    assert loop.is_healthy() is True


def test_a_store_that_fails_for_a_while_and_recovers_leaves_the_scheduler_running(
        product_scheduler):
    """The error need not be a single one. Every tick fails while the store
    is unreadable, nothing runs, and the first tick after it recovers runs
    what is due. The scheduler counts exactly the ticks that failed."""
    scheduler = product_scheduler()
    scheduler.add("job", every_ms=HOUR_MS)
    failed = _Failing(scheduler.store, "entries",
                      OSError(errno.EIO, "Input/output error"), times=None)

    thread = scheduler.start()
    _wait(scheduler, thread, lambda: failed.raised >= 5, "five ticks in a row had failed")
    assert scheduler.agent.turns == []

    failed.stop()
    _wait(scheduler, thread, lambda: len(scheduler.agent.turns) == 1,
          "the due entry ran once the store answered again")
    loop = scheduler.scheduler
    _wait(scheduler, thread, lambda: loop.snapshot()["ticks_total"] >= 2,
          "two ticks had completed since")
    report = loop.snapshot()
    assert report["tick_failures_total"] == failed.raised == len(scheduler.tick_errors)
    assert report["healthy"] is True


def test_a_run_of_failed_ticks_is_reported_once_and_without_its_text(
        product_scheduler, capfd):
    """A failed tick is a content-free critical event, not a traceback: the
    exception's text can carry a path or a tenant's data. A run of failures
    is one event, not one a second for as long as a disk stays full, and the
    tick that ends the run says how long it was."""
    logs = []
    scheduler = product_scheduler()
    scheduler.scheduler.log_sink = logs.append
    failed = _Failing(
        scheduler.store, "entries",
        OSError(errno.EMFILE, "/state/private-tenant-path: too many open files"),
        times=3)

    thread = scheduler.start()
    _wait(scheduler, thread, lambda: failed.raised == 3, "three ticks in a row had failed")
    _wait(scheduler, thread, lambda: len(logs) >= 2, "the recovery was reported")
    _ticks(scheduler, thread, 3)

    assert logs == [
        {"event": "scheduler_tick", "severity": "critical", "status": "failed"},
        {"event": "scheduler_tick", "severity": "info", "status": "ok",
         "failed_ticks": 3},
    ]
    out, err = capfd.readouterr()
    assert "private-tenant-path" not in out + err
    assert "Traceback" not in err


def test_a_log_sink_that_raises_does_not_end_the_scheduler(product_scheduler):
    """The failure report goes to a sink that can fail for the same reason
    the tick did. Reporting must not end the loop the guard just saved."""
    scheduler = product_scheduler()
    scheduler.add("job", every_ms=HOUR_MS)

    def full_disk(record):
        raise OSError(errno.ENOSPC, "No space left on device")

    scheduler.scheduler.log_sink = full_disk
    failed = _Failing(scheduler.store, "entries",
                      OSError(errno.ENOSPC, "No space left on device"), times=2)

    thread = scheduler.start()
    _wait(scheduler, thread, lambda: len(scheduler.agent.turns) == 1,
          "the due entry ran after the failed ticks")
    _ticks(scheduler, thread, 2)
    assert thread.is_alive() and failed.raised == 2
    assert scheduler.scheduler.snapshot()["tick_failures_total"] == 2


STORE_CALLS = ("entries", "claim_due", "get", "complete_claim")
FAILURE_PLANS = st.lists(
    st.tuples(st.sampled_from(STORE_CALLS), st.integers(min_value=0, max_value=4),
              st.sampled_from(sorted(STORE_ERRORS))),
    max_size=6)


def test_no_sequence_of_schedule_store_errors_ends_the_scheduler(tmp_path):
    """The class, not the two calls the audit named: whichever of the store
    calls the scheduler makes fails, at whichever call, with whichever error,
    the thread is alive afterwards, it has counted exactly the ticks that
    failed, and once the store answers again every entry runs and is
    recorded. A store call added outside tick()'s handlers later is covered
    without a new test."""
    examples = itertools.count()

    @settings(max_examples=25, deadline=None)
    @given(plan=FAILURE_PLANS, entries=st.integers(min_value=1, max_value=3))
    @example(plan=[("entries", 0, "too many open files")], entries=1)   # r7 part (b)
    @example(plan=[("complete_claim", 0, "full disk")], entries=1)
    @example(plan=[("claim_due", 0, "corrupt store"), ("get", 0, "out of memory"),
                   ("complete_claim", 1, "full disk"), ("entries", 3, "full disk")],
             entries=3)
    def run(plan, entries):
        scheduler = ProductSchedulerHandle(
            tmp_path / f"example-{next(examples)}", agent=ScriptedTurnAgent(),
            clock=ManualClock(), interval_s=0.005, lease_s=60)
        try:
            names = [f"job{index}" for index in range(entries)]
            for name in names:
                scheduler.add(name, every_ms=HOUR_MS)
            planned = {(call, nth): STORE_ERRORS[error] for call, nth, error in plan}
            calls = collections.Counter()
            armed = [True]
            for call in STORE_CALLS:
                def flaky(*args, _call=call, _real=getattr(scheduler.store, call),
                          **kwargs):
                    nth = calls[_call]
                    calls[_call] += 1
                    if armed[0] and (_call, nth) in planned:
                        raise planned[(_call, nth)]
                    return _real(*args, **kwargs)
                setattr(scheduler.store, call, flaky)

            thread = scheduler.start()
            _ticks(scheduler, thread, 8)
            armed[0] = False
            # Past every lease a failed completion left, and every interval.
            now = scheduler.clock.advance(HOUR_S + 61)

            def settled():
                stored = [scheduler.stored(name) for name in names]
                return all(entry["last_status"] == "ok" and entry["last_run"] == now
                           and "lease_owner" not in entry for entry in stored)

            _wait(scheduler, thread, settled,
                  "every entry ran and was recorded once the store answered again")
            loop = scheduler.scheduler
            _wait(scheduler, thread,
                  lambda: loop.snapshot()["tick_failures_total"] == len(scheduler.tick_errors),
                  f"the scheduler counted the {len(scheduler.tick_errors)} tick(s) that "
                  f"failed")
            assert thread.is_alive() and loop.is_healthy() is True
        finally:
            scheduler.close()

    run()


# ── the scheduler reports its liveness ──────────────────────────────────


def test_a_scheduler_that_is_not_running_is_not_healthy(product_scheduler):
    """Thread alive is half of liveness. A scheduler that was never started
    runs nothing, and neither does one that has stopped: neither is healthy,
    so a host that forgot to start its scheduler is not ready."""
    scheduler = product_scheduler()
    loop = scheduler.scheduler
    assert loop.is_healthy() is False
    assert loop.snapshot() == {
        "healthy": False, "thread_alive": False, "ticks_total": 0,
        "tick_failures_total": 0, "last_tick_unix": None,
        "last_tick_age_seconds": None, "last_progress_age_seconds": None,
        "stall_after_seconds": 300 + 0.01,
    }

    scheduler.start()
    scheduler.wait_for(loop.is_healthy, "the started scheduler reported itself healthy")
    assert scheduler.stop(10) is True
    assert loop.is_healthy() is False
    assert loop.snapshot()["thread_alive"] is False


def test_a_running_scheduler_reports_its_last_completed_tick(product_scheduler):
    """The other half is the age of the last completed tick. It is stamped
    with the scheduler's own clock, and its age grows on a monotonic clock
    while no tick completes."""
    scheduler = product_scheduler(interval_s=HOUR_S)   # one tick, then an hour's wait
    loop = scheduler.scheduler
    scheduler.start()
    scheduler.wait_for(lambda: loop.snapshot()["ticks_total"] == 1,
                       "the first tick completed")

    first = loop.snapshot()
    assert set(first) == SCHEDULER_REPORT
    assert first["healthy"] is True and first["thread_alive"] is True
    assert first["tick_failures_total"] == 0
    assert first["last_tick_unix"] == scheduler.clock.time()
    assert 0 <= first["last_progress_age_seconds"] <= first["last_tick_age_seconds"] < 5
    assert first["stall_after_seconds"] == 300 + HOUR_S

    time.sleep(0.05)
    later = loop.snapshot()
    assert later["last_tick_age_seconds"] >= first["last_tick_age_seconds"] + 0.04
    assert later["ticks_total"] == 1 and later["healthy"] is True


def test_a_scheduler_whose_thread_has_ended_is_not_healthy(product_scheduler):
    """Whatever ends the thread. The loop guard catches every Exception, so
    this ends it the way nothing in the product should: by SystemExit."""
    scheduler = product_scheduler()
    loop = scheduler.scheduler
    thread = scheduler.start()
    scheduler.wait_for(loop.is_healthy, "the started scheduler reported itself healthy")

    _Failing(scheduler.store, "entries", SystemExit(3))
    scheduler.wait_for(lambda: not thread.is_alive(), "the scheduler thread ended")

    report = loop.snapshot()
    assert loop.is_healthy() is False
    assert report["healthy"] is False and report["thread_alive"] is False


class _StuckAgent(ScriptedTurnAgent):
    """A turn that ignores its deadline and returns only when the test says:
    a compiler exchange that never comes back (audit F-11 was one)."""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.unstuck = threading.Event()

    def turn_with_usage(self, session, message, **kwargs):
        self.entered.set()
        self.unstuck.wait(30)
        return super().turn_with_usage(session, message, **kwargs)


def test_a_scheduler_stuck_past_its_stall_bound_is_not_healthy_until_it_moves(
        product_scheduler):
    """A live thread that does nothing is the other way to stop scheduling. A
    turn can take as long as its lease allows; a scheduler that has neither
    completed a tick nor recorded a run for longer than that is stuck."""
    agent = _StuckAgent()
    scheduler = product_scheduler(agent=agent, lease_s=1.0, turn_deadline_s=0.5)
    loop = scheduler.scheduler
    scheduler.add("stuck")
    thread = scheduler.start()
    try:
        assert agent.entered.wait(5)
        scheduler.wait_for(lambda: not loop.is_healthy(),
                           "the stuck scheduler reported itself unhealthy", timeout=10)
        report = loop.snapshot()
        assert thread.is_alive() and report["thread_alive"] is True
        assert report["healthy"] is False
        assert report["last_progress_age_seconds"] > report["stall_after_seconds"] == 1.0 + 0.01
    finally:
        agent.unstuck.set()
    scheduler.wait_for(loop.is_healthy, "the scheduler was healthy again once the turn returned")


class _SlowAgent(ScriptedTurnAgent):
    def __init__(self, seconds):
        super().__init__()
        self.seconds = seconds

    def turn_with_usage(self, session, message, **kwargs):
        time.sleep(self.seconds)
        return super().turn_with_usage(session, message, **kwargs)


def test_a_long_tick_of_many_turns_is_not_a_stalled_scheduler(product_scheduler):
    """Turns run inline, so one tick lasts as long as every turn due in it
    (audit F-06). A scheduler working through them is not stuck: each recorded
    run is progress. Judged by the age of its last completed tick alone, a
    busy scheduler would drop the whole instance out of readiness."""
    scheduler = product_scheduler(agent=_SlowAgent(0.5), lease_s=2.0, turn_deadline_s=1.9)
    loop = scheduler.scheduler
    for index in range(5):
        scheduler.add(f"job{index}", every_ms=HOUR_MS)
        scheduler.store.mark_run(ProductScheduleStore._key(SCHEDULER_TENANT, f"job{index}"),
                                 scheduler.clock.time(), "ok")
    thread = scheduler.start()
    scheduler.wait_for(lambda: loop.snapshot()["ticks_total"] >= 1,
                       "a tick with nothing due completed")

    turns = scheduler.agent.turns
    started = time.monotonic()
    scheduler.clock.advance(HOUR_S)     # all five are due at once, in one tick
    samples = []
    while not (len(turns) == 5 and turns[-1].finished is not None):
        samples.append(loop.snapshot())
        assert thread.is_alive() and time.monotonic() < started + 30
        time.sleep(0.01)

    tick_s = turns[-1].finished - started
    assert tick_s > loop.stall_after_s, (
        f"the tick took {tick_s:.2f}s, not longer than the {loop.stall_after_s}s stall "
        f"bound, so this run proved nothing")
    assert max(sample["last_tick_age_seconds"] for sample in samples) > loop.stall_after_s
    unhealthy = [sample for sample in samples if not sample["healthy"]]
    assert not unhealthy, (
        f"a scheduler running five 0.5s turns in one tick reported itself unhealthy "
        f"{len(unhealthy)} time(s), first as {unhealthy[0]}")
    assert max(sample["last_progress_age_seconds"] for sample in samples) < loop.stall_after_s


def test_one_failed_tick_does_not_make_the_scheduler_unhealthy(product_scheduler):
    """Readiness must not flap on a transient error that the next tick, a
    moment later, does not see. The failure is still counted."""
    scheduler = product_scheduler()
    loop = scheduler.scheduler
    failed = _Failing(scheduler.store, "entries", OSError(errno.EMFILE, "Too many open files"))

    thread = scheduler.start()
    samples = []
    deadline = time.monotonic() + 5
    while loop.snapshot()["ticks_total"] < 3:
        samples.append(loop.is_healthy())
        assert thread.is_alive() and time.monotonic() < deadline
        time.sleep(0.002)
    assert failed.raised == 1 and all(samples)
    assert loop.snapshot()["tick_failures_total"] == 1


@pytest.mark.parametrize("call", ["entries", "claim_due"])
def test_ticks_that_keep_failing_make_the_scheduler_unhealthy_after_its_stall_bound(
        product_scheduler, call):
    """A loop that survives every error but completes nothing is as dead to
    its tenants as one whose thread ended, whether the store cannot be read
    or a due entry cannot be claimed. No tick completing within the stall
    bound is unhealthy; the first that completes is healthy again."""
    scheduler = product_scheduler(lease_s=1.0)
    loop = scheduler.scheduler
    scheduler.add("job", every_ms=HOUR_MS)
    failed = _Failing(scheduler.store, call,
                      OSError(errno.EIO, "Input/output error"), times=None)

    thread = scheduler.start()
    _wait(scheduler, thread, lambda: failed.raised >= 1, "a tick had failed")
    scheduler.wait_for(lambda: not loop.is_healthy(),
                       "the failing scheduler reported itself unhealthy", timeout=10)
    report = loop.snapshot()
    assert thread.is_alive() and report["thread_alive"] is True
    assert report["ticks_total"] == 0 and report["tick_failures_total"] >= 1
    assert report["last_tick_unix"] is None and report["last_tick_age_seconds"] is None
    assert report["last_progress_age_seconds"] > report["stall_after_seconds"]

    failed.stop()
    scheduler.wait_for(lambda: loop.snapshot()["ticks_total"] >= 1,
                       "a tick completed once the store answered again")
    report = loop.snapshot()
    assert report["healthy"] is True
    assert report["last_tick_unix"] == scheduler.clock.time()


# ── readiness and the metrics read that report ──────────────────────────


def _service(scheduler, **kwargs):
    return ProductService(
        scheduler.agent, scheduler.auth, schedule_store=scheduler.store,
        session_locks=scheduler.locks, scheduler=scheduler.scheduler,
        log_sink=lambda record: None, **kwargs)


def test_readiness_names_the_scheduler_and_follows_its_liveness(product_scheduler):
    """The audit's observation, reversed: with the scheduler thread gone,
    GET /v1/ready answers 503 and names the scheduler, while the schedule
    store, which was never the problem, still reads true."""
    scheduler = product_scheduler(auth=_auth())
    service = _service(scheduler)
    status, body = _ready(service)
    assert status == 503 and body["dependencies"] == {
        "runtime": True, "schedule_store": True, "scheduler": False}

    thread = scheduler.start()
    scheduler.wait_for(scheduler.scheduler.is_healthy, "the scheduler was healthy")
    status, body = _ready(service)
    assert status == 200 and body["status"] == "ready"
    assert body["dependencies"] == {
        "runtime": True, "schedule_store": True, "scheduler": True}

    _Failing(scheduler.store, "entries", SystemExit(3))
    scheduler.wait_for(lambda: not thread.is_alive(), "the scheduler thread ended")
    status, body = _ready(service)
    assert status == 503 and body["status"] == "not_ready"
    assert body["dependencies"] == {
        "runtime": True, "schedule_store": True, "scheduler": False}
    assert service.is_ready() is False


def test_the_metrics_report_the_scheduler_in_both_formats(product_scheduler):
    scheduler = product_scheduler(auth=_auth())
    service = _service(scheduler)
    loop = scheduler.scheduler
    thread = scheduler.start()
    scheduler.wait_for(lambda: loop.snapshot()["ticks_total"] >= 2, "two ticks completed")

    metrics = _metrics(service)
    report = metrics["scheduler"]
    assert set(report) == SCHEDULER_REPORT
    assert report["healthy"] is True and report["thread_alive"] is True
    assert report["ticks_total"] >= 2 and report["tick_failures_total"] == 0
    assert report["last_tick_unix"] == scheduler.clock.time()
    assert metrics["dependency_components"]["scheduler"] is True
    assert metrics["dependency_ready"] is True

    exported = _prometheus(service)
    assert "\nsigil_pi_scheduler_healthy 1\n" in exported
    assert "\nsigil_pi_scheduler_thread_alive 1\n" in exported
    assert "\nsigil_pi_scheduler_tick_failures_total 0\n" in exported
    assert "\nsigil_pi_scheduler_stall_after_seconds 300.01\n" in exported
    for name in ("ticks_total", "last_tick_unix", "last_tick_age_seconds",
                 "last_progress_age_seconds"):
        assert re.search(rf"\nsigil_pi_scheduler_{name} [0-9.e+-]+\n", exported), name
    assert 'sigil_pi_dependency_component_ready{dependency="scheduler"} 1' in exported
    series = [line for line in exported.splitlines()
              if line.startswith("sigil_pi_scheduler_")]
    assert len(series) == len(SCHEDULER_REPORT)
    assert not [line for line in series if "{" in line], "scheduler series carry no label"

    _Failing(scheduler.store, "entries", SystemExit(3))
    scheduler.wait_for(lambda: not thread.is_alive(), "the scheduler thread ended")
    metrics = _metrics(service)
    assert metrics["scheduler"]["healthy"] is False
    assert metrics["scheduler"]["thread_alive"] is False
    assert metrics["dependency_components"]["scheduler"] is False
    assert metrics["dependency_ready"] is False
    exported = _prometheus(service)
    assert "\nsigil_pi_scheduler_healthy 0\n" in exported
    assert "\nsigil_pi_scheduler_thread_alive 0\n" in exported
    assert 'sigil_pi_dependency_component_ready{dependency="scheduler"} 0' in exported
    assert "\nsigil_pi_dependency_ready 0\n" in exported


def test_a_scheduler_that_has_not_ticked_exports_no_tick_age(product_scheduler):
    """Before the first tick there is no last tick to date. The exporter
    leaves those series out: a zero would read as a tick that had completed
    just now, or at the epoch."""
    scheduler = product_scheduler(auth=_auth())
    exported = _prometheus(_service(scheduler))
    assert "\nsigil_pi_scheduler_healthy 0\n" in exported
    assert "\nsigil_pi_scheduler_thread_alive 0\n" in exported
    assert "\nsigil_pi_scheduler_ticks_total 0\n" in exported
    series = {line.split()[0] for line in exported.splitlines()
              if line.startswith("sigil_pi_scheduler_")}
    assert series == {"sigil_pi_scheduler_" + name for name in (
        "healthy", "thread_alive", "ticks_total", "tick_failures_total",
        "stall_after_seconds")}


def test_a_service_given_no_scheduler_reports_none():
    """Readiness names configured components only. A service built without a
    scheduler (a library user, most tests) says nothing about one, and the
    exporter invents no series an alert could read as a measurement."""
    service = ProductService(ScriptedTurnAgent(), _auth(), log_sink=lambda record: None)
    assert service.readiness_snapshot() == {
        "ready": True, "draining": False, "dependencies": {"runtime": True}}
    metrics = service.operational_metrics()
    assert "scheduler" not in metrics
    assert "sigil_pi_scheduler_" not in render_prometheus_metrics(metrics, "1.0.0")


def test_a_scheduler_that_reports_nothing_adds_nothing():
    """Components are probed by what they offer, as every other dependency
    is: a scheduler double with neither is_healthy() nor snapshot() (the ones
    other test harnesses hand to product_main) is not a dependency."""
    class Idle:
        def start(self):
            pass

        def stop(self, timeout_s):
            return True

    service = ProductService(ScriptedTurnAgent(), _auth(), scheduler=Idle(),
                             log_sink=lambda record: None)
    assert service.readiness_snapshot()["dependencies"] == {"runtime": True}
    assert "scheduler" not in service.operational_metrics()


def _alert(alert_id):
    policy = json.loads((PI_ROOT / "config" / "alert-policy.json").read_text())
    [alert] = [entry for entry in policy["alerts"] if entry["id"] == alert_id]
    return alert


def _fires(alert, metrics):
    """Evaluate an ``equals`` alert condition against a metrics document."""
    condition = alert["condition"]
    assert condition["operator"] == "equals"
    value = metrics
    for part in condition["path"].split("."):
        value = value[part]
    return value == condition["value"]


def test_the_scheduler_alert_fires_only_while_the_scheduler_is_not_running(
        product_scheduler):
    """The alert is the policy's own condition, read from the metrics the
    product serves: quiet over a running scheduler, firing once its thread
    has ended."""
    alert = _alert("scheduler_not_running")
    assert alert["severity"] == "critical" and alert["owner_role"] == "operations"
    assert alert["for_seconds"] == 0
    assert alert["runbook"] == "docs/runbooks.md#scheduler-not-running"

    scheduler = product_scheduler(auth=_auth())
    service = _service(scheduler)
    thread = scheduler.start()
    scheduler.wait_for(scheduler.scheduler.is_healthy, "the scheduler was healthy")
    assert _fires(alert, _metrics(service)) is False

    _Failing(scheduler.store, "entries", SystemExit(3))
    scheduler.wait_for(lambda: not thread.is_alive(), "the scheduler thread ended")
    assert _fires(alert, _metrics(service)) is True


# ── product_main wires the scheduler it starts into the service ─────────


def _run_product(tmp_path, monkeypatch, during):
    """``product_main.run()`` as an operator starts it, with its own
    scheduler, monitors, stores and service, over a scripted agent and no
    compiler. ``during(product)`` runs where run() waits for its signal, with
    everything started; the product then drains and stops. An assertion that
    fails in ``during`` is raised after the drain, so no thread outlives the
    test."""
    now = int(time.time())
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": [
        {**_entry(OPS_TOKEN, "operator", "operations", ["ops:read"]),
         "not_before_unix": now - 60, "expires_unix": now + 86400},
        {**_entry(PLANNER_TOKEN, PLANNER, SCHEDULER_TENANT,
                  ["chat", "schedules:read", "schedules:write"]),
         "not_before_unix": now - 60, "expires_unix": now + 86400},
    ]}))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "provider-key-that-is-never-used")
    monkeypatch.setenv("PI_AUDIT_KEY", "background-loop-audit-key-0123456789")
    monkeypatch.setenv("PI_AUTH_FILE", str(auth))
    monkeypatch.setenv("PI_STATE", str(tmp_path / "state"))
    product = SimpleNamespace(service=None, agent=None, schedulers=[], threads=[],
                              result=None, error=None)

    class Scheduler(ProductScheduler):
        """The product's own scheduler, looking every 10 ms instead of every
        second so that the test does not wait on its cadence."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, interval_s=0.01, **kwargs)
            product.schedulers.append(self)

        def start(self):
            thread = super().start()
            product.threads.append(thread)
            return thread

    class Agent(ScriptedTurnAgent):
        manifest = {}

        def __init__(self, endpoint, api_key, **kwargs):
            super().__init__()
            self.store = kwargs["store"]
            self.sandbox_root = kwargs["sandbox_root"]
            self.audit = kwargs["audit"]
            self._mcp = kwargs["mcp"]
            product.agent = self

    class Compiler:
        is_healthy = True
        _proc = SimpleNamespace(pid=os.getpid(), kill=lambda: None)

        def initialize(self, timeout_s=None):
            return {}

        def close(self):
            pass

    class Signal:
        """Stands in for the event run() waits on for SIGTERM."""

        def set(self):
            pass

        def wait(self, timeout=None):
            try:
                product.result = during(product)
            except BaseException as error:  # noqa: BLE001  re-raised after the drain
                product.error = error
            return True

    def serve(service, **kwargs):
        product.service = service
        return SimpleNamespace(shutdown=lambda: None, server_close=lambda: None)

    monkeypatch.setattr(product_main, "threading", SimpleNamespace(Event=Signal))
    monkeypatch.setattr(
        product_main, "ProductionSigilMCP",
        SimpleNamespace(spawn=lambda path, timeout_s=90.0, generation=0: Compiler()))
    monkeypatch.setattr(
        product_main.toolchain, "resolve",
        lambda require=True: SimpleNamespace(forge_bin=Path("/nonexistent/sigil-mcp"),
                                             origin="test double"))
    monkeypatch.setattr(product_main.toolchain, "verify_binary",
                        lambda path, rev=None: None)
    monkeypatch.setattr(product_main, "PiAgent", Agent)
    monkeypatch.setattr(product_main, "ProductScheduler", Scheduler)
    monkeypatch.setattr(product_main, "serve_product", serve)
    monkeypatch.setattr(product_main, "load_system_prompt", lambda *args: None)
    product_main.run()
    if product.error is not None:
        raise product.error
    return product


def _planner(service):
    [principal] = [p for p in service.auth.principals if p.principal_id == PLANNER]
    return principal


def test_the_product_keeps_running_schedules_after_one_store_error(tmp_path, monkeypatch):
    """The reproduction's scenario on the product's own wiring: one EMFILE
    from the schedule store, then a schedule that is due. It runs, and the
    product is ready throughout."""
    def during(product):
        [thread] = product.threads
        service = product.service
        failed = _Failing(service.schedule_store, "entries",
                          OSError(errno.EMFILE, "Too many open files"))
        service.schedule_store.put(_planner(service), name="job", session="work",
                                   message="go", every_ms=HOUR_MS)
        deadline = time.monotonic() + 5
        while not (failed.raised == 1 and product.agent.turns):
            assert thread.is_alive(), (
                "after one transient error from the schedule store the product's "
                "scheduler thread was dead and the due schedule never ran (audit F-05)")
            assert time.monotonic() < deadline, "the due schedule did not run within 5s"
            time.sleep(0.005)
        return _ready(service)

    status, body = _run_product(tmp_path, monkeypatch, during).result
    assert status == 200 and body["dependencies"]["scheduler"] is True


def test_the_product_is_not_ready_once_its_scheduler_thread_has_ended(tmp_path, monkeypatch):
    """What the audit saw, from the product's own entry point: a dead
    scheduler thread and GET /v1/ready still answering 200."""
    def during(product):
        [thread] = product.threads
        service = product.service
        before = _ready(service)
        _Failing(service.schedule_store, "entries", SystemExit(3))
        deadline = time.monotonic() + 5
        while thread.is_alive():
            assert time.monotonic() < deadline, "the scheduler thread did not end"
            time.sleep(0.005)
        return before, _ready(service), _metrics(service)

    product = _run_product(tmp_path, monkeypatch, during)
    (ready_status, ready), (status, body), metrics = product.result
    assert ready_status == 200, ready
    assert status == 503, (
        f"GET /v1/ready answered {status} {body['dependencies']} with the scheduler "
        f"thread dead: nothing reports that every tenant's schedules have stopped "
        f"(audit F-05)")
    assert ready["dependencies"]["scheduler"] is True
    assert body["dependencies"] == {
        "audit_verification": True, "quota_store": True, "retention": True,
        "runtime": True, "schedule_store": True, "scheduler": False,
        "state_storage": True}
    assert metrics["scheduler"]["thread_alive"] is False
    assert metrics["scheduler"]["healthy"] is False
    assert metrics["dependency_ready"] is False
    [scheduler] = product.schedulers
    assert product.service.scheduler is scheduler, (
        "the service reports on a scheduler other than the one product_main started")


# ── every background loop of the product host ───────────────────────────
#
# One kit per class whose start() creates a thread: the loop over scripted
# collaborators, how many rounds of work it has done, a way to make its next
# round fail outside the loop's own handlers, a way to make its log sink
# fail, and a way to keep a round in flight. The guard at the end fails when
# the product host gains a loop that has no kit, so a new loop cannot ship
# without meeting the rules below.


class _Kit:
    def __init__(self):
        self.work_errors = collections.deque()
        self.sink_errors = collections.deque()
        self.sink_raised = 0
        self.work_raised = 0
        self.logs = []
        self.rounds_begun = 0
        self.gate = threading.Event()
        self.gate.set()
        self.entered = threading.Event()
        self.thread = None

    def _sink(self, record):
        if self.sink_errors:
            self.sink_raised += 1
            raise self.sink_errors.popleft()
        self.logs.append(record)

    def _work(self):
        """Called from inside one round of the loop's work."""
        self.rounds_begun += 1
        self.entered.set()
        self.gate.wait(30)

    def _next_work_error(self):
        if self.work_errors:
            self.work_raised += 1
            raise self.work_errors.popleft()

    def _clock(self):
        # Both monitors read their clock outside their own handlers.
        self._next_work_error()
        return 1000.0

    def rounds(self):
        return self.rounds_begun

    def fail_work(self, error, times=1):
        self.work_errors.extend([error] * times)

    def fail_sink(self, error):
        self.sink_errors.append(error)

    def hold(self):
        """Keep the next round in flight until release()."""
        self.entered.clear()
        self.gate.clear()

    def release(self):
        self.gate.set()

    def wait_until_in_flight(self):
        assert self.entered.wait(5), "no round of work began within 5s"

    def start(self):
        """Start the loop and return the thread it runs on."""
        before = set(threading.enumerate())
        returned = self.loop.start()
        started = [thread for thread in threading.enumerate() if thread not in before]
        self.thread = (returned if isinstance(returned, threading.Thread)
                       else started[0] if started else None)
        assert self.thread is not None, f"{type(self.loop).__name__}.start() ran no thread"
        return self.thread

    def close(self):
        self.release()
        assert self.loop.stop(10) is True, f"{type(self.loop).__name__} did not stop"


class _AuditKit(_Kit):
    component, service_keyword = "audit_verification", "audit_monitor"

    def __init__(self, product_scheduler):
        super().__init__()
        self.loop = AuditVerificationMonitor(
            self._verify, interval_s=0.01, clock=self._clock, log_sink=self._sink)

    def _verify(self):
        self._work()
        return {"ok": True, "chains": 1, "records": 2}


class _RetentionKit(_Kit):
    component, service_keyword = "retention", "retention_monitor"

    def __init__(self, product_scheduler):
        super().__init__()
        # Nothing registered is inactive, and each sweep removes one stale
        # schedule, so every sweep succeeds and logs what it removed.
        quota = SimpleNamespace(inactive_sessions=lambda cutoff: ())
        schedules = SimpleNamespace(remove_inactive=lambda cutoff, now: 1)
        self.loop = ProductRetentionMonitor(
            quota, None, schedules, None, retention_s=1, interval_s=0.01,
            clock=self._clock, precheck=self._precheck, log_sink=self._sink)

    def _precheck(self):
        self._work()
        return True


class _SchedulerKit(_Kit):
    component, service_keyword = "scheduler", "scheduler"

    def __init__(self, product_scheduler):
        super().__init__()
        self.handle = product_scheduler(auth=_auth())
        self.loop = self.handle.scheduler
        self.loop.log_sink = self._sink
        entries = self.handle.store.entries

        def flaky_entries():
            self._next_work_error()
            return entries()

        self.handle.store.entries = flaky_entries

    def rounds(self):
        return self.handle.ticks

    def fail_sink(self, error):
        # The scheduler logs when a tick fails, so make one fail.
        super().fail_sink(error)
        self.fail_work(OSError(errno.EIO, "Input/output error"))

    def hold(self):
        self.handle.agent.hold()
        self.handle.add(f"held-{len(self.handle.agent.turns)}")

    def release(self):
        self.handle.agent.release()

    def wait_until_in_flight(self):
        self.handle.agent.wait_for_turns(1)


LOOPS = {
    "AuditVerificationMonitor": _AuditKit,
    "ProductRetentionMonitor": _RetentionKit,
    "ProductScheduler": _SchedulerKit,
}
MONITORS = ("AuditVerificationMonitor", "ProductRetentionMonitor")


@pytest.fixture()
def loops(product_scheduler):
    kits = []

    def build(name):
        kit = LOOPS[name](product_scheduler)
        kits.append(kit)
        return kit

    yield build
    for kit in kits:
        kit.close()


def _until(kit, predicate, what, timeout=5.0):
    """Wait for predicate(). A loop whose thread has ended fails at once."""
    deadline = time.monotonic() + timeout
    while not predicate():
        assert kit.thread.is_alive(), (
            f"{type(kit.loop).__name__}'s thread ended after {kit.rounds()} round(s) "
            f"of work, before {what}. Nothing restarts it, so its work has stopped "
            f"for good (audit F-05)")
        assert time.monotonic() < deadline, (
            f"{type(kit.loop).__name__}: not within {timeout}s: {what}")
        time.sleep(0.005)


def _rounds(kit, more=1):
    """The loop begins ``more`` further rounds of work."""
    target = kit.rounds() + more
    _until(kit, lambda: kit.rounds() >= target, f"{more} more round(s) of work began")


ESCAPING_ERRORS = {
    "OSError": OSError(errno.ENOSPC, "No space left on device"),
    "RuntimeError": RuntimeError("an unexpected failure"),
    "MemoryError": MemoryError(),
}


@pytest.mark.parametrize("error", sorted(ESCAPING_ERRORS))
@pytest.mark.parametrize("name", sorted(LOOPS))
def test_an_error_that_escapes_one_round_does_not_end_a_background_loop(
        loops, name, error):
    kit = loops(name)
    thread = kit.start()
    _rounds(kit, 1)

    kit.fail_work(ESCAPING_ERRORS[error])
    _until(kit, lambda: kit.work_raised == 1, "the error was raised")
    _rounds(kit, 4)
    assert thread.is_alive()


@pytest.mark.parametrize("name", sorted(LOOPS))
def test_a_log_sink_that_raises_does_not_end_a_background_loop(loops, name):
    """A full disk under stdout, for example. The loop outlives it, and what
    it had found still stands: a log line that could not be written is not a
    failed verification, sweep or tick."""
    kit = loops(name)
    thread = kit.start()
    _rounds(kit, 1)

    kit.fail_sink(OSError(errno.ENOSPC, "No space left on device"))
    _until(kit, lambda: kit.sink_raised == 1, "the log sink was called and raised")
    _rounds(kit, 4)
    assert thread.is_alive()
    assert kit.loop.is_healthy() is True


@pytest.mark.parametrize("name", MONITORS)
def test_a_monitor_keeps_its_verdict_when_its_log_sink_raises(loops, name):
    """Every event a monitor logs is lost here, round after round. Its checks
    go on, none is counted as a failure, and readiness keeps the verdict."""
    kit = loops(name)
    thread = kit.start()
    _rounds(kit, 1)

    for _ in range(5):
        kit.fail_sink(OSError(errno.ENOSPC, "No space left on device"))
    _until(kit, lambda: kit.sink_raised == 5, "five events in a row were lost")
    _rounds(kit, 3)
    assert thread.is_alive()
    report = kit.loop.snapshot()
    assert report["healthy"] is True and report["failures_total"] == 0
    assert report["checks_total"] >= 6


@pytest.mark.parametrize("name", MONITORS)
def test_a_monitor_round_that_fails_outside_its_handlers_fails_closed(loops, name):
    """An error the loop guard caught is not a silent pass. The round did
    not complete, so the monitor is unhealthy and has counted a failure,
    until a round completes again."""
    kit = loops(name)
    thread = kit.start()
    _until(kit, kit.loop.is_healthy, "the monitor's first rounds passed")
    before = kit.loop.snapshot()

    kit.fail_work(OSError(errno.EIO, "Input/output error"), times=10 ** 6)
    _until(kit, lambda: kit.work_raised >= 3, "three rounds in a row had failed")
    assert thread.is_alive()
    report = kit.loop.snapshot()
    assert kit.loop.is_healthy() is False and report["healthy"] is False
    assert report["failures_total"] >= before["failures_total"] + 2

    kit.work_errors.clear()
    _until(kit, kit.loop.is_healthy, "the monitor was healthy again once a round completed")


@pytest.mark.parametrize("order", ["stopped before it was started", "stopped and restarted"])
@pytest.mark.parametrize("name", sorted(LOOPS))
def test_a_background_loop_started_after_a_stop_runs(loops, name, order):
    """stop() left the scheduler's and the retention monitor's stop flag set,
    so the next start() returned a thread that exited at once: no work, no
    error (found by T2's harness)."""
    kit = loops(name)
    if order == "stopped and restarted":
        kit.start()
        _rounds(kit, 1)
    assert kit.loop.stop(10) is True

    thread = kit.start()
    _rounds(kit, 3)
    assert thread.is_alive()


@pytest.mark.parametrize("name", sorted(LOOPS))
def test_a_background_loop_refuses_to_start_while_it_is_still_stopping(loops, name):
    """A stop() that timed out leaves the old thread finishing its round.
    start() then used to return as if it had started, and the loop ended a
    moment later. It refuses, loudly, until the old thread has ended."""
    kit = loops(name)
    kit.hold()
    first = kit.start()
    kit.wait_until_in_flight()
    assert kit.loop.stop(0) is False

    with pytest.raises(RuntimeError, match="still stopping"):
        kit.loop.start()

    kit.release()
    assert kit.loop.stop(10) is True and not first.is_alive()
    second = kit.start()
    _rounds(kit, 2)
    assert second is not first and second.is_alive()


@pytest.mark.parametrize("name", sorted(LOOPS))
def test_a_background_loop_whose_thread_has_ended_is_not_ready(loops, name):
    """The finding's second half, for every loop: a thread that ended without
    being asked to stop is reported, whatever the loop last found."""
    kit = loops(name)
    thread = kit.start()
    _rounds(kit, 2)
    service = ProductService(ScriptedTurnAgent(), _auth(), log_sink=lambda record: None,
                             **{kit.service_keyword: kit.loop})
    _until(kit, lambda: service.readiness_snapshot()["dependencies"][kit.component],
           f"{kit.component} was ready")

    kit.fail_work(SystemExit(3))
    deadline = time.monotonic() + 5
    while thread.is_alive():
        assert time.monotonic() < deadline, "the loop's thread did not end"
        time.sleep(0.005)

    assert kit.loop.is_healthy() is False, (
        f"{name}'s thread has ended and it still reports itself healthy")
    snapshot = service.readiness_snapshot()
    assert snapshot["dependencies"][kit.component] is False and snapshot["ready"] is False
    assert kit.loop.snapshot()["healthy"] is False


def test_every_background_loop_of_the_product_host_is_held_to_these_rules():
    found = set()
    for module in PRODUCT_HOST_MODULES:
        found |= _classes_that_start_a_thread(PI_ROOT / module)

    assert found, "the scan found no background loop at all; it no longer matches the code"
    assert found == set(LOOPS), (
        f"these product host classes start a thread: {sorted(found)}; the tests above "
        f"run {sorted(LOOPS)}. Give every new loop a kit, so that it too is shown to "
        f"outlive an error, restart after a stop and report a thread that has ended.")
