"""One supported Python range, defined once and refused outside at startup (F-24).

docs/support-matrix.md ships inside the bundle. It lists Python <3.12 and >3.14
as unsupported and requires that "unsupported selections must fail startup
where the process can detect them", and the release SBOM stamped
`python.requires >=3.12,<3.15`. But product_main.py and state_tool.py checked
only the floor, and pyproject.toml said `>=3.12`, so both entry points started
on this machine's default Python 3.15.1, which is the interpreter the bundle's
launcher (`exec python3`) runs here (audit 2026-11-10, F-24).

The range is now defined once, in toolchain.py. These tests pin every
restatement of it to that definition: pyproject.toml, the SBOM a real build
stamps, the support matrix and the operations guide. They also run each entry
point as a fresh process under a pretended `sys.version_info`, on both sides of
both bounds, so they need no second interpreter and never skip.
"""

import json
import re
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest

from conftest import PI_ROOT
from scripts.build_release import APP_FILES, build_release
from test_release_build import _fake_sigil

import toolchain

# The bundled programs held to the range. Each is run here as the launcher or an
# operator would run it, under interpreters it must refuse.
ENTRY_POINTS = ("product_main.py", "state_tool.py")

# Bundled programs this file does not hold to the range, and why. Pinned so that
# a NEW bundled program cannot arrive without either the startup check or a
# stated reason (see test_every_bundled_program_is_accounted_for).
OUTSIDE_THIS_GUARD = {
    "agent.py": "the research host, which the bundle never launches; it checks no "
                "version (agent lock)",
    "scripts/release_drill.py": "the release drill; it still checks only the floor "
                                "(drills lock)",
}

# The bundle's own top-level modules. Importing any of them before refusing an
# interpreter is work done on an interpreter the support matrix forbids.
BUNDLED_MODULES = sorted(
    Path(name).stem for name in APP_FILES if name.endswith(".py") and "/" not in name)

# Each entry point is executed with a pretended sys.version_info. A namedtuple
# keeps both the tuple order and the attribute names the real one has.
_PROBE = r"""
import json, runpy, sys
from collections import namedtuple

root, script, mode = sys.argv[1], sys.argv[2], sys.argv[3]
versions, bundled = json.loads(sys.argv[4]), set(json.loads(sys.argv[5]))
sys.path.insert(0, root)
VersionInfo = namedtuple("VersionInfo", "major minor micro releaselevel serial")


def loaded():
    return sorted(name for name in sys.modules if name in bundled)


if mode == "fresh":
    (version,) = versions
    sys.version_info = VersionInfo(*version)
    try:
        runpy.run_path(script, run_name="python_support_probe")
    finally:
        print(json.dumps(loaded()), flush=True)
else:
    outcomes = []
    for version in versions:
        sys.version_info = VersionInfo(*version)
        try:
            runpy.run_path(script, run_name="python_support_probe")
        except SystemExit as refusal:
            outcomes.append(str(refusal.code))
        else:
            outcomes.append(None)
    print(json.dumps(outcomes))
"""


def _probe(entry, versions, mode):
    return subprocess.run(
        [sys.executable, "-I", "-c", _PROBE, str(PI_ROOT), str(PI_ROOT / entry), mode,
         json.dumps([list(v) for v in versions]), json.dumps(BUNDLED_MODULES)],
        cwd=PI_ROOT, env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
        timeout=120)


def _shown(version):
    major, minor, micro, level, serial = version
    suffix = {"alpha": "a", "beta": "b", "candidate": "rc"}.get(level)
    return f"{major}.{minor}.{micro}" + (f"{suffix}{serial}" if suffix else "")


def _matrix_range():
    """(oldest supported, first unsupported) minor version, as the support
    matrix's Python row states them in its "Explicitly unsupported" column."""
    text = (PI_ROOT / "docs" / "support-matrix.md").read_text()
    row = next((line for line in text.splitlines() if line.startswith("| Python |")),
               None)
    assert row, "docs/support-matrix.md lost its Python row"
    unsupported = row.split("|")[3]
    below = re.search(r"<(\d+)\.(\d+)", unsupported)
    above = re.search(r">(\d+)\.(\d+)", unsupported)
    assert below and above, (
        f"the support matrix's Python row no longer states both bounds: {row}")
    return ((int(below[1]), int(below[2])), (int(above[1]), int(above[2]) + 1))


def _sbom_python_requires(sbom):
    component = next(c for c in sbom["components"] if c["name"] == "sigil-pi")
    properties = {p["name"]: p["value"] for p in component["properties"]}
    return properties["python.requires"]


def _admits(specifier, release):
    """Whether a PEP 440 specifier of plain comparison clauses admits a final
    release (major, minor, micro). Release segments compare zero-padded."""
    operators = {">=": lambda a, b: a >= b, ">": lambda a, b: a > b,
                 "<=": lambda a, b: a <= b, "<": lambda a, b: a < b,
                 "==": lambda a, b: a == b, "!=": lambda a, b: a != b}
    for clause in specifier.split(","):
        match = re.fullmatch(r"\s*(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+)*)\s*", clause)
        assert match, f"clause {clause!r} is outside what this test evaluates"
        bound = tuple(int(part) for part in match[2].split("."))
        width = max(len(bound), len(release))
        padded = (tuple(release) + (0,) * width)[:width]
        if not operators[match[1]](padded, (bound + (0,) * width)[:width]):
            return False
    return True


# ── one definition, every restatement pinned to it ──────────────────────────


def test_pyproject_and_the_built_sbom_state_the_defined_range(tmp_path, monkeypatch):
    """pip reads pyproject.toml's requires-python; an operator and a scanner
    read the SBOM's python.requires. They said different things (>=3.12 and
    >=3.12,<3.15), and neither was what the entry points enforced."""
    requires = tomllib.loads(
        (PI_ROOT / "pyproject.toml").read_text())["project"]["requires-python"]
    # Bytecode writing on, whatever the environment says (PYTHONDONTWRITEBYTECODE
    # is set on some developer hosts), so the bytecode check below can fire.
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    sigil, pin = _fake_sigil(tmp_path)
    archive, _ = build_release(
        sigil_root=sigil, output_dir=tmp_path / "out", app_root=PI_ROOT,
        pin_file=pin, build_runtime=False, platform_tag="test")
    published = json.loads(archive.with_name(
        archive.name.removesuffix(".tar.gz") + ".SBOM.cdx.json").read_text())
    with tarfile.open(archive, "r:gz") as opened:
        names = opened.getnames()
        prefix = names[0].split("/", 1)[0]
        bundled = json.load(opened.extractfile(f"{prefix}/SBOM.cdx.json"))
    # The build reads the range by running the bundle's own toolchain.py. An
    # import would leave a __pycache__ in the tree being archived, stamped with
    # copy times, so the bundle would carry bytecode and stop being reproducible.
    compiled = [name for name in names if "__pycache__" in name or name.endswith(".pyc")]
    assert not compiled, f"the bundle carries bytecode: {compiled}"
    stamped = {_sbom_python_requires(published), _sbom_python_requires(bundled)}
    assert stamped == {requires}, (
        f"pyproject.toml's requires-python is {requires!r} but the release SBOM "
        f"stamps {sorted(stamped)}: pip and the SBOM must state the same range")
    assert requires == toolchain.PYTHON_REQUIRES, (
        f"pyproject.toml and the SBOM say {requires!r}, but the range the entry "
        f"points enforce, defined in toolchain.py, is {toolchain.PYTHON_REQUIRES!r}")


def test_the_shipped_documents_state_the_defined_range():
    """The support matrix and the operations guide ship in the bundle and state
    the range in prose. A range changed in code alone would leave them telling
    operators something the startup check no longer does."""
    defined = (toolchain.MINIMUM_PYTHON, toolchain.FIRST_UNSUPPORTED_PYTHON)
    assert _matrix_range() == defined, (
        f"docs/support-matrix.md supports {_matrix_range()} (oldest, first "
        f"unsupported) but toolchain.py defines {defined}")
    guide = (PI_ROOT / "docs" / "operations.md").read_text()
    stated = re.search(r"Python (\d+)\.(\d+)–(\d+)\.(\d+)", guide)
    assert stated, "docs/operations.md no longer states the supported Python range"
    oldest = (int(stated[1]), int(stated[2]))
    first_unsupported = (int(stated[3]), int(stated[4]) + 1)
    assert (oldest, first_unsupported) == defined, (
        f"docs/operations.md says {stated[0]!r}, but toolchain.py defines "
        f"{toolchain.PYTHON_REQUIRES}")


def test_the_specifier_admits_exactly_the_defined_range():
    """PYTHON_REQUIRES is what pip and the SBOM see; the entry points compare
    against the two tuples. They must describe the same set of releases.

    Exhaustive rather than sampled: a specifier that is wrong only at a bound,
    such as `<=3.15`, differs on the single release 3.15.0, which random
    draws over this space almost never produce."""
    disagreements = [
        release
        for release in ((major, minor, micro) for major in range(10)
                        for minor in range(41) for micro in (0, 1, 9, 40))
        if _admits(toolchain.PYTHON_REQUIRES, release)
        != (toolchain.MINIMUM_PYTHON <= release[:2] < toolchain.FIRST_UNSUPPORTED_PYTHON)]
    assert not disagreements, (
        f"{toolchain.PYTHON_REQUIRES!r} and the range "
        f"[{toolchain.MINIMUM_PYTHON}, {toolchain.FIRST_UNSUPPORTED_PYTHON}) disagree "
        f"about {len(disagreements)} release(s), first {disagreements[:5]}")


# ── both entry points refuse both sides, before any work ────────────────────

UNSUPPORTED = (
    (2, 7, 18, "final", 0),
    (3, 11, 9, "final", 0),
    (3, 15, 0, "alpha", 1),
    (3, 15, 0, "final", 0),
    (3, 15, 1, "final", 0),  # this machine's default python3, which the launcher runs
    (3, 16, 2, "final", 0),
    (4, 0, 0, "final", 0),
)


@pytest.mark.parametrize("version", UNSUPPORTED, ids=_shown)
@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_an_entry_point_refuses_an_unsupported_interpreter_before_any_work(
        entry, version):
    result = _probe(entry, [version], "fresh")
    lines = result.stdout.splitlines()
    loaded = json.loads(lines[-1]) if lines else None
    assert result.returncode != 0, (
        f"{entry} started on Python {_shown(version)}, which docs/support-matrix.md "
        f"lists as unsupported: it exited 0 after importing {loaded}")
    assert loaded == ["toolchain"], (
        f"{entry} refused Python {_shown(version)} only after importing {loaded}; "
        f"the check must come before any of the bundle's own modules is loaded. "
        f"stderr: {result.stderr[-600:]!r}")
    assert result.returncode == 1 and result.stdout == '["toolchain"]\n', (
        f"exit status {result.returncode}, stdout {result.stdout!r}")
    assert result.stderr == toolchain.unsupported_python_message(version) + "\n", (
        f"the refusal must name the supported range and this interpreter: "
        f"{result.stderr!r}")


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_an_entry_point_admits_exactly_what_the_support_matrix_supports(entry):
    """Every major from 2 to 4, minors 0 to 20, a first and a later patch, an
    alpha and a final release: refused exactly when the support matrix calls
    that minor version unsupported."""
    grid = [(major, minor, micro, level, serial)
            for major in (2, 3, 4) for minor in range(21) for micro in (0, 9)
            for level, serial in (("alpha", 1), ("final", 0))]
    result = _probe(entry, grid, "grid")
    assert result.returncode == 0, result.stderr[-2000:]
    outcomes = json.loads(result.stdout.splitlines()[-1])
    low, high = _matrix_range()
    started_unsupported = [_shown(v) for v, refusal in zip(grid, outcomes)
                           if refusal is None and not low <= v[:2] < high]
    refused_supported = [_shown(v) for v, refusal in zip(grid, outcomes)
                         if refusal is not None and low <= v[:2] < high]
    assert not started_unsupported and not refused_supported, (
        f"{entry} started on {len(started_unsupported)} interpreter(s) the support "
        f"matrix lists as unsupported ({', '.join(started_unsupported[:12])}"
        f"{', ...' if len(started_unsupported) > 12 else ''}) and refused "
        f"{len(refused_supported)} it supports ({', '.join(refused_supported[:12])})")


def test_every_bundled_program_is_accounted_for():
    """A program added to the bundle must refuse unsupported interpreters at
    startup, which means joining ENTRY_POINTS, or say here why it does not."""
    programs = {
        name for name in APP_FILES if name.endswith(".py") and re.search(
            r"^if __name__ == [\"']__main__[\"']:", (PI_ROOT / name).read_text(),
            re.M)}
    assert set(ENTRY_POINTS) <= programs, (
        f"not bundled programs: {sorted(set(ENTRY_POINTS) - programs)}")
    unaccounted = programs - set(ENTRY_POINTS) - set(OUTSIDE_THIS_GUARD)
    assert not unaccounted, (
        f"bundled program(s) {sorted(unaccounted)} are not held to the supported "
        f"Python range: add them to ENTRY_POINTS, with the startup check")
    stale = set(OUTSIDE_THIS_GUARD) - programs
    assert not stale, f"no longer bundled programs: {sorted(stale)}"
