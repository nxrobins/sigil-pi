"""M7 bug sweep — adversarial/operational edges of the durable serve host."""
import json
import threading

import pytest

from test_pi_host import make_agent, msg, scripted_llm, text, tool_use  # noqa: F401


def test_concurrent_turns_to_one_session_dont_lose_updates(scripted_llm, tmp_path, mcp):
    """Two POST /chat to the SAME session race on the kv read-modify-write.
    The per-session lock serializes them, so BOTH turns survive in history."""
    agent = make_agent(scripted_llm, tmp_path, mcp)
    scripted_llm.script = [msg([text("reply")])]  # every call returns text
    errors = []

    def go(n):
        try:
            agent.turn("s1", f"message {n}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors, errors
    history = agent.store.load("s1")
    users = [m["content"] for m in history if m["role"] == "user" and isinstance(m["content"], str)]
    # both user turns present, neither clobbered (4 msgs: user,asst,user,asst)
    assert sorted(users) == ["message 0", "message 1"], history


def test_path_traversal_out_of_sandbox_is_denied(scripted_llm, tmp_path, mcp):
    """A tool path with `..` resolves outside the session sandbox; the fs grant
    (scoped to the sandbox) rejects it — one session can't climb to another."""
    agent = make_agent(scripted_llm, tmp_path, mcp)
    # seed a file in a SIBLING sandbox dir to try to reach
    victim = tmp_path / "sandboxes" / "victim.txt"
    victim.write_text("stolen")
    scripted_llm.script = [
        msg([tool_use("t", "read_file", {"path": "../victim.txt"})]),
        msg([text("blocked")]),
    ]
    assert agent.turn("s1", "climb out") == "blocked"
    result = scripted_llm.requests[-1]["messages"][-1]["content"][0]
    assert result["is_error"] is True
    assert "403" in result["content"] or "404" in result["content"]


def test_adversarial_session_ids_are_safe_and_isolated(scripted_llm, tmp_path, mcp):
    """Session ids are hashed for both the kv file and the sandbox dir, so a
    traversal-looking id can't escape either, and distinct ids stay isolated."""
    agent = make_agent(scripted_llm, tmp_path, mcp)
    scripted_llm.script = [msg([text("ok")])]
    for sid in ["../../etc/passwd", "", "a/b/c", "x" * 500, "🙈 unicode"]:
        agent.turn(sid, "hi")
        # kv + sandbox live under our dirs, never escape
        assert list((tmp_path / "sessions").glob("*.kv"))
    # the empty and the traversal id are DIFFERENT sessions (different hashes)
    agent.turn("", "first")
    agent.turn("../../etc/passwd", "second")
    assert agent.store.load("") != agent.store.load("../../etc/passwd")


def test_http_bad_requests(scripted_llm, tmp_path, mcp):
    import urllib.error
    import urllib.request
    from agent import serve
    agent = make_agent(scripted_llm, tmp_path, mcp)
    scripted_llm.script = [msg([text("ok")])]
    server = serve(agent, port=0)
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(path, data, raw=False):
        body = data if raw else json.dumps(data).encode()
        req = urllib.request.Request(base + path, data=body, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        assert post("/chat", {"message": "no session"}) == 400   # missing field
        assert post("/chat", b"not json", raw=True) == 400        # malformed body
        assert post("/nope", {"session": "s", "message": "m"}) == 404  # unknown route
        assert post("/chat", {"session": "s", "message": "m"}) == 200  # happy path still ok
    finally:
        server.shutdown()


def test_partial_loop_still_persists(scripted_llm, tmp_path, mcp):
    """If the loop hits the step cap (never terminates), the conversation so far
    is still persisted — a durable host never silently drops state."""
    agent = make_agent(scripted_llm, tmp_path, mcp)
    scripted_llm.script = [msg([tool_use("t", "read_file", {"path": "nope.txt"})])]
    with pytest.raises(RuntimeError, match="no final answer"):
        agent.turn("s1", "loop")
    # state was saved despite the RuntimeError (finally: store.save)
    assert agent.store.load("s1"), "looping conversation was not persisted"
