"""M3 integration: the dispatch loop — every step a separately-forged
program with its own minimal manifest."""
import json

import pytest

from conftest import API_KEY, PI_ROOT, SIGIL_ROOT


@pytest.fixture()
def scripted_llm():
    """A mock endpoint whose responses are FULLY scripted raw JSON bodies
    (unlike the M2 mock, tests control tool_use ids and block layout)."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace

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


@pytest.fixture()
def agent(scripted_llm, tmp_path, mcp):
    import sys
    sys.path.insert(0, str(PI_ROOT))
    from agent import PiAgent
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    a = PiAgent(scripted_llm.url, API_KEY, sandbox=sandbox, mcp=mcp,
                model="claude-mock")
    a._sandbox_path = sandbox
    return a


def msg(content):
    return {"id": "m", "type": "message", "role": "assistant",
            "model": "claude-mock", "content": content,
            "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}}


def tool_use(tu_id, name, tool_input):
    return {"type": "tool_use", "id": tu_id, "name": name, "input": tool_input}


def test_plain_text_turn(agent, scripted_llm):
    scripted_llm.script = [msg([{"type": "text", "text": "just chatting"}])]
    assert agent.turn("hi") == "just chatting"
    # tools were offered in the request
    assert [t["name"] for t in scripted_llm.requests[0]["tools"]] == \
        ["read_file", "write_file"]


def test_read_file_dispatch(agent, scripted_llm):
    f = agent._sandbox_path / "notes.txt"
    f.write_text("the secret plans")
    scripted_llm.script = [
        msg([{"type": "text", "text": "let me look"},
             tool_use("tu_9", "read_file", {"path": str(f)})]),
        msg([{"type": "text", "text": "done reading"}]),
    ]
    assert agent.turn("read my notes") == "done reading"

    # the tool result went back with the right id and the file contents
    result_msg = scripted_llm.requests[1]["messages"][-1]
    [result] = result_msg["content"]
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "tu_9"
    assert result["content"] == "the secret plans"
    assert "is_error" not in result

    # and the forge carried ONLY the fs grant, scoped to the sandbox
    [(name, grants)] = agent.grant_log
    assert name == "read_file"
    assert grants == {"fs": [str(agent._sandbox_path)]}


def test_write_file_dispatch(agent, scripted_llm):
    target = agent._sandbox_path / "out.txt"
    scripted_llm.script = [
        msg([tool_use("tu_1", "write_file",
                      {"path": str(target), "content": "hello disk"})]),
        msg([{"type": "text", "text": "written"}]),
    ]
    assert agent.turn("write it") == "written"
    assert target.read_text() == "hello disk"
    [(name, grants)] = agent.grant_log
    assert name == "write_file"
    assert grants == {"fs_write": [str(agent._sandbox_path)]}


def test_sandbox_escape_is_tool_error_not_crash(agent, scripted_llm):
    scripted_llm.script = [
        msg([tool_use("tu_2", "read_file", {"path": "/etc/hosts"})]),
        msg([{"type": "text", "text": "that failed, sorry"}]),
    ]
    assert agent.turn("read /etc/hosts") == "that failed, sorry"
    [result] = scripted_llm.requests[1]["messages"][-1]["content"]
    assert result["is_error"] is True
    assert "403" in result["content"]


def test_unknown_tool_is_error_result(agent, scripted_llm):
    scripted_llm.script = [
        msg([tool_use("tu_3", "launch_missiles", {"target": "moon"})]),
        msg([{"type": "text", "text": "understood"}]),
    ]
    assert agent.turn("do it") == "understood"
    [result] = scripted_llm.requests[1]["messages"][-1]["content"]
    assert result["is_error"] is True
    assert "unknown tool" in result["content"]


def test_runaway_tool_loop_hits_step_cap(agent, scripted_llm):
    scripted_llm.script = [
        msg([tool_use("tu_x", "read_file", {"path": "/nope"})]),  # repeats forever
    ]
    with pytest.raises(RuntimeError, match="no final answer"):
        agent.turn("loop forever")


def test_multi_block_reply_with_unicode(agent, scripted_llm):
    scripted_llm.script = [msg([
        {"type": "text", "text": "part one 😀"},
        {"type": "text", "text": 'part "two"\nwith lines'},
    ])]
    assert agent.turn("hi") == 'part one 😀\npart "two"\nwith lines'
