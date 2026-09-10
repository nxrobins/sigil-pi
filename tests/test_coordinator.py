"""Pure SIGIL coordinator decisions; HTTP integration is qualified separately."""
import json

import pytest

from api_support import credential
from conftest import SIGIL_ROOT, forge_ok, needs_toolchain
from dispatch_support import initial
from scripts.compose_application import compose_application
from settlement_support import snapshot
from turn_support import FUEL, fields, record, refused

OP = "a" * 64
GEN = "e" * 64


@pytest.fixture(scope="module")
def program():
    needs_toolchain()
    return compose_application("coordinator", SIGIL_ROOT).text


@pytest.fixture(scope="module")
def admitted(mcp):
    return initial(mcp)


def carry(step, *, alias="provider", generation="", cursor=OP):
    return record("KC1\n", [step, cursor, OP, "same-session", OP + ":1", OP + ":2", alias, generation])


def envelope(*, stage="init", observation="", continuation="", active=None):
    row = credential()
    aliases = active or [record("AF1\n", ["provider", "", ""]), record("AF1\n", ["reader", "", ""])]
    return [row["facts"], "b" * 64, record("LB3\n", ["provider", "reader", "interpret", "settle", "preclaim"]),
            "151", stage, observation, continuation, json.dumps(aliases), json.dumps(["interpret", "settle", "preclaim"]),
            json.dumps([*row["grants"], "executor.claim", "executor.delivery"]), "executor.claim", "executor.delivery"]


def decision(mcp, program, **kwargs):
    return fields(forge_ok(mcp, program, record("CL1\n", envelope(**kwargs)), fuel=FUEL), "LC1\n", 6)


def test_boot_and_initial_scan_use_only_fixed_operation_namespace(mcp, program):
    assert decision(mcp, program, stage="boot") == ["ready", "", "", "", "", "0"]
    selected = decision(mcp, program)
    assert selected[:4] == ["keys", "a.operations", "", "1"]
    assert fields(selected[4], "KC1\n", 8)[0] == "keys"


def test_discovery_cursor_is_a_hint_and_empty_pages_restart_the_scan(mcp, program):
    selected = decision(mcp, program, continuation=carry("keys"), observation=record("KP1\n", [json.dumps([OP]), OP]))
    assert selected[:4] == ["read", "a.operations", OP, ""]
    selected = decision(mcp, program, continuation=carry("keys"), observation=record("KP1\n", ["[]", ""]))
    assert selected[:4] == ["yield", "", "", ""] and selected[5] == "250"
    assert fields(selected[4], "KC1\n", 8)[:2] == ["scan", ""]


def test_actual_operation_and_state_select_the_first_model_action(mcp, program, admitted):
    selected = decision(mcp, program, continuation=carry("op"), observation=admitted[4])
    assert selected[:4] == ["read", "a.state", "same-session", ""]
    selected = decision(mcp, program, continuation=carry("state"), observation=admitted[6])
    assert selected[:4] == ["read", "executor.delivery", OP + ":1", ""]
    assert fields(selected[4], "KC1\n", 8)[4:7] == [OP + ":1", OP + ":2", "provider"]


@pytest.mark.parametrize("revision", [0, 2, 3])
def test_nonaccepted_operation_revisions_do_not_dispatch(mcp, program, revision):
    selected = decision(mcp, program, continuation=carry("op"), observation=snapshot("opaque", revision))
    assert selected[0] == "yield"


def test_delivery_selects_actual_interpretation_and_absence_selects_claim_lookup(mcp, program):
    selected = decision(mcp, program, continuation=carry("delivery"), observation=snapshot("actual-delivery", 1))
    assert selected[0:2] == ["transaction", "interpret"]
    assert json.loads(selected[2]) == ["same-session", OP, OP + ":1", OP + ":2"]
    selected = decision(mcp, program, continuation=carry("delivery"), observation=snapshot("", 0))
    assert selected[:4] == ["read", "executor.claim", OP + ":1", ""]


def test_unclaimed_action_is_checked_and_abandoned_claim_is_recovered_not_started(mcp, program):
    selected = decision(mcp, program, continuation=carry("claim"), observation=snapshot("", 0))
    assert selected[:2] == ["transaction", "preclaim"]
    assert json.loads(selected[2]) == [OP, "same-session", OP + ":1", OP + ":cancel"]
    selected = decision(mcp, program, continuation=carry("claim"), observation=snapshot("native-claim", 1))
    assert selected[:2] == ["recover", "provider"]
    assert json.loads(selected[2]) == ["a.intent", OP + ":1"]


def test_only_actual_eligible_noop_selects_existing_policy_bound_start(mcp, program):
    selected = decision(mcp, program, stage="transaction", continuation=carry("preclaim"),
        observation=json.dumps({"context": ["eligible", ""], "receipt": None}))
    assert selected[:2] == ["start", "provider"]
    assert json.loads(selected[2]) == [OP, "same-session", OP + ":1", OP + ":cancel"]
    assert fields(selected[4], "KC1\n", 8)[0] == "started"


@pytest.mark.parametrize("reason", ["deadline_exceeded", "credential_inactive", "usage_unknown", "quota_exhausted"])
def test_terminal_preclaim_commit_never_starts_or_settles_again(mcp, program, reason):
    selected = decision(mcp, program, stage="transaction", continuation=carry("preclaim"),
        observation=json.dumps({"context": ["failed", reason], "receipt": {"revision": 3}}))
    assert selected[:4] == ["yield", "", "", ""]
    assert fields(selected[4], "KC1\n", 8)[:2] == ["scan", OP]


@pytest.mark.parametrize("context,receipt", [(["eligible", ""], {"revision": 3}), (["eligible", "failure"], None),
    (["failed", "deadline_exceeded"], None), (["failed", "other"], {"revision": 3}),
    (["eligible", ""], 3), (["other", ""], None), (["failed", "usage_unknown"], {}),
    (["failed", "usage_unknown"], {"revision": 0}), (["failed", "usage_unknown"], {"revision": "3"}),
    (["failed", "usage_unknown"], {"revision": 9223372036854775808})])
def test_inconsistent_preclaim_result_cannot_trigger_an_effect(mcp, program, context, receipt):
    values = envelope(stage="transaction", continuation=carry("preclaim"),
        observation=json.dumps({"context": context, "receipt": receipt}))
    refused(mcp, program, record("CL1\n", values), 409)


@pytest.mark.parametrize("marker", ["LB1\n", "LB2\n"])
def test_previous_registry_binding_cannot_silently_skip_cancellation(mcp, program, marker):
    values = envelope(stage="boot")
    aliases = ["provider", "reader", "interpret", "settle"]
    values[2] = record(marker, aliases if marker == "LB1\n" else aliases + ["preclaim"])
    refused(mcp, program, record("CL1\n", values), 400)


def test_owned_claim_requires_matching_operation_and_generation(mcp, program):
    context = record("DX1\n", [OP, "1", "alice", "tenant-a", "b" * 64])
    active = [record("AF1\n", ["provider", GEN, context]), record("AF1\n", ["reader", "", ""])]
    selected = decision(mcp, program, continuation=carry("claim"), observation=snapshot("claim", 1), active=active)
    assert selected[:4] == ["read", "a.operations", OP + ":cancel", ""]
    assert fields(selected[4], "KC1\n", 8)[7] == GEN
    context = record("DX1\n", ["f" * 64, "1", "alice", "tenant-a", "b" * 64])
    active[0] = record("AF1\n", ["provider", GEN, context])
    selected = decision(mcp, program, continuation=carry("claim"), observation=snapshot("claim", 1), active=active)
    assert selected[0] == "yield" and selected[5] == "100"


def test_pending_poll_retains_generation_and_does_not_resend(mcp, program):
    selected = decision(mcp, program, continuation=carry("started"), observation=json.dumps({"generation": GEN}))
    assert selected[:4] == ["read", "a.operations", OP + ":cancel", ""]
    selected = decision(mcp, program, stage="read", continuation=selected[4], observation=snapshot("", 0))
    assert selected[:4] == ["poll", "provider", GEN, ""]
    selected = decision(mcp, program, continuation=carry("poll", generation=GEN), observation="null")
    assert selected[0] == "yield" and selected[5] == "50"
    selected = decision(mcp, program, stage="yield", continuation=selected[4])
    assert selected[:4] == ["read", "a.operations", OP + ":cancel", ""]
    selected = decision(mcp, program, stage="read", continuation=selected[4], observation=snapshot("", 0))
    assert selected[:4] == ["poll", "provider", GEN, ""]
    selected = decision(mcp, program, stage="poll", continuation=selected[4], observation="{\"actual\":true}")
    assert selected[:4] == ["read", "a.state", "same-session", ""]


def test_requested_cancellation_signals_only_the_held_generation_then_observes(mcp, program):
    requested = snapshot(record("CR1\n", [OP, "alice", "tenant-a", "b" * 64, "151"]))
    selected = decision(mcp, program, stage="read", continuation=carry("cancel_check", generation=GEN), observation=requested)
    assert selected[:4] == ["cancel", "provider", GEN, ""]
    selected = decision(mcp, program, stage="cancel", continuation=selected[4], observation="true")
    assert selected[:4] == ["poll", "provider", GEN, ""]


def test_actual_cancelled_preclaim_commit_rescans_without_another_settlement(mcp, program):
    selected = decision(mcp, program, stage="transaction", continuation=carry("preclaim"),
        observation=json.dumps({"context": ["cancelled", "cancelled"], "receipt": {"revision": 3}}))
    assert selected[:4] == ["yield", "", "", ""]


@pytest.mark.parametrize("stage,seen", [("read", "true"), ("cancel", '{"stopped":true}'), ("cancel", "null")])
def test_signal_acknowledgement_cannot_be_interpreted_as_completion(mcp, program, stage, seen):
    values = envelope(stage=stage, continuation=carry("cancel_signalled", generation=GEN), observation=seen)
    refused(mcp, program, record("CL1\n", values), 400)


def test_mechanism_error_rescans_without_replaying_an_effect(mcp, program):
    selected = decision(mcp, program, stage="error", observation="storage", continuation=carry("started"))
    assert selected[0] == "yield" and selected[5] == "250"
    assert fields(selected[4], "KC1\n", 8)[:2] == ["scan", OP]


@pytest.mark.parametrize("index,replacement", [(0, "bad-facts"), (1, "not-a-bundle"), (2, "bad-binding"),
    (3, "not-a-clock"), (7, "[]"), (8, '["interpret","other"]'), (9, "[]"),
    (10, "a.state"), (11, "executor.claim")])
def test_invalid_boot_bindings_fail_closed(mcp, program, index, replacement):
    values = envelope(stage="boot")
    values[index] = replacement
    refused(mcp, program, record("CL1\n", values), 400)


@pytest.mark.parametrize("step,seen", [("delivery", snapshot("", 2)), ("delivery", snapshot("retained", 0)),
    ("claim", snapshot("claim", 2)), ("state", snapshot("", 0))])
def test_inconsistent_retained_revisions_cannot_select_effects(mcp, program, step, seen):
    values = envelope(continuation=carry(step), observation=seen)
    refused(mcp, program, record("CL1\n", values), 409)
