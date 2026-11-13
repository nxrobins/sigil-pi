"""The drill workflows cannot be steered by their inputs, and keep what they find (audit F-18).

`recovery-drill.yml` wrote its two dispatch inputs straight into a shell
script, where `failure-drill.yml` passed its input through the environment: a
tag such as `v0.3.0"; curl ... | sh; echo "` ran as part of the job. Its
upload step had no `if: always()`, so a recovery drill that failed kept
nothing. It declared no permissions, so its token had whatever the repository
default grants, and both workflows left that token in the checkout's git
configuration for every later step, which is where a drill starts releases.

Each workflow also signs its report (audit F-19), in a job of its own that
holds the only token able to sign and runs nothing of the repository's, so
the readiness gate can tell a report the drill produced from one typed by hand.

A workflow cannot be run here, so these tests read the files, and run the
script of the step that takes the inputs, in bash, with `gh` replaced by a
shell function that records how it was called. The signing job's scripts are
run the same way.
"""

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import PI_ROOT

WORKFLOWS = PI_ROOT / ".github" / "workflows"
DRILLS = ("failure-drill.yml", "recovery-drill.yml")
# What a drill's token may do. A drill downloads a release and uploads a run
# artifact; the first needs to read contents and the second needs nothing.
# This is the workflow's token, and the drill job's: the job that runs the
# drill and every release it starts declares nothing of its own.
PERMISSIONS = {
    "failure-drill.yml": {"contents": "read"},
    "recovery-drill.yml": {"contents": "read"},
}
# What the job that signs the report may do, and only that job (audit F-19):
# mint this run's OIDC identity for a Sigstore signing certificate
# (`id-token`), and store the attestation in the repository (`attestations`).
# Not `artifact-metadata`: actions/attest needs it only for a storage record,
# which it makes only with `push-to-registry`. Not `contents`: the job checks
# nothing out. A token that can sign as this workflow is held by a job that
# runs nothing of the repository's.
ATTEST_PERMISSIONS = {"id-token": "write", "attestations": "write"}
# The report each drill workflow signs, which the readiness gate reads under
# the same name in docs/evidence/.
REPORTS = {"failure-drill.yml": "failure-injection.json",
           "recovery-drill.yml": "recovery-drill.json"}
# The script each workflow runs its drill with.
DRILL_SCRIPTS = {"failure-drill.yml": "scripts/failure_drill.py",
                 "recovery-drill.yml": "scripts/release_drill.py"}
# Contexts whose text is chosen by whoever dispatches a workflow, opens a pull
# request or writes a commit. The runner pastes an expression into a script
# before the shell reads it, so one of these in a `run:` block is that
# person's shell command.
STEERABLE = re.compile(
    r"\$\{\{[^}]*\b(?:inputs|github\.head_ref|github\.event\.(?:inputs|pull_request|issue|"
    r"comment|review|review_comment|head_commit|commits|pages|discussion|workflow_run))\b")
HOSTILE = [
    'v0.3.0"; touch injected; echo "',
    "v0.3.0$(touch injected)",
    "v0.3.0`touch injected`",
    "v0.3.0; touch injected",
    "--repo=someone-else/sigil-pi",
    "-R someone-else/sigil-pi v0.3.0",
    "v0.3.0\n::add-mask::x",
    "v0.3.0 v0.4.0",
    "../v0.3.0",
    "main",
    "",
]


def _text(workflow):
    return (WORKFLOWS / workflow).read_text()


def _indent(line):
    return len(line) - len(line.lstrip())


def _steps(text):
    """Every step of a workflow, each as its own text."""
    steps, current, column = [], None, None
    for line in text.splitlines():
        start = re.match(r"^(\s*)- (?:name|uses|run|id|if|env|with):", line)
        if start and column in (None, len(start.group(1))):
            column, current = len(start.group(1)), [line]
            steps.append(current)
        elif current is not None:
            if line.strip() and not line.lstrip().startswith("#") and _indent(line) <= column:
                current = None  # the steps list ended
            else:
                current.append(line)
    return ["\n".join(step) for step in steps]


def _block(text, key):
    """The lines under `key:` in a step or a workflow, without their indentation."""
    lines = text.splitlines()
    for number, line in enumerate(lines):
        start = re.match(rf"^(\s*(?:- )?){re.escape(key)}:\s*(.*)$", line)
        if not start:
            continue
        column, inline = len(start.group(1)), start.group(2)
        if inline and inline[0] not in "|>":
            return [inline]
        body = []
        for following in lines[number + 1:]:
            if following.strip() and _indent(following) <= column:
                break
            body.append(following)
        depth = min((_indent(entry) for entry in body if entry.strip()), default=0)
        return [entry[depth:] for entry in body]
    return None


def _scripts(text):
    """The script of every `run:` step."""
    return ["\n".join(script) for script in
            (_block(step, "run") for step in _steps(text)) if script is not None]


def _mapping(lines):
    return dict(re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line).groups()
                for line in lines or () if line.strip() and not line.startswith("#"))


def _jobs(text):
    """Each job of a workflow, by name and in order, as its own text."""
    jobs, current = {}, None
    for line in text.split("\njobs:\n", 1)[1].splitlines():
        if line.strip() and not line.startswith((" ", "#")):
            break  # a top-level key after the jobs
        head = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if head:
            current = jobs[head.group(1)] = []
        elif current is not None:
            current.append(line)
    return {name: "\n".join(body) for name, body in jobs.items()}


def _job_keys(job):
    """A job's own keys and their inline values."""
    return _mapping(line[4:] for line in job.splitlines() if re.match(r"^    [A-Za-z0-9_-]+:", line))


def _step_keys(step):
    """A step's own keys and their inline values."""
    lines = step.splitlines()
    column = len(re.match(r"^(\s*)- ", lines[0]).group(1)) + 2
    own = [lines[0][column:]] + [line[column:] for line in lines[1:]
                                 if line.strip() and _indent(line) == column]
    return _mapping(own)


def _download_step(workflow):
    [step] = [step for step in _steps(_text(workflow)) if "gh release download" in step]
    return step


def _attest_step(workflow, marker):
    [step] = [step for step in _steps(_jobs(_text(workflow))["attest"]) if marker in step]
    return step


def _run_step(step, cwd, environment):
    """A step's script, run in bash in ``cwd`` with ``environment`` only."""
    return subprocess.run(
        ["bash", "-c", "\n".join(_block(step, "run"))], cwd=cwd, capture_output=True,
        text=True, timeout=120, env={"PATH": os.environ["PATH"], **environment})


def _run_download(tmp_path, workflow, inputs):
    """Run the step that takes the dispatch inputs the way the runner does:
    render each expression into the step's environment and into its script,
    then give the script to bash. `gh` is a function that records its call."""
    def rendered(text):
        text = re.sub(r"\$\{\{\s*inputs\.(\w+)\s*\}\}", lambda m: inputs[m.group(1)], text)
        return re.sub(r"\$\{\{\s*github\.(\w+)\s*\}\}", r"github-\1", text)

    step = _download_step(workflow)
    environment = {name: rendered(value) for name, value in _mapping(_block(step, "env")).items()}
    script = ('gh() { printf "%s\\n" "$*" >> gh-calls; }\n'
              + rendered("\n".join(_block(step, "run"))))
    for directory in ("old", "new", "candidate"):
        (tmp_path / directory).mkdir()
    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=120,
        env={"PATH": os.environ["PATH"], **environment})
    calls = tmp_path / "gh-calls"
    return result, calls.read_text().splitlines() if calls.exists() else []


def test_the_workflows_can_be_read_step_by_step():
    """Anti-vacuity for every test below: the reader finds the steps, the
    scripts and the environments that are there."""
    for workflow, drill in zip(DRILLS, ("scripts/failure_drill.py", "scripts/release_drill.py")):
        text = _text(workflow)
        steps, scripts = _steps(text), _scripts(text)
        assert len(steps) >= 7 and len(scripts) >= 4, (workflow, len(steps), len(scripts))
        assert sum("uses: " in step for step in steps) == len(steps) - len(scripts)
        assert sum(text.count(marker) for marker in ("run: |", "run: >")) == len(scripts)
        assert any(drill in script for script in scripts)
        assert all(script.startswith("set -euo pipefail") for script in scripts), workflow
        assert "GH_TOKEN" in _mapping(_block(_download_step(workflow), "env"))


@pytest.mark.parametrize("workflow", DRILLS)
def test_a_drill_workflow_writes_no_expression_into_a_shell_script(workflow):
    for script in _scripts(_text(workflow)):
        written = re.findall(r"\$\{\{[^}]*\}\}", script)
        assert not written, (
            f"{workflow} writes {written} into a shell script; pass it through "
            f"the step's `env:` and use the variable")


def test_no_workflow_writes_text_its_trigger_chose_into_a_shell_script():
    """The class guard, over every workflow: a dispatch input, a branch name
    or a field of the triggering event reaches a shell only as a variable."""
    workflows = sorted(WORKFLOWS.glob("*.yml"))
    assert len(workflows) >= 4
    for path in workflows:
        for script in _scripts(path.read_text()):
            assert not STEERABLE.search(script), (
                f"{path.name} writes {STEERABLE.search(script).group(0)}...}}}} into a shell "
                f"script: whoever sets it chooses a command the job runs")


@pytest.mark.parametrize("workflow,variables", [
    ("recovery-drill.yml", {"OLD_TAG": "old_tag", "NEW_TAG": "new_tag"}),
    ("failure-drill.yml", {"CANDIDATE_TAG": "tag"}),
])
@pytest.mark.parametrize("hostile", HOSTILE)
def test_a_tag_that_is_not_a_release_tag_reaches_neither_the_shell_nor_gh(
        tmp_path, workflow, variables, hostile):
    for name in variables.values():
        inputs = {other: "v0.4.0" for other in variables.values()}
        inputs[name] = hostile
        where = tmp_path / name
        where.mkdir()
        result, calls = _run_download(where, workflow, inputs)
        assert not (where / "injected").exists(), (
            f"{workflow}: the dispatch input {name}={hostile!r} ran as a shell command")
        assert calls == [], f"{workflow}: gh was called for {name}={hostile!r}: {calls}"
        assert result.returncode != 0
        assert "not a release tag" in result.stderr
        assert "::" not in result.stdout + result.stderr


def test_release_tags_are_downloaded_as_given(tmp_path):
    for workflow, inputs, expected in (
            ("recovery-drill.yml", {"old_tag": "v0.3.0", "new_tag": "v0.5.0-rc.1"},
             ["release download v0.3.0 --pattern *.tar.gz -D old",
              "release download v0.5.0-rc.1 --pattern *.tar.gz -D new"]),
            ("failure-drill.yml", {"tag": "v0.4.0", "bounded_filesystem_mb": "64"},
             ["release download v0.4.0 --pattern *.tar.gz -D candidate"])):
        where = tmp_path / workflow
        where.mkdir()
        result, calls = _run_download(where, workflow, inputs)
        assert result.returncode == 0, result.stderr
        assert calls == expected
        environment = _mapping(_block(_download_step(workflow), "env"))
        # gh is told the repository, so it does not depend on the checkout's
        # git configuration, which no longer holds a credential.
        assert environment["GH_REPO"] == "${{ github.repository }}"


@pytest.mark.parametrize("workflow", DRILLS)
def test_a_drill_workflows_token_is_read_only_and_is_not_left_on_disk(workflow):
    text = _text(workflow)
    assert _mapping(_block(text, "permissions")) == PERMISSIONS[workflow], (
        f"{workflow} must declare exactly {PERMISSIONS[workflow]} for its token: "
        f"undeclared, it has whatever the repository's default grants")
    declared = {name: _mapping(_block(job, "permissions"))
                for name, job in _jobs(text).items() if _block(job, "permissions") is not None}
    assert declared == {"attest": ATTEST_PERMISSIONS}, (
        f"{workflow}: its jobs declare {declared}. The drill job, which runs the drill "
        f"and the releases, keeps the workflow's read-only token; only the job that "
        f"signs the report holds more, and only {ATTEST_PERMISSIONS}")
    assert len(re.findall(r"^\s*permissions:", text, re.M)) == 2, (
        f"{workflow}: a job widens the workflow's permissions")
    checkouts = [step for step in _steps(text) if "uses: actions/checkout@" in step]
    assert checkouts
    for step in checkouts:
        assert _mapping(_block(step, "with")).get("persist-credentials") == "false", (
            f"{workflow}: actions/checkout leaves the job's token in .git/config, "
            f"where every later step, and every release the drill starts, can read it")
    credential = r"\$\{\{[^}]*\b(?:secrets\.|github\.token\b)"
    given = [step for step in _steps(text) if re.search(credential, step)]
    assert len(re.findall(credential, text)) == len(given) == 2, (
        f"{workflow}: a credential is given in more places than the two steps "
        f"that read from GitHub")
    assert given == [_download_step(workflow), _attest_step(workflow, "gh attestation verify")], (
        f"{workflow}: the token is given to a step other than the one that "
        f"downloads the release and the one that reads the report's attestation back")
    for step in given:
        assert re.search(credential, "\n".join(_block(step, "env"))), (
            f"{workflow}: the token is given to a job, not to the one step that uses it")


@pytest.mark.parametrize("workflow,kept", [
    ("failure-drill.yml", ["failure-injection.json", "drill-work/**/*-service.log"]),
    ("recovery-drill.yml", ["recovery-drill.json", "fixture-evidence.json",
                            "fixture-work/service.log", "drill-work/logs/*-service.log"]),
])
def test_a_drill_workflow_keeps_its_report_and_logs_when_the_drill_fails(workflow, kept):
    uploads = [step for step in _steps(_text(workflow)) if "uses: actions/upload-artifact@" in step]
    assert len(uploads) == 1, f"{workflow} uploads {len(uploads)} artifacts"
    # The steps of the job that runs the drill, where the upload is.
    steps = _steps(_jobs(_text(workflow))["drill"])
    [upload] = [step for step in steps if "uses: actions/upload-artifact@" in step]
    assert _block(upload, "if") == ["always()"], (
        f"{workflow}: the upload is skipped when an earlier step fails, so a failed "
        f"drill keeps nothing")
    settings = _block(upload, "with")
    assert [line.strip() for line in _block("\n".join(settings), "path")] == kept
    assert _mapping(line for line in settings if not line.startswith(" "))[
        "if-no-files-found"] == "error"
    # The last step of the job that runs the drill. The signing job comes
    # after that job and needs it, so it never stands between a failed drill
    # and the upload of what it kept.
    assert steps[-1] == upload, f"{workflow}: a step after the upload can be skipped"


def test_the_recovery_workflow_makes_its_key_private_before_writing_it():
    [fixture] = [script for script in _scripts(_text("recovery-drill.yml"))
                 if "scripts/drill_fixture.py" in script]
    assert fixture.index("umask 077") < fixture.index("> audit.key")


# ── the signing job (audit F-19) ──

@pytest.mark.parametrize("workflow", DRILLS)
def test_each_drill_workflow_signs_its_report_in_a_job_of_its_own(workflow):
    """The run that produced the report signs it, so the readiness gate can
    tell the report from one typed by hand or edited after the run. The job
    that signs needs the drill job and runs only when the drill passed, the
    only report the gate accepts; it takes the drill job's artifact, checks
    the report is the one the drill wrote, signs that one file as SLSA
    provenance, and reads the signature back as the gate will."""
    text = _text(workflow)
    jobs = _jobs(text)
    assert list(jobs) == ["drill", "attest"], f"{workflow} signs nothing: its jobs are {list(jobs)}"
    keys = _job_keys(jobs["attest"])
    assert keys.get("needs") == "drill" and "if" not in keys, keys
    assert keys.get("runs-on") == "ubuntu-24.04" and int(keys.get("timeout-minutes", 0)) <= 15, keys
    steps = _steps(jobs["attest"])
    uses = [_step_keys(step).get("uses", "").split(" #")[0] for step in steps]
    assert [use.split("@")[0] for use in uses] == [
        "actions/download-artifact", "", "actions/attest", ""], (
        f"{workflow}: the signing job must take the artifact, check the report, sign it "
        f"and verify the signature, in that order: it runs {uses}")
    release = (WORKFLOWS / "release.yml").read_text()
    for use in filter(None, uses):
        assert f"uses: {use} " in release, (
            f"{workflow} signs with {use}, not the commit release.yml signs with")
    download, check, sign, verify = steps
    assert _mapping(_block(download, "with")) == {"path": "evidence", "merge-multiple": "true"}
    assert "sha256sum --check --strict" in check
    # A literal path and no predicate: one subject, the report, and the
    # statement is SLSA build provenance, the type the gate asks for.
    assert _mapping(_block(sign, "with")) == {"subject-path": f"evidence/{REPORTS[workflow]}"}
    assert "gh attestation verify" in verify
    drill = jobs["drill"]
    assert _mapping(_block(drill, "outputs")) == {
        "report_sha256": "${{ steps.report.outputs.sha256 }}"}
    drill_steps = _steps(drill)
    [record] = [step for step in drill_steps if _step_keys(step).get("id") == "report"]
    [runs_drill] = [step for step in drill_steps if DRILL_SCRIPTS[workflow] in step]
    assert drill_steps.index(runs_drill) < drill_steps.index(record) == len(drill_steps) - 2, (
        f"{workflow}: the report's digest is recorded after the drill and before the upload")


@pytest.mark.parametrize("workflow", DRILLS)
def test_the_signing_job_runs_nothing_of_the_repository(workflow):
    """Its token can sign as this workflow. Nothing it runs comes from the
    repository or from what the drill started: no checkout, no interpreter,
    and its scripts call only sha256sum and gh."""
    attest = _jobs(_text(workflow))["attest"]
    assert not any(action in attest for action in ("actions/checkout", "actions/setup-python")), (
        f"{workflow}: the signing job checks out or installs something")
    scripts = _scripts(attest)
    assert len(scripts) == 2
    for script in scripts:
        assert "scripts/" not in script and "./" not in script, (
            f"{workflow}: the signing job runs a file")
        for word in ("python", "python3", "pip", "node", "npm", "source", "bash", "sh",
                     "eval", "exec", "curl", "wget", "chmod", "make", "cargo"):
            assert not re.search(rf"(?<![\w-]){word}(?![\w-])", script), (
                f"{workflow}: the signing job runs {word!r}")


@pytest.mark.parametrize("workflow", DRILLS)
def test_the_signing_job_signs_the_bytes_the_drill_wrote(tmp_path, workflow):
    """The drill job records the report's digest after the drill; the signing
    job checks the report it took from the artifact against it before
    signing, and refuses other bytes, no report, and a digest that is not
    one, whatever the digest says."""
    report = REPORTS[workflow]
    jobs = _jobs(_text(workflow))
    [record] = [step for step in _steps(jobs["drill"]) if _step_keys(step).get("id") == "report"]
    check = _attest_step(workflow, "sha256sum --check")
    assert _mapping(_block(check, "env")) == {
        "REPORT_SHA256": "${{ needs.drill.outputs.report_sha256 }}"}
    written = tmp_path / "drill"
    written.mkdir()
    (written / report).write_text('{"written": "by the drill"}')
    output = tmp_path / "github-output"
    recorded = _run_step(record, written, {"GITHUB_OUTPUT": str(output)})
    assert recorded.returncode == 0, recorded.stderr
    digest = hashlib.sha256((written / report).read_bytes()).hexdigest()
    assert output.read_text().splitlines() == [f"sha256={digest}"]

    signing = tmp_path / "attest"
    (signing / "evidence").mkdir(parents=True)
    taken = signing / "evidence" / report
    taken.write_bytes((written / report).read_bytes())
    same = _run_step(check, signing, {"REPORT_SHA256": digest})
    assert same.returncode == 0, same.stderr
    taken.write_bytes(taken.read_bytes() + b" ")
    assert _run_step(check, signing, {"REPORT_SHA256": digest}).returncode != 0
    taken.unlink()
    assert _run_step(check, signing, {"REPORT_SHA256": digest}).returncode != 0
    taken.write_bytes((written / report).read_bytes())
    for hostile in ("", "0" * 63, "0" * 65, digest.upper(), f"{digest}\n::add-mask::x",
                    f"{digest}  evidence/other.json", '"; touch injected; echo "',
                    "$(touch injected)", f"{digest}`touch injected`", *HOSTILE):
        refused = _run_step(check, signing, {"REPORT_SHA256": hostile})
        assert refused.returncode != 0, hostile
        assert "reported no digest" in refused.stderr, (hostile, refused.stderr)
        assert not (signing / "injected").exists(), hostile
        assert "::" not in refused.stdout + refused.stderr, hostile


@pytest.mark.parametrize("workflow", DRILLS)
def test_the_signing_job_reads_the_signature_back_as_the_readiness_gate_does(tmp_path, workflow):
    """The job's last step runs the readiness gate's own command for this
    report, so a run whose signature the gate would refuse (one dispatched
    from another branch, say) fails here, where the maintainer sees it."""
    from scripts.check_readiness_evidence import ATTESTED_REPORTS, attestation_command
    report = REPORTS[workflow]
    assert ATTESTED_REPORTS[report] == f".github/workflows/{workflow}"
    verify = _attest_step(workflow, "gh attestation verify")
    assert _mapping(_block(verify, "env")) == {"GH_TOKEN": "${{ github.token }}"}
    script = 'gh() { printf "%s\\n" "$@" > gh-call; }\n' + "\n".join(_block(verify, "run"))
    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=120,
        env={"PATH": os.environ["PATH"], "GITHUB_REPOSITORY": "nxrobins/sigil-pi",
             "GH_TOKEN": "a token"})
    assert result.returncode == 0, result.stderr
    called = ["gh", *(tmp_path / "gh-call").read_text().splitlines()]
    gate = attestation_command(Path("evidence") / report, ATTESTED_REPORTS[report])
    assert gate[-2:] == ["--format", "json"]
    assert called == gate[:-2], f"{workflow} verifies {called}; the gate runs {gate}"


def test_the_recovery_workflow_prints_what_its_report_shows(tmp_path):
    """The job log is where the maintainer first reads the run, so the drill
    step prints the report, the online backup included (H5's patch), and
    does not fail on a report as the drill writes it."""
    from test_readiness_evidence import _fixture
    [step] = [step for step in _steps(_jobs(_text("recovery-drill.yml"))["drill"])
              if DRILL_SCRIPTS["recovery-drill.yml"] in step]
    lines = _block(step, "run")
    start = lines.index("python - <<'PY'")
    program = "\n".join(lines[start + 1:lines.index("PY", start)])
    evidence, _, _ = _fixture(tmp_path)
    result = subprocess.run([sys.executable, "-c", program], cwd=evidence,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    printed = result.stdout.splitlines()
    assert ("online backup: online taken by 1.0.0 age at restore: 1.5 s, longest session "
            "lock hold: 1.8 ms, restore to first turn: 0.5 s") in printed, printed
    for name, before in (("backup_online_new", 8), ("restore_online_new", 12)):
        assert (f"  {name} (1.0.0): read {before} message(s), sent the provider {before} "
                f"of them, holds {before + 2} after the turn") in printed, printed
