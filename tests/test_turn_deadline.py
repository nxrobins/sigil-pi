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


def test_deadline_uses_a_monotonic_clock(tmp_path, monkeypatch):
    clocks = {"monotonic": 100.0, "wall": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clocks["monotonic"])
    monkeypatch.setattr(time, "time", lambda: clocks["wall"])
    agent, calls = _looping_agent(tmp_path, turn_deadline_s=5)
    original = agent._forge

    def advancing_forge(*args, **kwargs):
        if kwargs.get("kind") == "llm":
            clocks["wall"] += 100000 if not calls else -200000
            clocks["monotonic"] += 3
        return original(*args, **kwargs)

    agent._forge = advancing_forge
    with pytest.raises(RuntimeError, match="deadline"):
        agent.turn("s", "hi")
    assert calls.count("llm") == 2
    assert agent.store.load("s")[0]["content"] == "hi"


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


def _grounded_agent(tmp_path, monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    calls, records = [], []
    receipt = {"status": "awaiting_review", "recorded": True, "fact_verified": False}
    memory = SimpleNamespace(fact_grounding=True, complete=lambda r: {}, judge=lambda r: {},
        recall=lambda *a: "", record=lambda *a: (records.append(a), receipt)[1])

    class Runtime:
        delay = 0.0

        def forge(self, source, **kwargs):
            calls.append((source, kwargs))
            clock["now"] += self.delay
            output = "raw" if source == "llm" else "t00000005draft"
            return {"status": "ok", "data": {"output_text": output}}

    runtime = Runtime()
    agent = PiAgent("http://localhost/v1/messages", "key", store=SessionStore(tmp_path / "sessions"),
                    sandbox_root=tmp_path / "sandboxes", memory=memory, mcp=runtime,
                    turn_deadline_s=5, provider_timeout_ms=30000)
    agent._llm_src, agent._parse_src = "llm", "parse"
    return agent, runtime, clock, calls, records, receipt


@pytest.mark.parametrize("external,budget", [(None, 5), (102.0, 2), (120.0, 5)])
def test_grounded_entry_uses_the_strictest_budget_for_all_providers(tmp_path, monkeypatch, external, budget):
    agent, runtime, clock, calls, records, receipt = _grounded_agent(tmp_path, monkeypatch)
    runtime.delay = 0.1

    def recall(*args):
        for kind in ("fact_support", "fact_revision"):
            agent._forge(kind, "input", {"net": ["localhost"]}, kind=kind, session="s")
        return ""

    agent.memory.recall = recall
    reply, _ = agent.turn_with_usage("s", "Hello", deadline_monotonic=external)
    assert reply == "draft"
    assert [source for source, _ in calls] == ["fact_support", "fact_revision", "llm", "parse"]
    for index, (_, kwargs) in enumerate(calls):
        assert kwargs["timeout_s"] == pytest.approx(budget - index * 0.1)
        if index < 3:
            assert 0 < kwargs["grants"]["http_timeout_ms"] <= (budget - index * 0.1 - 1) * 1000 + 1
        else:
            assert kwargs["grants"] is None
    assert agent.memory_delivery_receipt() == receipt
    assert not hasattr(agent._turn_context, "deadline")
    assert records[-1] == ("s", reply, "agent_action")


@pytest.mark.parametrize("stage", ["llm", "parse", "inspect", "recall"])
def test_grounded_text_reply_cannot_succeed_after_the_deadline(tmp_path, monkeypatch, stage):
    agent, runtime, clock, calls, records, receipt = _grounded_agent(tmp_path, monkeypatch)
    original = runtime.forge

    def forge(source, **kwargs):
        result = original(source, **kwargs)
        if source == stage:
            clock["now"] += 20
        return result

    runtime.forge = forge

    def inspect(*args):
        clock["now"] += 20
        return "Verified current answer"

    if stage == "inspect":
        agent.memory.current_residence_answer = inspect
    if stage == "recall":
        agent.memory.recall = lambda *args: agent._forge(
            "recall", "input", {"net": ["localhost"]}, kind="fact_support", session="s")
    with pytest.raises(TimeoutError, match="deadline"):
        agent.turn("s", "Where does Nadia live?")
    assert records == [("s", "Where does Nadia live?", "user_message")]
    assert agent.memory_delivery_receipt() == receipt
    assert agent.store.load("s") == [{"role": "user", "content": "Where does Nadia live?"}]
    assert not hasattr(agent._turn_context, "deadline")
    if stage != "inspect":
        assert agent.audit.read("s")[-1]["error"] == "turn_deadline_exceeded"


@pytest.mark.parametrize("stream", [False, True])
def test_grounded_http_deadline_preserves_the_correction_receipt(tmp_path, monkeypatch, stream):
    from test_sse_progress import _sse_request
    agent, runtime, clock, calls, records, receipt = _grounded_agent(tmp_path, monkeypatch)
    runtime.delay = 20
    server = serve(agent, port=0)
    payload = {"session": "s", "message": "Correction: Nadia no longer lives in Oslo."}
    try:
        if stream:
            events = _sse_request(server, payload)
            assert events[-1][0] == "error"
            result = events[-1][1]
        else:
            req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/chat",
                data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(req)
            assert error.value.code == 500
            result = json.load(error.value)
        assert "deadline" in result["error"]
        assert result["memory_receipt"] == receipt
        assert records == [("s", payload["message"], "user_message")]
        assert agent.store.load("s") == [{"role": "user", "content": payload["message"]}]
        assert calls[0][1]["timeout_s"] == 5
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("failure", [False, True])
def test_nested_grounded_turn_restores_the_callers_budget_and_receipt(tmp_path, monkeypatch, failure):
    agent, runtime, clock, calls, records, receipt = _grounded_agent(tmp_path, monkeypatch)
    original = agent._llm
    seen = []

    def llm(payload, session=None):
        if session == "outer":
            context = vars(agent._turn_context).copy()
            clock["now"] += 0.5
            if failure:
                with pytest.raises(RuntimeError, match="nested failure"):
                    agent.turn_with_usage("inner", "nested", deadline_monotonic=120)
            else:
                agent.turn_with_usage("inner", "nested", deadline_monotonic=120)
            assert vars(agent._turn_context) == context
        elif session == "inner":
            seen.append(agent._turn_context.deadline)
            if failure:
                raise RuntimeError("nested failure")
        return original(payload, session=session)

    agent._llm = llm
    assert agent.turn("outer", "hello") == "draft"
    assert seen == [105]
    assert agent.turn_telemetry()["input_tokens"] == 0
    assert agent.memory_delivery_receipt() == receipt
    assert not hasattr(agent._turn_context, "deadline")
    assert agent.turn("after", "hello") == "draft"


def test_grounded_concurrent_callers_keep_separate_budgets(tmp_path, monkeypatch):
    import concurrent.futures
    import threading
    agent, runtime, clock, calls, records, receipt = _grounded_agent(tmp_path, monkeypatch)
    barrier = threading.Barrier(2)
    original = agent._llm
    seen = {}

    def llm(payload, session=None):
        barrier.wait(timeout=5)
        seen[session] = agent._turn_context.deadline
        return original(payload, session=session)

    agent._llm = llm

    def turn(session, deadline):
        reply = agent.turn_with_usage(session, "hello", deadline_monotonic=deadline)[0]
        assert not hasattr(agent._turn_context, "deadline")
        return reply

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(turn, "a", 102), pool.submit(turn, "b", 120)]
        assert [future.result(timeout=5) for future in futures] == ["draft", "draft"]
    assert seen == {"a": 102, "b": 105}


def test_grounded_session_wait_uses_budget_and_cleans_up_on_failure(tmp_path, monkeypatch):
    agent, runtime, clock, calls, records, receipt = _grounded_agent(tmp_path, monkeypatch)
    waits = []

    class Lock:
        def acquire(self, timeout=None):
            waits.append(timeout)
            clock["now"] += timeout
            return False

    monkeypatch.setattr(agent, "_session_lock", lambda session: Lock())
    with pytest.raises(TimeoutError, match="waiting for session"):
        agent.turn("s", "hello")
    assert waits == [5]
    assert calls == records == []
    assert not hasattr(agent._turn_context, "deadline")
    assert agent.memory_delivery_receipt() is None


def _retrying_provider_turn(agent, caller):
    from memory_judgment import FactJudgment
    from test_memory_grounding import request
    results = []
    if caller != "chat":
        if caller == "extraction":
            invoke = lambda: agent._memory_complete({"user": "extract facts"})
        else:
            req = request()
            if caller == "fact_revision":
                req["state"]["task"] = "review_stored_fact"
            judge = FactJudgment(agent, "synthetic-jev-key", endpoint="http://localhost/jev")
            invoke = lambda: judge(req)

        def recall(*args):
            results.append(invoke())
            return ""

        agent.memory.recall = recall
    reply = agent.turn("s", "Correction: Nadia no longer lives in Oslo.")
    return reply, results


@pytest.mark.parametrize("caller", ["chat", "extraction", "fact_support", "fact_revision"])
@pytest.mark.parametrize("phase", ["compile", "second_failure", "success", "unbounded"])
def test_provider_retry_waits_consume_only_the_original_deadline(tmp_path, monkeypatch, caller, phase):
    from test_memory_grounding import response
    agent, runtime, clock, calls, records, receipt = _grounded_agent(tmp_path, monkeypatch)
    sleeps = []
    wall = {"now": 1000.0}
    monkeypatch.setattr(time, "time", lambda: wall["now"])
    agent._llm_src = None
    if phase == "unbounded":
        agent.turn_deadline_s = None

    def compose(*args):
        if phase == "compile":
            clock["now"] += 3.5
        return "llm"

    def sleep(delay):
        sleeps.append(delay)
        clock["now"] += delay

    agent._compose = compose
    agent._sleep = sleep
    monkeypatch.setattr(time, "sleep", sleep)
    failures = [104.875] if phase == "compile" else (
        [102.0, 104.875] if phase == "second_failure" else [100.5, 101.5])

    def forge(source, **kwargs):
        calls.append((source, kwargs))
        if len(calls) <= len(failures):
            clock["now"] = failures[len(calls) - 1]
            wall["now"] += 100000 if len(calls) == 1 else -200000
            return {"status": "error", "diagnostics": [{
                "code": "R803", "message": "tool returned error (429)"}]}
        output = (json.dumps(response()) if caller.startswith("fact_")
                  else "raw") if source == "llm" else "t00000005draft"
        return {"status": "ok", "data": {"output_text": output}}

    runtime.forge = forge
    if phase in ("compile", "second_failure"):
        with pytest.raises(TimeoutError, match="deadline"):
            _retrying_provider_turn(agent, caller)
        assert clock["now"] == 105.0
        assert len(calls) == len(failures)
        assert sleeps == ([0.125] if phase == "compile" else
                          [0.25 if caller.startswith("fact_") else 0.5, 0.125])
        assert records == [("s", "Correction: Nadia no longer lives in Oslo.", "user_message")]
        assert agent.store.load("s") == [{"role": "user", "content": records[0][1]}]
    else:
        reply, results = _retrying_provider_turn(agent, caller)
        assert "saved as evidence" in reply
        assert sleeps == ([0.25, 0.5] if caller.startswith("fact_") else [0.5, 2.0])
        if caller.startswith("fact_"):
            assert results[0]["answers"]["q"]["confidence"] == 0.79
        elif caller == "extraction":
            assert results == [{"text": "draft"}]
        assert records[-1] == ("s", reply, "agent_action")
    assert agent.memory_delivery_receipt() == receipt
    assert not hasattr(agent._turn_context, "deadline")
    assert all(("timeout_s" not in kwargs) if phase == "unbounded" else kwargs["timeout_s"] <= 5
               for _, kwargs in calls)


@pytest.mark.parametrize("caller", ["chat", "extraction", "fact_support", "fact_revision"])
def test_capability_negotiation_leaves_only_its_remainder_for_retry(tmp_path, monkeypatch, caller):
    from collections import deque
    from runtime_client import ProductionSigilMCP
    from test_runtime_client import _Proc
    agent, _, clock, _, records, receipt = _grounded_agent(tmp_path, monkeypatch)
    agent._compose = lambda *args: "llm"
    schema = {"tools": [{"name": "sigil_forge", "inputSchema": {
        "properties": {"grants": {"properties": {"http_timeout_ms": {"type": "integer"}}}}}}]}
    error = {"content": [{"text": json.dumps({"status": "error", "diagnostics": [
        {"code": "R803", "message": "tool returned error (502)"}]})}]}
    proc = _Proc([json.dumps({"jsonrpc": "2.0", "id": i + 1, "result": result}) + "\n"
                  for i, result in enumerate([schema, error])])
    agent._mcp = ProductionSigilMCP(proc, deque(), None)
    waits, sleeps = [], []

    def select(readable, writable, errors, timeout):
        waits.append(timeout)
        if len(waits) == 1:
            clock["now"] += 4.875
        return [proc.stdout], [], []

    def sleep(delay):
        sleeps.append(delay)
        clock["now"] += delay

    monkeypatch.setattr("runtime_client.select.select", select)
    monkeypatch.setattr(time, "sleep", sleep)
    agent._sleep = sleep
    with pytest.raises(TimeoutError, match="deadline"):
        _retrying_provider_turn(agent, caller)
    assert waits == [5, 0.125]
    assert sleeps == [0.125]
    assert clock["now"] == 105
    assert [json.loads(line)["method"] for line in proc.stdin.writes] == ["tools/list", "tools/call"]
    assert records == [("s", "Correction: Nadia no longer lives in Oslo.", "user_message")]
    assert agent.memory_delivery_receipt() == receipt
    assert agent.store.load("s")[0]["content"] == records[0][1]
    assert not hasattr(agent._turn_context, "deadline")
