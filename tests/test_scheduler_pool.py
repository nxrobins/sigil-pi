"""Audit F-06: one tenant's scheduled turns delayed every other tenant's.

``ProductScheduler.tick()`` claimed each due entry and ran its turn inline on
the scheduler's one thread, visiting the entries in store order, which is by
tenant hash. With one tenant holding three due schedules of 2 seconds each,
another tenant's due schedule started 6.0 seconds late
(``audit/repro/r3_scheduler_hol.py``); at the defaults, 32 schedules with a
120-second deadline each, that is an hour behind one tenant. And ``every_ms``
had no floor above 1, so a tenant could keep all 32 of its schedules due.

What these tests hold:

- A tick starts the turns of due entries on a small bounded pool of threads,
  at most ``turns_per_tenant`` (one) of a tenant's at a time. Another
  tenant's due entry starts while the first tenant's turns run, and an entry
  that falls due during a long tick starts within an interval of it.
- When the pool is full, the tenant served least recently goes first, and of
  a tenant's own entries the one due longest goes first.
- No more than ``workers`` turns run at once, and one entry never runs twice
  at once. A stop or a drain still starts nothing more, ``stop()`` waits for
  every turn in the pool, and each turn is charged its own tokens.
- Each claim is stamped with the clock as it is made, not as the tick began,
  and an idle tick reads the schedule store once, however many entries.
- The product refuses an ``every_ms`` below one minute; a schedule an earlier
  release stored below it still loads, and runs no more often than that.

``tick()`` returns once the entries that were due at its first look have
run, as the callers in tests/test_drain.py and tests/test_online_backup.py
rely on. It starts the entries that fall due meanwhile without waiting for
them, so a tick ends however busy the pool stays.
"""

import errno
import hashlib
import json
import threading
import time
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from test_product_main import _base_env, _install_happy_runtime

import product_main
import product_service
from product_service import (
    AuthRegistry,
    ConfigError,
    DurableQuotaStore,
    InFlightWork,
    Principal,
    ProductError,
    ProductScheduler,
    ProductScheduleStore,
    ProductService,
    _internal_session,
)

HOUR_MS = 3_600_000
HOUR_S = 3600
SOON_S = 2.0          # "about one tick" is 0.01 s here; this allows a loaded machine


def _tenants(*names):
    """The tenant ids in the order the schedule store lists their entries,
    which is by the SHA-256 of the tenant id."""
    return sorted(names, key=lambda name: hashlib.sha256(name.encode()).hexdigest())


def _token(tenant):
    return f"{tenant}-pool-test-token-with-enough-entropy-0123456789abcdef"


def _auth(tenants):
    return AuthRegistry([{
        "sha256": hashlib.sha256(_token(tenant).encode()).hexdigest(),
        "principal": f"planner-{tenant}",
        "tenant": tenant,
        "scopes": ["chat", "schedules:read", "schedules:write"],
        "tools": ["read_file"],
    } for tenant in tenants])


def _principal(auth, tenant):
    return auth.authenticate(f"Bearer {_token(tenant)}")


def _key(tenant, name):
    return ProductScheduleStore._key(tenant, name)


class GatedAgent:
    """A PiAgent stand-in whose turns each wait at their session's gate.

    Every turn is recorded as it starts and as it finishes, with the sessions
    in flight at the moment it started. A session's turns wait until
    ``open(session)``, or ``open()`` for every session; ``release()`` is
    ``open()``, which is what the harness calls in teardown. ``during(session)``
    runs on the turn's thread before it waits. ``spends`` maps a session to
    the tokens its turn counts on its own thread, as PiAgent's per-thread
    count does, and ``fail`` names sessions whose turn then raises."""

    manifest = {"read_file": {}, "write_file": {}}
    memory = None
    _mcp = object()

    def __init__(self, *, opened=False, hold_limit_s=10.0, during=None):
        self._changed = threading.Condition()
        self._open = set()
        self._all_open = opened
        self.hold_limit_s = hold_limit_s
        self.during = during
        self.spends = {}
        self.fail = set()
        self.started = []       # sessions, in the order their turns started
        self.finished = []
        self.running = []       # sessions whose turns are in flight now
        self.in_flight_at_start = []
        self._counted = threading.local()

    def open(self, *sessions):
        with self._changed:
            if sessions:
                self._open.update(sessions)
            else:
                self._all_open = True
            self._changed.notify_all()

    release = open

    def turn_with_usage(self, session, message, *, allowed_tools=None,
                        deadline_monotonic=None, **context):
        self._counted.usage = dict(self.spends.get(
            session, {"input_tokens": 2, "output_tokens": 1}))
        with self._changed:
            self.started.append(session)
            self.running.append(session)
            self.in_flight_at_start.append(tuple(self.running))
            self._changed.notify_all()
        try:
            if self.during is not None:
                self.during(session)
            limit = time.monotonic() + self.hold_limit_s
            with self._changed:
                while not (self._all_open or session in self._open):
                    remaining = limit - time.monotonic()
                    if remaining <= 0:
                        raise AssertionError(
                            f"a turn on {session} was held {self.hold_limit_s}s")
                    self._changed.wait(remaining)
            if session in self.fail:
                raise RuntimeError("the model call failed after it was paid for")
            return "reply", dict(self._counted.usage)
        finally:
            with self._changed:
                self.running.remove(session)
                self.finished.append(session)
                self._changed.notify_all()

    def turn_telemetry(self):
        counted = getattr(self._counted, "usage", {"input_tokens": 0, "output_tokens": 0})
        return {"forge_queue_wait_ms": 0.0, "tool_calls": 0, "retries": 0, **counted}

    def wait_until(self, predicate, timeout=5.0):
        """Whether predicate() became true within ``timeout`` seconds."""
        deadline = time.monotonic() + timeout
        with self._changed:
            while not predicate():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._changed.wait(remaining)
            return True

    def peak(self, sessions=None):
        """The most turns in flight at once, of ``sessions`` if given."""
        return max((sum(1 for s in running if sessions is None or s in sessions)
                    for running in self.in_flight_at_start), default=0)


def _in_background(function):
    errors = []

    def run():
        try:
            function()
        except BaseException as error:     # the test reports it, not the thread
            errors.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.errors = errors
    return thread


# ── F-06: one tenant's turns do not hold up another tenant's ────────────


def test_another_tenant_s_due_entry_starts_while_the_first_tenant_s_turns_run(
        product_scheduler):
    """The audit's case on the real thread. The busy tenant's three entries
    are listed first and its turns do not finish; the other tenant's due
    entry starts anyway, within about one tick, and the busy tenant's three
    still run one at a time."""
    busy, other = _tenants("tenant-busy", "tenant-other")
    auth = _auth([busy, other])
    agent = GatedAgent()
    scheduler = product_scheduler(agent=agent, auth=auth)
    for index in range(3):
        scheduler.add(f"job{index}", principal=_principal(auth, busy))
    scheduler.add("report", principal=_principal(auth, other))
    busy_sessions = {_internal_session(busy, f"job{index}") for index in range(3)}
    report = _internal_session(other, "report")

    scheduler.start()
    assert agent.wait_until(lambda: bool(busy_sessions & set(agent.started)))
    started = agent.wait_until(lambda: report in agent.started, timeout=SOON_S)
    ahead = [session for session in agent.running if session in busy_sessions]
    assert started, (
        f"the other tenant's due schedule had not started {SOON_S}s after the busy "
        f"tenant's first turn began: it waits behind {len(busy_sessions)} turns it "
        f"does not own (audit F-06)")
    assert ahead, "the busy tenant's turn had finished first, so this proved nothing"

    agent.open()
    scheduler.wait_for(
        lambda: all((scheduler.schedule(name, _principal(auth, busy)) or {}).get(
            "last_status") == "ok" for name in ("job0", "job1", "job2"))
        and scheduler.schedule("report", _principal(auth, other))["last_status"] == "ok",
        "every entry ran and was recorded")
    assert sorted(agent.started) == sorted([*busy_sessions, report])
    assert agent.peak(busy_sessions) == 1, (
        f"{agent.peak(busy_sessions)} of one tenant's turns ran at once; its cap is one")


def test_an_entry_that_falls_due_during_a_long_tick_starts_within_an_interval(
        product_scheduler):
    """A tick lasts as long as the turns it started. An entry of another
    tenant that falls due meanwhile does not wait for that tick to end: the
    tick looks again every interval while its turns run."""
    busy, other = _tenants("tenant-busy", "tenant-other")
    auth = _auth([busy, other])
    agent = GatedAgent()
    scheduler = product_scheduler(agent=agent, auth=auth)
    scheduler.add("long", principal=_principal(auth, busy))
    scheduler.add("hourly", principal=_principal(auth, other), every_ms=HOUR_MS)
    scheduler.store.mark_run(_key(other, "hourly"), scheduler.clock.time(), "ok")
    long_turn, hourly = _internal_session(busy, "long"), _internal_session(other, "hourly")

    scheduler.start()
    assert agent.wait_until(lambda: long_turn in agent.started)
    scheduler.clock.advance(HOUR_S)      # "hourly" is due now; "long" is still running
    started = agent.wait_until(lambda: hourly in agent.started, timeout=SOON_S)
    assert started, (
        f"an entry that fell due while another tenant's turn ran had not started "
        f"{SOON_S}s later: it waits for that turn's tick to end (audit F-06)")
    assert long_turn in agent.running

    agent.open()
    scheduler.wait_for(
        lambda: scheduler.schedule("long", _principal(auth, busy))["last_status"] == "ok",
        "the long turn was recorded")
    assert agent.started.count(long_turn) == 1, (
        "the entry in flight ran again once its lease had passed on the clock")


class _TickingClock:
    """A wall clock that moves one second forward each time it is read, so
    an entry that runs every second is due again at the next look."""

    def __init__(self, now=1_000_000.0):
        self._lock = threading.Lock()
        self._now = float(now)

    def time(self):
        with self._lock:
            self._now += 1.0
            return self._now


def test_a_tick_ends_once_what_was_due_when_it_began_has_run(product_scheduler):
    """While the slow tenant's turn runs, the busy tenant's entry keeps
    falling due, and the tick keeps starting it, so no tenant waits for the
    tick to end. But the tick ends once the entries due when it began have
    run: a tick that waited for every turn it started never ended while
    entries kept falling due, so a failure it held was never reported and an
    entry the store refused was never tried again."""
    slow, busy = _tenants("tenant-slow", "tenant-busy")
    auth = _auth([slow, busy])
    long_turn, often = _internal_session(slow, "long"), _internal_session(busy, "often")

    def take_a_moment(session):
        if session == often:
            time.sleep(0.05)

    agent = GatedAgent(during=take_a_moment)
    agent.open(often)
    scheduler = product_scheduler(agent=agent, auth=auth, clock=_TickingClock())
    scheduler.add("long", principal=_principal(auth, slow))
    scheduler.add("often", principal=_principal(auth, busy), every_ms=1000)

    tick = _in_background(scheduler.scheduler.tick)
    try:
        assert agent.wait_until(lambda: long_turn in agent.started)
        assert agent.wait_until(lambda: agent.started.count(often) >= 3), (
            "the tick did not keep starting the entry that kept falling due")
        agent.open(long_turn)
        tick.join(SOON_S)
        ended = not tick.is_alive()
    finally:
        agent.open()
        scheduler.scheduler.stop(timeout_s=0)
        tick.join(10)
    assert ended, (
        f"the tick had not ended {SOON_S}s after the last entry due when it began had "
        f"run: it waited for every turn it started, and an entry kept falling due")
    assert tick.errors == []


def test_a_run_the_tick_did_not_wait_for_that_cannot_be_recorded_fails_a_later_tick(
        product_scheduler):
    """A turn the tick started for an entry that fell due during it goes on
    after the tick ends. If that run cannot be recorded (a full disk), the
    failure is not lost with the tick: the next tick to end fails with it,
    once."""
    slow, busy = _tenants("tenant-slow", "tenant-busy")
    auth = _auth([slow, busy])
    long_turn, late = _internal_session(slow, "long"), _internal_session(busy, "late")
    agent = GatedAgent()
    scheduler = product_scheduler(agent=agent, auth=auth)
    scheduler.add("long", principal=_principal(auth, slow))
    scheduler.add("late", principal=_principal(auth, busy), every_ms=HOUR_MS)
    scheduler.store.mark_run(_key(busy, "late"), scheduler.clock.time(), "ok")
    complete = scheduler.store.complete_claim

    def full_disk(key, *args):
        if key == _key(busy, "late"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return complete(key, *args)

    scheduler.store.complete_claim = full_disk
    tick = _in_background(scheduler.scheduler.tick)
    assert agent.wait_until(lambda: long_turn in agent.started)
    scheduler.clock.advance(HOUR_S)         # "late" falls due, and the tick starts it
    assert agent.wait_until(lambda: late in agent.started)
    agent.open(long_turn)
    tick.join(SOON_S)
    ended = not tick.is_alive()
    agent.open(late)
    scheduler.wait_for(lambda: scheduler.scheduler._pool == set(), "the late turn finished")
    tick.join(10)

    assert ended, "the tick waited for a turn it did not owe"
    assert tick.errors == []
    with pytest.raises(OSError):
        scheduler.scheduler.tick()
    scheduler.scheduler.tick()              # reported once
    assert "lease_owner" in scheduler.stored("late", _principal(auth, busy))


def test_an_owed_entry_deleted_while_it_waits_for_room_is_owed_no_more(product_scheduler):
    """The tick owes both of the tenant's due entries; the second waits for
    the first's turn and is deleted meanwhile. The tick does not wait for it
    for ever: it ends once the first's turn is done."""
    agent = GatedAgent()
    scheduler = product_scheduler(agent=agent)
    tenant = scheduler.principal.tenant_id
    scheduler.add("job0")
    scheduler.add("job1")

    tick = _in_background(scheduler.scheduler.tick)
    assert agent.wait_until(lambda: len(agent.started) == 1)
    assert scheduler.store.remove(tenant, "job1") is True
    agent.open()
    tick.join(SOON_S)

    assert not tick.is_alive(), "the tick went on waiting for an entry that was deleted"
    assert tick.errors == []
    assert agent.started == [_internal_session(tenant, "job0")]


def test_when_the_pool_is_full_the_tenant_served_least_recently_goes_first(
        product_scheduler):
    """With one worker, the busy tenant, listed first, has three entries due
    and the other tenant one. The other tenant's runs second, not fourth: a
    full pool serves tenants in turn, not in store order."""
    busy, other = _tenants("tenant-busy", "tenant-other")
    auth = _auth([busy, other])
    agent = GatedAgent(opened=True)
    scheduler = product_scheduler(agent=agent, auth=auth, workers=1)
    for index in range(3):
        scheduler.add(f"job{index}", principal=_principal(auth, busy))
    scheduler.add("report", principal=_principal(auth, other))

    scheduler.scheduler.tick()

    order = ["other" if session == _internal_session(other, "report") else "busy"
             for session in agent.started]
    assert order == ["busy", "other", "busy", "busy"], (
        f"turns started in the order {order}: the other tenant waited behind the busy "
        f"tenant's backlog")
    assert agent.peak() == 1


def test_room_for_two_of_a_tenant_s_turns_still_serves_the_other_tenant_first(
        product_scheduler):
    """With a cap of two, the busy tenant's second turn still waits for the
    other tenant's first: every tenant's first turn comes before any
    tenant's second."""
    busy, other = _tenants("tenant-busy", "tenant-other")
    auth = _auth([busy, other])
    agent = GatedAgent()
    scheduler = product_scheduler(agent=agent, auth=auth, workers=2, turns_per_tenant=2)
    scheduler.add("job0", principal=_principal(auth, busy))
    scheduler.add("job1", principal=_principal(auth, busy))
    scheduler.add("report", principal=_principal(auth, other))

    tick = _in_background(scheduler.scheduler.tick)
    assert agent.wait_until(lambda: len(agent.started) == 2)
    assert not agent.wait_until(lambda: len(agent.started) > 2, timeout=0.2)
    first_two = set(agent.started)
    agent.open()
    tick.join(10)

    assert first_two == {_internal_session(busy, "job0"), _internal_session(other, "report")}
    assert not tick.is_alive() and tick.errors == [] and len(agent.started) == 3


def test_of_a_tenant_s_own_entries_the_one_due_longest_goes_first(product_scheduler):
    """A tenant at its cap is served in the order its entries fell due, not
    by name, so a tenant whose entries stay due does not starve its last."""
    agent = GatedAgent(opened=True)
    scheduler = product_scheduler(agent=agent)
    now = scheduler.clock.time()
    for name, ran_s_ago in (("a-recent", 10), ("b-oldest", 100)):
        scheduler.add(name, every_ms=1000)
        scheduler.store.mark_run(_key(scheduler.principal.tenant_id, name),
                                 now - ran_s_ago, "ok")

    scheduler.scheduler.tick()

    tenant = scheduler.principal.tenant_id
    assert agent.started == [_internal_session(tenant, "b-oldest"),
                             _internal_session(tenant, "a-recent")]


@pytest.mark.parametrize("workers, cap, tenants, entries", [
    (1, 1, 2, 2), (2, 1, 3, 2), (3, 1, 2, 3), (3, 2, 2, 3), (4, 1, 6, 1), (2, 2, 1, 4)])
def test_the_pool_runs_no_more_than_its_workers_and_no_tenant_more_than_its_cap(
        product_scheduler, workers, cap, tenants, entries):
    """The bounds, both at once: while every turn is held, as many start as
    the pool and the caps allow and no more; once released, every entry runs
    exactly once in the one tick."""
    names = _tenants(*[f"tenant-{index}" for index in range(tenants)])
    auth = _auth(names)
    agent = GatedAgent()
    scheduler = product_scheduler(agent=agent, auth=auth, workers=workers,
                                  turns_per_tenant=cap)
    sessions = {}
    for tenant in names:
        for index in range(entries):
            scheduler.add(f"job{index}", principal=_principal(auth, tenant))
            sessions[_internal_session(tenant, f"job{index}")] = tenant
    expected = min(workers, tenants * min(cap, entries))

    tick = _in_background(scheduler.scheduler.tick)
    assert agent.wait_until(lambda: len(agent.started) >= expected)
    assert not agent.wait_until(lambda: len(agent.started) > expected, timeout=0.3), (
        f"{len(agent.started)} turns started while {expected} were held: the pool has "
        f"{workers} workers and each tenant a cap of {cap}")
    agent.open()
    tick.join(10)

    assert not tick.is_alive() and tick.errors == []
    assert sorted(agent.started) == sorted(sessions), "an entry ran twice or not at all"
    assert agent.peak() <= workers
    for tenant in names:
        mine = {session for session, owner in sessions.items() if owner == tenant}
        assert agent.peak(mine) <= cap


def test_the_settings_of_the_pool_are_checked(tmp_path):
    store = ProductScheduleStore(tmp_path / "schedules.json")
    auth = _auth(["tenant-settings"])
    for bad in (0, -1, True, 1.5, "2"):
        with pytest.raises(ConfigError, match="workers"):
            ProductScheduler(store, GatedAgent(), auth, workers=bad)
        with pytest.raises(ConfigError, match="turns_per_tenant"):
            ProductScheduler(store, GatedAgent(), auth, turns_per_tenant=bad)
        with pytest.raises(ConfigError, match="min_every_ms"):
            ProductScheduleStore(tmp_path / "other.json", min_every_ms=bad)
    scheduler = ProductScheduler(store, GatedAgent(), auth)
    assert (scheduler.workers, scheduler.turns_per_tenant) == (
        product_service.SCHEDULER_WORKERS, product_service.SCHEDULED_TURNS_PER_TENANT) == (4, 1)


# ── what the pool keeps: no overlap, the stop, the drain, the charge ───


@pytest.mark.parametrize("how", ["stop", "drain"])
def test_a_request_during_concurrent_turns_starts_nothing_more(product_scheduler, how):
    """C2's rule with several turns in flight at once: the three in flight
    when the stop or the drain is requested finish and are recorded; the
    other three entries are not claimed and stay due; the count of work in
    flight is back at zero when the tick returns."""
    names = _tenants("tenant-a", "tenant-b", "tenant-c")
    auth = _auth(names)
    work = InFlightWork()
    agent = GatedAgent()
    scheduler = product_scheduler(agent=agent, auth=auth, work=work, workers=3)
    for tenant in names:
        for index in range(2):
            scheduler.add(f"job{index}", principal=_principal(auth, tenant))

    tick = _in_background(scheduler.scheduler.tick)
    assert agent.wait_until(lambda: len(agent.started) == 3)
    if how == "stop":
        scheduler.scheduler.stop(timeout_s=0)
    else:
        work.start_draining()
    agent.open()
    tick.join(10)

    assert not tick.is_alive() and tick.errors == []
    assert len(agent.started) == 3
    statuses = [scheduler.stored(f"job{index}", _principal(auth, tenant))
                for tenant in names for index in range(2)]
    ran = [entry for entry in statuses if entry["last_status"] == "ok"]
    untouched = [entry for entry in statuses if entry["last_status"] is None
                 and entry["last_run"] is None and "lease_owner" not in entry]
    assert (len(ran), len(untouched)) == (3, 3)
    assert work.wait(0) is True
    assert scheduler.scheduler._running == set()


def test_a_stop_requested_during_a_look_claims_nothing_more_in_it(product_scheduler):
    """The request arrives as a look makes its first claim, with room in the
    pool for all three tenants. Nothing more is claimed; the one claimed is
    given back before its turn starts, and nothing is left counted."""
    names = _tenants("tenant-a", "tenant-b", "tenant-c")
    auth = _auth(names)
    work = InFlightWork()
    agent = GatedAgent(opened=True)
    scheduler = product_scheduler(agent=agent, auth=auth, work=work, workers=3)
    for tenant in names:
        scheduler.add("daily", principal=_principal(auth, tenant))
    claim = scheduler.store.claim_due
    claims = []

    def claim_due(key, *args):
        claims.append(key)
        entry = claim(key, *args)
        scheduler.scheduler.stop(timeout_s=0)
        return entry

    scheduler.store.claim_due = claim_due
    scheduler.scheduler.tick()

    assert len(claims) == 1, f"{len(claims)} entries were claimed in the look that saw the stop"
    assert agent.started == []
    for tenant in names:
        stored = scheduler.stored("daily", _principal(auth, tenant))
        assert (stored["last_run"], "lease_owner" in stored) == (None, False)
    assert work.wait(0) is True


def test_stop_waits_for_every_turn_in_the_pool(product_scheduler):
    """drain_service relies on stop(timeout): it is True only once every
    pooled turn has finished, not when the loop's thread is first free."""
    one, two = _tenants("tenant-one", "tenant-two")
    auth = _auth([one, two])
    agent = GatedAgent()
    scheduler = product_scheduler(agent=agent, auth=auth)
    scheduler.add("a", principal=_principal(auth, one))
    scheduler.add("b", principal=_principal(auth, two))
    first, second = _internal_session(one, "a"), _internal_session(two, "b")

    scheduler.start()
    assert agent.wait_until(lambda: len(agent.running) == 2)
    assert scheduler.stop(timeout_s=0) is False
    agent.open(first)
    assert agent.wait_until(lambda: first in agent.finished)
    assert scheduler.stop(timeout_s=0.05) is False, (
        "stop() reported the scheduler stopped with a pooled turn still running")
    agent.open(second)
    assert scheduler.stop(timeout_s=10) is True
    assert sorted(agent.started) == sorted([first, second])


def test_stop_waits_for_the_turns_of_a_tick_called_outside_the_loop(product_scheduler):
    agent = GatedAgent()
    scheduler = product_scheduler(agent=agent)
    scheduler.add("daily")
    tick = _in_background(scheduler.scheduler.tick)
    assert agent.wait_until(lambda: len(agent.running) == 1)

    assert scheduler.stop(timeout_s=0) is False, (
        "stop() reported the scheduler stopped while a turn it started was running")
    agent.open()
    assert scheduler.stop(timeout_s=10) is True
    tick.join(10)
    assert not tick.is_alive() and tick.errors == []


def test_a_turn_stuck_past_the_stall_bound_is_unhealthy_while_other_tenants_run(
        product_scheduler):
    """One turn that does not return used to stop every tenant's schedules,
    and the scheduler's progress age reported it (audit F-05). With the pool
    the other tenants' runs go on and keep that age fresh, so a turn in
    flight for longer than the stall bound (its lease and an interval) is
    what makes the scheduler unhealthy now, until it returns."""
    stuck, busy = _tenants("tenant-stuck", "tenant-busy")
    auth = _auth([stuck, busy])
    agent = GatedAgent(hold_limit_s=30.0)
    agent.open(_internal_session(busy, "minutely"))
    scheduler = product_scheduler(agent=agent, auth=auth, lease_s=1.0, turn_deadline_s=0.5)
    loop = scheduler.scheduler
    scheduler.add("held", principal=_principal(auth, stuck))
    scheduler.add("minutely", principal=_principal(auth, busy), every_ms=1000)
    scheduler.start()
    assert agent.wait_until(lambda: _internal_session(stuck, "held") in agent.started)

    give_up = time.monotonic() + 10
    while loop.is_healthy():
        assert time.monotonic() < give_up, (
            "a scheduler with a turn in flight past its stall bound stayed healthy, while "
            "another tenant's runs kept its progress age fresh")
        scheduler.clock.advance(1)      # "minutely" is due again, and runs
        time.sleep(0.05)
    report = loop.snapshot()
    runs = agent.started.count(_internal_session(busy, "minutely"))
    assert runs >= 2, f"the other tenant's entry ran {runs} time(s), so this proved nothing"
    assert report["thread_alive"] is True
    assert report["last_progress_age_seconds"] <= report["stall_after_seconds"]

    agent.open()
    scheduler.wait_for(loop.is_healthy, "the scheduler was healthy again once the turn returned")


def _metered_quota(tmp_path):
    return DurableQuotaStore(
        tmp_path / "quota.sqlite3", tokens_per_day=1_000_000,
        token_reservation_per_turn=10_000,
        storage_bytes_per_tenant=1 << 20, storage_reservation_per_turn=1 << 10,
        audit_bytes_per_tenant=1 << 20, audit_reservation_per_turn=1 << 10)


class _Footprints:
    @staticmethod
    def footprint(internal_session):
        return {"storage_bytes": 7, "audit_bytes": 8}


@pytest.mark.parametrize("outcome", ["reply", "failure"])
def test_concurrent_turns_are_each_charged_their_own_tokens(
        product_scheduler, tmp_path, outcome):
    """Two tenants' turns are in flight at once on two threads of the pool.
    Each tenant pays what its own turn counted: from the reply, or, when the
    turn failed after the model call was paid for, from the count the agent
    keeps on that turn's thread."""
    one, two = _tenants("tenant-one", "tenant-two")
    auth = _auth([one, two])
    quota = _metered_quota(tmp_path)
    agent = GatedAgent()
    first, second = _internal_session(one, "a"), _internal_session(two, "b")
    agent.spends = {first: {"input_tokens": 700, "output_tokens": 300},
                    second: {"input_tokens": 5, "output_tokens": 5}}
    if outcome == "failure":
        agent.fail = {first, second}
    scheduler = product_scheduler(agent=agent, auth=auth, quota_store=quota,
                                  data_manager=_Footprints())
    scheduler.add("a", principal=_principal(auth, one))
    scheduler.add("b", principal=_principal(auth, two))

    tick = _in_background(scheduler.scheduler.tick)
    assert agent.wait_until(lambda: len(agent.running) == 2), "the two turns did not overlap"
    agent.open()
    tick.join(10)

    assert not tick.is_alive() and tick.errors == []
    expected = "ok" if outcome == "reply" else "failed"
    assert scheduler.schedule("a", _principal(auth, one))["last_status"] == expected
    assert scheduler.schedule("b", _principal(auth, two))["last_status"] == expected
    charged = {tenant: (quota.tenant_usage(tenant)["tokens_today"],
                        quota.tenant_usage(tenant)["reserved_tokens"]) for tenant in (one, two)}
    assert charged == {one: (1000, 0), two: (10, 0)}


def _no_thread_for(name, scheduler):
    """Make the scheduler's thread of that name fail to start, as a process
    out of threads would; any other thread starts as usual."""
    start = scheduler._start_thread
    refused = []

    def starting(thread_name, target, *args):
        if thread_name == name:
            refused.append(thread_name)
            raise RuntimeError("can't start new thread")
        return start(thread_name, target, *args)

    scheduler._start_thread = starting
    return refused


def test_a_turn_that_gets_no_thread_gives_its_claim_back_and_fails_the_tick(
        product_scheduler):
    """The tick's own thread runs the first turn; the second tenant's needs a
    thread of its own and none can be started. Its claim is given back, so
    the entry is untouched and still due, nothing is left counted, and the
    tick fails with the error once the first turn is recorded."""
    one, two = _tenants("tenant-one", "tenant-two")
    auth = _auth([one, two])
    work = InFlightWork()
    scheduler = product_scheduler(agent=GatedAgent(opened=True), auth=auth, work=work)
    scheduler.add("a", principal=_principal(auth, one))
    scheduler.add("b", principal=_principal(auth, two))
    refused = _no_thread_for("pi-scheduled-turn", scheduler.scheduler)

    with pytest.raises(RuntimeError, match="can't start new thread"):
        scheduler.scheduler.tick()

    assert refused, "no turn needed a thread of its own, so this proved nothing"
    assert scheduler.schedule("a", _principal(auth, one))["last_status"] == "ok"
    second = scheduler.stored("b", _principal(auth, two))
    assert (second["last_run"], second["last_status"], "lease_owner" in second) == (
        None, None, False)
    assert work.wait(0) is True
    assert scheduler.scheduler._running == set() and scheduler.scheduler._pool == set()


def test_without_a_thread_to_look_with_the_tick_looks_once_its_own_turn_is_done(
        product_scheduler):
    """No looker thread can be started: the tick runs its first turn, then
    looks again itself at once, not an interval later (an hour here), so the
    tenant's second entry still runs in the tick."""
    agent = GatedAgent(opened=True)
    scheduler = product_scheduler(agent=agent, interval_s=HOUR_S)
    tenant = scheduler.principal.tenant_id
    scheduler.add("job0")
    scheduler.add("job1")
    refused = _no_thread_for("pi-scheduler-look", scheduler.scheduler)

    tick = _in_background(scheduler.scheduler.tick)
    tick.join(SOON_S)
    ended = not tick.is_alive()
    scheduler.scheduler.stop(timeout_s=0)
    tick.join(10)

    assert ended, "the tick waited an interval before it looked again"
    assert refused == ["pi-scheduler-look"] and tick.errors == []
    assert agent.started == [_internal_session(tenant, "job0"),
                             _internal_session(tenant, "job1")]
    assert scheduler.schedule("job1")["last_status"] == "ok"


def test_an_entry_the_look_cannot_read_is_refused_alone(product_scheduler):
    """An entry with no interval cannot be judged due by the look. It is left
    to claim_due, which refuses it, as it did before there was a look: the
    other tenant's entry runs, and then the tick fails."""
    broken, healthy = _tenants("tenant-broken", "tenant-healthy")
    auth = _auth([broken, healthy])
    agent = GatedAgent(opened=True)
    scheduler = product_scheduler(agent=agent, auth=auth)
    scheduler.add("bad", principal=_principal(auth, broken))
    scheduler.add("good", principal=_principal(auth, healthy))
    path = scheduler.store.path
    stored = json.loads(path.read_text())
    stored[_key(broken, "bad")]["last_run"] = scheduler.clock.time() - 10
    del stored[_key(broken, "bad")]["every_ms"]
    path.write_text(json.dumps(stored, sort_keys=True))

    with pytest.raises(KeyError):
        scheduler.scheduler.tick()

    assert agent.started == [_internal_session(healthy, "good")]
    assert scheduler.schedule("good", _principal(auth, healthy))["last_status"] == "ok"
    assert "lease_owner" not in scheduler.stored("bad", _principal(auth, broken))


def test_an_entry_the_store_refuses_is_tried_once_a_tick(product_scheduler):
    """The tick looks again every interval while its turns run. An entry
    whose claim the store refused (a full disk) is not tried on each of
    those looks, only once a tick, as before there were looks; the tick
    fails with the refusal once its turns are done."""
    busy, other = _tenants("tenant-busy", "tenant-other")
    auth = _auth([busy, other])
    agent = GatedAgent()
    scheduler = product_scheduler(agent=agent, auth=auth)
    scheduler.add("held", principal=_principal(auth, busy))
    scheduler.add("refused", principal=_principal(auth, other))
    refused_key = _key(other, "refused")
    claim = scheduler.store.claim_due
    attempts = []

    def claim_due(key, *args):
        if key == refused_key:
            attempts.append(key)
            raise OSError(errno.ENOSPC, "No space left on device")
        return claim(key, *args)

    scheduler.store.claim_due = claim_due
    tick = _in_background(scheduler.scheduler.tick)
    assert agent.wait_until(lambda: _internal_session(busy, "held") in agent.started)
    looked = scheduler.ticks
    time.sleep(0.3)                     # some thirty looks at a 0.01 s interval
    assert scheduler.ticks == looked, "the tick ended while its turn was held"
    agent.open()
    tick.join(10)

    assert len(attempts) == 1, f"the refused claim was tried {len(attempts)} times in one tick"
    assert [type(error) for error in tick.errors] == [OSError]
    assert scheduler.schedule("held", _principal(auth, busy))["last_status"] == "ok"


def test_a_drain_that_begins_as_an_entry_is_taken_leaves_nothing_counted(product_scheduler):
    """The drain begins between the tick's look at the drain and its count of
    the entry as work: the entry is not claimed, and its place in the pool is
    given back without a count of work to give back with it."""

    class DrainBeginsNow(InFlightWork):
        def enter(self):
            self.start_draining()
            return super().enter()

    work = DrainBeginsNow()
    agent = GatedAgent(opened=True)
    scheduler = product_scheduler(agent=agent, work=work)
    scheduler.add("daily")

    scheduler.scheduler.tick()

    assert agent.started == []
    stored = scheduler.stored("daily")
    assert (stored["last_run"], "lease_owner" in stored) == (None, False)
    assert work.wait(0) is True
    assert scheduler.scheduler._running == set() and scheduler.scheduler._pool == set()


def test_a_full_pool_serves_tenants_in_turn(product_scheduler):
    """The class: with one worker, and tenant number t holding layout[t] due
    entries, no tenant ever has a turn ahead of a tenant still waiting."""

    @settings(max_examples=25, deadline=None)
    @given(layout=st.lists(st.integers(min_value=1, max_value=3), min_size=1, max_size=4))
    def run(layout):
        names = _tenants(*[f"tenant-{index}" for index in range(len(layout))])
        auth = _auth(names)
        agent = GatedAgent(opened=True)
        scheduler = product_scheduler(agent=agent, auth=auth, workers=1)
        owner = {}
        for tenant, entries in zip(names, layout):
            for index in range(entries):
                scheduler.add(f"job{index}", principal=_principal(auth, tenant))
                owner[_internal_session(tenant, f"job{index}")] = tenant

        scheduler.scheduler.tick()

        assert sorted(agent.started) == sorted(owner)
        wanted = dict(zip(names, layout))
        had = dict.fromkeys(names, 0)
        for position, session in enumerate(agent.started):
            tenant = owner[session]
            waiting = [other for other in names
                       if other != tenant and had[other] < wanted[other]]
            assert all(had[tenant] <= had[other] for other in waiting), (
                f"turn {position + 1} went to {tenant} with {had[tenant]} turn(s) "
                f"already, while {[(other, had[other]) for other in waiting]} waited")
            had[tenant] += 1

    run()


# ── while here: the clock is read for each claim ────────────────────────


def test_each_claim_is_leased_from_the_moment_it_is_made(product_scheduler):
    """tick() read the clock once and stamped every claim with it, so a
    claim made late in a long tick was leased for less than lease_s and could
    expire while its turn still ran."""
    agent = GatedAgent(opened=True)
    scheduler = product_scheduler(agent=agent, lease_s=300)
    tenant = scheduler.principal.tenant_id
    first, second = _internal_session(tenant, "first"), _internal_session(tenant, "second")
    seen = {}

    def advance_then_look(session):
        if session == first:
            scheduler.clock.advance(200)
        else:
            seen["now"] = scheduler.clock.time()
            seen["lease_until"] = scheduler.stored("second")["lease_until"]

    agent.during = advance_then_look
    scheduler.add("first")
    scheduler.add("second")
    scheduler.scheduler.tick()

    assert agent.started == [first, second]
    leased_for = seen["lease_until"] - seen["now"]
    assert leased_for == 300, (
        f"the claim made 200s into the tick was leased for {leased_for:.0f}s from when "
        f"it was made, not lease_s=300s: tick() stamped it with the clock it read as it "
        f"began")


def test_an_entry_that_falls_due_during_a_tick_is_claimed_by_its_next_look(
        product_scheduler):
    """tick() judged what was due by the clock it read as it began. Each look
    reads it again: an entry that falls due while the tick's first turn runs
    is claimed and started by the tick's next look."""
    held, other = _tenants("tenant-held", "tenant-other")
    auth = _auth([held, other])
    agent = GatedAgent()
    scheduler = product_scheduler(agent=agent, auth=auth)
    first, minutely = _internal_session(held, "first"), _internal_session(other, "minutely")
    scheduler.add("first", principal=_principal(auth, held))
    scheduler.add("minutely", principal=_principal(auth, other), every_ms=60_000)
    scheduler.store.mark_run(_key(other, "minutely"), scheduler.clock.time() - 30, "ok")
    agent.open(minutely)

    tick = _in_background(scheduler.scheduler.tick)
    assert agent.wait_until(lambda: first in agent.started)
    scheduler.clock.advance(60)            # "minutely" falls due 30 s into this
    started = agent.wait_until(lambda: minutely in agent.started, timeout=SOON_S)
    agent.open()
    tick.join(10)

    assert started, (
        "an entry that fell due during the tick was not claimed by its next look: tick() "
        "judged what was due by the clock it read as it began")
    assert not tick.is_alive() and tick.errors == []


def test_an_idle_tick_reads_the_schedule_store_once(product_scheduler):
    """The tick looks again every interval while its turns run, so a look
    must be cheap: an entry the store's listing shows is not due, or is
    leased, is not claimed. Each claim reads and rewrites the whole store."""
    scheduler = product_scheduler(agent=GatedAgent(opened=True))
    for index in range(5):
        scheduler.add(f"job{index}")
    scheduler.scheduler.tick()             # all five run; none is due for an hour
    reads = []
    load = scheduler.store._load

    def counted():
        reads.append(1)
        return load()

    scheduler.store._load = counted
    scheduler.scheduler.tick()

    assert len(reads) == 1, (
        f"a tick with nothing due read the store {len(reads)} times for 5 entries")


# ── the floor on every_ms ───────────────────────────────────────────────


def test_the_product_refuses_an_every_ms_below_one_minute(tmp_path, monkeypatch):
    """The schedule store product_main builds refuses an interval below the
    floor with 400 invalid_interval; one minute is accepted."""
    _base_env(tmp_path, monkeypatch)
    principal = Principal("planner", "tenant", frozenset({"schedules:write"}), frozenset())
    seen = {}

    def during(service):
        for every_ms in (1, 1000, 59_999, 60_000):
            try:
                service.schedule_store.put(principal, name=f"every-{every_ms}",
                                           session="work", message="m", every_ms=every_ms)
            except ProductError as error:
                seen[every_ms] = (error.status, error.code)
            else:
                seen[every_ms] = "accepted"

    _install_happy_runtime(monkeypatch, during=during)
    product_main.run()

    assert seen[1] == (400, "invalid_interval"), (
        f"the product accepted every_ms=1 ({seen[1]}): a tenant can keep every one of "
        f"its schedules due (audit F-06)")
    assert seen == {1: (400, "invalid_interval"), 1000: (400, "invalid_interval"),
                    59_999: (400, "invalid_interval"), 60_000: "accepted"}


def test_the_schedule_route_answers_a_short_interval_with_invalid_interval(tmp_path):
    tenant = "tenant-route"
    auth = _auth([tenant])
    store = ProductScheduleStore(tmp_path / "schedules.json",
                                 min_every_ms=product_service.MIN_SCHEDULE_EVERY_MS)
    service = ProductService(GatedAgent(), auth, schedule_store=store,
                             log_sink=lambda record: None)
    headers = {"Authorization": f"Bearer {_token(tenant)}"}

    def post(every_ms):
        body = json.dumps({"name": "daily", "session": "work", "message": "m",
                           "every_ms": every_ms}).encode()
        status, _, payload = service.dispatch("POST", "/v1/schedules", headers, body)
        return status, payload

    status, payload = post(59_999)
    assert status == 400 and payload["error"]["code"] == "invalid_interval"
    assert payload["error"]["message"] == "every_ms must be at least 60000"
    assert post(60_000)[0] == 201
    [listed] = store.for_tenant(tenant)
    assert listed["every_ms"] == 60_000


def test_a_schedule_stored_below_the_floor_loads_and_runs_no_more_often_than_it(
        tmp_path, monkeypatch):
    """An earlier release accepted every_ms=1. The store it wrote still
    loads and the service starts; the entry is listed as stored, and runs
    no more often than the floor."""
    _base_env(tmp_path, monkeypatch)
    auth = _auth(["tenant"])
    principal = _principal(auth, "tenant")
    # As an earlier release wrote it: the store creates the state directory.
    ProductScheduleStore(tmp_path / "state" / "product-schedules.json").put(
        principal, name="spin", session="work", message="m", every_ms=1)
    seen = {}

    def during(service):
        seen["listed"] = [(entry["name"], entry["every_ms"])
                          for entry in service.schedule_store.for_tenant("tenant")]
        seen["ready"] = service.readiness_snapshot()["dependencies"]["schedule_store"]
        now = [1_000_000.0]
        agent = GatedAgent(opened=True)
        scheduler = ProductScheduler(service.schedule_store, agent, auth,
                                     clock=SimpleNamespace(time=lambda: now[0]))
        runs = []
        for advance in (0, 1, 58, 1):          # at 0 s, 1 s, 59 s and 60 s
            now[0] += advance
            scheduler.tick()
            runs.append(len(agent.started))
        seen["runs"] = runs

    _install_happy_runtime(monkeypatch, during=during)
    product_main.run()

    assert seen["listed"] == [("spin", 1)] and seen["ready"] is True
    assert seen["runs"] == [1, 1, 1, 2], (
        f"turns run by 0, 1, 59 and 60 seconds: {seen['runs']}; a schedule stored with "
        f"every_ms=1 must run no more often than once a minute")
