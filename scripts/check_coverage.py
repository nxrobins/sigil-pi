#!/usr/bin/env python3
"""Fail closed unless line and branch coverage independently meet the gate."""

import argparse
import ast
import json
from pathlib import Path


class CoverageGateError(RuntimeError):
    pass


def _percent(covered, total, label):
    if not isinstance(covered, int) or not isinstance(total, int) or total <= 0:
        raise CoverageGateError(f"coverage report has no valid {label} total")
    return covered * 100.0 / total


def _symbol_node(source, symbol, selector):
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise CoverageGateError(
            f"critical path source cannot be parsed: {selector}") from error
    found = {}

    def walk(nodes, prefix=""):
        for node in nodes:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = f"{prefix}.{node.name}" if prefix else node.name
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found[qualified] = node
                walk(node.body, qualified)

    walk(tree.body)
    node = found.get(symbol)
    if node is None:
        raise CoverageGateError(f"critical symbol is absent from source: {selector}")
    return node


def _check_critical_symbol(files, selector, source_root):
    name, symbol = selector.split(":", 1)
    data = files.get(name)
    if not data:
        raise CoverageGateError(f"critical path is absent from coverage data: {name}")
    try:
        source = (Path(source_root) / name).read_text(encoding="utf-8")
    except OSError as error:
        raise CoverageGateError(
            f"critical path source cannot be read: {name}") from error
    node = _symbol_node(source, symbol, selector)
    start, end = node.lineno, node.end_lineno
    executed = data.get("executed_lines")
    missing = data.get("missing_lines")
    missing_branches = data.get("missing_branches")
    if not all(isinstance(value, list)
               for value in (executed, missing, missing_branches)):
        raise CoverageGateError(
            f"coverage report lacks detailed critical-symbol data: {selector}")
    relevant = [line for line in (*executed, *missing) if start <= line <= end]
    if not relevant:
        raise CoverageGateError(
            f"critical symbol has no executable coverage data: {selector}")
    line_gaps = sorted(line for line in missing if start <= line <= end)
    branch_gaps = sorted(
        branch for branch in missing_branches
        if (isinstance(branch, list) and len(branch) == 2
            and start <= branch[0] <= end))
    if line_gaps or branch_gaps:
        raise CoverageGateError(
            f"critical symbol {selector} is not 100% covered; "
            f"missing lines={line_gaps}, branches={branch_gaps}")


def evaluate(report, *, minimum_line=85.0, minimum_branch=85.0, critical=(),
             source_root="."):
    totals = report.get("totals", {})
    line = _percent(totals.get("covered_lines"), totals.get("num_statements"), "line")
    branch = _percent(totals.get("covered_branches"), totals.get("num_branches"), "branch")
    failures = []
    if line < minimum_line:
        failures.append(f"line coverage {line:.2f}% is below {minimum_line:.2f}%")
    if branch < minimum_branch:
        failures.append(f"branch coverage {branch:.2f}% is below {minimum_branch:.2f}%")
    files = report.get("files", {})
    for name in critical:
        if ":" in name:
            try:
                _check_critical_symbol(files, name, source_root)
            except CoverageGateError as error:
                failures.append(str(error))
            continue
        summary = files.get(name, {}).get("summary")
        if not summary:
            failures.append(f"critical path is absent from coverage data: {name}")
            continue
        critical_line = _percent(
            summary.get("covered_lines"), summary.get("num_statements"), f"{name} line")
        critical_branch = _percent(
            summary.get("covered_branches"), summary.get("num_branches"), f"{name} branch")
        if critical_line < 100.0 or critical_branch < 100.0:
            failures.append(
                f"critical path {name} is {critical_line:.2f}% line / "
                f"{critical_branch:.2f}% branch, requires 100% / 100%")
    if failures:
        raise CoverageGateError("; ".join(failures))
    return line, branch


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report")
    parser.add_argument("--minimum-line", type=float, default=85.0)
    parser.add_argument("--minimum-branch", type=float, default=85.0)
    parser.add_argument("--critical", action="append", default=[])
    parser.add_argument("--source-root", default=".")
    args = parser.parse_args(argv)
    try:
        with open(args.report, encoding="utf-8") as stream:
            report = json.load(stream)
        line, branch = evaluate(
            report, minimum_line=args.minimum_line,
            minimum_branch=args.minimum_branch, critical=args.critical,
            source_root=args.source_root)
    except (OSError, json.JSONDecodeError, CoverageGateError) as error:
        parser.exit(1, f"coverage gate failed: {error}\n")
    print(f"coverage gate passed: line={line:.2f}% branch={branch:.2f}%")


if __name__ == "__main__":
    main()
