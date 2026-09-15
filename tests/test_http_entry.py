"""Actual SIGIL ABI-wrapper conformance, not a native service or migrated route."""
import json

import pytest

from api_support import BODY, binding, credential
from scripts.compose_http_entry import HEADER_NAMES, compose_http_entry
from conftest import PI_ROOT, SIGIL_ROOT, forge_ok, needs_toolchain
from http_compat_support import FRESH, legacy_request_id, request_facts
from listing_support import compose_entry, envelope
from turn_support import FUEL, fields, record

INVENTORY = json.dumps(HEADER_NAMES, separators=(",", ":"))


@pytest.fixture(scope="module")
def entries():
    needs_toolchain()
    before = compose_entry().text
    after = compose_http_entry(PI_ROOT, SIGIL_ROOT).text
    assert len(after.encode()) <= 65536
    return before, after


def upgrade(incoming, *, hints=(), facts=None, inventory=INVENTORY):
    values = fields(incoming, "AH4\n", 13)
    metadata = "" if values[11] == "boot" else request_facts(hints)
    return record("AH5\n", [*values, metadata if facts is None else facts, inventory])


def decision(mcp, source, incoming, marker):
    return fields(forge_ok(mcp, source, incoming, fuel=FUEL), marker, 5)


REPLIES = [
    {"method": "GET", "path": "/v1/health"},
    {"method": "GET", "path": "/v1/missing"},
    {"method": "GET", "path": "/v1/health", "row": credential(scopes=[])},
    {"method": "GET", "path": "/v1/health", "row": {"facts": ""}},
    {"method": "GET", "path": "/v1/health", "now": 200, "row": credential(expires=200)},
    {"method": "POST", "path": "/v1/operations", "body": "{"},
    {"method": "POST", "path": "/v1/operations", "body": json.dumps({**BODY, "request_id": "forged-body-id"})},
    {"method": "POST", "path": "/v1/operations", "body": json.dumps(BODY),
     "stage": "commit", "observation": record("SC1\n", ["ok", "1"]), "continuation": "a" * 64},
    {"method": "POST", "path": "/v1/operations", "body": json.dumps(BODY),
     "stage": "commit", "observation": record("SC1\n", ["error", "0"]), "continuation": "a" * 64},
]


@pytest.mark.parametrize("incoming", [envelope(**case) for case in REPLIES])
@pytest.mark.parametrize("hints", [[], [b"caller-trace-id"]])
def test_http_wrapper_keeps_existing_sigil_status_body_and_guard(mcp, entries, incoming, hints):
    old = decision(mcp, entries[0], incoming, "HC4\n")
    new = decision(mcp, entries[1], upgrade(incoming, hints=hints), "HC5\n")
    assert old[0] == new[0] == "reply"
    mime, headers, body = fields(new[2], "HR1\n", 3)
    assert mime == "application/json"
    assert fields(headers, "HH1\n", 3) == ["1", "x-request-id", legacy_request_id(hints)]
    assert [*new[:2], body, *new[3:]] == old


@pytest.mark.parametrize("incoming", [
    envelope(method="POST", path="/v1/operations", body=json.dumps(BODY)),
    envelope(method="GET", path="/v1/sessions"),
    envelope(method="GET", path="/v1/operations/" + "a" * 64),
    envelope(stage="boot", body=json.dumps([binding(credential())])),
])
def test_http_wrapper_adds_no_host_action_or_step_before_the_original_command(mcp, entries, incoming):
    old = decision(mcp, entries[0], incoming, "HC4\n")
    new = decision(mcp, entries[1], upgrade(incoming), "HC5\n")
    assert old[0] in {"read", "call"}
    assert new == old


def test_boot_approval_remains_guarded_empty_and_without_http_metadata(mcp, entries):
    incoming = envelope(stage="call", purpose="boot", body=json.dumps([binding(credential())]),
        observation="profiles_validated", continuation="profiles")
    old = decision(mcp, entries[0], incoming, "HC4\n")
    new = decision(mcp, entries[1], upgrade(incoming), "HC5\n")
    assert old[:4] == ["reply", "204", "", ""] and old[4]
    mime, headers, body = fields(new[2], "HR1\n", 3)
    assert mime == "application/json" and fields(headers, "HH1\n", 1) == ["0"] and body == ""
    assert [*new[:2], body, *new[3:]] == old


@pytest.mark.parametrize("incoming", [
    upgrade(envelope(method="POST", path="/v1/operations", body=json.dumps(BODY)), inventory="[]"),
    upgrade(envelope(), inventory=json.dumps(list(reversed(HEADER_NAMES)), separators=(",", ":"))),
    upgrade(envelope(), facts=""),
    upgrade(envelope(), facts=record("RF1\n", [FRESH, "0", "61"])),
    upgrade(envelope(stage="boot", body=json.dumps([binding(credential())])), facts=request_facts()),
    envelope(),
])
def test_http_wrapper_refuses_missing_mismatched_or_misrouted_facts_before_core_work(mcp, entries, incoming):
    result = mcp.forge(entries[1], input=incoming, fuel=FUEL)
    assert result["status"] == "error", result
    diagnostic = result["diagnostics"][0]
    assert diagnostic["code"] == "R803", diagnostic
    assert diagnostic["message"] == "tool trapped: tool returned error (400)", diagnostic
