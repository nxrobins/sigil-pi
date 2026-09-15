"""SIGIL classifies actual-result shapes and reuses shared executor transactions."""
import pytest

from completion_support import completion, facts, snapshot
from conftest import SIGIL_ROOT, needs_toolchain
from scripts.compose_application import compose_application
from turn_support import fields, refused


@pytest.fixture(scope="module")
def program():
    needs_toolchain()
    return compose_application("worker_completion", SIGIL_ROOT).text


def test_prepared_fact_builds_the_same_exact_bound_claim(mcp, program):
    seen = facts(kind="prepared", sent="0", status="", output_kind="missing", payload="")
    chosen = completion(mcp, program, seen, revision="0")
    assert chosen.phase == "1" and chosen.continuation == "dispatch_after_commit"
    assert len(chosen.request["writes"]) == 1
    assert fields(chosen.request["writes"][0]["value"], "SD1\n", 5) == ["pi-a.intent", "operation-1:1", "1", "worker-1", "1"]


@pytest.mark.parametrize("payload", ["", "returned", "a|b\n\"quoted\" 😀", "\0control\b\f\r\t"])
def test_only_observed_successful_output_becomes_returned_delivery(mcp, program, payload):
    chosen = completion(mcp, program, facts(payload=payload))
    assert chosen.phase == "2" and chosen.continuation == "record_after_commit"
    assert fields(chosen.request["writes"][1]["value"], "DR1\n", 7)[4:] == ["2", "returned", payload]


@pytest.mark.parametrize("kind,fault", [("refused", "time_guard"), ("refused", "clock"),
    ("observed", "cancelled"), ("observed", "deadline"), ("observed", "spawn"), ("observed", "protocol")])
def test_confirmed_no_forge_initiation_is_recorded_as_definitely_unsent(mcp, program, kind, fault):
    chosen = completion(mcp, program, facts(kind=kind, sent="0", fault=fault, status="", output_kind="missing", payload=""))
    assert fields(chosen.request["writes"][1]["value"], "DR1\n", 7)[4:] == ["3", "", ""]


@pytest.mark.parametrize("kwargs", [
    {"fault": "deadline", "status": "", "output_kind": "missing", "payload": ""},
    {"fault": "protocol", "status": "", "output_kind": "missing", "payload": ""},
    {"fault": "cleanup_unconfirmed", "reaped": "0", "status": "", "output_kind": "missing", "payload": ""},
    {"sent": "0", "fault": "cleanup_unconfirmed", "reaped": "0", "status": "", "output_kind": "missing", "payload": ""},
    {"status": "error", "output_kind": "missing", "payload": ""},
    {"status": "error", "payload": "not a successful response"},
    {"output_kind": "missing", "payload": ""},
    {"output_kind": "other", "payload": ""},
    {"output_kind": "oversized", "payload": "", "length": 1048577},
])
def test_errors_incomplete_output_and_uncertain_cleanup_never_fabricate_success_or_retry(mcp, program, kwargs):
    chosen = completion(mcp, program, facts(**kwargs))
    assert fields(chosen.request["writes"][1]["value"], "DR1\n", 7)[4:] == ["4", "", ""]
    assert chosen.continuation == "record_after_commit"


@pytest.mark.parametrize("kwargs,code", [({"generation": "wrong-worker"}, 409), ({"sent": "true"}, 400),
    ({"reaped": "yes"}, 400), ({"kind": "caller_says_ok"}, 400), ({"fault": "bad/fault"}, 400),
    ({"status": "success"}, 400), ({"length": 1}, 409), ({"length": "01"}, 413),
    ({"output_kind": "missing"}, 409), ({"output_kind": "oversized", "payload": "", "length": 1}, 409),
    ({"sent": "0"}, 409), ({"reaped": "0"}, 409), ({"fault": "deadline"}, 409),
    ({"status": "", "output_kind": "missing", "payload": ""}, 409),
    ({"kind": "refused", "fault": "deadline"}, 409),
])
def test_contradictory_or_malformed_facts_cannot_emit_any_commit(mcp, program, kwargs, code):
    refused(mcp, program, snapshot(facts(**kwargs)), code)


@pytest.mark.parametrize("phase,revision", [("1", "0"), ("1", "2"), ("2", "1"), ("4", "1")])
def test_result_requires_the_exact_current_live_claim(mcp, program, phase, revision):
    refused(mcp, program, snapshot(phase=phase, revision=revision), 409)


def abandoned(**kwargs):
    return facts(**{"kind": "abandoned", "reaped": "0", "fault": "owner_unavailable",
                    "status": "", "output_kind": "missing", "payload": "", **kwargs})


def test_recovery_preserves_original_owner_and_never_invents_a_response(mcp, program):
    chosen = completion(mcp, program, abandoned(), claim_generation="old-worker")
    assert chosen.phase == "4" and chosen.continuation == "record_after_commit"
    assert fields(chosen.request["writes"][0]["value"], "SD1\n", 5)[3:] == ["old-worker", "4"]
    assert fields(chosen.request["writes"][1]["value"], "DR1\n", 7)[3:] == ["old-worker", "4", "", ""]


@pytest.mark.parametrize("kwargs", [{"sent": "0"}, {"reaped": "1"}, {"fault": ""},
    {"fault": "cancelled"}, {"status": "ok"}, {"output_kind": "other"},
    {"status": "ok", "output_kind": "string", "payload": "pretend result"}])
def test_recovery_cannot_claim_unsent_stopped_or_observed_result(mcp, program, kwargs):
    refused(mcp, program, snapshot(abandoned(**kwargs), claim_generation="old-worker"), 409)


@pytest.mark.parametrize("phase,revision", [("1", "0"), ("1", "2"), ("2", "1"), ("4", "1")])
def test_recovery_requires_an_unfinished_retained_claim(mcp, program, phase, revision):
    refused(mcp, program, snapshot(abandoned(), phase=phase, revision=revision), 409)
