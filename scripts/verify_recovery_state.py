"""Check the fixture's files and schedules across the real recovery workspace.

The workflow adds these observations to its report before hashing and signing it.
This module reads persisted formats independently of the releases' migration code.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.release_drill import (  # noqa: E402
    ReleaseDrillError, _backup_payloads, _session_files, _sha256,
)

SCHEDULE_FIELDS = (
    "tenant_sha256", "principal", "name", "session", "internal_session", "message",
    "every_ms", "created_unix", "updated_unix", "last_run", "last_status",
    "lease_owner", "lease_until",
)
ROW_MARKER = ["sigil-pi schedule store schema 2: the schedules are rows of "
              "product-quotas.sqlite3 (docs/state-compatibility.md)"]


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _schedules(root):
    marker = json.loads((root / "product-schedules.json").read_text())
    if isinstance(marker, dict):
        schema, entries = 1, marker
    else:
        _require(marker == ROW_MARKER, "unrecognized schedule store marker")
        schema = 2
        with sqlite3.connect((root / "product-quotas.sqlite3").as_uri() + "?mode=ro",
                             uri=True) as db:
            db.row_factory = sqlite3.Row
            entries = {row["key"]: dict(row) for row in db.execute(
                "SELECT s.*, m.message FROM schedules s LEFT JOIN schedule_messages m "
                "ON m.key=s.key ORDER BY s.key")}
            _require(db.execute("SELECT COUNT(*) FROM schedule_messages").fetchone()[0]
                     == len(entries), "orphan or missing schedule messages")
    _require(entries and all(isinstance(e, dict) for e in entries.values()),
             "fixture has no persisted schedules")
    return schema, {key: {field: entry.get(field) for field in SCHEDULE_FIELDS}
                    for key, entry in sorted(entries.items())}


def snapshot(state, tenant, session):
    """The session's actual sandbox bytes and all persisted schedule fields."""
    root = Path(state).resolve()
    digest = Path(_session_files(tenant, session)[0]).stem
    directories = [(layout, root / "sandboxes" / name)
                   for layout, name in ((1, digest[:16]), (2, digest))]
    present = [(layout, path) for layout, path in directories if path.exists()]
    _require(len(present) == 1, "expected exactly one sandbox layout")
    layout, directory = present[0]
    _require(not directory.is_symlink(), "sandbox is a symlink")
    files = {}
    for path in sorted(directory.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        _require(stat.S_ISREG(mode), "sandbox contains a non-regular file")
        body = path.read_bytes()
        files[path.relative_to(directory).as_posix()] = {
            "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    _require(files and all(item["size"] > 0 for item in files.values()),
             "fixture has no nonempty sandbox files")
    schema, schedules = _schedules(root)
    return {"sandbox_layout": layout,
            "sandbox_directory": directory.relative_to(root).as_posix(),
            "sandbox_files": files, "schedule_schema": schema, "schedules": schedules}


def archive_snapshot(backup, tenant, session):
    _, payloads = _backup_payloads(backup)
    with tempfile.TemporaryDirectory(prefix="recovery-state-") as temporary:
        root = Path(temporary)
        for name, body in payloads.items():
            path = root / name  # _backup_payloads has checked every relative path.
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        return snapshot(root, tenant, session)


def verify_state(report, fixture, work_dir):
    _require(report.get("qualification_eligible") is True
             and not report.get("qualification_failures"), "recovery drill is not qualified")
    root = Path(work_dir).resolve()
    expected = fixture["migration_state"]
    tenant, session = fixture["tenant"], fixture["session"]
    _require(fixture["backup_sha256"] == report["backup"]["sha256"], "fixture backup mismatch")
    _require(fixture["artifact_sha256"] == report["artifacts"]["old"]["sha256"]
             and fixture["artifact_version"] == report["artifacts"]["old"]["version"],
             "fixture release mismatch")
    held = report["evaluation"]["continuity"]["session"]
    _require((held["tenant"], held["session"]) == (tenant, session), "fixture session mismatch")
    _require(len(expected["sandbox_files"]) >= 2 and len(expected["schedules"]) >= 2,
             "nonempty fixture requires multiple files and schedules")
    change = report["state_migration"]
    formats = change["formats"]["new"] or {}
    new_layout = formats.get("to_layout", expected["sandbox_layout"])
    new_schema = formats.get("schedule_store.to_schema", expected["schedule_schema"])
    changing = (new_layout, new_schema) != (expected["sandbox_layout"], expected["schedule_schema"])
    observations = []

    def check(name, found, layout, schema):
        _require((found["sandbox_layout"], found["schedule_schema"]) == (layout, schema),
                 f"{name}: wrong sandbox layout or schedule schema")
        _require(found["sandbox_files"] == expected["sandbox_files"], f"{name}: sandbox bytes changed")
        _require(found["schedules"] == expected["schedules"], f"{name}: schedules changed")
        observations.append({"phase": name, **found})

    for name, path, layout, schema in (
        ("restore_old", "restored-state", expected["sandbox_layout"], expected["schedule_schema"]),
        ("upgrade_new", "upgrade-state", new_layout, new_schema),
        ("rollback_old", "rollback-state" if changing else "upgrade-state",
         expected["sandbox_layout"] if changing else new_layout,
         expected["schedule_schema"] if changing else new_schema),
        ("backup_online_new", "online-state", new_layout, new_schema),
        ("restore_online_new", "online-restored-state", new_layout, new_schema),
    ):
        check(name, snapshot(root / path, tenant, session), layout, schema)
    check("online_archive", archive_snapshot(root / "online-backup.tar.gz", tenant, session),
          new_layout, new_schema)

    migrated = []
    if changing:
        _require(change["declaration"] == "state migration required", "missing migration declaration")
        for stage in ("upgrade", "online"):
            dry = [s for s in change[stage] if s["command"] == "migrate --dry-run"
                   and s["release"] == "new"]
            actual = [s for s in change[stage] if s["command"] == "migrate"]
            _require(len(dry) == len(actual) == 1 and dry[0]["has_work"] is True,
                     f"{stage}: nonempty migration did not execute")
            planned, moved = dry[0]["report"], actual[0]["report"]
            _require(planned["dry_run"] is True and moved["dry_run"] is False,
                     f"{stage}: migration was only a dry run")
            if new_layout != expected["sandbox_layout"]:
                move = {"from": expected["sandbox_directory"],
                        "to": "sandboxes/" + Path(_session_files(tenant, session)[0]).stem}
                _require(planned["moves"] == moved["moves"] == [move], f"{stage}: wrong sandbox move")
            if new_schema != expected["schedule_schema"]:
                for outcome in (planned, moved):
                    schedules = outcome["schedule_store"]
                    _require((schedules["from_schema"], schedules["to_schema"], schedules["schedules"])
                             == (expected["schedule_schema"], new_schema, len(expected["schedules"])),
                             f"{stage}: schedule migration missing rows")
                    _require("schedule-store-rows" in outcome["steps"], f"{stage}: no schedule step")
            before = actual[0]["pre_migration_backup"]
            path = root / before["backup"]
            _require(path.parent == root and path.is_file() and not path.is_symlink(),
                     f"{stage}: invalid pre-migration backup")
            _require(stat.S_IMODE(path.stat().st_mode) == 0o400, f"{stage}: backup is writable")
            _require(_sha256(path) == before["sha256"] == moved["backup_sha256"],
                     f"{stage}: backup digest mismatch")
            check(stage + "_pre_migration_backup", archive_snapshot(path, tenant, session),
                  expected["sandbox_layout"], expected["schedule_schema"])
            migrated.append({"stage": stage, "backup": before["backup"],
                             "sha256": before["sha256"], "read_only": True,
                             "moves": moved["moves"], "schedule_store": moved["schedule_store"]})
        rollback = [s for s in change["rollback"] if s["command"] == "restore"]
        _require(len(rollback) == 1 and rollback[0]["release"] == "old"
                 and (rollback[0]["backup"], rollback[0]["sha256"])
                 == (migrated[0]["backup"], migrated[0]["sha256"]),
                 "rollback did not restore the checked pre-migration backup")
    return {"passed": True, "fixture_backup_sha256": fixture["backup_sha256"],
            "artifacts": report["artifacts"], "legacy_state_migrated": changing,
            "migrations": migrated, "observations": observations}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    try:
        result = verify_state(report, json.loads(args.fixture.read_text()), args.work_dir)
        result["fixture_evidence_sha256"] = _sha256(args.fixture)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, ReleaseDrillError) as error:
        result = {"passed": False, "error": str(error)}
        report["qualification_eligible"] = False
        report.setdefault("qualification_failures", []).append("fixture state: " + str(error))
    report["state_fixture_verification"] = result
    args.report.write_text(json.dumps(report, sort_keys=True) + "\n")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
