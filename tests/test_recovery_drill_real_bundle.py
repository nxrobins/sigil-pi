"""The recovery drill against real bundles of this tree (audit F-18).

tests/test_recovery_drill_turns.py drives the drill against stand-in releases,
which proves the drill's own logic and nothing about whether it fits the
product. This module builds two bundles on the pinned toolchain, the tree as
it is and the same tree under a later version number, produces the pre-upgrade
backup with scripts/drill_fixture.py as the `Recovery drill` workflow does, and
runs the whole drill: a restore by the old bundle's own state_tool and, in
every phase, a real turn through the embedded compiler against the drill's
stand-in provider.

Nothing is injected and nothing is shortened, so the report has to be
eligible to qualify and has to satisfy the readiness gate's validator. The
published releases are linux-x86_64, so the drill of those two archives is the
dispatch-only `Recovery drill` workflow's.
"""

from types import SimpleNamespace

import pytest

from conftest import PI_ROOT, needs_toolchain

import toolchain
from scripts.build_release import build_release
from scripts.check_readiness_evidence import _validate_recovery_report
from scripts.drill_fixture import produce_backup
from scripts.release_drill import (
    FIXTURE_SESSION,
    FIXTURE_TENANT,
    ONLINE_PHASES,
    SERVICE_SETTINGS,
    run_drill,
)
from test_release_drill import PHASES, _app_variant

CANDIDATE_VERSION = "9.9.9"


@pytest.fixture(scope="module")
def drill(tmp_path_factory):
    """One fixture and one drill for the module. Either raises its own error
    when it does not complete, so every test then reports that error and none
    fails on a report that was never whole."""
    needs_toolchain()
    tmp = tmp_path_factory.mktemp("recovery-drill")
    sigil = toolchain.resolve().stdlib_repo
    old, _ = build_release(
        sigil_root=sigil, output_dir=tmp / "old", app_root=PI_ROOT, build_runtime=False)
    new, _ = build_release(
        sigil_root=sigil, output_dir=tmp / "new", build_runtime=False,
        app_root=_app_variant(tmp, "candidate-tree", CANDIDATE_VERSION),
        pin_file=PI_ROOT / "SIGIL_REV")
    key = tmp / "audit.key"
    key.write_text("recovery-drill-real-bundle-key-with-32-plus-bytes\n")
    key.chmod(0o600)
    evidence = produce_backup(
        artifact=old, output=tmp / "pre-upgrade-backup.tar.gz", audit_key_file=key,
        work_dir=tmp / "fixture-work")
    report = run_drill(
        old_artifact=old, new_artifact=new, state_backup=tmp / "pre-upgrade-backup.tar.gz",
        audit_key_file=key, output=tmp / "recovery-drill.json", work_dir=tmp / "drill-work")
    return SimpleNamespace(report=report, evidence=evidence, directory=tmp)


def test_a_real_drill_qualifies_and_satisfies_the_readiness_validator(drill):
    report = drill.report
    assert report["qualification_failures"] == [] and report["qualification_eligible"] is True
    assert report["topology"]["real_service_probe"] is True
    assert report["artifacts"]["new"]["version"] == CANDIDATE_VERSION
    assert report["artifacts"]["old"]["version"] == (PI_ROOT / "VERSION").read_text().strip()
    assert _validate_recovery_report(
        drill.directory, artifact_sha256=report["artifacts"]["new"]["sha256"],
        version=CANDIDATE_VERSION, rollback_from=report["artifacts"]["old"]) == report


def test_every_phase_ran_a_real_turn_in_the_session_the_fixture_committed(drill):
    assert (drill.evidence["tenant"], drill.evidence["session"]) == (
        FIXTURE_TENANT, FIXTURE_SESSION)
    phases = drill.report["phases"]
    assert [phase["name"] for phase in phases] == list(PHASES)
    turns = [phase["turn"] for phase in phases]
    assert [turn["read_before_turn"] for turn in turns] == [False, True, True, True]
    # The fixture's turn left a message and a reply; each phase added two.
    assert [turn["messages_before"] for turn in turns] == [0, 2, 4, 6]
    assert [turn["messages_after"] for turn in turns] == [2, 4, 6, 8]
    assert [turn["history_messages_sent"] for turn in turns] == [0, 2, 4, 6]
    for turn in turns:
        assert turn["session"] == FIXTURE_SESSION and turn["provider_requests"] >= 1
        # A real turn leaves an audit record for each forge it ran.
        assert turn["audit_records_after"] >= turn["audit_records_before"] + 2


def test_the_state_after_the_rollback_holds_what_was_committed_and_every_turn(drill):
    continuity = drill.report["evaluation"]["continuity"]
    assert continuity["preserved"] is True and continuity["lost_or_changed"] == []
    session = continuity["session"]
    assert (session["tenant"], session["session"]) == (FIXTURE_TENANT, FIXTURE_SESSION)
    assert session["held_committed_content"] is True and session["preserved"] is True
    assert (session["committed_messages"], session["messages_after"]) == (2, 8)
    assert session["audit_records_after"] >= session["committed_audit_records"] + 6


def test_recovery_is_timed_to_the_first_real_turn(drill):
    evaluation = drill.report["evaluation"]
    restore, _, rollback = drill.report["phases"][1:]
    for phase in drill.report["phases"]:
        assert phase["ready_seconds"] < phase["first_turn_seconds"] < phase["total_seconds"]
        assert phase["turn"]["seconds"] > 0
    assert evaluation["restore_rto_seconds"] > restore["first_turn_seconds"]
    assert evaluation["rollback_seconds"] > rollback["first_turn_seconds"]
    assert evaluation["measured_to"] == "the first successful turn in the restored session"


def test_both_real_releases_report_the_settings_the_drill_gave_them(drill):
    configuration = drill.report["configuration"]
    assert configuration["settings"] == SERVICE_SETTINGS
    for release in ("old", "new"):
        in_force = configuration["effective_settings"][release]
        assert in_force is not None, f"the {release} release reported no effective settings"
        assert in_force["endpoint_host"] == "127.0.0.1"
        assert {name: str(in_force[name.removeprefix("PI_").lower()])
                for name in SERVICE_SETTINGS} == SERVICE_SETTINGS
        # Left to the release, as it ships them.
        assert (in_force["turn_lease_seconds"], in_force["turn_deadline_seconds"]) == (300, 120)


def test_the_candidate_restores_an_online_backup_it_took_while_turns_ran(drill):
    """Audit F-03: the 15-minute objective rests on online backups, so the
    candidate's own `state_tool.py backup --online` copies the state while
    the candidate serves turns, and the candidate starts on the archive,
    restored into a new path, and runs a turn in the session it holds."""
    report = drill.report
    online = report["online_backup"]
    assert online["mode"] == "online" and online["release"] == report["artifacts"]["new"]
    assert online["command"] == "state_tool.py backup --online"
    assert 0 <= online["age_at_restore_seconds"] <= report["thresholds"][
        "maximum_backup_age_seconds"]
    assert isinstance(online["max_session_lock_hold_ms"], (int, float))
    assert online["max_session_lock_hold_ms"] >= 0
    assert online["capture"]["sessions"] >= 1
    turns = online["turns_while_backing_up"]
    assert turns["in_flight_when_backup_started"] is True
    assert turns["turns"] >= 2 and turns["answered"] == turns["turns"]
    assert turns["overlapping_backup"] >= 1 and turns["held_by_backup"] >= 1
    assert online["continuity"]["preserved"] is True
    assert online["continuity"]["lost_or_changed"] == []
    assert [phase["name"] for phase in online["phases"]] == list(ONLINE_PHASES)
    serving, restored = (phase["turn"] for phase in online["phases"])
    # The candidate continued the session the rollback left (8 messages) ...
    assert (serving["read_before_turn"], serving["messages_before"],
            serving["messages_after"]) == (True, 8, 10)
    assert turns["messages_before"] == 10
    # ... and, restored from the online backup, the session as the archive
    # holds it: with the turn that was in flight when the backup started.
    held = online["session_in_backup"]["messages"]
    assert held == 10 + 2 * turns["held_by_backup"]
    assert (restored["read_before_turn"], restored["messages_before"],
            restored["history_messages_sent"], restored["messages_after"]) == (
        True, held, held, held + 2)
    assert restored["audit_records_after"] >= restored["audit_records_before"] + 2
    assert online["restore_rto_seconds"] > online["phases"][1]["first_turn_seconds"]
    assert online["measured_to"] == "the first successful turn in the restored session"


def test_each_phase_leaves_the_log_of_the_service_it_started(drill):
    logs = sorted((drill.directory / "drill-work" / "logs").iterdir())
    assert [log.name for log in logs] == [
        f"{number}-{name}-service.log"
        for number, name in enumerate((*PHASES, *ONLINE_PHASES), 1)]
    for log in logs:
        assert "/v1/chat" in log.read_text()
