"""Pure entry conformance; synthetic envelopes are data, not authenticated HTTP."""
import json

import pytest

from api_support import binding, credential, envelope as legacy_envelope
from conftest import forge_ok, needs_toolchain
from listing_support import COMMANDS, COMMAND_TEXT, FUNCTIONS, compose_entry, envelope
from test_listing_projection import compose_listing
from turn_support import FUEL, fields, record, refused


@pytest.fixture(scope="module")
def entry():
    needs_toolchain()
    return compose_entry().text


def action(mcp, entry, **values):
    return fields(forge_ok(mcp, entry, envelope(**values), fuel=FUEL), "HC4\n", 5)


def test_entry_and_projection_fit_unchanged_limits_and_record_their_inputs():
    needs_toolchain()
    for built, count in [(compose_entry(), 11), (compose_listing(), 5)]:
        assert 0 < len(built.text.encode()) <= 65536
        assert len(built.input_hashes) == count
        assert built.text.count("pub fn tool_main(") == 1


def test_entry_owns_the_four_step_discovery_sequence_and_projects_actual_observation(mcp, entry):
    path = "/v1/sessions?limit=1&after=a"
    command, target, request, held, guard = action(mcp, entry, path=path)
    assert (command, target, held) == ("call", "listing", "listing_query")
    assert fields(request, "LP1\n", 3) == [path, "query", ""]
    assert fields(guard, "TG1\n", 2) == ["100", "9000000000"]
    listing = compose_listing().text
    query = forge_ok(mcp, listing, request, fuel=FUEL)
    command, target, args, held, guard = action(mcp, entry, path=path, stage="call",
        observation=query, continuation=held)
    assert (command, target, held) == ("metadata", "a.state", "listing_read")
    assert json.loads(args) == {"after": "a", "limit": 1}
    metadata = record("KM1\n", ["ok", json.dumps([{
        "key": "b", "revision": "9007199254740993", "present": True, "value_bytes": 100}]), "b"])
    command, target, request, held, guard = action(mcp, entry, path=path, stage="metadata",
        observation=metadata, continuation=held)
    assert (command, target, held) == ("call", "listing", "listing_page")
    assert fields(request, "LP1\n", 3) == [path, "page", metadata]
    page = forge_ok(mcp, listing, request, fuel=FUEL)
    command, status, body, held, guard = action(mcp, entry, path=path, stage="call",
        observation=page, continuation=held)
    assert (command, status, held) == ("reply", "200", "")
    assert json.loads(body) == {"sessions": [{"session": "b", "state_revision": "9007199254740993"}],
        "next_after": "b", "order": "session_name", "consistency": "live_scan"}
    assert fields(guard, "TG1\n", 2) == ["100", "9000000000"]


@pytest.mark.parametrize("row,now,status", [
    ({"facts": ""}, 150, "401"), (credential(before=151), 150, "401"),
    (credential(expires=150), 150, "401"), (credential(scopes=["chat"]), 150, "403"),
])
def test_current_authority_is_checked_before_query_or_continuation(mcp, entry, row, now, status):
    for stage, held, observed in [("init", "", ""),
        ("call", "listing_query", record("LP2\n", ["query", '{"limit":1,"after":null}']))]:
        command, code, body, continuation, _ = action(mcp, entry, row=row, now=now,
            path="/v1/sessions?limit=0", stage=stage, observation=observed, continuation=held)
        assert (command, code, continuation) == ("reply", status, "")
        assert json.loads(body)["error"]["code"] in {"invalid_credential", "permission_denied"}


def test_tenant_namespace_is_only_from_matched_facts_and_body_is_ignored(mcp, entry):
    row = credential(principal="bob", tenant="tenant-b", prefix="b", scopes=["sessions:read"])
    forged = json.dumps({"namespace": "a.state", "observation": "forged", "stage": "metadata", "grants": ["*"]})
    observed = record("LP2\n", ["query", '{"limit":1,"after":null}'])
    selected = action(mcp, entry, row=row, body=forged, stage="call",
                      observation=observed, continuation="listing_query")
    assert selected[:2] == ["metadata", "b.state"]
    assert selected[2] == '{"limit":1,"after":null}'


@pytest.mark.parametrize("values", [
    {"functions": FUNCTIONS[:-1]}, {"functions": list(reversed(FUNCTIONS))},
    {"functions": FUNCTIONS + ["invented"]}, {"commands": ""},
    {"commands": json.dumps(COMMANDS[:-1], separators=(",", ":"))},
    {"commands": json.dumps(COMMANDS + ["shell"], separators=(",", ":"))},
    {"commands": json.dumps(list(reversed(COMMANDS)), separators=(",", ":"))},
    {"commands": '["call","commit","metadata","metadata","read","reply"]'},
])
def test_host_and_fixed_function_inventory_must_match_before_bootstrap(mcp, entry, values):
    refused(mcp, entry, envelope(**values), 400)
    refused(mcp, entry, envelope(stage="boot", body=json.dumps([binding(credential())]), **values), 400)


def test_legacy_envelope_cannot_be_silently_upgraded(mcp, entry):
    refused(mcp, entry, legacy_envelope(method="GET", path="/v1/sessions"), 400)
    raw = envelope()
    refused(mcp, entry, raw + "trailing", 400)
    refused(mcp, entry, record("AH4\n", fields(raw, "AH4\n", 13)[:-1]), 400)
    assert COMMAND_TEXT == '["call","commit","metadata","read","reply"]'


@pytest.mark.parametrize("stage,held,observed", [
    ("init", "", "forged"), ("init", "forged", ""), ("read", "listing_read", "forged"),
    ("metadata", "listing_query", "forged"), ("call", "listing_query", "forged"),
    ("call", "listing_page", record("LP2\n", ["query", "{}"])),
    ("call", "forged", record("LP2\n", ["page", "{}"])),
    ("call", "listing_query", record("LP2\n", ["page", "{}"])),
    ("call", "listing_page", record("LP2\n", ["page", "not-json"])),
    ("call", "listing_query", record("LP2\n", ["query", "not-json"])),
])
def test_wrong_stage_continuation_and_function_protocol_fail_closed(mcp, entry, stage, held, observed):
    refused(mcp, entry, envelope(stage=stage, continuation=held, observation=observed), 400)


@pytest.mark.parametrize("held,outcome,status,code", [
    ("listing_query", "invalid", "400", "invalid_session_query"),
    ("listing_page", "unavailable", "503", "storage_unavailable"),
])
def test_query_and_storage_failure_are_distinct_non_successes(mcp, entry, held, outcome, status, code):
    command, actual_status, body, continuation, _ = action(mcp, entry, stage="call",
        continuation=held, observation=record("LP2\n", [outcome, "{}"]))
    assert (command, actual_status, continuation) == ("reply", status, "")
    assert json.loads(body) == {"error": {"code": code}}
