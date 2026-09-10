"""SIGIL never-claimed finalization; actual service behavior is tested separately."""
import json

import pytest

from api_support import credential
from conftest import forge_ok, needs_toolchain
from dispatch_support import change_snapshot, initial
from scripts.compose_application import compose_application
from conftest import SIGIL_ROOT
from settlement_support import snapshot
from test_dispatch import tool_view
from turn_support import FUEL, fields, record, refused


@pytest.fixture(scope="module")
def program():
    needs_toolchain()
    return compose_application("preclaim", SIGIL_ROOT).text


@pytest.fixture(scope="module")
def model(mcp):
    return initial(mcp)


def incoming(model, *, now=151, row=None):
    return [model[0] if row is None else row["facts"], model[1], model[2], "same-session", model[9],
            "executor.claim", "executor.delivery", str(now), model[4], model[5], model[6], model[7], model[8],
            snapshot("", 0), snapshot("", 0)]


def plan(mcp, program, values):
    kind, reason, raw = fields(forge_ok(mcp, program, record("UF1\n", values), fuel=FUEL), "TX1\n", 3)
    return kind, reason, json.loads(raw) if raw else None


def test_eligible_is_only_a_noop_not_a_dispatch_grant(mcp, program, model):
    assert plan(mcp, program, incoming(model)) == ("eligible", "", None)


@pytest.mark.parametrize("now,row,reason", [(270, None, "deadline_exceeded"), (271, None, "deadline_exceeded"),
    (151, credential(expires=151), "credential_inactive"), (151, credential(before=152), "credential_inactive")])
def test_never_claimed_expiry_atomically_finalizes_state_operation_and_capacity(mcp, program, model, now, row, reason):
    phase, actual_reason, batch = plan(mcp, program, incoming(model, now=now, row=row))
    assert (phase, actual_reason) == ("failed", reason)
    assert [(c["namespace"], c["revision"]) for c in batch["checks"]] == [
        ("a.intent", 1), ("executor.claim", 0), ("executor.delivery", 0)]
    assert [(w["namespace"], w["revision"]) for w in batch["writes"]] == [
        ("a.operations", 1), ("a.reservation", 1), ("a.budget", 1), ("a.state", 1)]
    operation = fields(batch["writes"][0]["value"], "OQ3\n", 13)
    state = fields(batch["writes"][3]["value"], "PT1\n", 17)
    assert operation[9] == state[1] == "failed" and operation[12] == "2"
    assert state[10:13] == ["0", "0", "1"] and state[14] == reason
    assert fields(batch["writes"][2]["value"], "BH1\n", 5)[2:] == ["0", "0", "0"]


@pytest.mark.parametrize("index,value,reason,accounting,held", [
    (12, "0", "usage_unknown", "unknown", ["0", "20000", "4096"]),
    (10, "20000", "quota_exhausted", "reported", ["0", "0", "0"]),
    (10, "20001", "quota_exhausted", "overrun", ["0", "20000", "4096"]),
    (11, "3073", "quota_exhausted", "reported", ["0", "0", "0"])])
def test_unknown_and_exhausted_usage_preserve_actual_accounting(mcp, program, model, index, value, reason, accounting, held):
    changed = list(model)
    change_snapshot(changed, 6, "PT1\n", index, value)
    phase, found, batch = plan(mcp, program, incoming(changed))
    assert (phase, found) == ("failed", reason)
    assert fields(batch["writes"][1]["value"], "BR2\n", 13)[7] == accounting
    assert fields(batch["writes"][2]["value"], "BH1\n", 5)[2:] == held


@pytest.mark.parametrize("index,seen", [(13, snapshot("claim", 1)), (13, snapshot("", 2)),
    (14, snapshot("delivery", 1)), (14, snapshot("", 2)), (13, snapshot("phantom", 0))])
def test_claimed_or_observed_work_is_never_reclassified_as_unsent(mcp, program, model, index, seen):
    values = incoming(model, now=270)
    values[index] = seen
    refused(mcp, program, record("UF1\n", values), 409)


@pytest.mark.parametrize("field,value", [(0, "other"), (1, "2"), (2, "tool"), (4, "forged payload")])
def test_current_intent_must_match_the_exact_state_and_payload(mcp, program, model, field, value):
    changed = list(model)
    change_snapshot(changed, 7, "SI1\n", field, value)
    refused(mcp, program, record("UF1\n", incoming(changed, now=270)), 409)


@pytest.mark.parametrize("index,marker,field,value", [(4, "OQ2\n", 10, "f" * 64),
    (5, "BR1\n", 4, "20001"), (8, "BH1\n", 2, "0"), (6, "PT1\n", 16, "other-key")])
def test_original_settlement_invariants_cannot_be_bypassed(mcp, program, model, index, marker, field, value):
    changed = list(model)
    change_snapshot(changed, index, marker, field, value)
    refused(mcp, program, record("UF1\n", incoming(changed, now=270)), 409)


@pytest.mark.parametrize("known,inputs,outputs,expected", [("1", "0", "0", None), ("1", "19999", "3072", None),
    ("1", "20000", "0", "quota_exhausted"), ("1", "0", "3073", "quota_exhausted"),
    ("0", "0", "0", "usage_unknown"), ("0", "20000", "3073", "usage_unknown")])
def test_dispatch_and_finalization_agree_at_exact_allowance_boundaries(mcp, program, model, known, inputs, outputs, expected):
    changed = list(model)
    for index, value in [(12, known), (10, inputs), (11, outputs)]:
        change_snapshot(changed, 6, "PT1\n", index, value)
    dispatch = compose_application("dispatch", SIGIL_ROOT).text
    if expected is None:
        assert mcp.forge(dispatch, input=record("DF1\n", changed), fuel=FUEL)["status"] == "ok"
        assert plan(mcp, program, incoming(changed)) == ("eligible", "", None)
    else:
        refused(mcp, dispatch, record("DF1\n", changed), 429)
        assert plan(mcp, program, incoming(changed))[:2] == ("failed", expected)


@pytest.mark.parametrize("now", [151, 270])
@pytest.mark.parametrize("kwargs", [{"principal": "bob"}, {"tenant": "other"}, {"epoch": "new"},
    {"scopes": []}, {"tools": []}, {"prefix": "other"}, {"turn_seconds": 121}])
def test_changed_owner_or_policy_cannot_reconcile_an_old_operation(mcp, program, model, now, kwargs):
    refused(mcp, program, record("UF1\n", incoming(model, now=now, row=credential(**kwargs))), 409)


@pytest.mark.parametrize("usage", [None, {"input_tokens": 7, "output_tokens": 3}])
@pytest.mark.parametrize("now", [151, 270])
def test_unclaimed_tool_preserves_preceding_model_usage_and_checks_its_own_deadline(mcp, program, model, usage, now):
    values = tool_view(mcp, model, usage=usage)
    result = plan(mcp, program, incoming(values, now=now))
    if now == 151:
        # Unknown model usage forbids another model call, not this already requested file read.
        assert result == ("eligible", "", None)
    else:
        assert result[:2] == ("failed", "deadline_exceeded")
        state = fields(result[2]["writes"][3]["value"], "PT1\n", 17)
        assert state[10:13] == (["0", "0", "0"] if usage is None else ["7", "3", "1"])
        operation = fields(result[2]["writes"][0]["value"], "OQ3\n", 13)
        assert operation[12] == "3"
        reservation = fields(result[2]["writes"][1]["value"], "BR2\n", 13)
        assert reservation[7] == ("unknown" if usage is None else "reported")


@pytest.mark.parametrize("index,value", [(3, "other_tool"), (4, '{"path":"different"}')])
def test_unclaimed_tool_requires_exact_pending_name_and_payload(mcp, program, model, index, value):
    values = tool_view(mcp, model)
    change_snapshot(values, 7, "SI1\n", index, value)
    refused(mcp, program, record("UF1\n", incoming(values, now=270)), 409)
