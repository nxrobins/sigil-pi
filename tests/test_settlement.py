"""Terminal result/reservation consistency, not a billing or automatic-service claim."""
import json

import pytest

from api_support import credential
from conftest import SIGIL_ROOT, needs_toolchain
from scripts.compose_application import compose_application
from settlement_support import incoming, plan, snapshot
from test_admission import plan as admit
from turn_support import Decision, fields, record, refused, run, text_reply


@pytest.fixture(scope="module")
def program():
    needs_toolchain()
    return compose_application("settlement", SIGIL_ROOT).text


@pytest.fixture
def terminal_records(mcp):
    row = credential()
    _, raw, _ = admit(mcp, compose_application("admission", SIGIL_ROOT).text, row=row)
    writes = json.loads(raw)["writes"]
    state = writes[2]["value"]
    intent = fields(writes[3]["value"], "SI1\n", 5)
    first = Decision(state, intent[2], intent[1], intent[3], intent[4])
    final = run(mcp, compose_application("turn", SIGIL_ROOT).text,
                first.result(text_reply("Retained answer 😀", usage={"input_tokens": 4, "output_tokens": 3}), now=160))
    return row, [snapshot(writes[1]["value"]), snapshot(final.state, 2),
                 snapshot(writes[5]["value"]), snapshot(writes[4]["value"])], writes


def change(records, index, marker, count, field, value):
    modified = records.copy()
    sr = fields(modified[index], "SR1\n", 3)
    item = fields(sr[2], marker, count)
    item[field] = value
    sr[2] = record(marker, item)
    modified[index] = record("SR1\n", sr)
    return modified


def test_terminal_result_and_reservation_settle_in_one_exact_transaction(mcp, program, terminal_records):
    row, records, _ = terminal_records
    chosen = plan(mcp, program, row, records)
    assert chosen.phase == "done" and chosen.accounting == "reported"
    batch = chosen.request
    assert batch["checks"] == [{"namespace": "a.state", "key": "same-session", "revision": 2}]
    assert [(w["namespace"], w["key"], w["revision"]) for w in batch["writes"]] == [
        ("a.operations", "a" * 64, 1), ("a.reservation", "a" * 64, 1), ("a.budget", "active", 1)]
    op = fields(batch["writes"][0]["value"], "OQ3\n", 13)
    assert op[9] == "done" and op[12] == "2"
    assert fields(op[11], "OR1\n", 8) == ["Retained answer 😀", "4", "3", "1", "", "1", "160", "reported"]
    reserve = fields(batch["writes"][1]["value"], "BR2\n", 13)
    assert reserve[7] == "reported" and reserve[9:] == ["4", "3", "1", "160"]
    assert fields(batch["writes"][2]["value"], "BH1\n", 5)[2:] == ["0", "0", "0"]


@pytest.mark.parametrize("kind", ["unknown", "overrun"])
def test_unresolved_usage_releases_only_the_active_slot_not_its_token_hold(mcp, program, terminal_records, kind):
    row, records, _ = terminal_records
    records = change(records, 1, "PT1\n", 17, 12 if kind == "unknown" else 10, "0" if kind == "unknown" else "20001")
    chosen = plan(mcp, program, row, records)
    assert chosen.accounting == kind
    assert fields(chosen.request["writes"][2]["value"], "BH1\n", 5)[2:] == ["0", "20000", "4096"]


@pytest.mark.parametrize("index,marker,count,field,value", [
    (0, "OQ2\n", 11, 4, "c" * 64), (0, "OQ2\n", 11, 10, "c" * 64),
    (1, "PT1\n", 17, 1, "model"), (1, "PT1\n", 17, 0, "c" * 64),
    (1, "PT1\n", 17, 15, "other"), (1, "PT1\n", 17, 16, "other"),
    (2, "BR1\n", 9, 1, "other"), (2, "BR1\n", 9, 4, "1"),
    (3, "BH1\n", 5, 2, "0"), (3, "BH1\n", 5, 3, "19999"),
])
def test_mismatched_or_active_state_cannot_publish_a_terminal_result(
        mcp, program, terminal_records, index, marker, count, field, value):
    row, records, _ = terminal_records
    refused(mcp, program, incoming(row, change(records, index, marker, count, field, value)), 409)
