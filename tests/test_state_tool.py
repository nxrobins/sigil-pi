"""Offline state backup/restore is validated, bounded, and fail-closed."""

import io
import json
import hashlib
import sqlite3
import sys
import tarfile
from contextlib import closing

import pytest

from agent import AuditLog, SessionStore
from product_service import DurableQuotaStore, _internal_session
import state_tool
from state_tool import (
    CLEAN_MARKER,
    StateToolError,
    create_backup,
    restore_backup,
    run_managed_backup_cycle,
)


AUDIT_KEY = b"backup-test-audit-key-with-32-bytes-minimum"


def _state(tmp_path):
    state = tmp_path / "state"
    internal = _internal_session("tenant-a", "project")
    store = SessionStore(state / "sessions")
    store.save(internal, [{"role": "user", "content": "committed"}])
    sandbox_name = hashlib.sha256(internal.encode()).hexdigest()[:16]
    sandbox = state / "sandboxes" / sandbox_name
    sandbox.mkdir(parents=True)
    (sandbox / "data.bin").write_bytes(b"sandbox-data")
    audit = AuditLog(state / "audit", key=AUDIT_KEY)
    audit.record(internal, "tool", "source", "input", "output", None, None, 1)
    (state / "product-schedules.json").write_text(json.dumps({"entry": {"name": "daily"}}))
    quota = DurableQuotaStore(
        state / "product-quotas.sqlite3", max_concurrent_turns=10,
        tokens_per_day=1000, token_reservation_per_turn=10,
        storage_bytes_per_tenant=1000, storage_reservation_per_turn=10,
        audit_bytes_per_tenant=1000, audit_reservation_per_turn=10,
        clock=lambda: 100.0)
    lease = quota.acquire_turn("tenant-a")
    lease.release(
        usage={"input_tokens": 2, "output_tokens": 3},
        session_id=internal, storage_bytes=100, audit_bytes=50)
    (state / CLEAN_MARKER).write_text(json.dumps({
        "schema_version": 1, "product_version": "1.0.0", "clean_shutdown_unix": 100,
    }))
    return state


def test_backup_and_restore_round_trip_committed_state(tmp_path):
    state = _state(tmp_path)
    backup = tmp_path / "state.tar.gz"
    manifest = create_backup(state, backup, audit_key=AUDIT_KEY, now=200)
    assert backup.stat().st_mode & 0o777 == 0o600
    assert manifest["audit_records"] == 1
    assert "product-quotas.sqlite3" in {entry["path"] for entry in manifest["files"]}

    restored = tmp_path / "restored"
    restored_manifest = restore_backup(backup, restored, audit_key=AUDIT_KEY)
    assert restored_manifest == manifest
    assert next((restored / "sessions").glob("*.kv")).read_bytes() == next(
        (state / "sessions").glob("*.kv")).read_bytes()
    sandbox_name = hashlib.sha256(_internal_session("tenant-a", "project").encode()).hexdigest()[:16]
    assert (restored / "sandboxes" / sandbox_name / "data.bin").read_bytes() == b"sandbox-data"
    restored_quota = DurableQuotaStore(
        restored / "product-quotas.sqlite3", max_concurrent_turns=10,
        tokens_per_day=1000, token_reservation_per_turn=10,
        storage_bytes_per_tenant=1000, storage_reservation_per_turn=10,
        audit_bytes_per_tenant=1000, audit_reservation_per_turn=10,
        clock=lambda: 100.0)
    usage = restored_quota.tenant_usage("tenant-a")
    assert usage["tokens_today"] == 5 and usage["storage_bytes"] == 100
    assert json.loads((restored / CLEAN_MARKER).read_text())["source_created_unix"] == 200


def test_backup_publication_never_overwrites_a_file_or_symlink(tmp_path):
    state = _state(tmp_path)
    output = tmp_path / "existing.tar.gz"
    output.write_bytes(b"preserve-me")
    with pytest.raises(StateToolError, match="refusing to overwrite"):
        create_backup(state, output, audit_key=AUDIT_KEY)
    assert output.read_bytes() == b"preserve-me"

    output.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    output.symlink_to(outside)
    with pytest.raises(StateToolError, match="refusing to overwrite"):
        create_backup(state, output, audit_key=AUDIT_KEY)
    assert outside.read_bytes() == b"outside"


def test_managed_backup_cycle_creates_valid_digest_names_and_expires_old_files(tmp_path):
    state = _state(tmp_path)
    backups = tmp_path / "managed"
    first = run_managed_backup_cycle(
        state, backups, audit_key=AUDIT_KEY, retention_s=100, now=1000)
    assert first["deleted"] == []
    assert first["created"]["created_new_file"] is True
    first_path = backups / first["created"]["name"]
    assert first_path.is_file() and first_path.stat().st_mode & 0o777 == 0o600
    assert first["created"]["name"] == (
        f"sigil-pi-state-0000001000-{first['created']['sha256']}.tar.gz")

    second = run_managed_backup_cycle(
        state, backups, audit_key=AUDIT_KEY, retention_s=100, now=1200)
    assert second["created"]["created_new_file"] is True
    assert second["deleted"] == [{
        "name": first["created"]["name"],
        "sha256": first["created"]["sha256"],
        "created_unix": 1000,
    }]
    assert not first_path.exists()
    assert (backups / second["created"]["name"]).is_file()

    repeated = run_managed_backup_cycle(
        state, backups, audit_key=AUDIT_KEY, retention_s=100, now=1200)
    assert repeated["created"]["created_new_file"] is False
    assert repeated["deleted"] == []


def test_managed_backup_retention_fails_before_deleting_tampered_or_linked_files(tmp_path):
    state = _state(tmp_path)
    backups = tmp_path / "managed"
    first = run_managed_backup_cycle(
        state, backups, audit_key=AUDIT_KEY, retention_s=100, now=1000)
    first_path = backups / first["created"]["name"]
    first_path.write_bytes(first_path.read_bytes() + b"tampered")
    with pytest.raises(StateToolError, match="digest does not match"):
        run_managed_backup_cycle(
            state, backups, audit_key=AUDIT_KEY, retention_s=100, now=1200)
    assert first_path.exists()

    first_path.unlink()
    outside = tmp_path / "outside-backup"
    outside.write_bytes(b"outside")
    linked = backups / ("sigil-pi-state-0000001000-" + "0" * 64 + ".tar.gz")
    linked.symlink_to(outside)
    with pytest.raises(StateToolError, match="private regular file"):
        run_managed_backup_cycle(
            state, backups, audit_key=AUDIT_KEY, retention_s=100, now=1300)
    assert outside.read_bytes() == b"outside"


def test_managed_backup_cycle_requires_explicit_positive_policy_and_private_directory(
        tmp_path):
    state = _state(tmp_path)
    with pytest.raises(StateToolError, match="positive integer"):
        run_managed_backup_cycle(
            state, tmp_path / "managed", audit_key=AUDIT_KEY,
            retention_s=0, now=1000)
    backups = tmp_path / "open-managed"
    backups.mkdir(mode=0o755)
    backups.chmod(0o755)
    with pytest.raises(StateToolError, match="group or other"):
        run_managed_backup_cycle(
            state, backups, audit_key=AUDIT_KEY,
            retention_s=100, now=1000)


def test_backup_requires_clean_drain_and_valid_audit_chain(tmp_path):
    state = _state(tmp_path)
    (state / CLEAN_MARKER).unlink()
    with pytest.raises(StateToolError, match="clean-shutdown"):
        create_backup(state, tmp_path / "bad.tar.gz", audit_key=AUDIT_KEY)

    (state / CLEAN_MARKER).write_text(json.dumps({"schema_version": 1}))
    audit = next((state / "audit").glob("*.jsonl"))
    audit.write_text(audit.read_text().replace("source", "tampered"))
    with pytest.raises(StateToolError, match="audit verification failed"):
        create_backup(state, tmp_path / "bad.tar.gz", audit_key=AUDIT_KEY)


def test_backup_refuses_state_symlinks_before_reading_them(tmp_path):
    state = _state(tmp_path)
    outside = tmp_path / "outside-secret"
    outside.write_text("must-not-be-backed-up")
    sandbox = next((state / "sandboxes").iterdir())
    (sandbox / "link").symlink_to(outside)
    with pytest.raises(StateToolError, match="refuses symlink"):
        create_backup(state, tmp_path / "bad.tar.gz", audit_key=AUDIT_KEY)


def test_backup_rejects_corrupt_quota_state_and_active_leases(tmp_path):
    state = _state(tmp_path)
    quota = state / "product-quotas.sqlite3"
    quota.write_bytes(b"not sqlite")
    with pytest.raises(StateToolError, match="quota database is corrupt"):
        create_backup(state, tmp_path / "bad.tar.gz", audit_key=AUDIT_KEY)

    state = _state(tmp_path / "active")
    store = DurableQuotaStore(state / "product-quotas.sqlite3")
    lease = store.acquire_turn("tenant-a")
    with pytest.raises(StateToolError, match="active turn leases"):
        create_backup(state, tmp_path / "active.tar.gz", audit_key=AUDIT_KEY)
    lease.release()


def test_backup_requires_complete_valid_quota_registry(tmp_path):
    state = _state(tmp_path)
    quota = state / "product-quotas.sqlite3"
    quota.unlink()
    with pytest.raises(StateToolError, match="no durable quota registry"):
        create_backup(state, tmp_path / "missing.tar.gz", audit_key=AUDIT_KEY)

    state = _state(tmp_path / "invalid")
    with closing(sqlite3.connect(state / "product-quotas.sqlite3")) as db:
        db.execute("UPDATE session_usage SET internal_session='bad'")
        db.commit()
    with pytest.raises(StateToolError, match="invalid session registry"):
        create_backup(state, tmp_path / "invalid.tar.gz", audit_key=AUDIT_KEY)

    state = _state(tmp_path / "negative-activity")
    with closing(sqlite3.connect(state / "product-quotas.sqlite3")) as db:
        db.execute("UPDATE session_usage SET last_activity_unix=-1")
        db.commit()
    with pytest.raises(StateToolError, match="invalid usage records"):
        create_backup(
            state, tmp_path / "negative-activity.tar.gz", audit_key=AUDIT_KEY)


def test_restore_rejects_traversal_and_existing_target(tmp_path):
    malicious = tmp_path / "malicious.tar.gz"
    with tarfile.open(malicious, "w:gz") as archive:
        data = b"bad"
        info = tarfile.TarInfo("../outside")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    with pytest.raises(StateToolError, match="unsafe backup path"):
        restore_backup(malicious, tmp_path / "restored", audit_key=AUDIT_KEY)
    assert not (tmp_path / "outside").exists()

    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(StateToolError, match="refusing to overwrite"):
        restore_backup(malicious, existing, audit_key=AUDIT_KEY)


def test_restore_detects_content_tampering(tmp_path):
    state = _state(tmp_path)
    original = tmp_path / "original.tar.gz"
    create_backup(state, original, audit_key=AUDIT_KEY)
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(original, "r:gz") as source, tarfile.open(tampered, "w:gz") as target:
        for member in source.getmembers():
            data = source.extractfile(member).read()
            if member.name.startswith("data/sandboxes/"):
                data = b"changed-data"
                member.size = len(data)
            target.addfile(member, io.BytesIO(data))
    with pytest.raises(StateToolError, match="checksum failed"):
        restore_backup(tampered, tmp_path / "restored", audit_key=AUDIT_KEY)


@pytest.mark.parametrize("corruption,match", [
    ("marker-json", "marker is invalid"),
    ("marker-schema", "marker schema is unsupported"),
    ("session-json", "conversation state is corrupt"),
    ("session-shape", "not a message list"),
    ("schedule-json", "schedule state is corrupt"),
    ("schedule-shape", "schedule state is not an object"),
])
def test_backup_rejects_corrupt_structured_state(tmp_path, corruption, match):
    state = _state(tmp_path)
    marker = state / CLEAN_MARKER
    session = next((state / "sessions").glob("*.kv"))
    schedule = state / "product-schedules.json"
    if corruption == "marker-json":
        marker.write_text("{")
    elif corruption == "marker-schema":
        marker.write_text(json.dumps({"schema_version": 999}))
    elif corruption == "session-json":
        session.write_text("{")
    elif corruption == "session-shape":
        session.write_text("{}")
    elif corruption == "schedule-json":
        schedule.write_text("{")
    else:
        schedule.write_text("[]")
    with pytest.raises(StateToolError, match=match):
        create_backup(state, tmp_path / "bad.tar.gz", audit_key=AUDIT_KEY)


def test_backup_rejects_invalid_roots_keys_and_bounds(tmp_path):
    with pytest.raises(StateToolError, match="does not exist"):
        create_backup(tmp_path / "missing", tmp_path / "bad.tar.gz", audit_key=AUDIT_KEY)

    state = _state(tmp_path)
    with pytest.raises(StateToolError, match="verification key"):
        create_backup(state, tmp_path / "bad.tar.gz", audit_key=b"")
    with pytest.raises(StateToolError, match="max_bytes must be positive"):
        create_backup(state, tmp_path / "bad.tar.gz", audit_key=AUDIT_KEY, max_bytes=0)
    with pytest.raises(StateToolError, match="backup size limit"):
        create_backup(state, tmp_path / "bad.tar.gz", audit_key=AUDIT_KEY, max_bytes=1)

    schedule = state / "product-schedules.json"
    schedule.unlink()
    schedule.symlink_to(tmp_path / "outside-schedule")
    with pytest.raises(StateToolError, match="must be a regular file"):
        create_backup(state, tmp_path / "bad.tar.gz", audit_key=AUDIT_KEY)


def _write_archive(path, members):
    with tarfile.open(path, "w:gz") as archive:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                info.size = 0
                archive.addfile(info)
            else:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))


def _manifest(files, schema=1):
    return json.dumps({"schema_version": schema, "files": files}).encode()


@pytest.mark.parametrize("members,max_bytes,match", [
    ([("other", b"x", "file")], 1000, "manifest is missing"),
    ([("manifest.json", b"{", "file")], 1000, "manifest is invalid"),
    ([("manifest.json", _manifest([], 2), "file")], 1000, "schema is unsupported"),
    ([("manifest.json", json.dumps({"schema_version": 1, "files": {}}).encode(), "file")],
     1000, "file list is invalid"),
    ([("manifest.json", _manifest([None]), "file")], 1000, "entry is invalid"),
    ([("manifest.json", _manifest([{"path": None}]), "file")], 1000,
     "unsafe backup path"),
    ([("manifest.json", _manifest([]), "file"), ("data/extra", b"x", "file")],
     1000, "undeclared data"),
    ([("manifest.json", _manifest([]), "file"), ("directory", b"", "dir")],
     1000, "non-regular member"),
    ([("manifest.json", _manifest([]), "file")], 1, "restore size limit"),
])
def test_restore_rejects_malformed_archive_contract(tmp_path, members, max_bytes, match):
    archive = tmp_path / "invalid.tar.gz"
    _write_archive(archive, members)
    with pytest.raises(StateToolError, match=match):
        restore_backup(
            archive, tmp_path / "restored", audit_key=AUDIT_KEY, max_bytes=max_bytes)


def test_restore_rejects_repeated_manifest_path_and_wrong_size(tmp_path):
    data = b"value"
    digest = hashlib.sha256(data).hexdigest()
    entry = {"path": "sessions/a.kv", "size": len(data), "sha256": digest}
    repeated = tmp_path / "repeated.tar.gz"
    _write_archive(repeated, [
        ("manifest.json", _manifest([entry, entry]), "file"),
        ("data/sessions/a.kv", data, "file"),
    ])
    with pytest.raises(StateToolError, match="repeats a path"):
        restore_backup(repeated, tmp_path / "one", audit_key=AUDIT_KEY)

    wrong = dict(entry, size=999)
    wrong_size = tmp_path / "wrong.tar.gz"
    _write_archive(wrong_size, [
        ("manifest.json", _manifest([wrong]), "file"),
        ("data/sessions/a.kv", data, "file"),
    ])
    with pytest.raises(StateToolError, match="wrong size"):
        restore_backup(wrong_size, tmp_path / "two", audit_key=AUDIT_KEY)


def test_state_tool_cli_backup_restore_and_safe_error(tmp_path, monkeypatch, capsys):
    state = _state(tmp_path)
    backup = tmp_path / "cli.tar.gz"
    monkeypatch.setenv("PI_AUDIT_KEY", AUDIT_KEY.decode())
    monkeypatch.setattr(sys, "argv", [
        "state_tool.py", "backup", "--state", str(state), "--output", str(backup)])
    state_tool.main()
    assert json.loads(capsys.readouterr().out)["schema_version"] == 1

    managed = tmp_path / "managed"
    monkeypatch.setattr(sys, "argv", [
        "state_tool.py", "backup-cycle", "--state", str(state),
        "--backup-dir", str(managed), "--retention-days", "30"])
    state_tool.main()
    cycle = json.loads(capsys.readouterr().out)
    assert cycle["schema_version"] == 1
    assert cycle["retention_seconds"] == 30 * 86400
    assert (managed / cycle["created"]["name"]).is_file()

    restored = tmp_path / "cli-restored"
    monkeypatch.setattr(sys, "argv", [
        "state_tool.py", "restore", "--backup", str(backup), "--state", str(restored)])
    state_tool.main()
    assert json.loads(capsys.readouterr().out)["schema_version"] == 1

    monkeypatch.delenv("PI_AUDIT_KEY")
    monkeypatch.setattr(sys, "argv", [
        "state_tool.py", "backup", "--state", str(state), "--output", str(backup)])
    with pytest.raises(SystemExit) as error:
        state_tool.main()
    assert error.value.code == 2
    assert "verification key" in capsys.readouterr().err
