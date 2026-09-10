"""Actual SIGIL v8 entry transitions; native receipts are explicit test inputs."""

import hashlib
import json

import pytest

from api_support import BODY, binding, credential, envelope as base_envelope
from conftest import forge_ok, mcp as mcp, needs_toolchain
from http_compat_support import request_facts
from request_entry_support import ROOT, compose_request_entry
from request_policy_support import compose_request_policy, incoming as policy_input
from request_rate_support import snapshot
from scripts.compose_http_entry import HEADER_NAMES
from turn_support import FUEL, fields, record, refused

COMMANDS = '["call","commit","metadata","read","read_many","reply"]'
INVENTORY = json.dumps(HEADER_NAMES, separators=(",", ":"))


def envelope(*, presentation="1", fraction="0", hints=(), **kwargs):
    values = fields(base_envelope(functions=["admission", "history", "listing", "request_policy"], **kwargs), "AH3\n", 12)
    booting = values[11] == "boot"
    return record("AH6\n", [*values, COMMANDS, "" if booting else request_facts(hints),
        INVENTORY, "" if booting else presentation, fraction])


@pytest.fixture(scope="module")
def entry():
    needs_toolchain()
    return compose_request_entry(2).text


def step(mcp, entry, **kwargs):
    return fields(forge_ok(mcp, entry, envelope(**kwargs), fuel=FUEL), "HC6\n", 5)


def reply(command):
    assert command[0] == "reply" and command[3] == ""
    mime, headers, body = fields(command[2], "HR1\n", 3)
    assert mime == "application/json"
    count_size = int(headers[4:12])
    count = int(headers[12:12 + count_size])
    pairs = fields(headers, "HH1\n", 1 + 2 * count)
    return int(command[1]), dict(zip(pairs[1::2], pairs[2::2])), json.loads(body) if body else None


def policy(mcp, **kwargs):
    return forge_ok(mcp, compose_request_policy().text, policy_input(**kwargs), fuel=FUEL)


def test_full_entry_fits_without_removing_tokens_or_implicit_limit_defaults():
    built = compose_request_entry(2)
    assert built == compose_request_entry(2)
    assert 0 < len(built.text.encode()) <= 65536 < len(built.layout_input.encode())
    assert built.text.count("pub fn tool_main(") == 1
    assert "decode_submission(" not in built.text and "prepared_submission(" in built.text
    assert len(built.input_hashes) == 20
    assert len(compose_request_entry(9223372036854775807).text.encode()) <= 65536
    assert built.compiler_input_sha256 != compose_request_entry(3).compiler_input_sha256
    for name in ("app/pi/request_entry.sigil", "app/pi/request_grouped.sigil", "app/shared/read_set.sigil",
                 "scripts/compose_request_entry.py", "scripts/sigil_layout.py"):
        assert built.input_hashes[name] == hashlib.sha256((ROOT / name).read_bytes()).hexdigest()


@pytest.mark.parametrize("limit", [None, True, False, 0, -1, 1.0, "2", 9223372036854775808])
def test_invalid_or_implicit_build_limits_cannot_create_an_entry(limit):
    with pytest.raises(ValueError):
        compose_request_entry(limit)


def test_real_bootstrap_retains_profile_validation_and_never_counts_as_a_request(mcp, entry):
    body = json.dumps([binding(credential())])
    first = step(mcp, entry, stage="boot", body=body)
    assert first[:2] == ["call", "admission"] and first[3] == "profiles"
    assert fields(first[2], "AV1\n", 1) == [body]
    assert fields(first[4], "TG1\n", 2) == ["150", "9000000000"]
    approved = step(mcp, entry, stage="call", purpose="boot", body=body,
                    observation="profiles_validated", continuation="profiles")
    assert reply(approved) == (204, {}, None)
    assert fields(approved[4], "TG1\n", 2) == ["150", "9000000000"]


@pytest.mark.parametrize("presentation,row,code", [
    ("0", {"facts": ""}, "authentication_required"),
    ("1", {"facts": ""}, "invalid_credential"),
    ("1", credential(before=151), "invalid_credential"),
    ("1", credential(expires=150), "invalid_credential"),
])
def test_unmatched_or_inactive_credentials_reply_without_any_accounting_read(mcp, entry, presentation, row, code):
    command = step(mcp, entry, presentation=presentation, row=row, body="{")
    status, headers, body = reply(command)
    assert status == 401 and body == {"error": {"code": code}}
    assert set(headers) == {"x-request-id"} and command[4] == ""


@pytest.mark.parametrize("case", [{}, {"body": "{"}, {"row": credential(scopes=[]), "body": "{"},
    {"method": "GET", "path": "/v1/health"}, {"method": "POST", "path": "/unknown"}])
def test_all_active_requests_read_the_bound_quota_before_route_or_body_handling(mcp, entry, case):
    command = step(mcp, entry, **case)
    assert command[:4] == ["read", "a.budget", "request-window", "request-rate"]
    assert fields(command[4], "TG1\n", 2) == ["100", "9000000000"]


def test_policy_call_receives_original_request_and_actual_observation_with_same_sample_fraction(mcp, entry):
    raw = '{"body":"not-a-valid-submission"}'
    observed = snapshot(revision=3, count=1)
    command = step(mcp, entry, body=raw, stage="read", continuation="request-rate", observation=observed, fraction="1")
    assert command[:2] == ["call", "request_policy"] and command[3] == "request-policy"
    assert fields(command[2], "RP1\n", 8) == [credential()["facts"], "2", "150", "1", observed,
                                             "POST", "/v1/operations", raw]


@pytest.mark.parametrize("raw", [json.dumps(BODY), "{"])
def test_policy_proposal_must_be_committed_before_domain_work_or_deferred_error(mcp, entry, raw):
    proposed = policy(mcp, body=raw)
    command = step(mcp, entry, body=raw, stage="call", continuation="request-policy", observation=proposed)
    assert command[0] == "commit" and command[2] == ""
    assert json.loads(command[1]) == json.loads(fields(fields(proposed, "RP2\n", 5)[0], "RA1\n", 4)[1])
    plan = fields(command[3], "RP4\n", 4)
    assert plan[0] == ("refused" if raw == "{" else "submission")
    failed = step(mcp, entry, body=raw, stage="commit", continuation=command[3], observation=record("SC1\n", ["error", "0"]))
    assert reply(failed)[::2] == (503, {"error": {"code": "storage_unavailable"}})
    committed = step(mcp, entry, body=raw, stage="commit", continuation=command[3], observation=record("SC1\n", ["ok", "1"]))
    if raw == "{":
        assert reply(committed)[::2] == (400, {"error": {"code": "invalid_request"}})
    else:
        assert committed[0] == "read_many" and committed[2] == ""
        assert json.loads(committed[1]) == [
            {"namespace": "a.requests", "key": "alice".encode().hex() + ":" + BODY["submission_key"]},
            {"namespace": "a.state", "key": BODY["session"]},
            {"namespace": "a.budget", "key": "active"}]
        prepared, inner = fields(committed[3], "RP5\n", 2)
        assert inner == "domain-set"
        assert fields(prepared, "PS1\n", 3) == [BODY["session"], BODY["message"], BODY["submission_key"]]


@pytest.mark.parametrize("receipt", [record("SC1\n", ["ok", "0"]), record("SC1\n", ["ok", "01"]),
    record("SC1\n", ["error", "1"]), record("SC1\n", ["other", "1"]), "caller-supplied-receipt"])
def test_malformed_receipts_cannot_unlock_the_held_body(mcp, entry, receipt):
    plan = record("RP4\n", ["submission", record("PS1\n", [BODY["session"], BODY["message"], BODY["submission_key"]]), "", ""])
    refused(mcp, entry, envelope(stage="commit", continuation=plan, observation=receipt), 400)


def test_exhausted_rate_returns_retry_metadata_without_any_write_or_body_error(mcp, entry):
    observed = policy(mcp, body="{", fractional=1, observed=snapshot(revision=1, count=2))
    command = step(mcp, entry, body="{", stage="call", continuation="request-policy", observation=observed, fraction="1")
    status, headers, body = reply(command)
    assert status == 429 and body == {"error": {"code": "rate_limited"}}
    assert headers["retry-after"] == "29" and set(headers) == {"retry-after", "x-request-id"}
