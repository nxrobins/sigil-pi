"""M3 integration: the dispatch loop — every step a separately-forged
program with its own minimal manifest."""
import pytest

from conftest import msg, tool_use  # `agent`/`scripted_llm` fixtures via conftest


def test_plain_text_turn(agent, scripted_llm):
    scripted_llm.script = [msg([{"type": "text", "text": "just chatting"}])]
    assert agent.turn("s1", "hi") == "just chatting"
    # every manifest tool is offered to the model
    offered = {t["name"] for t in scripted_llm.requests[0]["tools"]}
    assert offered == set(agent.manifest)
    assert {"read_file", "write_file", "fetch", "list_dir"} <= offered


def test_read_file_dispatch(agent, scripted_llm):
    f = agent._sandbox_path / "notes.txt"
    f.write_text("the secret plans")
    scripted_llm.script = [
        msg([{"type": "text", "text": "let me look"},
             tool_use("tu_9", "read_file", {"path": str(f)})]),
        msg([{"type": "text", "text": "done reading"}]),
    ]
    assert agent.turn("s1", "read my notes") == "done reading"

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
    assert agent.turn("s1", "write it") == "written"
    assert target.read_text() == "hello disk"
    [(name, grants)] = agent.grant_log
    assert name == "write_file"
    assert grants == {"fs_write": [str(agent._sandbox_path)]}


def test_sandbox_escape_is_tool_error_not_crash(agent, scripted_llm):
    scripted_llm.script = [
        msg([tool_use("tu_2", "read_file", {"path": "/etc/hosts"})]),
        msg([{"type": "text", "text": "that failed, sorry"}]),
    ]
    assert agent.turn("s1", "read /etc/hosts") == "that failed, sorry"
    [result] = scripted_llm.requests[1]["messages"][-1]["content"]
    assert result["is_error"] is True
    assert "403" in result["content"]


def test_unknown_tool_is_error_result(agent, scripted_llm):
    scripted_llm.script = [
        msg([tool_use("tu_3", "launch_missiles", {"target": "moon"})]),
        msg([{"type": "text", "text": "understood"}]),
    ]
    assert agent.turn("s1", "do it") == "understood"
    [result] = scripted_llm.requests[1]["messages"][-1]["content"]
    assert result["is_error"] is True
    assert "unknown tool" in result["content"]


def test_runaway_tool_loop_hits_step_cap(agent, scripted_llm):
    scripted_llm.script = [
        msg([tool_use("tu_x", "read_file", {"path": "/nope"})]),  # repeats forever
    ]
    with pytest.raises(RuntimeError, match="no final answer"):
        agent.turn("s1", "loop forever")


def test_multi_block_reply_with_unicode(agent, scripted_llm):
    scripted_llm.script = [msg([
        {"type": "text", "text": "part one 😀"},
        {"type": "text", "text": 'part "two"\nwith lines'},
    ])]
    assert agent.turn("s1", "hi") == 'part one 😀\npart "two"\nwith lines'
