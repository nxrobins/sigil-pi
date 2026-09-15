"""Actual SIGIL recorder failures before claim, after send and during recovery."""
import json
from pathlib import Path
import select
import time

import pytest

from automatic_audit_support import AutomaticAuditApi, events
from conftest import API_KEY
from readiness_host_support import readiness_host_binary as readiness_host_binary
from test_automatic_audit import audit_secrets as audit_secrets
from test_automatic_effect_audit import effect_secrets as effect_secrets
from test_automatic_preclaim import retained
from test_evaluation_audit_publication import digest, enable
from test_turn_execution import programs as programs
from turn_support import fields, record, text_reply


def install_failure(config, root, *, phase, kind, enabled=True, reporter_error=False):
    participant = config["automatic"]["participants"][0]
    audit = participant["effect_audit"]
    if enabled:
        enable(audit, root)
        if reporter_error:
            path = Path(audit["evaluation"]["source"])
            source = path.read_text()
            anchor = 'return write_record(result, "AR1\\n", 2);'
            assert source.count(anchor) == 1
            source = source.replace(anchor, "return -403;")
            path.write_text(source)
            audit["evaluation"]["source_sha256"] = digest(source)
    worker = participant["effects"]["provider"]["recorder"]["worker"]
    source = Path(worker["source"]).read_text()
    anchor = "pub fn tool_main(input_ptr: i64, input_len: i64)"
    assert source.count(anchor) == 1
    source = source.replace(anchor,
        "fn retained_recorder(input_ptr: i64 @Internal, input_len: i64 @Internal)")
    # Keep real claim production for the post-send case. Also refuse abandoned
    # recovery so a failed recorder cannot silently turn its lost result into a
    # successful receipt in the same process or after restart.
    predicate = ('is_text(get(f, 0), "prepared")' if phase == "prepared" else
                 'is_text(get(f, 0), "observed") || is_text(get(f, 0), "abandoned")')
    refusal = '-403' if kind == "runtime_error" else 'text("private malformed recorder output")'
    source += f'''
pub fn tool_main(input_ptr: i64, input_len: i64) -> i64 @Internal ! {{ Alloc }} {{
    if input_len > 4194304 || !valid_utf8(input_ptr, input_len) {{ return -400; }}
    let x: i64 @Internal = read_record(slice(input_ptr, input_len), "WR1\\n", 10);
    if x < 0 {{ return x; }}
    let f: i64 @Internal = read_record(get(x, 9), "WF1\\n", 9);
    if f < 0 {{ return f; }}
    if {predicate} {{ return {refusal}; }}
    return retained_recorder(input_ptr, input_len);
}}
'''
    assert len(source.encode()) <= 65536 and source.count("pub fn tool_main(") == 1
    path = root / "failure-recorder.sigil"
    path.write_text(source)
    worker.update(source=str(path), source_sha256=digest(source))


def wait_failure(root, config, *, after=0, api=None):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        observed = events(root, config, 0, effects=True)
        failures = [event for event in observed
                    if event["schema"] == "sigil-pi/evaluation-failure-audit/v1"]
        if len(failures) > after:
            return observed, failures
        time.sleep(0.05)
    diagnostic = None
    if api is not None:
        readable, _, _ = select.select([api.proc.stderr], [], [], 0)
        if readable:
            diagnostic = api.proc.stderr.readline().strip()
    pytest.fail("actual recorder failure never reached the authenticated journal; "
                f"native diagnostic={diagnostic!r}; "
                f"claims={len(retained(root, 'executor0.claim'))}; "
                f"deliveries={len(retained(root, 'executor0.delivery'))}")


def assert_failure(event, config, accepted, kind):
    effect = config["automatic"]["participants"][0]["effects"]["provider"]
    worker = effect["recorder"]["worker"]
    assert event["event"] == ("pure_evaluation_failed" if kind == "runtime_error" else "pure_proposal_refused")
    assert event["mechanical_refusal"] == ("application" if kind == "runtime_error" else "protocol")
    assert event["subject_storage_grants"] == effect["grants"]
    assert event["evaluation"]["admitted_source_sha256"] == worker["source_sha256"]
    assert event["evaluation"]["admitted_runtime_sha256"] == worker["runtime_sha256"]
    assert event["evaluation"]["request_may_have_run"] is True
    assert event["evaluation"]["worker_reaped"] is True
    assert event["lookup_count"] == 4
    assert event["lookup_values_sha256"] == digest(record("EL1\n", [
        "a.intent", "executor0.claim", "executor0.delivery", accepted["operation"] + ":1"]))
    assert not {"claim", "receipt", "authorized", "delivered", "principal", "operation"} & event.keys()
    raw = json.dumps(event)
    assert all(value not in raw for value in [API_KEY, "private malformed recorder output", "same-session"])


@pytest.mark.parametrize("kind", ["runtime_error", "malformed"])
def test_failed_claim_recorder_is_journaled_without_a_claim_or_provider_send(
        readiness_host_binary, programs, scripted_llm, tmp_path, kind):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    def patch(config):
        install_failure(config, root, phase="prepared", kind=kind)
    with AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace,
                          audit_effects=True, patch=patch) as api:
        code, accepted = api.request()
        assert code == 202
        observed, failures = wait_failure(root, api.config, api=api)
        assert observed == failures
        for event in failures:
            assert_failure(event, api.config, accepted, kind)
        assert retained(root, "executor0.claim") == retained(root, "executor0.delivery") == []
        assert events(root, api.config, 0) == []
        assert scripted_llm.requests == []


@pytest.mark.parametrize("kind", ["runtime_error", "malformed"])
def test_failed_result_recorder_and_recovery_preserve_consumed_claim_without_replaying_effect(
        readiness_host_binary, programs, scripted_llm, tmp_path, kind):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    scripted_llm.script = [json.loads(text_reply("private model response", usage={"input_tokens": 1, "output_tokens": 1}))]
    def patch(config):
        install_failure(config, root, phase="observed", kind=kind)
    with AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace,
                          audit_effects=True, patch=patch) as api:
        code, accepted = api.request()
        assert code == 202
        observed, failures = wait_failure(root, api.config, api=api)
        assert observed[0]["event"] == "prepared"
        assert all(event["schema"] == "sigil-pi/evaluation-failure-audit/v1" for event in observed[1:])
        for event in failures:
            assert_failure(event, api.config, accepted, kind)
        claim = retained(root, "executor0.claim")
        assert len(claim) == 1 and claim[0][:2] == (accepted["operation"] + ":1", 1)
        assert fields(claim[0][2], "SD1\n", 5)[4] == "1"
        assert retained(root, "executor0.delivery") == []
        assert len(scripted_llm.requests) == 1
        config = api.config
    saved = events(root, config, 0, effects=True)
    count = sum(event["schema"] == "sigil-pi/evaluation-failure-audit/v1" for event in saved)
    with AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace,
                          audit_effects=True, patch=patch, mode="open") as api:
        observed, failures = wait_failure(root, api.config, after=count, api=api)
        assert observed[:len(saved)] == saved
        assert len({event["audit_id"] for event in failures}) == len(failures)
        assert_failure(failures[-1], api.config, accepted, kind)
        assert retained(root, "executor0.claim") == claim
        assert retained(root, "executor0.delivery") == []
        assert api.request()[1]["replayed"] is True
    assert len(scripted_llm.requests) == 1
    assert "private model response" not in json.dumps(observed)


@pytest.mark.parametrize("phase", ["prepared", "observed"])
def test_recorder_failure_reporter_failure_is_visible_and_never_invents_delivery(
        readiness_host_binary, programs, scripted_llm, tmp_path, phase):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    scripted_llm.script = [json.loads(text_reply("not a durable receipt", usage={"input_tokens": 1, "output_tokens": 1}))]
    def patch(config):
        install_failure(config, root, phase=phase, kind="runtime_error", reporter_error=True)
    with AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace,
                          audit_effects=True, patch=patch) as api:
        assert api.request()[0] == 202
        readable, _, _ = select.select([api.proc.stderr], [], [], 15)
        assert readable, "missing actual reporting refusal diagnostic"
        assert json.loads(api.proc.stderr.readline()) == {"event": "coordinator_refused", "code": "audit_recording"}
        observed = events(root, api.config, 0, effects=True)
        assert [event["event"] for event in observed] == ([] if phase == "prepared" else ["prepared"])
        assert retained(root, "executor0.delivery") == []
        assert len(retained(root, "executor0.claim")) == (0 if phase == "prepared" else 1)
        assert len(scripted_llm.requests) == (0 if phase == "prepared" else 1)


@pytest.mark.parametrize("kind", ["runtime_error", "malformed"])
def test_legacy_recorder_failure_keeps_original_no_failure_append_semantics(
        readiness_host_binary, programs, scripted_llm, tmp_path, kind):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    def patch(config):
        install_failure(config, root, phase="prepared", kind=kind, enabled=False)
    with AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace,
                          audit_effects=True, patch=patch) as api:
        assert api.request()[0] == 202
        readable, _, _ = select.select([api.proc.stderr], [], [], 15)
        assert readable
        assert json.loads(api.proc.stderr.readline()) == {"event": "coordinator_refused", "code": "application" if kind == "runtime_error" else "protocol"}
        assert events(root, api.config, 0, effects=True) == []
        assert retained(root, "executor0.claim") == retained(root, "executor0.delivery") == []
        assert scripted_llm.requests == []
