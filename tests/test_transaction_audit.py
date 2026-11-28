"""Real compiled audit policy; direct facts here are structural fixtures only."""
import hashlib
import json

import pytest

from conftest import PI_ROOT, SIGIL_ROOT, forge_ok
from scripts.compose_transaction_audit import compose_transaction_audit
from turn_support import FUEL, fields, record, refused


def manifest(source="c" * 64):
    return record("TM1\n", [source, "d" * 64, str(FUEL), "15000"])


def observation():
    return ["a" * 64, "b" * 64, manifest(), manifest("e" * 64), '{"a.state":"read_write"}',
            "100", "101", "230", str(FUEL), "14990", "f" * 64, "123", "1" * 64,
            "300", "2" * 64, "250", "0", "1"]


@pytest.fixture(scope="module")
def audit_source():
    return compose_transaction_audit(PI_ROOT, SIGIL_ROOT).text


def project(mcp, source, facts):
    return fields(forge_ok(mcp, source, record("TA1\n", facts), fuel=FUEL), "AR1\n", 2)


def test_actual_compilation_retains_bounded_hash_only_schema(mcp, audit_source):
    built = compose_transaction_audit(PI_ROOT, SIGIL_ROOT)
    assert built == compose_transaction_audit(PI_ROOT, SIGIL_ROOT)
    assert len(built.text.encode()) <= 65536 and built.text.count("pub fn tool_main(") == 1
    assert built.stdlib_hash == "b5f40e2eba41b6734f8db071"
    assert all(not path.startswith("tests/") for path in built.input_hashes)
    kind, raw = project(mcp, audit_source, observation())
    event = json.loads(raw)
    assert kind == "publish"
    assert event["schema"] == "sigil-pi/state-transition-audit/v1"
    assert event["event"] == "pure_transition_publication"
    assert event["storage_grants"] == {"a.state": "read_write"}
    assert event["selected_timeout_ms"] == 14990 and event["selected_fuel"] == FUEL
    assert event["net"] == event["fs"] == event["secret_names"] == []
    assert event["input_sha256"] == "f" * 64 and event["output_sha256"] == "1" * 64
    assert event["writes"] == 1 and event["checks"] == 0
    assert not {"input", "output", "response", "success", "key", "secret", "authority"} & event.keys()


@pytest.mark.parametrize("index,value", [(0, "A" * 64), (1, "0" * 63), (5, "-1"),
    (5, "0100"), (6, "99"), (7, "30001"), (8, "0"), (9, "0"), (9, "15001"),
    (10, "not-a-digest"), (11, "4194305"), (12, "X" * 64), (13, "2097153"),
    (14, "bad"), (15, "0"), (16, "62"), (17, "0"), (17, "63")])
def test_invalid_or_inconsistent_observations_are_refused(mcp, audit_source, index, value):
    facts = observation()
    facts[index] = value
    response = mcp.forge(audit_source, input=record("TA1\n", facts), fuel=FUEL)
    assert response["status"] == "error"
    assert not response.get("data", {}).get("output_text")


@pytest.mark.parametrize("grants", ['{}', '{"a":"all"}', '{"a":"read","a":"read"}',
    '{"z":"read","a":"read"}', '{"a":"read",}', '{"a":"read"}extra',
    '{"bad/path":"read"}', '{"a":"read","secret":"raw-secret"}',
    '{"a":"read_write","b":3}', '{ "a":"read"}', '{"\\u0061":"read"}'])
def test_grant_projection_is_exact_canonical_bounded_and_cannot_smuggle_values(mcp, audit_source, grants):
    facts = observation()
    facts[4] = grants
    refused(mcp, audit_source, record("TA1\n", facts), 400)


def test_all_62_full_length_storage_grants_are_retained_without_truncation(mcp, audit_source):
    facts = observation()
    grants = {f"n{i:02d}" + "x" * 125: ["read", "read_write", "create_only"][i % 3] for i in range(62)}
    facts[4] = json.dumps(grants, separators=(",", ":"), sort_keys=True)
    kind, raw = project(mcp, audit_source, facts)
    assert kind == "publish" and json.loads(raw)["storage_grants"] == grants
    assert len(raw.encode()) <= 16384
    grants["z"] = "read"
    facts[4] = json.dumps(grants, separators=(",", ":"), sort_keys=True)
    refused(mcp, audit_source, record("TA1\n", facts), 400)


def test_noop_has_explicit_no_publication_policy_and_requires_empty_batch(mcp, audit_source):
    facts = observation()
    facts[14:] = [hashlib.sha256(b"").hexdigest(), "0", "0", "0"]
    assert project(mcp, audit_source, facts) == ["none", ""]
    facts[14] = "a" * 64
    refused(mcp, audit_source, record("TA1\n", facts), 409)


@pytest.mark.parametrize("manifest_value", ["", manifest() + "x",
    record("TM1\n", ["c" * 64, "d" * 64, str(FUEL), "30001"]),
    record("TM1\n", ["c" * 64, "d" * 64, str(FUEL), "15000", "grant-all"])])
def test_manifest_is_not_optional_or_an_extensible_authority_claim(mcp, audit_source, manifest_value):
    facts = observation()
    facts[2] = manifest_value
    refused(mcp, audit_source, record("TA1\n", facts), 400)


def test_trailing_frames_and_bytes_cannot_become_audit_content(mcp, audit_source):
    refused(mcp, audit_source, record("TA1\n", observation() + ["raw-secret"]), 400)
    refused(mcp, audit_source, record("TA1\n", observation()) + "raw-secret", 400)


def test_sigil_bootstrap_accepts_configured_metadata_without_fabricating_an_execution(mcp, audit_source):
    facts = observation()
    raw = record("AB1\n", [facts[1], facts[2], facts[3], facts[4]])
    assert forge_ok(mcp, audit_source, raw, fuel=FUEL) == "transaction_audit_admitted"


@pytest.mark.parametrize("index,value", [(0, "bad"), (1, ""), (2, "wrong-manifest"),
    (3, '{"a":"all"}'), (3, '{"a":"read","a":"read"}')])
def test_sigil_bootstrap_does_not_admit_malformed_audit_bindings(mcp, audit_source, index, value):
    f = observation()
    boot = [f[1], f[2], f[3], f[4]]
    boot[index] = value
    refused(mcp, audit_source, record("AB1\n", boot), 400)
