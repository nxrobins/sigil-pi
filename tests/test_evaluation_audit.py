"""Compiled structural classification. Synthetic facts do not prove provenance."""
import hashlib
import json

import pytest

from conftest import PI_ROOT, SIGIL_ROOT, forge_ok
from scripts.compose_evaluation_audit import compose_evaluation_audit
from test_transaction_audit import manifest
from turn_support import FUEL, fields, record


def encoded(value, *, sorted_keys=False):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=sorted_keys)


def content(value, representation="utf8"):
    raw = value.encode()
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "representation": representation}


def facts(mode="fresh", case="proposal"):
    inner = {"status": "ok", "data": {"output_text": "private-output"}}
    value = {
        "mode": mode,
        "admitted_source_sha256": "1" * 64,
        "admitted_runtime_sha256": "d" * 64,
        "input_bytes": len("private-input"),
        "input": content("private-input"),
        "selected_fuel": FUEL,
        "selected_timeout_ms": 14000,
        "elapsed_ms": 125,
        "boundary": "complete",
        "error": None,
        "fault": None,
        "request_may_have_run": None if mode == "cached" else True,
        "worker_reaped": None if mode == "cached" else True,
        "runtime_status": "ok",
        "parsed_result": None,
        "output_kind": "string",
        "output": content("private-output"),
    }
    if case == "runtime_error":
        inner = {"status": "error", "error": {"message": "private-diagnostic"}}
        value.update(boundary="runtime_status", error="application", runtime_status="error",
                     output_kind="missing", output=None)
    elif case == "output_invalid":
        inner = {"status": "ok", "data": {"output_text": None}}
        value.update(boundary="output", error="protocol", output_kind="non_string", output=None)
    elif case == "empty_output":
        inner = {"status": "ok", "data": {"output_text": ""}}
        value["output"] = content("")
    elif case in {"protocol_fault", "predeadline", "oversized_input", "replacement"}:
        inner = None
        value.update(error="worker", fault="protocol", runtime_status=None,
                     output_kind="not_observed", output=None,
                     boundary="cached_invoke" if mode == "cached" else "observation")
        if case == "predeadline":
            value.update(error="deadline", fault=None, selected_timeout_ms=None,
                         boundary="before_invoke", request_may_have_run=None, worker_reaped=None)
        elif case == "oversized_input":
            value.update(input_bytes=4194305, input=None, fault="limit",
                         boundary="cached_invoke" if mode == "cached" else "prepare",
                         request_may_have_run=None, worker_reaped=None)
        elif case == "replacement":
            value.update(fault="invalid", request_may_have_run=None if mode == "cached" else False)
    if inner is not None:
        value["parsed_result"] = content(encoded(inner, sorted_keys=True), "serde-json-value/v1")
    return value


def envelope(value=None, code=None):
    value = facts() if value is None else value
    code = code or value["error"] or "binding"
    return ["a" * 64, "b" * 64, "c" * 64, manifest("1" * 64), manifest("2" * 64),
            '{"a.intent":"read"}', "e" * 64, "2", code, encoded(value)]


@pytest.fixture(scope="module")
def evaluation_source():
    return compose_evaluation_audit(PI_ROOT, SIGIL_ROOT).text


def project(mcp, source, value):
    answer = forge_ok(mcp, source, record("EA1\n", value), fuel=FUEL)
    action, payload = fields(answer, "AR1\n", 2)
    assert action == "publish"
    return json.loads(payload)


def test_actual_compilation_and_non_publishing_boot_admission(mcp, evaluation_source):
    built = compose_evaluation_audit(PI_ROOT, SIGIL_ROOT)
    assert len(built.text.encode()) <= 65536 and built.text.count("pub fn tool_main(") == 1
    assert built.stdlib_hash == "b5f40e2eba41b6734f8db071"
    assert built == compose_evaluation_audit(PI_ROOT, SIGIL_ROOT)
    boot = record("EB1\n", ["b" * 64, manifest("2" * 64), '{"a.intent":"read"}'])
    assert forge_ok(mcp, evaluation_source, boot, fuel=FUEL) == "evaluation_audit_admitted"


@pytest.mark.parametrize("mode", ["fresh", "cached"])
@pytest.mark.parametrize("case", ["proposal", "runtime_error", "output_invalid", "empty_output",
                                  "protocol_fault", "predeadline", "oversized_input", "replacement"])
def test_truthful_classification_preserves_actual_availability(mcp, evaluation_source, mode, case):
    value = facts(mode, case)
    event = project(mcp, evaluation_source, envelope(value))
    assert event["schema"] == "sigil-pi/evaluation-failure-audit/v1"
    assert event["event"] == ("pure_proposal_refused" if value["error"] is None else "pure_evaluation_failed")
    assert event["evaluation"] == value
    assert event["mechanical_refusal"] == (value["error"] or "binding")
    assert event["subject_storage_grants"] == {"a.intent": "read"}
    assert event["lookup_count"] == 2
    assert not {"authorized", "denied", "delivered", "claim", "receipt", "operation", "principal"} & event.keys()
    raw = encoded(event)
    for private in ["private-input", "private-output", "private-diagnostic"]:
        assert private not in raw
    assert len(raw.encode()) <= 16384


@pytest.mark.parametrize("key,value", [
    ("mode", "other"), ("admitted_source_sha256", "3" * 64),
    ("admitted_runtime_sha256", "3" * 64), ("input_bytes", 1), ("input", None),
    ("selected_fuel", FUEL + 1), ("selected_timeout_ms", 0), ("selected_timeout_ms", 15001),
    ("elapsed_ms", -1), ("boundary", "arbitrary"), ("error", "denied"), ("fault", "private-diagnostic"),
    ("request_may_have_run", False), ("worker_reaped", None), ("runtime_status", "other"),
    ("parsed_result", None), ("output_kind", "not_observed"), ("output", None),
    ("input_bytes", "13"), ("selected_fuel", str(FUEL)), ("selected_timeout_ms", "14000"),
    ("elapsed_ms", "125"), ("request_may_have_run", "true"), ("fault", "null"),
])
def test_misbound_or_coerced_facts_are_refused(mcp, evaluation_source, key, value):
    observation = facts()
    observation[key] = value
    result = mcp.forge(evaluation_source, input=record("EA1\n", envelope(observation)), fuel=FUEL)
    assert result["status"] == "error" and not result.get("data", {}).get("output_text")


@pytest.mark.parametrize("change", ["extra", "duplicate", "reorder", "nested_extra", "wrong_representation",
                                    "cached_fresh_flags", "mismatched_error", "raw_payload", "extra_frame"])
def test_unknown_or_inconsistent_payloads_are_not_a_signing_interface(mcp, evaluation_source, change):
    value = facts()
    if change == "nested_extra": value["output"]["private"] = "private-output"
    elif change == "wrong_representation": value["parsed_result"]["representation"] = "utf8"
    elif change == "cached_fresh_flags": value["mode"] = "cached"
    elif change == "mismatched_error": value = facts(case="runtime_error")
    values = envelope(value)
    if change == "extra": values[9] = values[9][:-1] + ',"private":"private-output"}'
    elif change == "duplicate": values[9] = values[9][:-1] + ',"mode":"fresh"}'
    elif change == "reorder": values[9] = encoded(value, sorted_keys=True)
    elif change == "mismatched_error": values[8] = "worker"
    elif change == "raw_payload": values[9] = "sign this private-output"
    elif change == "extra_frame": values.append("private-output")
    result = mcp.forge(evaluation_source, input=record("EA1\n", values), fuel=FUEL)
    assert result["status"] == "error" and not result.get("data", {}).get("output_text")


@pytest.mark.parametrize("code", ["intent", "claimed", "time_guard"])
def test_post_evaluation_attempt_binding_refusals_are_not_effect_observations(mcp, evaluation_source, code):
    values = envelope(code=code)
    event = project(mcp, evaluation_source, values)
    assert event["event"] == "pure_proposal_refused"
    assert event["evaluation"]["error"] is None
    assert event["mechanical_refusal"] == code
    assert not {"claim", "receipt", "delivered", "authorized"} & event.keys()


def test_empty_subject_grants_do_not_invent_storage_authority(mcp, evaluation_source):
    values = envelope()
    values[5] = "{}"
    assert project(mcp, evaluation_source, values)["subject_storage_grants"] == {}
