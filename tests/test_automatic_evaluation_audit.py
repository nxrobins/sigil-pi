"""Real HTTP, SIGIL dispatch failure, native journal and pre-state admission."""
import json
from pathlib import Path
import select
import shutil
import time

import pytest

from automatic_audit_support import AutomaticAuditApi, events
from api_support import TOKEN_A, TOKEN_B, credential
from automatic_support import wait_operation
from http_service_support import effect_counts
from readiness_host_support import readiness_host_binary as readiness_host_binary
from test_automatic_audit import audit_secrets as audit_secrets
from test_automatic_effect_audit import effect_secrets as effect_secrets
from test_evaluation_audit_publication import digest, enable, fixed_output
from test_turn_execution import programs as programs
from turn_support import fields, record, text_reply, tool_reply


def test_v2_normal_tool_turn_followup_two_tenants_and_restart_keep_original_success_semantics(
        readiness_host_binary, programs, scripted_llm, tmp_path):
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    workspaces = [tmp_path / "alice", tmp_path / "bob"]
    for workspace in workspaces:
        workspace.mkdir()
    (workspaces[0] / "README.md").write_text("Private project owner: Ada.\n")
    usage = {"input_tokens": 1, "output_tokens": 1}
    scripted_llm.script = [json.loads(tool_reply("read_file", usage=usage)),
                          json.loads(text_reply("Ada owns it.", usage=usage)),
                          json.loads(text_reply("Bob's separate answer.", usage=usage)),
                          json.loads(text_reply("Still Ada.", usage=usage))]
    root = tmp_path / "service"
    def patch(c):
        for participant in c["automatic"]["participants"]:
            for role in ["transaction_audit", "effect_audit"]:
                enable(participant[role], root)
    with AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspaces,
                          rows=rows, audit_effects=True, patch=patch) as api:
        assert api.request(token="invalid")[0] == 401
        assert scripted_llm.requests == []
        accepted, results = [], []
        for index, token in enumerate([TOKEN_A, TOKEN_B]):
            assert events(root, api.config, index, effects=True) == events(root, api.config, index) == []
            code, submission = api.request(token=token)
            assert code == 202
            answer = wait_operation(api, submission["operation"], token=token)
            assert answer["status"] == "done"
            assert answer["reply"] == ["Ada owns it.", "Bob's separate answer."][index]
            assert api.request("GET", submission["status_url"], token=[TOKEN_B, TOKEN_A][index])[0] == 404
            lifecycle = events(root, api.config, index, effects=True)
            transitions = events(root, api.config, index)
            count = [3, 1][index]
            assert [e["event"] for e in lifecycle] == ["prepared", "observed"] * count
            assert len(transitions) == count + 1
            assert {e["schema"] for e in lifecycle} == {"sigil-pi/worker-lifecycle-audit/v1"}
            assert {e["schema"] for e in transitions} == {"sigil-pi/state-transition-audit/v1"}
            assert {e["operation"] for e in lifecycle} == {submission["operation"]}
            assert {e["tenant"] for e in lifecycle} == {"tenant-a" if index == 0 else "tenant-b"}
            for prepared, observed in zip(lifecycle[::2], lifecycle[1::2]):
                assert prepared["dispatch_policy"] == observed["dispatch_policy"]
                assert prepared["claim_generation"] == observed["claim_generation"]
                assert observed["request_may_have_run"] == observed["worker_reaped"] == 1
            assert api.request(token=token)[1]["replayed"] is True
            assert events(root, api.config, index, effects=True) == lifecycle
            assert events(root, api.config, index) == transitions
            accepted.append(submission)
            results.append(answer)
        code, following = api.request(body={"session": "same-session", "message": "Who owns it?", "submission_key": "followup"})
        assert code == 202
        continued = wait_operation(api, following["operation"])
        assert continued["reply"] == "Still Ada."
        saved = [(events(root, api.config, i), events(root, api.config, i, effects=True)) for i in range(2)]
        assert [len(v) for v in saved[0]] == [6, 8]
        assert [len(v) for v in saved[1]] == [2, 2]
        assert "Ada" not in json.dumps(saved) and "same-session" not in json.dumps(saved)
    assert len(scripted_llm.requests) == 4
    assert scripted_llm.requests[1]["messages"][-1]["content"][0]["content"] == "Private project owner: Ada.\n"
    with AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspaces,
                          rows=rows, audit_effects=True, patch=patch, mode="open") as api:
        for index, token in enumerate([TOKEN_A, TOKEN_B]):
            assert api.request("GET", accepted[index]["status_url"], token=token)[1] == results[index]
            assert api.request(token=token)[1]["replayed"] is True
            assert events(root, api.config, index) == saved[index][0]
            assert events(root, api.config, index, effects=True) == saved[index][1]
        assert api.request("GET", following["status_url"])[1] == continued
    assert len(scripted_llm.requests) == 4


def test_v2_background_failure_reporter_refusal_is_visible_without_claim_or_send(
        readiness_host_binary, programs, scripted_llm, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    def patch(c):
        participant = c["automatic"]["participants"][0]
        audit = participant["effect_audit"]
        enable(audit, root)
        audit["limits"]["payload_bytes"] = 64
        source = fixed_output()
        path = root / "refused-dispatch.sigil"
        path.write_text(source)
        participant["effects"]["provider"]["policy"]["worker"].update(source=str(path), source_sha256=digest(source))
    with AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace,
                          audit_effects=True, patch=patch) as api:
        assert api.request()[0] == 202
        readable, _, _ = select.select([api.proc.stderr], [], [], 15)
        assert readable, "missing actual background refusal diagnostic"
        diagnostic = json.loads(api.proc.stderr.readline())
        assert diagnostic == {"event": "coordinator_refused", "code": "audit_recording"}
        assert events(root, api.config, 0, effects=True) == events(root, api.config, 0) == []
        assert effect_counts(root) == {}
        assert scripted_llm.requests == []


@pytest.mark.parametrize("kind", ["runtime_error", "malformed", "binding"])
def test_actual_dispatch_failure_has_a_redacted_event_but_no_claim_or_effect(
        readiness_host_binary, programs, scripted_llm, tmp_path, kind):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    def patch(c):
        participant = c["automatic"]["participants"][0]
        enable(participant["effect_audit"], root)
        policy = participant["effects"]["provider"]["policy"]
        output = {"runtime_error": None, "malformed": "private-output",
                  "binding": record("DW1\n", ["wrong_alias", "", "a.intent", "none", "1", "", ""])}[kind]
        source = fixed_output(output)
        path = root / "refused-dispatch.sigil"
        path.write_text(source)
        policy["worker"].update(source=str(path), source_sha256=digest(source))
    with AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace,
                          audit_effects=True, patch=patch) as api:
        assert api.request()[0] == 202
        end = time.monotonic() + 15
        audit = []
        while time.monotonic() < end:
            audit = events(root, api.config, 0, effects=True)
            if audit:
                break
            time.sleep(0.05)
        assert audit, "actual dispatch refusal never published"
        policy = api.config["automatic"]["participants"][0]["effects"]["provider"]["policy"]
        assert effect_counts(root) == {}
        assert scripted_llm.requests == []
        assert events(root, api.config, 0) == []
        for event in audit:
            assert event["schema"] == "sigil-pi/evaluation-failure-audit/v1"
            assert event["event"] == ("pure_evaluation_failed" if kind == "runtime_error" else "pure_proposal_refused")
            assert event["mechanical_refusal"] == {"runtime_error": "application", "malformed": "protocol", "binding": "binding"}[kind]
            assert event["evaluation"]["admitted_source_sha256"] == policy["worker"]["source_sha256"]
            assert event["subject_storage_grants"] == policy["read_grants"]
            assert not {"authorized", "claim", "receipt", "delivered", "principal", "operation"} & event.keys()
            assert "private-output" not in json.dumps(event)


@pytest.mark.parametrize("role", ["effect_audit", "transaction_audit"])
@pytest.mark.parametrize("kind", ["null", "v1_optin", "network", "secret", "wrong_hash", "invalid_source", "wrong_boot"])
def test_evaluation_admission_fails_before_state_creation_or_effects(
        readiness_host_binary, programs, scripted_llm, tmp_path, role, kind):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    def patch(c):
        audit = c["automatic"]["participants"][0][role]
        enable(audit, root)
        worker = audit["evaluation"]
        if kind == "null": audit["evaluation"] = None
        elif kind == "v1_optin": audit["version"] = 1
        elif kind == "network": worker["net"] = ["127.0.0.1"]
        elif kind == "secret": worker["secret_env"] = {"audit": audit["key_env"]}
        elif kind == "wrong_hash": worker["source_sha256"] = "0" * 64
        else:
            path = Path(worker["source"])
            source = "invalid SIGIL" if kind == "invalid_source" else path.read_text().replace(
                '"evaluation_audit_admitted"', '"wrong_boot"')
            path.write_text(source)
            worker["source_sha256"] = digest(source)
    with pytest.raises(AssertionError, match="config|application|worker|deadline"):
        AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace,
                          audit_effects=True, patch=patch)
    assert not (root / "records").exists()
    assert scripted_llm.requests == []


@pytest.mark.parametrize("role", ["effect_audit", "transaction_audit"])
def test_optional_projector_is_in_real_read_only_artifact_inspection(
        readiness_host_binary, programs, scripted_llm, tmp_path, role):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    def patch(c):
        audit = c["automatic"]["participants"][0][role]
        enable(audit, root)
        worker = audit["evaluation"]
        runtime = root / "private-evaluation-runtime"
        shutil.copy2(worker["runtime"], runtime)
        worker["runtime"] = str(runtime)
    with AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace,
                          audit_effects=True, patch=patch, diagnostic=True) as api:
        def inspected():
            code, _, raw = api.exchange("GET", "/v1/ready")
            assert code == 218  # Explicit diagnostic, not a healthy-product assertion.
            observation = fields(json.loads(raw)["diagnostic"], "AC1\n", 4)[1]
            return fields(fields(observation, "EI1\n", 2)[1], "RI1\n", 3)
        assert inspected() == ["ok", "", "0"]
        replacement = root / "changed-evaluation-runtime"
        replacement.write_text("not admitted")
        replacement.chmod(0o700)
        replacement.replace(root / "private-evaluation-runtime")
        assert inspected() == ["error", "artifact_invalid", ""]
        assert events(root, api.config, 0) == events(root, api.config, 0, effects=True) == []
        assert scripted_llm.requests == []
