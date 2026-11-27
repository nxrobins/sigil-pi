"""Shared SIGIL executor commits, through the real pin and scoped native store."""

import pytest

from conftest import PI_ROOT, SIGIL_ROOT, needs_toolchain
from executor_support import executor_plan, executor_snapshot
from scripts.compose_application import compose_application
from store_support import NativeStore, mutation
from turn_support import fields, record, refused


INTENT = {"revision": 1, "value": "opaque application intent 😀"}
GRANTS = {"pi-a.intent": "read", "pi-a.dispatch": "read_write", "pi-a.delivery": "create_only"}


@pytest.fixture(scope="module")
def program():
    needs_toolchain()
    return compose_application("executor_transaction", SIGIL_ROOT).text


def dispatch(phase, *, owner="executor-1", revision=1, intent_revision="1", prefix="pi-a", key="operation-1:1"):
    return {"revision": revision, "value": record("SD1\n", [f"{prefix}.intent", key, intent_revision, owner, str(phase)])}


@pytest.mark.parametrize("phase", range(7))
@pytest.mark.parametrize("event", range(6))
def test_all_delivery_kernel_transitions_have_correct_atomic_requests_or_noop(mcp, program, phase, event):
    prior = None if phase == 0 else dispatch(phase)
    facts = {"event": event, "outcome": "returned" if event == 1 else "", "payload": "observed" if event == 1 else ""}
    expected = phase if phase >= 2 else ({0: 1, 3: 0, 4: 5, 5: 6}.get(event) if phase == 0
                                       else {1: 2, 2: 3, 3: 4, 4: 4, 5: 4}.get(event))
    if expected is None:
        refused(mcp, program, executor_snapshot(INTENT, prior, **facts), 409)
        return
    decision = executor_plan(mcp, program, INTENT, prior, **facts)
    assert decision.phase == str(expected)
    if expected == phase:
        assert decision.continuation == "none" and decision.raw == ""
        return
    assert decision.continuation == ("dispatch_after_commit" if expected == 1 else "record_after_commit")
    request = decision.request
    assert request["op"] == "commit"
    assert request["checks"] == [{"namespace": "pi-a.intent", "key": "operation-1:1", "revision": 1}]
    writes = request["writes"]
    assert len(writes) == (1 if expected == 1 else 2)
    expected_record = ["pi-a.intent", "operation-1:1", "1", "executor-1", str(expected)]
    assert writes[0] == mutation("pi-a.dispatch", "operation-1:1", record("SD1\n", expected_record), 0 if phase == 0 else 1)
    if expected != 1:
        assert writes[1] == mutation("pi-a.delivery", "operation-1:1",
                                     record("DR1\n", expected_record + [facts["outcome"], facts["payload"]]))


@pytest.mark.parametrize("event", [1, 2, 4, 5])
def test_nonowning_generation_cannot_supply_live_observation_or_cancellation(mcp, program, event):
    refused(mcp, program, executor_snapshot(INTENT, dispatch(1), event=event, generation="another-worker",
                                            outcome="returned" if event == 1 else ""), 409)


def test_owner_loss_preserves_the_claiming_identity_not_the_recovery_worker(mcp, program):
    result = executor_plan(mcp, program, INTENT, dispatch(1), event=3, generation="replacement-worker")
    assert result.phase == "4" and result.continuation == "record_after_commit"
    saved = fields(result.request["writes"][1]["value"], "DR1\n", 7)
    assert saved[3:] == ["executor-1", "4", "", ""]


@pytest.mark.parametrize("index,replacement", [(0, "other.intent"), (1, "other-key"), (2, "2")])
def test_dispatch_record_must_match_exact_intent_coordinate_and_revision(mcp, program, index, replacement):
    prior = dispatch(1)
    values = fields(prior["value"], "SD1\n", 5)
    values[index] = replacement
    prior["value"] = record("SD1\n", values)
    refused(mcp, program, executor_snapshot(INTENT, prior, event=1, outcome="returned"), 409)


@pytest.mark.parametrize("index,replacement", [(0, ""), (0, "../escape"), (1, "x\n"),
    (2, "😀"), (3, "x" * 257), (4, "0"), (4, "01"), (4, "9223372036854775808"),
    (6, "-1"), (6, "1.0"), (8, "6"), (8, "00"), (9, ""), (9, "worker/1")])
def test_invalid_snapshot_fields_cannot_emit_a_commit(mcp, program, index, replacement):
    values = fields(executor_snapshot(INTENT), "EX1\n", 12)
    values[index] = replacement
    refused(mcp, program, record("EX1\n", values), 400)


@pytest.mark.parametrize("left,right", [(0, 1), (0, 2), (1, 2)])
def test_executor_namespaces_must_be_distinct(mcp, program, left, right):
    values = fields(executor_snapshot(INTENT), "EX1\n", 12)
    values[left] = values[right]
    refused(mcp, program, record("EX1\n", values), 400)


def test_record_presence_revision_versions_and_terminal_phase_are_strict(mcp, program):
    prior = dispatch(1, revision=0)
    refused(mcp, program, executor_snapshot(INTENT, prior), 409)
    refused(mcp, program, executor_snapshot(INTENT, {"revision": 1, "value": None}), 400)
    for phase in (0, 7, "01"):
        refused(mcp, program, executor_snapshot(INTENT, dispatch(phase)), 400)
    for bad in ("", "EX2\n", executor_snapshot(INTENT) + "extra"):
        refused(mcp, program, bad, 400)


def test_only_observed_response_can_carry_outcome_and_payload(mcp, program):
    for event in (0, 2, 3, 4, 5):
        refused(mcp, program, executor_snapshot(INTENT, dispatch(1), event=event, outcome="returned"), 400)
        refused(mcp, program, executor_snapshot(INTENT, dispatch(1), event=event, payload="invented result"), 400)
    for bad in ("", "success", "unknown"):
        refused(mcp, program, executor_snapshot(INTENT, dispatch(1), event=1, outcome=bad), 400)
    failed = executor_plan(mcp, program, INTENT, dispatch(1), event=1, outcome="failed", payload="observed failure")
    assert fields(failed.request["writes"][1]["value"], "DR1\n", 7)[4:] == ["2", "failed", "observed failure"]


def test_bounded_observation_and_empty_intent_are_refused_before_output(mcp, program):
    refused(mcp, program, executor_snapshot({"revision": 1, "value": ""}), 413)
    refused(mcp, program, executor_snapshot(INTENT, dispatch(1), event=1, outcome="returned", payload="x" * 1048577), 413)


@pytest.mark.parametrize("revision", [9007199254740993, 9223372036854775807])
def test_executor_preserves_full_native_revision_precision(mcp, program, revision):
    intent = {**INTENT, "revision": revision}
    result = executor_plan(mcp, program, intent, dispatch(1, revision=revision, intent_revision=str(revision)), event=3)
    assert result.request["checks"][0]["revision"] == revision
    assert result.request["writes"][0]["revision"] == revision
    assert fields(result.request["writes"][1]["value"], "DR1\n", 7)[2] == str(revision)


def seed(native_store_binary, root):
    with NativeStore(native_store_binary, root, {"pi-a.intent": "create_only"}, initialize=True) as app:
        app.commit([mutation("pi-a.intent", "operation-1:1", INTENT["value"])])


def test_exact_sigil_claim_delivery_commits_survive_restart_and_replays_cannot_rewrite(
        mcp, program, native_store_binary, tmp_path):
    root = tmp_path / "records"
    seed(native_store_binary, root)
    with NativeStore(native_store_binary, root, GRANTS) as executor:
        claim = executor_plan(mcp, program, executor.get("pi-a.intent", "operation-1:1"))
        claim.commit(executor)
        executor.kill()
    with NativeStore(native_store_binary, root, GRANTS) as executor:
        assert executor.raw(claim.raw)["code"] == "conflict"
        saved = executor.get("pi-a.dispatch", "operation-1:1")
        complete = executor_plan(mcp, program, INTENT, saved, event=1, outcome="returned", payload="fact 😀\x00\n")
        complete.commit(executor)
        executor.kill()
    with NativeStore(native_store_binary, root, GRANTS) as executor:
        assert executor.raw(complete.raw)["code"] == "conflict"
        terminal = executor.get("pi-a.dispatch", "operation-1:1")
        late = executor_plan(mcp, program, INTENT, terminal, event=1, outcome="returned", payload="late replacement")
        assert late.raw == "" and late.continuation == "none"
        assert executor.request({"op": "get", "namespace": "pi-a.delivery", "key": "operation-1:1"})["code"] == "denied"
    with NativeStore(native_store_binary, root, {"pi-a.delivery": "read"}) as app:
        assert fields(app.get("pi-a.delivery", "operation-1:1")["value"], "DR1\n", 7)[6] == "fact 😀\x00\n"


def test_existing_delivery_rolls_back_dispatch_completion(mcp, program, native_store_binary, tmp_path):
    root = tmp_path / "records"
    seed(native_store_binary, root)
    with NativeStore(native_store_binary, root, GRANTS) as executor:
        claim = executor_plan(mcp, program, INTENT)
        claim.commit(executor)
        saved = executor.get("pi-a.dispatch", "operation-1:1")
        executor.commit([mutation("pi-a.delivery", "operation-1:1", "pre-existing")])
        complete = executor_plan(mcp, program, INTENT, saved, event=3)
        assert executor.raw(complete.raw)["code"] == "conflict"
        assert executor.get("pi-a.dispatch", "operation-1:1") == saved


def test_changed_intent_revision_prevents_claim_without_partial_dispatch_record(mcp, program, native_store_binary, tmp_path):
    root = tmp_path / "records"
    seed(native_store_binary, root)
    planned = executor_plan(mcp, program, INTENT)
    with NativeStore(native_store_binary, root, {"pi-a.intent": "read_write"}) as maintenance:
        maintenance.commit([mutation("pi-a.intent", "operation-1:1", "replaced", 1)])
    with NativeStore(native_store_binary, root, GRANTS) as executor:
        assert executor.raw(planned.raw)["code"] == "conflict"
        assert executor.get("pi-a.dispatch", "operation-1:1") == {"revision": 0, "value": None}


def test_native_executor_scope_rejects_caller_selected_domain_write(mcp, program, native_store_binary, tmp_path):
    from conftest import forge_ok
    from executor_support import ExecutorDecision
    from turn_support import FUEL
    root = tmp_path / "records"
    seed(native_store_binary, root)
    values = fields(executor_snapshot(INTENT), "EX1\n", 12)
    values[1] = "pi-a.state"
    raw = forge_ok(mcp, program, record("EX1\n", values), fuel=FUEL)
    forged = ExecutorDecision(*fields(raw, "ER1\n", 3))
    with NativeStore(native_store_binary, root, GRANTS) as executor:
        assert executor.raw(forged.raw)["code"] == "denied"
        assert executor.get("pi-a.dispatch", "operation-1:1") == {"revision": 0, "value": None}


def test_shared_executor_has_no_pi_or_control_plane_policy_or_native_authority():
    source = (PI_ROOT / "app/shared/executor_transaction.sigil").read_text()
    stripped = "\n".join(line.split("//", 1)[0] for line in source.splitlines())
    assert 'extern "C"' not in stripped and "#[trusted]" not in stripped
    for forbidden in ('"model"', '"tool"', '"conversation"', '"company"', '"SO1\\n"', '"SI1\\n"'):
        assert forbidden not in stripped
    integration = (PI_ROOT / "tests/test_durable_turn.py").read_text()
    assert "mutation(" not in integration and "check(" not in integration
