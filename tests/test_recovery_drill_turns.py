"""Every phase of the recovery drill reads the session and runs a turn in it (audit F-18).

The drill restored a backup and started the old release, the candidate and the
old release again on it. It asked each for `/v1/ready`, `/v1/version` and
`/v1/metrics`, with an operations-only credential in a tenant of its own, and
compared the files afterwards. No phase read the restored conversation or ran
a turn, so the rollback was over state the candidate had only started on: a
candidate that could not read the old release's sessions passed. The restore
and rollback figures were the time to readiness of a two-file state. Readiness
was recorded before the version and metrics requests, whose errors only
repeated the loop, so a release whose metrics endpoint always failed passed
too. And a drill that failed left no report.

These tests drive the drill's real probe, over real HTTP, against stand-in
releases (tests/test_release_drill.py) that misbehave in one phase each. None
may pass. tests/test_recovery_drill_real_bundle.py runs the same drill against
real bundles of this tree.
"""

import functools
import hashlib
import json
import sys
import threading
import time
import urllib.request

import pytest

from agent import AuditLog, SessionStore
from conftest import PI_ROOT
from product_service import _internal_session
from scripts import failure_drill, release_drill
from scripts.check_readiness_evidence import EvidenceError, _validate_recovery_report
from scripts.release_drill import ReleaseDrillError, run_drill
from test_release_drill import (
    BYSTANDER,
    COMMITTED,
    ENDPOINT_FAULTS,
    NEEDS_HISTORY,
    PHASES,
    SESSION,
    SLOW_TURN_SECONDS,
    TENANT,
    TURN_FAULTS,
    _artifacts,
    _backup,
    _stand_ins,
)

# What the harness needs from its own process, as opposed to what configures
# the release it starts.
PROCESS_PLUMBING = {"PATH", "LANG", "LC_ALL", "TMPDIR", "PYTHONPATH"}
THE_TURN = ["GET /v1/sessions/{session}/export", "POST /v1/chat",
            "GET /v1/sessions/{session}/export"]
OPERATIONS = {"GET /v1/ready", "GET /v1/version", "GET /v1/metrics"}
ENDPOINTS = {"metrics_fail": "/v1/metrics", "version_fail": "/v1/version"}


@pytest.fixture(scope="module")
def releases(tmp_path_factory):
    """The old (0.9.0) and new (1.0.0) archives, built once."""
    return _artifacts(tmp_path_factory.mktemp("releases"))


def _run(tmp_path, releases, *, backup=None, output="report.json", **arguments):
    old, new = releases
    backup, key, _ = backup or _backup(tmp_path)
    return run_drill(
        old_artifact=old, new_artifact=new, state_backup=backup, audit_key_file=key,
        output=tmp_path / output, work_dir=tmp_path / "work", **arguments)


def _restored_conversation(tmp_path, tenant=TENANT, session=SESSION):
    store = SessionStore(tmp_path / "work" / "restored-state" / "sessions")
    return store.load(_internal_session(tenant, session))


# ── a turn in every phase ────────────────────────────────────────────────

def test_every_phase_reads_the_session_and_runs_a_turn_in_it(tmp_path, monkeypatch, releases):
    stand_ins = _stand_ins(monkeypatch)
    report = _run(tmp_path, releases, output="recovery-drill.json")

    # The four phases, then the candidate's two starts that take and restore
    # an online backup (tests/test_recovery_drill_online_backup.py).
    assert [boot.version for boot in stand_ins.boots] == [
        "0.9.0", "0.9.0", "1.0.0", "0.9.0", "1.0.0", "1.0.0"]
    for name, boot in zip(PHASES, stand_ins.boots):
        assert boot.routes(TENANT) == THE_TURN, (
            f"{name}: as the session's tenant the release was asked {boot.routes(TENANT)}, "
            f"and in all {sorted(set(boot.routes()))}: the phase neither read the "
            f"session nor ran a turn in it (audit F-18)")

    # The restored state holds what was committed and then one turn for each
    # phase that ran on it, in order: each release continued the conversation
    # the one before it left.
    conversation = _restored_conversation(tmp_path)
    assert conversation[:len(COMMITTED)] == COMMITTED
    assert [message["role"] for message in conversation] == ["user", "assistant"] * 4
    asked = [message["content"] for message in conversation[2::2]]
    assert all(name in text for name, text in zip(PHASES[1:], asked)), asked

    turns = [phase["turn"] for phase in report["phases"]]
    assert [turn["read_before_turn"] for turn in turns] == [False, True, True, True]
    assert [turn["messages_before"] for turn in turns] == [0, 2, 4, 6]
    assert [turn["messages_after"] for turn in turns] == [2, 4, 6, 8]
    assert [turn["history_messages_sent"] for turn in turns] == [0, 2, 4, 6]
    for phase, turn in zip(report["phases"], turns):
        assert turn["session"] == SESSION and turn["provider_requests"] == 1
        assert turn["audit_records_after"] == turn["audit_records_before"] + 1
        assert phase["ready_seconds"] <= phase["first_turn_seconds"] <= phase["total_seconds"]
    assert report["evaluation"]["mechanics_passed"] is True
    assert report["qualification_eligible"] is True, report["qualification_failures"]
    # The readiness gate's validator still reads this report.
    assert _validate_recovery_report(
        tmp_path, artifact_sha256=report["artifacts"]["new"]["sha256"], version="1.0.0",
        rollback_from=report["artifacts"]["old"]) == report


def test_the_turn_runs_as_the_sessions_tenant_and_readiness_is_asked_as_the_drills(
        tmp_path, monkeypatch, releases):
    """Every authenticated request is charged to its tenant's request window,
    which is part of the restored state. Waiting for readiness therefore uses
    a credential of the drill's own tenant, and the session's tenant is
    charged the three requests of the turn and no more; in the start whose
    state is backed up online, also one for each turn run meanwhile and one
    to read the session after them."""
    stand_ins = _stand_ins(monkeypatch, {1: {"slow_ready"}})
    report = _run(tmp_path, releases)

    credentials = {entry["tenant"]: entry for entry in json.loads(
        (tmp_path / "work" / "auth.json").read_text())["tokens"]}
    assert set(credentials) == {"release-drill", TENANT}, (
        f"the drill's credentials are for {sorted(credentials)}: none is in "
        f"{TENANT!r}, the tenant of the restored session (audit F-18)")
    assert credentials[TENANT]["scopes"] == ["chat", "sessions:read"]
    assert credentials["release-drill"]["scopes"] == ["ops:read"]
    assert all(entry["tools"] == [] for entry in credentials.values())
    meanwhile = report["online_backup"]["turns_while_backing_up"]["turns"]
    assert len(stand_ins.boots) == 6
    for number, boot in enumerate(stand_ins.boots):
        assert set(boot.routes("release-drill")) == OPERATIONS
        extra = ["POST /v1/chat"] * meanwhile + [THE_TURN[0]] if number == 4 else []
        assert boot.routes(TENANT) == THE_TURN + extra
    assert stand_ins.boots[1].routes("release-drill").count("GET /v1/ready") > 8


def test_a_candidate_that_cannot_read_the_old_releases_session_does_not_pass(
        tmp_path, monkeypatch, releases):
    stand_ins = _stand_ins(monkeypatch, {2: {"forgets_history"}})
    with pytest.raises(ReleaseDrillError) as failure:
        report = _run(tmp_path, releases)
        pytest.fail(
            "the candidate read the restored conversation as empty and the drill "
            f"reported mechanics_passed={report['evaluation']['mechanics_passed']}, "
            f"qualification_eligible={report['qualification_eligible']} (audit F-18)")
    assert "upgrade_new: " in str(failure.value)
    assert "0 message(s) where 4 were committed" in str(failure.value)
    assert len(stand_ins.boots) == 3, "the rollback ran after the candidate failed"


def test_a_rollback_that_cannot_read_what_the_candidate_wrote_does_not_pass(
        tmp_path, monkeypatch, releases):
    _stand_ins(monkeypatch, {3: {"forgets_history"}})
    with pytest.raises(ReleaseDrillError) as failure:
        _run(tmp_path, releases)
    assert "rollback_old: " in str(failure.value)
    assert "0 message(s) where 6 were committed" in str(failure.value)


TURNS_THAT_ARE_NOT = [(start, fault) for start in range(4) for fault in TURN_FAULTS
                      if start or fault not in NEEDS_HISTORY]


@pytest.mark.parametrize("start,fault", TURNS_THAT_ARE_NOT, ids=[
    f"{PHASES[start]}-{fault}" for start, fault in TURNS_THAT_ARE_NOT])
def test_no_phase_passes_without_a_real_turn_in_the_session_as_committed(
        tmp_path, monkeypatch, releases, start, fault):
    """The class guard: whichever release it is and whatever it gets wrong,
    a phase that did not read the session as committed, run a turn in it
    through the provider, and commit that turn stops the drill there."""
    stand_ins = _stand_ins(monkeypatch, {start: {fault}})
    with pytest.raises(ReleaseDrillError) as failure:
        _run(tmp_path, releases)
    assert str(failure.value).startswith(f"{PHASES[start]}: "), failure.value
    assert len(stand_ins.boots) == start + 1, "a later phase ran after the failed one"
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["qualification_eligible"] is False
    assert report["evaluation"]["mechanics_passed"] is False


@pytest.mark.parametrize("start", range(4), ids=PHASES)
def test_a_release_without_the_export_route_is_not_taken_for_one_without_the_session(
        tmp_path, monkeypatch, releases, start):
    """A clean install has no session yet, and the release says so with 404
    session_not_found. Any other 404 is a release the drill cannot read
    sessions through, on a clean install as much as anywhere."""
    stand_ins = _stand_ins(monkeypatch, {start: {"no_export_route"}})
    with pytest.raises(ReleaseDrillError) as failure:
        _run(tmp_path, releases)
    assert str(failure.value) == (
        f"{PHASES[start]}: GET /v1/sessions/{SESSION}/export returned 404 not_found")
    assert "POST /v1/chat" not in stand_ins.boots[start].routes()


# ── version and metrics ──────────────────────────────────────────────────

@pytest.mark.parametrize("fault", ENDPOINT_FAULTS)
@pytest.mark.parametrize("start", range(4), ids=PHASES)
def test_a_failing_version_or_metrics_request_fails_the_phase(
        tmp_path, monkeypatch, releases, start, fault):
    stand_ins = _stand_ins(monkeypatch, {start: {fault}})
    # The wait for readiness is shortened only so that a drill which retries
    # the failure until that wait runs out shows it in seconds.
    probe = functools.partial(release_drill._probe_release, timeout_s=10)
    with pytest.raises(ReleaseDrillError) as failure:
        report = _run(tmp_path, releases, probe=probe)
        pytest.fail(
            f"{ENDPOINTS[fault]} answered 500 to every request of {PHASES[start]} and "
            f"the drill reported mechanics_passed={report['evaluation']['mechanics_passed']} "
            f"(audit F-18)")
    assert str(failure.value).startswith(f"{PHASES[start]}: "), failure.value
    assert ENDPOINTS[fault] in str(failure.value) and "500" in str(failure.value)
    asked = stand_ins.boots[start].routes()
    assert asked.count(f"GET {ENDPOINTS[fault]}") == 1, (
        f"the failing request was repeated: {asked}")
    assert "POST /v1/chat" not in asked
    assert len(stand_ins.boots) == start + 1


# ── what the figures measure ─────────────────────────────────────────────

def test_recovery_is_timed_to_the_first_successful_turn(tmp_path, monkeypatch, releases):
    """The restored release and the rollback each take SLOW_TURN_SECONDS over
    their turn and as long again to stop. The figures hold the first and not
    the second."""
    slow = {"slow_turn", "slow_stop"}
    _stand_ins(monkeypatch, {1: slow, 3: slow})
    report = _run(tmp_path, releases)
    evaluation = report["evaluation"]
    restore, _, rollback = report["phases"][1:]
    assert evaluation["rollback_seconds"] >= SLOW_TURN_SECONDS, (
        f"rollback is reported as {evaluation['rollback_seconds']} s although the first "
        f"turn after it took {SLOW_TURN_SECONDS} s: the figure is the time to "
        f"readiness of a restored state (audit F-18)")
    assert evaluation["restore_rto_seconds"] >= SLOW_TURN_SECONDS
    for phase in (restore, rollback):
        assert phase["first_turn_seconds"] >= phase["ready_seconds"] + SLOW_TURN_SECONDS
        assert phase["turn"]["seconds"] >= SLOW_TURN_SECONDS
        assert phase["total_seconds"] >= phase["first_turn_seconds"] + SLOW_TURN_SECONDS
    # From the start of the rollback to the end of its turn, and no further:
    # the whole phase took one slow stop longer.
    assert evaluation["rollback_seconds"] <= (
        rollback["elapsed_seconds"] - SLOW_TURN_SECONDS + 0.25)
    assert evaluation["measured_to"] == "the first successful turn in the restored session"


# ── a failed drill leaves its report ─────────────────────────────────────

def test_a_failed_phase_leaves_a_report_that_names_it_and_the_service_logs(
        tmp_path, monkeypatch, releases):
    _stand_ins(monkeypatch, {2: {"forgets_history"}})
    with pytest.raises(ReleaseDrillError) as failure:
        _run(tmp_path, releases, output="recovery-drill.json")
    text = (tmp_path / "recovery-drill.json").read_text()
    report = json.loads(text)
    assert report["qualification_eligible"] is False
    assert report["qualification_failures"] == [f"the drill did not complete: {failure.value}"]
    assert report["evaluation"]["mechanics_passed"] is False
    assert report["evaluation"]["failures"] == [str(failure.value)]
    assert [phase["name"] for phase in report["phases"]] == list(PHASES[:3])
    assert [phase.get("failed", False) for phase in report["phases"]] == [False, False, True]
    assert report["phases"][2]["error"] == str(failure.value)
    assert report["artifacts"]["new"]["version"] == "1.0.0"
    assert (tmp_path / "recovery-drill.json").stat().st_mode & 0o077 == 0
    logs = sorted(path.name for path in (tmp_path / "work" / "logs").iterdir())
    assert logs == ["1-clean_install-service.log", "2-restore_old-service.log",
                    "3-upgrade_new-service.log"]
    assert "stand-in 1.0.0 serving" in (tmp_path / "work" / "logs" / logs[2]).read_text()
    # It can never be taken for the evidence of a drill that passed.
    with pytest.raises(EvidenceError):
        _validate_recovery_report(
            tmp_path, artifact_sha256=report["artifacts"]["new"]["sha256"], version="1.0.0")
    # A report holds counts. What the tenant's conversation says is not its to keep.
    assert COMMITTED[0]["content"] not in text and "the reply committed" not in text


def test_a_drill_that_cannot_start_work_leaves_a_report_too(tmp_path, monkeypatch, releases):
    _stand_ins(monkeypatch)
    old, _ = releases
    backup, key, _ = _backup(tmp_path)
    with pytest.raises(ReleaseDrillError, match="new artifact is not a regular file"):
        run_drill(old_artifact=old, new_artifact=tmp_path / "missing.tar.gz",
                  state_backup=backup, audit_key_file=key,
                  output=tmp_path / "report.json", work_dir=tmp_path / "work")
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["phases"] == [] and report["qualification_eligible"] is False
    assert report["artifacts"]["old"]["version"] == "0.9.0" and report["artifacts"]["new"] is None


def _command(monkeypatch, tmp_path, releases, *extra):
    old, new = releases
    backup, key, _ = _backup(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "release_drill.py", "--old-artifact", str(old), "--new-artifact", str(new),
        "--state-backup", str(backup), "--audit-key-file", str(key),
        "--work-dir", str(tmp_path / "work"), "--output", str(tmp_path / "report.json"),
        *extra])
    with pytest.raises(SystemExit) as stopped:
        release_drill.main()
    return stopped.value.code


def test_the_command_fails_when_a_phase_fails_and_when_the_run_cannot_qualify(
        tmp_path, monkeypatch, releases, capsys):
    _stand_ins(monkeypatch, {3: {"fails_turn"}})
    assert _command(monkeypatch, tmp_path / "failed", releases) == 2
    assert "release drill failed: rollback_old: " in capsys.readouterr().err

    _stand_ins(monkeypatch, {2: {"alters_bystander"}})
    assert _command(monkeypatch, tmp_path / "altered", releases) == 1
    assert "not qualification-eligible" in capsys.readouterr().err


# ── continuity ───────────────────────────────────────────────────────────

def test_the_final_backup_holds_the_committed_turn_and_every_phases_turn(
        tmp_path, monkeypatch, releases):
    _stand_ins(monkeypatch)
    report = _run(tmp_path, releases)
    conversation, chain = release_drill._session_files(TENANT, SESSION)
    continuity = report["evaluation"]["continuity"]
    assert continuity["session"] == {
        "tenant": TENANT, "session": SESSION,
        "conversation": conversation, "audit_chain": chain,
        "held_committed_content": True,
        "committed_messages": 2, "messages_after": 8,
        "committed_audit_records": 1, "audit_records_after": 4,
        "preserved": True,
    }
    assert continuity["lost_or_changed"] == [] and continuity["preserved"] is True
    assert continuity["committed_customer_files"] == 4
    assert COMMITTED[0]["content"] not in (tmp_path / "report.json").read_text()


def test_a_turn_that_is_not_in_the_state_backed_up_afterwards_fails_the_mechanics(
        tmp_path, monkeypatch, releases):
    """The last release answers the turn, and serves it for as long as it
    runs, without writing it. Nothing a phase can ask shows that; the backup
    taken after the rollback does."""
    _stand_ins(monkeypatch, {3: {"volatile_turn"}})
    report = _run(tmp_path, releases)
    assert report["phases"][3]["turn"]["messages_after"] == 8
    assert report["evaluation"]["continuity"]["session"]["messages_after"] == 6
    assert report["evaluation"]["mechanics_passed"] is False
    assert report["evaluation"]["failures"] == [
        "the state backed up after the rollback does not hold the turns the phases ran"]
    assert report["qualification_eligible"] is False


def test_a_release_that_alters_a_session_no_phase_addressed_fails_the_mechanics(
        tmp_path, monkeypatch, releases):
    _stand_ins(monkeypatch, {2: {"alters_bystander"}})
    report = _run(tmp_path, releases)
    bystander, _ = release_drill._session_files(*BYSTANDER)
    assert report["evaluation"]["continuity"]["lost_or_changed"] == [bystander]
    assert report["evaluation"]["mechanics_passed"] is False
    assert report["evaluation"]["failures"] == [
        "committed customer state changed or disappeared"]
    assert report["qualification_eligible"] is False


def test_the_drill_addresses_a_session_the_way_the_product_stores_it(tmp_path):
    """The drill finds the session's two files in a backup itself. If the
    product's layout moved and the drill's did not, it would compare nothing."""
    internal = _internal_session(TENANT, SESSION)
    conversation, chain = release_drill._session_files(TENANT, SESSION)
    assert conversation == SessionStore(tmp_path / "sessions")._path(
        internal).relative_to(tmp_path).as_posix()
    assert chain == AuditLog(tmp_path / "audit")._path(
        internal).relative_to(tmp_path).as_posix()
    assert chain == f"audit/{hashlib.sha256(internal.encode()).hexdigest()}.jsonl"


def test_both_drills_judge_committed_state_by_one_rule():
    """The failure drill compares the session its fault landed in, and the
    recovery drill the session its phases continued, with the same
    definition: release_drill.py's, which ships in the bundle."""
    assert failure_drill._messages is release_drill._messages
    assert failure_drill._committed_continuity is release_drill._continuity
    assert failure_drill._session_files("a-session") == release_drill._session_files(
        failure_drill.TENANT, "a-session")
    before = {"sessions/" + "0" * 64 + ".kv": b"[1]"}
    assert (failure_drill._continuity(before, {}, "a-session")["lost_or_changed"]
            == release_drill._continuity(before, {}, TENANT, SESSION)["lost_or_changed"])


# ── which session ────────────────────────────────────────────────────────

def test_the_fixture_commits_its_turn_where_the_drill_reads():
    assert (release_drill.FIXTURE_TENANT, release_drill.FIXTURE_SESSION) == (TENANT, SESSION)
    fixture = (PI_ROOT / "scripts" / "drill_fixture.py").read_text()
    assert "FIXTURE_TENANT" in fixture and "FIXTURE_SESSION" in fixture
    assert TENANT not in fixture and SESSION not in fixture, (
        "the fixture names its tenant or session itself instead of using the "
        "drill's definition")


def test_the_drill_continues_the_tenant_and_session_it_is_given(tmp_path, monkeypatch, releases):
    stand_ins = _stand_ins(monkeypatch)
    report = _run(tmp_path, releases, tenant="tenant-a", session="project",
                  backup=_backup(tmp_path, tenant="tenant-a", session="project"))
    assert report["configuration"]["session"] == {"tenant": "tenant-a", "session": "project"}
    assert report["evaluation"]["continuity"]["session"]["messages_after"] == 8
    assert len(_restored_conversation(tmp_path, "tenant-a", "project")) == 8
    meanwhile = report["online_backup"]["turns_while_backing_up"]["turns"]
    assert [boot.routes("tenant-a") for boot in stand_ins.boots] == [THE_TURN] * 4 + [
        THE_TURN + ["POST /v1/chat"] * meanwhile + [THE_TURN[0]], THE_TURN]
    assert [phase["turn"]["session"] for phase in report["online_backup"]["phases"]] == [
        "project", "project"]
    assert report["qualification_eligible"] is True, report["qualification_failures"]


def test_a_session_the_backup_does_not_hold_fails_before_any_release_starts(
        tmp_path, monkeypatch, releases):
    stand_ins = _stand_ins(monkeypatch)
    with pytest.raises(ReleaseDrillError, match="holds no conversation for session"):
        _run(tmp_path, releases, tenant="tenant-b")
    assert stand_ins.boots == []


def test_a_backup_whose_session_holds_no_conversation_cannot_qualify(
        tmp_path, monkeypatch, releases):
    _stand_ins(monkeypatch)
    report = _run(tmp_path, releases, backup=_backup(tmp_path, messages=[]))
    assert report["evaluation"]["continuity"]["session"]["held_committed_content"] is False
    assert report["qualification_eligible"] is False
    assert any("held no committed conversation" in failure
               for failure in report["qualification_failures"])


@pytest.mark.parametrize("names", [
    {"session": ""}, {"session": "has space"}, {"session": "../escape"},
    {"session": "a" * 129}, {"session": 7}, {"tenant": ""}, {"tenant": None},
    {"tenant": "release-drill"},
])
def test_an_unusable_tenant_or_session_is_refused_before_any_work(tmp_path, releases, names):
    with pytest.raises(ReleaseDrillError, match="tenant|session"):
        _run(tmp_path, releases, **names)
    assert not (tmp_path / "work").exists() and not (tmp_path / "report.json").exists()


# ── the configuration record ─────────────────────────────────────────────

def test_the_report_records_the_provider_the_wiring_and_what_each_release_reported(
        tmp_path, monkeypatch, releases):
    # The old release is one from before audit 2026-11-10: it reports no settings.
    silent = {"reports_no_settings"}
    stand_ins = _stand_ins(monkeypatch, {0: silent, 1: silent, 3: silent})
    report = _run(tmp_path, releases)
    configuration = report["configuration"]
    assert configuration["provider"]["kind"] == "stand-in"
    assert configuration["provider"]["endpoint"].startswith("http://127.0.0.1:")
    assert configuration["session"] == {"tenant": TENANT, "session": SESSION}
    assert configuration["effective_settings"]["old"] is None
    in_force = configuration["effective_settings"]["new"]
    assert in_force["endpoint_host"] == "127.0.0.1"
    assert {name: str(in_force[name.removeprefix("PI_").lower()])
            for name in configuration["settings"]} == configuration["settings"]
    for boot in stand_ins.boots:
        unrecorded = (set(boot.env) - PROCESS_PLUMBING - set(configuration["settings"])
                      - set(configuration["wiring"]))
        assert not unrecorded, f"a release was started with {unrecorded}, which no report records"
        assert set(configuration["wiring"]) <= set(boot.env)
        assert boot.env["PI_ENDPOINT"] == configuration["provider"]["endpoint"]
    # Only the candidate has to report: the old release is already published.
    assert report["qualification_eligible"] is True, report["qualification_failures"]


@pytest.mark.parametrize("fault,reason", [
    ("reports_no_settings", "the candidate does not report its effective settings"),
    ("ignores_settings", "does not report these settings as in force: PI_AUDIT_VERIFY_SECONDS=3600"),
])
def test_a_candidate_whose_configuration_is_only_the_drills_word_cannot_qualify(
        tmp_path, monkeypatch, releases, fault, reason):
    _stand_ins(monkeypatch, {2: {fault}})
    report = _run(tmp_path, releases)
    assert report["evaluation"]["mechanics_passed"] is True
    assert report["qualification_eligible"] is False
    assert any(reason in failure for failure in report["qualification_failures"]), (
        report["qualification_failures"])


# ── the stand-in provider ────────────────────────────────────────────────

def test_the_provider_keeps_what_each_request_carried_and_numbers_its_replies():
    provider = release_drill._Provider()
    threading.Thread(target=provider.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    try:
        replies = []
        for messages in ([{"role": "user", "content": "one"}], "not a list"):
            request = urllib.request.Request(
                provider.endpoint, data=json.dumps({"messages": messages}).encode(),
                headers={"content-type": "application/json"})
            with urllib.request.urlopen(request, timeout=5) as response:
                body = json.load(response)
            assert body["stop_reason"] == "end_turn" and body["role"] == "assistant"
            replies.append(body["content"][0]["text"])
        assert replies == [provider.reply(0), provider.reply(1)] and replies[0] != replies[1]
        assert provider.requests == [[{"role": "user", "content": "one"}], None]
    finally:
        provider.shutdown()
        provider.server_close()


def test_a_drill_does_not_outlive_its_provider(tmp_path, monkeypatch, releases):
    _stand_ins(monkeypatch)
    report = _run(tmp_path, releases)
    endpoint = report["configuration"]["provider"]["endpoint"]
    time.sleep(0.05)
    with pytest.raises(OSError):
        urllib.request.urlopen(urllib.request.Request(endpoint, data=b"{}"), timeout=2)
