"""The drill workflows cannot be steered by their inputs, and keep what they find (audit F-18).

`recovery-drill.yml` wrote its two dispatch inputs straight into a shell
script, where `failure-drill.yml` passed its input through the environment: a
tag such as `v0.3.0"; curl ... | sh; echo "` ran as part of the job. Its
upload step had no `if: always()`, so a recovery drill that failed kept
nothing. It declared no permissions, so its token had whatever the repository
default grants, and both workflows left that token in the checkout's git
configuration for every later step, which is where a drill starts releases.

A workflow cannot be run here, so these tests read the files, and run the
script of the step that takes the inputs, in bash, with `gh` replaced by a
shell function that records how it was called.
"""

import os
import re
import subprocess

import pytest

from conftest import PI_ROOT

WORKFLOWS = PI_ROOT / ".github" / "workflows"
DRILLS = ("failure-drill.yml", "recovery-drill.yml")
# What a drill's token may do. A drill downloads a release and uploads a run
# artifact; the first needs to read contents and the second needs nothing.
# A workflow that comes to need more (an attestation needs `id-token` and
# `attestations`) adds it here, with the reason, in the same change.
PERMISSIONS = {
    "failure-drill.yml": {"contents": "read"},
    "recovery-drill.yml": {"contents": "read"},
}
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


def _download_step(workflow):
    [step] = [step for step in _steps(_text(workflow)) if "gh release download" in step]
    return step


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
    assert len(re.findall(r"^\s*permissions:", text, re.M)) == 1, (
        f"{workflow}: a job widens the workflow's permissions")
    checkouts = [step for step in _steps(text) if "uses: actions/checkout@" in step]
    assert checkouts
    for step in checkouts:
        assert _mapping(_block(step, "with")).get("persist-credentials") == "false", (
            f"{workflow}: actions/checkout leaves the job's token in .git/config, "
            f"where every later step, and every release the drill starts, can read it")
    credential = r"\$\{\{[^}]*\b(?:secrets\.|github\.token\b)"
    assert len(re.findall(credential, text)) == 1, (
        f"{workflow}: more than one place is given a credential")
    assert re.search(credential, "\n".join(_block(_download_step(workflow), "env"))), (
        f"{workflow}: the token is given to the job or to a step other than the "
        f"one that downloads the release")


@pytest.mark.parametrize("workflow,kept", [
    ("failure-drill.yml", ["failure-injection.json", "drill-work/**/*-service.log"]),
    ("recovery-drill.yml", ["recovery-drill.json", "fixture-evidence.json",
                            "fixture-work/service.log", "drill-work/logs/*-service.log"]),
])
def test_a_drill_workflow_keeps_its_report_and_logs_when_the_drill_fails(workflow, kept):
    steps = _steps(_text(workflow))
    [upload] = [step for step in steps if "uses: actions/upload-artifact@" in step]
    assert _block(upload, "if") == ["always()"], (
        f"{workflow}: the upload is skipped when an earlier step fails, so a failed "
        f"drill keeps nothing")
    settings = _block(upload, "with")
    assert [line.strip() for line in _block("\n".join(settings), "path")] == kept
    assert _mapping(line for line in settings if not line.startswith(" "))[
        "if-no-files-found"] == "error"
    assert steps[-1] == upload, f"{workflow}: a step after the upload can be skipped"


def test_the_recovery_workflow_makes_its_key_private_before_writing_it():
    [fixture] = [script for script in _scripts(_text("recovery-drill.yml"))
                 if "scripts/drill_fixture.py" in script]
    assert fixture.index("umask 077") < fixture.index("> audit.key")
