"""Atomic and adversarial terminal-operation contract, independent of effect dispatch."""
import json

import pytest

from api_support import credential, envelope
from conftest import SIGIL_ROOT
from scripts.compose_application import compose_application
from settlement_support import incoming, plan, snapshot
from store_support import NativeStore, mutation
from test_admission import incoming as admission_input, plan as admit
from test_native_api import decision
from test_settlement import change, program as program, terminal_records as terminal_records
from turn_support import fields, record, refused


@pytest.mark.parametrize("phase,known,accounting", [("done", "1", "reported"),
    ("failed", "0", "unknown"), ("cancelled", "1", "reported"), ("uncertain", "0", "unknown")])
def test_each_terminal_kind_has_a_truthful_public_result(mcp, program, terminal_records, phase, known, accounting):
    row, records, _ = terminal_records
    records = change(records, 1, "PT1\n", 17, 1, phase)
    records = change(records, 1, "PT1\n", 17, 12, known)
    error = "" if phase == "done" else "possibly_delivered" if phase == "uncertain" else "stopped"
    records = change(records, 1, "PT1\n", 17, 14, error)
    chosen = plan(mcp, program, row, records)
    assert chosen.phase == phase and chosen.accounting == accounting
    op = chosen.request["writes"][0]["value"]
    answer = decision(mcp, compose_application("api", SIGIL_ROOT).text,
        envelope(method="GET", path="/v1/operations/" + "a" * 64, stage="read", continuation="poll",
                 observation=snapshot(op, 2)))
    assert answer[:2] == ["reply", "200"]
    body = json.loads(answer[2])
    assert body["status"] == phase and body["accounting"] == accounting and body["error"] == error
    assert body["usage"]["known"] is (known == "1")


@pytest.mark.parametrize("coordinate", [0, 1, 2, 3])
def test_every_stale_precondition_rolls_back_the_entire_settlement(
        mcp, program, terminal_records, native_store_binary, tmp_path, coordinate):
    row, records, initial = terminal_records
    chosen = plan(mcp, program, row, records)
    points = [("a.operations", "a" * 64), ("a.reservation", "a" * 64),
              ("a.budget", "active"), ("a.state", "same-session")]
    with NativeStore(native_store_binary, tmp_path / "records", row["grants"], initialize=True) as store:
        store.commit(initial)
        store.commit([mutation("a.state", "same-session", fields(records[1], "SR1\n", 3)[2], 1)])
        ns, key = points[coordinate]
        retained = store.get(ns, key)
        store.commit([mutation(ns, key, retained["value"], retained["revision"])])
        before = [store.get(ns, key) for ns, key in points]
        result = store.raw(chosen.raw)
        assert result["code"] == "conflict"
        assert [store.get(ns, key) for ns, key in points] == before


def settled_records(chosen, records):
    writes = chosen.request["writes"]
    return [snapshot(writes[0]["value"], 2), records[1], snapshot(writes[1]["value"], 2), snapshot(writes[2]["value"], 2)]


def test_repeated_settlement_uses_the_retained_pair_not_a_newer_conversation(mcp, program, terminal_records):
    row, records, _ = terminal_records
    chosen = plan(mcp, program, row, records)
    updated = settled_records(chosen, records)
    updated[1] = snapshot("", 0)  # Its earlier terminal snapshot is no longer required.
    updated[3] = snapshot("", 0)
    repeated = plan(mcp, program, row, updated, now=200)
    assert repeated.phase == "done" and repeated.accounting == "reported" and repeated.raw == ""


@pytest.mark.parametrize("field,value", [(1, "other-tenant"), (4, "c" * 64),
    (9, "cancelled"), (11, "malformed-outcome"), (12, "1")])
def test_followup_cannot_replace_a_conversation_with_a_mismatched_terminal_operation(
        mcp, program, terminal_records, field, value):
    row, records, _ = terminal_records
    completed = plan(mcp, program, row, records).request["writes"]
    op = fields(completed[0]["value"], "OQ3\n", 13)
    op[field] = value
    request = admission_input(row=row, prior=fields(records[1], "SR1\n", 3)[2], state_revision=2,
        held=completed[2]["value"], budget_revision=2, previous=record("OQ3\n", op), previous_revision=2,
        operation="c" * 64, body={"session": "same-session", "message": "Next", "submission_key": "next"})
    refused(mcp, compose_application("admission", SIGIL_ROOT).text, request, 409)


@pytest.mark.parametrize("revision", [0, 1, 3])
def test_followup_requires_the_exact_committed_terminal_operation_revision(mcp, program, terminal_records, revision):
    row, records, _ = terminal_records
    completed = plan(mcp, program, row, records).request["writes"]
    assert admit(mcp, compose_application("admission", SIGIL_ROOT).text, row=row,
        prior=fields(records[1], "SR1\n", 3)[2], state_revision=2,
        held=completed[2]["value"], budget_revision=2,
        previous="" if revision == 0 else completed[0]["value"], previous_revision=revision,
        operation="c" * 64, body={"session": "same-session", "message": "Next", "submission_key": "next"}) == ["busy", "", ""]


@pytest.mark.parametrize("field,value", [(0, "b" * 64), (1, "other-tenant"), (2, "bob"),
    (3, "changed-profile"), (4, "1"), (6, "271"), (7, "unknown"), (8, "c" * 64),
    (9, "5"), (10, "4"), (11, "0"), (12, "161")])
def test_terminal_operation_and_reservation_must_remain_a_matching_pair(
        mcp, program, terminal_records, field, value):
    row, records, _ = terminal_records
    updated = settled_records(plan(mcp, program, row, records), records)
    updated = change(updated, 2, "BR2\n", 13, field, value)
    refused(mcp, program, incoming(row, updated), 409)


@pytest.mark.parametrize("index,marker,count,field,value", [
    (0, "OQ2\n", 11, 0, "bob"), (0, "OQ2\n", 11, 1, "other-tenant"),
    (0, "OQ2\n", 11, 5, "new-epoch"), (0, "OQ2\n", 11, 6, "99"),
    (0, "OQ2\n", 11, 7, "271"), (0, "OQ2\n", 11, 9, "done"),
    (2, "BR1\n", 9, 2, "bob"), (2, "BR1\n", 9, 5, "4095"),
    (2, "BR1\n", 9, 6, "269"), (2, "BR1\n", 9, 7, "reported"),
    (3, "BH1\n", 5, 0, "other-tenant"), (3, "BH1\n", 5, 4, "4095"),
    (3, "BH1\n", 5, 2, "9"), (3, "BH1\n", 5, 3, "160001"),
    (3, "BH1\n", 5, 4, "32769"),
])
def test_forged_admission_and_ledger_bindings_emit_no_terminal_transaction(
        mcp, program, terminal_records, index, marker, count, field, value):
    row, records, _ = terminal_records
    refused(mcp, program, incoming(row, change(records, index, marker, count, field, value)), 409)


@pytest.mark.parametrize("index", [0, 1, 2, 3])
def test_missing_or_tombstoned_snapshot_cannot_settle(mcp, program, terminal_records, index):
    row, records, _ = terminal_records
    for revision in (0, 1):
        changed = records.copy()
        changed[index] = snapshot("", revision)
        refused(mcp, program, incoming(row, changed), 409)


@pytest.mark.parametrize("index,value", [(0, ""), (1, "b" * 63), (2, "c" * 64), (3, "other-session"),
    (4, "149"), (4, "0160"), (4, "9007199254740992")])
def test_fixed_context_lookups_and_clock_cannot_be_substituted(mcp, program, terminal_records, index, value):
    row, records, _ = terminal_records
    outer = fields(incoming(row, records), "SF1\n", 9)
    outer[index] = value
    refused(mcp, program, record("SF1\n", outer), 400 if index in {0, 1} or value in {"0160", "9007199254740992"} else 409)


def test_recording_after_expiry_is_allowed_but_a_changed_policy_is_not(mcp, program, terminal_records):
    row, records, _ = terminal_records
    rotated = {**row, "facts": credential(before=300, expires=400)["facts"]}
    assert plan(mcp, program, rotated, records, now=500).phase == "done"
    changed = {**row, "facts": credential(epoch="other")["facts"]}
    refused(mcp, program, incoming(changed, records), 409)


@pytest.mark.parametrize("field,value", [(1, "-1"), (2, "03"), (3, "true"), (5, "0"),
    (6, "9007199254740992"), (7, "settled"), (3, "0")])
def test_api_refuses_malformed_terminal_outcomes(mcp, program, terminal_records, field, value):
    row, records, _ = terminal_records
    op = fields(plan(mcp, program, row, records).request["writes"][0]["value"], "OQ3\n", 13)
    outcome = fields(op[11], "OR1\n", 8)
    outcome[field] = value
    op[11] = record("OR1\n", outcome)
    query = envelope(method="GET", path="/v1/operations/" + "a" * 64, stage="read", continuation="poll",
                     observation=snapshot(record("OQ3\n", op), 2))
    refused(mcp, compose_application("api", SIGIL_ROOT).text, query, 400)
    # A different principal gets no content or malformed-state oracle.
    peer = envelope(method="GET", path="/v1/operations/" + "a" * 64, row=credential(principal="bob"),
                    stage="read", continuation="poll", observation=snapshot(record("OQ3\n", op), 2))
    assert decision(mcp, compose_application("api", SIGIL_ROOT).text, peer)[:2] == ["reply", "404"]
