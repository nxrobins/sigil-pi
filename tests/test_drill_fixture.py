"""The drill's missing input: a fresh backup with a real committed session.

scripts/release_drill.py self-provisions everything except three inputs, and
two of those are trivial. The third — a state backup no older than 15 minutes
containing at least one COMMITTED SESSION whose signed audit chain verifies
under the drill's key — had no producer: the 2026-08-20 mechanics run used a
hand-made "fresh stopped-state fixture" that nothing can reproduce. Without a
producer, the qualifying distinct-version drill cannot run in CI, where there
is no human to hand-make anything.

The fixture is honest by construction: it boots the OLD release for real, runs
one real /v1/chat turn against a local Anthropic-shaped mock (state and
recovery are what the drill measures; the provider is not), drains cleanly,
and backs up with the old release's OWN state_tool — the ordering
docs/state-compatibility.md requires of a real operator.
"""

import json
import tarfile

import pytest

from conftest import PI_ROOT, needs_toolchain

import toolchain
from scripts.build_release import build_release
from scripts.drill_fixture import produce_backup


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    """A real bundle of the CURRENT tree against the pinned toolchain."""
    needs_toolchain()
    tmp = tmp_path_factory.mktemp("bundle")
    archive, _ = build_release(
        sigil_root=toolchain.resolve().stdlib_repo, output_dir=tmp,
        app_root=PI_ROOT, build_runtime=False)
    return archive


def test_fixture_backup_holds_a_committed_session_and_fresh_timestamp(
        bundle, tmp_path):
    key_file = tmp_path / "audit.key"
    key_file.write_text("drill-fixture-test-key-with-32-plus-bytes\n")
    key_file.chmod(0o600)
    backup = tmp_path / "state-backup.tar.gz"
    evidence = produce_backup(
        artifact=bundle, output=backup, audit_key_file=key_file,
        work_dir=tmp_path / "work")
    assert backup.is_file()
    with tarfile.open(backup, "r:gz") as archive:
        manifest = json.load(archive.extractfile("manifest.json"))
    paths = [entry["path"] for entry in manifest["files"]]
    assert any(p.startswith("sessions/") for p in paths), (
        "no committed session — the drill would refuse this backup as vacuous")
    assert any(p.startswith("audit/") for p in paths), (
        "no audit chain — restore validates signed chains and needs one to validate")
    assert evidence["committed_session_files"] >= 1
    assert evidence["backup_sha256"]
    assert manifest["created_unix"] >= evidence["started_unix"], (
        "the backup must be created AFTER the turn, or its age proves nothing")


def test_fixture_backup_restores_under_the_same_key(bundle, tmp_path):
    """The whole point: what the fixture produces, the drill can restore.
    state_tool restore validates every signed audit chain, so a fixture that
    signed with a different key than it reports would fail here, not mid-drill."""
    import subprocess
    import sys
    key_file = tmp_path / "audit.key"
    key_file.write_text("drill-fixture-test-key-with-32-plus-bytes\n")
    key_file.chmod(0o600)
    backup = tmp_path / "state-backup.tar.gz"
    produce_backup(artifact=bundle, output=backup, audit_key_file=key_file,
                   work_dir=tmp_path / "work")
    # Restore with the repo's own state_tool (same code the bundle carries).
    result = subprocess.run(
        [sys.executable, str(PI_ROOT / "state_tool.py"), "restore",
         "--backup", str(backup), "--state", str(tmp_path / "restored")],
        env={"PATH": "/usr/bin:/bin", "PI_AUDIT_KEY": key_file.read_text().strip()},
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-1500:]
    assert (tmp_path / "restored" / "sessions").is_dir()


def test_fixture_refuses_a_weak_or_shared_key(tmp_path):
    """The drill refuses group/other-readable keys; producing a backup under a
    key the drill will then refuse is a wasted boot. Fail at the same bar."""
    from scripts.drill_fixture import DrillFixtureError
    key_file = tmp_path / "audit.key"
    key_file.write_text("drill-fixture-test-key-with-32-plus-bytes\n")
    key_file.chmod(0o644)
    with pytest.raises(DrillFixtureError, match="group or other"):
        produce_backup(artifact=tmp_path / "unused.tar.gz",
                       output=tmp_path / "out.tar.gz",
                       audit_key_file=key_file, work_dir=tmp_path / "work")
