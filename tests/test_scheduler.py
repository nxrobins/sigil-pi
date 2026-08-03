"""M15 — scheduled agent turns: the README's longest-standing open milestone.

sigil-serve has always had scheduling, but it drives ONE forged tool, which on
that stack means `chat_turn` — the single-turn path. Nothing scheduled the
*agent loop*. That needs a scheduler in this host, and this is it.

Design constraints taken from the rest of the host, not invented here:

  DURABLE      — an entry and its last-run mark survive a restart, like
                 SessionStore. A scheduler that forgets on restart silently
                 stops doing the thing it was configured for.
  CATCH-UP ONCE — a host down for six hours must not wake and fire a
                 five-minute job seventy times. It runs once and resumes the
                 cadence. This is the property most naive schedulers get wrong.
  NO OVERLAP   — a turn that outruns its own interval must not stack. The next
                 fire waits; it does not run concurrently with itself.
  INJECTABLE CLOCK — every timing property is tested deterministically. A
                 scheduler tested with sleeps is a scheduler tested badly.
"""
import json
import threading

import pytest

from conftest import API_KEY, msg, text  # noqa: F401  (fixtures)


class FakeClock:
    """Monotonic time under test control. `advance` is what a real deployment
    experiences as time passing; nothing here ever sleeps."""

    def __init__(self, now=1_000_000.0):
        self._now = now
        self.slept = []

    def time(self):
        return self._now

    def sleep(self, seconds):
        self.slept.append(seconds)

    def advance(self, seconds):
        self._now += seconds


def _store(tmp_path):
    from agent import ScheduleStore
    return ScheduleStore(tmp_path / "schedules")


# ── the durable store ────────────────────────────────────────────────────


def test_entries_round_trip_and_survive_a_fresh_store(tmp_path):
    s = _store(tmp_path)
    s.put("nightly", session="ops", message="summarize", every_ms=3_600_000)
    fresh = _store(tmp_path)
    [e] = fresh.entries()
    assert e["name"] == "nightly"
    assert e["session"] == "ops"
    assert e["message"] == "summarize"
    assert e["every_ms"] == 3_600_000
    assert e["last_run"] is None


def test_put_replaces_by_name_rather_than_duplicating(tmp_path):
    s = _store(tmp_path)
    s.put("job", session="a", message="one", every_ms=1000)
    s.put("job", session="b", message="two", every_ms=2000)
    [e] = s.entries()
    assert (e["session"], e["message"], e["every_ms"]) == ("b", "two", 2000)


def test_remove_and_missing_name(tmp_path):
    s = _store(tmp_path)
    s.put("job", session="a", message="m", every_ms=1000)
    assert s.remove("job") is True
    assert s.entries() == []
    assert s.remove("nope") is False


def test_last_run_is_durable(tmp_path):
    s = _store(tmp_path)
    s.put("job", session="a", message="m", every_ms=1000)
    s.mark_run("job", 12345.0)
    assert _store(tmp_path).entries()[0]["last_run"] == 12345.0


@pytest.mark.parametrize("bad", [0, -1, -1000])
def test_non_positive_interval_is_rejected(tmp_path, bad):
    """An interval of zero is a busy loop wearing a schedule's clothing."""
    s = _store(tmp_path)
    with pytest.raises(ValueError, match="every_ms"):
        s.put("job", session="a", message="m", every_ms=bad)


def test_entry_names_are_not_filesystem_paths(tmp_path):
    """One file holds every entry, so a traversal-shaped NAME cannot escape —
    but pin it, because the obvious alternative (a file per entry) would."""
    s = _store(tmp_path)
    s.put("../../etc/passwd", session="a", message="m", every_ms=1000)
    assert [e["name"] for e in s.entries()] == ["../../etc/passwd"]
    assert not (tmp_path / "schedules").is_dir() or \
        list((tmp_path / "schedules").rglob("passwd")) == []


# ── due-ness: the arithmetic, in isolation ───────────────────────────────


def test_a_new_entry_is_due_immediately(tmp_path):
    from agent import due_at
    e = {"every_ms": 60_000, "last_run": None}
    assert due_at(e, now=1000.0) is True


def test_not_due_before_the_interval_elapses(tmp_path):
    from agent import due_at
    e = {"every_ms": 60_000, "last_run": 1000.0}
    assert due_at(e, now=1000.0 + 59.9) is False
    assert due_at(e, now=1000.0 + 60.0) is True


def test_a_long_outage_fires_once_not_once_per_missed_interval(tmp_path):
    """CATCH-UP ONCE. Six hours of downtime on a five-minute job is 72 missed
    fires; a scheduler that queues them all wakes up and hammers the API.
    Due-ness is a boolean, not a backlog — and the next mark_run resumes the
    cadence from NOW rather than from the missed slot."""
    from agent import due_at
    e = {"every_ms": 300_000, "last_run": 1000.0}
    assert due_at(e, now=1000.0 + 6 * 3600) is True   # due, but only once


# ── the runner ───────────────────────────────────────────────────────────


class RecordingAgent:
    """Stands in for PiAgent: records turns, optionally blocks, optionally
    raises — the three behaviours the runner must handle."""

    def __init__(self, block=None, raises=False):
        self.turns = []
        self._block = block
        self._raises = raises
        self.lock = threading.Lock()

    def turn(self, session, message):
        with self.lock:
            self.turns.append((session, message))
        if self._block is not None:
            self._block.wait(timeout=5)
        if self._raises:
            raise RuntimeError("scheduled turn blew up")
        return "ok"


def _runner(tmp_path, agent, clock):
    from agent import Scheduler
    return Scheduler(_store(tmp_path), agent, clock=clock)


def test_tick_runs_a_due_entry_and_marks_it(tmp_path):
    clock, agent = FakeClock(), RecordingAgent()
    sched = _runner(tmp_path, agent, clock)
    sched.store.put("job", session="s", message="do it", every_ms=60_000)
    sched.tick()
    assert agent.turns == [("s", "do it")]
    assert sched.store.entries()[0]["last_run"] == clock.time()


def test_tick_does_not_rerun_before_the_interval(tmp_path):
    clock, agent = FakeClock(), RecordingAgent()
    sched = _runner(tmp_path, agent, clock)
    sched.store.put("job", session="s", message="m", every_ms=60_000)
    sched.tick()
    clock.advance(59)
    sched.tick()
    assert len(agent.turns) == 1
    clock.advance(2)
    sched.tick()
    assert len(agent.turns) == 2


def test_a_failing_scheduled_turn_does_not_kill_the_scheduler(tmp_path):
    """A scheduled job that raises must not take the loop down with it —
    otherwise one bad entry silently stops every other schedule."""
    clock = FakeClock()
    boom = RecordingAgent(raises=True)
    sched = _runner(tmp_path, boom, clock)
    sched.store.put("bad", session="s", message="m", every_ms=1000)
    sched.tick()                       # must not raise
    assert len(boom.turns) == 1
    # and it is still marked, so a permanently-failing job retries on its
    # cadence rather than spinning every tick
    assert sched.store.entries()[0]["last_run"] == clock.time()


def test_a_slow_turn_does_not_stack_with_itself(tmp_path):
    """NO OVERLAP. A turn that outruns its interval must not be re-entered —
    two concurrent turns for one session would serialize on the session lock
    anyway, but the queue behind them would grow without bound."""
    clock = FakeClock()
    gate = threading.Event()
    slow = RecordingAgent(block=gate)
    sched = _runner(tmp_path, slow, clock)
    sched.store.put("job", session="s", message="m", every_ms=1000)

    t = threading.Thread(target=sched.tick, daemon=True)
    t.start()
    while not slow.turns:                      # wait until it is mid-turn
        pass
    clock.advance(10_000)                      # long past due again
    sched.tick()                               # must be a no-op: still running
    assert len(slow.turns) == 1, "the entry re-entered while still running"
    gate.set()
    t.join(timeout=5)

    clock.advance(10_000)
    sched.tick()
    assert len(slow.turns) == 2, "the entry never became eligible again"


def test_entries_are_independent(tmp_path):
    clock, agent = FakeClock(), RecordingAgent()
    sched = _runner(tmp_path, agent, clock)
    sched.store.put("fast", session="s", message="fast", every_ms=1_000)
    sched.store.put("slow", session="s", message="slow", every_ms=100_000)
    sched.tick()
    clock.advance(2)
    sched.tick()
    messages = [m for _, m in agent.turns]
    assert messages.count("fast") == 2
    assert messages.count("slow") == 1


def test_removing_an_entry_stops_it(tmp_path):
    clock, agent = FakeClock(), RecordingAgent()
    sched = _runner(tmp_path, agent, clock)
    sched.store.put("job", session="s", message="m", every_ms=1000)
    sched.tick()
    sched.store.remove("job")
    clock.advance(10_000)
    sched.tick()
    assert len(agent.turns) == 1


def test_scheduler_survives_a_restart_and_resumes_the_cadence(tmp_path):
    """The durability property end to end: a fresh Scheduler over the same
    directory honours the previous run's mark rather than firing immediately."""
    clock, agent = FakeClock(), RecordingAgent()
    _runner(tmp_path, agent, clock).store.put("job", session="s",
                                              message="m", every_ms=60_000)
    first = _runner(tmp_path, agent, clock)
    first.tick()
    assert len(agent.turns) == 1

    clock.advance(30)
    fresh = _runner(tmp_path, agent, clock)    # brand-new instance
    fresh.tick()
    assert len(agent.turns) == 1, "a restart re-fired a job that was not due"
    clock.advance(31)
    fresh.tick()
    assert len(agent.turns) == 2


# ── the HTTP surface ─────────────────────────────────────────────────────


def _post(server, path, payload):
    """POST and return (status, parsed-body-or-None). The body is optional
    because an unknown route answers via send_error, which emits HTML — the
    same shape /chat's 404 has always had."""
    import urllib.error
    import urllib.request

    def parse(raw):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    req = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}{path}",
        data=json.dumps(payload).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, parse(r.read())
    except urllib.error.HTTPError as e:
        return e.code, parse(e.read())


def test_schedule_endpoints(tmp_path):
    from types import SimpleNamespace
    from agent import Scheduler, serve
    clock, agent = FakeClock(), RecordingAgent()
    sched = Scheduler(_store(tmp_path), agent, clock=clock)
    stub = SimpleNamespace(turn=agent.turn, scheduler=sched)
    server = serve(stub, port=0)
    try:
        code, body = _post(server, "/schedule",
                           {"name": "n", "session": "s", "message": "m",
                            "every_ms": 60_000})
        assert code == 200 and body["ok"] is True
        code, body = _post(server, "/schedule/list", {})
        assert code == 200 and [e["name"] for e in body["entries"]] == ["n"]
        code, body = _post(server, "/schedule/remove", {"name": "n"})
        assert code == 200 and body["removed"] is True
        code, body = _post(server, "/schedule/list", {})
        assert body["entries"] == []
    finally:
        server.shutdown()


def test_schedule_endpoint_rejects_bad_input(tmp_path):
    from types import SimpleNamespace
    from agent import Scheduler, serve
    sched = Scheduler(_store(tmp_path), RecordingAgent(), clock=FakeClock())
    stub = SimpleNamespace(turn=lambda s, m: "ok", scheduler=sched)
    server = serve(stub, port=0)
    try:
        for payload in ({"name": "n"},                        # missing fields
                        {"name": "n", "session": "s", "message": "m",
                         "every_ms": 0},                      # non-positive
                        {"name": "n", "session": "s", "message": "m",
                         "every_ms": "soon"}):                # not a number
            code, _ = _post(server, "/schedule", payload)
            assert code == 400, f"{payload} was accepted"
    finally:
        server.shutdown()


def test_schedule_endpoints_are_absent_without_a_scheduler(tmp_path):
    """A deployment that configured no scheduler must not expose the surface
    at all — an endpoint that 500s is worse than one that 404s."""
    from types import SimpleNamespace
    from agent import serve
    server = serve(SimpleNamespace(turn=lambda s, m: "ok"), port=0)
    try:
        code, _ = _post(server, "/schedule",
                        {"name": "n", "session": "s", "message": "m",
                         "every_ms": 1000})
        assert code == 404
    finally:
        server.shutdown()
