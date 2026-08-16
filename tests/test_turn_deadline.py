"""Turn deadline and client-disconnect abandonment (issue #18, increment 1).

POST /chat blocks for the whole turn: up to max_steps LLM round trips plus a
forge per tool call, silent throughout. PI_MAX_STEPS bounds the number of
round trips, not wall-clock — a slow upstream makes a turn arbitrarily long —
and a client that gives up does not stop the turn: the host keeps forging,
keeps spending the api key, and (because forges serialize process-wide, #17)
keeps every other session waiting.

Two pure host-side bounds, both checked at STEP boundaries:
- a wall-clock deadline per turn (PI_TURN_DEADLINE_S; monotonic clock, so a
  wall-clock jump cannot kill a healthy turn);
- an `abort` callback threaded from the HTTP handler, wired to a socket-EOF
  peek, so a disconnected client stops costing money at the next boundary.

Step boundaries only, deliberately: a forge in flight is already fuel-bounded
and an LLM call has its own bounded retries — interrupting mid-step would buy
little and cost the invariant that every started step is audited whole.
"""
import json
import socket
import time
import urllib.request
from types import SimpleNamespace

import pytest

from agent import PiAgent, SessionStore, TurnAbandoned, serve
from conftest import PI_ROOT


def _looping_agent(tmp_path, **kwargs):
    """A PiAgent whose scripted forge asks for a tool on every step — the
    shape of a turn that never converges. Steps are counted so tests can
    assert WHEN the loop stopped, not just that it did."""
    agent = PiAgent("http://127.0.0.1:9/v1/messages", "key",
                    store=SessionStore(tmp_path / "s"),
                    sandbox_root=tmp_path / "b", mcp=None, **kwargs)
    calls = []

    def fake_forge(source, input_text, grants, fuel=20_000_000, **kw):
        calls.append(kw.get("kind", "?"))
        if kw.get("kind") == "llm":
            return "raw", None
        if kw.get("kind") == "parse":
            # one tool_use frame + usage: the loop continues
            payload = "tu1\x1fread_file\x1f" + json.dumps({"path": "x"})
            frames = (f"u{len(payload.encode()):08d}{payload}").encode()
            return frames.decode(), None
        return "file contents", None

    agent._forge = fake_forge
    agent._llm_src = "stub"
    agent._parse_src = "stub"
    return agent, calls


def _decode_stub(agent):
    """Route parse through decode_frames exactly as the real path does."""
    return agent


# ── the deadline ────────────────────────────────────────────────────────


def test_a_turn_that_outruns_its_deadline_fails_loudly(tmp_path):
    agent, calls = _looping_agent(tmp_path, turn_deadline_s=10)
    clock = iter(range(0, 1000, 4)).__next__   # +4s per look
    agent._now = clock
    with pytest.raises(RuntimeError, match="deadline"):
        agent.turn("s", "hi")
    # the loop stopped at a boundary well before max_steps exhausted itself
    assert 0 < len([c for c in calls if c == "llm"]) < agent.max_steps


def test_no_deadline_means_the_old_contract(tmp_path):
    agent, calls = _looping_agent(tmp_path)
    with pytest.raises(RuntimeError, match="no final answer"):
        agent.turn("s", "hi")
    assert len([c for c in calls if c == "llm"]) == agent.max_steps


def test_the_deadline_error_names_the_budget_and_the_knob(tmp_path):
    """Operational errors here are written for the person who will read
    them: the message must say what the budget was and which knob sets it."""
    agent, _ = _looping_agent(tmp_path, turn_deadline_s=10)
    agent._now = iter(range(0, 1000, 6)).__next__
    with pytest.raises(RuntimeError, match=r"10.*PI_TURN_DEADLINE_S"):
        agent.turn("s", "hi")


def test_partial_history_survives_a_deadline_kill(tmp_path):
    """The finally-persist contract (M7) must hold on this exit path too:
    a deadline kill loses the turn, never the session."""
    agent, _ = _looping_agent(tmp_path, turn_deadline_s=10)
    agent._now = iter(range(0, 1000, 6)).__next__
    with pytest.raises(RuntimeError):
        agent.turn("s", "hi")
    saved = agent.store.load("s")
    assert saved and saved[0]["content"] == "hi"


def test_a_negative_deadline_is_a_named_config_error(tmp_path):
    with pytest.raises(ValueError, match="turn_deadline_s"):
        PiAgent("http://127.0.0.1:9/v1/messages", "k",
                store=SessionStore(tmp_path / "s"),
                sandbox_root=tmp_path / "b", mcp=None, turn_deadline_s=-1)


def test_deadline_uses_a_monotonic_clock():
    """A wall-clock jump (NTP step, DST) must not kill a healthy turn. Pin
    the source rather than the symptom: the default clock is time.monotonic."""
    src = (PI_ROOT / "agent.py").read_text()
    assert "self._now = time.monotonic" in src, \
        "the turn deadline must be measured on the monotonic clock"


# ── the abort callback ──────────────────────────────────────────────────


def test_an_aborted_turn_stops_at_the_next_boundary(tmp_path):
    agent, calls = _looping_agent(tmp_path)
    with pytest.raises(TurnAbandoned):
        agent.turn_with_usage("s", "hi",
                              abort=lambda: len(calls) >= 4)
    n_after = len(calls)
    assert n_after < agent.max_steps * 3, "abort did not stop the loop"


def test_abort_is_checked_before_spending_not_after(tmp_path):
    """An abort that is already true at turn start must cost ZERO forges —
    the point is to stop spending the api key for a caller who is gone."""
    agent, calls = _looping_agent(tmp_path)
    with pytest.raises(TurnAbandoned):
        agent.turn_with_usage("s", "hi", abort=lambda: True)
    assert calls == [], "a pre-aborted turn still reached the forge"


def test_an_abandoned_turn_still_persists_history(tmp_path):
    agent, _ = _looping_agent(tmp_path)
    with pytest.raises(TurnAbandoned):
        agent.turn_with_usage("s", "hi", abort=lambda: True)
    assert agent.store.load("s"), "abandonment lost the session history"


# ── the HTTP wiring ─────────────────────────────────────────────────────


def test_serve_passes_abort_only_to_agents_that_accept_it():
    """The seam is duck-typed on purpose: every stub agent in this suite is a
    SimpleNamespace with a two-arg lambda, and serve() must keep working with
    all of them. Introspect, don't except — a TypeError catch would mask real
    TypeErrors raised inside the turn."""
    src = (PI_ROOT / "agent.py").read_text()
    assert "inspect.signature" in src, \
        "serve() must introspect turn_with_usage for the abort parameter"
    server = serve(SimpleNamespace(turn_with_usage=lambda s, m: (f"ok:{m}", {})),
                   port=0)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/chat"
        req = urllib.request.Request(
            url, data=json.dumps({"session": "s", "message": "m"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            assert json.loads(r.read())["reply"] == "ok:m"
    finally:
        server.shutdown()


def test_a_disconnected_client_aborts_the_turn(tmp_path):
    """End to end over a real socket: the client sends a request and hangs
    up; the turn must stop early rather than run all max_steps. The scripted
    forge sleeps briefly per step so the disconnect lands mid-turn."""
    agent, calls = _looping_agent(tmp_path)
    real_forge = agent._forge

    def slow_forge(*a, **kw):
        time.sleep(0.05)
        return real_forge(*a, **kw)

    agent._forge = slow_forge
    server = serve(agent, port=0)
    try:
        body = json.dumps({"session": "s", "message": "hi"}).encode()
        raw = (b"POST /chat HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
               b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        with socket.create_connection(("127.0.0.1", server.server_address[1])) as c:
            c.sendall(raw)
            time.sleep(0.12)   # let a step or two run
        # socket closed here — the turn should notice at a boundary
        deadline = time.time() + 5
        while time.time() < deadline and not getattr(agent, "last_usage", None) is None:
            n_before = len(calls)
            time.sleep(0.3)
            if len(calls) == n_before:
                break
        assert len([c for c in calls if c == "llm"]) < agent.max_steps, (
            "the turn ran to max_steps despite the client hanging up")
    finally:
        server.shutdown()
