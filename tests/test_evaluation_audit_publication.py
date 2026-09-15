"""Actual compiled SIGIL producer/classifier + native authenticated failure append."""
import hashlib
import json
from pathlib import Path
import select

import pytest

from audited_transaction_support import (NativeAudit, audited_native as audited_native,
    inspect, retained_audit as retained_audit, readiness_host_binary as readiness_host_binary)
from conftest import PI_ROOT, SIGIL_ROOT
from scripts.compose_evaluation_audit import compose_evaluation_audit
from scripts.compose_transaction_audit import compose_transaction_audit
from test_audited_transaction import native_key as native_key, verify_mac
from test_settlement import terminal_records as terminal_records
from turn_support import record


def digest(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def fixed_output(output=None):
    source = compose_transaction_audit(PI_ROOT, SIGIL_ROOT).text
    source = source.replace("pub fn tool_main(input_ptr: i64, input_len: i64)",
                            "fn unused_original(input_ptr: i64 @Internal, input_len: i64 @Internal)")
    result = "-403" if output is None else f"text({json.dumps(output)})"
    return source + f'\npub fn tool_main(input_ptr: i64, input_len: i64) -> i64 @Internal ! {{ Alloc }} {{ return {result}; }}'


def enable(audit, directory, source=None):
    source = compose_evaluation_audit(PI_ROOT, SIGIL_ROOT).text if source is None else source
    path = directory / "evaluation-audit.sigil"
    path.write_text(source)
    audit["version"] = 2
    audit["evaluation"] = {**audit["worker"], "source": str(path), "source_sha256": digest(source)}


def patch_fixture(config, *, mode="enabled"):
    # Fixed, bounded actual input makes its native digest independently checkable.
    config["inputs"] = [{"kind": "value", "index": 0}]
    config["grants"] = {"a.state": "read"}
    if mode != "legacy":
        enable(config["audit"], Path(config["worker"]["source"]).parent)


@pytest.mark.parametrize("kind", ["runtime_error", "malformed", "binding"])
def test_failure_is_signed_redacted_and_restart_verifiable_without_domain_commit(
        audited_native, retained_audit, kind):
    root, row = retained_audit
    before = inspect(audited_native[1], root, row)[0]
    output = {"runtime_error": None, "malformed": "private-output",
              "binding": record("TX1\n", ["", "", '{"op":"commit","checks":[],"writes":[]}'])}[kind]
    expected = {"runtime_error": "application", "malformed": "protocol", "binding": "binding"}[kind]
    producer = fixed_output(output)
    with NativeAudit(audited_native[0], root, row, producer=producer, patch=patch_fixture) as worker:
        assert worker.apply() == {"status": "error", "code": expected}
        checked = worker.request({"op": "verify_audit"})
        assert checked["status"] == "ok" and checked["checkpoint"]["count"] == 1
        # Invalid lookup input cannot replay the previously consumed observation.
        assert worker.request({"op": "apply", "values": []})["code"] == "protocol"
        assert worker.request({"op": "verify_audit"})["checkpoint"] == checked["checkpoint"]
        config = worker.config
    after, head, entries = inspect(audited_native[1], root, row, count=2)
    assert after == before
    assert head["revision"] == entries[0]["revision"] == 1 and entries[1]["revision"] == 0
    event = json.loads(verify_mac(entries[0]["value"], "record")["payload"])
    observation = event["evaluation"]
    assert event["schema"] == "sigil-pi/evaluation-failure-audit/v1"
    assert event["event"] == ("pure_evaluation_failed" if kind == "runtime_error" else "pure_proposal_refused")
    assert event["mechanical_refusal"] == expected
    assert event["subject_storage_grants"] == config["grants"]
    assert event["projector_source"] == config["audit"]["evaluation"]["source_sha256"]
    assert observation["admitted_source_sha256"] == digest(producer)
    assert observation["admitted_runtime_sha256"] == config["worker"]["runtime_sha256"]
    assert observation["input"]["sha256"] == digest(record("SF1\n", ["a" * 64]))
    assert event["lookup_values_sha256"] == digest(record("EL1\n", ["a" * 64, "same-session"]))
    assert event["lookup_count"] == 2
    assert observation["request_may_have_run"] is True and observation["worker_reaped"] is True
    assert observation["parsed_result"]["representation"] == "serde-json-value/v1"
    if output is not None:
        assert observation["output"]["sha256"] == digest(output)
        assert observation["error"] is None
    else:
        assert observation["runtime_status"] == "error" and observation["error"] == "application"
    assert "private-output" not in json.dumps(event) and "same-session" not in json.dumps(event)
    assert not {"claim", "receipt", "delivered", "authorized", "principal", "operation"} & event.keys()
    with NativeAudit(audited_native[0], root, row, producer=producer, patch=patch_fixture) as worker:
        assert worker.request({"op": "verify_audit"})["checkpoint"] == checked["checkpoint"]
    assert inspect(audited_native[1], root, row, count=2) == (after, head, entries)


def test_legacy_failure_keeps_original_no_append_behavior(audited_native, retained_audit):
    root, row = retained_audit
    before = inspect(audited_native[1], root, row)
    with NativeAudit(audited_native[0], root, row, producer=fixed_output(),
                     patch=lambda c: patch_fixture(c, mode="legacy")) as worker:
        assert worker.apply() == {"status": "error", "code": "application"}
    assert inspect(audited_native[1], root, row) == before


@pytest.mark.parametrize("kind", ["capacity", "skip", "malformed", "classifier_error"])
def test_failed_failure_publication_is_visible_and_never_a_domain_or_audit_receipt(
        audited_native, retained_audit, kind):
    root, row = retained_audit
    before = inspect(audited_native[1], root, row)
    def patch(c):
        patch_fixture(c)
        if kind == "capacity":
            c["audit"]["limits"]["payload_bytes"] = 64
        else:
            source = compose_evaluation_audit(PI_ROOT, SIGIL_ROOT).text
            anchor = 'return write_record(result, "AR1\\n", 2);'
            assert source.count(anchor) == 1
            replacement = {"skip": 'return text("AR1\\n00000004none00000000");',
                           "malformed": 'return text("invalid");', "classifier_error": "return -403;"}[kind]
            enable(c["audit"], Path(c["worker"]["source"]).parent, source.replace(anchor, replacement))
    with NativeAudit(audited_native[0], root, row, producer=fixed_output(), patch=patch) as worker:
        assert worker.apply() == {"status": "error", "code": "audit_recording"}
    assert inspect(audited_native[1], root, row) == before


def test_lost_failure_acknowledgement_survives_restart_without_replaying_its_observation(
        audited_native, retained_audit):
    root, row = retained_audit
    before = inspect(audited_native[1], root, row)[0]
    producer = fixed_output()
    with NativeAudit(audited_native[0], root, row, producer=producer, patch=patch_fixture) as worker:
        worker.proc.stdin.write(json.dumps({"op": "apply", "values": ["a" * 64, "same-session"]}) + "\n")
        worker.proc.stdin.flush()
        readable, _, _ = select.select([worker.proc.stdout], [], [], 30)
        assert readable, "failure result never became available"
        # A native result is available but not consumed. This is after-publication
        # acknowledgement loss, NOT injected in-commit uncertainty or disk failure.
        worker.proc.kill()
        worker.proc.wait(timeout=5)
    saved = inspect(audited_native[1], root, row, count=2)
    assert saved[0] == before and saved[1]["revision"] == 1
    assert [entry["revision"] for entry in saved[2]] == [1, 0]
    first = json.loads(verify_mac(saved[2][0]["value"], "record")["payload"])
    with NativeAudit(audited_native[0], root, row, producer=producer, patch=patch_fixture) as worker:
        assert worker.request({"op": "verify_audit"})["checkpoint"]["count"] == 1
        assert worker.request({"op": "apply", "values": []})["code"] == "protocol"
        assert worker.request({"op": "verify_audit"})["checkpoint"]["count"] == 1
        # A new actual evaluation is a new observation, not a replayed old fact.
        assert worker.apply() == {"status": "error", "code": "application"}
        assert worker.request({"op": "verify_audit"})["checkpoint"]["count"] == 2
    after, head, entries = inspect(audited_native[1], root, row, count=3)
    assert after == before and head["revision"] == 2
    assert entries[0] == saved[2][0] and entries[2]["revision"] == 0
    second = json.loads(verify_mac(entries[1]["value"], "record")["payload"])
    assert first["audit_id"] != second["audit_id"]
    assert first["lookup_values_sha256"] == second["lookup_values_sha256"]
    assert first["event"] == second["event"] == "pure_evaluation_failed"
