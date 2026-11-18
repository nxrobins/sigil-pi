"""`write_file` refuses every path that leaves the session's sandbox.

`write_file` is the only tool whose sole grant is write (`fs_write`, the
session sandbox), and until these tests no test gave it an escaping path
(audit 2026-11-10, F-17). The audit found the behaviour correct, by hand: a
`..`, another session's sandbox, an absolute path and three planted symlinks
were all denied with 403. These tests pin each of those on the real compiler,
through the whole dispatch path a model's tool call takes: the host resolves
the path against the sandbox, grants `fs_write` on the sandbox alone, and the
runtime refuses the write. Each case checks the target afterwards, so a write
that lands while the tool still reports an error fails here too, and a
control case writes inside the sandbox first, so a denial cannot come from a
broken setup.
"""

import pytest

from conftest import make_agent, msg, text, tool_use

VICTIM = "committed bytes that no other session may change"


def _last_tool_result(scripted_llm):
    return scripted_llm.requests[-1]["messages"][-1]["content"][0]


def _write(agent, scripted_llm, session, path, content):
    """One model turn that calls write_file once; returns the tool result the
    model was sent and the grants the tool was forged with."""
    scripted_llm.requests.clear()
    agent.grant_log.clear()
    scripted_llm.script = [
        msg([tool_use("w", "write_file", {"path": path, "content": content})]),
        msg([text("done")]),
    ]
    assert agent.turn(session, "write it") == "done"
    [(name, grants)] = agent.grant_log
    assert name == "write_file"
    return _last_tool_result(scripted_llm), grants


@pytest.fixture()
def host(scripted_llm, tmp_path, mcp):
    """A real agent with two sessions' sandboxes and a directory outside
    every sandbox, each holding a file a write must not reach."""
    agent = make_agent(scripted_llm, tmp_path, mcp)
    sandbox = agent.sandbox_for("writer")
    sibling = agent.sandbox_for("another-tenant")
    outside = tmp_path / "outside"
    outside.mkdir()
    targets = {
        "root": tmp_path / "sandboxes" / "victim.txt",
        "sibling": sibling / "note.txt",
        "outside": outside / "victim.txt",
    }
    for target in targets.values():
        target.write_text(VICTIM)
    return agent, sandbox, sibling, outside, targets


# name -> (how the path escapes, the file it would create or change)
ESCAPES = {
    "dotdot_into_the_sandbox_root": lambda h: ("../victim.txt", h["targets"]["root"]),
    "dotdot_into_another_session": lambda h: (
        f"../{h['sibling'].name}/note.txt", h["targets"]["sibling"]),
    "absolute_path_outside": lambda h: (
        str(h["targets"]["outside"]), h["targets"]["outside"]),
    "absolute_path_new_file": lambda h: (
        str(h["outside"] / "created.txt"), h["outside"] / "created.txt"),
    "symlink_to_another_sessions_file": lambda h: (
        h["link"]("to-sibling.txt", h["targets"]["sibling"]), h["targets"]["sibling"]),
    "symlink_to_a_directory_outside": lambda h: (
        h["link"]("out-dir", h["outside"]) + "/created.txt",
        h["outside"] / "created.txt"),
    "symlink_to_a_host_file": lambda h: (
        h["link"]("to-host.txt", h["targets"]["outside"]), h["targets"]["outside"]),
}


@pytest.mark.parametrize("escape", sorted(ESCAPES))
def test_write_file_refuses_a_path_outside_its_sandbox(host, scripted_llm, escape):
    agent, sandbox, sibling, outside, targets = host

    def link(name, points_to):
        (sandbox / name).symlink_to(points_to)
        return name

    path, reached = ESCAPES[escape]({
        "targets": targets, "sibling": sibling, "outside": outside, "link": link})
    before = reached.read_bytes() if reached.exists() else None

    # Control: the same session writes inside its own sandbox, so the setup
    # can write and a refusal below is about the path alone.
    result, grants = _write(agent, scripted_llm, "writer", "inside.txt", "fine")
    assert "is_error" not in result and result["content"] == "ok", result
    assert (sandbox / "inside.txt").read_text() == "fine"
    assert grants == {"fs_write": [str(sandbox)]}

    result, grants = _write(agent, scripted_llm, "writer", path, "OVERWRITTEN")

    assert grants == {"fs_write": [str(sandbox)]}, (
        "write_file must be forged with a grant on its own sandbox only")
    assert result.get("is_error") is True, (
        f"write_file accepted the escaping path {path!r}: {result!r}")
    assert "403" in result["content"], (
        f"write_file refused {path!r} for another reason than its grant: {result!r}")
    after = reached.read_bytes() if reached.exists() else None
    assert after == before, f"write_file reached {reached} through {path!r}"
    for target in targets.values():
        assert target.read_text() == VICTIM, f"{target} was changed through {path!r}"
    assert not (outside / "created.txt").exists()
