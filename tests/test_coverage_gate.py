"""The quality gate treats line and branch coverage as separate blockers."""

import pytest

from scripts.check_coverage import CoverageGateError, evaluate


def _report(lines=(90, 100), branches=(85, 100), files=None):
    return {
        "totals": {
            "covered_lines": lines[0], "num_statements": lines[1],
            "covered_branches": branches[0], "num_branches": branches[1],
        },
        "files": files or {},
    }


def test_line_and_branch_thresholds_pass_independently():
    assert evaluate(_report()) == (90.0, 85.0)


@pytest.mark.parametrize("lines,branches,match", [
    ((84, 100), (100, 100), "line coverage"),
    ((100, 100), (84, 100), "branch coverage"),
    ((0, 0), (100, 100), "valid line total"),
])
def test_each_missing_coverage_dimension_fails(lines, branches, match):
    with pytest.raises(CoverageGateError, match=match):
        evaluate(_report(lines, branches))


def test_named_critical_paths_require_complete_line_and_branch_coverage():
    files = {"security.py": {"summary": {
        "covered_lines": 10, "num_statements": 10,
        "covered_branches": 3, "num_branches": 4,
    }}}
    with pytest.raises(CoverageGateError, match="requires 100%"):
        evaluate(_report(files=files), critical=["security.py"])
    with pytest.raises(CoverageGateError, match="absent"):
        evaluate(_report(), critical=["missing.py"])


def test_named_critical_symbols_require_every_line_and_branch(tmp_path):
    source = tmp_path / "security.py"
    source.write_text(
        "class Boundary:\n"
        "    def enforce(self, allowed):\n"
        "        if allowed:\n"
        "            return True\n"
        "        return False\n")
    complete = {"security.py": {
        "executed_lines": [1, 2, 3, 4, 5],
        "missing_lines": [],
        "missing_branches": [],
    }}
    assert evaluate(
        _report(files=complete),
        critical=["security.py:Boundary.enforce"],
        source_root=tmp_path) == (90.0, 85.0)

    line_gap = {"security.py": {
        "executed_lines": [1, 2, 3, 4],
        "missing_lines": [5],
        "missing_branches": [],
    }}
    with pytest.raises(CoverageGateError, match=r"missing lines=\[5\]"):
        evaluate(
            _report(files=line_gap),
            critical=["security.py:Boundary.enforce"], source_root=tmp_path)

    branch_gap = {"security.py": {
        "executed_lines": [1, 2, 3, 4, 5],
        "missing_lines": [],
        "missing_branches": [[3, 5]],
    }}
    with pytest.raises(CoverageGateError, match="branches"):
        evaluate(
            _report(files=branch_gap),
            critical=["security.py:Boundary.enforce"], source_root=tmp_path)


def test_critical_symbol_gate_rejects_missing_source_symbol_and_detail(tmp_path):
    source = tmp_path / "security.py"
    source.write_text("def present():\n    return True\n")
    detailed = {"security.py": {
        "executed_lines": [1, 2], "missing_lines": [], "missing_branches": []}}
    with pytest.raises(CoverageGateError, match="symbol is absent"):
        evaluate(
            _report(files=detailed), critical=["security.py:missing"],
            source_root=tmp_path)
    with pytest.raises(CoverageGateError, match="cannot be read"):
        evaluate(
            _report(files={"missing.py": detailed["security.py"]}),
            critical=["missing.py:present"], source_root=tmp_path)
    with pytest.raises(CoverageGateError, match="lacks detailed"):
        evaluate(
            _report(files={"security.py": {"summary": {}}}),
            critical=["security.py:present"], source_root=tmp_path)
