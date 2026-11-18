"""The recovery drill qualifies a release that declares `state migration required` (audit F-12, F-23, F-27; node H6).

Three changes of this run change an on-disk format behind a versioned
migration: a backup's manifest carries an authentication tag (B2, F-12), a
session's sandbox is named by its full digest, state layout 2 (A7, F-23), and
the schedule store is kept as rows (C4b, F-27). docs/state-compatibility.md
says how such a release is upgraded to and rolled back from. The recovery
drill did not do it: it started the candidate on the state the old release
had served and the old release on the state the candidate had served, and the
candidate's tool restored the old release's backup without the flag an
untagged archive needs. A candidate with layout 2 refuses layout-1 state, a
release before it refuses a full-width sandbox, and the candidate's restore
refuses an untagged archive, so the drill from any published release to a
candidate of this tree stopped. Every drill test built both releases from
one tree, so none saw it.

The drill now follows the documented upgrade and rollback: the old release's
tool backs the state up; the candidate's tool restores that archive into a
new path, with `--skip-manifest-authentication` only for an archive without
a tag, and the candidate's `state_tool.py migrate` dry-runs that copy and
migrates it when the dry run names work; and when the release declares
`state migration required` the old release is rolled back onto the
pre-migration or pre-upgrade backup, restored into a new path by its own tool.

Here: the drill's decisions, which run everywhere; the drill against stand-in
releases whose state tools write no tag or have no `migrate` command; and the
drill against real bundles, the old one built from c41f3e5, the tree before
manifest authentication and state layout 2 (needs the toolchain and that
commit, which a shallow clone lacks).
"""

import io
import json
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import state_tool
import toolchain
from conftest import PI_ROOT, needs_toolchain
from scripts import release_drill
from scripts.build_release import APP_FILES, DOC_FILES, build_release
from scripts.check_readiness_evidence import _validate_recovery_report
from scripts.drill_fixture import produce_backup
from scripts.release_drill import (
    FIXTURE_SESSION,
    FIXTURE_TENANT,
    ReleaseDrillError,
    run_drill,
)
from state_tool import migrate_state
from test_release_drill import (
    AUDIT_KEY,
    _app_variant,
    _backup,
    _fake_sigil,
    _stand_ins,
)

# The run's integration tip before A7 (state layout 2) and B2 (manifest
# authentication) merged: its state tool writes untagged archives and has no
# `migrate`, and its product names sandboxes by 16 hex characters and refuses
# a full-width one. The same compiler pin as this tree, so it builds here.
OLDER_TREE = "c41f3e5e34f065c212156506b98e604adf544ca9"
CANDIDATE_VERSION = "9.9.9"
FLAG = "--skip-manifest-authentication"
# The two declarations a release makes (docs/state-compatibility.md, "Upgrade
# and rollback rule"), as the report names them.
MIGRATION_REQUIRED = "state migration required"
SCHEMA_UNCHANGED = "state schema unchanged"


def _steps(report, stage):
    return [(step["command"], step["release"]) for step in report["state_migration"][stage]]


def _step(report, stage, command, release):
    [found] = [step for step in report["state_migration"][stage]
               if (step["command"], step["release"]) == (command, release)]
    return found


# ── the drill's decisions ────────────────────────────────────────────────

def test_the_drill_names_the_tag_the_flag_and_the_declarations_as_the_documents_do():
    assert release_drill.MANIFEST_TAG_HEADER == state_tool.MANIFEST_TAG_HEADER
    assert release_drill.SKIP_AUTHENTICATION_FLAG == state_tool.SKIP_AUTHENTICATION_FLAG == FLAG
    assert (release_drill.MIGRATION_REQUIRED, release_drill.SCHEMA_UNCHANGED) == (
        MIGRATION_REQUIRED, SCHEMA_UNCHANGED)
    rule = (PI_ROOT / "docs" / "state-compatibility.md").read_text()
    assert f"`{MIGRATION_REQUIRED}`" in rule and f"`{SCHEMA_UNCHANGED}`" in rule


@pytest.mark.parametrize("tagged", [True, False])
@pytest.mark.parametrize("offered", [True, False])
def test_the_flag_is_given_only_for_an_archive_without_a_tag(tagged, offered):
    """The class guard: the drill never passes the flag for a tagged archive,
    and never to a tool that does not have it (a release before manifest
    authentication restores an untagged archive without it)."""
    options = release_drill._restore_options(tagged, offered)
    assert options == ([FLAG] if offered and not tagged else [])


def _flag_given_outside_its_rule(source):
    """The lines of `source` (a drill script) that name the flag that skips
    manifest authentication other than by release_drill's constant, or that
    use the constant other than to define it, inside `_restore_options`, or to
    look for it in a state tool's help (`SKIP_AUTHENTICATION_FLAG in ...`)."""
    import ast

    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def within(node, name):
        while node in parents:
            node = parents[node]
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return True
        return False

    offending = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == FLAG:
            parent = parents.get(node)
            if not (isinstance(parent, ast.Assign) and [target.id for target in parent.targets
                                                        if isinstance(target, ast.Name)]
                    == ["SKIP_AUTHENTICATION_FLAG"]):
                offending.append(node.lineno)
        elif isinstance(node, ast.Name) and node.id == "SKIP_AUTHENTICATION_FLAG":
            parent = parents.get(node)
            looked_for = (isinstance(parent, ast.Compare) and parent.left is node
                          and [type(op) for op in parent.ops] == [ast.In])
            if not (isinstance(node.ctx, ast.Store) or looked_for
                    or within(node, "_restore_options")):
                offending.append(node.lineno)
    return offending


@pytest.mark.parametrize("script", ["release_drill.py", "failure_drill.py", "drill_fixture.py"])
def test_no_drill_gives_the_flag_except_by_its_rule(script):
    """The class guard in the source: whether a restore skips manifest
    authentication is `_restore_options`'s decision alone."""
    source = (PI_ROOT / "scripts" / script).read_text()
    assert _flag_given_outside_its_rule(source) == []
    if script == "release_drill.py":
        assert "def _restore_options(" in source and source.count(f'"{FLAG}"') == 1


def test_the_flag_guard_fires_on_a_restore_that_passes_the_flag_itself():
    for forbidden in (
            f'options = ["{FLAG}"]\n',
            "options = [SKIP_AUTHENTICATION_FLAG]\n",
            "def restore(tagged):\n    return [SKIP_AUTHENTICATION_FLAG] if not tagged else []\n"):
        assert _flag_given_outside_its_rule(forbidden) == [1 if "def" not in forbidden else 2]


def _untagged(archive, destination):
    """`archive` written again without the tag: as a release before manifest
    authentication writes it."""
    with tarfile.open(archive, "r:gz") as source, \
            tarfile.open(destination, "w:gz", format=tarfile.PAX_FORMAT) as copy:
        for member in source.getmembers():
            member.pax_headers = {}
            copy.addfile(member, source.extractfile(member) if member.isreg() else None)
    return destination


def test_the_drill_tells_a_tagged_archive_from_an_untagged_one(tmp_path):
    backup, _, _ = _backup(tmp_path)
    assert release_drill._manifest_tagged(backup) is True
    assert release_drill._manifest_tagged(_untagged(backup, tmp_path / "old.tar.gz")) is False
    with pytest.raises(ReleaseDrillError, match="cannot read state backup"):
        release_drill._manifest_tagged(tmp_path / "missing.tar.gz")


# A dry run of A7's command (state layout 2), and of C4b's, which adds the
# schedule store's step and names the steps it would take.
A7_DRY_RUN = {"migration": "sandbox-full-digest", "from_layout": 1, "to_layout": 2,
              "dry_run": True, "resumed": False, "problems": [], "backup": None,
              "backup_sha256": None,
              "moves": [{"from": "sandboxes/0123456789abcdef", "to": "sandboxes/" + "0" * 64}]}
C4B_DRY_RUN = {"migration": "sandbox-full-digest", "from_layout": 2, "to_layout": 2,
               "schedule_store": {"migration": "schedule-store-rows", "from_schema": 1,
                                  "to_schema": 2, "schedules": 3},
               "steps": ["schedule-store-rows"], "dry_run": True, "resumed": False,
               "moves": [], "problems": [], "backup": None, "backup_sha256": None}


@pytest.mark.parametrize("report, work", [
    (A7_DRY_RUN, True),
    ({**A7_DRY_RUN, "from_layout": 2, "moves": []}, False),
    ({**A7_DRY_RUN, "from_layout": 2, "moves": [], "resumed": True}, True),
    (C4B_DRY_RUN, True),
    ({**C4B_DRY_RUN, "steps": [], "schedule_store": {
        **C4B_DRY_RUN["schedule_store"], "from_schema": 2, "schedules": 0}}, False),
], ids=["layout-1", "layout-2", "resumed", "schedule-store", "current"])
def test_a_dry_run_names_work_by_a_step_a_move_or_a_run_to_finish(report, work):
    assert release_drill._has_work(report) is work


def test_a_release_names_its_state_formats_by_what_its_migration_moves_state_to(tmp_path):
    assert release_drill._formats(A7_DRY_RUN) == {"to_layout": 2}
    assert release_drill._formats(C4B_DRY_RUN) == {
        "to_layout": 2, "schedule_store.to_schema": 2}
    # And from this tree's own command, on a state it has nothing to do to.
    backup, _, _ = _backup(tmp_path)
    state = tmp_path / "restored"
    state_tool.restore_backup(backup, state, audit_key=AUDIT_KEY.encode())
    report = migrate_state(state, audit_key=AUDIT_KEY.encode(), dry_run=True)
    assert release_drill._has_work(report) is False
    assert release_drill._formats(report)["to_layout"] == 2


@pytest.mark.parametrize("old, new, migrated, declared", [
    (None, None, False, SCHEMA_UNCHANGED),
    ({"to_layout": 2}, {"to_layout": 2}, False, SCHEMA_UNCHANGED),
    # The published releases have no migrate command; the candidate's state
    # is one they refuse even when there was nothing to move (a backup holds
    # no empty directory, so the old release's empty sandboxes are not in it).
    (None, {"to_layout": 2}, False, MIGRATION_REQUIRED),
    ({"to_layout": 2}, {"to_layout": 2, "schedule_store.to_schema": 2}, False,
     MIGRATION_REQUIRED),
    ({"to_layout": 2}, {"to_layout": 2}, True, MIGRATION_REQUIRED),
])
def test_the_rollback_restores_a_backup_whenever_the_release_changes_a_format(
        old, new, migrated, declared):
    declaration, because = release_drill._declaration(old, new, migrated)
    assert declaration == declared, because


def test_files_keep_their_bytes_under_the_names_a_migration_gave_them():
    moves = [{"from": "sandboxes/0123456789abcdef", "to": "sandboxes/" + "a" * 64}]
    payloads = {"sandboxes/0123456789abcdef/notes.txt": b"n",
                "sandboxes/0123456789abcdef0/other": b"o",
                "sessions/x.kv": b"[]"}
    assert release_drill._renamed(payloads, moves) == {
        "sandboxes/" + "a" * 64 + "/notes.txt": b"n",
        "sandboxes/0123456789abcdef0/other": b"o",
        "sessions/x.kv": b"[]"}
    with pytest.raises(ReleaseDrillError, match="invalid moves"):
        release_drill._renamed(payloads, [{"from": "sandboxes/x"}])


# ── against stand-in releases whose state tools came before ──────────────

def _release(tmp_path, name, version, patches=()):
    """A release of this tree at `version` whose state_tool.py has each
    (old, new) of `patches` applied, every occurrence."""
    sigil, pin = _fake_sigil(tmp_path / name)
    app = _app_variant(tmp_path / name, "app", version)
    source = (app / "state_tool.py").read_text()
    for old, new in patches:
        assert old in source, f"the patch no longer applies to state_tool.py: {old!r}"
        source = source.replace(old, new)
    (app / "state_tool.py").write_text(source)
    return build_release(
        sigil_root=sigil, output_dir=tmp_path / name / "out", app_root=app,
        pin_file=pin, build_runtime=False, platform_tag="test")[0]


# A state tool like those before manifest authentication: it writes no tag.
WRITES_NO_TAG = [("pax_headers=tag)", "pax_headers=None)")]
# A state tool like those before state layout 2: it has no `migrate` command.
NO_MIGRATE_COMMAND = [('"migrate", help=', '"migrate-is-not-in-this-release", help=')]


def _drill(tmp_path, old, new):
    backup, key, _ = _backup(tmp_path)
    return run_drill(old_artifact=old, new_artifact=new, state_backup=backup,
                     audit_key_file=key, output=tmp_path / "recovery-drill.json",
                     work_dir=tmp_path / "work")


def test_the_candidate_restores_an_old_release_s_untagged_backups_with_the_flag(
        tmp_path, monkeypatch):
    """B2's sweep (4): the candidate's tool refused the backup the old release
    wrote after the rollback, so the drill stopped at backup_online_new. Each
    backup the old release writes in the drill is restored with the flag, its
    digest recorded when it was written and checked before the restore."""
    stand_ins = _stand_ins(monkeypatch)
    old = _release(tmp_path, "old", "0.9.0", WRITES_NO_TAG)
    new = _release(tmp_path, "new", "1.0.0")
    report = _drill(tmp_path, old, new)
    work = tmp_path / "work"
    assert report["qualification_eligible"] is True, report["qualification_failures"]

    change = report["state_migration"]
    # Both state tools name the same formats and nothing needed moving.
    assert change["declaration"] == SCHEMA_UNCHANGED, change["because"]
    assert change["formats"]["old"] == change["formats"]["new"]
    assert change["formats"]["new"]["to_layout"] == 2
    assert change["commands"] == {
        "old": {"migrate": True, "skip_manifest_authentication": True},
        "new": {"migrate": True, "skip_manifest_authentication": True}}
    assert _steps(report, "upgrade") == [
        ("backup", "old"), ("migrate --dry-run", "old"), ("restore", "new"),
        ("migrate --dry-run", "new")]
    for stage, archive in (("upgrade", "pre-upgrade-state.tar.gz"),
                           ("online", "post-rollback-state.tar.gz")):
        written = _step(report, "rollback" if stage == "online" else stage, "backup", "old")
        assert (written["backup"], written["manifest_tag"]) == (archive, False)
        assert written["sha256"] == release_drill._sha256(work / archive)
        restored = _step(report, stage, "restore", "new")
        assert restored["options"] == [FLAG], (
            f"the candidate restored {archive}, which carries no tag, without {FLAG}")
        assert restored["manifest_authentication"] == "skipped"
        assert restored["sha256"] == written["sha256"]
        assert "SHA-256 recorded then and checked before this restore" in restored["origin"]
        assert _step(report, stage, "migrate --dry-run", "new")["has_work"] is False
    # Nothing was migrated, so the rollback continued the candidate's state.
    assert change["rollback_continues"] == "the state the candidate left, upgrade-state"
    assert [boot.state.name for boot in stand_ins.boots] == [
        "clean-state", "restored-state", "upgrade-state", "upgrade-state", "online-state",
        "online-restored-state"]
    turns = [phase["turn"] for phase in report["phases"]]
    assert [turn["messages_before"] for turn in turns] == [0, 2, 4, 6]


def test_a_candidate_after_a_release_without_migrate_rolls_back_from_the_pre_upgrade_backup(
        tmp_path, monkeypatch):
    """The old release's state tool has no `migrate`: it predates every state
    format the candidate's migrates to, so the candidate's state is one it
    refuses, and the rollback starts it on its own pre-upgrade backup,
    restored into a new path by its own tool, even though the dry run found
    nothing to move."""
    stand_ins = _stand_ins(monkeypatch)
    old = _release(tmp_path, "old", "0.9.0", NO_MIGRATE_COMMAND)
    new = _release(tmp_path, "new", "1.0.0")
    report = _drill(tmp_path, old, new)
    work = tmp_path / "work"
    assert report["qualification_eligible"] is True, report["qualification_failures"]

    change = report.get("state_migration") or {}
    assert change.get("declaration") == MIGRATION_REQUIRED, (
        "the drill rolled the old release back onto the state the candidate left, "
        "which a release that changes a state format forbids "
        "(docs/state-compatibility.md, Upgrade and rollback rule)")
    assert change["formats"]["old"] is None and change["formats"]["new"]["to_layout"] == 2
    assert "no migrate command" in change["because"]
    assert _steps(report, "upgrade") == [
        ("backup", "old"), ("restore", "new"), ("migrate --dry-run", "new")]
    assert _steps(report, "rollback") == [("restore", "old"), ("backup", "old")]
    rollback = _step(report, "rollback", "restore", "old")
    pre_upgrade = _step(report, "upgrade", "backup", "old")
    assert (rollback["backup"], rollback["sha256"], rollback["state"]) == (
        "pre-upgrade-state.tar.gz", pre_upgrade["sha256"], "rollback-state")
    assert (rollback["options"], rollback["manifest_authentication"]) == ([], "authenticated")
    assert change["rollback_continues"].startswith(
        "the state in pre-upgrade-state.tar.gz, restored into rollback-state")
    assert [boot.state.name for boot in stand_ins.boots] == [
        "clean-state", "restored-state", "upgrade-state", "rollback-state", "online-state",
        "online-restored-state"]
    # The rollback read the session as the pre-upgrade backup holds it, not
    # with the candidate's turn, and the state it left holds two turns more
    # than was committed, not three.
    turns = [phase["turn"] for phase in report["phases"]]
    assert [turn["messages_before"] for turn in turns] == [0, 2, 4, 4]
    session = report["evaluation"]["continuity"]["session"]
    assert (session["committed_messages"], session["messages_after"]) == (2, 6)
    assert report["evaluation"]["continuity"]["preserved"] is True
    assert (work / "upgrade-state").is_dir() and (work / "rollback-state").is_dir()
    serving = report["online_backup"]["phases"][0]["turn"]
    assert serving["messages_before"] == 6
    assert _validate_recovery_report(
        tmp_path, artifact_sha256=report["artifacts"]["new"]["sha256"], version="1.0.0",
        rollback_from=report["artifacts"]["old"]) == report

    # The `Recovery drill` workflow's log says so, from the report as written.
    from test_drill_workflows import DRILL_SCRIPTS, _block, _jobs, _text
    from test_drill_workflows import _steps as _workflow_steps
    [run] = [step for step in _workflow_steps(_jobs(_text("recovery-drill.yml"))["drill"])
             if DRILL_SCRIPTS["recovery-drill.yml"] in step]
    lines = _block(run, "run")
    start = lines.index("python - <<'PY'")
    program = "\n".join(lines[start + 1:lines.index("PY", start)])
    printed = subprocess.run([sys.executable, "-c", program], cwd=tmp_path,
                             capture_output=True, text=True, timeout=120)
    assert printed.returncode == 0, printed.stderr
    shown = printed.stdout.splitlines()
    assert f"state: {MIGRATION_REQUIRED} - {change['because']}" in shown, shown
    assert any(line.startswith("  rollback: the old release's state_tool.py restore {"
                               "'backup': 'pre-upgrade-state.tar.gz'") for line in shown), shown


def test_an_archive_changed_after_the_drill_wrote_it_is_not_restored(tmp_path, monkeypatch):
    """An untagged archive's origin is the drill's own record of it: its
    digest, taken as the old release wrote it, is checked before the
    candidate restores it with the flag. One changed in between stops the
    upgrade at upgrade_new, which the report names, and no candidate starts."""
    stand_ins = _stand_ins(monkeypatch)
    old = _release(tmp_path, "old", "0.9.0", WRITES_NO_TAG)
    new = _release(tmp_path, "new", "1.0.0")
    archive = tmp_path / "work" / "pre-upgrade-state.tar.gz"
    read_help = release_drill._state_tool_help

    def tamper_then_read(release_root, command):
        # Between the backup and the candidate's restore, the drill reads
        # each state tool's --help: change the archive the backup wrote.
        if archive.is_file() and not (tmp_path / "tampered").exists():
            with archive.open("ab") as stream:
                stream.write(b"\0")
            (tmp_path / "tampered").touch()
        return read_help(release_root, command)

    monkeypatch.setattr(release_drill, "_state_tool_help", tamper_then_read)
    with pytest.raises(ReleaseDrillError) as failure:
        _drill(tmp_path, old, new)
    assert str(failure.value) == (
        "upgrade_new: pre-upgrade-state.tar.gz is not the archive the old release "
        "wrote: its SHA-256 changed")
    assert [boot.version for boot in stand_ins.boots] == ["0.9.0", "0.9.0"]
    assert not (tmp_path / "work" / "upgrade-state").exists()
    report = json.loads((tmp_path / "recovery-drill.json").read_text())
    assert report["phases"][-1] == {
        "name": "upgrade_new", "artifact_sha256": report["artifacts"]["new"]["sha256"],
        "version": "1.0.0", "failed": True, "error": str(failure.value)}
    assert report["qualification_eligible"] is False
    assert _steps(report, "upgrade") == [("backup", "old"), ("migrate --dry-run", "old")]


# ── against real bundles: from the tree before B2 and A7 ─────────────────

def _git(*arguments):
    return subprocess.run(["git", "-C", str(PI_ROOT), *arguments], capture_output=True,
                          timeout=120)


def _export(destination):
    """The files a release is built from, as OLDER_TREE holds them."""
    paths = [*APP_FILES, *DOC_FILES, "SIGIL_REV", "tools", "config/auth.example.json",
             "config/alert-policy.json", "config/grafana-slo-dashboard.json"]
    exported = _git("archive", "--format=tar", OLDER_TREE, "--", *paths)
    assert exported.returncode == 0, exported.stderr.decode()[-2000:]
    with tarfile.open(fileobj=io.BytesIO(exported.stdout)) as archive:
        for member in archive.getmembers():
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            assert member.isreg(), member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())
    return destination


def _older_tool(tree, key, *arguments):
    """OLDER_TREE's own state_tool.py, as its release runs it."""
    result = subprocess.run(
        [sys.executable, str(tree / "state_tool.py"), *map(str, arguments)], cwd=tree,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PI_AUDIT_KEY": key},
        capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def older(tmp_path_factory):
    """Two real bundles: OLDER_TREE as it was (version 0.4.0), and this tree
    as the candidate."""
    needs_toolchain()
    try:
        present = _git("cat-file", "-e", f"{OLDER_TREE}^{{commit}}").returncode == 0
    except (OSError, subprocess.TimeoutExpired) as error:
        pytest.skip(f"git is not usable here: {error}")
    if not present:
        pytest.skip(f"commit {OLDER_TREE[:7]}, the tree before manifest authentication and "
                    "state layout 2, is not in this checkout (a shallow clone)")
    tmp = tmp_path_factory.mktemp("older-release")
    sigil = toolchain.resolve().stdlib_repo
    tree = _export(tmp / "older-tree")
    old, _ = build_release(sigil_root=sigil, output_dir=tmp / "old", app_root=tree,
                           build_runtime=False)
    new, _ = build_release(
        sigil_root=sigil, output_dir=tmp / "new", build_runtime=False,
        app_root=_app_variant(tmp, "candidate-tree", CANDIDATE_VERSION),
        pin_file=PI_ROOT / "SIGIL_REV")
    key = tmp / "audit.key"
    key.write_text("recovery-drill-older-release-key-with-32-plus-bytes\n")
    key.chmod(0o600)
    return SimpleNamespace(old=old, new=new, key=key, tree=tree, directory=tmp,
                           version=(tree / "VERSION").read_text().strip())


def _real_drill(older, name, plant=None):
    """The fixture's backup from the old bundle, as the `Recovery drill`
    workflow makes it, then the drill. `plant` may add to the backed-up state
    before the drill reads it, through the old release's own tool."""
    work = older.directory / name
    work.mkdir()
    backup = work / "pre-upgrade-backup.tar.gz"
    produce_backup(artifact=older.old, output=backup, audit_key_file=older.key,
                   work_dir=work / "fixture-work")
    if plant is not None:
        key = older.key.read_text().strip()
        state = work / "planted-state"
        _older_tool(older.tree, key, "restore", "--backup", backup, "--state", state)
        plant(state)
        backup = work / "planted-backup.tar.gz"
        _older_tool(older.tree, key, "backup", "--state", state, "--output", backup)
    report = run_drill(old_artifact=older.old, new_artifact=older.new, state_backup=backup,
                       audit_key_file=older.key, output=work / "recovery-drill.json",
                       work_dir=work / "drill-work")
    return SimpleNamespace(report=report, work=work / "drill-work", directory=work)


def _qualified(drill, older):
    report = drill.report
    assert report["qualification_failures"] == [] and report["qualification_eligible"] is True
    assert report["artifacts"]["old"]["version"] == older.version == "0.4.0"
    assert _validate_recovery_report(
        drill.directory, artifact_sha256=report["artifacts"]["new"]["sha256"],
        version=CANDIDATE_VERSION, rollback_from=report["artifacts"]["old"]) == report


# The session the fixture commits, and the name of its sandbox at state
# layout 1 and at layout 2.
SESSION_DIGEST = Path(release_drill._session_files(FIXTURE_TENANT, FIXTURE_SESSION)[0]).stem
LAYOUT_1, LAYOUT_2 = f"sandboxes/{SESSION_DIGEST[:16]}", f"sandboxes/{SESSION_DIGEST}"


@pytest.fixture(scope="module")
def from_the_fixture(older):
    return _real_drill(older, "fixture")


def test_a_drill_from_a_release_before_b2_and_a7_qualifies_the_candidate(older, from_the_fixture):
    """The workflow's own input: the old release's turn leaves its sandbox
    empty, and a backup holds no empty directory, so the candidate's dry run
    finds nothing to move. The candidate's state is still one the old release
    refuses (its first turn makes a full-width sandbox), so the rollback
    restores the pre-upgrade backup with the old release's tool."""
    report = from_the_fixture.report
    _qualified(from_the_fixture, older)
    change = report["state_migration"]
    assert change["declaration"] == MIGRATION_REQUIRED
    assert change["formats"]["old"] is None and change["formats"]["new"]["to_layout"] == 2
    assert "no migrate command" in change["because"]
    assert change["commands"] == {
        "old": {"migrate": False, "skip_manifest_authentication": False},
        "new": {"migrate": True, "skip_manifest_authentication": True}}
    assert _steps(report, "upgrade") == [
        ("backup", "old"), ("restore", "new"), ("migrate --dry-run", "new")]
    restored = _step(report, "upgrade", "restore", "new")
    assert (restored["manifest_tag"], restored["options"], restored["manifest_authentication"]) == (
        False, [FLAG], "skipped")
    assert restored["sha256"] == release_drill._sha256(
        from_the_fixture.work / "pre-upgrade-state.tar.gz")
    assert _step(report, "upgrade", "migrate --dry-run", "new")["has_work"] is False
    rollback = _step(report, "rollback", "restore", "old")
    assert (rollback["backup"], rollback["options"], rollback["manifest_authentication"]) == (
        "pre-upgrade-state.tar.gz", [],
        "not checked: the release predates manifest authentication")
    online = _step(report, "online", "restore", "new")
    assert (online["backup"], online["options"]) == ("post-rollback-state.tar.gz", [FLAG])
    turns = [phase["turn"] for phase in report["phases"]]
    assert [turn["messages_before"] for turn in turns] == [0, 2, 4, 4]
    assert [turn["messages_after"] for turn in turns] == [2, 4, 6, 6]


def _with_a_sandbox_file(state):
    sandbox = state / LAYOUT_1
    sandbox.mkdir(parents=True, exist_ok=True)
    (sandbox / "notes.txt").write_bytes(b"a file a tool of the old release wrote\n")


@pytest.fixture(scope="module")
def with_a_sandbox_file(older):
    return _real_drill(older, "sandbox-file", plant=_with_a_sandbox_file)


def test_the_candidate_migrates_a_layout_one_sandbox_and_the_old_release_rolls_back(
        older, with_a_sandbox_file):
    """A state whose sandbox holds a file: the candidate's dry run names the
    move, the migration makes it with a read-only pre-migration backup, the
    candidate serves the migrated state, the old release rolls back onto the
    pre-migration backup restored by its own tool, and the online backup holds
    the file, byte for byte, under its full-width name."""
    drill = with_a_sandbox_file
    report = drill.report
    _qualified(drill, older)
    change = report["state_migration"]
    assert change["declaration"] == MIGRATION_REQUIRED
    assert change["because"] == "the candidate's migration moved the old release's state"
    assert _steps(report, "upgrade") == [
        ("backup", "old"), ("restore", "new"), ("migrate --dry-run", "new"),
        ("migrate", "new")]
    planned = _step(report, "upgrade", "migrate --dry-run", "new")
    migrated = _step(report, "upgrade", "migrate", "new")
    assert planned["has_work"] is True
    assert planned["report"]["moves"] == migrated["report"]["moves"] == [
        {"from": LAYOUT_1, "to": LAYOUT_2}]
    backup = drill.work / "pre-migration-state.tar.gz"
    assert migrated["pre_migration_backup"]["sha256"] == release_drill._sha256(backup)
    assert stat.S_IMODE(backup.stat().st_mode) == 0o400
    assert (drill.work / "upgrade-state" / LAYOUT_2 / "notes.txt").is_file()
    rollback = _step(report, "rollback", "restore", "old")
    assert (rollback["backup"], rollback["state"]) == (
        "pre-migration-state.tar.gz", "rollback-state")
    assert (drill.work / "rollback-state" / LAYOUT_1 / "notes.txt").is_file()
    assert _step(report, "online", "migrate", "new")["report"]["moves"] == [
        {"from": LAYOUT_1, "to": LAYOUT_2}]
    online = report["online_backup"]
    assert online["continuity"]["preserved"] is True
    assert online["continuity"]["committed_customer_files"] >= 3
    with tarfile.open(drill.work / "online-backup.tar.gz", "r:gz") as archive:
        held = archive.extractfile(f"data/{LAYOUT_2}/notes.txt").read()
    assert held == b"a file a tool of the old release wrote\n"
