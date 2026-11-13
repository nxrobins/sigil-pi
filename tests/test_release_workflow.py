"""The release tests, builds and signs in separate jobs, and only from main (audit F-16).

`release.yml` had one job, and it held `contents: write`, `id-token: write`
and `attestations: write`. It installed the development packages and ran the
whole test suite, then built, packaged, signed and published from the same
workspace, so anything the tests ran could change the files that were then
signed, and the attestation would vouch for them. Nothing checked that the
tag named a commit on `main`, so branch protection did not constrain what was
released.

A workflow cannot be run here. These tests read the file job by job (with the
step reader of tests/test_drill_workflows.py), and run the script of every
step that reads the tag in bash, rendered the way the runner renders it, in a
scratch repository laid out the way actions/checkout leaves one, with `gh`
and `git` replaced by shell functions that record how they were called.
"""

import fnmatch
import hashlib
import os
import re
import shutil
import subprocess

import pytest

from conftest import PI_ROOT
from test_drill_workflows import HOSTILE, _indent, _mapping, _steps

RELEASE = PI_ROOT / ".github" / "workflows" / "release.yml"
# What each job's token may do. The gate runs the tests and every development
# package, and the build runs the compiler's build scripts: both only read.
# Only the job that signs and publishes writes, and it runs nothing of the
# repository's. A job that comes to need more adds it here, with the reason.
PERMISSIONS = {
    "gate": {"contents": "read"},
    "build": {"contents": "read"},
    "publish": {"contents": "write", "id-token": "write", "attestations": "write",
                "artifact-metadata": "write"},
}
NEEDS = {"gate": set(), "build": {"gate"}, "publish": {"gate", "build"}}
# The only actions the publish job may use: one takes the build job's bytes,
# the other signs them.
PUBLISH_ACTIONS = {"actions/download-artifact", "actions/attest"}
# Tag names that git accepts and the trigger `v*` matches, and that are
# commands once pasted into a script or evaluated: a ref name may hold
# $ ( ) ; ' " ` and &.
TAG_HOSTILE = [
    "v0.4.0$(touch${IFS}injected)",
    "v0.4.0;touch${IFS}injected",
    "v0.4.0'$(touch${IFS}injected)'",
    'v0.4.0"`touch${IFS}injected`"',
    "v0.4.0&&touch${IFS}injected",
]
READS_TAG = re.compile(r"GITHUB_REF|github\.ref|github\.event\.ref\b")
# Running something the repository holds, or something fetched, from a script.
RUNS_CODE = re.compile(
    r"\b(?:python3?|pip3?|cargo|make|curl|wget|bash|sh|source|node|npm|npx)\b"
    r"|(?:^|[\s;&|(])\.{1,2}/")
VERSION = "0.4.0"
ASSET = f"sigil-pi-{VERSION}-linux-x86_64"
GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "release test",
    "GIT_AUTHOR_EMAIL": "release-test@example.invalid",
    "GIT_COMMITTER_NAME": "release test",
    "GIT_COMMITTER_EMAIL": "release-test@example.invalid",
}
# Put before every script the tests run: `gh` and `git` record each call, one
# per line, with its arguments separated by \037. `git` then runs, `gh` never.
RECORDERS = (
    "gh() { printf '%s\\037' \"$@\" >> \"$RECORDS/gh-calls\"; "
    "printf '\\n' >> \"$RECORDS/gh-calls\"; }\n"
    "git() { printf '%s\\037' \"$@\" >> \"$RECORDS/git-calls\"; "
    "printf '\\n' >> \"$RECORDS/git-calls\"; command git \"$@\"; }\n"
)


# -- reading the workflow ----------------------------------------------------

def _keys(lines, column):
    """The keys written at `column`: each one's inline value (a string), or
    the lines of its block without the block's indentation (a list)."""
    keys, key = {}, None
    for line in lines:
        own = re.match(rf"^ {{{column}}}([A-Za-z0-9_-]+):(?:\s+(.*?))?\s*$", line)
        if own:
            key, inline = own.groups()
            keys[key] = inline if inline and inline[0] not in "|>" else []
        elif line.strip() and _indent(line) <= column:
            key = None  # a comment at this level, or the end of the mapping
        elif key is not None and isinstance(keys[key], list):
            keys[key].append(line)
    for key, value in keys.items():
        if isinstance(value, list):
            while value and not value[-1].strip():
                value.pop()
            depth = min((_indent(entry) for entry in value if entry.strip()), default=0)
            keys[key] = [entry[depth:] for entry in value]
    return keys


def _step(text):
    lines = text.splitlines()
    column = len(re.match(r"^(\s*)- ", lines[0]).group(1)) + 2
    return _keys([" " * column + lines[0][column:]] + lines[1:], column)


def _job_texts(text):
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


def _workflow(text=None):
    """The workflow's top-level keys, and its jobs in the order they are written."""
    text = RELEASE.read_text() if text is None else text
    top = _keys(text.split("\njobs:\n", 1)[0].splitlines(), 0)
    jobs = {}
    for name, body in _job_texts(text).items():
        job = _keys(body.splitlines(), 4)
        job["steps"] = [_step(step) for step in _steps(body)]
        jobs[name] = job
    return top, jobs


def _job(jobs, name):
    assert name in jobs, f"release.yml has no {name} job; its jobs are {list(jobs)}"
    return jobs[name]


def _unquote(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _uses(step):
    return (step.get("uses") or "").split(" #")[0].strip()


def _action(step):
    return _uses(step).split("@")[0]


def _with(step):
    return {key: _unquote(value) for key, value in _mapping(step.get("with")).items()}


def _env(scope):
    return {key: _unquote(value) for key, value in _mapping(scope.get("env")).items()}


def _script(step):
    run = step.get("run")
    return "\n".join(run) if isinstance(run, list) else (run or "")


def _name(step):
    return _unquote(step.get("name") or _uses(step) or "an unnamed step")


def _reads_tag(step):
    values = [_script(step), *_env(step).values(), *_with(step).values()]
    return any(READS_TAG.search(value) for value in values)


def _tag_steps(jobs):
    """Every step that reads the tag, as (job, step), in the order the jobs run."""
    return [(name, step) for name, job in jobs.items() for step in job["steps"]
            if _reads_tag(step)]


def _permissions(value):
    if value is None:
        return None  # undeclared: whatever the repository's default grants
    if isinstance(value, list):
        return _mapping(value)
    return {} if value.strip() == "{}" else value.strip()


def _writes(granted):
    if granted is None:
        return ["the repository's default permissions"]
    if isinstance(granted, str):
        return [granted] if granted.startswith("write") else []
    return [f"{key}: {value}" for key, value in granted.items() if value == "write"]


def _needs(job):
    value = job.get("needs")
    if value is None:
        return set()
    entries = value if isinstance(value, list) else value.strip("[]").split(",")
    return {entry.strip().lstrip("- ").strip() for entry in entries if entry.strip()}


def _repository_code(job):
    """What in a job runs code from this repository, or installs packages."""
    found = []
    for step in job["steps"]:
        if _action(step) == "actions/checkout" and "repository" not in _with(step):
            found.append("actions/checkout")
        elif _uses(step).startswith("./"):
            found.append(_uses(step))
        script = _script(step)
        found += [marker for marker in ("./ci.sh", "scripts/", "pip install")
                  if marker in script]
    return found


# -- running the steps that read the tag -------------------------------------

@pytest.fixture(scope="module")
def release_workspaces(tmp_path_factory):
    """One scratch workspace per place the tagged commit can be, built once."""
    root = tmp_path_factory.mktemp("release-workspaces")
    return {where: _build_workspace(root / where, where)
            for where in ("tip", "older", "branch", "unfetched")}


def _build_workspace(root, where):
    """The workspace a release job finds: sigil-pi checked out at the tag the
    way actions/checkout leaves it with fetch-depth 0 (detached, no local
    branch, main as refs/remotes/origin/main), and the build's assets in dist/."""
    repository = root / "workspace" / "sigil-pi"
    repository.mkdir(parents=True)

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repository, check=True, capture_output=True, text=True,
            env={"PATH": os.environ["PATH"], "HOME": str(root), **GIT_ENV}).stdout.strip()

    git("init", "-q", "-b", "main")
    (repository / "VERSION").write_text(VERSION + "\n")
    git("add", "VERSION")
    git("commit", "-q", "-m", "Release 0.4.0")
    tagged = git("rev-parse", "HEAD")
    if where == "older":
        (repository / "NEWS").write_text("a later commit on main\n")
        git("add", "NEWS")
        git("commit", "-q", "-m", "Later on main")
    elif where == "branch":
        git("checkout", "-q", "-b", "topic")
        (repository / "NEWS").write_text("never merged\n")
        git("add", "NEWS")
        git("commit", "-q", "-m", "Not on main")
        tagged = git("rev-parse", "HEAD")
    if where != "unfetched":
        git("update-ref", "refs/remotes/origin/main", "refs/heads/main")
    git("tag", "-a", "-m", f"v{VERSION}", f"v{VERSION}", tagged)
    git("checkout", "-q", "--detach", f"v{VERSION}")
    for branch in git("for-each-ref", "--format=%(refname:short)", "refs/heads").split():
        git("branch", "-q", "-D", branch)
    dist = root / "workspace" / "dist"
    dist.mkdir()
    archive = dist / f"{ASSET}.tar.gz"
    archive.write_bytes(b"the bytes the build job made\n")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (dist / f"{ASSET}.tar.gz.sha256").write_text(f"{digest}  {archive.name}\n")
    (dist / f"{ASSET}.SBOM.cdx.json").write_text("{}\n")
    return root, digest


def _workspace(templates, tmp_path, where="tip"):
    template, digest = templates[where]
    shutil.copytree(template, tmp_path, dirs_exist_ok=True)
    (tmp_path / "records").mkdir()
    return tmp_path / "workspace", digest


def _context(workspace, tag, digest, *, earlier_outputs=False):
    context = {
        "github.ref_name": tag,
        "github.ref": f"refs/tags/{tag}",
        "github.token": "not-a-token",
        "github.repository": "example/sigil-pi",
        "github.workspace": str(workspace),
        "github.event.repository.private": "false",
        "needs.build.outputs.sha256": digest,
    }
    if earlier_outputs:  # for a step run alone: what the step before it wrote
        context.update({
            "steps.assets.outputs.archive": f"dist/{ASSET}.tar.gz",
            "steps.assets.outputs.checksums": f"dist/{ASSET}.tar.gz.sha256",
            "steps.assets.outputs.sbom": f"dist/{ASSET}.SBOM.cdx.json",
        })
    return context


def _render(text, context):
    """What the runner does to a value before a step sees it: each expression
    is replaced by its value, in `env:` and in the script alike."""
    def value(match):
        expression = match.group(1).strip()
        assert expression in context, f"this test does not model ${{{{ {expression} }}}}"
        return context[expression]
    return re.sub(r"\$\{\{(.*?)\}\}", value, text)


def _run(step, job, workspace, context):
    """Run one step's script as the runner runs it (`bash -e`, the default
    shell): with the runner's variables, the job's and the step's `env:`
    rendered, in the step's working directory, and its outputs read back."""
    records = workspace.parent / "records"
    output = records / "github-output"
    output.write_text("")
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(workspace.parent),
        "RECORDS": str(records),
        "GH_CONFIG_DIR": str(records / "gh-config"),
        "GITHUB_REF_NAME": context["github.ref_name"],
        "GITHUB_REF": context["github.ref"],
        "GITHUB_REPOSITORY": context["github.repository"],
        "GITHUB_WORKSPACE": str(workspace),
        "GITHUB_OUTPUT": str(output),
        "RUNNER_TEMP": str(workspace.parent),
        **GIT_ENV,
    }
    for scope in (job, step):
        environment.update({key: _render(value, context) for key, value in _env(scope).items()})
    script = records / "step.sh"
    script.write_text(RECORDERS + _render(_script(step), context) + "\n")
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", str(script)],
        cwd=workspace / _unquote(step.get("working-directory") or "."),
        env=environment, capture_output=True, text=True, timeout=120)
    if step.get("id"):
        for line in output.read_text().splitlines():
            key, _, value = line.partition("=")
            context[f"steps.{step['id']}.outputs.{key}"] = value
    return result


def _calls(workspace, command):
    path = workspace.parent / "records" / f"{command}-calls"
    if not path.exists():
        return []
    return [line.split("\x1f")[:-1] for line in path.read_text().splitlines()]


def _release(templates, tmp_path, tag, *, where="tip", digest=None, missing=None):
    """Push `tag`: run, job by job, every step that reads it, and stop at the
    first that fails. Returns what ran, as (job, step, result), and the calls
    made to gh."""
    workspace, built = _workspace(templates, tmp_path, where)
    if missing:
        (workspace / "dist" / missing).unlink()
    context = _context(workspace, tag, built if digest is None else digest)
    _, jobs = _workflow()
    ran = []
    for name, step in _tag_steps(jobs):
        if "run" not in step:
            continue  # an action's inputs: test_no_action_takes_the_tag_as_an_input
        result = _run(step, jobs[name], workspace, context)
        ran.append((name, step, result))
        if result.returncode != 0:
            break
    return ran, _calls(workspace, "gh")


def _failure(ran):
    failed = [(name, step, result) for name, step, result in ran if result.returncode != 0]
    return failed[0] if failed else (None, None, None)


# -- the shape ---------------------------------------------------------------

def test_the_release_workflow_can_be_read_job_by_job():
    """Anti-vacuity for every test below: the reader finds every job, step,
    script and action in the file, and the steps that read the tag."""
    text = RELEASE.read_text()
    directives = "\n".join(line for line in text.splitlines()
                           if not line.lstrip().startswith("#"))
    top, jobs = _workflow(text)
    assert {"name", "on"} <= set(top), top
    assert len(jobs) == len(re.findall(r"(?m)^  [A-Za-z0-9_-]+:\s*$",
                                       directives.split("\njobs:\n", 1)[1]))
    steps = [step for job in jobs.values() for step in job["steps"]]
    assert len(steps) == len(re.findall(r"(?m)^      - ", directives)) > 0
    assert sum("run" in step for step in steps) == len(
        re.findall(r"(?m)^\s+(?:- )?run: ", directives))
    assert sum("uses" in step for step in steps) == len(
        re.findall(r"(?m)^\s+(?:- )?uses: ", directives))
    assert all(("run" in step) != ("uses" in step) for step in steps)
    assert all(_script(step).strip() for step in steps if "run" in step)
    assert any(_action(step) == "actions/checkout" for step in steps)
    assert [step for _, step in _tag_steps(jobs) if "run" in step]


def test_no_job_that_runs_code_from_the_repository_can_write_or_sign():
    """The class this finding is in: a token that can write to the repository
    or sign is held only by a job that runs nothing the repository holds."""
    top, jobs = _workflow()
    for name, job in jobs.items():
        granted = _permissions(job.get("permissions", top.get("permissions")))
        writes, runs = _writes(granted), _repository_code(job)
        assert not (writes and runs), (
            f"release.yml job {name} can write and sign ({', '.join(writes)}) and runs code "
            f"from the repository ({', '.join(runs)}): anything the tests run can change the "
            f"files it then signs and publishes")


def test_each_job_holds_only_the_permissions_it_needs():
    top, jobs = _workflow()
    assert _permissions(top.get("permissions")) == {}, (
        "release.yml must grant nothing at the top (`permissions: {}`), so a job that "
        "names no permissions gets none")
    assert list(jobs) == list(PERMISSIONS), (
        f"release.yml's jobs are {list(jobs)}; the tests, the build and the signing each "
        f"run in a job of their own: {list(PERMISSIONS)}")
    for name, wanted in PERMISSIONS.items():
        assert _permissions(jobs[name].get("permissions")) == wanted, (
            f"release.yml's {name} job must hold exactly {wanted}")


def test_the_tests_the_build_and_the_signing_run_in_separate_jobs():
    _, jobs = _workflow()
    where = {}
    for name, job in jobs.items():
        for step in job["steps"]:
            for what, found in (
                    ("the tests (./ci.sh)", "./ci.sh" in _script(step)),
                    ("the build (scripts/build_release.py)",
                     "scripts/build_release.py" in _script(step)),
                    ("the signing (actions/attest)", _action(step) == "actions/attest"),
                    ("the publication (gh release create)",
                     "gh release create" in _script(step))):
                if found:
                    where.setdefault(what, set()).add(name)
    assert where == {
        "the tests (./ci.sh)": {"gate"},
        "the build (scripts/build_release.py)": {"build"},
        "the signing (actions/attest)": {"publish"},
        "the publication (gh release create)": {"publish"},
    }, f"release.yml runs these in these jobs: {where}"
    for name, needed in NEEDS.items():
        assert _needs(_job(jobs, name)) == needed, (
            f"release.yml's {name} job must need exactly {sorted(needed)}")
    order = list(jobs)
    for name, needed in NEEDS.items():
        assert all(order.index(other) < order.index(name) for other in needed), order


def test_the_gate_checks_the_tag_before_it_fetches_builds_or_tests_anything():
    _, jobs = _workflow()
    gate = _job(jobs, "gate")
    checkout, check = gate["steps"][:2]
    assert _action(checkout) == "actions/checkout" and "repository" not in _with(checkout)
    assert _with(checkout).get("fetch-depth") == "0", (
        "the gate's checkout needs main's history to tell whether the tag is on it")
    assert _with(checkout).get("persist-credentials") == "false", (
        "the gate runs the tests: no token may be left in .git/config")
    assert _reads_tag(check) and "git merge-base --is-ancestor" in _script(check), (
        f"the step after the gate's checkout must check the tag; it is {_name(check)!r}")
    assert any("./ci.sh" in _script(step) for step in gate["steps"][2:])


def test_the_build_starts_from_a_fresh_checkout_and_builds_the_compiler_itself():
    """The build job sees nothing the gate's tests could have touched: it
    checks out again on a fresh runner, restores no cache (a cache written by
    the gate could carry a compiler the build would take as up to date), and
    installs no Python package. It packages a compiler it built itself, from
    the pinned source; `--no-build` packages whatever is already in target/."""
    _, jobs = _workflow()
    build = _job(jobs, "build")
    steps = build["steps"]
    checkouts = [step for step in steps if _action(step) == "actions/checkout"]
    assert sorted(_with(step).get("repository", "this repository") for step in checkouts) == [
        "nxrobins/sigil", "this repository"]
    for step in checkouts:
        assert _with(step).get("persist-credentials") == "false", _with(step)
    [sigil] = [step for step in checkouts if _with(step).get("repository")]
    assert _with(sigil).get("ref") == "${{ steps.pin.outputs.ref }}", _with(sigil)
    builds = [step for step in steps if "scripts/build_release.py" in _script(step)]
    assert len(builds) == 1 and "--no-build" not in _script(builds[0]), (
        "the build job must build the compiler it packages")
    for step in steps:
        hit = re.search(r"\b(?:pip3?|venv|pytest)\b|\./ci\.sh", _script(step))
        assert not hit, f"the build job runs {hit.group(0)!r} in {_name(step)!r}"
        assert _action(step) not in ("Swatinem/rust-cache", "actions/cache",
                                     "actions/download-artifact"), (
            f"the build job takes files from elsewhere: {_uses(step)}")
        if _action(step) == "actions/setup-python":
            assert "cache" not in _with(step), "the build job restores no cache"
    uploads = [step for step in steps if _action(step) == "actions/upload-artifact"]
    assert len(uploads) == 1 and steps[-1] is uploads[0], (
        "the build job hands its assets on as its last step")
    artifact = _with(uploads[0]).get("name")
    assert artifact and "${{" not in artifact, artifact
    assert _with(uploads[0]).get("if-no-files-found") == "error"
    assert _mapping(build.get("outputs")).get("sha256") == "${{ steps.assets.outputs.sha256 }}", (
        "the build job reports the archive's digest apart from the artifact")


def test_the_build_job_builds_with_the_toolchain_the_gate_tested():
    _, jobs = _workflow()
    gate, build = _job(jobs, "gate"), _job(jobs, "build")
    assert _env(gate) == _env(build), "the gate and the build must pin the same Z3"
    for action in ("dtolnay/rust-toolchain", "actions/setup-python"):
        pinned = [[(_uses(step), _with(step)) for step in job["steps"] if _action(step) == action]
                  for job in (gate, build)]
        assert pinned[0] == pinned[1] and len(pinned[0]) == 1, (action, pinned)
    for name in ("Read the immutable SIGIL ref",
                 "Install pinned Z3 (official release binary, SHA256-verified)",
                 "Install the pinned Lean verifier toolchain"):
        scripts = [[_script(step) for step in job["steps"] if _name(step) == name]
                   for job in (gate, build)]
        assert scripts[0] == scripts[1] and len(scripts[0]) == 1, name
    names = [_name(step) for step in build["steps"]]
    position = next(number for number, step in enumerate(build["steps"])
                    if "scripts/build_release.py" in _script(step))
    assert names.index("Install the pinned Lean verifier toolchain") < position
    assert names.index("Install pinned Z3 (official release binary, SHA256-verified)") < position


def test_the_publish_job_runs_no_code_from_the_repository():
    _, jobs = _workflow()
    build, publish = _job(jobs, "build"), _job(jobs, "publish")
    for step in publish["steps"]:
        if "uses" in step:
            assert _action(step) in PUBLISH_ACTIONS, (
                f"the publish job uses {_uses(step)}; it may only take the build job's "
                f"assets and sign them")
        assert "working-directory" not in step, _name(step)
        code = "\n".join(line for line in _script(step).splitlines()
                         if not line.lstrip().startswith("echo "))
        hit = RUNS_CODE.search(code)
        assert not hit, f"the publish job runs {hit.group(0)!r} in {_name(step)!r}"
    [download] = [step for step in publish["steps"]
                  if _action(step) == "actions/download-artifact"]
    assert publish["steps"][0] is download
    [upload] = [step for step in build["steps"] if _action(step) == "actions/upload-artifact"]
    assert _with(download).get("name") == _with(upload).get("name"), (
        "the publish job must take exactly the artifact the build job made")
    for name, job in jobs.items():
        if name != "build":
            assert not any(_action(step) == "actions/upload-artifact" for step in job["steps"]), (
                f"only the build job makes the artifact; {name} uploads one too")


def test_no_action_takes_the_tag_as_an_input():
    """A script checks the tag before it uses it; an action's input is not
    checked, so the tag never reaches one."""
    _, jobs = _workflow()
    for name, job in jobs.items():
        for step in job["steps"]:
            for key, value in _with(step).items():
                assert not READS_TAG.search(value), (
                    f"release.yml {name}: {_uses(step)} takes the tag in {key}: {value!r}")


def test_no_expression_is_pasted_into_a_release_script():
    """The runner pastes an expression into a script before bash reads it, so
    what the expression holds becomes code. A value reaches a release script
    only through the step's `env:`, and every script is strict."""
    _, jobs = _workflow()
    pasted, lax = [], []
    for name, job in jobs.items():
        for step in job["steps"]:
            written = re.findall(r"\$\{\{[^}]*\}\}", _script(step))
            if written:
                pasted.append(f"{name}: {_name(step)!r} writes {written}")
            if _reads_tag(step) and not _script(step).startswith("set -euo pipefail\n"):
                lax.append(f"{name}: {_name(step)!r}")
    assert not pasted, f"release.yml pastes expressions into scripts: {pasted}"
    assert not lax, f"release.yml reads the tag in scripts that do not start strict: {lax}"


def test_the_release_does_not_upgrade_pip_to_whatever_is_newest():
    """The job that ran the tests upgraded pip, unpinned, before installing
    the development lock: the installer was whatever the index served that
    day. The venv's own pip comes with the pinned Python."""
    directives = "\n".join(line for line in RELEASE.read_text().splitlines()
                           if not line.lstrip().startswith("#"))
    unpinned = re.findall(r"pip3? install[^\n]*(?:--upgrade|-U)\b[^\n]*\bpip\b(?!=)", directives)
    assert not unpinned, f"release.yml upgrades pip without a version: {unpinned}"


# -- the steps that read the tag, run ----------------------------------------

@pytest.mark.parametrize("where", ["tip", "older"])
def test_a_release_tag_on_main_is_published_with_the_assets_the_build_made(
        release_workspaces, tmp_path, where):
    ran, gh = _release(release_workspaces, tmp_path, f"v{VERSION}", where=where)
    name, step, result = _failure(ran)
    assert result is None, f"release.yml {name}: {_name(step)!r} failed: {result.stderr}"
    assert [call[:3] for call in gh] == [["release", "create", f"v{VERSION}"]], gh
    [call] = gh
    assert set(call[3:6]) == {f"dist/{ASSET}.tar.gz", f"dist/{ASSET}.tar.gz.sha256",
                              f"dist/{ASSET}.SBOM.cdx.json"}, call
    assert call[call.index("--repo") + 1] == "example/sigil-pi", call
    for flag in ("--verify-tag", "--prerelease", "--latest=false"):
        assert flag in call, call


@pytest.mark.parametrize("where", ["branch", "unfetched"])
def test_a_tag_whose_commit_is_not_on_main_is_not_released(release_workspaces, tmp_path, where):
    ran, gh = _release(release_workspaces, tmp_path, f"v{VERSION}", where=where)
    assert gh == [], (
        f"a tag on a commit that is not on main reached gh: {gh}; nothing checks that the "
        f"tagged commit is on main")
    name, step, result = _failure(ran)
    assert name == "gate" and "main" in result.stderr, (name, result and result.stderr)
    _, jobs = _workflow()
    assert step == _job(jobs, "gate")["steps"][1], "the tag is refused before anything else runs"


def test_a_tag_that_is_not_the_version_is_not_released(release_workspaces, tmp_path):
    ran, gh = _release(release_workspaces, tmp_path, "v0.4.1")
    name, step, result = _failure(ran)
    assert gh == [] and name == "gate" and "VERSION" in result.stderr, (gh, name, result)


@pytest.mark.parametrize("digest", ["0" * 64, "", "not-a-digest"])
def test_assets_the_build_job_did_not_make_are_neither_signed_nor_published(
        release_workspaces, tmp_path, digest):
    """The archive's digest travels from the build job as a job output, apart
    from the artifact: the publish job signs only the bytes the build made."""
    ran, gh = _release(release_workspaces, tmp_path, f"v{VERSION}", digest=digest)
    assert gh == [], f"an archive the build did not make was published: {gh}"
    name, step, result = _failure(ran)
    _, jobs = _workflow()
    steps = _job(jobs, "publish")["steps"]
    first_signature = min(number for number, other in enumerate(steps)
                          if _action(other) == "actions/attest")
    assert name == "publish" and steps.index(step) < first_signature, (name, _name(step))


@pytest.mark.parametrize("missing", [f"{ASSET}.tar.gz", f"{ASSET}.tar.gz.sha256",
                                     f"{ASSET}.SBOM.cdx.json"])
def test_a_missing_asset_stops_the_publish_job_before_it_signs(
        release_workspaces, tmp_path, missing):
    ran, gh = _release(release_workspaces, tmp_path, f"v{VERSION}", missing=missing)
    assert gh == [], gh
    name, step, result = _failure(ran)
    _, jobs = _workflow()
    steps = _job(jobs, "publish")["steps"]
    first_signature = min(number for number, other in enumerate(steps)
                          if _action(other) == "actions/attest")
    assert name == "publish" and steps.index(step) < first_signature, (name, _name(step))


def test_the_hostile_tags_are_names_git_accepts_and_the_trigger_matches():
    """Anti-vacuity for the test below: each of these can really be pushed
    as a tag, and starts a release run."""
    for tag in TAG_HOSTILE:
        result = subprocess.run(["git", "check-ref-format", f"refs/tags/{tag}"],
                                capture_output=True, text=True)
        assert result.returncode == 0, (tag, result.stderr)
        assert fnmatch.fnmatchcase(tag, "v*"), tag


@pytest.mark.parametrize("hostile", HOSTILE + TAG_HOSTILE)
def test_a_tag_that_is_not_a_release_tag_reaches_neither_the_shell_nor_git_nor_gh(
        release_workspaces, tmp_path, hostile):
    _, jobs = _workflow()
    steps = [(name, step) for name, step in _tag_steps(jobs) if "run" in step]
    assert steps
    for number, (name, step) in enumerate(steps):
        where = tmp_path / str(number)
        workspace, digest = _workspace(release_workspaces, where)
        context = _context(workspace, hostile, digest, earlier_outputs=True)
        result = _run(step, jobs[name], workspace, context)
        named = f"release.yml {name}: {_name(step)!r}"
        assert not list(where.rglob("injected")), (
            f"{named}: the tag {hostile!r} ran as a shell command")
        assert _calls(workspace, "gh") == [], f"{named}: gh was called for the tag {hostile!r}"
        assert _calls(workspace, "git") == [], (
            f"{named}: git was called for the tag {hostile!r} before the tag was checked")
        assert result.returncode != 0, f"{named} accepted the tag {hostile!r}"
        assert "not a release tag" in result.stderr, (named, hostile, result.stderr)
        assert "::" not in result.stdout + result.stderr, (named, hostile, result.stderr)
