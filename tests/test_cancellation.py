"""Pure SIGIL cancellation admission, dispatch-time refusal and terminal policy."""
import json

import pytest

from api_support import credential, envelope
from conftest import SIGIL_ROOT, forge_ok, needs_toolchain
from dispatch_support import initial
from scripts.compose_application import compose_application
from settlement_support import snapshot
from test_dispatch import tool_view
from test_preclaim import incoming as preclaim_input
from turn_support import FUEL, fields, record, refused

OP = "a" * 64
PATH = "/v1/operations/" + OP + "/cancel"


@pytest.fixture(scope="module")
def programs():
    needs_toolchain()
    return {name: compose_application(name, SIGIL_ROOT).text for name in
            ["api", "dispatch", "dispatch_cancellable", "preclaim_cancellable"]}


@pytest.fixture(scope="module")
def model(mcp):
    return initial(mcp)


def cancellation(*, operation=OP, principal="alice", tenant="tenant-a", bundle="b" * 64, at=151):
    return record("CR1\n", [operation, principal, tenant, bundle, str(at)])


def api_input(**kwargs):
    return envelope(**{"method": "POST", "path": PATH, "body": "{}", "now": 151, **kwargs})


def decision(mcp, programs, **kwargs):
    return fields(forge_ok(mcp, programs["api"], api_input(**kwargs), fuel=FUEL), "HC3\n", 5)


def read_cancel(mcp, programs, model, **kwargs):
    return decision(mcp, programs, stage="read", observation=model[4], continuation="cancel_op", **kwargs)


@pytest.mark.parametrize("body", ["", "{}", " \n{ \t }\r"])
def test_cancel_accepts_only_empty_body_or_empty_object(mcp, programs, body):
    assert decision(mcp, programs, body=body)[:4] == ["read", "a.operations", OP, "cancel_op"]


@pytest.mark.parametrize("body", ['{"phase":"cancelled"}', '{"receipt":1}', '[]', 'null', '{', '{}{}', '"{}"', ' ' * 1025])
def test_cancel_payload_cannot_supply_outcome_or_control_fields(mcp, programs, body):
    assert decision(mcp, programs, body=body)[:2] == ["reply", "400"]


def test_cancel_request_is_a_checked_create_only_record_not_a_state_or_effect_result(mcp, programs, model):
    step = read_cancel(mcp, programs, model)
    assert step[:3] == ["read", "a.operations", OP + ":cancel"]
    command = decision(mcp, programs, stage="read", observation=snapshot("", 0), continuation=step[3])
    batch = json.loads(command[1])
    assert command[0] == "commit" and command[3] == "cancel_commit"
    assert batch["checks"] == [{"namespace": "a.operations", "key": OP, "revision": 1}]
    assert batch["writes"] == [{"namespace": "a.operations", "key": OP + ":cancel", "revision": 0,
                                "value": cancellation()}]
    ack = decision(mcp, programs, stage="commit", observation=record("SC1\n", ["ok", "2"]), continuation=command[3])
    body = json.loads(ack[2])
    assert ack[:2] == ["reply", "202"] and body["status"] == "accepted"
    assert body["cancellation_requested"] and body["cancellation_status"] == "requested" and not body["replayed"]


@pytest.mark.parametrize("kwargs", [{"principal": "bob"}, {"tenant": "other"}])
def test_cancel_requires_accepting_principal_and_tenant(mcp, programs, model, kwargs):
    assert read_cancel(mcp, programs, model, row=credential(**kwargs))[:2] == ["reply", "404"]


def test_cancel_scope_is_checked_before_any_operation_read(mcp, programs):
    assert decision(mcp, programs, row=credential(scopes=["sessions:read"]))[:2] == ["reply", "403"]


def test_terminal_cancel_does_not_grant_result_read_permission(mcp, programs, model):
    op = fields(fields(model[4], "SR1\n", 3)[2], "OQ2\n", 11)
    op[9] = "done"
    op += [record("OR1\n", ["private-result-canary", "7", "2", "1", "", "1", "160", "reported"]), "2"]
    answer = decision(mcp, programs, row=credential(scopes=["chat"]), now=170,
        stage="read", observation=snapshot(record("OQ3\n", op), 2), continuation="cancel_op")
    assert answer[:2] == ["reply", "200"]
    assert "private-result-canary" not in answer[2]
    body = json.loads(answer[2])
    assert body["operation"] == OP and body["status"] == "done" and body["cancellation_status"] == "terminal"
    assert not {"reply", "usage", "error"} & body.keys()


def test_repeated_request_is_an_acknowledged_noop(mcp, programs, model):
    step = read_cancel(mcp, programs, model)
    ack = decision(mcp, programs, stage="read", observation=snapshot(cancellation()), continuation=step[3])
    assert ack[:2] == ["reply", "202"] and json.loads(ack[2])["replayed"]


@pytest.mark.parametrize("snapshot_value", [snapshot("", 2), snapshot("forged", 0), snapshot(cancellation(), 2),
    snapshot(cancellation(principal="bob")), snapshot(cancellation(operation="f" * 64)),
    snapshot(cancellation(bundle="f" * 64)), snapshot(cancellation(at=149)), snapshot(cancellation(at=152))])
def test_conflicting_retained_request_never_gets_overwritten(mcp, programs, model, snapshot_value):
    step = read_cancel(mcp, programs, model)
    assert decision(mcp, programs, stage="read", observation=snapshot_value, continuation=step[3])[:2] == ["reply", "409"]


def test_failed_commit_cannot_acknowledge_cancellation(mcp, programs):
    assert decision(mcp, programs, stage="commit", observation=record("SC1\n", ["error", "0"]),
                    continuation="cancel_commit")[:2] == ["reply", "503"]


def test_actual_absent_cancellation_preserves_the_exact_existing_dispatch(mcp, programs, model):
    original = forge_ok(mcp, programs["dispatch"], record("DF1\n", model), fuel=FUEL)
    current = record("DF2\n", model + [OP + ":cancel", snapshot("", 0)])
    assert forge_ok(mcp, programs["dispatch_cancellable"], current, fuel=FUEL) == original


@pytest.mark.parametrize("seen,code", [(snapshot(cancellation()), 499), (snapshot("", 2), 409),
    (snapshot(cancellation(operation="f" * 64)), 409), (snapshot("forged", 0), 409)])
def test_dispatch_reads_current_cancellation_and_cannot_start_from_an_earlier_eligible_decision(mcp, programs, model, seen, code):
    refused(mcp, programs["dispatch_cancellable"], record("DF2\n", model + [OP + ":cancel", seen]), code)


@pytest.mark.parametrize("tool", [False, True])
@pytest.mark.parametrize("usage", [None, {"input_tokens": 7, "output_tokens": 3}])
@pytest.mark.parametrize("now", [151, 270])
def test_cancelled_unclaimed_action_finalizes_atomically_and_preserves_previous_usage(mcp, programs, model, tool, usage, now):
    values = tool_view(mcp, model, usage=usage) if tool else model
    incoming = preclaim_input(values, now=now) + [OP + ":cancel", snapshot(cancellation())]
    phase, reason, raw = fields(forge_ok(mcp, programs["preclaim_cancellable"], record("UF2\n", incoming), fuel=FUEL), "TX1\n", 3)
    assert (phase, reason) == ("cancelled", "cancelled")
    batch = json.loads(raw)
    assert batch["checks"][-1] == {"namespace": "a.operations", "key": OP + ":cancel", "revision": 1}
    assert len(batch["checks"]) == len(batch["writes"]) == 4
    terminal = fields(batch["writes"][3]["value"], "PT1\n", 17)
    before = fields(fields(values[6], "SR1\n", 3)[2], "PT1\n", 17)
    assert terminal[1] == terminal[14] == "cancelled" and terminal[10:13] == before[10:13]
    assert fields(batch["writes"][0]["value"], "OQ3\n", 13)[9] == "cancelled"


def test_cancel_request_cannot_reclassify_a_claimed_action_as_unsent(mcp, programs, model):
    incoming = preclaim_input(model) + [OP + ":cancel", snapshot(cancellation())]
    incoming[13] = snapshot("actual-claim", 1)
    refused(mcp, programs["preclaim_cancellable"], record("UF2\n", incoming), 409)


@pytest.mark.parametrize("name,marker,values", [("dispatch_cancellable", "DF1\n", 11), ("preclaim_cancellable", "UF1\n", 15)])
def test_cancellation_aware_components_refuse_old_input_contracts(mcp, programs, model, name, marker, values):
    incoming = model if values == 11 else preclaim_input(model)
    refused(mcp, programs[name], record(marker, incoming), 400)
