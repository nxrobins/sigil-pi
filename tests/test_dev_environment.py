"""The gate's Python environment is pinned to exact files, and every test and every CI job has
a time limit (audit 2026-11-10, F-16 and F-17).

F-16: requirements-dev.lock pinned versions but not hashes, and CI upgraded pip to whatever
version the index served that day, so the repository did not fix the code that runs the gate
(nor, in the release job, the code that runs beside the files that are then packaged and
signed). F-17: no test had a time limit and no CI job had one, so a hung test held a runner
until GitHub's six-hour default and named nothing.

These guards read the files that define the environment and the jobs. Two run what they
guard: pytest, with this repository's pytest.ini, on a test that never returns, and ci.sh's
check that .venv holds the locked versions, on a lock that disagrees with it.
"""
import configparser
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path

PI_ROOT = Path(__file__).resolve().parent.parent
LOCK = PI_ROOT / "requirements-dev.lock"
WORKFLOWS = PI_ROOT / ".github" / "workflows"
# Where the gate's environment is built: ci.sh's header is the documented command for a local
# .venv, and the two CI jobs install the same way.
GATE_FILES = ("ci.sh", ".github/workflows/ci.yml")

# The per-test limit may not drop below this. The slowest test took 24.8 s on a healthy
# machine, and none of the 1,768 took 30 s (measured 2026-11-12 with --durations, coverage on,
# as ci.sh runs it). But on a loaded Mac a new executable can take more than 90 s to start,
# and some tests start several; a limit those starts could reach would fail tests for the
# machine's sake under a message that names the wrong cause.
MIN_TEST_TIMEOUT_S = 1200
# A job that runs the suite must outlast one test reaching the per-test limit by this much, so
# pytest ends and names the hung test before the runner kills the job and its report with it.
JOB_MARGIN_S = 600

# Defects in a file this change does not edit: the release workflow is being restructured
# separately (audit F-16), and until then it still upgrades pip from the index and sets no job
# limit. Each entry must still be a defect: when the workflow is fixed, the guard fails until
# the entry is deleted, so an exemption cannot outlive what it exempts.
KNOWN_GAPS = {
    ("release.yml", "installs-outside-the-lock"),
    ("release.yml", "job-without-time-limit"),
}

PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)((?:\s+--hash=sha256:[0-9a-f]{64})+)")


def _normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _logical_lines(path):
    """A requirements file's lines as pip reads them: continuations joined, comments dropped."""
    for line in path.read_text().replace("\\\n", " ").splitlines():
        line = re.sub(r"(^|\s)#.*", "", line).strip()
        if line:
            yield line


def _lock(path=LOCK):
    """(options, requirements) of a requirements file and of the files it includes with -r."""
    options, requirements = [], []
    for line in _logical_lines(path):
        include = re.fullmatch(r"(?:-r|--requirement)[\s=]+(\S+)", line)
        if include:
            more_options, more_requirements = _lock(path.parent / include.group(1))
            options += more_options
            requirements += more_requirements
        elif line.startswith("-"):
            options.append(line)
        else:
            requirements.append(line)
    return options, requirements


def _pins():
    """{normalized name: version} of every requirement the lock pins with hashes."""
    return {_normalize(m.group(1)): m.group(2)
            for m in map(PIN.fullmatch, _lock()[1]) if m}


def _pip_installs(text):
    """Every pip install command in a script or workflow. Comment lines count: ci.sh's header
    is the documented way to build the gate's .venv."""
    return re.findall(r"\bpip3? install\b[^\n]*", text)


def _jobs(text):
    """{job id: job body} of a workflow, read by indentation: the ids are the two-space keys
    under the top-level `jobs:`."""
    jobs, current, inside = {}, None, False
    for line in text.splitlines():
        if line.startswith("jobs:"):
            inside = True
            continue
        if not inside or not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            break
        key = re.fullmatch(r"  ([A-Za-z0-9_-]+):\s*", line)
        if key:
            current = key.group(1)
            jobs[current] = []
        elif current:
            jobs[current].append(line)
    return {job: "\n".join(lines) for job, lines in jobs.items()}


def _job_minutes(body):
    """The job-level timeout-minutes (four spaces in; a step's would be deeper), or None."""
    match = re.search(r"^    timeout-minutes:\s*(\d+)\s*$", body, re.M)
    return int(match.group(1)) if match else None


def _pytest_ini():
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(PI_ROOT / "pytest.ini")
    return parser["pytest"]


def _assert_only_known_gaps(kind, found):
    """found: {workflow file: what is wrong in it}. Only the known gaps of this kind may be
    there, and each of those must still be there."""
    known = {name for name, gap in KNOWN_GAPS if gap == kind}
    unexpected = {name: what for name, what in found.items() if name not in known}
    assert not unexpected, f"{kind}: {unexpected}"
    closed = sorted(known - set(found))
    assert not closed, (
        f"{closed} no longer has the known defect {kind!r}: delete its KNOWN_GAPS entry so "
        f"this guard covers the file again")


def test_every_locked_requirement_is_pinned_to_exact_files():
    """Each requirement names one version and the sha256 of its files. A version alone lets
    the index serve other bytes under the same number."""
    requirements = _lock()[1]
    assert requirements, "requirements-dev.lock lists no requirements"
    loose = [requirement.split()[0] for requirement in requirements
             if not PIN.fullmatch(requirement)]
    assert not loose, (
        f"requirements-dev.lock pins {loose} without a sha256 for their files, so pip "
        f"installs whatever the index serves under those versions")


def test_any_install_of_the_lock_checks_hashes_and_builds_nothing():
    """The lock enforces its own hashes. --require-hashes in the file holds for every install
    of it, whatever command runs it, and refuses a dependency missing from the list as well as
    one without a hash. --only-binary refuses source builds, whose build backends nothing
    pins."""
    options = _lock()[0]
    assert "--require-hashes" in options, (
        "requirements-dev.lock does not require hashes itself: an install that leaves out "
        "--require-hashes would accept a requirement without one")
    assert any(re.fullmatch(r"--only-binary[\s=]+:all:", option) for option in options), (
        "requirements-dev.lock allows building from source, which fetches and runs a build "
        "backend that nothing pins")


def test_the_gate_installs_only_the_lock_and_checks_its_hashes():
    """ci.sh's header and both CI jobs install the lock with --require-hashes, and nothing
    else: the forge job used to upgrade pip to whatever the index served."""
    problems = []
    for name in GATE_FILES:
        installs = _pip_installs((PI_ROOT / name).read_text())
        if not installs:
            problems.append(f"{name} no longer shows how the gate's environment is installed")
        for command in installs:
            if "-r requirements-dev.lock" not in command:
                problems.append(f"{name} installs from outside the lock: {command!r}")
            elif "--require-hashes" not in command:
                problems.append(f"{name} installs the lock without --require-hashes: {command!r}")
    assert not problems, "\n".join(problems)


def test_no_workflow_installs_python_code_outside_the_lock():
    """Every workflow, including any added later: pip may install the lock, which checks its
    own hashes, and nothing else. Upgrading pip or installing a package by name runs whatever
    the index serves that day."""
    found = {}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        outside = [command for command in _pip_installs(path.read_text())
                   if "-r requirements-dev.lock" not in command]
        if outside:
            found[path.name] = outside
    _assert_only_known_gaps("installs-outside-the-lock", found)


def test_every_ci_job_has_a_time_limit():
    """GitHub's default job limit is six hours, so a hung step held a runner that long. Every
    job of every workflow sets timeout-minutes below it."""
    found = {}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        jobs = _jobs(path.read_text())
        assert jobs, f"{path.name}: found no jobs to check"
        unbounded = sorted(job for job, body in jobs.items()
                           if not 0 < (_job_minutes(body) or 0) < 360)
        if unbounded:
            found[path.name] = unbounded
    assert {"standalone", "forge"} <= set(_jobs((WORKFLOWS / "ci.yml").read_text())), \
        "the job reader no longer finds ci.yml's two jobs"
    _assert_only_known_gaps("job-without-time-limit", found)


def test_a_job_outlasts_a_test_reaching_its_limit():
    """The job limit backs up the per-test one and must not pre-empt it, or a hung test dies
    with the job and the run reports nothing. Each job that runs the suite leaves room for one
    test to reach the per-test limit, with JOB_MARGIN_S to spare."""
    ini = _pytest_ini()
    assert "timeout" in ini, "pytest.ini sets no per-test limit for the jobs to outlast"
    limit = float(ini["timeout"])
    checked = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for job, body in _jobs(path.read_text()).items():
            minutes = _job_minutes(body)
            if minutes is None or not re.search(r"\bpytest\b|\./ci\.sh", body):
                continue
            checked.append(f"{path.name}:{job}")
            assert minutes * 60 >= limit + JOB_MARGIN_S, (
                f"{path.name}:{job} stops after {minutes} min, less than {JOB_MARGIN_S} s "
                f"beyond the {limit:g}-second per-test limit")
    assert {"ci.yml:standalone", "ci.yml:forge"} <= set(checked), checked


def test_every_test_has_a_time_limit():
    """pytest.ini gives every test a limit, by the plugin the lock pins, which pytest must
    load: a venv without it would otherwise ignore the setting with a warning."""
    ini = _pytest_ini()
    assert "timeout" in ini, (
        "pytest.ini sets no per-test limit: a hung test holds the run until the CI job is "
        "killed, and nothing names it")
    limit = float(ini["timeout"])
    assert limit >= MIN_TEST_TIMEOUT_S, (
        f"the per-test limit ({limit:g} s) is below {MIN_TEST_TIMEOUT_S} s, close enough to "
        f"the slowest tests that a slow-starting machine would trip it")
    # signal interrupts the test itself, so it fails, the run goes on and the report names
    # it; the thread method ends the whole process, report and coverage included.
    assert ini.get("timeout_method", "").strip() == "signal", \
        "pytest.ini must keep timeout_method = signal"
    required = [_normalize(re.split(r"[<>=!~;\s]", plugin)[0])
                for plugin in ini.get("required_plugins", "").split()]
    assert "pytest-timeout" in required, (
        "pytest.ini must list pytest-timeout in required_plugins; without the plugin the "
        "timeout setting is ignored with a warning")
    assert "pytest-timeout" in _pins(), "requirements-dev.lock must pin pytest-timeout"


def test_nothing_in_the_gate_turns_the_limit_off():
    """A test may raise its own limit with @pytest.mark.timeout(N) but not remove it, and the
    gate's pytest commands may not override or unload it."""
    disabled = []
    for path in sorted((PI_ROOT / "tests").glob("*.py")):
        for marker in re.findall(r"^\s*@pytest\.mark\.timeout\(([^)]*)\)", path.read_text(),
                                 re.M):
            first = marker.split(",")[0].strip()
            if not re.fullmatch(r"(timeout\s*=\s*)?[0-9.]+", first) or float(
                    first.split("=")[-1]) <= 0:
                disabled.append(f"{path.name}: timeout({marker})")
    for name in GATE_FILES:
        for line in (PI_ROOT / name).read_text().splitlines():
            if "pytest" in line and re.search(r"--timeout|no:timeout|-o\s*timeout", line):
                disabled.append(f"{name}: {line.strip()}")
    assert not disabled, f"the per-test limit is turned off or lowered: {disabled}"


def test_a_test_that_never_returns_is_ended_and_named(tmp_path):
    """The limit is in force, not only written down: pytest run with this repository's
    pytest.ini, its limit lowered to 2 s with -o, ends a test that would sleep for 20 s, fails
    it and names it, and goes on to report."""
    hung = tmp_path / "test_hangs.py"
    hung.write_text("import time\n\n\ndef test_never_returns():\n    time.sleep(20)\n")
    started = time.monotonic()
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(PI_ROOT / "pytest.ini"),
         "--rootdir", str(tmp_path), "-p", "no:cacheprovider", "-o", "timeout=2", str(hung)],
        capture_output=True, text=True, cwd=tmp_path, timeout=300)
    elapsed = time.monotonic() - started
    output = run.stdout + run.stderr
    assert "Timeout (>2.0s) from pytest-timeout" in output, (
        f"a test that never returns was not stopped (exit {run.returncode} after "
        f"{elapsed:.0f} s):\n{output[-3000:]}")
    assert run.returncode == 1 and "1 failed" in run.stdout, output[-3000:]
    assert "test_never_returns" in run.stdout, "the report does not name the hung test"


def test_the_test_extra_installs_what_the_lock_pins():
    """pyproject.toml's `test` extra is the lock's direct requirements for whoever installs
    the package instead: it names the same versions, and pytest-timeout, which pytest.ini
    requires."""
    extra = tomllib.loads((PI_ROOT / "pyproject.toml").read_text())[
        "project"]["optional-dependencies"]["test"]
    pins = _pins()
    drift = [f"{spec} (lock: {pins.get(_normalize(spec.partition('==')[0]))})"
             for spec in extra
             if pins.get(_normalize(spec.partition("==")[0])) != spec.partition("==")[2]]
    assert not drift, f"pyproject.toml's test extra disagrees with requirements-dev.lock: {drift}"
    assert "pytest-timeout" in {_normalize(spec.partition("==")[0]) for spec in extra}, \
        "pyproject.toml's test extra lacks pytest-timeout, which pytest.ini requires"


def _venv_check():
    """ci.sh's check that .venv holds exactly the locked versions, as ci.sh runs it."""
    source = (PI_ROOT / "ci.sh").read_text()
    match = re.search(r"\.venv/bin/python - <<'PY'[^\n]*\n(.*?)\nPY\n", source, re.S)
    assert match and "requirements-dev.lock" in match.group(1), \
        "ci.sh no longer checks that .venv holds the locked versions"
    return match.group(1)


def test_the_gate_refuses_a_venv_that_is_not_the_lock(tmp_path):
    """ci.sh installs nothing; it runs on the .venv its header builds. It must run on the
    locked tools or not at all, and say which package differs: a .venv built from an older
    lock, or by hand, would run the gate with other versions."""
    check = _venv_check()
    (tmp_path / "requirements-dev.lock").write_text(LOCK.read_text())
    (tmp_path / "requirements-runtime.lock").write_text(
        (PI_ROOT / "requirements-runtime.lock").read_text())
    same = subprocess.run([sys.executable, "-c", check], cwd=tmp_path,
                          capture_output=True, text=True, timeout=120)
    assert same.returncode == 0, f"the gate's own .venv fails the check:\n{same.stderr}"
    pinned = _pins()["pytest-timeout"]
    (tmp_path / "requirements-dev.lock").write_text(
        LOCK.read_text().replace(f"pytest-timeout=={pinned}", "pytest-timeout==0.0.1"))
    other = subprocess.run([sys.executable, "-c", check], cwd=tmp_path,
                           capture_output=True, text=True, timeout=120)
    assert other.returncode != 0, "a .venv that differs from the lock passes the check"
    assert "pytest-timeout" in other.stderr and "0.0.1" in other.stderr, other.stderr
