"""The drills create their credentials and reports private, and a recovery drill that stops still reports (audit F-18).

The recovery drill, the failure drill and the drill fixture each wrote a
credentials file and then changed its mode, and the recovery drill wrote its
report the same way. Between the write and the change the file had whatever
mode the umask gave it (0644 by default): readable by every local user while
it held a chat credential in the customer's tenant. Each is now created with
mode 0600, and never through a path that already exists.

A recovery drill that stopped on anything but an OSError or its own error left
no report: a truncated backup archive raised EOFError and a manifest that is
not an object raised AttributeError or TypeError, straight out of the drill,
and the command then exited 1, the code that means "completed but not
qualification-eligible". The archive reader now refuses such a backup with
the drill's own error, and whatever stops a drill, the report is written and
the command exits 2.
"""

import gzip
import io
import json
import os
import stat
import sys
import tarfile
from pathlib import Path

import pytest

from conftest import PI_ROOT
from scripts import failure_drill, release_drill
from scripts.drill_fixture import DrillFixtureError, produce_backup
from scripts.release_drill import ReleaseDrillError, run_drill
from test_drill_configuration import _spawned_environments
from test_failure_drill import _artifact, _audit_key_file
from test_release_drill import _artifacts, _backup, _stand_ins

DRILLS = ("scripts/release_drill.py", "scripts/failure_drill.py", "scripts/drill_fixture.py")


@pytest.fixture(scope="module")
def releases(tmp_path_factory):
    return _artifacts(tmp_path_factory.mktemp("releases"))


@pytest.fixture
def modes(monkeypatch):
    """Run with no umask, so a file is created with exactly the mode its
    writer asked for, and record the mode every file had at the moment its
    mode was changed: what any local user could have opened until then."""
    seen = []
    real_chmod, real_fchmod = os.chmod, os.fchmod

    def chmod(path, mode, *args, **kwargs):
        if not isinstance(path, int):
            seen.append((str(path), stat.S_IMODE(os.stat(path).st_mode)))
        return real_chmod(path, mode, *args, **kwargs)

    def fchmod(descriptor, mode):
        seen.append((f"fd {descriptor}", stat.S_IMODE(os.fstat(descriptor).st_mode)))
        return real_fchmod(descriptor, mode)

    monkeypatch.setattr(os, "chmod", chmod)
    monkeypatch.setattr(os, "fchmod", fchmod)
    previous = os.umask(0)
    try:
        yield seen
    finally:
        os.umask(previous)


def _recovery_drill_credentials(tmp_path):
    path = tmp_path / "auth.json"
    release_drill._write_auth(path, token="operator", session_token="session",
                              tenant="a-tenant", now=0)
    return path


def _failure_drill_credentials(tmp_path):
    path = tmp_path / "auth.json"
    failure_drill._write_auth(path, "operator", 0)
    return path


def _fixture_credentials(tmp_path, monkeypatch):
    artifact, key = _artifact(tmp_path), _audit_key_file(tmp_path)
    _spawned_environments(monkeypatch)  # the release "exits" at once
    with pytest.raises(DrillFixtureError, match="exited during startup"):
        produce_backup(artifact=artifact, output=tmp_path / "backup.tar.gz",
                       audit_key_file=key, work_dir=tmp_path / "work")
    return tmp_path / "work" / "auth.json"


def _recovery_report(tmp_path):
    path = tmp_path / "recovery-drill.json"
    release_drill._write_report(path, {"schema_version": 1})
    return path


WRITERS = {
    "recovery drill credentials": lambda tmp_path, _: _recovery_drill_credentials(tmp_path),
    "failure drill credentials": lambda tmp_path, _: _failure_drill_credentials(tmp_path),
    "drill fixture credentials": _fixture_credentials,
    "recovery drill report": lambda tmp_path, _: _recovery_report(tmp_path),
}


@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_a_drill_file_is_private_from_the_moment_it_exists(
        tmp_path, monkeypatch, modes, writer):
    path = WRITERS[writer](tmp_path, monkeypatch)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # The file itself, a temporary it was written as first (whose name holds
    # its own), or a descriptor. A release file the drill installs is
    # written, then given its mode, and holds nothing private.
    exposed = [(name, oct(mode)) for name, mode in modes
               if (name.startswith("fd ") or path.name in Path(name).name) and mode & 0o077]
    assert not exposed, (
        f"the {writer} existed with mode {exposed[0][1]} before its mode was "
        f"changed to 0600: written first and made private after (audit F-18)")


@pytest.mark.parametrize("writer", ["recovery drill", "failure drill"])
def test_a_credentials_file_is_never_written_through_what_is_already_there(tmp_path, writer):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("not the drill's")
    elsewhere.chmod(0o644)
    (tmp_path / "auth.json").symlink_to(elsewhere)
    write = (_recovery_drill_credentials if writer == "recovery drill"
             else _failure_drill_credentials)
    with pytest.raises(FileExistsError):
        write(tmp_path)
    assert elsewhere.read_text() == "not the drill's", (
        f"the {writer} wrote its credentials through a link to another file")
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o644


def test_no_drill_makes_a_file_private_after_writing_it():
    """The class guard: a private file is created private."""
    for name in DRILLS:
        source = (PI_ROOT / name).read_text()
        assert "chmod(0o600)" not in source, (
            f"{name} writes a file and then makes it private; create it with "
            f"mode 0600 instead (release_drill._create_private)")


# ── a drill that stops reports ───────────────────────────────────────────

def _archive(manifest):
    data = json.dumps(manifest).encode() if not isinstance(manifest, bytes) else manifest
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as archive:
        member = tarfile.TarInfo("manifest.json")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    return out.getvalue()


CORRUPTIONS = {
    "truncated": lambda whole: whole[: len(whole) // 2],
    "manifest_is_a_list": lambda _: _archive([]),
    "entry_path_is_a_number": lambda _: _archive({"schema_version": 1, "files": [{"path": 7}]}),
    "entry_size_is_text": lambda _: _archive(
        {"schema_version": 1, "files": [{"path": "sessions/a.kv", "size": "big"}]}),
    "not_utf8_json": lambda _: _archive(b"\xff\xfe{}"),
    "gzip_of_nothing": lambda _: gzip.compress(b""),
}


@pytest.mark.parametrize("corruption", sorted(CORRUPTIONS))
def test_a_corrupt_backup_stops_the_drill_with_its_own_error_and_a_report(
        tmp_path, monkeypatch, releases, corruption):
    _stand_ins(monkeypatch)
    old, new = releases
    backup, key, _ = _backup(tmp_path)
    backup.write_bytes(CORRUPTIONS[corruption](backup.read_bytes()))
    output = tmp_path / "recovery-drill.json"
    with pytest.raises(ReleaseDrillError) as failure:
        run_drill(old_artifact=old, new_artifact=new, state_backup=backup,
                  audit_key_file=key, output=output, work_dir=tmp_path / "work")
    assert "state backup" in str(failure.value), failure.value
    assert output.is_file(), "a drill stopped by a corrupt backup left no report (audit F-18)"
    report = json.loads(output.read_text())
    assert report["qualification_eligible"] is False
    assert report["evaluation"]["failures"] == [str(failure.value)]
    assert report["phases"] == []


@pytest.mark.parametrize("fraction", [0.3, 0.99])
def test_a_truncated_release_archive_is_refused_with_the_drills_own_error(
        tmp_path, releases, fraction):
    """The three drills install releases with release_drill._install_artifact.
    A truncated archive ended in EOFError, which the failure drill and the
    fixture do not catch."""
    old, _ = releases
    cut = tmp_path / "truncated.tar.gz"
    whole = old.read_bytes()
    cut.write_bytes(whole[: int(len(whole) * fraction)])
    with pytest.raises(ReleaseDrillError, match="cannot install old artifact"):
        release_drill._install_artifact(cut, tmp_path / "releases", "old")
    assert not any((tmp_path / "releases").iterdir())


def _unexpected(monkeypatch):
    def broken(*_, **__):
        raise RuntimeError("an error the drill did not expect")

    monkeypatch.setattr(release_drill, "_continuity", broken)


def test_any_error_that_stops_the_drill_leaves_a_report_that_names_it(
        tmp_path, monkeypatch, releases):
    _stand_ins(monkeypatch)
    _unexpected(monkeypatch)
    old, new = releases
    backup, key, _ = _backup(tmp_path)
    output = tmp_path / "recovery-drill.json"
    with pytest.raises(RuntimeError, match="an error the drill did not expect"):
        run_drill(old_artifact=old, new_artifact=new, state_backup=backup,
                  audit_key_file=key, output=output, work_dir=tmp_path / "work")
    assert output.is_file(), (
        "a drill stopped by an error other than OSError or ReleaseDrillError left "
        "no report (audit F-18)")
    report = json.loads(output.read_text())
    reason = "RuntimeError: an error the drill did not expect"
    assert report["evaluation"]["failures"] == [reason]
    assert report["qualification_failures"] == [f"the drill did not complete: {reason}"]
    assert [phase["name"] for phase in report["phases"]] == [
        "clean_install", "restore_old", "upgrade_new", "rollback_old"]
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_the_command_exits_2_whatever_stops_the_drill(tmp_path, monkeypatch, releases, capsys):
    _stand_ins(monkeypatch)
    _unexpected(monkeypatch)
    old, new = releases
    backup, key, _ = _backup(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "release_drill.py", "--old-artifact", str(old), "--new-artifact", str(new),
        "--state-backup", str(backup), "--audit-key-file", str(key),
        "--work-dir", str(tmp_path / "work"), "--output", str(tmp_path / "report.json")])
    with pytest.raises(SystemExit) as stopped:
        release_drill.main()
    assert stopped.value.code == 2, (
        "a drill stopped by an unexpected error exited as one that completed "
        "and was not eligible")
    assert ("release drill failed: RuntimeError: an error the drill did not expect"
            in capsys.readouterr().err)
    assert (tmp_path / "report.json").is_file()


def test_the_failure_drill_command_exits_2_whatever_stops_it(tmp_path, monkeypatch, capsys):
    """The same exit code in the failure drill: 1 is a drill that completed
    and cannot qualify, so a drill stopped by an error nobody foresaw exits 2."""
    def broken(**_):
        raise RuntimeError("an error the drill did not expect")

    monkeypatch.setattr(failure_drill, "run_drill", broken)
    monkeypatch.setattr(sys, "argv", [
        "failure_drill.py", "--artifact", str(tmp_path / "release.tar.gz"),
        "--audit-key-file", str(tmp_path / "audit-key"),
        "--evidence-url", "https://example.invalid/run",
        "--output", str(tmp_path / "failure-injection.json")])
    with pytest.raises(SystemExit) as stopped:
        failure_drill.main()
    assert stopped.value.code == 2
    assert ("failure drill failed: RuntimeError: an error the drill did not expect"
            in capsys.readouterr().err)
