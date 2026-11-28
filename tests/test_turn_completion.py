"""SIGIL derives events from actual delivery snapshots; no fixture-supplied PE1."""
import json

import pytest

from conftest import SIGIL_ROOT, forge_ok, needs_toolchain
from scripts.compose_application import compose_application
from settlement_support import snapshot
from transaction_support import observation
from turn_support import FUEL, event, fields, record, refused, run, source, text_reply, tool_reply


@pytest.fixture(scope="module")
def program():
    needs_toolchain()
    return compose_application("turn_completion", SIGIL_ROOT).text


@pytest.fixture(scope="module")
def first(mcp):
    return run(mcp, source(), event())


def incoming(first, *, payload=None, kind="ok", now=101):
    result = first.result(text_reply("Observed answer") if payload is None else payload, kind=kind)
    delivered = observation(result)
    intent = record("SI1\n", ["operation-1", first.sequence, first.action, first.tool, first.input])
    return record("TF1\n", ["pi-a.state", "pi-a.intent", "pi-a.delivery", "conversation", "operation-1",
        "operation-1:" + first.sequence, f"operation-1:{int(first.sequence) + 1}", str(now),
        snapshot(first.state), snapshot(intent), snapshot(delivered), snapshot("", 0)])


def plan(mcp, program, value):
    phase, sequence, raw = fields(forge_ok(mcp, program, value, fuel=FUEL), "TX1\n", 3)
    return phase, sequence, json.loads(raw)


def test_delivery_alone_selects_event_and_exact_next_intent(mcp, program, first):
    phase, sequence, batch = plan(mcp, program, incoming(first, payload=tool_reply("read_file")))
    assert (phase, sequence) == ("tool", "2")
    assert batch["checks"] == [{"namespace": "pi-a.intent", "key": "operation-1:1", "revision": 1},
                               {"namespace": "pi-a.delivery", "key": "operation-1:1", "revision": 1}]
    assert [w["namespace"] for w in batch["writes"]] == ["pi-a.state", "pi-a.intent"]
    assert fields(batch["writes"][1]["value"], "SI1\n", 5)[:4] == ["operation-1", "2", "tool", "read_file"]
    assert batch["writes"][1]["revision"] == 0


@pytest.mark.parametrize("kind,expected", [("ok", "done"), ("unknown", "uncertain"),
    ("cancelled_unsent", "cancelled"), ("expired_unsent", "failed"), ("error", "failed")])
def test_terminal_outcome_covers_all_actual_reads_without_creating_a_next_intent(mcp, program, first, kind, expected):
    phase, sequence, batch = plan(mcp, program, incoming(first, kind=kind, payload=text_reply() if kind == "ok" else ""))
    assert (phase, sequence) == (expected, "1")
    assert len(batch["writes"]) == 1 and len(batch["checks"]) == 3
    assert batch["checks"][-1] == {"namespace": "pi-a.intent", "key": "operation-1:2", "revision": 0}
    assert fields(batch["writes"][0]["value"], "PT1\n", 17)[1] == expected


@pytest.mark.parametrize("index,value", [(3, "other-session"), (4, "other-operation"),
    (5, "operation-1:2"), (6, "operation-1:3"), (6, "operation-1:1")])
def test_lookup_correlations_cannot_be_substituted(mcp, program, first, index, value):
    values = fields(incoming(first), "TF1\n", 12)
    values[index] = value
    refused(mcp, program, record("TF1\n", values), 409)


@pytest.mark.parametrize("index,revision", [(8, 0), (9, 0), (9, 2), (10, 0), (10, 2), (11, 1)])
def test_missing_or_changed_snapshots_and_retained_next_slots_are_refused(mcp, program, first, index, revision):
    values = fields(incoming(first), "TF1\n", 12)
    old = fields(values[index], "SR1\n", 3)
    values[index] = snapshot(old[2], revision)
    refused(mcp, program, record("TF1\n", values), 409)


@pytest.mark.parametrize("left,right", [(0, 1), (0, 2), (1, 2)])
def test_completion_namespace_aliases_fail_closed(mcp, program, first, left, right):
    values = fields(incoming(first), "TF1\n", 12)
    values[left] = values[right]
    refused(mcp, program, record("TF1\n", values), 400)


@pytest.mark.parametrize("suffix", ["extra", "00000008override"])
def test_no_caller_event_or_result_field_can_be_appended(mcp, program, first, suffix):
    refused(mcp, program, incoming(first) + suffix, 400)
