"""Actual compiled entry transitions, up to inspection; not final readiness.

Receipt inputs in this module are explicit independent fixtures. Actual native
accounting is covered separately by the admission conformance HTTP artifact.
"""

import json

import pytest

from api_support import BODY, binding, credential
from conftest import forge_ok, mcp as mcp, needs_toolchain
from emergency_support import NATIVE_ERRORS, cause, incoming, snapshot, source as emergency_source
from readiness_admission_support import durable_commit, durable_read, envelope, source, step
from request_policy_support import compose_request_policy, incoming as policy_input
from test_request_entry import reply
from turn_support import FUEL, fields, record, refused


@pytest.fixture(scope="module")
def entry():
    needs_toolchain()
    return source().text


def ready_step(mcp, entry, **kwargs):
    return step(mcp, entry, method="GET", path="/v1/ready", body="", **kwargs)


def normal_plan(mcp, entry, *, ready=True, body=""):
    kwargs = dict(method="GET", path="/v1/ready", body=body) if ready else dict(body=json.dumps(BODY))
    proposed = forge_ok(mcp, compose_request_policy().text, policy_input(**kwargs), fuel=FUEL)
    command = step(mcp, entry, **kwargs, stage="call", continuation="request-policy", observation=proposed)
    assert command[0] == "commit_observed"
    return command


def test_recipe_binds_all_inputs_without_widening_existing_limits():
    built = source()
    assert built == source()
    assert len(built.input_hashes) == 28
    assert len(built.text.encode()) <= 65536 and len(source(9223372036854775807).text.encode()) <= 65536
    assert built.compiler_input_sha256 != source(3).compiler_input_sha256
    assert built.text.count("pub fn tool_main(") == 1
    assert '"AH6\\n"' not in built.text and '"HC6\\n"' not in built.text
    assert "readiness_project(" not in built.text  # Deliberately still unfinished.


def test_real_bootstrap_validates_original_profiles_and_sorted_function_inventory(mcp, entry):
    body = json.dumps([binding(credential())])
    command = step(mcp, entry, stage="boot", body=body)
    assert command[:2] == ["call", "admission"] and command[3] == "registry"
    assert fields(command[2], "AV2\n", 1) == [body]
    approved = step(mcp, entry, stage="call", purpose="boot", body=body,
                    continuation="registry", observation="registry_validated")
    assert reply(approved) == (204, {}, None)


@pytest.mark.parametrize("presentation,row,code", [
    ("0", {"facts": ""}, "authentication_required"),
    ("1", {"facts": ""}, "invalid_credential"),
    ("1", credential(before=151), "invalid_credential"),
    ("1", credential(expires=150), "invalid_credential"),
])
def test_authentication_precedes_both_accounting_kinds(mcp, entry, presentation, row, code):
    command = ready_step(mcp, entry, presentation=presentation, row=row)
    assert reply(command)[::2] == (401, {"error": {"code": code}})
    assert command[4] == ""


@pytest.mark.parametrize("kwargs", [{}, {"body": "{"}, {"row": credential(scopes=[])},
                                  {"method": "GET", "path": "/v1/ready"}, {"path": "/unknown"}])
def test_all_authenticated_requests_start_with_real_observed_durable_read(mcp, entry, kwargs):
    command = step(mcp, entry, **kwargs)
    assert command[:4] == ["read_observed", "a.budget", "request-window", "request-rate"]
    assert fields(command[4], "TG1\n", 2) == ["100", "9000000000"]


@pytest.mark.parametrize("revision,presence,value", [("0", "0", ""), ("2", "0", ""), ("3", "1", "record-bytes")])
def test_positive_observation_translation_preserves_revision_presence_and_value(mcp, entry, revision, presence, value):
    command = ready_step(mcp, entry, stage="read_observed", continuation="request-rate",
                         observation=durable_read(revision, presence, value), fraction="1")
    args = fields(command[2], "RP1\n", 8)
    assert command[:2] == ["call", "request_policy"]
    assert args[:4] == [credential()["facts"], "2", "150", "1"]
    assert fields(args[4], "SR1\n", 3) == ["ok", revision, value]
    assert args[5:] == ["GET", "/v1/ready", ""]


@pytest.mark.parametrize("observed", [
    durable_read("00"), durable_read("0", "1", "value"), durable_read("1", "0", "value"),
    durable_read("1", "2", ""), durable_read(error="storage"), durable_read(origin="storage"),
    durable_read(status="unknown"), record("SR1\n", ["0", "0", ""]),
])
def test_malformed_durable_success_cannot_become_empty_quota_state(mcp, entry, observed):
    refused(mcp, entry, envelope(method="GET", path="/v1/ready", body="", stage="read_observed",
                               continuation="request-rate", observation=observed), 400)


@pytest.mark.parametrize("label", NATIVE_ERRORS)
def test_no_precheck_failure_can_open_temporary_accounting(mcp, entry, label):
    command = ready_step(mcp, entry, stage="read_observed", continuation="request-rate",
                         observation=cause(label, origin="precheck"))
    assert reply(command)[::2] == (503, {"error": {"code": "storage_unavailable"}})


@pytest.mark.parametrize("label", ["storage", "busy", "corrupt", "commit_uncertain", "reopen_required", "denied", "limit"])
def test_eligible_actual_failure_is_retained_before_temporary_read(mcp, entry, label):
    failed = cause(label)
    command = ready_step(mcp, entry, stage="read_observed", continuation="request-rate", observation=failed)
    assert command[:3] == ["temporary_read", "a.budget", "readiness-window"]
    assert fields(command[3], "RE1\n", 1) == [failed]


@pytest.mark.parametrize("kwargs,status,code", [
    ({"row": credential(scopes=["chat"])}, 403, "permission_denied"),
    ({"method": "POST"}, 503, "storage_unavailable"),
    ({"path": "/v1/health"}, 503, "storage_unavailable"),
    ({"row": credential(expires=150)}, 401, "invalid_credential"),
])
def test_route_scope_and_expiry_refuse_before_temporary_read(mcp, entry, kwargs, status, code):
    options = dict(method="GET", path="/v1/ready", body="", stage="read_observed",
                   continuation="request-rate", observation=cause())
    options.update(kwargs)
    assert reply(step(mcp, entry, **options))[::2] == (status, {"error": {"code": code}})


def test_normal_receipt_unlocks_inspection_or_existing_domain_never_before_commit(mcp, entry):
    plan = normal_plan(mcp, entry)
    inspect = ready_step(mcp, entry, stage="commit_observed", continuation=plan[3], observation=durable_commit())
    assert inspect[0] == "inspect_execution" and inspect[2] == ""
    assert json.loads(inspect[1]) == ["a.requests", "a.operations", "a.state", "a.intent", "a.budget", "a.reservation"]
    assert fields(inspect[3], "RN1\n", 1) == ["0"]
    plan = normal_plan(mcp, entry, ready=False)
    domain = step(mcp, entry, stage="commit_observed", continuation=plan[3], observation=durable_commit())
    assert domain[0] == "read_many"
    assert fields(domain[3], "RP5\n", 2)[1] == "domain-set"


def test_commit_failure_fallback_fits_eight_entry_evaluations_and_requires_actual_receipt(mcp, entry):
    first = ready_step(mcp, entry)
    second = ready_step(mcp, entry, stage="read_observed", continuation=first[3], observation=durable_read())
    assert second[0] == "call"
    # Feed the entry's ACTUAL translated output into the unchanged compiled
    # policy. Hand-building a separate valid RP1 would miss a broken adapter.
    proposed_normal = forge_ok(mcp, compose_request_policy().text, second[2], fuel=FUEL)
    third = ready_step(mcp, entry, stage="call", continuation=second[3], observation=proposed_normal)
    assert third[0] == "commit_observed"
    failed = cause("commit_uncertain", "DC2\n")
    fourth = ready_step(mcp, entry, stage="commit_observed", continuation=third[3], observation=failed)
    fifth = ready_step(mcp, entry, stage="temporary_read", continuation=fourth[3], observation=snapshot())
    assert fifth[:2] == ["call", "emergency_policy"]
    args = fields(fifth[2], "ER1\n", 8)
    assert args == fields(incoming(failed=failed), "ER1\n", 8)
    proposed = forge_ok(mcp, emergency_source().text, fifth[2], fuel=FUEL)
    sixth = ready_step(mcp, entry, stage="call", continuation=fifth[3], observation=proposed)
    assert sixth[0] == "temporary_write" and sixth[2] == "" and sixth[3] == "emergency-commit"
    assert sixth[1] == fields(proposed, "EA1\n", 4)[1]
    seventh = ready_step(mcp, entry, stage="temporary_write", continuation=sixth[3],
                         observation=record("VC1\n", ["ok", "1", ""]))
    assert seventh[0] == "inspect_execution" and fields(seventh[3], "RN1\n", 1) == ["1"]
    # The eighth evaluation is RESERVED for the unfinished final readiness
    # decision. This test proves the transition budget, not that final decision.
    assert len([first, second, third, fourth, fifth, sixth, seventh]) == 7


@pytest.mark.parametrize("revision,presence,value,admitted", [
    ("0", "0", "", True),
    ("2", "1", record("RW1\n", ["tenant-a", "120", "1"]), True),
    ("2", "0", "", False),
])
def test_translated_durable_observation_is_consumed_by_real_policy_without_tombstone_reset(
        mcp, entry, revision, presence, value, admitted):
    call = ready_step(mcp, entry, stage="read_observed", continuation="request-rate",
                      observation=durable_read(revision, presence, value))
    if not admitted:
        refused(mcp, compose_request_policy().text, call[2], 400)
        return
    answer = forge_ok(mcp, compose_request_policy().text, call[2], fuel=FUEL)
    rate = fields(fields(answer, "RP2\n", 5)[0], "RA1\n", 4)
    assert rate[0] == "admit"
    mutation = json.loads(rate[1])["writes"][0]
    assert mutation["revision"] == int(revision)
    assert fields(mutation["value"], "RW1\n", 3)[2] == ("1" if revision == "0" else "2")


@pytest.mark.parametrize("label", NATIVE_ERRORS)
def test_failed_temporary_cas_never_retries_resets_or_inspects(mcp, entry, label):
    command = ready_step(mcp, entry, stage="temporary_write", continuation="emergency-commit",
                         observation=record("VC1\n", ["error", "0", label]))
    assert reply(command)[::2] == (503, {"error": {"code": "storage_unavailable"}})


@pytest.mark.parametrize("receipt", [
    record("VC1\n", ["ok", "0", ""]), record("VC1\n", ["ok", "01", ""]),
    record("VC1\n", ["ok", "1", "storage"]), record("VC1\n", ["error", "1", "storage"]),
    record("VC1\n", ["error", "0", "invented"]), durable_commit(), record("SC1\n", ["ok", "1"]),
])
def test_malformed_or_durable_receipts_cannot_unlock_emergency_inspection(mcp, entry, receipt):
    refused(mcp, entry, envelope(method="GET", path="/v1/ready", body="", stage="temporary_write",
                               continuation="emergency-commit", observation=receipt), 400)


@pytest.mark.parametrize("stage,continuation,observation", [
    ("temporary_read", record("RE1\n", [cause()]), snapshot()),
    ("call", "emergency-policy", record("EA1\n", ["limited", "", "", "61"])),
    ("temporary_write", "emergency-commit", record("VC1\n", ["ok", "1", ""])),
])
def test_expiry_is_rechecked_at_each_emergency_entry_boundary(mcp, entry, stage, continuation, observation):
    command = ready_step(mcp, entry, row=credential(expires=150), stage=stage,
                         continuation=continuation, observation=observation)
    assert reply(command)[::2] == (401, {"error": {"code": "invalid_credential"}})


def test_real_exhausted_emergency_policy_preserves_61_second_retry_header(mcp, entry):
    proposed = forge_ok(mcp, emergency_source().text,
        incoming(clock_ns=100_000_000_000, observed=snapshot(revision=2, events=[100_000_000_000] * 2)), fuel=FUEL)
    command = ready_step(mcp, entry, stage="call", continuation="emergency-policy", observation=proposed)
    status, headers, body = reply(command)
    assert status == 429 and headers["retry-after"] == "61" and body == {"error": {"code": "rate_limited"}}


def test_unfinished_dependency_path_cannot_accidentally_claim_product_readiness(mcp, entry):
    observed = record("EI1\n", [record("SI1\n", ["ok", "", ""]), record("RI1\n", ["ok", "", "0"])])
    refused(mcp, entry, envelope(method="GET", path="/v1/ready", body="", stage="inspect_execution",
                               continuation=record("RN1\n", ["0"]), observation=observed), 400)
