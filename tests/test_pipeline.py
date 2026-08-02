"""M12 pipeline dispatch — fetch→shape as two forges.

`http` is outer-ring and `json` is inner-ring (R004), so every AXI-style
digested tool is necessarily a pipeline: a granted fetch stage, then a
zero-grant shaping stage. The host already runs exactly this shape for the
LLM call (`_parse(self._llm(...))`); the manifest mechanism under test here
generalizes it to dispatched tools:

  "shape":      a SECOND forge after the granted stage — input is stage 1's
                output verbatim, grants are None ALWAYS (the parse_reply
                discipline: untrusted bytes are shaped in a sandbox with no
                capabilities at all).
  "bound_args": host-constant strings prepended to the model's args on the
                wire — how a fixed-host tool (npm_info, gh_issues) gets its
                base URL from the manifest rather than from the model, and
                what makes those tools point-at-a-mock testable.

Fixtures: tests/fixtures/{echo_tool,upper_shape,fail_shape}.sigil — probed
standalone before these tests were written.
"""
import json

from conftest import API_KEY, msg, text, tool_use  # fixtures via conftest
from test_tools import len8


def _spec(name, args):
    return {"name": name, "description": f"test tool {name}",
            "input_schema": {"type": "object",
                             "properties": {a: {"type": "string"} for a in args},
                             "required": list(args)}}


def pipeline_agent(scripted_llm, tmp_path, mcp, manifest: dict):
    """An agent over a TEST manifest — the mechanism is manifest-driven, so
    the tests drive it through manifest entries, not code changes."""
    from agent import PiAgent, SessionStore
    (tmp_path / "sessions").mkdir(exist_ok=True)
    (tmp_path / "sandboxes").mkdir(exist_ok=True)
    mpath = tmp_path / "manifest.json"
    mpath.write_text(json.dumps(manifest))
    return PiAgent(scripted_llm.url, API_KEY,
                   store=SessionStore(tmp_path / "sessions"),
                   sandbox_root=tmp_path / "sandboxes", mcp=mcp,
                   model="claude-mock", manifest_path=mpath)


def _last_result(scripted_llm):
    return scripted_llm.requests[-1]["messages"][-1]["content"][0]


READ_UPPER = {
    "read_upper": {
        "source": "tools/read_file.sigil",
        "shape": "tests/fixtures/upper_shape.sigil",
        "args": ["path"], "path_args": ["path"],
        "grants": {"fs": ["{SANDBOX}"]},
        "spec": _spec("read_upper", ["path"]),
    }
}


def test_shape_stage_receives_stage_one_output(scripted_llm, tmp_path, mcp):
    """The defining behavior: stage 2's input is stage 1's OUTPUT (the file
    bytes), not the tool's original input (the path) — proven by a shaper
    whose transform is visible in the result."""
    agent = pipeline_agent(scripted_llm, tmp_path, mcp, READ_UPPER)
    (agent.sandbox_for("s1") / "note.txt").write_text("quiet bytes")
    scripted_llm.script = [
        msg([tool_use("t", "read_upper", {"path": "note.txt"})]),
        msg([text("done")]),
    ]
    assert agent.turn("s1", "read it loudly") == "done"
    r = _last_result(scripted_llm)
    assert r["content"] == "QUIET BYTES"
    assert "is_error" not in r


def test_shape_stage_is_forged_grantless(scripted_llm, tmp_path, mcp):
    """The security half of the pattern: the granted stage's grants NEVER
    reach the shaper. grant_log must show stage 1 with its fs grant and the
    `.shape` forge with None — the parse_reply discipline, enforced."""
    agent = pipeline_agent(scripted_llm, tmp_path, mcp, READ_UPPER)
    sb = agent.sandbox_for("s1")
    (sb / "note.txt").write_text("hi")
    scripted_llm.script = [
        msg([tool_use("t", "read_upper", {"path": "note.txt"})]),
        msg([text("done")]),
    ]
    agent.turn("s1", "go")
    assert agent.grant_log == [
        ("read_upper", {"fs": [str(sb)]}),
        ("read_upper.shape", None),
    ]


def test_stage_one_error_skips_the_shape_stage(scripted_llm, tmp_path, mcp):
    """A failed fetch has no output to shape: the error surfaces as the
    tool_result and the shape forge never runs (no `.shape` in grant_log)."""
    agent = pipeline_agent(scripted_llm, tmp_path, mcp, READ_UPPER)
    scripted_llm.script = [
        msg([tool_use("t", "read_upper", {"path": "missing.txt"})]),
        msg([text("failed")]),
    ]
    assert agent.turn("s1", "read nothing") == "failed"
    r = _last_result(scripted_llm)
    assert r["is_error"] is True
    assert not [e for e in agent.grant_log if e[0].endswith(".shape")], \
        "shape stage ran despite a failed stage 1"


def test_shape_stage_error_is_a_tool_error(scripted_llm, tmp_path, mcp):
    """A shaper failure (malformed upstream payload, in real tools) is an
    is_error tool_result the model can react to — never a crash."""
    manifest = {"read_broken": {**READ_UPPER["read_upper"],
                                "shape": "tests/fixtures/fail_shape.sigil",
                                "spec": _spec("read_broken", ["path"])}}
    agent = pipeline_agent(scripted_llm, tmp_path, mcp, manifest)
    (agent.sandbox_for("s1") / "note.txt").write_text("hi")
    scripted_llm.script = [
        msg([tool_use("t", "read_broken", {"path": "note.txt"})]),
        msg([text("acknowledged")]),
    ]
    assert agent.turn("s1", "go") == "acknowledged"
    r = _last_result(scripted_llm)
    assert r["is_error"] is True
    assert "461" in r["content"]


ECHO_BOUND = {
    "echo": {
        "source": "tests/fixtures/echo_tool.sigil",
        "args": ["v"], "path_args": [],
        "bound_args": ["BASE"],
        "grants": {},
        "spec": _spec("echo", ["v"]),
    }
}


def test_bound_args_are_prepended_on_the_wire(scripted_llm, tmp_path, mcp):
    """bound_args come from the MANIFEST (operator), not the model — the
    echo fixture returns the wire bytes, so the test sees exactly what the
    guest would parse: `BASE|<model arg>`."""
    agent = pipeline_agent(scripted_llm, tmp_path, mcp, ECHO_BOUND)
    scripted_llm.script = [
        msg([tool_use("t", "echo", {"v": "value-from-model"})]),
        msg([text("ok")]),
    ]
    agent.turn("s1", "echo")
    assert _last_result(scripted_llm)["content"] == "BASE|value-from-model"


def test_bound_args_ride_len8_framing_too(scripted_llm, tmp_path, mcp):
    """Same prepend rule under len8 framing: bound args are frames like any
    other, so a shaped tool with arbitrary-byte args still gets its base."""
    manifest = {"echo": {**ECHO_BOUND["echo"], "framing": "len8"}}
    agent = pipeline_agent(scripted_llm, tmp_path, mcp, manifest)
    scripted_llm.script = [
        msg([tool_use("t", "echo", {"v": "pi|pe"})]),
        msg([text("ok")]),
    ]
    agent.turn("s1", "echo")
    assert _last_result(scripted_llm)["content"] == len8("BASE", "pi|pe")


def test_clip_applies_to_the_shaped_output(scripted_llm, tmp_path, mcp):
    """M8's bound applies to what actually enters the transcript — the
    SHAPED result, clipped after stage 2, uppercase head and honest notice."""
    agent = pipeline_agent(scripted_llm, tmp_path, mcp, READ_UPPER)
    agent.max_tool_result_bytes = 200
    (agent.sandbox_for("s1") / "big.txt").write_text("x" * 5000)
    scripted_llm.script = [
        msg([tool_use("t", "read_upper", {"path": "big.txt"})]),
        msg([text("done")]),
    ]
    agent.turn("s1", "read big")
    r = _last_result(scripted_llm)
    assert r["content"].startswith("XXX")
    assert "clipped" in r["content"]
    assert len(r["content"].encode()) <= 200
