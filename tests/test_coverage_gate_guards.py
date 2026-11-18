"""What the coverage gate measures, and the three lists that name its 100% inventory.

Audit 2026-11-10, F-17. ci.sh measures the modules its --cov arguments name,
requires 85% line and branch coverage of them, and 100% of each function its
--critical arguments name. Two ways that can hold less than it says:

- coverage.py leaves out of both measures every line one of its exclusion
  patterns matches: a "# pragma: no cover" comment, a "# pragma: no branch",
  or a pattern the configuration adds. With one such comment in a critical
  function the 100% gate passes while the line it hides never runs. The gate's
  policy forbade new pragmas, and nothing enforced it.
- the inventory is written three times, in ci.sh, in tests/test_guards.py and
  in docs/product-readiness.md, and only the first two were compared, so the
  record a reviewer reads could name functions the gate does not check, or
  miss ones it does.

Every list here is read from the gate itself (ci.sh), so a module added to the
measure is scanned, and an entry added to the inventory is held to the record,
with no edit to this file.
"""
import ast
import os
import re
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

import coverage
import coverage.config
import pytest

import toolchain
from test_release_workflow import _action, _permissions, _with, _workflow, _writes

PI_ROOT = Path(__file__).resolve().parent.parent
CI_SH = PI_ROOT / "ci.sh"
CI_YML = PI_ROOT / ".github" / "workflows" / "ci.yml"
READINESS = PI_ROOT / "docs" / "product-readiness.md"
INVENTORY_HEADING = "## Critical security coverage inventory"

# Spellings coverage.py honours, each planted below to show the scan finds it.
PLANTED = (
    "# pragma: no cover",
    "#pragma:no cover",
    "# PRAGMA: NO COVER",
    "#  pragma no cover",
    "# pragma: no branch",
    "#pragma no branch",
)


def measured_modules(ci_source):
    """The files the gate measures: one per --cov= argument of ci.sh."""
    names = re.findall(r"--cov=([A-Za-z_][\w.]*)", ci_source)
    return [PI_ROOT / (name.replace(".", "/") + ".py") for name in names]


def critical_inventory(ci_source):
    """The entries of the gate's 100% inventory: its --critical arguments."""
    return set(re.findall(r"--critical\s+([^\s\\]+)", ci_source))


def exclusion_patterns(root):
    """Every pattern by which coverage.py, configured as the gate runs it,
    leaves a line out of the line or the branch measure.

    The gate runs pytest from the repository root, where coverage.py reads
    its configuration (.coveragerc, setup.cfg, tox.ini or pyproject.toml). The
    built-in patterns are added whatever that configuration says, so a pragma
    is found even under a configuration that stopped honouring it."""
    cwd = os.getcwd()
    os.chdir(root)
    try:
        configured = coverage.Coverage(data_file=None)
        patterns = [
            *configured.get_option("report:exclude_lines"),
            *configured.get_option("report:partial_branches"),
        ]
    finally:
        os.chdir(cwd)
    for built_in in (*coverage.config.DEFAULT_EXCLUDE, *coverage.config.DEFAULT_PARTIAL):
        if built_in not in patterns:
            patterns.append(built_in)
    return patterns


def excluded_lines(path, patterns):
    """(file, line number, line) for each line of `path` a pattern matches.

    coverage.py matches each pattern against the whole text, multi-line, and
    excludes every line a match touches; this does the same."""
    text = Path(path).read_text(encoding="utf-8")
    lines = text.splitlines()
    found = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.MULTILINE):
            first = text.count("\n", 0, match.start())
            last = text.count("\n", 0, match.end())
            for index in range(first, min(last, len(lines) - 1) + 1):
                found.add((Path(path).name, index + 1, lines[index].strip()))
    return sorted(found)


def readiness_inventory(text):
    """The entries the readiness record's inventory table names: each
    backquoted file (the whole file) or file:function, written as ci.sh
    writes it, in the rows of the table under INVENTORY_HEADING."""
    section = text.split(INVENTORY_HEADING, 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("|")]
    entries = set()
    for row in rows[2:]:                # past the header and its rule
        entries.update(re.findall(r"`([\w/]+\.py(?::[\w.]+)?)`", row))
    return entries


def test_the_gate_measures_the_product_and_names_its_inventory():
    """The scans below read their lists from ci.sh. Were the patterns to stop
    matching, every scan would pass with nothing scanned."""
    source = CI_SH.read_text()
    modules = measured_modules(source)
    names = {module.relative_to(PI_ROOT).as_posix() for module in modules}
    assert {"agent.py", "product_service.py", "product_main.py",
            "runtime_client.py"} <= names, names
    missing = [name for name in sorted(names) if not (PI_ROOT / name).is_file()]
    assert not missing, f"ci.sh measures modules that do not exist: {missing}"
    inventory = critical_inventory(source)
    assert len(inventory) > 20, inventory
    unmeasured = sorted(entry for entry in inventory
                        if entry.split(":", 1)[0] not in names)
    assert not unmeasured, (
        f"ci.sh requires 100% of code it does not measure: {unmeasured}")


def test_no_line_of_a_measured_module_is_left_out_of_the_coverage_measure():
    """No coverage pragma, and no line any exclusion pattern matches, in a
    module the gate measures. The 85% and 100% figures are only true of the
    lines they count."""
    patterns = exclusion_patterns(PI_ROOT)
    hidden = []
    for module in measured_modules(CI_SH.read_text()):
        hidden.extend(excluded_lines(module, patterns))
    assert not hidden, (
        "coverage.py would leave these lines of measured modules out of the line "
        "and branch measures, so the gate would pass without them ever running; "
        "test them instead: "
        + "; ".join(f"{name}:{number}: {line}" for name, number, line in hidden))


def test_the_coverage_configuration_omits_no_file():
    """An omitted file leaves the measure silently: its lines count for
    nothing, and only a --critical entry in it would notice."""
    cwd = os.getcwd()
    os.chdir(PI_ROOT)
    try:
        configured = coverage.Coverage(data_file=None)
        omitted = (configured.get_option("run:omit") or [],
                   configured.get_option("report:omit") or [])
    finally:
        os.chdir(cwd)
    assert omitted == ([], []), f"the coverage configuration omits files: {omitted}"


@pytest.mark.parametrize("spelling", PLANTED)
def test_the_scan_finds_a_pragma_planted_in_each_measured_module(tmp_path, spelling):
    """The scan is shown to fire: a pragma planted on a statement of each
    measured module is found on that line, and coverage.py itself leaves the
    line out (a "no branch" pragma leaves out the line's branches instead,
    which coverage reports nowhere, so only the scan can see it)."""
    patterns = exclusion_patterns(PI_ROOT)
    measure = coverage.Coverage(data_file=None)
    for module in measured_modules(CI_SH.read_text()):
        source = module.read_text(encoding="utf-8")
        lines = source.splitlines(keepends=True)
        target = min(node.lineno for node in ast.walk(ast.parse(source))
                     if isinstance(node, ast.Return)) - 1
        lines[target] = f"{lines[target].rstrip()}  {spelling}\n"
        planted = tmp_path / spelling.replace(" ", "_").replace(":", "") / module.name
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text("".join(lines), encoding="utf-8")

        found = excluded_lines(planted, patterns)
        assert [(number, line) for _, number, line in found] == [
            (target + 1, lines[target].strip())], (module.name, found)
        if "branch" not in spelling:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                _, _, excluded, _, _ = measure.analysis2(str(planted))
            assert target + 1 in excluded, (module.name, spelling, excluded)


def test_a_pattern_the_configuration_adds_is_scanned_for(tmp_path):
    """A configuration that excludes more is held to the same rule: the scan
    reads the patterns coverage.py is configured with, not a fixed list."""
    (tmp_path / ".coveragerc").write_text(
        "[report]\nexclude_also =\n    raise ProductError\n", encoding="utf-8")
    patterns = exclusion_patterns(tmp_path)
    found = excluded_lines(PI_ROOT / "product_service.py", patterns)
    assert found and all("raise ProductError" in line for _, _, line in found), found[:3]


def test_the_readiness_record_names_exactly_the_gate_s_inventory():
    """docs/product-readiness.md is the third list. It is what a reviewer
    reads to learn which boundaries are held to 100%, so it names exactly the
    entries ci.sh checks, each written as ci.sh writes it."""
    gate = critical_inventory(CI_SH.read_text())
    record = readiness_inventory(READINESS.read_text())
    assert record == gate, (
        "docs/product-readiness.md's inventory differs from ci.sh's --critical "
        f"entries: only in the record={sorted(record - gate)}, "
        f"only in ci.sh={sorted(gate - record)}")



def test_every_size_the_readiness_record_gives_its_inventory_is_the_gate_s():
    """The record's prose also says how many entries the inventory names, and
    a number written once goes stale when ci.sh gains entries: it said 145
    after the compiler pool had taken the gate to 154. Every "names <n>
    entries" the record states must be the number ci.sh checks."""
    gate = len(critical_inventory(CI_SH.read_text()))
    stated = [int(n) for n in
              re.findall(r"inventory names (\d+) entries", READINESS.read_text())]
    assert stated, ("the record no longer states its inventory's size; "
                    "drop or retarget this guard")
    assert all(n == gate for n in stated), (
        f"docs/product-readiness.md says the inventory names {stated} entries; "
        f"ci.sh checks {gate}")

def test_the_record_parser_reads_every_entry_form():
    """readiness_inventory is shown to read whole files and functions, and
    nothing outside the table's body."""
    text = (
        "intro `a.py:Outside.table`\n"
        f"{INVENTORY_HEADING}\n\n"
        "Prose naming `b.py:Not.a.row`.\n\n"
        "| Boundary | Enforced code |\n|---|---|\n"
        "| One | All of `runtime_client.py`; `product_service.py:serve_product` |\n"
        "| Two | `agent.py:PiAgent._dispatch`, `scripts/x.py:f` and `ci.sh` |\n"
        "\n## Next\n| Other | `c.py:After.section` |\n")
    assert readiness_inventory(text) == {
        "runtime_client.py", "product_service.py:serve_product",
        "agent.py:PiAgent._dispatch", "scripts/x.py:f"}


# ── the gate's own inputs (the ci lock's items from W1's and W3's sweeps) ──

def test_the_ci_workflow_holds_a_read_only_token_and_leaves_it_nowhere():
    """W1's sweep: ci.yml declared no permissions, so both jobs ran the
    repository's code and installed its development packages holding
    whatever the repository's default token grants; and its checkouts
    (sigil-pi in both jobs, nxrobins/sigil in forge) wrote that token into
    .git/config, where every later step, a test included, could read it.
    The release and drill workflows already hold read-only tokens and keep
    none."""
    top, jobs = _workflow(CI_YML.read_text())
    problems = []
    granted = _permissions(top.get("permissions"))
    if granted is None:
        problems.append("ci.yml declares no permissions, so its jobs hold whatever the "
                        "repository's default token grants")
    elif _writes(granted):
        problems.append(f"ci.yml's token can write: {_writes(granted)}")
    for name, job in jobs.items():
        if _writes(_permissions(job.get("permissions")) or {}):
            problems.append(f"ci.yml's {name} job can write: {job.get('permissions')}")
    checkouts = [(name, _with(step)) for name, job in jobs.items()
                 for step in job["steps"] if _action(step) == "actions/checkout"]
    assert len(checkouts) >= 3, checkouts
    kept = [(name, settings.get("repository", "this repository"))
            for name, settings in checkouts
            if settings.get("persist-credentials") != "false"]
    if kept:
        problems.append(f"these checkouts leave the token in .git/config: {kept}")
    assert not problems, "; ".join(problems)


BUILD_INPUT_FILES = ("Cargo.lock", "Cargo.toml", "rust-toolchain.toml",
                     "proofs/lean/lean-toolchain",
                     "proofs/lean/LambdaSigil/CombinedKernel.lean")


def _pin_check():
    """ci.sh step 1's check of the SIGIL checkout against SIGIL_REV, as ci.sh
    runs it: python3, from the repository root, with $SIGIL_ROOT."""
    match = re.search(r"python3 - \"\$SIGIL_ROOT\" <<'PY'[^\n]*\n(.*?)\nPY\n",
                      CI_SH.read_text(), re.S)
    assert match and "SIGIL_REV" in match.group(1), \
        "ci.sh step 1 no longer checks SIGIL against SIGIL_REV"
    return match.group(1)


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=gate", "-c", "user.email=gate@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True, timeout=60).stdout.strip()


def _pinned_checkouts(tmp_path):
    """A SIGIL checkout with every build input, committed, and a copy of the
    files step 1 reads from the repository root, with SIGIL_REV pinning that
    commit."""
    sigil = tmp_path / "SIGIL"
    for name, text in {
        "crates/sigil-mcp/src/lib.rs": "pub fn forge() {}\n",
        "stdlib/sigil/kv.sigil": "fn get() {}\n",
        "Cargo.lock": "version = 4\n\n[[package]]\nname = \"sigil-mcp\"\nversion = \"0.1.0\"\n",
        "Cargo.toml": "[workspace]\nmembers = [\"crates/*\"]\n",
        "rust-toolchain.toml": "[toolchain]\nchannel = \"1.98.0\"\n",
        "proofs/lean/lean-toolchain": "leanprover/lean4:v4.20.0\n",
        "proofs/lean/LambdaSigil/CombinedKernel.lean": "theorem kernel : True := trivial\n",
        "README.md": "a test checkout\n",
    }.items():
        (sigil / name).parent.mkdir(parents=True, exist_ok=True)
        (sigil / name).write_text(text)
    _git(sigil.parent, "init", "-q", str(sigil))
    _git(sigil, "add", "-A")
    _git(sigil, "commit", "-qm", "the pinned ref")
    gate = tmp_path / "sigil-pi"
    (gate / "scripts").mkdir(parents=True)
    shutil.copy(PI_ROOT / "toolchain.py", gate / "toolchain.py")
    shutil.copy(PI_ROOT / "scripts" / "build_release.py", gate / "scripts" / "build_release.py")
    (gate / "SIGIL_REV").write_text(
        f"crates = {_git(sigil, 'rev-parse', 'HEAD:crates')}\n"
        f"stdlib = {_git(sigil, 'rev-parse', 'HEAD:stdlib')}\n"
        "note = a test checkout\n"
        f"ref = {_git(sigil, 'rev-parse', 'HEAD')}\n")
    return gate, sigil


def _run_pin_check(gate, sigil):
    env = {key: value for key, value in os.environ.items()
           if key not in toolchain.OVERRIDE_ENV}
    return subprocess.run([sys.executable, "-c", _pin_check(), str(sigil)], cwd=gate,
                          env=env, capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("path", BUILD_INPUT_FILES)
def test_the_gate_builds_its_compiler_only_from_the_pinned_build_inputs(tmp_path, path):
    """W3's sweep: step 1 compared crates/ and stdlib/ with the pin, then
    rebuilt the compiler the suite tests from whatever Cargo.lock,
    Cargo.toml, rust-toolchain.toml and proofs/lean the checkout held. The
    release build refuses a SIGIL whose build inputs are not the pinned
    ref's; the gate now refuses the same ones, edited or committed."""
    gate, sigil = _pinned_checkouts(tmp_path)
    pinned = _run_pin_check(gate, sigil)
    assert pinned.returncode == 0, pinned.stdout + pinned.stderr
    held = "proofs/lean" if path.startswith("proofs/lean/") else path

    (sigil / path).write_text((sigil / path).read_text() + "# moved\n")
    edited = _run_pin_check(gate, sigil)
    assert edited.returncode != 0, (
        f"ci.sh step 1 accepted a SIGIL with {path} edited:\n{edited.stdout}")
    assert held in edited.stderr and "uncommitted" in edited.stderr, edited.stderr

    _git(sigil, "commit", "-qam", f"move {path}")
    moved = _run_pin_check(gate, sigil)
    assert moved.returncode != 0, (
        f"ci.sh step 1 accepted a SIGIL whose {path} is not the pinned ref's:\n{moved.stdout}")
    assert f"SIGIL {held} is not the pinned ref's" in moved.stderr, moved.stderr


def test_a_sigil_commit_that_touches_no_build_input_passes_the_pin_check(tmp_path):
    """The inputs are compared object by object, as SIGIL_REV pins trees: a
    later SIGIL commit that changes none of them is the same build."""
    gate, sigil = _pinned_checkouts(tmp_path)
    (sigil / "README.md").write_text("a later commit\n")
    _git(sigil, "commit", "-qam", "touch no build input")
    later = _run_pin_check(gate, sigil)
    assert later.returncode == 0, later.stdout + later.stderr
    assert "matches pin" in later.stdout, later.stdout
