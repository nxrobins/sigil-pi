"""Compiled SIGIL accounting/body plans, not HTTP admission or commit receipts."""

import hashlib
import json

import pytest

from api_support import BODY, credential
from conftest import forge_ok, mcp as mcp, needs_toolchain
from request_policy_support import ROOT, compose_request_policy, incoming
from request_rate_support import snapshot
from turn_support import FUEL, fields, record, refused


@pytest.fixture(scope="module")
def program():
    needs_toolchain()
    return compose_request_policy().text


def plan(mcp, program, **kwargs):
    answer = fields(forge_ok(mcp, program, incoming(**kwargs), fuel=FUEL), "RP2\n", 5)
    return fields(answer[0], "RA1\n", 4), answer[1:]


def assert_admission_only(rate):
    assert rate[0] == "admit" and rate[3] == ""
    assert fields(rate[2], "TG1\n", 2) == ["100", "9000000000"]
    assert json.loads(rate[1]) == {"op": "commit", "checks": [], "writes": [{
        "namespace": "a.budget", "key": "request-window", "revision": 0,
        "value": record("RW1\n", ["tenant-a", "120", "1"]),
    }]}


def test_policy_binds_all_inputs_and_moves_the_real_decoder_into_one_pure_entry():
    built = compose_request_policy()
    assert built == compose_request_policy()
    assert len(built.text.encode()) <= 65536 and built.text.count("pub fn tool_main(") == 1
    assert built.text.count("pub fn decode_submission(") == 1
    assert built.text.count("fn request_rate(") == 1
    assert len(built.input_hashes) == 8
    for relative in ("app/pi/request_rate.sigil", "app/pi/request_policy.sigil", "scripts/compose_request_policy.py"):
        assert built.input_hashes[relative] == hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()


@pytest.mark.parametrize("body", [json.dumps(BODY), json.dumps(BODY, indent=2),
    json.dumps(dict(reversed(list(BODY.items())))).replace('"session"', '"s\\u0065ssion"')])
def test_equivalent_submissions_produce_one_canonical_body_without_domain_writes(mcp, program, body):
    rate, following = plan(mcp, program, body=body)
    assert_admission_only(rate)
    assert following == ["submission", record("PS1\n", [BODY["session"], BODY["message"], BODY["submission_key"]]), "", ""]


@pytest.mark.parametrize("message", ['line\nwith\tcontrols\x00', 'café 😀', 'quotes " and \\'])
def test_relocated_decoder_preserves_exact_utf8_and_control_bytes(mcp, program, message):
    body = {**BODY, "message": message}
    rate, following = plan(mcp, program, body=json.dumps(body))
    assert_admission_only(rate)
    assert fields(following[1], "PS1\n", 3) == [BODY["session"], message, BODY["submission_key"]]
    assert following[0] == "submission" and following[2:] == ["", ""]


@pytest.mark.parametrize("body", ["", "{", "{}", "[]", "null", "true",
    json.dumps({**BODY, "session": "../escape"}),
    json.dumps({**BODY, "message": ""}), json.dumps({**BODY, "grants": "all"}),
    json.dumps(BODY)[:-1] + ',"message":"duplicate"}'])
def test_bad_submission_is_deferred_but_does_not_remove_the_accounting_proposal(mcp, program, body):
    rate, following = plan(mcp, program, body=body)
    assert_admission_only(rate)
    assert following == ["refused", "", "400", "invalid_request"]


def test_oversize_message_keeps_the_existing_413_mapping_and_quota_proposal(mcp, program):
    rate, following = plan(mcp, program, body=json.dumps({**BODY, "message": "m" * 262145}))
    assert_admission_only(rate)
    assert following == ["refused", "", "413", "request_too_large"]


@pytest.mark.parametrize("method,path", [("GET", "/v1/health"), ("GET", "/v1/version"),
    ("GET", "/v1/ready"), ("GET", "/v1/sessions"), ("GET", "/v1/operations/invalid"),
    ("POST", "/v1/chat"), ("POST", "/v1/schedules"), ("DELETE", "/v1/sessions/name"),
    ("PATCH", "/v1/operations"), ("POST", "/unknown"), ("POST", "/v1/operations?unexpected=1")])
def test_other_routes_retain_original_body_and_do_not_escape_request_accounting(mcp, program, method, path):
    raw = "deliberately-not-submission-json"
    rate, following = plan(mcp, program, method=method, path=path, body=raw)
    assert_admission_only(rate)
    assert following == ["raw", raw, "", ""]


@pytest.mark.parametrize("scopes", [[], ["ops:read"], ["sessions:read", "schedules:write"]])
def test_missing_chat_scope_preserves_permission_precedence_over_body_decoding(mcp, program, scopes):
    rate, following = plan(mcp, program, row=credential(scopes=scopes), body="{")
    assert_admission_only(rate)
    assert following == ["raw", "{", "", ""]


@pytest.mark.parametrize("kwargs,expected", [
    ({"limit": 1, "observed": snapshot(revision=1, count=1)}, ["limited", "", "", "30"]),
    ({"limit": 1, "fractional": 1, "observed": snapshot(revision=1, count=1)}, ["limited", "", "", "29"]),
    ({"observed": snapshot(status="error")}, ["storage", "", "", ""]),
    ({"observed": snapshot(revision=1, window=180)}, ["clock", "", "", ""]),
    ({"row": credential(expires=150), "observed": "malformed"}, ["expired", "", "", ""]),
])
def test_nonadmission_returns_no_decoded_body_deferred_error_or_commit(mcp, program, kwargs, expected):
    rate, following = plan(mcp, program, body="{", **kwargs)
    assert rate == expected and following == ["", "", "", ""]


@pytest.mark.parametrize("kwargs", [{"row": {"facts": ""}}, {"row": {"facts": "forged"}},
    {"limit": "0"}, {"limit": "01"}, {"fractional": "2"}, {"now": "0150"},
    {"observed": record("SR1\n", ["error", "0", "partial-forged-value"])}])
def test_invalid_bound_inputs_cannot_produce_an_accounting_plan(mcp, program, kwargs):
    refused(mcp, program, incoming(**kwargs), 400)
