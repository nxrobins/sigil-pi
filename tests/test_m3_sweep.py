"""M3 bug sweep — host hardening around hostile/degenerate model output."""
from conftest import msg, tool_use  # `agent`/`scripted_llm` fixtures via conftest


def test_missing_file_is_error_result(agent, scripted_llm):
    scripted_llm.script = [
        msg([tool_use("tu_1", "read_file",
                      {"path": str(agent._sandbox_path / "nope.txt")})]),
        msg([{"type": "text", "text": "gone"}]),
    ]
    assert agent.turn("s1", "read it") == "gone"
    [result] = scripted_llm.requests[1]["messages"][-1]["content"]
    assert result["is_error"] is True
    assert "404" in result["content"]


def test_non_object_tool_input_is_error_not_crash(agent, scripted_llm):
    """A model emitting {"input": "just a string"} must not crash the host."""
    scripted_llm.script = [
        msg([{"type": "tool_use", "id": "tu_2", "name": "read_file",
              "input": "not-an-object"}]),
        msg([{"type": "text", "text": "recovered"}]),
    ]
    assert agent.turn("s1", "go") == "recovered"
    [result] = scripted_llm.requests[1]["messages"][-1]["content"]
    assert result["is_error"] is True


def test_pipe_in_path_argument_is_rejected(agent, scripted_llm):
    """write_file joins args on '|'; a pipe inside the PATH would shift the
    split and write to the wrong file. The host rejects it."""
    scripted_llm.script = [
        msg([tool_use("tu_3", "write_file",
                      {"path": f"{agent._sandbox_path}/a|b.txt", "content": "x"})]),
        msg([{"type": "text", "text": "refused"}]),
    ]
    assert agent.turn("s1", "go") == "refused"
    [result] = scripted_llm.requests[1]["messages"][-1]["content"]
    assert result["is_error"] is True
    assert "'|'" in result["content"]
    # nothing was written anywhere
    assert list(agent._sandbox_path.iterdir()) == []


def test_pipe_in_last_argument_is_fine(agent, scripted_llm):
    """content is the LAST arg — pipes there are legal by construction."""
    target = agent._sandbox_path / "ok.txt"
    scripted_llm.script = [
        msg([tool_use("tu_4", "write_file",
                      {"path": str(target), "content": "a|b|c"})]),
        msg([{"type": "text", "text": "done"}]),
    ]
    assert agent.turn("s1", "go") == "done"
    assert target.read_text() == "a|b|c"


def test_string_content_block_shorthand(agent, scripted_llm):
    """content: "string" shorthand (some providers) → -400 from the parser,
    surfaced as a host error, not a crash."""
    import pytest
    scripted_llm.script = [
        {"id": "m", "type": "message", "role": "assistant",
         "model": "claude-mock", "content": "bare string",
         "stop_reason": "end_turn", "usage": {}},
    ]
    with pytest.raises(RuntimeError, match="parse forge failed"):
        agent.turn("s1", "hi")
