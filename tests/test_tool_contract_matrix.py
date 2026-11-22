"""Every production tool proves the five required test classes.

The specialized semantic/property tests remain in their focused modules. This
file supplies uniform malformed-input and denied-capability executions, then
checks that the evidence matrix names collected tests for every manifest tool,
and that it says what each of its columns tests (audit 2026-11-10, F-17).
"""

import ast
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


def _matrix_document():
    return json.loads((PI_ROOT / "config" / "tool-test-matrix.json").read_text())


def test_tool_test_evidence_matrix_matches_manifest_and_collected_tests():
    matrix = _matrix_document()["tools"]
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


# ── what each column tests ───────────────────────────────────────────────


def _calls_in(node_id):
    """The names every call in the module-level test function `node_id`
    names makes: `f(...)` gives f, `x.f(...)` gives f."""
    path, _, name = node_id.partition("::")
    tree = ast.parse((PI_ROOT / path).read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name.split("[", 1)[0]:
            return {call.func.attr if isinstance(call.func, ast.Attribute)
                    else getattr(call.func, "id", None)
                    for call in ast.walk(node) if isinstance(call, ast.Call)}
    raise AssertionError(f"{node_id}: no module-level test function of that name")


def test_tool_test_matrix_says_what_each_column_tests():
    """Five columns read as five proofs per tool, and they were not: for 9
    of 12 tools `happy_path` and `end_to_end` named the same test, and
    `malformed_input` is one host-side branch run for all 12, in which the
    tool never executes (audit 2026-11-10, F-17). The matrix now says what
    each column tests, and this keeps what it says true: the tools whose two
    success columns share a test are listed, and each column's statement of
    what its tests call, or never call, is checked against their code."""
    document = _matrix_document()
    columns, tools = document["columns"], document["tools"]
    assert set(columns) == REQUIRED_CLASSES, sorted(columns)
    for column, statement in columns.items():
        assert isinstance(statement.get("tests"), str) and statement["tests"].strip(), (
            f"{column}: says nothing about what it tests")
        assert set(statement) <= {"tests", "bound_tests_call_one_of",
                                  "bound_tests_call_none_of"}, sorted(statement)

    shared = sorted(tool for tool, evidence in tools.items()
                    if evidence["happy_path"] == evidence["end_to_end"])
    assert document["same_test_as_end_to_end"] == shared, (
        "same_test_as_end_to_end must list exactly the tools whose happy_path "
        f"and end_to_end name one test: {shared}")

    for column, statement in sorted(columns.items()):
        one_of = set(statement.get("bound_tests_call_one_of", ()))
        none_of = set(statement.get("bound_tests_call_none_of", ()))
        for tool, evidence in sorted(tools.items()):
            calls = _calls_in(evidence[column])
            assert not one_of or calls & one_of, (
                f"{tool}.{column}: {evidence[column]} calls none of {sorted(one_of)}, "
                f"which is what the column says its tests do")
            assert not calls & none_of, (
                f"{tool}.{column}: {evidence[column]} calls {sorted(calls & none_of)}, "
                f"which the column says its tests never do")
