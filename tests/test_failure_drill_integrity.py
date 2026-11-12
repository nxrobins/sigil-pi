"""The failure drill's verdicts are about the state its faults touched (audit F-18).

Each category committed one session, injected its fault into OTHER sessions and
compared only the files that existed before. `state_integrity_verified: true`
therefore meant that a bystander's two files survived: a release that lost the
session it was writing when the fault hit would have passed. The
interrupted-write case required that its target did not exist yet, so it only
ever interrupted the creation of a file, never the replacement of one; and the
corrupt-state case wrote the original bytes back itself before comparing, so
nothing the release did to the corrupted chain could show.

The drill also started the release with a 60-second lease (it ships with 300),
30-second deadlines (120 and 90) and one model retry (2), and its report
recorded none of that.

These tests pin the corrected drill with releases that misbehave: each one
loses or alters committed state exactly where a fault lands, and none may pass.
"""

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import example, given, settings, strategies as st

from agent import SessionStore, write_durably
from conftest import PI_ROOT
from product_service import _internal_session
from scripts import failure_drill
from scripts.failure_drill import FailureDrillError, run_drill
from test_failure_drill import (
    AUDIT_KEY,
    EVIDENCE_URL,
    SHIPPED_SETTINGS,
    TENANT,
    _FakeService,
    _run,
)

# The categories whose fault is a turn that fails or is cut off. The sixth,
# corrupt_state, corrupts a file while the release is stopped.
TURN_FAULTS = ("runtime_crashes", "unavailable_model_providers", "network_failures",
               "full_disks", "interrupted_writes")

# What the harness needs from its own process, as opposed to what configures
# the release it starts.
PROCESS_PLUMBING = {"PATH", "LANG", "LC_ALL", "TMPDIR"}
BUDGET_SUFFIXES = ("_SECONDS", "_DAYS", "_RETRIES")


def _holds_content(path):
    return path.is_file() and bool(json.loads(path.read_bytes() or b"[]"))


def _failed(outcome):
    return outcome == "disconnected" or outcome >= 500


@pytest.fixture(scope="module")
def shipped(tmp_path_factory):
    """One drill of a well-behaved release at the configuration it ships, with
    every turn recorded: (category, session, whether the session held
    committed content beforehand, outcome)."""
    turns = []

    class Recording(_FakeService):
        def chat(self, session, message, timeout=None):
            held = _holds_content(self.conversation_path(session))
            try:
                status, body = super().chat(session, message, timeout)
            except Exception:
                turns.append((self.label, session, held, "disconnected"))
                raise
            turns.append((self.label, session, held, status))
            return status, body

    report = _run(tmp_path_factory.mktemp("shipped"), service_factory=Recording)
    return SimpleNamespace(report=report, turns=turns)


# ── every fault lands on committed content ──────────────────────────────────


def test_every_fault_is_injected_into_a_session_that_holds_committed_content(shipped):
    """The verdict can only be about the file the fault touched if the fault
    touches a file that had something to lose."""
    report, turns = shipped.report, shipped.turns
    for category in TURN_FAULTS:
        faulted = [(session, held) for label, session, held, outcome in turns
                   if label == category and _failed(outcome)]
        assert faulted, f"{category} injected no failing turn at all"
        bystanders = sorted({session for session, held in faulted if not held})
        assert not bystanders, (
            f"{category} injected its fault into {bystanders}, which held no "
            f"committed content, so its integrity verdict is about files the "
            f"fault never touched (audit F-18)")
    for category, result in report["categories"].items():
        sessions = {session for label, session, _, _ in turns if label == category}
        assert len(sessions) == 1, (
            f"{category} spoke to {sorted(sessions)}: every turn of a category "
            f"belongs to the one session it committed and compares")
        fault = result["continuity"]["fault_session"]
        assert fault["session"] in sessions and fault["held_committed_content"] is True
        assert fault["committed_messages"] >= 2 and fault["committed_audit_records"] >= 2


# ── a release that loses the faulted session cannot pass ────────────────────


def _empty_the_conversation(service, session):
    service.conversation_path(session).write_bytes(b"")


def _drop_the_committed_history(service, session):
    path = service.conversation_path(session)
    path.write_bytes(json.dumps(json.loads(path.read_bytes())[-1:]).encode())


def _delete_the_conversation(service, session):
    service.conversation_path(session).unlink()


def _drop_audit_records(service, session):
    path = service.chain_path(session)
    path.write_bytes(path.read_bytes().splitlines(keepends=True)[0])


def _delete_the_audit_chain(service, session):
    service.chain_path(session).unlink()


LOSSES = {
    "conversation emptied": _empty_the_conversation,
    "committed history dropped": _drop_the_committed_history,
    "conversation deleted": _delete_the_conversation,
    "audit records dropped": _drop_audit_records,
    "audit chain deleted": _delete_the_audit_chain,
}


@pytest.mark.parametrize("loss", sorted(LOSSES))
def test_a_release_that_loses_the_session_a_fault_hit_fails_that_category(tmp_path, loss):
    """Every category at once: whichever turn fails, the release loses the
    committed content of the session that turn was writing."""
    lost_in = []

    class Loses(_FakeService):
        def chat(self, session, message, timeout=None):
            held = (_holds_content(self.conversation_path(session))
                    and self.chain_path(session).is_file())
            outcome = "disconnected"
            try:
                status, body = super().chat(session, message, timeout)
                outcome = status
                return status, body
            finally:
                if held and _failed(outcome) and self.label not in lost_in:
                    LOSSES[loss](self, session)
                    lost_in.append(self.label)

    report = _run(tmp_path, service_factory=Loses)

    for category in TURN_FAULTS:
        assert category in lost_in, (
            f"{category} never directs a failing turn at committed content, so a "
            f"release that loses the session it was writing ({loss}) has nothing "
            f"to lose there and passes (audit F-18)")
        result = report["categories"][category]
        assert result["state_integrity_verified"] is False and result["passed"] is False, (
            f"{category}: the release lost the session the fault hit ({loss}) and "
            f"the drill reported state_integrity_verified="
            f"{result['state_integrity_verified']}, passed={result['passed']}")
        assert result["continuity"]["lost_or_changed"], category
        assert result["continuity"]["fault_session"]["preserved"] is False
    assert report["qualification_eligible"] is False


@pytest.mark.parametrize("alteration", ["deleted", "emptied", "records dropped",
                                        "record appended"])
def test_a_release_that_alters_the_corrupted_chain_while_refusing_fails(tmp_path, alteration):
    """The harness repairs the record it corrupted, so what the release did to
    that chain has to be read BEFORE the repair or it can never show."""
    what = {"deleted": "deleted", "emptied": "emptied",
            "records dropped": "dropped records from",
            "record appended": "appended a record to"}[alteration]

    class Alters(_FakeService):
        def try_start(self):
            started, detail = super().try_start()
            if not started:
                for chain in sorted((self.state_dir / "audit").glob("*.jsonl")):
                    if alteration == "deleted":
                        chain.unlink()
                    elif alteration == "emptied":
                        chain.write_bytes(b"")
                    elif alteration == "records dropped":
                        chain.write_bytes(chain.read_bytes().splitlines(keepends=True)[0])
                    else:
                        chain.write_bytes(chain.read_bytes() + b'{"written": "while refusing"}\n')
            return started, detail

    report = _run(tmp_path, service_factory=Alters)
    corrupt = report["categories"]["corrupt_state"]
    assert corrupt["state_integrity_verified"] is False and corrupt["passed"] is False, (
        f"the release {what} the corrupted audit chain while refusing to "
        f"start, the harness wrote the original bytes back, and the category "
        f"reported state_integrity_verified={corrupt['state_integrity_verified']}, "
        f"passed={corrupt['passed']} (audit F-18)")
    assert any("refused start" in failure for failure in corrupt["failures"])
    # The other five categories were not touched by this release's misbehavior.
    for category in TURN_FAULTS:
        assert report["categories"][category]["passed"] is True, category


@pytest.mark.parametrize("alteration", ["emptied", "grown"])
def test_a_refusing_release_that_touches_the_conversation_fails_too(tmp_path, alteration):
    """Refusing to start must leave every committed file alone, not only the
    corrupted chain. A conversation the release emptied is lost for good; one
    it added to would read as growth in the final comparison, so only the
    look between the refusal and the repair can see it."""

    class Touches(_FakeService):
        def try_start(self):
            started, detail = super().try_start()
            if not started:
                for conversation in sorted((self.state_dir / "sessions").glob("*.kv")):
                    held = json.loads(conversation.read_bytes())
                    conversation.write_bytes(b"" if alteration == "emptied" else json.dumps(
                        held + [{"role": "user", "content": "written while refusing"}]).encode())
            return started, detail

    corrupt = _run(tmp_path, service_factory=Touches)["categories"]["corrupt_state"]
    assert corrupt["state_integrity_verified"] is False and corrupt["passed"] is False
    assert corrupt["detail"]["refused_start_left_committed_files_unchanged"] is False
    assert any("refused start" in failure and "sessions/" in failure
               for failure in corrupt["failures"])
    assert corrupt["continuity"]["fault_session"]["preserved"] is (alteration == "grown")


def test_the_corrupted_chain_is_the_committed_sessions_and_a_turn_follows_the_repair(shipped):
    result = shipped.report["categories"]["corrupt_state"]
    fault = result["continuity"]["fault_session"]
    assert result["detail"]["chain"] == Path(fault["audit_chain"]).name
    assert result["detail"]["refused_start_left_committed_files_unchanged"] is True
    after_repair = [outcome for label, session, _, outcome in shipped.turns
                    if label == "corrupt_state" and session == fault["session"]]
    assert after_repair == [200, 200], (
        "the baseline turn, then one turn on the repaired session: a chain the "
        "release can no longer extend is not a recovery")
    assert fault["audit_records_after"] > fault["committed_audit_records"]


# ── the comparison itself ───────────────────────────────────────────────────

_MESSAGES = st.lists(
    st.fixed_dictionaries({
        "role": st.sampled_from(["user", "assistant"]),
        "content": st.text(max_size=12)}),
    min_size=1, max_size=5)
_RECORDS = st.lists(st.text(alphabet="abcdef0123456789", min_size=1, max_size=8),
                    min_size=1, max_size=5)


def _state(session, messages, records, others=None):
    conversation, chain = failure_drill._session_files(session)
    state = {
        conversation: json.dumps(messages, ensure_ascii=False).encode(),
        chain: "".join(json.dumps({"record": record}) + "\n" for record in records).encode(),
        "product-quotas.sqlite3": b"not customer state",
    }
    state.update(others or {})
    return state


def test_the_drill_addresses_a_session_the_way_the_product_stores_it(tmp_path):
    """The drill derives the faulted session's files itself. If the product's
    layout moved and the drill's did not, it would compare nothing."""
    internal = _internal_session(TENANT, "a-session")
    conversation, chain = failure_drill._session_files("a-session")
    store = SessionStore(tmp_path / "sessions")
    assert conversation == store._path(internal).relative_to(tmp_path).as_posix()
    assert chain == f"audit/{hashlib.sha256(internal.encode()).hexdigest()}.jsonl"
    assert failure_drill.TENANT == TENANT


@settings(max_examples=150, deadline=None)
@given(committed=_MESSAGES, grown=st.lists(_MESSAGES.map(lambda m: m[0]), max_size=4),
       records=_RECORDS, appended=st.lists(_RECORDS.map(lambda r: r[0]), max_size=4))
def test_a_session_that_only_grew_is_preserved(committed, grown, records, appended):
    before = _state("s", committed, records, {"sessions/" + "0" * 64 + ".kv": b"[1]"})
    after = _state("s", committed + grown, records + appended,
                   {"sessions/" + "0" * 64 + ".kv": b"[1]",
                    "sessions/" + "1" * 64 + ".kv": b"[2]"})
    continuity = failure_drill._continuity(before, after, "s")
    assert continuity["preserved"] is True and continuity["lost_or_changed"] == []
    assert continuity["committed_customer_files"] == 3
    assert continuity["committed_session_files"] == 2
    fault = continuity["fault_session"]
    assert fault["held_committed_content"] is True and fault["preserved"] is True
    assert fault["committed_messages"] == len(committed)
    assert fault["messages_after"] == len(committed) + len(grown)
    assert fault["committed_audit_records"] == len(records)
    assert fault["audit_records_after"] == len(records) + len(appended)


@settings(max_examples=200, deadline=None)
@given(committed=_MESSAGES, records=_RECORDS, data=st.data())
def test_any_loss_of_committed_content_is_reported(committed, records, data):
    """Dropping, reordering or changing a committed message or record, or
    losing a file, is never read as growth."""
    before = _state("s", committed, records)
    conversation, chain = failure_drill._session_files("s")
    surface = data.draw(st.sampled_from(["conversation", "chain"]))
    after = dict(before)
    if surface == "conversation":
        loss = data.draw(st.sampled_from(["missing", "empty", "not json", "not a list",
                                          "dropped", "changed", "prepended"]))
        if loss == "missing":
            del after[conversation]
        elif loss == "empty":
            after[conversation] = b""
        elif loss == "not json":
            after[conversation] = before[conversation][:-1]
        elif loss == "not a list":
            after[conversation] = json.dumps({"messages": committed}).encode()
        elif loss == "dropped":
            index = data.draw(st.integers(0, len(committed) - 1))
            kept = committed[:index] + committed[index + 1:]
            tail = data.draw(st.lists(_MESSAGES.map(lambda m: m[0]), max_size=3))
            if (kept + tail)[:len(committed)] == committed:
                tail = []
                kept = committed[:-1]
            after[conversation] = json.dumps(kept + tail).encode()
        elif loss == "changed":
            index = data.draw(st.integers(0, len(committed) - 1))
            changed = [dict(message) for message in committed]
            changed[index]["content"] += "!"
            after[conversation] = json.dumps(changed).encode()
        else:
            after[conversation] = json.dumps(
                [{"role": "user", "content": "inserted first"}] + committed).encode()
        lost = conversation
    else:
        loss = data.draw(st.sampled_from(["missing", "empty", "truncated", "changed"]))
        if loss == "missing":
            del after[chain]
        elif loss == "empty":
            after[chain] = b""
        elif loss == "truncated":
            after[chain] = before[chain][:-1] + b"0\n"
        else:
            after[chain] = b"x" + before[chain][1:]
        lost = chain
    continuity = failure_drill._continuity(before, after, "s")
    assert continuity["preserved"] is False, (surface, loss)
    assert continuity["lost_or_changed"] == [lost]
    assert continuity["fault_session"]["preserved"] is False


def test_a_bystander_must_be_byte_identical_and_only_the_faulted_session_may_grow():
    bystander = "sessions/" + "0" * 64 + ".kv"
    before = _state("s", [{"role": "user", "content": "a"}], ["r"],
                    {bystander: b'[{"role": "user", "content": "b"}]',
                     "sandboxes/0123456789abcdef/note.txt": b"kept"})
    grown = dict(before)
    grown[bystander] = b'[{"role": "user", "content": "b"}, {"role": "user", "content": "c"}]'
    assert failure_drill._continuity(before, grown, "s")["lost_or_changed"] == [bystander]
    rewritten = dict(before)
    rewritten["sandboxes/0123456789abcdef/note.txt"] = b"kept, and more"
    assert failure_drill._continuity(before, rewritten, "s")["lost_or_changed"] == [
        "sandboxes/0123456789abcdef/note.txt"]
    assert failure_drill._continuity(before, dict(before), "s")["preserved"] is True


@pytest.mark.parametrize("missing", ["conversation", "chain", "empty conversation"])
def test_a_faulted_session_with_nothing_committed_is_vacuous(missing):
    before = _state("s", [{"role": "user", "content": "a"}], ["r"])
    conversation, chain = failure_drill._session_files("s")
    if missing == "conversation":
        del before[conversation]
    elif missing == "chain":
        del before[chain]
    else:
        before[conversation] = b"[]"
    continuity = failure_drill._continuity(before, dict(before), "s")
    assert continuity["fault_session"]["held_committed_content"] is False
    assert continuity["fault_session"]["preserved"] is False


def test_a_category_whose_session_committed_nothing_fails_as_vacuous(tmp_path):
    """A release whose baseline turn answers 200 and saves no conversation."""

    class CommitsNothing(_FakeService):
        def _turn(self, name, message, reply):
            super()._turn(name, message, reply)
            if self.label == "network_failures":
                self.conversation_path(name).write_bytes(b"[]")

    result = _run(tmp_path, service_factory=CommitsNothing)["categories"]["network_failures"]
    assert result["passed"] is False and result["state_integrity_verified"] is False
    assert any("held no committed" in failure for failure in result["failures"])


# ── a save interrupted while it replaces a committed file ───────────────────

PID = 4242


class _Killed(BaseException):
    """The host is stopped at its rename and then killed: nothing after the
    rename call runs."""


class _LinuxOnly:
    """`sys` as the drill sees it on the platform the boundary needs."""

    platform = "linux"

    def __getattr__(self, name):
        return getattr(sys, name)


def _armed_boundary(tmp_path, monkeypatch, committed):
    """A write boundary around a committed session file, with the observer's
    build and the stopped process's /proc entry stood in for."""
    monkeypatch.setattr(failure_drill, "sys", _LinuxOnly())
    monkeypatch.setattr(shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(subprocess, "run", lambda command, **_: subprocess.CompletedProcess(
        command, 0, "", ""))
    sessions = tmp_path / "state" / "sessions"
    sessions.mkdir(parents=True)
    target = sessions / ("ab" * 32 + ".kv")
    target.write_bytes(committed)
    boundary = failure_drill._WriteBoundary(tmp_path, target)
    read_text = Path.read_text

    def proc_status(path, *args, **kwargs):
        if str(path) == f"/proc/{PID}/status":
            return "Name:\tpython3\nState:\tT (stopped)\nPid:\t4242\n"
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", proc_status)
    return boundary, target, SimpleNamespace(pid=PID, poll=lambda: None)


COMMITTED = json.dumps([{"role": "user", "content": "committed"},
                        {"role": "assistant", "content": "committed reply"}]).encode()
UNCOMMITTED = json.dumps([{"role": "user", "content": "committed"},
                          {"role": "assistant", "content": "committed reply"},
                          {"role": "user", "content": "a turn to interrupt"}]).encode()


def test_the_boundary_is_observed_when_a_save_replaces_a_committed_file(tmp_path, monkeypatch):
    """The product's own save, stopped where the LD_PRELOAD observer stops it:
    at the rename, with the scratch file complete and the committed file still
    in place. The drill used to demand that the target did not exist yet, and
    looked for the scratch under the one name releases before 2026-11-10 used."""
    boundary, target, process = _armed_boundary(tmp_path, monkeypatch, COMMITTED)
    replace = os.replace
    seen = {}

    def stopped_at_the_rename(source, destination, *args, **kwargs):
        if Path(os.fsdecode(destination)) != target:
            return replace(source, destination, *args, **kwargs)
        boundary.marker.write_text(f"{PID}\n")  # what the observer writes
        try:
            seen["observation"] = boundary.wait(process, timeout=2)
        except FailureDrillError as error:
            seen["refused"] = str(error)
        seen["scratch"] = Path(os.fsdecode(source))
        raise _Killed()

    monkeypatch.setattr(os, "replace", stopped_at_the_rename)
    with pytest.raises(_Killed):
        write_durably(target, UNCOMMITTED)

    assert "refused" not in seen, (
        f"the save was stopped at its rename with the committed file in place and "
        f"its complete scratch at {seen['scratch'].name}, and the drill refused "
        f"the boundary: {seen['refused']!r} (audit F-18: it only interrupted the "
        f"creation of a new file)")
    observation = seen["observation"]
    assert observation["boundary_observed"] is True and observation["host_pid"] == PID
    assert observation["temporary_bytes"] == len(UNCOMMITTED)
    assert observation["temporary_sha256"] == hashlib.sha256(UNCOMMITTED).hexdigest()
    assert observation["committed_bytes"] == len(COMMITTED)
    assert observation["committed_sha256"] == hashlib.sha256(COMMITTED).hexdigest()
    assert target.read_bytes() == COMMITTED
    assert boundary.adopted() is False


def test_the_boundary_is_observed_under_the_scratch_name_older_releases_used(
        tmp_path, monkeypatch):
    boundary, target, process = _armed_boundary(tmp_path, monkeypatch, COMMITTED)
    target.with_suffix(".kv.tmp").write_bytes(UNCOMMITTED)
    boundary.marker.write_text(f"{PID}\n")
    observation = boundary.wait(process, timeout=2)
    assert observation["boundary_observed"] is True
    assert observation["temporary_sha256"] == hashlib.sha256(UNCOMMITTED).hexdigest()
    assert observation["committed_sha256"] == hashlib.sha256(COMMITTED).hexdigest()


@pytest.mark.parametrize("state,match", [
    ("no scratch", "uncommitted write"),
    ("empty scratch", "uncommitted write"),
    ("scratch equal to committed", "uncommitted write"),
    ("two new files", "uncommitted write"),
    ("committed file changed", "committed"),
    ("committed file gone", "committed"),
])
def test_a_stop_that_is_not_a_pending_replacement_is_refused(
        tmp_path, monkeypatch, state, match):
    boundary, target, process = _armed_boundary(tmp_path, monkeypatch, COMMITTED)
    scratch = target.with_name(f".{target.name}.0123456789abcdef.tmp")
    if state == "empty scratch":
        scratch.write_bytes(b"")
    elif state == "scratch equal to committed":
        scratch.write_bytes(COMMITTED)
    elif state != "no scratch":
        scratch.write_bytes(UNCOMMITTED)
    if state == "two new files":
        target.with_name(f".{target.name}.fedcba9876543210.tmp").write_bytes(UNCOMMITTED)
    elif state == "committed file changed":
        target.write_bytes(UNCOMMITTED)
    elif state == "committed file gone":
        target.unlink()
    boundary.marker.write_text(f"{PID}\n")
    with pytest.raises(FailureDrillError, match=match):
        boundary.wait(process, timeout=2)


@pytest.mark.parametrize("committed", [None, b"", b"[]"])
def test_a_session_with_nothing_committed_cannot_be_the_interruption_target(
        tmp_path, monkeypatch, committed):
    monkeypatch.setattr(failure_drill, "sys", _LinuxOnly())
    monkeypatch.setattr(shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(subprocess, "run", lambda command, **_: subprocess.CompletedProcess(
        command, 0, "", ""))
    target = tmp_path / ("ab" * 32 + ".kv")
    if committed is not None:
        target.write_bytes(committed)
    with pytest.raises(FailureDrillError, match="no committed conversation"):
        failure_drill._WriteBoundary(tmp_path, target)


@pytest.mark.parametrize("after_the_kill,adopted", [
    ("unchanged", False), ("the scratch was renamed into place", True),
    ("emptied", True), ("deleted", True)])
def test_adoption_is_read_from_the_committed_file_itself(
        tmp_path, monkeypatch, after_the_kill, adopted):
    boundary, target, process = _armed_boundary(tmp_path, monkeypatch, COMMITTED)
    scratch = target.with_name(f".{target.name}.0123456789abcdef.tmp")
    scratch.write_bytes(UNCOMMITTED)
    boundary.marker.write_text(f"{PID}\n")
    boundary.wait(process, timeout=2)
    if after_the_kill == "the scratch was renamed into place":
        os.replace(scratch, target)
    elif after_the_kill == "emptied":
        target.write_bytes(b"")
    elif after_the_kill == "deleted":
        target.unlink()
    assert boundary.adopted() is adopted


def test_an_adopted_write_fails_the_integrity_verdict_not_only_the_category(tmp_path):
    class Adopts(_FakeService):
        def interrupted_write_was_adopted(self):
            return True

    report = _run(tmp_path, service_factory=Adopts)
    result = report["categories"]["interrupted_writes"]
    assert result["passed"] is False and result["state_integrity_verified"] is False
    # A category that fails says why, and so does the run.
    assert any("adopted" in failure for failure in result["failures"])
    assert any("interrupted_writes did not pass" in failure and "adopted" in failure
               for failure in report["qualification_failures"])


def test_scratch_left_after_the_restart_fails_the_interrupted_write(tmp_path, shipped):
    """The killed save's scratch holds the conversation that never committed.
    The release removes it at startup; one that does not leaves customer
    content outside every file that export, deletion and backup cover."""

    class KeepsScratch(_FakeService):
        def _remove_scratch(self):
            return None

    result = _run(tmp_path, service_factory=KeepsScratch)["categories"]["interrupted_writes"]
    assert result["passed"] is False
    assert result["detail"]["scratch_files_after_restart"]
    assert any("scratch" in failure for failure in result["failures"])
    clean = shipped.report["categories"]["interrupted_writes"]
    assert clean["passed"] is True and clean["detail"]["scratch_files_after_restart"] == []


# ── the configuration the release ran with ──────────────────────────────────


def _real_service(tmp_path, **kwargs):
    service = failure_drill._Service(
        tmp_path, tmp_path / "state", auth_file=tmp_path / "auth", token="test",
        audit_key=AUDIT_KEY, endpoint="http://127.0.0.1:9/v1/messages", label="test",
        **kwargs)
    service.port = 1
    return service


def test_a_qualifying_drill_leaves_every_timer_to_the_release(tmp_path):
    environment = _real_service(tmp_path)._environment()
    budgets = {name: value for name, value in environment.items()
               if name.startswith("PI_") and name.endswith(BUDGET_SUFFIXES)}
    assert not budgets, (
        f"the drill starts the release with its own timers and retry budget, "
        f"{budgets}, not the ones it ships with, so what it measures (a lease "
        f"recovery of 60 seconds where the shipped lease is 300) is not what an "
        f"operator gets (audit F-18)")


def _recorded(report):
    configuration = report.get("configuration", {})
    return set(configuration.get("wiring", {})) | {
        entry["setting"] for entry in configuration.get("overrides", [])}


def test_the_report_records_every_setting_the_drill_gives_the_release(tmp_path, shipped):
    """The class guard: a variable added to the service's environment without
    a line in the report fails here."""
    environment = _real_service(tmp_path)._environment()
    recorded = _recorded(shipped.report)
    unrecorded = sorted(set(environment) - PROCESS_PLUMBING - recorded)
    assert not unrecorded, (
        f"the release is started with {unrecorded} and the report does not "
        f"record them: a reader cannot tell what configuration the drill "
        f"measured (audit F-18)")
    assert sorted(recorded - set(environment)) == [], "the report records what was not set"


def test_a_rehearsals_overrides_reach_the_release_and_the_report(tmp_path):
    overrides = {"PI_MAX_CONCURRENT_TURNS": "4", "PI_MODEL": "another-model"}
    report = _run(tmp_path, service_settings=overrides)
    environment = _real_service(tmp_path, settings=overrides)._environment()
    assert {name: environment[name] for name in overrides} == overrides
    assert _recorded(report) == set(environment) - PROCESS_PLUMBING
    # Nothing the report says about the configuration is a secret or a host path.
    rendered = json.dumps(report["configuration"])
    assert AUDIT_KEY not in rendered and str(tmp_path) not in rendered


def _configuration_failures(report):
    return [failure for failure in report["qualification_failures"]
            if "shorter" in failure or "effective settings" in failure
            or "as in force" in failure or "what it ships for" in failure]


def test_a_drill_at_the_shipped_configuration_records_it_and_stays_eligible_for_it(shipped):
    report = shipped.report
    configuration = report["configuration"]
    assert configuration["overrides"] == [] and configuration["shortened"] == []
    assert configuration["effective_settings"] == SHIPPED_SETTINGS
    assert _configuration_failures(report) == []
    assert report["qualification_failures"] == [
        "service was injected rather than the real release"]


def test_a_run_with_shortened_timers_cannot_qualify(tmp_path):
    report = _run(tmp_path, service_settings={
        "PI_TURN_LEASE_SECONDS": "60", "PI_TURN_DEADLINE_SECONDS": "30",
        "PI_MCP_TIMEOUT_SECONDS": "30", "PI_LLM_RETRIES": "1"})
    configuration = report["configuration"]
    assert configuration["shortened"] == [
        "PI_LLM_RETRIES", "PI_MCP_TIMEOUT_SECONDS", "PI_TURN_DEADLINE_SECONDS",
        "PI_TURN_LEASE_SECONDS"]
    assert {entry["setting"]: (entry["value"], entry["shipped_default"], entry["shortened"])
            for entry in configuration["overrides"]} == {
        "PI_LLM_RETRIES": ("1", 2, True),
        "PI_MCP_TIMEOUT_SECONDS": ("30", 90, True),
        "PI_TURN_DEADLINE_SECONDS": ("30", 120, True),
        "PI_TURN_LEASE_SECONDS": ("60", 300, True)}
    assert configuration["effective_settings"]["turn_lease_seconds"] == 60
    [failure] = _configuration_failures(report)
    for expected in ("PI_TURN_LEASE_SECONDS=60 (shipped 300)",
                     "PI_TURN_DEADLINE_SECONDS=30 (shipped 120)",
                     "PI_MCP_TIMEOUT_SECONDS=30 (shipped 90)",
                     "PI_LLM_RETRIES=1 (shipped 2)"):
        assert expected in failure
    assert report["qualification_eligible"] is False
    # Every category still ran and passed: the run is a rehearsal, not a failure.
    assert all(result["passed"] for result in report["categories"].values())


def test_a_lengthened_timer_or_another_limit_is_recorded_and_does_not_disqualify(tmp_path):
    report = _run(tmp_path, service_settings={
        "PI_TURN_LEASE_SECONDS": "900", "PI_MAX_CONCURRENT_TURNS": "1"})
    configuration = report["configuration"]
    assert configuration["shortened"] == []
    assert [(entry["setting"], entry["value"], entry["shipped_default"], entry["shortened"])
            for entry in configuration["overrides"]] == [
        ("PI_MAX_CONCURRENT_TURNS", "1", 2, False),
        ("PI_TURN_LEASE_SECONDS", "900", 300, False)]
    assert _configuration_failures(report) == []


_BUDGETS = sorted(name for name in SHIPPED_SETTINGS
                  if name.endswith(tuple(suffix.lower() for suffix in BUDGET_SUFFIXES)))


@settings(max_examples=200, deadline=None)
@given(overrides=st.dictionaries(
    st.sampled_from(_BUDGETS), st.integers(min_value=1, max_value=1000), max_size=5))
# The boundary, which random draws rarely land on: exactly what ships is not
# shortened, one less is, one more is not.
@example(overrides={name: SHIPPED_SETTINGS[name] for name in _BUDGETS})
@example(overrides={name: SHIPPED_SETTINGS[name] - 1 for name in _BUDGETS})
@example(overrides={name: SHIPPED_SETTINGS[name] + 1 for name in _BUDGETS})
def test_any_timer_below_its_shipped_value_disqualifies_and_nothing_else_does(overrides):
    asked = {f"PI_{name.upper()}": str(value) for name, value in overrides.items()}
    in_force = {**SHIPPED_SETTINGS, **overrides}
    record, failures = failure_drill._configuration(
        asked, shipped=dict(SHIPPED_SETTINGS), in_force=in_force,
        endpoint="http://127.0.0.1:9/v1/messages")
    shortened = sorted(f"PI_{name.upper()}" for name, value in overrides.items()
                       if value < SHIPPED_SETTINGS[name])
    assert record["shortened"] == shortened
    assert bool(failures) == bool(shortened)
    assert [entry["setting"] for entry in record["overrides"]] == sorted(asked)


@pytest.mark.parametrize("case", ["release reports nothing", "override not applied",
                                  "shipped value unknown"])
def test_a_configuration_that_cannot_be_confirmed_does_not_qualify(case):
    asked = {"PI_TURN_LEASE_SECONDS": "400"}
    shipped, in_force = dict(SHIPPED_SETTINGS), {**SHIPPED_SETTINGS, "turn_lease_seconds": 400}
    if case == "release reports nothing":
        asked, shipped, in_force = {}, None, None
    elif case == "override not applied":
        in_force = dict(SHIPPED_SETTINGS)
    else:
        del shipped["turn_lease_seconds"]
    record, failures = failure_drill._configuration(
        asked, shipped=shipped, in_force=in_force, endpoint="http://127.0.0.1:9/v1/messages")
    assert len(failures) == 1, failures
    assert record["effective_settings"] == in_force


def test_a_release_that_reports_no_settings_is_recorded_as_such_and_cannot_qualify(tmp_path):
    class Silent(_FakeService):
        def effective_settings(self):
            return None

    report = _run(tmp_path, service_factory=Silent)
    assert report["configuration"]["effective_settings"] is None
    assert any("effective settings" in failure for failure in report["qualification_failures"])
    assert all(result["passed"] for result in report["categories"].values())


@pytest.mark.parametrize("fault", ["metrics fail", "never ready", "does not drain"])
def test_a_release_that_cannot_report_its_configuration_still_gets_a_report(tmp_path, fault):
    stopped = []

    class Broken(_FakeService):
        def start(self):
            if fault == "never ready" and self.label.startswith("configuration"):
                raise FailureDrillError("configuration service never became ready")
            return super().start()

        def effective_settings(self):
            if fault == "metrics fail":
                raise FailureDrillError("/v1/metrics returned 500")
            return super().effective_settings()

        def drain(self):
            if fault == "does not drain" and self.label.startswith("configuration"):
                raise FailureDrillError("configuration service did not drain cleanly")
            super().drain()

        def kill(self):
            stopped.append(self.label)
            super().kill()

    report = _run(tmp_path, service_factory=Broken)
    expected = {"metrics fail": "/v1/metrics returned 500", "never ready": "never became ready",
                "does not drain": "did not drain cleanly"}[fault]
    assert report["configuration"]["effective_settings"] is None
    [failure] = [failure for failure in report["qualification_failures"]
                 if "configuration could not be read" in failure]
    assert expected in failure
    assert "configuration-shipped" in stopped, "the release that failed was left running"
    assert set(report["categories"]) == set(failure_drill.CATEGORIES)
    assert all(result["passed"] for result in report["categories"].values())


@pytest.mark.parametrize("name,value", [
    ("PI_STATE", "/elsewhere"), ("PI_ENDPOINT", "https://example.invalid"),
    ("PI_AUDIT_KEY", "x" * 40), ("ANTHROPIC_API_KEY", "x"), ("PATH", "/bin"),
    ("LD_PRELOAD", "/tmp/x.so"), ("pi_turn_lease_seconds", "60"),
    ("PI_TURN_LEASE_SECONDS", ""), ("PI_TURN_LEASE_SECONDS", 60),
    # The report records an override in full, so its value must not be a
    # secret; and the harness speaks plain HTTP to the release it starts.
    ("PI_SECRET_GITHUB", "a-token"), ("PI_TLS_CERT", "/cert.pem"),
    ("PI_TLS_KEY", "/key.pem")])
def test_only_release_settings_other_than_the_wiring_can_be_overridden(tmp_path, name, value):
    with pytest.raises(FailureDrillError, match="service setting"):
        run_drill(artifact="unused", audit_key_file="unused", evidence_url=EVIDENCE_URL,
                  output=tmp_path / "report.json", work_dir=tmp_path / "work",
                  service_settings={name: value})
    assert not (tmp_path / "work").exists()


def test_the_harness_waits_as_long_as_the_release_in_force_may_take(tmp_path, monkeypatch):
    """With the shipped timers a turn may run 120 seconds and a crashed turn's
    lease lasts 300. A harness that still hung up after 45 seconds, or gave up
    on the lease after 65, would fail a release for being configured as shipped."""
    asked = []
    monkeypatch.setattr(
        failure_drill, "_request",
        lambda method, url, token, payload=None, timeout=5:
            asked.append((url.rsplit("/", 1)[-1], timeout)) or (200, {}))
    shipped = _real_service(tmp_path)
    shipped.chat("s", "m")
    rehearsal = _real_service(tmp_path, waits={
        "turn_deadline_seconds": 30, "turn_lease_seconds": 60, "drain_timeout_seconds": 30})
    rehearsal.chat("s", "m")
    assert asked == [("chat", 135), ("chat", 45)]
    assert (shipped.lease_wait_s, rehearsal.lease_wait_s) == (305, 65)
    assert (shipped.drain_wait_s, rehearsal.drain_wait_s) == (135, 45)


@pytest.mark.parametrize("settings,reported,expected", [
    ({}, None, (120, 120, 300)),
    ({}, {"turn_deadline_seconds": 200, "drain_timeout_seconds": 90,
          "turn_lease_seconds": 600}, (200, 90, 600)),
    ({"PI_TURN_DEADLINE_SECONDS": "30", "PI_TURN_LEASE_SECONDS": "60"}, None, (30, 120, 60)),
    ({"PI_TURN_LEASE_SECONDS": "60"}, {"turn_lease_seconds": 45}, (120, 120, 45)),
    ({"PI_TURN_LEASE_SECONDS": "soon"}, {"turn_lease_seconds": "45", "drain_timeout_seconds": 0,
                                         "turn_deadline_seconds": True}, (120, 120, 300)),
])
def test_the_waits_follow_what_the_release_reports_then_what_was_asked(
        settings, reported, expected):
    waits = failure_drill._waits(settings, reported)
    assert (waits["turn_deadline_seconds"], waits["drain_timeout_seconds"],
            waits["turn_lease_seconds"]) == expected


def test_the_command_line_passes_a_rehearsals_settings_through(tmp_path, monkeypatch, capsys):
    asked = {}

    def recorded_run(**kwargs):
        asked.update(kwargs)
        return {"qualification_eligible": True}

    monkeypatch.setattr(failure_drill, "run_drill", recorded_run)
    arguments = ["failure_drill.py", "--artifact", "a.tar.gz", "--audit-key-file", "key",
                 "--evidence-url", EVIDENCE_URL, "--output", str(tmp_path / "report.json")]
    monkeypatch.setattr(sys, "argv", arguments)
    failure_drill.main()
    assert asked["service_settings"] == {}, "a plain run overrides nothing"

    monkeypatch.setattr(sys, "argv", arguments + [
        "--service-setting", "PI_TURN_LEASE_SECONDS=60",
        "--service-setting", "PI_MODEL=a=b"])
    failure_drill.main()
    assert asked["service_settings"] == {"PI_TURN_LEASE_SECONDS": "60", "PI_MODEL": "a=b"}

    for bad in (["--service-setting", "PI_TURN_LEASE_SECONDS"],
                ["--service-setting", "PI_MAX_STEPS=1", "--service-setting", "PI_MAX_STEPS=2"]):
        monkeypatch.setattr(sys, "argv", arguments + bad)
        with pytest.raises(SystemExit) as refusal:
            failure_drill.main()
        assert refusal.value.code == 2
    capsys.readouterr()


# ── source guards ───────────────────────────────────────────────────────────

_SOURCE = (PI_ROOT / "scripts" / "failure_drill.py").read_text()
_TREE = ast.parse(_SOURCE)


def _functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def test_an_injector_cannot_name_a_session_of_its_own():
    """The class of the defect: a fault sent to a session other than the one
    whose committed content is compared. Injectors reach a session only
    through the category's context, which knows exactly one."""
    injectors = [node for node in _functions(_TREE) if node.name.startswith("_inject_")]
    assert sorted(node.name for node in injectors) == sorted(
        f"_inject_{category}" for category in failure_drill.CATEGORIES)
    assert set(failure_drill.INJECTORS) == set(failure_drill.CATEGORIES)
    for injector in injectors:
        direct = [node.lineno for node in ast.walk(injector)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr in ("chat", "prepare_write_interruption")]
        assert not direct, (
            f"{injector.name} calls the service's chat or prepare_write_interruption "
            f"itself at line(s) {direct}: it must go through the context's turn() "
            f"and boot(), which address the committed session")
    callers = {node.name for node in _functions(_TREE)
               if any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                      and call.func.attr == "chat" for call in ast.walk(node))}
    assert callers == {"turn"}, (
        f"only _CategoryContext.turn may send a turn; found callers {sorted(callers)}")


def test_the_harness_sets_no_timer_of_its_own_anywhere():
    """No literal PI_* timer or retry budget in the drill's source: the only
    way one reaches the release is an override, which the report records."""
    literals = sorted({node.value for node in ast.walk(_TREE)
                       if isinstance(node, ast.Constant) and isinstance(node.value, str)
                       and node.value.startswith("PI_")
                       and node.value.endswith(BUDGET_SUFFIXES)})
    assert literals == [], (
        f"scripts/failure_drill.py names {literals}: a timer the harness sets is "
        f"a configuration the release does not ship with")
