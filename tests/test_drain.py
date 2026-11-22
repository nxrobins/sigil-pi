"""Draining stops new work and waits, within one budget, for the work in
flight (audit 2026-11-10, F-07 and F-26).

The threat model says a drain "stops schedule intake" and "waits for accepted
work", and the operations guide gives it ``PI_DRAIN_TIMEOUT_SECONDS``. Three
things were not so.

- ``ProductScheduler.tick()`` walked its snapshot of the entries without
  looking at the stop flag, and a scheduled turn never asked whether the
  service was draining. A stop requested with one turn in flight and five
  entries due let the other four start, the last of them seconds later, and
  the drain failed (``audit/repro/r7_scheduler_lifecycle.py``, part a). A
  failed drain writes no clean-shutdown marker, and without the marker the
  offline backup refuses to run.
- Shutdown waited for the scheduler, the retention monitor, the audit monitor
  and the requests one after the other, and gave each the whole budget: up to
  four times the documented one.
- Only chat turns were counted as work in flight. An export, a deletion or a
  schedule change in progress was not waited for, so the marker that
  authorizes an offline backup could be written while a deletion was halfway
  through.

What these tests hold the product to:

- After a stop request no scheduled turn starts, and the one in flight
  finishes. That covers a turn that was claimed and was still waiting for its
  session: it does not start, its claim is given back with nothing recorded,
  the tenant is charged nothing for it, and it runs when a scheduler next
  starts.
- A scheduled turn respects the draining state a chat turn respects, and is
  counted in the same count of work in flight.
- Every route that reads or changes a tenant's data is work a drain waits
  for, and is refused with ``503 service_draining`` once the drain has begun.
  The operational reads keep answering, because they are how a drain is
  watched.
- One deadline covers the whole drain: the listener, the three loops and the
  work in flight are waited for at once, and the drain gives up at that
  deadline whatever is still running. The marker is written only after
  everything has finished.
- The retention sweep had the scheduler's defect and is held to the same
  rule, and a chat turn stays counted until its quota has been released.

The scheduler runs on its real thread throughout (conftest's
``product_scheduler``), except where a test calls ``tick()`` itself so that
the request can arrive at an exact point.
"""

import ast
import hashlib
import json
import threading
import time
from types import SimpleNamespace

import pytest

from conftest import PI_ROOT, SCHEDULER_TENANT, ScriptedTurnAgent
from test_product_main import _base_env, _install_happy_runtime

import product_main
import product_service
from product_service import (
    AuthRegistry,
    DurableQuotaStore,
    ProductRetentionMonitor,
    ProductService,
    SessionOperationLocks,
    _internal_session,
)

HOUR_MS = 3_600_000
TENANT = "tenant-a"
TENANT_TOKEN = "drain-tenant-token-with-test-only-entropy-0123456789abcdef"
OPS_TOKEN = "drain-operator-token-with-test-only-entropy-0123456789abc"
TENANT_SCOPES = ["chat", "schedules:read", "schedules:write",
                 "sessions:read", "sessions:delete"]


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _auth_entries():
    return [
        {"sha256": _digest(TENANT_TOKEN), "principal": "worker", "tenant": TENANT,
         "scopes": TENANT_SCOPES, "tools": ["read_file"]},
        {"sha256": _digest(OPS_TOKEN), "principal": "operator", "tenant": "operations",
         "scopes": ["ops:read"], "tools": []},
    ]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _lease_fields(entry):
    return {key: entry[key] for key in ("lease_owner", "lease_until") if key in entry}


def _claimed(scheduler, name):
    owner = scheduler.scheduler.owner
    return lambda: (scheduler.stored(name) or {}).get("lease_owner") == owner


def _claims_after(scheduler, requested):
    """Record, in the list returned, every claim the scheduler asks its store
    for once ``requested`` has something in it."""
    late = []
    claim_due = scheduler.store.claim_due

    def recording(key, *args):
        if requested:
            late.append(key)
        return claim_due(key, *args)

    scheduler.store.claim_due = recording
    return late


def _untouched(scheduler, name):
    """Whether the entry is as it was created: never run, nothing recorded,
    no claim. It is then still due."""
    stored = scheduler.stored(name)
    return (stored.get("last_run"), stored.get("last_status"), _lease_fields(stored)) == (
        None, None, {})


class MeteredAgent(ScriptedTurnAgent):
    """ScriptedTurnAgent with PiAgent's per-thread count of a turn's tokens.

    PiAgent counts a turn's tokens on the thread that runs the turn and starts
    the count over first thing in ``turn_with_usage``; ``turn_telemetry()``
    reports the calling thread's count. Until that thread begins another turn
    the count is therefore still the last turn's. ``spends`` maps a session to
    what a turn on it costs (``usage`` otherwise), counted before the turn can
    fail, as a model call that was paid for is. ``during`` is called with the
    turn, on the turn's own thread, once it is in flight."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.spends = {}
        self.during = None
        self._counted = threading.local()

    def turn_with_usage(self, session, message, **kwargs):
        self._counted.usage = {"input_tokens": 0, "output_tokens": 0}
        reply, _ = super().turn_with_usage(session, message, **kwargs)
        return reply, dict(self._counted.usage)

    def _wait_while_held(self, deadline_monotonic):
        turn = self.turns[-1]
        self._counted.usage = dict(self.spends.get(turn.session, self.usage))
        if self.during is not None:
            self.during(turn)
        super()._wait_while_held(deadline_monotonic)

    def turn_telemetry(self):
        counted = getattr(self._counted, "usage", {"input_tokens": 0, "output_tokens": 0})
        return {**super().turn_telemetry(), **counted}


# ── F-07: the scheduler starts nothing after a stop request ─────────────


def test_no_scheduled_turn_starts_after_a_stop_request(product_scheduler):
    """Five entries are due and the first turn is in flight when the stop is
    requested. That turn finishes and is recorded. The other four do not
    start: they are not claimed, nothing is recorded for them, and they are
    still due. The audit's reproduction saw four start, the last 3.7 seconds
    after the request."""
    scheduler = product_scheduler()
    names = [f"job{i}" for i in range(5)]
    for name in names:
        scheduler.add(name)
    requested = []
    claimed_late = _claims_after(scheduler, requested)
    scheduler.agent.hold()
    scheduler.start()
    [first] = scheduler.agent.wait_for_turns(1)

    requested.append(True)
    assert scheduler.stop(timeout_s=0) is False, "the turn in flight was abandoned"
    scheduler.agent.release()
    assert scheduler.stop(timeout_s=10) is True

    late = scheduler.agent.turns[1:]
    assert not late, f"{len(late)} scheduled turn(s) started after the stop request"
    assert claimed_late == [], (
        f"{len(claimed_late)} entries were claimed after the stop request")
    assert first.finished is not None
    ran = [name for name in names if scheduler.schedule(name)["last_status"] == "ok"]
    assert [_internal_session(SCHEDULER_TENANT, name) for name in ran] == [first.session]
    waiting = [name for name in names if name not in ran]
    assert [name for name in waiting if not _untouched(scheduler, name)] == []


def test_a_claimed_turn_still_waiting_for_its_session_does_not_start_after_a_stop_request(
        product_scheduler):
    """A due entry is claimed before its session is free: a chat turn, an
    export or a deletion on that session holds the lock (the test does here).
    A stop requested while it waits means the turn does not start when the
    lock comes free. Its claim is given back, so it does not wait out a lease
    of five minutes, and nothing is recorded: it runs when a scheduler next
    starts."""
    scheduler = product_scheduler()
    scheduler.add("daily", session="work")

    with scheduler.locks.hold(_internal_session(SCHEDULER_TENANT, "work")):
        scheduler.start()
        scheduler.wait_for(_claimed(scheduler, "daily"), "the thread claimed the due entry")
        assert scheduler.stop(timeout_s=0) is False
    assert scheduler.stop(timeout_s=10) is True

    started = len(scheduler.agent.turns)
    assert started == 0, f"{started} claimed turn(s) started after the stop request"
    assert _untouched(scheduler, "daily"), (
        f"the entry whose turn never started was left as {scheduler.stored('daily')}")
    assert scheduler.locks.size() == 0

    scheduler.start()
    [turn] = scheduler.agent.wait_for_turns(1)
    scheduler.wait_for(lambda: scheduler.schedule("daily")["last_status"] == "ok",
                       "the entry ran once a scheduler started again")
    assert turn.session == _internal_session(SCHEDULER_TENANT, "work")


def test_a_scheduled_turn_respects_the_draining_state_a_chat_turn_respects(
        product_scheduler):
    """The service is told to drain and nobody has stopped the scheduler yet,
    which is the half second ``product_main`` spends stopping the listener.
    The scheduled turn in flight is counted with the chat turns the drain
    waits for; the entries that are due do not start."""
    work = product_service.InFlightWork()
    scheduler = product_scheduler(work=work)
    service = ProductService(
        scheduler.agent, scheduler.auth, work=work, scheduler=scheduler.scheduler,
        log_sink=lambda record: None)
    names = [f"job{i}" for i in range(3)]
    for name in names:
        scheduler.add(name)
    scheduler.agent.hold()
    scheduler.start()
    [first] = scheduler.agent.wait_for_turns(1)

    service.start_draining()
    counted = service.wait_for_drain(0) is False
    scheduler.agent.release()
    finished = service.wait_for_drain(10)
    scheduler.wait_for_ticks(3)

    late = scheduler.agent.turns[1:]
    assert not late, f"{len(late)} scheduled turn(s) started while the service was draining"
    assert counted, "the drain did not count the scheduled turn in flight"
    assert finished and first.finished is not None
    assert scheduler.thread.is_alive(), "draining ended the scheduler's thread"
    ran = [name for name in names if scheduler.schedule(name)["last_status"] == "ok"]
    assert [_internal_session(SCHEDULER_TENANT, name) for name in ran] == [first.session]
    assert [name for name in names if name not in ran and not _untouched(scheduler, name)] == []
    assert service.readiness_snapshot()["draining"] is True


@pytest.mark.parametrize("how", ["stop", "drain"])
@pytest.mark.parametrize(
    "due, after", [(due, after) for due in range(1, 5) for after in range(1, due + 1)])
def test_whenever_the_request_arrives_no_later_entry_starts(
        product_scheduler, due, after, how):
    """The guard for the class. Of ``due`` entries, the stop or the drain is
    requested while turn number ``after`` is in flight. Exactly ``after``
    turns run, each is recorded, the rest are untouched and were not even
    claimed, and the drain's count of work in flight is back at zero.
    ``tick()`` is called here, so the request arrives on the scheduler's own
    thread at that exact turn."""
    work = product_service.InFlightWork()
    agent = MeteredAgent()
    scheduler = product_scheduler(agent=agent, work=work)
    names = [f"job{i}" for i in range(due)]
    for name in names:
        scheduler.add(name)
    requested = []
    claimed_late = _claims_after(scheduler, requested)

    def request(turn):
        if len(agent.turns) == after:
            requested.append(True)
            if how == "stop":
                scheduler.stop(timeout_s=0)
            else:
                work.start_draining()

    agent.during = request
    scheduler.scheduler.tick()

    assert len(agent.turns) == after
    assert claimed_late == []
    statuses = sorted(str(scheduler.schedule(name)["last_status"]) for name in names)
    assert statuses == ["None"] * (due - after) + ["ok"] * after
    not_run = [name for name in names if scheduler.schedule(name)["last_status"] is None]
    assert [name for name in not_run if not _untouched(scheduler, name)] == []
    assert work.wait(0) is True, "a tick left work counted as in flight"


@pytest.mark.parametrize("how", ["stop", "drain"])
def test_a_tick_that_begins_after_the_request_does_not_read_the_schedule_store(
        product_scheduler, how):
    """Once the stop or the drain has been requested, a tick is over before
    it opens the store. A disk that filled up during shutdown is then not
    reported as a failed tick, and nothing is claimed."""
    work = product_service.InFlightWork()
    scheduler = product_scheduler(work=work)
    scheduler.add("daily")
    if how == "stop":
        scheduler.stop(timeout_s=0)
    else:
        work.start_draining()

    def unreadable():
        raise OSError(5, "Input/output error")

    scheduler.store.entries = unreadable
    scheduler.scheduler.tick()

    assert scheduler.tick_errors == [] and scheduler.agent.turns == []
    assert _untouched(scheduler, "daily")


def test_an_entry_that_is_already_running_is_skipped_and_not_left_counted(product_scheduler):
    """A second tick that arrives while an entry's turn is running skips that
    entry. The skip leaves the drain's count as it found it: an entry that
    stayed counted would keep every later drain from completing."""
    work = product_service.InFlightWork()
    agent = MeteredAgent()
    scheduler = product_scheduler(agent=agent, work=work)
    scheduler.add("daily")
    seen = []

    def second_tick(turn):
        scheduler.scheduler.tick()
        seen.append(work.wait(0))

    agent.during = second_tick
    scheduler.scheduler.tick()

    assert len(agent.turns) == 1, "the entry ran again while its turn was in flight"
    assert seen == [False], "the turn in flight was not counted during the second tick"
    assert work.wait(0) is True
    assert scheduler.schedule("daily")["last_status"] == "ok"


# ── what a turn that never started is charged ───────────────────────────


class _Footprints:
    @staticmethod
    def footprint(internal_session):
        return {"storage_bytes": 7, "audit_bytes": 8}


def _metered(tmp_path):
    quota = DurableQuotaStore(
        tmp_path / "quota.sqlite3", tokens_per_day=1_000_000,
        token_reservation_per_turn=10_000,
        storage_bytes_per_tenant=1 << 20, storage_reservation_per_turn=1 << 10,
        audit_bytes_per_tenant=1 << 20, audit_reservation_per_turn=1 << 10)
    return quota, MeteredAgent()


def test_a_scheduled_turn_the_stop_held_back_is_charged_nothing(product_scheduler, tmp_path):
    """The scheduler's thread serves every scheduled turn, and the agent's
    count of tokens is per thread: after a turn it is still that turn's. A
    turn that is held back after its quota was reserved never reaches the
    agent, so the count it would be settled with is the turn before it. Read
    as its own, the tenant paid for that earlier turn twice (the listener's
    case of this is in tests/test_product_turn_budget.py)."""
    quota, agent = _metered(tmp_path)
    agent.spends = {
        _internal_session(SCHEDULER_TENANT, "first"): {"input_tokens": 700, "output_tokens": 300},
        _internal_session(SCHEDULER_TENANT, "second"): {"input_tokens": 5, "output_tokens": 5},
    }
    scheduler = product_scheduler(agent=agent, quota_store=quota, data_manager=_Footprints())
    scheduler.add("first")
    scheduler.start()
    scheduler.wait_for(lambda: scheduler.schedule("first")["last_status"] == "ok",
                       "the first scheduled turn ran")
    charged = quota.tenant_usage(SCHEDULER_TENANT)["tokens_today"]
    assert charged == 1000

    with scheduler.locks.hold(_internal_session(SCHEDULER_TENANT, "second")):
        scheduler.add("second")
        scheduler.wait_for(_claimed(scheduler, "second"), "the thread claimed the second entry")
        assert scheduler.stop(timeout_s=0) is False
    assert scheduler.stop(timeout_s=10) is True

    usage = quota.tenant_usage(SCHEDULER_TENANT)
    extra = usage["tokens_today"] - charged
    assert extra == 0, (
        f"a scheduled turn that the stop request held back was charged {extra:,} tokens: "
        + ("the turn before it, again" if extra == charged else "its own, so it ran"))
    assert usage["reserved_tokens"] == 0, "its reservation was kept"
    assert len(agent.turns) == 1
    assert _untouched(scheduler, "second")


def test_a_claim_that_cannot_be_given_back_fails_the_tick_and_is_left_to_its_lease(
        product_scheduler):
    """Giving a claim back rewrites the store, which can fail (a full disk).
    Like a run that could not be recorded, that fails the tick, which the
    loop counts and reports; the entry is not left marked as running in this
    process or counted as work in flight, and its claim expires with its
    lease."""
    work = product_service.InFlightWork()
    scheduler = product_scheduler(work=work)
    scheduler.add("daily", session="work")

    def full_disk(key, owner):
        raise OSError(28, "No space left on device")

    scheduler.store.release_claim = full_disk
    with scheduler.locks.hold(_internal_session(SCHEDULER_TENANT, "work")):
        scheduler.start()
        scheduler.wait_for(_claimed(scheduler, "daily"), "the thread claimed the due entry")
        assert scheduler.stop(timeout_s=0) is False
    assert scheduler.stop(timeout_s=10) is True

    assert [type(error) for error in scheduler.tick_errors] == [OSError]
    assert scheduler.scheduler.snapshot()["tick_failures_total"] == 1
    assert scheduler.agent.turns == []
    assert scheduler.scheduler._running == set()
    assert work.wait(0) is True
    assert _lease_fields(scheduler.stored("daily")).keys() == {"lease_owner", "lease_until"}


def test_only_the_owner_of_a_claim_gives_it_back_and_nothing_is_recorded(product_scheduler):
    """``release_claim`` undoes ``claim_due`` and nothing else. Another
    scheduler process cannot give back a claim it does not hold, and an entry
    that was deleted meanwhile has nothing to give back."""
    scheduler = product_scheduler()
    scheduler.add("daily")
    store = scheduler.store
    [(key, created)] = store.entries()
    assert store.claim_due(key, scheduler.clock.time(), "worker-1", 300) is not None
    assert _lease_fields(store.get(key)).keys() == {"lease_owner", "lease_until"}

    assert store.release_claim(key, "worker-2") is False
    assert store.get(key)["lease_owner"] == "worker-1"
    assert store.release_claim(key, "worker-1") is True
    assert store.get(key) == created, "giving a claim back changed the entry"
    assert store.claim_due(key, scheduler.clock.time(), "worker-2", 300) is not None

    assert store.remove(scheduler.principal.tenant_id, "daily") is True
    assert store.release_claim(key, "worker-2") is False
    assert store.entries() == []


def test_a_scheduled_turn_that_fails_after_the_agent_was_asked_stays_charged(
        product_scheduler, tmp_path):
    """The other side of the same rule: a turn the agent was asked for is
    charged what the agent counted, whether or not it ended in a reply."""
    quota, agent = _metered(tmp_path)
    agent.spends = {
        _internal_session(SCHEDULER_TENANT, "report"): {"input_tokens": 700, "output_tokens": 300}}
    agent.script(RuntimeError("the model call failed after it was paid for"))
    scheduler = product_scheduler(agent=agent, quota_store=quota, data_manager=_Footprints())
    scheduler.add("report")

    scheduler.scheduler.tick()

    assert len(agent.turns) == 1
    assert scheduler.schedule("report")["last_status"] == "failed"
    usage = quota.tenant_usage(SCHEDULER_TENANT)
    assert (usage["tokens_today"], usage["reserved_tokens"]) == (1000, 0)


# ── F-26: every piece of tenant work is work in flight ──────────────────


class Held:
    """A collaborator call that keeps its first caller inside it until
    ``released`` is set, and lets every later caller straight through.
    ``finishing`` is called on the first caller's thread as it leaves."""

    def __init__(self, result=None, finishing=None):
        self.result = result
        self.finishing = finishing
        self.entered = threading.Event()
        self.released = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, *args, **kwargs):
        with self._lock:
            self.calls += 1
            first = self.calls == 1
        if first:
            self.entered.set()
            assert self.released.wait(10), "the held call was never released"
            if self.finishing is not None:
                self.finishing()
        return self.result


OPS_ROUTES = {"/v1/health", "/v1/ready", "/v1/version", "/v1/metrics",
              "/v1/metrics/prometheus"}
_CHAT = json.dumps({"session": "work", "message": "hello"}).encode()
_SCHEDULE = json.dumps({"name": "daily", "session": "work", "message": "report",
                        "every_ms": HOUR_MS}).encode()
# route as dispatch() names it, method, path, body, the status of a request
# that is served, and the collaborator call that does its work
WORK_ROUTES = [
    ("/v1/chat", "POST", "/v1/chat", _CHAT, 200, "turn_with_usage"),
    ("/v1/schedules", "GET", "/v1/schedules", b"", 200, "for_tenant"),
    ("/v1/schedules", "POST", "/v1/schedules", _SCHEDULE, 201, "put"),
    ("/v1/schedules/{name}", "DELETE", "/v1/schedules/daily", b"", 200, "remove"),
    ("/v1/sessions/{session}/export", "GET", "/v1/sessions/work/export", b"", 200, "export"),
    ("/v1/sessions/{session}", "DELETE", "/v1/sessions/work", b"", 200, "delete"),
]


def _service_over_held_collaborators():
    """A ProductService whose agent, schedule store and data manager are
    ``Held`` calls, by the name of the method the service calls."""
    held = {
        "turn_with_usage": Held(("done", {"input_tokens": 2, "output_tokens": 1})),
        "for_tenant": Held([]),
        "put": Held(None),
        "remove": Held(True),
        "export": Held({"session": "work"}),
        "delete": Held(True),
    }
    agent = SimpleNamespace(manifest={"read_file": {}}, memory=None, _mcp=object(),
                            turn_with_usage=held["turn_with_usage"])
    store = SimpleNamespace(is_healthy=lambda: True, **{
        name: held[name] for name in ("for_tenant", "put", "remove")})
    data = SimpleNamespace(is_healthy=lambda: True, **{
        name: held[name] for name in ("export", "delete")})
    service = ProductService(
        agent, AuthRegistry(_auth_entries()), schedule_store=store, data_manager=data,
        log_sink=lambda record: None)
    return service, held


@pytest.mark.parametrize(
    "route, method, path, body, served, call", WORK_ROUTES,
    ids=[f"{method} {route}" for route, method, *_ in WORK_ROUTES])
def test_a_drain_waits_for_the_work_in_flight_and_refuses_more(
        route, method, path, body, served, call):
    """One request is inside its work when the drain begins. The drain is not
    complete until it has finished, and it does finish, with its usual
    answer. A second request for the same work is refused before it starts.
    Before, only a chat turn was counted: a drain reported complete, and the
    clean-shutdown marker was written, with a deletion in flight."""
    service, held = _service_over_held_collaborators()
    work = held[call]
    replies = []
    request = threading.Thread(
        target=lambda: replies.append(
            service.dispatch(method, path, _bearer(TENANT_TOKEN), body)),
        daemon=True)
    request.start()
    assert work.entered.wait(5), "the request never reached its work"

    service.start_draining()
    try:
        complete = service.wait_for_drain(0)
        status, _, answer = service.dispatch(method, path, _bearer(TENANT_TOKEN), body)
    finally:
        work.released.set()
        request.join(5)

    assert complete is False, (
        f"the drain reported complete while {method} {route} was in flight")
    assert (status, answer.get("error", {}).get("code")) == (503, "service_draining"), (
        f"{method} {route} was accepted after the drain began: {status}")
    assert work.calls == 1, "work started after the drain began"
    assert [reply[0] for reply in replies] == [served], "the work in flight did not finish"
    assert service.wait_for_drain(5) is True


def test_the_operational_reads_keep_answering_through_a_drain():
    """Health, readiness, version and the metrics are how a drain is watched.
    They are never refused and never counted as work."""
    service, _ = _service_over_held_collaborators()
    service.start_draining()

    answers = {path: service.dispatch("GET", path, _bearer(OPS_TOKEN))
               for path in sorted(OPS_ROUTES)}

    assert {path: answer[0] for path, answer in answers.items()} == {
        "/v1/health": 200, "/v1/metrics": 200, "/v1/metrics/prometheus": 200,
        "/v1/ready": 503, "/v1/version": 200}
    ready = answers["/v1/ready"][2]
    assert (ready["status"], ready["draining"]) == ("not_ready", True)
    assert answers["/v1/metrics"][2]["metrics"]["service_draining"] is True
    assert service.wait_for_drain(0) is True


def _routes_dispatch_names():
    tree = ast.parse((PI_ROOT / "product_service.py").read_text())
    service = next(node for node in tree.body
                   if isinstance(node, ast.ClassDef) and node.name == "ProductService")
    dispatch = next(node for node in service.body
                    if isinstance(node, ast.FunctionDef) and node.name == "dispatch")
    return {node.value.value for node in ast.walk(dispatch)
            if isinstance(node, ast.Assign)
            and [getattr(target, "id", None) for target in node.targets] == ["route"]
            and isinstance(node.value, ast.Constant)}


def test_every_route_is_work_a_drain_waits_for_or_an_operational_read():
    """The guard for the class. Whoever adds a route says which it is: work,
    and then the test above holds it to the drain, or an operational read."""
    routes = _routes_dispatch_names() - {"unknown"}
    assert routes, "the scan found no route at all; it no longer matches dispatch()"
    work = {route for route, *_ in WORK_ROUTES}
    assert routes == work | OPS_ROUTES, (
        f"dispatch() serves {sorted(routes)}; the drain tests cover "
        f"{sorted(work)} as work and {sorted(OPS_ROUTES)} as operational reads")


def test_a_drain_waits_for_a_chat_turn_s_quota_to_be_released(tmp_path):
    """A chat turn's place in its tenant's concurrency is given back once the
    turn is over: for every turn when resource limits are off, and with them
    on for a request that never reached its session. That write came after
    the turn had left the drain's count, so a drain could complete, and the
    process exit, with the release half done; the place then stayed taken
    until its lease ran out."""
    quota = DurableQuotaStore(tmp_path / "quota.sqlite3")
    releasing = Held()
    settle = quota.settle_turn

    def held_settle(*args, **kwargs):
        releasing()
        return settle(*args, **kwargs)

    quota.settle_turn = held_settle
    agent = SimpleNamespace(manifest={"read_file": {}}, memory=None, _mcp=object(),
                            turn_with_usage=lambda *args, **kwargs: ("done", {}))
    service = ProductService(agent, AuthRegistry(_auth_entries()), quota_store=quota,
                             log_sink=lambda record: None)
    replies = []
    request = threading.Thread(
        target=lambda: replies.append(
            service.dispatch("POST", "/v1/chat", _bearer(TENANT_TOKEN), _CHAT)),
        daemon=True)
    request.start()
    assert releasing.entered.wait(5), "the turn never came to release its quota"

    service.start_draining()
    try:
        complete = service.wait_for_drain(0)
    finally:
        releasing.released.set()
        request.join(5)

    assert complete is False, (
        "the drain reported complete while a turn's quota was being released")
    assert [reply[0] for reply in replies] == [200]
    assert service.wait_for_drain(5) is True


# ── the same pattern in the retention sweep ─────────────────────────────


def test_a_retention_sweep_deletes_nothing_more_after_a_stop_request():
    """The retention sweep had the scheduler's defect: it walked every
    inactive session without looking at its stop flag. Five sessions have
    expired and the first deletion is in flight when the stop is requested.
    That deletion finishes. The other four sessions and the schedules are
    left for the next sweep, which startup runs before it serves."""
    deleting = Held()
    deleted, late = [], []

    def delete_files(internal):
        deleted.append(internal)
        deleting()

    quota = SimpleNamespace(
        inactive_sessions=lambda cutoff: [
            (TENANT, f"s{i}", f"internal-{i}", 0) for i in range(5)],
        registered_session_inactive=lambda *args: True,
        clear_session_bytes=lambda *args: None,
        remove_registered_session=lambda *args, **kwargs: True)
    schedules = SimpleNamespace(
        internal_active=lambda *args: False, remove_internal=lambda *args: 0,
        remove_inactive=lambda *args: late.append("schedules") or 0)
    monitor = ProductRetentionMonitor(
        quota, SimpleNamespace(delete_internal_files=delete_files), schedules,
        SessionOperationLocks(), retention_s=1, interval_s=3600,
        log_sink=lambda record: None)
    monitor.start()
    try:
        assert deleting.entered.wait(5), "the sweep never began"
        assert monitor.stop(0) is False, "the deletion in flight was abandoned"
    finally:
        deleting.released.set()
    assert monitor.stop(10) is True

    assert deleted == ["internal-0"], (
        f"{len(deleted) - 1} session(s) were deleted after the stop request")
    assert late == [], "the schedules were swept after the stop request"
    report = monitor.snapshot()
    assert (report["deleted_sessions_total"], report["failures_total"]) == (1, 0)


# ── F-26: one budget ────────────────────────────────────────────────────

PARTS = ("listener", "scheduler", "retention", "audit", "requests")


class Part:
    """A loop, or the service's work in flight, as a drain sees it. Every
    call is recorded in ``asked``. A wait, which is any call with a timeout
    above zero, first runs ``waiting(name)`` and then reports ``drains``."""

    def __init__(self, name, asked, waiting=None, drains=True):
        self.name, self.asked, self.waiting, self.drains = name, asked, waiting, drains

    def stop(self, timeout_s):
        self.asked.append((self.name, timeout_s))
        if timeout_s and self.waiting is not None:
            self.waiting(self.name)
        return self.drains

    wait_for_drain = stop

    def start_draining(self):
        self.asked.append((self.name, "start_draining"))


class Listener:
    def __init__(self, asked, waiting=None):
        self.asked, self.waiting = asked, waiting

    def shutdown(self):
        self.asked.append(("listener", "shutdown"))
        if self.waiting is not None:
            self.waiting("listener")

    def server_close(self):
        self.asked.append(("listener", "server_close"))


def _drain(budget, asked, waiting=None, undrained=()):
    """Drain doubles of the listener, the three loops and the requests."""
    scheduler, retention, audit, requests = (
        Part(name, asked, waiting, drains=name not in undrained) for name in PARTS[1:])
    return product_service.drain_service(
        requests, Listener(asked, waiting), (scheduler, retention, audit), budget)


def test_a_drain_refuses_new_work_and_asks_every_loop_to_stop_before_it_waits():
    """New work is refused first. Then every loop is asked to stop, so that
    none begins another round while the drain waits for a different one, and
    only then is anything waited for. Each wait is handed the budget, and the
    listening socket is closed last."""
    asked = []

    assert _drain(30, asked) is True

    assert asked[:4] == [("requests", "start_draining"),
                         ("scheduler", 0), ("retention", 0), ("audit", 0)]
    assert sorted(asked[4:9]) == sorted([
        ("listener", "shutdown"), ("scheduler", 30), ("retention", 30), ("audit", 30),
        ("requests", 30)])
    assert asked[9:] == [("listener", "server_close")]


def test_everything_a_drain_waits_for_is_waited_for_at_once():
    """Each of the five waits meets the other four before it may end, which
    is possible only if all five are in progress together. They used to run
    one after the other, each with the whole budget: four budgets to find
    out that nothing was going to stop."""
    together = threading.Barrier(len(PARTS))
    alone = []

    def waiting(name):
        try:
            together.wait(5)
        except threading.BrokenBarrierError:
            alone.append(name)

    assert _drain(30, [], waiting) is True
    assert alone == [], f"these were waited for one at a time: {sorted(alone)}"


@pytest.mark.parametrize("undrained", PARTS[1:])
def test_a_drain_is_complete_only_when_everything_reports_it_finished(undrained):
    """Whichever loop says it did not stop, or if work is still in flight,
    the drain did not complete."""
    asked = []

    assert _drain(30, asked, undrained={undrained}) is False
    assert asked[-1] == ("listener", "server_close")


@pytest.mark.parametrize(
    "stuck",
    [tuple(name for bit, name in enumerate(PARTS) if number >> bit & 1)
     for number in range(1, 1 << len(PARTS))],
    ids="+".join)
def test_a_drain_stops_waiting_at_its_deadline_whatever_is_still_running(stuck):
    """The guard for the class: any of the five that never finish, alone or
    together, cost the drain its one budget and no more. It returns at the
    deadline, says that it did not complete, and closes the listening socket.
    A listener that would not stop used to hold shutdown for good."""
    entered = {name: threading.Event() for name in PARTS}
    release = threading.Event()
    asked = []

    def waiting(name):
        entered[name].set()
        if name in stuck:
            release.wait(30)

    began = time.monotonic()
    try:
        drained = _drain(0.05, asked, waiting)
        elapsed = time.monotonic() - began
        still_stuck = not release.is_set()
    finally:
        release.set()

    assert drained is False
    assert still_stuck and elapsed < 10, f"the drain took {elapsed:.1f} s on a 0.05 s budget"
    assert all(entered[name].wait(10) for name in stuck), "a wait was never begun"
    assert ("listener", "server_close") in asked


# ── the real pieces together ────────────────────────────────────────────


def test_one_call_drains_a_running_product(product_scheduler, product_server):
    """The real service, the real scheduler thread and the real listener,
    with a chat turn and a scheduled turn in flight and two more entries due.
    The drain waits for both turns, both finish with their usual outcome,
    nothing else starts, and the listener is closed."""
    work = product_service.InFlightWork()
    auth = AuthRegistry(_auth_entries())
    scheduler = product_scheduler(auth=auth, work=work)
    service = ProductService(
        scheduler.agent, auth, work=work, scheduler=scheduler.scheduler,
        schedule_store=scheduler.store, session_locks=scheduler.locks,
        log_sink=lambda record: None)
    server = product_server(service)
    for name in ("job0", "job1", "job2"):
        scheduler.add(name)
    scheduler.agent.hold()
    scheduler.start()
    scheduler.agent.wait_for_turns(1)
    chat = []
    client = threading.Thread(
        target=lambda: chat.append(server.request(
            "POST", "/v1/chat", headers=_bearer(TENANT_TOKEN), body=_CHAT, timeout=20)),
        daemon=True)
    client.start()
    scheduler.agent.wait_for_turns(2)

    outcome = []
    drain = threading.Thread(
        target=lambda: outcome.append(product_service.drain_service(
            service, server.server, (scheduler.scheduler,), 20)),
        daemon=True)
    drain.start()
    scheduler.wait_for(lambda: service.readiness_snapshot()["draining"], "the drain began")
    assert service.wait_for_drain(0) is False and outcome == []
    scheduler.agent.release()
    drain.join(20)
    client.join(20)

    assert outcome == [True]
    assert [reply.status for reply in chat] == [200]
    assert len(scheduler.agent.turns) == 2, "a turn started after the drain began"
    assert sorted(str(scheduler.schedule(name)["last_status"])
                  for name in ("job0", "job1", "job2")) == ["None", "None", "ok"]
    assert not scheduler.thread.is_alive()
    with pytest.raises(OSError):
        server.connect(timeout=2)


# ── product_main: the marker is written only after a complete drain ─────


def _product_env(tmp_path, monkeypatch, drain_seconds=None):
    """test_product_main's environment, with a principal that may delete."""
    _base_env(tmp_path, monkeypatch)
    now = int(time.time())
    (tmp_path / "auth.json").write_text(json.dumps({"tokens": [
        {**entry, "tools": [], "not_before_unix": now - 60, "expires_unix": now + 86400}
        for entry in _auth_entries()]}))
    if drain_seconds is not None:
        monkeypatch.setenv("PI_DRAIN_TIMEOUT_SECONDS", str(drain_seconds))
    return tmp_path / "state" / ".product-clean-shutdown.json"


def test_shutdown_waits_for_the_scheduler_and_the_retention_sweep_at_once(
        tmp_path, monkeypatch):
    """``product_main`` itself: the scheduler's wait and the retention
    monitor's wait each meet the other before they may end. Shutdown used to
    wait for the scheduler, the retention monitor, the audit monitor and the
    requests in that order, and gave each the whole of
    ``PI_DRAIN_TIMEOUT_SECONDS``."""
    marker = _product_env(tmp_path, monkeypatch)
    together = threading.Barrier(2)
    alone = []

    def stopping(loop, timeout):
        if timeout:
            try:
                together.wait(3)
            except threading.BrokenBarrierError:
                alone.append(loop)

    _install_happy_runtime(monkeypatch, stopping=stopping)

    product_main.run()

    assert alone == [], (
        f"shutdown waited for the {' and then the '.join(alone)} one at a time, "
        f"each with the whole drain budget")
    assert marker.exists()


def _deletion_in_flight(marker, seen, finishes_after_s=None):
    """A ``during`` hook for ``_install_happy_runtime``: when the product
    stops, a DELETE is inside the data manager. It finishes
    ``finishes_after_s`` seconds later, or when the test releases it."""
    def during(service):
        def finishing():
            seen["marker_when_the_deletion_finished"] = marker.exists()

        held = Held(True, finishing)
        service.data_manager.delete = held
        seen["held"] = held
        seen["request"] = threading.Thread(
            target=lambda: seen.update(reply=service.dispatch(
                "DELETE", "/v1/sessions/work", _bearer(TENANT_TOKEN))),
            daemon=True)
        seen["request"].start()
        assert held.entered.wait(5), "the DELETE never reached the data manager"
        if finishes_after_s is not None:
            threading.Timer(finishes_after_s, held.released.set).start()
    return during


def test_the_marker_is_written_only_after_the_work_in_flight_has_finished(
        tmp_path, monkeypatch):
    """A deletion is in flight when the product is told to stop and finishes
    0.2 seconds later. Shutdown waits for it, and only then writes the marker
    that lets an offline backup run. The marker used to be on disk while the
    deletion was still removing files."""
    marker = _product_env(tmp_path, monkeypatch)
    seen = {}
    _install_happy_runtime(
        monkeypatch, during=_deletion_in_flight(marker, seen, finishes_after_s=0.2))

    product_main.run()
    seen["request"].join(10)

    assert seen["reply"][0] == 200
    assert seen["marker_when_the_deletion_finished"] is False, (
        "the clean-shutdown marker was written while a DELETE was still in flight")
    assert marker.exists()


def test_work_that_outlives_the_budget_leaves_no_marker(tmp_path, monkeypatch):
    """The deletion is still running when the one-second budget ends. The
    shutdown is not clean, says so, and writes no marker."""
    marker = _product_env(tmp_path, monkeypatch, drain_seconds=1)
    seen = {}
    _install_happy_runtime(monkeypatch, during=_deletion_in_flight(marker, seen))

    try:
        with pytest.raises(RuntimeError, match="did not drain within 1 seconds"):
            product_main.run()
        assert not marker.exists()
    finally:
        seen["held"].released.set()
        seen["request"].join(10)
    assert not marker.exists()


def test_product_main_gives_the_scheduler_the_service_s_drain_state_and_stops_every_loop(
        tmp_path, monkeypatch):
    """The wiring. The scheduler is built with the same count of work in
    flight as the service, and shutdown stops the scheduler, the retention
    monitor and the audit monitor."""
    marker = _product_env(tmp_path, monkeypatch)
    seen = {}
    events = _install_happy_runtime(
        monkeypatch, during=lambda service: seen.update(service=service))

    product_main.run()

    service = seen["service"]
    shared = dict(events)["scheduler_init"]
    assert isinstance(shared, product_service.InFlightWork)
    assert shared is service.work
    stopped = {name for name, _ in events}
    assert {"scheduler_stop", "retention_stop"} <= stopped
    assert service.audit_monitor.stop(0) is True, "the audit monitor was left running"
    assert service.readiness_snapshot()["draining"] is True
    assert marker.exists()
