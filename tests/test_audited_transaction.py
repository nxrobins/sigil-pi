"""Actual compiled settlement + audit projection + atomic native publication.

No HTTP readiness, complete effect audit, or pilot qualification is claimed.
"""
import hashlib
import hmac
import json
from pathlib import Path
import shutil

import pytest

from audited_transaction_support import (CHAIN, ENTRIES, HEADS, KEY, KEY_ENV, NativeAudit,
    audited_native as audited_native, inspect, retained_audit as retained_audit,
    readiness_host_binary as readiness_host_binary)
from conftest import PI_ROOT, SIGIL_ROOT, forge_ok
from scripts.compose_application import compose_application
from scripts.compose_transaction_audit import compose_transaction_audit
from store_support import NativeStore, mutation
from test_settlement import terminal_records as terminal_records
from turn_support import FUEL, fields, record


@pytest.fixture(autouse=True)
def native_key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)


def digest(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def verify_mac(raw, kind):
    signed = json.loads(raw)
    body = json.dumps(signed["body"], ensure_ascii=False, separators=(",", ":"))
    parts = [HEADS.encode(), ENTRIES.encode(), body.encode()]
    message = f"sigil-authenticated-log/{kind}/v1\0".encode()
    message += b"".join(len(p).to_bytes(8, "big") + p for p in parts)
    assert hmac.compare_digest(signed["tag"], hmac.new(KEY.encode(), message, hashlib.sha256).hexdigest())
    return signed["body"]


def test_actual_settlement_execution_and_audit_share_receipt_and_survive_restart(
        audited_native, retained_audit, mcp):
    root, row = retained_audit
    before, _, _ = inspect(audited_native[1], root, row)
    with NativeAudit(audited_native[0], root, row) as worker:
        assert worker.request({"op": "verify_audit"})["code"] == "audit_verification"
        result = worker.apply()
        assert result["status"] == "ok", result
        assert result["applied"]["context"] == ["done", "reported"]
        assert result["applied"]["receipt"]["revision"] == 3
        checkpoint = result["applied"]["checkpoint"]
        assert checkpoint["head_revision"] == checkpoint["count"] == 1
        checked = worker.request({"op": "verify_audit"})
        assert checked == {"status": "ok", "checkpoint": checkpoint, "independent_checkpoint_checked": False}
        assert worker.apply()["applied"] == {"context": ["done", "reported"], "receipt": None, "checkpoint": None}
        config = worker.config
    after, head, entries = inspect(audited_native[1], root, row, count=2)
    assert [v["revision"] for v in after] == [2, 2, 2, 2]
    assert entries[1] == {"revision": 0, "value": None}
    signed = verify_mac(entries[0]["value"], "record")
    signed_head = verify_mac(head["value"], "head")
    assert signed_head["tip"] == checkpoint["tip"] == digest(entries[0]["value"])
    assert signed_head["count"] == 1
    event = json.loads(signed["payload"])
    assert event["producer_source"] == config["worker"]["source_sha256"]
    assert event["producer_runtime"] == config["worker"]["runtime_sha256"]
    assert event["projector_source"] == config["audit"]["worker"]["source_sha256"]
    assert event["storage_grants"] == config["grants"]
    assert event["net"] == event["fs"] == event["secret_names"] == []
    assert event["selected_fuel"] == FUEL and 0 < event["selected_timeout_ms"] <= 15000
    assert event["clock_after"] >= event["clock_before"]
    assert event["writes"] == 3 and event["checks"] == 1
    sr = [record("SR1\n", ["ok", str(before[i]["revision"]), before[i]["value"]]) for i in (0, 3, 1, 2)]
    input_text = record("SF1\n", [row["facts"], "b" * 64, "a" * 64, "same-session", str(event["clock_before"]), *sr])
    output = forge_ok(mcp, compose_application("settlement", SIGIL_ROOT).text, input_text, fuel=FUEL)
    batch = fields(output, "TX1\n", 3)[2]
    for prefix, raw in [("input", input_text), ("output", output), ("batch", batch)]:
        assert event[prefix + "_sha256"] == digest(raw)
        assert event[prefix + "_bytes"] == len(raw.encode())
    assert KEY not in json.dumps((head, entries, result))
    assert "Retained answer" not in signed["payload"] and "same-session" not in signed["payload"]
    with NativeAudit(audited_native[0], root, row) as restarted:
        assert restarted.request({"op": "verify_audit"})["checkpoint"] == checkpoint
        assert restarted.apply()["applied"]["receipt"] is None
    assert inspect(audited_native[1], root, row, count=2) == (after, head, entries)


@pytest.mark.parametrize("extra", ["facts", "clock", "input_sha256", "output_sha256", "receipt", "batch", "audit", "chain", "key_env"])
def test_requests_cannot_supply_execution_facts_or_audit_targets(audited_native, retained_audit, extra):
    root, row = retained_audit
    before = inspect(audited_native[1], root, row)
    with NativeAudit(audited_native[0], root, row) as worker:
        request = {"op": "apply", "values": ["a" * 64, "same-session"], extra: "forged"}
        assert worker.request(request)["code"] == "protocol"
    assert inspect(audited_native[1], root, row) == before


@pytest.mark.parametrize("kind", ["skip", "empty", "malformed", "damaged_runtime", "capacity"])
def test_audit_refusal_never_falls_back_to_unaudited_domain_commit(audited_native, retained_audit, kind):
    root, row = retained_audit
    before = inspect(audited_native[1], root, row)
    source = compose_transaction_audit(PI_ROOT, SIGIL_ROOT).text
    if kind in {"skip", "empty", "malformed"}:
        output = {"skip": record("AR1\n", ["none", ""]), "empty": record("AR1\n", ["publish", ""]),
                  "malformed": "not-an-audit-command"}[kind]
        source = source.replace("pub fn tool_main(input_ptr: i64, input_len: i64)",
                                "fn original_audit(input_ptr: i64 @Internal, input_len: i64 @Internal)")
        source += f'\npub fn tool_main(input_ptr: i64, input_len: i64) -> i64 @Internal ! {{ Alloc }} {{ return text({json.dumps(output)}); }}'
    def patch(config):
        if kind == "capacity":
            config["audit"]["limits"]["payload_bytes"] = 64
        elif kind == "damaged_runtime":
            worker = config["audit"]["worker"]
            copy = Path(worker["source"]).parent / "private-audit-runtime"
            shutil.copy2(worker["runtime"], copy)
            worker["runtime"] = str(copy)
    with NativeAudit(audited_native[0], root, row, formatter=source, patch=patch) as worker:
        if kind == "damaged_runtime":
            Path(worker.config["audit"]["worker"]["runtime"]).write_text("changed after native admission")
        result = worker.apply()
        assert result["status"] == "error", result
        if kind in {"skip", "empty"}:
            assert result["code"] == "binding", result
        elif kind == "malformed":
            assert result["code"] == "protocol", result
        elif kind == "capacity":
            assert result["code"] == "storage", result
        elif kind == "damaged_runtime":
            assert result["code"] == "worker", result
        assert "applied" not in result
    assert inspect(audited_native[1], root, row) == before


def test_post_admission_source_change_cannot_replace_the_verified_in_memory_program(audited_native, retained_audit):
    root, row = retained_audit
    with NativeAudit(audited_native[0], root, row) as worker:
        admitted_digest = worker.config["audit"]["worker"]["source_sha256"]
        worker.formatter_path.write_text("not the admitted SIGIL program")
        assert digest(worker.formatter_path.read_text()) != admitted_digest
        assert worker.apply()["status"] == "ok"
        assert worker.request({"op": "verify_audit"})["status"] == "ok"
    _, _, entries = inspect(audited_native[1], root, row)
    event = json.loads(verify_mac(entries[0]["value"], "record")["payload"])
    assert event["projector_source"] == admitted_digest
    assert event["schema"] == "sigil-pi/state-transition-audit/v1"


@pytest.mark.parametrize("kind", ["missing_key", "short_key", "long_key", "null", "legacy_optin", "missing_optin", "overlap", "same_namespace", "invalid_chain", "guest_secret", "guest_network", "wrong_hash", "scope_limit"])
def test_audit_bootstrap_is_explicit_bounded_scoped_and_grantless(audited_native, retained_audit, monkeypatch, kind):
    root, row = retained_audit
    before = inspect(audited_native[1], root, row)
    if kind == "missing_key":
        monkeypatch.delenv(KEY_ENV)
    if kind == "short_key":
        monkeypatch.setenv(KEY_ENV, "x" * 31)
    if kind == "long_key":
        monkeypatch.setenv(KEY_ENV, "x" * 1025)
    def patch(c):
        if kind == "null": c["audit"] = None
        elif kind == "legacy_optin": c["version"] = 1
        elif kind == "missing_optin": del c["audit"]
        elif kind == "overlap": c["audit"]["heads"] = "a.state"
        elif kind == "same_namespace": c["audit"]["heads"] = ENTRIES
        elif kind == "invalid_chain": c["audit"]["chain"] = "not-a-chain"
        elif kind == "guest_secret": c["audit"]["worker"]["secret_env"] = {"key": KEY_ENV}
        elif kind == "guest_network": c["audit"]["worker"]["net"] = ["https://example.invalid"]
        elif kind == "wrong_hash": c["audit"]["worker"]["source_sha256"] = "0" * 64
        elif kind == "scope_limit": c["grants"].update({f"unused{i}": "read" for i in range(59)})
    with pytest.raises(AssertionError, match="config|limit"):
        NativeAudit(audited_native[0], root, row, patch=patch)
    assert inspect(audited_native[1], root, row) == before


def test_missing_or_corrupt_audit_is_not_verified_or_repaired(audited_native, retained_audit):
    root, row = retained_audit
    with NativeAudit(audited_native[0], root, row) as worker:
        assert worker.apply()["status"] == "ok"
    with NativeStore(audited_native[1], root, {ENTRIES: "read_write"}) as store:
        store.commit([mutation(ENTRIES, CHAIN + ".0000000000000000", "tampered", 1)])
    before = inspect(audited_native[1], root, row)
    with NativeAudit(audited_native[0], root, row) as worker:
        assert worker.request({"op": "verify_audit"})["code"] == "audit_verification"
    assert inspect(audited_native[1], root, row) == before
