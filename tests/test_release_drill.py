"""Release recovery drills are safe, digest-bound, and anti-vacuous."""

import io
import json
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

from agent import AuditLog, SessionStore
from conftest import PI_ROOT
from product_service import DurableQuotaStore, _internal_session
from scripts.build_release import APP_FILES, DOC_FILES, build_release
from scripts.release_drill import ReleaseDrillError, _install_artifact, run_drill
from state_tool import CLEAN_MARKER, create_backup


AUDIT_KEY = "release-drill-test-audit-key-with-sufficient-entropy"


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True,
        capture_output=True, text=True).stdout.strip()


def _fake_sigil(tmp_path):
    root = tmp_path / "SIGIL"
    (root / "crates").mkdir(parents=True)
    (root / "crates" / "runtime.txt").write_text("runtime source")
    stdlib = root / "stdlib" / "sigil"
    stdlib.mkdir(parents=True)
    for name in ("http", "json", "kv"):
        (stdlib / f"{name}.sigil").write_text(f"module sigil::{name};\n")
    binary = root / "target" / "release" / "sigil-mcp"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"fake immutable runtime")
    binary.chmod(0o755)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "crates", "stdlib"], check=True)
    subprocess.run([
        "git", "-C", str(root), "-c", "user.name=test", "-c",
        "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
    pin = tmp_path / "SIGIL_REV"
    pin.write_text(
        f"crates = {_git(root, 'rev-parse', 'HEAD:crates')}\n"
        f"stdlib = {_git(root, 'rev-parse', 'HEAD:stdlib')}\n"
        f"ref = {_git(root, 'rev-parse', 'HEAD')}\n")
    return root, pin


def _app_variant(tmp_path, name, version):
    root = tmp_path / name
    for relative in (
            *APP_FILES, *DOC_FILES,
            "config/auth.example.json", "config/alert-policy.json",
            "config/grafana-slo-dashboard.json"):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PI_ROOT / relative, destination)
    for source in (PI_ROOT / "tools").iterdir():
        if source.is_file() and source.suffix in (".sigil", ".json"):
            destination = root / "tools" / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    (root / "VERSION").write_text(version + "\n")
    return root


def _artifacts(tmp_path):
    sigil, pin = _fake_sigil(tmp_path)
    old = build_release(
        sigil_root=sigil, output_dir=tmp_path / "old-out",
        app_root=_app_variant(tmp_path, "old-app", "0.9.0"),
        pin_file=pin, build_runtime=False, platform_tag="test")[0]
    new = build_release(
        sigil_root=sigil, output_dir=tmp_path / "new-out",
        app_root=_app_variant(tmp_path, "new-app", "1.0.0"),
        pin_file=pin, build_runtime=False, platform_tag="test")[0]
    return old, new


def _backup(tmp_path):
    state = tmp_path / "source-state"
    internal = _internal_session("tenant-a", "project")
    SessionStore(state / "sessions").save(
        internal, [{"role": "user", "content": "committed before upgrade"}])
    audit = AuditLog(state / "audit", key=AUDIT_KEY.encode())
    audit.record(internal, "tool", "source", "input", "output", None, None, 1)
    quota = DurableQuotaStore(
        state / "product-quotas.sqlite3",
        max_concurrent_turns=10,
        tokens_per_day=1000,
        token_reservation_per_turn=10,
        storage_bytes_per_tenant=10000,
        storage_reservation_per_turn=10,
        audit_bytes_per_tenant=10000,
        audit_reservation_per_turn=10)
    lease = quota.acquire_turn("tenant-a")
    lease.release(
        session_id=internal, usage={"input_tokens": 1, "output_tokens": 1},
        storage_bytes=SessionStore(state / "sessions")._path(internal).stat().st_size,
        audit_bytes=audit._path(internal).stat().st_size)
    now = int(time.time())
    (state / CLEAN_MARKER).write_text(json.dumps({
        "schema_version": 1,
        "product_version": "0.9.0",
        "clean_shutdown_unix": now,
    }))
    backup = tmp_path / "state.tar.gz"
    create_backup(state, backup, audit_key=AUDIT_KEY.encode(), now=now)
    key = tmp_path / "audit-key"
    key.write_text(AUDIT_KEY)
    key.chmod(0o600)
    return backup, key, now


def _injected_probe(release, state, auth_file, token, audit_key):
    assert Path(auth_file).stat().st_mode & 0o077 == 0
    assert token and audit_key == AUDIT_KEY
    state.mkdir(parents=True, exist_ok=True)
    marker = state / CLEAN_MARKER
    if not marker.exists():
        marker.write_text(json.dumps({
            "schema_version": 1,
            "product_version": (release / "app" / "VERSION").read_text().strip(),
            "clean_shutdown_unix": int(time.time()),
        }))
    return {
        "version": (release / "app" / "VERSION").read_text().strip(),
        "ready_seconds": 0.001,
        "total_seconds": 0.002,
    }


def test_drill_installs_switches_restores_rolls_back_and_preserves_state(tmp_path):
    old, new = _artifacts(tmp_path)
    backup, key, now = _backup(tmp_path)
    output = tmp_path / "report.json"
    report = run_drill(
        old_artifact=old, new_artifact=new, state_backup=backup,
        audit_key_file=key, output=output, work_dir=tmp_path / "work",
        probe=_injected_probe, now_fn=lambda: now)

    assert report["evaluation"]["mechanics_passed"] is True
    assert report["evaluation"]["continuity"] == {
        "committed_customer_files": 2,
        "committed_session_files": 1,
        "lost_or_changed": [],
        "preserved": True,
    }
    assert [phase["name"] for phase in report["phases"]] == [
        "clean_install", "restore_old", "upgrade_new", "rollback_old"]
    assert report["artifacts"]["old"]["version"] == "0.9.0"
    assert report["artifacts"]["new"]["version"] == "1.0.0"
    assert report["qualification_eligible"] is False
    assert report["qualification_failures"] == [
        "probe was injected rather than the real service"]
    assert json.loads(output.read_text()) == report
    assert output.stat().st_mode & 0o077 == 0
    assert (tmp_path / "work" / "current").resolve().name.startswith("old-")


def test_artifact_install_rejects_traversal_and_manifest_tampering(tmp_path):
    traversal = tmp_path / "traversal.tar.gz"
    with tarfile.open(traversal, "w:gz") as archive:
        data = b"bad"
        member = tarfile.TarInfo("../outside")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    with pytest.raises(ReleaseDrillError, match="unsafe archive path"):
        _install_artifact(traversal, tmp_path / "releases", "bad")
    assert not (tmp_path / "outside").exists()

    old, _ = _artifacts(tmp_path / "fixture")
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(old, "r:gz") as source, tarfile.open(tampered, "w:gz") as target:
        for member in source.getmembers():
            stream = source.extractfile(member) if member.isreg() else None
            data = stream.read() if stream is not None else None
            if member.name.endswith("/app/VERSION"):
                data = b"9.9.9\n"
                member.size = len(data)
            target.addfile(member, io.BytesIO(data) if data is not None else None)
    with pytest.raises(ReleaseDrillError, match="checksum failed"):
        _install_artifact(tampered, tmp_path / "releases", "tampered")


@pytest.mark.parametrize("mode,match", [
    (0o644, "group or other"),
    (0o600, "overwrite"),
])
def test_drill_fails_closed_on_private_input_or_output_contract(tmp_path, mode, match):
    old, new = _artifacts(tmp_path)
    backup, key, now = _backup(tmp_path)
    output = tmp_path / "report.json"
    if mode == 0o644:
        key.chmod(mode)
    else:
        output.write_text("existing evidence")
    with pytest.raises(ReleaseDrillError, match=match):
        run_drill(
            old_artifact=old, new_artifact=new, state_backup=backup,
            audit_key_file=key, output=output, probe=_injected_probe,
            now_fn=lambda: now)
