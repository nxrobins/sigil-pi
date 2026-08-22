"""Every production tool proves the five required test classes.

The specialized semantic/property tests remain in their focused modules. This
file supplies uniform malformed-input and denied-capability executions, then
checks that the evidence matrix names collected tests for every manifest tool.
"""

import json
import re
import subprocess
import sys

import pytest

from agent import PiAgent, SessionStore
from conftest import PI_ROOT


MANIFEST = json.loads((PI_ROOT / "tools" / "manifest.json").read_text())
TOOLS = tuple(sorted(MANIFEST))
REQUIRED_CLASSES = {
    "happy_path", "malformed_input", "denied_capability", "boundary", "end_to_end"}


@pytest.mark.parametrize("tool", TOOLS)
def test_every_production_tool_rejects_a_missing_required_argument(tool, tmp_path, mcp):
    agent = PiAgent(
        "http://127.0.0.1:9/v1/messages", "unused",
        store=SessionStore(tmp_path / "sessions"),
        sandbox_root=tmp_path / "sandboxes", mcp=mcp)
    sandbox = agent.sandbox_for("matrix")
    entry = MANIFEST[tool]
    supplied = {name: "valid" for name in entry["args"][:-1]}
    grant_log = []
    message, is_error = agent._dispatch(
        tool, supplied, sandbox, grant_log, session="matrix")
    assert is_error is True
    assert "missing tool argument" in message
    assert grant_log == [], "malformed input reached a forge"


def _valid_wire_input(tool, sandbox):
    entry = MANIFEST[tool]
    file_path = sandbox / "note.txt"
    file_path.write_text("seed")
    values = {
        "path": str(sandbox if tool in {"list_dir", "list_tree", "grep_tree"}
                    else file_path),
        "content": "replacement",
        "old": "seed",
        "new": "changed",
        "pattern": "seed",
        "url": "https://example.com/resource",
        "package": "left-pad",
        "repo": "octocat/Hello-World",
        "project": "group/project",
    }
    args = [str(value) for value in entry.get("bound_args", [])]
    args.extend(values[name] for name in entry["args"])
    if entry.get("framing") == "len8":
        return "".join(f"{len(value.encode()):08d}{value}" for value in args)
    return "|".join(args)


@pytest.mark.parametrize("tool", TOOLS)
def test_every_production_tool_fails_without_its_manifest_capabilities(
        tool, tmp_path, mcp):
    sandbox = tmp_path / tool
    sandbox.mkdir()
    entry = MANIFEST[tool]
    source = (PI_ROOT / entry["source"]).read_text()
    result = mcp.forge(
        source, input=_valid_wire_input(tool, sandbox),
        fuel=50_000_000, grants={})
    assert result.get("status") != "ok", f"{tool} ran without its capabilities"
    rendered = json.dumps(result.get("diagnostics", []))
    assert "403" in rendered, f"{tool} did not fail with capability denial: {rendered}"


def test_tool_test_evidence_matrix_matches_manifest_and_collected_tests():
    matrix = json.loads(
        (PI_ROOT / "config" / "tool-test-matrix.json").read_text())
    assert set(matrix) == set(MANIFEST)
    for tool, evidence in matrix.items():
        assert set(evidence) == REQUIRED_CLASSES, (
            f"{tool} test classes differ: "
            f"missing={sorted(REQUIRED_CLASSES - set(evidence))}, "
            f"extra={sorted(set(evidence) - REQUIRED_CLASSES)}")
        assert all(isinstance(node, str) and node.startswith("tests/")
                   for node in evidence.values())

    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only"],
        capture_output=True, text=True, cwd=PI_ROOT)
    assert collected.returncode == 0, collected.stdout + collected.stderr
    node_ids = set(re.findall(r"^(tests/\S+::test_\S+)$", collected.stdout, re.M))
    for tool, evidence in matrix.items():
        for category, expected in evidence.items():
            assert any(node == expected or node.startswith(expected + "[")
                       for node in node_ids), (
                f"{tool}.{category} points to an uncollected test: {expected}")
