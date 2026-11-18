"""Independent persisted-state reader and fail-closed report augmentation."""
import hashlib
import json
import sqlite3
import subprocess
import sys

import pytest

from conftest import PI_ROOT
from scripts.release_drill import FIXTURE_SESSION, FIXTURE_TENANT, _session_files
from scripts.verify_recovery_state import ROW_MARKER, SCHEDULE_FIELDS, snapshot, verify_state


@pytest.mark.parametrize("layout,schema", [(1, 1), (2, 2)])
def test_snapshot_reads_persisted_files_and_schedule_fields(tmp_path, layout, schema):
    digest = _session_files(FIXTURE_TENANT, FIXTURE_SESSION)[0].split("/")[1].removesuffix(".kv")
    sandbox = tmp_path / "sandboxes" / (digest[:16] if layout == 1 else digest)
    sandbox.mkdir(parents=True)
    body = "persisted λ\n".encode()
    (sandbox / "first.txt").write_bytes(body)
    entry = {field: None for field in SCHEDULE_FIELDS}
    entry.update(name="daily", message='preserve "message"', every_ms=86400000,
                 last_run=1700000000, last_status="ok")
    if schema == 1:
        (tmp_path / "product-schedules.json").write_text(json.dumps({"key": entry}))
    else:
        (tmp_path / "product-schedules.json").write_text(json.dumps(ROW_MARKER))
        with sqlite3.connect(tmp_path / "product-quotas.sqlite3") as db:
            columns = [field for field in SCHEDULE_FIELDS if field != "message"]
            db.execute("CREATE TABLE schedules (key PRIMARY KEY, " + ", ".join(columns) + ")")
            db.execute("CREATE TABLE schedule_messages (key PRIMARY KEY, message)")
            db.execute("INSERT INTO schedules VALUES (" + ",".join("?" for _ in range(len(columns)+1))
                       + ")", ["key", *[entry[field] for field in columns]])
            db.execute("INSERT INTO schedule_messages VALUES (?, ?)", ["key", entry["message"]])
    found = snapshot(tmp_path, FIXTURE_TENANT, FIXTURE_SESSION)
    assert (found["sandbox_layout"], found["schedule_schema"]) == (layout, schema)
    assert found["sandbox_files"] == {"first.txt": {"size": len(body),
                                                   "sha256": hashlib.sha256(body).hexdigest()}}
    assert found["schedules"] == {"key": entry}


def test_state_verifier_never_qualifies_a_failed_drill(tmp_path):
    with pytest.raises(ValueError, match="not qualified"):
        verify_state({"qualification_eligible": False}, {}, tmp_path)


def test_verifier_cli_records_failure_before_report_can_be_signed(tmp_path):
    report = tmp_path / "report.json"
    fixture = tmp_path / "fixture.json"
    report.write_text(json.dumps({"qualification_eligible": True, "qualification_failures": []}))
    fixture.write_text("{}")
    result = subprocess.run([sys.executable, str(PI_ROOT / "scripts/verify_recovery_state.py"),
                             "--report", str(report), "--fixture", str(fixture),
                             "--work-dir", str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 1
    saved = json.loads(report.read_text())
    assert saved["qualification_eligible"] is False
    assert saved["state_fixture_verification"]["passed"] is False
    assert saved["qualification_failures"] == ["fixture state: 'migration_state'"]
