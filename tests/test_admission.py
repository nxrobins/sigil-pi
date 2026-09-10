"""SIGIL's six-record admission proposal, independently checked against real storage."""
import json

import pytest

from api_support import BODY, binding, credential
from conftest import SIGIL_ROOT, forge_ok, needs_toolchain
from scripts.compose_application import compose_application
from settlement_support import plan as settle, snapshot as retained_snapshot
from store_support import NativeStore, mutation
from turn_support import Decision, FUEL, configuration, event, fields, record, refused, run, text_reply


@pytest.fixture(scope="module")
def program():
    needs_toolchain()
    return compose_application("admission", SIGIL_ROOT).text


def profile(*, turns=8, inputs=160000, outputs=32768, per_turn=20000, config=None):
    return record("BP1\n", [configuration() if config is None else config, str(turns), str(inputs), str(outputs), str(per_turn)])


def incoming(*, row=None, prior="", state_revision=0, held="", budget_revision=0,
             created=150, now=150, deadline=270, operation="a" * 64, body=None,
             previous="", previous_revision=0):
    row = credential() if row is None else row
    body = BODY if body is None else body
    values = fields(row["facts"], "CF2\n", 15)
    canonical = record("PS1\n", [body["session"], body["message"], body["submission_key"]])
    return record("AP2\n", [row["facts"], canonical, operation, str(created), str(deadline),
        record("SR1\n", ["ok", str(state_revision), prior]),
        record("SR1\n", ["ok", str(budget_revision), held]),
        values[0].encode().hex() + ":" + body["submission_key"], "b" * 64, str(now),
        record("SR1\n", ["ok", str(previous_revision), previous])])


def plan(mcp, program, **kwargs):
    return fields(forge_ok(mcp, program, incoming(**kwargs), fuel=FUEL), "AD1\n", 3)


def test_admission_uses_the_exact_existing_turn_start_and_binds_six_records(mcp, program):
    outcome, raw, guard = plan(mcp, program)
    assert outcome == "ok" and fields(guard, "TG1\n", 2) == ["100", "270"]
    batch = json.loads(raw)
    assert batch["op"] == "commit" and batch["checks"] == []
    writes = batch["writes"]
    assert [(w["namespace"], w["key"], w["revision"]) for w in writes] == [
        ("a.requests", "616c696365:same-key", 0), ("a.operations", "a" * 64, 0),
        ("a.state", "same-session", 0), ("a.intent", "a" * 64 + ":1", 0),
        ("a.budget", "active", 0), ("a.reservation", "a" * 64, 0)]
    request = record("PS1\n", [BODY["session"], BODY["message"], BODY["submission_key"]])
    expected = run(mcp, compose_application("turn", SIGIL_ROOT).text,
                   event(operation="a" * 64, payload=request, now=150, deadline=270))
    assert writes[2]["value"] == expected.state
    assert fields(writes[3]["value"], "SI1\n", 5) == ["a" * 64, "1", "model", "", expected.input]
    op = fields(writes[1]["value"], "OQ2\n", 11)
    dedup = fields(writes[0]["value"], "DQ2\n", 10)
    assert op[:9] == dedup[:9] and op[10] == dedup[9] == "b" * 64
    assert op[8] == credential()["facts"]
    assert fields(writes[4]["value"], "BH1\n", 5)[2:] == ["1", "20000", "4096"]
    assert fields(writes[5]["value"], "BR1\n", 9)[4:] == ["20000", "4096", "270", "reserved", "b" * 64]


@pytest.mark.parametrize("message", ["x" * 262144, "é" * 131072], ids=["ascii", "utf8"])
def test_maximum_message_admission_preserves_every_copy_with_existing_limits(mcp, program, message):
    assert len(message.encode()) == 262144
    body = {**BODY, "message": message}
    outcome, raw, guard = plan(mcp, program, body=body)
    assert outcome == "ok" and fields(guard, "TG1\n", 2) == ["100", "270"]
    batch = json.loads(raw)
    assert batch["op"] == "commit" and batch["checks"] == []
    assert len(raw.encode()) <= 2097152
    writes = batch["writes"]
    assert len(writes) == 6 and all(w["revision"] == 0 for w in writes)
    canonical = record("PS1\n", [body["session"], message, body["submission_key"]])
    assert fields(writes[0]["value"], "DQ2\n", 10)[3] == canonical
    assert fields(writes[1]["value"], "OQ2\n", 11)[3] == canonical
    state = fields(writes[2]["value"], "PT1\n", 17)
    assert json.loads(state[5]) == [{"role": "user", "content": message}]
    intent = fields(writes[3]["value"], "SI1\n", 5)
    assert intent[:4] == ["a" * 64, "1", "model", ""]
    assert json.loads(intent[4])["messages"] == json.loads(state[5])
    assert fields(writes[4]["value"], "BH1\n", 5)[2:] == ["1", "20000", "4096"]
    assert fields(writes[5]["value"], "BR1\n", 9)[4:] == ["20000", "4096", "270", "reserved", "b" * 64]


@pytest.fixture(scope="module")
def append_probe(program):
    # Pure helper conformance only: this synthetic prefix is not a valid batch
    # and is never passed to storage or used to establish authenticated admission.
    assert program.count("pub fn tool_main(") == 1
    return program.replace("pub fn tool_main(", "fn original_main(") + """
pub fn tool_main(input_ptr: i64, input_len: i64) -> i64 @Internal ! { Alloc } {
    return add_write(slice(input_ptr, input_len), text("a.requests"), text("k"), text("0"), text("v"));
}
"""


@pytest.mark.parametrize("extra", [-1, 0, 1], ids=["below", "exact", "above"])
def test_admission_append_keeps_the_inclusive_serialized_size_ceiling(mcp, append_probe, extra):
    suffix = ',{"namespace":"a.requests","key":"k","revision":0,"value":"v"}'
    prefix = "x" * (2097152 - len(suffix) + extra)
    if extra > 0:
        refused(mcp, append_probe, prefix, 413)
    else:
        assert forge_ok(mcp, append_probe, prefix, fuel=FUEL) == prefix + suffix


@pytest.mark.parametrize("tools,expected", [([], []), (["read_file"], ["read_file"]), (["not_in_catalog"], []), (["*"], ["read_file"])])
def test_intent_catalog_is_filtered_by_authenticated_tools(mcp, program, tools, expected):
    _, raw, _ = plan(mcp, program, row=credential(tools=tools))
    body = json.loads(fields(json.loads(raw)["writes"][3]["value"], "SI1\n", 5)[4])
    assert [tool["name"] for tool in body["tools"]] == expected


@pytest.mark.parametrize("policy", [profile(turns=0), profile(inputs=19999), profile(outputs=4095)])
def test_independent_reservation_limits_refuse_without_a_proposal(mcp, program, policy):
    assert plan(mcp, program, row=credential(profile=policy)) == ["quota", "", ""]


def test_exact_reservation_limit_is_allowed_but_outstanding_usage_is_not_released(mcp, program):
    row = credential(profile=profile(turns=1, inputs=20000, outputs=4096))
    first = plan(mcp, program, row=row)
    assert first[0] == "ok"
    held = json.loads(first[1])["writes"][4]["value"]
    assert plan(mcp, program, row=row, held=held, budget_revision=1) == ["quota", "", ""]


@pytest.mark.parametrize("config", ["", record("PC1\n", ["m", "0", "1", "", "[]", "1024", "1"]),
                                   configuration(tools=[{"name": "read_file"}])])
def test_bootstrap_validates_nested_model_profile_in_sigil(mcp, program, config):
    row = credential(profile=profile(config=config))
    refused(mcp, program, record("AV1\n", [json.dumps([binding(row)])]), 400)


@pytest.mark.parametrize("policy", [profile(turns=-1), profile(turns=1025), profile(inputs=-1),
                                    profile(outputs=1000000000001), profile(per_turn=0), profile(per_turn="01")])
def test_bootstrap_refuses_noncanonical_or_unbounded_reservation_profiles(mcp, program, policy):
    refused(mcp, program, record("AV1\n", [json.dumps([binding(credential(profile=policy))])]), 400)


def test_bootstrap_accepts_valid_disabled_and_active_profiles(mcp, program):
    rows = [credential(), credential(profile=profile(turns=0))]
    assert forge_ok(mcp, program, record("AV1\n", [json.dumps([binding(row) for row in rows])]), fuel=FUEL) == "profiles_validated"


@pytest.mark.parametrize("at", [270, 271, 9000000000])
def test_elapsed_operation_deadline_produces_no_transaction(mcp, program, at):
    assert plan(mcp, program, now=at) == ["expired", "", ""]


@pytest.mark.parametrize("changes,code", [({"now": 149}, 400), ({"created": 99}, 400),
    ({"deadline": 271}, 400), ({"prior": "nonempty", "state_revision": 0}, 400),
    ({"held": "nonempty", "budget_revision": 0}, 400), ({"held": "", "budget_revision": 1}, 400)])
def test_inconsistent_snapshots_or_time_fail_closed(mcp, program, changes, code):
    refused(mcp, program, incoming(**changes), code)


@pytest.mark.parametrize("kind", ["tenant", "policy"])
def test_budget_binding_cannot_be_reset_by_a_different_identity_or_profile(mcp, program, kind):
    policy = fields(credential()["facts"], "CF2\n", 15)[14]
    held = record("BH1\n", ["other-tenant" if kind == "tenant" else "tenant-a",
                           profile(turns=1) if kind == "policy" else policy, "0", "0", "0"])
    refused(mcp, program, incoming(held=held, budget_revision=1), 409)


def test_active_or_deleted_conversation_cannot_be_overwritten(mcp, program):
    _, raw, _ = plan(mcp, program)
    prior = json.loads(raw)["writes"][2]["value"]
    assert plan(mcp, program, prior=prior, state_revision=1, operation="c" * 64) == ["busy", "", ""]
    assert plan(mcp, program, prior="", state_revision=1) == ["busy", "", ""]


@pytest.mark.parametrize("index,value,code", [(2, "operation-name", 400), (8, "b" * 63, 400),
                                            (8, "B" * 64, 400), (7, "616c696365:changed", 409)])
def test_operation_bundle_and_submission_key_bindings_are_exact(mcp, program, index, value, code):
    values = fields(incoming(), "AP2\n", 11)
    values[index] = value
    refused(mcp, program, record("AP2\n", values), code)


def test_completed_conversation_reuses_history_with_a_revision_precondition(mcp, program):
    turn_program = compose_application("turn", SIGIL_ROOT).text
    row = credential()
    _, initial, _ = plan(mcp, program, row=row)
    writes = json.loads(initial)["writes"]
    intent = fields(writes[3]["value"], "SI1\n", 5)
    started = Decision(writes[2]["value"], intent[2], intent[1], intent[3], intent[4])
    completed = run(mcp, turn_program, started.result(text_reply("retained reply", usage={"input_tokens": 4, "output_tokens": 3}), now=160))
    held = writes[4]["value"]
    assert plan(mcp, program, prior=completed.state, state_revision=9, held=held, budget_revision=1) == ["busy", "", ""]
    settlement = settle(mcp, compose_application("settlement", SIGIL_ROOT).text, row,
        [retained_snapshot(writes[1]["value"]), retained_snapshot(completed.state, 9),
         retained_snapshot(writes[5]["value"]), retained_snapshot(held)])
    previous = settlement.request["writes"][0]["value"]
    held = settlement.request["writes"][2]["value"]
    refused(mcp, program, incoming(prior=completed.state, state_revision=9, previous=previous, previous_revision=2), 409)
    outcome, raw, _ = plan(mcp, program, prior=completed.state, state_revision=9, held=held, budget_revision=1,
                          body={**BODY, "message": "Follow up", "submission_key": "next"},
                          operation="c" * 64, previous=previous, previous_revision=2)
    assert outcome == "ok"
    w = json.loads(raw)["writes"][2]
    assert w["revision"] == 9
    assert "retained reply" in fields(w["value"], "PT1\n", 17)[5]
    assert json.loads(raw)["checks"] == [{"namespace": "a.operations", "key": "a" * 64, "revision": 2}]


def test_previous_admission_envelope_cannot_omit_the_settlement_precondition(mcp, program):
    legacy = record("AP1\n", fields(incoming(), "AP2\n", 11)[:10])
    refused(mcp, program, legacy, 400)


@pytest.mark.parametrize("maximum", [False, True], ids=["normal", "maximum"])
@pytest.mark.parametrize("coordinate", [0, 1, 2, 3, 4, 5])
def test_every_stale_coordinate_prevents_partial_native_admission(mcp, program, native_store_binary, tmp_path, coordinate, maximum):
    _, raw, _ = plan(mcp, program, body={**BODY, "message": "x" * 262144} if maximum else BODY)
    writes = json.loads(raw)["writes"]
    with NativeStore(native_store_binary, tmp_path / "records", credential()["grants"], initialize=True) as store:
        seed = writes[coordinate]
        store.commit([mutation(seed["namespace"], seed["key"], "preexisting")])
        rejected = store.raw(raw)
        assert rejected["status"] == "error" and rejected["code"] == "conflict", rejected
        for i, w in enumerate(writes):
            retained = store.get(w["namespace"], w["key"])
            assert retained["revision"] == (1 if i == coordinate else 0)
            assert retained["value"] == ("preexisting" if i == coordinate else None)


@pytest.mark.parametrize("maximum", [False, True], ids=["normal", "maximum"])
def test_denied_final_namespace_cannot_leave_earlier_writes_published(mcp, program, native_store_binary, tmp_path, maximum):
    _, raw, _ = plan(mcp, program, body={**BODY, "message": "x" * 262144} if maximum else BODY)
    grants = {**credential()["grants"], "a.reservation": "read"}
    with NativeStore(native_store_binary, tmp_path / "records", grants, initialize=True) as store:
        rejected = store.raw(raw)
        assert rejected["status"] == "error" and rejected["code"] == "denied", rejected
        for w in json.loads(raw)["writes"]:
            assert store.get(w["namespace"], w["key"])["revision"] == 0


@pytest.mark.parametrize("maximum", [False, True], ids=["normal", "maximum"])
def test_six_record_commit_survives_store_process_restart(mcp, program, native_store_binary, tmp_path, maximum):
    _, raw, _ = plan(mcp, program, body={**BODY, "message": "x" * 262144} if maximum else BODY)
    writes = json.loads(raw)["writes"]
    root = tmp_path / "records"
    with NativeStore(native_store_binary, root, credential()["grants"], initialize=True) as store:
        committed = store.raw(raw)
        assert committed["status"] == "ok", committed
        revision = committed["receipt"]["revision"]
        store.kill()
    with NativeStore(native_store_binary, root, credential()["grants"]) as reopened:
        for w in writes:
            assert reopened.get(w["namespace"], w["key"]) == {"revision": revision, "value": w["value"]}
