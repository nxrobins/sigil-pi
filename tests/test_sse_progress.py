"""Step-level progress over SSE (issue #18, increment 2).

Increment 1 bounded a silent turn; this makes it audible. A caller who asks
for `text/event-stream` gets an event per completed step from the host loop —
which knows exactly where it is — and the reply as the terminal event. No
guest streams anything: token streaming needs an upstream streaming forge and
a story for crossing the ring without bypassing parse_reply, and quietly
parsing host-side would trade away the two-forge discipline for cosmetics.

Opt-in by content negotiation, so the existing JSON contract is untouched:
same route, same auth chokepoint, same body. A stream whose write fails is a
departed audience — exactly TurnAbandoned's meaning, so it reuses that exit.
"""
import json
import urllib.request
from types import SimpleNamespace

import pytest

from agent import PiAgent, SessionStore, serve
from test_turn_deadline import _looping_agent

TOKEN = "tok-SECRET-value"


def _finishing_agent(tmp_path, tool_steps=2):
    """A PiAgent whose scripted forge runs `tool_steps` tool rounds and then
    answers — the shape of a real converging turn."""
    agent = PiAgent("http://127.0.0.1:9/v1/messages", "key",
                    store=SessionStore(tmp_path / "s"),
                    sandbox_root=tmp_path / "b", mcp=None)
    state = {"rounds": 0}

    def fake_forge(source, input_text, grants, fuel=20_000_000, **kw):
        kind = kw.get("kind", "?")
        if kind == "llm":
            return "raw", None
        if kind == "parse":
            if state["rounds"] < tool_steps:
                state["rounds"] += 1
                payload = "tu1\x1fread_file\x1f" + json.dumps({"path": "x"})
                frames = f"u{len(payload.encode()):08d}{payload}"
            else:
                text = "the final answer"
                frames = f"t{len(text.encode()):08d}{text}"
            return frames, None
        return "file contents", None

    agent._forge = fake_forge
    agent._llm_src = "stub"
    agent._parse_src = "stub"
    return agent


def _sse_request(server, payload, token=None):
    """POST /chat with Accept: text/event-stream; returns the parsed events
    as (event, data-dict) pairs in arrival order."""
    url = f"http://127.0.0.1:{server.server_address[1]}/chat"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Accept": "text/event-stream"})
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    events, name, data = [], None, []
    with urllib.request.urlopen(req) as r:
        assert r.headers["Content-Type"].startswith("text/event-stream")
        for raw in r:
            line = raw.decode().rstrip("\n")
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data.append(line[len("data: "):])
            elif line == "" and name is not None:
                events.append((name, json.loads("\n".join(data))))
                name, data = None, []
    return events


# ── the stream ──────────────────────────────────────────────────────────


def test_a_streaming_turn_emits_steps_then_the_reply(tmp_path):
    server = serve(_finishing_agent(tmp_path, tool_steps=2), port=0)
    try:
        events = _sse_request(server, {"session": "s", "message": "hi"})
    finally:
        server.shutdown()
    names = [n for n, _ in events]
    assert names[-1] == "reply", f"stream must end with the reply, got {names}"
    steps = [d for n, d in events if n == "step"]
    assert len(steps) == 2, f"expected one event per tool round, got {steps}"
    assert steps[0]["step"] == 1 and steps[1]["step"] == 2
    assert all(d["tools"] == ["read_file"] for d in steps)
    final = events[-1][1]
    assert final["reply"] == "the final answer"
    assert "usage" in final


def test_without_the_accept_header_nothing_changes(tmp_path):
    """The JSON contract is the contract; the stream is opt-in."""
    server = serve(_finishing_agent(tmp_path), port=0)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/chat"
        req = urllib.request.Request(
            url, data=json.dumps({"session": "s", "message": "hi"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            assert r.headers["Content-Type"] == "application/json"
            assert json.loads(r.read())["reply"] == "the final answer"
    finally:
        server.shutdown()


def test_the_stream_is_behind_the_same_credential(tmp_path):
    server = serve(_finishing_agent(tmp_path), port=0, auth_token=TOKEN)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/chat"
        req = urllib.request.Request(
            url, data=json.dumps({"session": "s", "message": "hi"}).encode(),
            headers={"Content-Type": "application/json",
                     "Accept": "text/event-stream"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 401
        events = _sse_request(server, {"session": "s", "message": "hi"},
                              token=TOKEN)
        assert events[-1][0] == "reply"
    finally:
        server.shutdown()


def test_an_operational_failure_is_a_terminal_error_event(tmp_path):
    """Mid-stream there is no status code left to change — the 200 went out
    with the first byte — so failures are IN-BAND: a terminal `error` event,
    same vocabulary as the JSON path's 500 body."""
    agent, _ = _looping_agent(tmp_path)   # never converges -> max_steps error
    server = serve(agent, port=0)
    try:
        events = _sse_request(server, {"session": "s", "message": "hi"})
    finally:
        server.shutdown()
    assert events[-1][0] == "error"
    assert "no final answer" in events[-1][1]["error"]
    assert len([n for n, _ in events if n == "step"]) == agent.max_steps


def test_progress_is_introspected_like_abort():
    """Same seam, same rule: stub agents with two-arg lambdas keep working,
    and the JSON path must not grow a mandatory third parameter."""
    server = serve(SimpleNamespace(turn_with_usage=lambda s, m: ("ok", {})),
                   port=0)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/chat"
        req = urllib.request.Request(
            url, data=json.dumps({"session": "s", "message": "m"}).encode(),
            headers={"Content-Type": "application/json",
                     "Accept": "text/event-stream"})
        with urllib.request.urlopen(req) as r:
            # a stub without `progress` still answers; it just has no step
            # events to offer, so the stream is reply-only
            body = r.read().decode()
            assert "event: reply" in body
    finally:
        server.shutdown()


# ── the callback contract, agent-side ───────────────────────────────────


def test_progress_fires_per_completed_step_with_the_tools_dispatched(tmp_path):
    agent = _finishing_agent(tmp_path, tool_steps=3)
    seen = []
    reply, _ = agent.turn_with_usage("s", "hi", progress=seen.append)
    assert reply == "the final answer"
    assert [e["step"] for e in seen] == [1, 2, 3]
    assert all(e["tools"] == ["read_file"] for e in seen)


def test_a_final_step_with_no_tools_emits_no_event(tmp_path):
    """The reply is its own terminal signal; a `step` event for the step that
    produced it would double-count."""
    agent = _finishing_agent(tmp_path, tool_steps=0)
    seen = []
    agent.turn_with_usage("s", "hi", progress=seen.append)
    assert seen == []


def test_progress_exceptions_use_the_abandoned_exit(tmp_path):
    """A progress sink that fails IS a departed audience: the stream write
    raised, nobody is listening. It must take TurnAbandoned's exit — history
    persisted, no attempt to keep forging for a dead socket."""
    from agent import TurnAbandoned
    agent = _finishing_agent(tmp_path, tool_steps=2)

    def broken_sink(event):
        raise BrokenPipeError("client went away mid-stream")

    with pytest.raises(TurnAbandoned):
        agent.turn_with_usage("s", "hi", progress=broken_sink)
    assert agent.store.load("s"), "abandonment lost the session history"
