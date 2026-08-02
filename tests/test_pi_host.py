"""M7 — serve-native agentic loop: durable, HTTP-fronted, session-isolated.

The pi host runs the full tool-using loop (LLM -> tool_use -> forge tool ->
tool_result -> repeat) per request, keyed by session, with conversation
history persisted in kv so a fresh host instance resumes mid-conversation.
Every step is still a sandboxed forge with minimal grants.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from conftest import API_KEY, PI_ROOT

sys.path.insert(0, str(PI_ROOT))


# ── a fully-scripted mock Anthropic endpoint (controls tool_use vs text) ──


@pytest.fixture()
def scripted_llm():
    state = SimpleNamespace(script=[], requests=[], url=None)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            state.requests.append(json.loads(self.rfile.read(n)))
            doc = state.script[min(len(state.requests) - 1, len(state.script) - 1)]
            payload = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{srv.server_address[1]}/v1/messages"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield state
    srv.shutdown()
    srv.server_close()


def msg(content):
    return {"id": "m", "type": "message", "role": "assistant", "model": "claude-mock",
            "content": content, "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}}


def text(s):
    return {"type": "text", "text": s}


def tool_use(tu_id, name, tool_input):
    return {"type": "tool_use", "id": tu_id, "name": name, "input": tool_input}


def make_agent(scripted_llm, tmp_path, mcp, model="claude-mock"):
    """A pi host bound to a kv session dir + a sandbox root, both under tmp."""
    from agent import PiAgent, SessionStore
    kv = tmp_path / "sessions"
    sandbox_root = tmp_path / "sandboxes"
    kv.mkdir(); sandbox_root.mkdir()
    store = SessionStore(kv)
    agent = PiAgent(scripted_llm.url, API_KEY, store=store, sandbox_root=sandbox_root,
                    mcp=mcp, model=model)
    agent._kv_dir = kv
    agent._sandbox_root = sandbox_root
    return agent


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
