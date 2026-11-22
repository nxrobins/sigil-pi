"""The drill scripts refuse the interpreters the product refuses (audit F-24).

toolchain.py defines the supported Python range once, and product_main.py and
state_tool.py refuse anything outside it at startup. The three drill scripts
still compared `sys.version_info` with a floor of their own, `(3, 12)`, so
`scripts/release_drill.py` (which ships in the bundle, behind a launcher that
runs whatever `python3` the host has), `scripts/drill_fixture.py` and
`scripts/failure_drill.py` all started on Python 3.15.1, which
docs/support-matrix.md lists as unsupported.

Each script is run here as a fresh process under a pretended
`sys.version_info`, the way tests/test_python_support.py runs the two entry
points, so no second interpreter is needed and nothing skips. The script is
given no help finding `toolchain`: it is not on the probe's path.
"""

import ast
import json
import subprocess
import sys

import pytest

from conftest import PI_ROOT

import toolchain

DRILLS = ("scripts/release_drill.py", "scripts/drill_fixture.py",
          "scripts/failure_drill.py")

# The project's own modules: loading any of them but toolchain before refusing
# an interpreter is work done on an interpreter the support matrix forbids.
PROJECT_MODULES = sorted(path.stem for path in PI_ROOT.glob("*.py"))

_PROBE = r"""
import json, runpy, sys
from collections import namedtuple

script, mode = sys.argv[1], sys.argv[2]
versions, project = json.loads(sys.argv[3]), set(json.loads(sys.argv[4]))
VersionInfo = namedtuple("VersionInfo", "major minor micro releaselevel serial")


def loaded():
    return sorted(name for name in sys.modules
                  if name in project or name.split(".")[0] == "scripts")


if mode == "fresh":
    (version,) = versions
    sys.version_info = VersionInfo(*version)
    try:
        runpy.run_path(script, run_name="drill_python_support_probe")
    finally:
        print(json.dumps(loaded()), flush=True)
else:
    outcomes = []
    for version in versions:
        sys.version_info = VersionInfo(*version)
        try:
            runpy.run_path(script, run_name="drill_python_support_probe")
        except SystemExit as refusal:
            outcomes.append(str(refusal.code))
        else:
            outcomes.append(None)
    print(json.dumps(outcomes))
"""


def _probe(script, versions, mode):
    return subprocess.run(
        [sys.executable, "-I", "-c", _PROBE, str(PI_ROOT / script), mode,
         json.dumps([list(version) for version in versions]),
         json.dumps(PROJECT_MODULES)],
        cwd=PI_ROOT, env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
        timeout=120)


def _shown(version):
    major, minor, micro, level, serial = version
    suffix = {"alpha": "a", "beta": "b", "candidate": "rc"}.get(level)
    return f"{major}.{minor}.{micro}" + (f"{suffix}{serial}" if suffix else "")


UNSUPPORTED = (
    (2, 7, 18, "final", 0),
    (3, 11, 9, "final", 0),
    (3, 15, 0, "alpha", 1),
    (3, 15, 0, "final", 0),
    (3, 15, 1, "final", 0),  # this machine's default python3
    (3, 16, 2, "final", 0),
    (4, 0, 0, "final", 0),
)


@pytest.mark.parametrize("version", UNSUPPORTED, ids=_shown)
@pytest.mark.parametrize("script", DRILLS)
def test_a_drill_refuses_an_unsupported_interpreter_before_any_work(script, version):
    result = _probe(script, [version], "fresh")
    lines = result.stdout.splitlines()
    loaded = json.loads(lines[-1]) if lines else None
    assert result.returncode != 0, (
        f"{script} started on Python {_shown(version)}, which docs/support-matrix.md "
        f"lists as unsupported: it exited 0 after loading {loaded} (audit F-24)")
    assert loaded == ["toolchain"], (
        f"{script} refused Python {_shown(version)} only after loading {loaded}; "
        f"the check must come before any other module of the project. "
        f"stderr: {result.stderr[-600:]!r}")
    assert result.returncode == 1 and result.stdout == '["toolchain"]\n', (
        f"exit status {result.returncode}, stdout {result.stdout!r}")
    assert result.stderr == toolchain.unsupported_python_message(version) + "\n", (
        f"the refusal must be the product's own, naming the supported range and "
        f"this interpreter: {result.stderr!r}")


@pytest.mark.parametrize("script", DRILLS)
def test_a_drill_admits_exactly_the_interpreters_the_product_admits(script):
    """Every major from 2 to 4, minors 0 to 20, a first and a later patch, an
    alpha and a final release: refused exactly when toolchain.py's range,
    which tests/test_python_support.py pins to the support matrix, excludes
    that minor version."""
    grid = [(major, minor, micro, level, serial)
            for major in (2, 3, 4) for minor in range(21) for micro in (0, 9)
            for level, serial in (("alpha", 1), ("final", 0))]
    result = _probe(script, grid, "grid")
    assert result.returncode == 0, result.stderr[-2000:]
    outcomes = json.loads(result.stdout.splitlines()[-1])

    def supported(version):
        return (toolchain.MINIMUM_PYTHON <= version[:2]
                < toolchain.FIRST_UNSUPPORTED_PYTHON)

    started_unsupported = [_shown(version) for version, refusal in zip(grid, outcomes)
                           if refusal is None and not supported(version)]
    refused_supported = [_shown(version) for version, refusal in zip(grid, outcomes)
                         if refusal is not None and supported(version)]
    assert not started_unsupported and not refused_supported, (
        f"{script} started on {len(started_unsupported)} interpreter(s) outside "
        f"{toolchain.PYTHON_REQUIRES} ({', '.join(started_unsupported[:12])}"
        f"{', ...' if len(started_unsupported) > 12 else ''}) and refused "
        f"{len(refused_supported)} inside it ({', '.join(refused_supported[:12])})")
    refusals = {refusal for refusal in outcomes if refusal is not None}
    assert all("sigil-pi requires Python " + toolchain.PYTHON_REQUIRES in refusal
               for refusal in refusals), refusals


@pytest.mark.parametrize("script", DRILLS)
def test_a_drill_states_no_python_range_of_its_own(script):
    """The class of the defect: a second definition of the range, which can
    drift from the first. Every comparison a drill makes with
    `sys.version_info` is against toolchain.py's two bounds."""
    tree = ast.parse((PI_ROOT / script).read_text())
    own = sorted(
        ast.unparse(target) for node in ast.walk(tree) if isinstance(node, ast.Assign)
        for target in node.targets if "PYTHON" in ast.unparse(target).upper())
    assert not own, f"{script} defines {own}: the range is toolchain.py's to define"
    checks = [node for node in ast.walk(tree)
              if isinstance(node, ast.Compare) and "version_info" in ast.unparse(node)]
    assert checks, f"{script} never checks the interpreter it runs on"
    bounds = set()
    for check in checks:
        for operand in (check.left, *check.comparators):
            text = ast.unparse(operand)
            if "version_info" not in text:
                bounds.add(text)
    assert bounds == {"toolchain.MINIMUM_PYTHON", "toolchain.FIRST_UNSUPPORTED_PYTHON"}, (
        f"{script} compares sys.version_info with {sorted(bounds)}, not with both "
        f"bounds of the one range toolchain.py defines")
