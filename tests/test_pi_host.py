"""M7 — serve-native agentic loop: durable, HTTP-fronted, session-isolated.

The pi host runs the full tool-using loop (LLM -> tool_use -> forge tool ->
tool_result -> repeat) per request, keyed by session, with conversation
history persisted in kv so a fresh host instance resumes mid-conversation.
Every step is still a sandboxed forge with minimal grants.
"""
import json

import pytest

from conftest import API_KEY, make_agent, msg, text, tool_use  # fixtures come via conftest


# ── durable, session-keyed multi-turn ────────────────────────────────────


def test_history_persists_across_turns_in_kv(scripted_llm, tmp_path, mcp):
    agent = make_agent(scripted_llm, tmp_path, mcp)
    scripted_llm.script = [msg([text("first reply")]), msg([text("second reply")])]
    assert agent.turn("s1", "hello") == "first reply"
    assert agent.turn("s1", "again") == "second reply"
    # the second LLM request carried the full prior history
    second = scripted_llm.requests[1]["messages"]
    assert second[0] == {"role": "user", "content": "hello"}
    assert second[-1] == {"role": "user", "content": "again"}
    assert len(second) == 3  # user, assistant, user
    # and it's durable on disk
    assert list((tmp_path / "sessions").glob("*.kv")), "no session persisted to kv"


def test_fresh_host_resumes_session_from_kv(scripted_llm, tmp_path, mcp):
    """Durability/restart: a brand-new PiAgent over the same kv dir continues
    the conversation — nothing lives in process memory."""
    a1 = make_agent(scripted_llm, tmp_path, mcp)
    scripted_llm.script = [msg([text("r1")]), msg([text("r2")])]
    a1.turn("s1", "turn one")

    # a completely fresh host instance, same kv dir + sandbox root
    from agent import PiAgent, SessionStore
    a2 = PiAgent(scripted_llm.url, API_KEY, store=SessionStore(tmp_path / "sessions"),
                 sandbox_root=tmp_path / "sandboxes", mcp=mcp, model="claude-mock")
    assert a2.turn("s1", "turn two") == "r2"
    resumed = scripted_llm.requests[1]["messages"]
    assert resumed[0] == {"role": "user", "content": "turn one"}
    assert len(resumed) == 3


def test_sessions_are_isolated(scripted_llm, tmp_path, mcp):
    agent = make_agent(scripted_llm, tmp_path, mcp)
    scripted_llm.script = [msg([text("ok")])]
    agent.turn("alpha", "hi from alpha")
    agent.turn("beta", "hi from beta")
    assert scripted_llm.requests[1]["messages"] == [{"role": "user", "content": "hi from beta"}]


# ── the tool-using loop, per session ─────────────────────────────────────


def test_tool_use_within_the_loop(scripted_llm, tmp_path, mcp):
    """Tool paths are RELATIVE to the session sandbox; the host resolves them.
    The LLM never sees absolute host paths."""
    agent = make_agent(scripted_llm, tmp_path, mcp)
    scripted_llm.script = [
        msg([tool_use("tu1", "write_file", {"path": "note.txt", "content": "remember"})]),
        msg([tool_use("tu2", "read_file", {"path": "note.txt"})]),
        msg([text("done")]),
    ]
    assert agent.turn("s1", "save and read a note") == "done"
    # three LLM calls (initial + after each tool_result)
    assert len(scripted_llm.requests) == 3
    # the write happened in THIS session's sandbox and the read saw it
    tr = scripted_llm.requests[2]["messages"][-1]["content"]
    assert tr[0]["type"] == "tool_result"
    assert tr[0]["content"] == "remember"


def test_dispatched_tool_sandbox_is_per_session(scripted_llm, tmp_path, mcp):
    """The same relative path resolves to a DIFFERENT sandbox per session, so a
    file written in A is not readable from B (fs grant scoped to B's sandbox)."""
    agent = make_agent(scripted_llm, tmp_path, mcp)
    scripted_llm.script = [
        msg([tool_use("t", "write_file", {"path": "secret.txt", "content": "A-only"})]),
        msg([text("written")]),
        msg([tool_use("t", "read_file", {"path": "secret.txt"})]),
        msg([text("could not read")]),
    ]
    assert agent.turn("A", "write my file") == "written"
    assert agent.turn("B", "read the same path") == "could not read"
    last_tr = scripted_llm.requests[-1]["messages"][-1]["content"][0]
    assert last_tr.get("is_error") is True  # -404 in B's empty sandbox


def test_step_cap_is_configurable(scripted_llm, tmp_path, mcp):
    """max_steps is a constructor knob (PI_MAX_STEPS in main), like every
    other operational bound — a deployment tunes LLM round-trips per turn
    without editing source."""
    from agent import PiAgent, SessionStore
    (tmp_path / "sessions").mkdir(); (tmp_path / "sandboxes").mkdir()
    agent = PiAgent(scripted_llm.url, API_KEY,
                    store=SessionStore(tmp_path / "sessions"),
                    sandbox_root=tmp_path / "sandboxes", mcp=mcp,
                    model="claude-mock", max_steps=2)
    scripted_llm.script = [msg([tool_use("t", "read_file", {"path": "nope.txt"})])]
    with pytest.raises(RuntimeError, match="after 2 steps"):
        agent.turn("s1", "loop")
    assert len(scripted_llm.requests) == 2  # exactly max_steps LLM calls


def test_step_cap_holds(scripted_llm, tmp_path, mcp):
    agent = make_agent(scripted_llm, tmp_path, mcp)
    scripted_llm.script = [msg([tool_use("t", "read_file", {"path": "/nope"})])]  # loops forever
    with pytest.raises(RuntimeError, match="no final answer"):
        agent.turn("s1", "loop")


# ── operator config parsing ──────────────────────────────────────────────


def test_endpoint_host_parse_is_strict_and_clear(tmp_path):
    """self._host becomes the `net` grant for every LLM-call forge. A
    malformed endpoint used to die with a bare IndexError three layers in —
    or worse, `http://` parsed to an EMPTY host and construction succeeded
    with a grant that can never match. Parse errors must be immediate, named,
    and clear."""
    from agent import PiAgent, SessionStore
    store = SessionStore(tmp_path / "s")

    def make(ep):
        return PiAgent(ep, "key", store=store, sandbox_root=tmp_path / "b", mcp=None)

    assert make("https://api.anthropic.com/v1/messages")._host == "api.anthropic.com"
    assert make("http://127.0.0.1:8973/v1/messages")._host == "127.0.0.1"
    for bad in ("api.anthropic.com/v1/messages",   # no scheme
                "", "http://", "http:///path",     # no host at all
                "ftp://host/x"):                   # not an http(s) endpoint
        with pytest.raises(ValueError, match="endpoint"):
            make(bad)


def test_env_int_knobs_fail_with_a_named_error(monkeypatch):
    """PI_MAX_*/PI_PORT are operator knobs; a typo used to be a bare
    `ValueError: invalid literal for int()` traceback that names no knob."""
    from agent import _env_int
    monkeypatch.delenv("PI_TEST_KNOB", raising=False)
    assert _env_int("PI_TEST_KNOB", 42) == 42
    monkeypatch.setenv("PI_TEST_KNOB", "17")
    assert _env_int("PI_TEST_KNOB", 42) == 17
    monkeypatch.setenv("PI_TEST_KNOB", "seventeen")
    with pytest.raises(SystemExit, match="PI_TEST_KNOB"):
        _env_int("PI_TEST_KNOB", 42)


def test_net_allowlist_parsing_survives_operator_whitespace():
    """PI_NET_ALLOWLIST is fail-closed, so a host that silently fails to
    match is indistinguishable from a deny: 'a, b' used to produce the
    grant ' b', which can never equal a URL host. Whitespace is operator
    noise, not part of a hostname."""
    from agent import _parse_allowlist
    assert _parse_allowlist("a, b") == ["a", "b"]
    assert _parse_allowlist(" api.anthropic.com ,,") == ["api.anthropic.com"]
    assert _parse_allowlist("") == []
    assert _parse_allowlist(" , ") == []


# ── llm retry (host-side, bounded, transient-only) ───────────────────────


def _retry_probe(tmp_path, responses, retries=2):
    """A PiAgent whose _forge is scripted and whose sleeps are recorded.
    `responses` are (out, err) pairs consumed per attempt (last one repeats)."""
    from agent import PiAgent, SessionStore
    agent = PiAgent("http://127.0.0.1:9/v1/messages", "key",
                    store=SessionStore(tmp_path / "s"),
                    sandbox_root=tmp_path / "b", mcp=None, llm_retries=retries)
    calls, sleeps = [], []

    # **kw absorbs the audit metadata (kind/session) the real _forge takes
    def fake_forge(source, input_text, grants, fuel=20_000_000, **kw):
        calls.append(grants)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    agent._forge = fake_forge
    agent._sleep = sleeps.append
    return agent, calls, sleeps


ERR_429 = (None, "R803: tool trapped: tool returned error (429)")
ERR_502 = (None, "R803: tool trapped: tool returned error (502)")
ERR_403 = (None, "R803: tool trapped: tool returned error (403)")
OK_RAW = ("raw-response", None)


def test_llm_retries_transient_failures_then_succeeds(tmp_path):
    """429 (rate limit) and 5xx (overload/network — the shim maps a dead host
    to 502) are transient: the call must survive them within the budget, with
    growing backoff between attempts."""
    agent, calls, sleeps = _retry_probe(tmp_path, [ERR_429, ERR_502, OK_RAW])
    assert agent._llm({"messages": []}) == "raw-response"
    assert len(calls) == 3
    assert sleeps == [0.5, 2.0]


def test_llm_never_retries_permanent_failures(tmp_path):
    """403 is a grant denial — retrying it would re-send a request the
    runtime already refused, and would blur the fail-closed story."""
    agent, calls, sleeps = _retry_probe(tmp_path, [ERR_403, OK_RAW])
    with pytest.raises(RuntimeError, match="403"):
        agent._llm({"messages": []})
    assert len(calls) == 1 and sleeps == []


def test_llm_retry_budget_exhausts_loudly(tmp_path):
    agent, calls, sleeps = _retry_probe(tmp_path, [ERR_429], retries=1)
    with pytest.raises(RuntimeError, match="429"):
        agent._llm({"messages": []})
    assert len(calls) == 2 and sleeps == [0.5]


def test_llm_retries_zero_means_single_attempt(tmp_path):
    agent, calls, sleeps = _retry_probe(tmp_path, [ERR_429, OK_RAW], retries=0)
    with pytest.raises(RuntimeError, match="429"):
        agent._llm({"messages": []})
    assert len(calls) == 1 and sleeps == []


# ── usage capture (through the ring bridge, accumulated per turn) ─────────


def test_decode_frames_reads_usage_and_tolerates_garbage():
    from agent import decode_frames
    frames = b"t" + b"00000002" + b"hi" + b"g" + b"00000004" + b"12|7"
    assert decode_frames(frames) == [("text", "hi"), ("usage", 12, 7)]
    # a malformed usage payload must degrade, never crash the turn
    bad = b"g" + b"00000005" + b"12|xy"
    assert decode_frames(bad) == [("other", "12|xy")]


def test_usage_accumulates_across_steps_and_turns(scripted_llm, tmp_path, mcp):
    """Every scripted mock response carries usage {1,1}; a two-step turn must
    therefore report {2,2}, and the process-lifetime total keeps counting
    across turns and sessions."""
    agent = make_agent(scripted_llm, tmp_path, mcp)
    scripted_llm.script = [
        msg([tool_use("t", "read_file", {"path": "nope.txt"})]),
        msg([text("done")]),
    ]
    reply, usage = agent.turn_with_usage("s1", "go")
    assert reply == "done"
    assert usage == {"input_tokens": 2, "output_tokens": 2}
    assert agent.last_usage == usage
    scripted_llm.script = [msg([text("ok")])]
    agent.turn("s2", "hi")
    assert agent.usage_total == {"input_tokens": 3, "output_tokens": 3}


def test_http_chat_response_carries_usage(scripted_llm, tmp_path, mcp):
    """Operators read usage off the wire: {reply, usage} — additive, so
    existing clients that only read `reply` are untouched."""
    import urllib.request
    from agent import serve
    agent = make_agent(scripted_llm, tmp_path, mcp)
    scripted_llm.script = [msg([text("hello")])]
    server = serve(agent, port=0)
    try:
        addr = f"http://127.0.0.1:{server.server_address[1]}/chat"
        req = urllib.request.Request(
            addr, data=json.dumps({"session": "s", "message": "m"}).encode(),
            method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
    finally:
        server.shutdown()
    assert body["reply"] == "hello"
    assert body["usage"] == {"input_tokens": 1, "output_tokens": 1}


# ── system prompt + project context ──────────────────────────────────────


def test_system_prompt_reaches_every_request(scripted_llm, tmp_path, mcp):
    """The system prompt is deployment identity: once configured it must ride
    EVERY step's payload, not just the first — the model re-reads it each
    round-trip of a tool-using turn."""
    from agent import PiAgent, SessionStore
    (tmp_path / "sessions").mkdir(); (tmp_path / "sandboxes").mkdir()
    agent = PiAgent(scripted_llm.url, API_KEY,
                    store=SessionStore(tmp_path / "sessions"),
                    sandbox_root=tmp_path / "sandboxes", mcp=mcp,
                    model="claude-mock", system_prompt="You are pi. Be terse.")
    scripted_llm.script = [
        msg([tool_use("t", "read_file", {"path": "nope.txt"})]),
        msg([text("done")]),
    ]
    assert agent.turn("s1", "hi") == "done"
    assert len(scripted_llm.requests) == 2
    for req in scripted_llm.requests:
        assert req["system"] == "You are pi. Be terse."


def test_without_system_prompt_the_field_is_absent(scripted_llm, tmp_path, mcp):
    """Back-compat pin: unconfigured deployments keep sending exactly the
    payload they always sent — no empty `system` field."""
    agent = make_agent(scripted_llm, tmp_path, mcp)
    scripted_llm.script = [msg([text("ok")])]
    agent.turn("s1", "hi")
    assert "system" not in scripted_llm.requests[0]


def test_load_system_prompt_assembles_inline_then_file(tmp_path):
    """PI_SYSTEM (deployment identity) comes first, the AGENTS.md-convention
    file (project instructions) second — pi's own layering. Absent, empty,
    and whitespace-only sources contribute nothing."""
    from agent import load_system_prompt
    f = tmp_path / "AGENTS.md"
    assert load_system_prompt(None, f) is None
    assert load_system_prompt("", f) is None
    assert load_system_prompt("   ", f) is None
    assert load_system_prompt("inline identity", f) == "inline identity"
    f.write_text("# project\nrules")
    assert load_system_prompt(None, f) == "# project\nrules"
    assert load_system_prompt("inline identity", f) == "inline identity\n\n# project\nrules"
    f.write_text("  \n")
    assert load_system_prompt(None, f) is None


def test_oversized_system_prompt_fails_loud_at_construction(tmp_path):
    """M8 ethos: everything that enters the payload is bounded. But CLIPPING
    instructions would silently change their meaning, so an over-cap system
    prompt is a named construction error, not a truncation."""
    from agent import MAX_SYSTEM_BYTES, PiAgent, SessionStore
    with pytest.raises(ValueError, match="MAX_SYSTEM_BYTES"):
        PiAgent("http://127.0.0.1:9/v1/messages", "k",
                store=SessionStore(tmp_path / "s"),
                sandbox_root=tmp_path / "b", mcp=None,
                system_prompt="x" * (MAX_SYSTEM_BYTES + 1))


# ── HTTP front ───────────────────────────────────────────────────────────


def test_http_chat_endpoint(scripted_llm, tmp_path, mcp):
    """POST /chat {session, message} -> {reply} over real HTTP."""
    import urllib.request
    from agent import PiAgent, SessionStore, serve

    (tmp_path / "sessions").mkdir(); (tmp_path / "sandboxes").mkdir()
    agent = PiAgent(scripted_llm.url, API_KEY, store=SessionStore(tmp_path / "sessions"),
                    sandbox_root=tmp_path / "sandboxes", mcp=mcp, model="claude-mock")
    scripted_llm.script = [msg([text("hello over http")])]
    server = serve(agent, host="127.0.0.1", port=0)
    try:
        addr = f"http://127.0.0.1:{server.server_address[1]}/chat"
        req = urllib.request.Request(addr, data=json.dumps({"session": "s1", "message": "hi"}).encode(),
                                     headers={"content-type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
        assert body["reply"] == "hello over http"
    finally:
        server.shutdown()


def test_concurrent_sessions_forge_correctly_under_the_lock(scripted_llm,
                                                            tmp_path, mcp):
    """Two sessions turning at once — the _forge serialization must keep
    every response attributed to its own request (SigilMCP itself matches
    replies by nothing but arrival order)."""
    import threading as th
    from conftest import make_agent
    agent = make_agent(scripted_llm, tmp_path, mcp)
    scripted_llm.script = [msg([text("same answer for everyone")])]
    replies, errors = {}, []

    def one_turn(name):
        try:
            replies[name] = agent.turn(name, f"hello from {name}")
        except Exception as e:  # noqa: BLE001 — collected for the assertion
            errors.append(e)

    threads = [th.Thread(target=one_turn, args=(f"s{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"concurrent turns must not corrupt the mcp: {errors}"
    assert all(r == "same answer for everyone" for r in replies.values())
    assert len(replies) == 6
