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
