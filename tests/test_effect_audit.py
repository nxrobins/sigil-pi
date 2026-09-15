"""Compiled lifecycle-audit policy; direct facts are structural fixtures only."""
import hashlib
import json

import pytest

from api_support import credential
from conftest import PI_ROOT, SIGIL_ROOT, forge_ok
from scripts.compose_effect_audit import compose_effect_audit
from test_transaction_audit import manifest
from turn_support import FUEL, fields, record


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def grants():
    return encoded({"a.intent": "read", "executor.claim": "read_write", "executor.delivery": "create_only"})


def effect(net=None, fs=None, secrets=None):
    return record("EM1\n", ["8" * 64, "9" * 64, str(FUEL), "15000",
        encoded(["127.0.0.1:1234"] if net is None else net), encoded([] if fs is None else fs),
        encoded(["provider"] if secrets is None else secrets)])


def boot():
    facts = credential()["facts"]
    return ["b" * 64, manifest(), manifest("e" * 64), grants(), effect(), facts,
            hashlib.sha256(facts.encode()).hexdigest()]


def context():
    return record("AT1\n", ["alice", "tenant-a", "epoch-1", boot()[6]])


def observation(kind="observed", *, status="ok", output_kind="string", may="1", reaped="1", fault=""):
    generation = "c" * 64
    phase = "2"
    prepared = record("WP1\n", ["d" * 64, "123", str(FUEL), "15000", record("TG1\n", ["0", "200"])])
    worker = effect()
    claim_revision = "1"
    length, digest = "5", "f" * 64
    if kind == "prepared":
        may, reaped, status, output_kind, phase, claim_revision = "0", "1", "", "missing", "1", "0"
    elif kind == "refused":
        may, reaped, status, output_kind, fault, phase = "0", "1", "", "missing", "deadline", "3"
    elif kind == "abandoned":
        may, reaped, status, output_kind, fault, phase = "1", "0", "", "missing", "owner_unavailable", "4"
        generation, prepared, worker = "7" * 64, "", ""
    elif may == "0" and reaped == "1": phase = "3"
    elif status != "ok" or reaped != "1" or fault or output_kind != "string": phase = "4"
    if output_kind != "string": length, digest = ("1048577" if output_kind == "oversized" else "0"), ""
    wf = record("WO1\n", [kind, generation, may, reaped, fault, status, output_kind, length, digest])
    return ["a" * 64, "b" * 64, manifest(), manifest("e" * 64), worker, grants(),
        "a.intent", "executor.claim", "executor.delivery", "6" * 64 + ":1", "1", claim_revision,
        "c" * 64, generation, wf, prepared, "d" * 64, "123", "e" * 64, "200", "f" * 64, "190",
        phase, "100", "101", "250", "14990", context()]


@pytest.fixture(scope="module")
def effect_audit_source():
    return compose_effect_audit(PI_ROOT, SIGIL_ROOT).text


def project(mcp, source, values):
    kind, payload = fields(forge_ok(mcp, source, record("WA1\n", values), fuel=FUEL), "AR1\n", 2)
    assert kind == "publish"
    return json.loads(payload)


def test_actual_compilation_and_bootstrap_bind_identity_without_an_execution(mcp, effect_audit_source):
    built = compose_effect_audit(PI_ROOT, SIGIL_ROOT)
    assert len(built.text.encode()) <= 65536 and built.text.count("pub fn tool_main(") == 1
    assert built.stdlib_hash == "b5f40e2eba41b6734f8db071"
    assert built == compose_effect_audit(PI_ROOT, SIGIL_ROOT)
    assert all(not key.startswith("tests/") for key in built.input_hashes)
    result = forge_ok(mcp, effect_audit_source, record("WB1\n", boot()), fuel=FUEL)
    assert fields(result, "WB2\n", 2) == ["effect_audit_admitted", context()]
    assert "audit_id" not in result and "publish" not in result


@pytest.mark.parametrize("case", ["prepared", "returned", "runtime_error", "refused", "abandoned",
                                  "deadline", "unreaped", "not_sent", "oversized", "non_string"])
def test_observed_lifecycle_states_never_claim_business_success(mcp, effect_audit_source, case):
    variants = {
        "prepared": {"kind": "prepared"}, "returned": {}, "runtime_error": {"status": "error"},
        "refused": {"kind": "refused"}, "abandoned": {"kind": "abandoned"},
        "deadline": {"status": "", "fault": "deadline", "output_kind": "missing"},
        "unreaped": {"status": "", "fault": "cleanup_unconfirmed", "reaped": "0", "output_kind": "missing"},
        "not_sent": {"status": "", "fault": "cancelled", "may": "0", "output_kind": "missing"},
        "oversized": {"output_kind": "oversized"}, "non_string": {"output_kind": "other"},
    }
    values = observation(**variants[case])
    event = project(mcp, effect_audit_source, values)
    assert event["schema"] == "sigil-pi/worker-lifecycle-audit/v1"
    assert event["principal"] == "alice" and event["tenant"] == "tenant-a"
    assert event["operation"] == "6" * 64 and event["step"] == 1
    assert event["phase"] == int(values[22])
    assert event["claim_generation"] == "c" * 64
    assert event["observer_generation"] == values[13]
    assert event["recording_storage_grants"] == json.loads(grants())
    assert not {"success", "delivered", "input", "output", "secret_values", "receipt"} & event.keys()
    if case == "abandoned":
        assert event["effect"] is event["prepared"] is event["retained_output_sha256"] is None
        assert event["request_may_have_run"] == 1 and event["worker_reaped"] == 0
    else:
        assert event["effect"]["net"] == ["127.0.0.1:1234"]
        assert event["effect"]["secret_names"] == ["provider"]
        assert event["prepared"]["input_sha256"] == "d" * 64
    if case in {"prepared", "refused", "deadline", "unreaped", "not_sent", "oversized", "non_string"}:
        assert event["retained_output_sha256"] is None


@pytest.mark.parametrize("index,value", [(0, "forged"), (1, "B" * 64), (2, "bad"), (3, ""),
    (4, "arbitrary grants"), (5, '{"a":"all"}'), (6, "bad/path"), (7, "a.intent"),
    (8, "executor.claim"), (9, "6" * 64 + ":01"), (10, "0"), (11, "0"),
    (12, "c" * 63), (13, "5" * 64), (15, ""), (16, "raw payload"), (17, "4194305"),
    (18, ""), (19, "2097153"), (20, "wrong"), (21, "0"), (22, "3"),
    (23, "0100"), (24, "99"), (25, "30001"), (26, "15001"), (27, "caller identity")])
def test_malformed_or_inconsistent_bound_fields_are_refused(mcp, effect_audit_source, index, value):
    values = observation()
    values[index] = value
    result = mcp.forge(effect_audit_source, input=record("WA1\n", values), fuel=FUEL)
    assert result["status"] == "error" and not result.get("data", {}).get("output_text")


@pytest.mark.parametrize("index,value", [(0, "not-hash"), (1, "wrong"), (2, ""),
    (3, '{"a":"read","a":"read"}'), (4, effect(net=[123])),
    (4, effect(secrets=["bad/name"])), (5, "caller identity"), (6, "raw credential")])
def test_invalid_boot_metadata_cannot_select_audit_identity(mcp, effect_audit_source, index, value):
    values = boot()
    values[index] = value
    result = mcp.forge(effect_audit_source, input=record("WB1\n", values), fuel=FUEL)
    assert result["status"] == "error" and not result.get("data", {}).get("output_text")


def test_abandoned_recovery_cannot_invent_the_previous_worker_or_input(mcp, effect_audit_source):
    for index, value in [(4, effect()), (15, observation()[15]), (22, "2")]:
        values = observation("abandoned")
        values[index] = value
        result = mcp.forge(effect_audit_source, input=record("WA1\n", values), fuel=FUEL)
        assert result["status"] == "error"


def test_full_storage_metadata_is_retained_and_oversized_caps_are_not_truncated(mcp, effect_audit_source):
    metadata = {f"n{i:02d}" + "x" * 125: ["read", "read_write", "create_only"][i % 3] for i in range(62)}
    values = observation()
    values[5] = encoded(metadata)
    event = project(mcp, effect_audit_source, values)
    assert event["recording_storage_grants"] == metadata
    assert len(encoded(event).encode()) <= 16384
    values = boot()
    values[4] = effect(fs=["/" + "x" * 512] * 64)
    result = mcp.forge(effect_audit_source, input=record("WB1\n", values), fuel=FUEL)
    assert result["status"] == "error" and not result.get("data", {}).get("output_text")


def test_extra_frames_and_raw_output_are_not_a_signing_interface(mcp, effect_audit_source):
    for raw in [record("WA1\n", observation() + ["raw-provider-secret"]),
                record("WA1\n", observation()) + "raw-provider-secret"]:
        result = mcp.forge(effect_audit_source, input=raw, fuel=FUEL)
        assert result["status"] == "error" and not result.get("data", {}).get("output_text")
