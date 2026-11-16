"""The recovery drill restores an online backup the candidate took while turns ran (audit F-03).

A backup needed the service stopped, and on one worker a stop every 15 minutes
does not fit in a 99.9% availability budget (F-03). state_tool.py gained an
online mode, but the recovery drill went on restoring only its input, a backup
the fixture takes with the service stopped. So the 15-minute objective rested
on a path production cannot use (F-18: the drill proved less than its report
said).

After its four phases the drill now restores the state the rollback left with
the candidate's own state tool, starts the candidate on it and runs a turn,
and then, while the candidate serves turns in the session, has the candidate's
`state_tool.py backup --online` copy that live state from another process. A
turn is held in flight at the stand-in provider until the backup has been
started, and turns go on until it ends. The archive is restored into a new
path, the candidate starts on it, reads the session exactly as the archive
holds it and runs a turn there. The report records the backup's mode, its age
when its restore began and the longest time it held a session's lock.

These tests drive the drill against stand-in releases (tests/test_release_drill.py)
whose live state the real state tool backs up from its own process;
tests/test_recovery_drill_real_bundle.py runs it against real bundles.
"""

import json
import subprocess
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_release_drill
from agent import SessionStore
from product_service import _internal_session
from scripts import release_drill
from scripts.build_release import build_release
from scripts.check_readiness_evidence import _validate_recovery_report
from scripts.release_drill import ReleaseDrillError, run_drill
from state_tool import DEFAULT_LOCK_WAIT_SECONDS
from test_drill_configuration import _shipped_budgets
from test_release_drill import (
    BYSTANDER,
    COMMITTED,
    PHASES,
    SESSION,
    TENANT,
    TURN_FAULTS,
    _app_variant,
    _artifacts,
    _backup,
    _fake_sigil,
    _stand_ins,
)

ONLINE_PHASES = ("backup_online_new", "restore_online_new")
# The routes a stand-in logs, as the product names them.
EXPORT = "GET /v1/sessions/{session}/export"
THE_TURN = [EXPORT, "POST /v1/chat", EXPORT]
# What the four phases leave in the session (tests/test_recovery_drill_turns.py):
# the fixture's turn and one turn for each phase on the restored state.
AFTER_ROLLBACK = {"messages": 8, "audit_records": 4}


@pytest.fixture(scope="module")
def releases(tmp_path_factory):
    """The old (0.9.0) and new (1.0.0) archives, built once."""
    return _artifacts(tmp_path_factory.mktemp("releases"))


def _run(tmp_path, releases, *, output="report.json", **arguments):
    old, new = releases
    backup, key, _ = _backup(tmp_path)
    return run_drill(
        old_artifact=old, new_artifact=new, state_backup=backup, audit_key_file=key,
        output=tmp_path / output, work_dir=tmp_path / "work", **arguments)


def _observe(monkeypatch):
    """Record, independently of the drill, when each stand-in turn ran (inside
    the session lock its service takes) and every state tool command the drill
    started, with the moment it started."""
    observed = SimpleNamespace(turns=[], tools=[])
    real_turn = test_release_drill._StandInAgent.turn_with_usage

    def turn(self, session, message, **kwargs):
        started = time.monotonic()
        try:
            return real_turn(self, session, message, **kwargs)
        finally:
            observed.turns.append(SimpleNamespace(
                message=message, started=started, ended=time.monotonic()))

    monkeypatch.setattr(test_release_drill._StandInAgent, "turn_with_usage", turn)
    inner = subprocess.Popen

    def popen(command, *args, **kwargs):
        process = inner(command, *args, **kwargs)
        launched = time.monotonic()  # the process exists
        if len(command) > 2 and Path(str(command[1])).name == "state_tool.py":
            observed.tools.append(SimpleNamespace(
                release=Path(str(command[1])).parent.parent.name.split("-")[0],
                arguments=[str(part) for part in command[2:]], launched=launched))
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    return observed


def _faults_after(monkeypatch):
    """Let a stand-in fault begin part way through a start: a start told
    "after:N:<fault>" gets <fault> from its (N+1)th turn on. The first turn of
    the candidate's online start is the phase's own; the turns after it run
    while the backup is taken."""
    real_turn = test_release_drill._StandInAgent.turn_with_usage

    def turn(self, session, message, **kwargs):
        done = getattr(self, "turns_done", 0)
        for fault in sorted(self.faults):
            if fault.startswith("after:"):
                _, count, name = fault.split(":", 2)
                if done >= int(count):
                    self.faults = self.faults | {name}
        self.turns_done = done + 1
        return real_turn(self, session, message, **kwargs)

    monkeypatch.setattr(test_release_drill._StandInAgent, "turn_with_usage", turn)


def _manifest(archive):
    with tarfile.open(archive, "r:gz") as opened:
        return json.load(opened.extractfile("manifest.json"))


def _conversation(state, tenant=TENANT, session=SESSION):
    return SessionStore(Path(state) / "sessions").load(_internal_session(tenant, session))


# ── the online backup, its restore and a turn on it ──────────────────────

def test_the_drill_restores_an_online_backup_the_candidate_took_while_turns_ran(
        tmp_path, monkeypatch, releases):
    stand_ins = _stand_ins(monkeypatch)
    observed = _observe(monkeypatch)
    report = _run(tmp_path, releases, output="recovery-drill.json")
    online = report.get("online_backup")
    assert online is not None, (
        "the drill restored only its input, a backup the fixture took with the "
        "service stopped: nothing took an online backup while turns ran and "
        "restored it, so the 15-minute objective rests on a path production "
        f"cannot use; the report holds {sorted(report)} (audit F-03)")
    work = tmp_path / "work"
    new = report["artifacts"]["new"]

    # Seen from outside the drill: the candidate's own state tool restored the
    # old release's backup of the state the rollback left into a path of its
    # own, dry-ran its migration there, backed that state up online while the
    # candidate served it, and restored the archive into another new path, in
    # that order and after the four phases. Before them, the upgrade as
    # docs/state-compatibility.md has it: the old release's pre-upgrade backup,
    # each state tool's commands read from its --help, and the candidate's
    # restore into a new path, where it dry-ran its migration. Both releases
    # are of this tree, so nothing needed migrating and the rollback ran on the
    # state the candidate left (tests/test_recovery_drill_state_migration.py).
    commands = [(tool.release, tool.arguments) for tool in observed.tools]
    assert commands == [
        ("old", ["restore", "--backup", str(tmp_path / "state.tar.gz"),
                 "--state", str(work / "restored-state")]),
        ("old", ["backup", "--state", str(work / "restored-state"),
                 "--output", str(work / "pre-upgrade-state.tar.gz")]),
        ("old", ["migrate", "--help"]),
        ("old", ["restore", "--help"]),
        ("new", ["migrate", "--help"]),
        ("new", ["restore", "--help"]),
        ("old", ["migrate", "--state", str(work / "restored-state"), "--dry-run"]),
        ("new", ["restore", "--backup", str(work / "pre-upgrade-state.tar.gz"),
                 "--state", str(work / "upgrade-state")]),
        ("new", ["migrate", "--state", str(work / "upgrade-state"), "--dry-run"]),
        ("old", ["backup", "--state", str(work / "upgrade-state"),
                 "--output", str(work / "post-rollback-state.tar.gz")]),
        ("new", ["restore", "--backup", str(work / "post-rollback-state.tar.gz"),
                 "--state", str(work / "online-state")]),
        ("new", ["migrate", "--state", str(work / "online-state"), "--dry-run"]),
        ("new", ["backup", "--online", "--state", str(work / "online-state"),
                 "--output", str(work / "online-backup.tar.gz")]),
        ("new", ["restore", "--backup", str(work / "online-backup.tar.gz"),
                 "--state", str(work / "online-restored-state")]),
    ], commands
    assert [boot.version for boot in stand_ins.boots] == [
        "0.9.0", "0.9.0", "1.0.0", "0.9.0", "1.0.0", "1.0.0"]
    serving, restored = stand_ins.boots[4:]
    assert (serving.state, restored.state) == (work / "online-state",
                                               work / "online-restored-state")
    # The backup started while one of the candidate's turns held the session.
    [backup] = [tool for tool in observed.tools if "--online" in tool.arguments]
    load = [turn for turn in observed.turns if "while the backup runs" in turn.message]
    assert any(turn.started < backup.launched < turn.ended for turn in load), (
        "no turn of the candidate was in flight when its online backup started")
    turns = online["turns_while_backing_up"]
    assert serving.routes(TENANT) == THE_TURN + ["POST /v1/chat"] * turns["turns"] + [EXPORT]
    assert restored.routes(TENANT) == THE_TURN

    # What the archive holds, read here and not through the drill.
    archive = work / "online-backup.tar.gz"
    manifest = _manifest(archive)
    assert manifest["mode"] == "online" and manifest["product_version"] is None
    assert online["mode"] == "online"
    assert online["release"] == new
    assert online["command"] == "state_tool.py backup --online"
    assert online["sha256"] == release_drill._sha256(archive)
    assert online["created_unix"] == manifest["created_unix"]
    assert online["capture"] == manifest["online"]
    assert online["max_session_lock_hold_ms"] == manifest["online"]["max_session_lock_hold_ms"]
    assert online["max_session_lock_hold_ms"] >= 0
    assert online["capture"]["sessions"] == 2
    assert 0 <= online["age_at_restore_seconds"] <= report["thresholds"][
        "maximum_backup_age_seconds"]
    assert online["age_at_restore_seconds"] == pytest.approx(
        online["restore_started_unix"] - manifest["created_unix"], abs=1e-6)
    assert online["backup_seconds"] > 0

    # Turns ran while the backup was taken, the first one held in flight
    # until it had started, and the archive holds that one at least.
    assert turns["in_flight_when_backup_started"] is True
    assert 1 <= turns["held_by_backup"] <= turns["turns"] <= release_drill.ONLINE_TURNS
    assert turns["overlapping_backup"] >= 1
    assert turns["answered"] == turns["turns"]
    before_load = AFTER_ROLLBACK["messages"] + 2
    assert turns["messages_before"] == before_load
    assert turns["messages_after"] == before_load + 2 * turns["turns"]
    held = before_load + 2 * turns["held_by_backup"]
    assert online["session_in_backup"] == {
        "messages": held,
        "audit_records": AFTER_ROLLBACK["audit_records"] + 1 + turns["held_by_backup"]}

    # Two starts of the candidate, each a phase like the four.
    assert [phase["name"] for phase in online["phases"]] == list(ONLINE_PHASES)
    for phase in online["phases"]:
        assert (phase["artifact_sha256"], phase["version"]) == (new["sha256"], new["version"])
        assert phase["ready_seconds"] <= phase["first_turn_seconds"] <= phase["total_seconds"]
    first, last = (phase["turn"] for phase in online["phases"])
    assert (first["read_before_turn"], first["messages_before"], first["messages_after"]) == (
        True, AFTER_ROLLBACK["messages"], AFTER_ROLLBACK["messages"] + 2)
    # The turn in the session restored from the online backup: the candidate
    # read it as the archive holds it, sent all of it, and committed after it.
    assert (last["read_before_turn"], last["messages_before"], last["history_messages_sent"],
            last["messages_after"]) == (True, held, held, held + 2)
    assert len(_conversation(work / "online-restored-state")) == held + 2
    assert online["restore_rto_seconds"] >= last["seconds"]
    assert online["measured_to"] == "the first successful turn in the restored session"
    assert online["continuity"]["preserved"] is True
    assert online["continuity"]["lost_or_changed"] == []

    # The four phases, the state they left and the readiness gate's reading
    # of the report are what they were.
    assert [phase["name"] for phase in report["phases"]] == list(PHASES)
    assert len(_conversation(work / "upgrade-state")) == AFTER_ROLLBACK["messages"]
    # The candidate never served the state the old release restored.
    assert len(_conversation(work / "restored-state")) == len(COMMITTED) + 2
    assert report["evaluation"]["mechanics_passed"] is True
    assert report["qualification_eligible"] is True, report["qualification_failures"]
    assert _validate_recovery_report(
        tmp_path, artifact_sha256=new["sha256"], version="1.0.0",
        rollback_from=report["artifacts"]["old"]) == report
    logs = sorted(path.name for path in (work / "logs").iterdir())
    assert logs[4:] == ["5-backup_online_new-service.log", "6-restore_online_new-service.log"]


def test_a_turn_is_held_in_flight_however_long_the_backup_takes_to_start(
        tmp_path, monkeypatch, releases):
    """The first turn run while backing up waits at the provider until the
    backup's process exists, so a backup that is slow to start cannot find
    the session idle and the turns over: here it takes half a second."""
    _stand_ins(monkeypatch)
    observed = _observe(monkeypatch)
    inner = subprocess.Popen

    def slow(command, *args, **kwargs):
        if "--online" in [str(part) for part in command]:
            time.sleep(0.5)
        return inner(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", slow)
    report = _run(tmp_path, releases)
    turns = report["online_backup"]["turns_while_backing_up"]
    assert turns["in_flight_when_backup_started"] is True and turns["held_by_backup"] >= 1
    [backup] = [tool for tool in observed.tools if "--online" in tool.arguments]
    held = [turn for turn in observed.turns if turn.message.endswith("turn 1 while the backup runs.")]
    assert len(held) == 1 and held[0].started < backup.launched < held[0].ended
    assert held[0].ended - held[0].started >= 0.5


LOAD_FAULTS = {
    # fault on every turn after the phase's own: what the drill must then say
    "after:1:fails_turn": "never reached the provider: it was answered 502 agent_failure",
    "after:2:fails_turn": "was answered 502 agent_failure",
    "after:1:silent_turn": "never reached the provider",
    "after:1:asks_something_else": "never reached the provider",
    "after:1:sends_no_history": "none of the session's conversation",
    "after:1:garbles_reply": "is not one the provider gave",
    "after:1:keeps_nothing": "was not committed",
    "after:1:drops_committed": "changed the",
    "after:1:alters_committed": "changed the",
    "after:1:no_audit": "left no audit record",
    "after:1:volatile_turn": "does not hold the turn that was in flight when it started",
}


@pytest.mark.parametrize("fault", sorted(LOAD_FAULTS))
def test_no_turn_run_while_the_backup_is_taken_may_fail_or_go_astray(
        tmp_path, monkeypatch, releases, fault):
    """The class guard for the turns the candidate serves while its backup is
    taken: whatever the release gets wrong with them, from a failed turn to
    one it answers and never writes, the drill stops in that phase."""
    stand_ins = _stand_ins(monkeypatch, {4: {fault}})
    _faults_after(monkeypatch)
    with pytest.raises(ReleaseDrillError) as failure:
        report = _run(tmp_path, releases)
        pytest.fail(
            f"the candidate's turns while its online backup ran had {fault!r} and the "
            f"drill reported mechanics_passed={report['evaluation']['mechanics_passed']} "
            f"(audit F-03)")
    assert str(failure.value).startswith(f"{ONLINE_PHASES[0]}: "), failure.value
    assert LOAD_FAULTS[fault] in str(failure.value), failure.value
    assert len(stand_ins.boots) == 5, "the candidate started on the archive anyway"
    assert not (tmp_path / "work" / "online-restored-state").exists()
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["qualification_eligible"] is False
    assert report["evaluation"]["failures"] == [str(failure.value)]
    [phase] = report["online_backup"]["phases"]
    assert (phase["name"], phase["failed"], phase["error"]) == (
        ONLINE_PHASES[0], True, str(failure.value))


@pytest.mark.parametrize("fault", TURN_FAULTS)
def test_a_candidate_that_cannot_continue_the_session_it_restored_does_not_pass(
        tmp_path, monkeypatch, releases, fault):
    """A turn in the session restored from the online backup is judged as
    every phase's turn is: read as the archive holds it, run through the
    provider with what it holds, and committed after it."""
    stand_ins = _stand_ins(monkeypatch, {5: {fault}})
    with pytest.raises(ReleaseDrillError) as failure:
        report = _run(tmp_path, releases)
        pytest.fail(
            f"the candidate restored from the online backup had {fault!r} and the "
            f"drill reported mechanics_passed={report['evaluation']['mechanics_passed']} "
            f"(audit F-03)")
    assert str(failure.value).startswith(f"{ONLINE_PHASES[1]}: "), failure.value
    assert len(stand_ins.boots) == 6
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["qualification_eligible"] is False
    assert [phase.get("failed", False) for phase in report["online_backup"]["phases"]] == [
        False, True]


def test_the_candidate_reading_the_restored_session_as_empty_is_named(
        tmp_path, monkeypatch, releases):
    _stand_ins(monkeypatch, {5: {"forgets_history"}})
    with pytest.raises(ReleaseDrillError) as failure:
        _run(tmp_path, releases)
    held = json.loads((tmp_path / "report.json").read_text())[
        "online_backup"]["session_in_backup"]["messages"]
    assert held >= AFTER_ROLLBACK["messages"] + 4
    assert str(failure.value).startswith(
        f"{ONLINE_PHASES[1]}: the release read a conversation of 0 message(s) "
        f"where {held} were committed"), failure.value


def test_an_online_backup_that_does_not_hold_what_was_committed_fails_the_mechanics(
        tmp_path, monkeypatch, releases):
    """Every committed file of the state the candidate served is compared
    with the archive: the session it continued by content, every other one
    byte for byte. This candidate rewrites a session no turn addressed while
    it serves, so the archive holds that session changed."""
    _stand_ins(monkeypatch, {4: {"alters_bystander"}})
    report = _run(tmp_path, releases)
    bystander, _ = release_drill._session_files(*BYSTANDER)
    assert report["online_backup"]["continuity"]["lost_or_changed"] == [bystander]
    assert report["evaluation"]["mechanics_passed"] is False
    assert report["evaluation"]["failures"] == [
        "committed customer state changed or disappeared in the online backup"]
    assert report["qualification_eligible"] is False


def test_a_restore_from_the_online_backup_is_held_to_the_rto_threshold(
        tmp_path, monkeypatch, releases):
    _stand_ins(monkeypatch)
    report = _run(tmp_path, releases, max_restore_s=0.001)
    assert report["online_backup"]["restore_rto_seconds"] > 0.001
    assert "restore from the online backup exceeded the configured RTO threshold" in (
        report["evaluation"]["failures"])
    assert report["qualification_eligible"] is False


# ── a candidate whose backups are not online ─────────────────────────────

def _candidate(tmp_path, old_text, new_text):
    """A 1.0.0 archive whose state_tool.py has `old_text` replaced."""
    sigil, pin = _fake_sigil(tmp_path)
    app = _app_variant(tmp_path, "patched-app", "1.0.0")
    source = (app / "state_tool.py").read_text()
    assert old_text in source, "the patch no longer applies to state_tool.py"
    (app / "state_tool.py").write_text(source.replace(old_text, new_text))
    return build_release(
        sigil_root=sigil, output_dir=tmp_path / "patched-out", app_root=app,
        pin_file=pin, build_runtime=False, platform_tag="test")[0]


CANDIDATE_TOOLS = {
    # every release before audit 2026-11-10 had no online backup
    "no_online_mode": (
        '"--online", action="store_true",', '"--live", dest="online", action="store_true",',
        "the candidate's online backup failed: ", "unrecognized arguments: --online"),
    "not_online": (
        'manifest.update(mode="online", online=capture)',
        'manifest.update(mode="snapshot", online=capture)',
        "the candidate's backup is not an online one: its manifest has mode 'snapshot'", ""),
    "no_lock_hold": (
        '"max_session_lock_hold_ms": round(longest * 1000, 3),', "",
        "the online backup does not record its longest session lock hold", ""),
}


@pytest.mark.parametrize("variant", sorted(CANDIDATE_TOOLS))
def test_a_candidate_without_a_recorded_online_backup_does_not_pass(
        tmp_path, monkeypatch, releases, variant):
    old_text, new_text, reason, detail = CANDIDATE_TOOLS[variant]
    stand_ins = _stand_ins(monkeypatch)
    old, _ = releases
    candidate = _candidate(tmp_path / "candidate", old_text, new_text)
    with pytest.raises(ReleaseDrillError) as failure:
        _run(tmp_path, (old, candidate))
    assert str(failure.value).startswith(f"{ONLINE_PHASES[0]}: {reason}"), failure.value
    assert detail in str(failure.value)
    assert len(stand_ins.boots) == 5
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["qualification_eligible"] is False
    assert report["evaluation"]["failures"] == [str(failure.value)]


# ── what the backup's age means ──────────────────────────────────────────

class _LaterClock:
    """The time module as release_drill sees it, with the wall clock
    `seconds` ahead: the restore of the online backup then starts that much
    after the backup did. The state tool runs in a process of its own and
    keeps the real clock."""

    def __init__(self, seconds):
        self.seconds = seconds

    def __getattr__(self, name):
        return getattr(time, name)

    def time(self):
        return time.time() + self.seconds


def test_an_online_backup_older_than_the_objective_at_restore_cannot_qualify(
        tmp_path, monkeypatch, releases):
    _stand_ins(monkeypatch)
    monkeypatch.setattr(release_drill, "time", _LaterClock(1000))
    report = _run(tmp_path, releases)
    age = report["online_backup"]["age_at_restore_seconds"]
    assert age >= 1000
    assert report["evaluation"]["mechanics_passed"] is True
    assert report["qualification_eligible"] is False
    assert report["qualification_failures"] == [
        f"the online backup does not demonstrate the RPO threshold: its restore "
        f"began {age} s after it was taken"]


def test_the_drill_waits_for_a_backup_as_long_as_the_backup_waits_for_a_turn():
    """The online backup waits for a busy session lock up to its lock wait,
    which has to outlast a turn at its shipped deadline. A drill that gave up
    on the backup sooner would fail a release that waits as designed."""
    shipped_deadline = _shipped_budgets()["PI_TURN_DEADLINE_SECONDS"]
    assert DEFAULT_LOCK_WAIT_SECONDS > shipped_deadline
    assert release_drill.BACKUP_WAIT_SECONDS > DEFAULT_LOCK_WAIT_SECONDS


# ── the judgement of the archive, on its own ─────────────────────────────

def _say(role, text):
    return {"role": role, "content": text}


def _load_case(turns=3):
    """A session of two messages before the load, then `turns` turns."""
    before = {"conversation": [_say("user", "u0"), _say("assistant", "a0")],
              "audit": [{"n": 0}]}
    conversation = list(before["conversation"])
    audit = list(before["audit"])
    for number in range(1, turns + 1):
        conversation += [_say("user", f"m{number}"), _say("assistant", f"r{number}")]
        audit.append({"n": number})
    return before, {"conversation": conversation, "audit": audit}


@pytest.mark.parametrize("held", [1, 2, 3])
def test_an_archive_at_a_turn_boundary_is_accepted(held):
    before, after = _load_case()
    archive = {"conversation": after["conversation"][:2 + 2 * held],
               "audit": after["audit"][:1 + held]}
    assert release_drill._turns_held(
        archive, before=before, after=after, boundaries=[2, 4, 6, 8]) == held


@pytest.mark.parametrize("case,reason", [
    ("before_the_turns", "does not hold the turn that was in flight when it started"),
    ("half_a_turn", "as no turn left it"),
    ("not_a_prefix", "as no turn left it"),
    ("chain_ahead_of_conversation", "is not the chain as it stood"),
    ("chain_behind_conversation", "is not the chain as it stood"),
    ("chain_rewritten", "is not the chain as it stood"),
])
def test_an_archive_that_is_not_the_session_at_a_turn_boundary_is_refused(case, reason):
    before, after = _load_case()
    archive = {
        "before_the_turns": {"conversation": after["conversation"][:2],
                             "audit": after["audit"][:1]},
        "half_a_turn": {"conversation": after["conversation"][:5],
                        "audit": after["audit"][:3]},
        "not_a_prefix": {"conversation": after["conversation"][:2] + after["conversation"][4:6],
                         "audit": after["audit"][:2]},
        "chain_ahead_of_conversation": {"conversation": after["conversation"][:4],
                                        "audit": after["audit"]},
        "chain_behind_conversation": {"conversation": after["conversation"][:6],
                                      "audit": after["audit"][:2]},
        "chain_rewritten": {"conversation": after["conversation"][:4],
                            "audit": [{"n": 0}, {"n": "forged"}]},
    }[case]
    with pytest.raises(ReleaseDrillError, match=reason):
        release_drill._turns_held(archive, before=before, after=after, boundaries=[2, 4, 6, 8])


def test_a_backup_that_started_with_no_turn_in_flight_is_refused():
    turns = [{"number": 1, "message": "m1", "status": 200, "code": None, "reply": "r1",
              "started": 1.0, "ended": 2.0}]
    load = {"turns": turns, "launched": 2.5, "exited": 3.0}
    with pytest.raises(ReleaseDrillError, match="no turn was in flight when the online backup started"):
        release_drill._judge_load(
            load, before={"conversation": [], "audit": []},
            after={"conversation": [_say("user", "m1"), _say("assistant", "r1")],
                   "audit": [{"n": 1}]},
            requests=[[_say("user", "m1")]], replies=["r1"])
