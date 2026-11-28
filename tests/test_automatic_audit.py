"""Actual HTTP/automatic SIGIL transitions, not complete effect-audit coverage."""
import json
from pathlib import Path
import shutil

import pytest

from api_support import TOKEN_A, TOKEN_B, credential
from automatic_audit_support import AutomaticAuditApi, audit_key, events
from automatic_support import wait_operation
from conftest import API_KEY
from http_service_support import state
from readiness_host_support import readiness_host_binary as readiness_host_binary
from readiness_host_support import ROOT as STAGE
from store_support import NativeStore, mutation
from test_turn_execution import hanging_provider, programs as programs
from turn_support import fields, text_reply, tool_reply


@pytest.fixture(autouse=True)
def audit_secrets(monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    for index in range(2):
        monkeypatch.setenv(f"PI_AUTOMATIC_AUDIT_FIXTURE_KEY_{index}", audit_key(index))


def test_actual_http_tool_turn_and_followup_publish_audit_without_a_step_driver(
        readiness_host_binary, programs, scripted_llm, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("The project owner is Ada.\n")
    usage = {"input_tokens": 2, "output_tokens": 1}
    scripted_llm.script = [json.loads(tool_reply("read_file", usage=usage)),
        json.loads(text_reply("Ada owns the project.", usage=usage)),
        json.loads(text_reply("Yes, Ada.", usage=usage))]
    root = tmp_path / "service"
    with AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace) as api:
        # AB1 must not fabricate a boot execution, receipt or signed log entry.
        assert events(root, api.config, 0) == []
        code, accepted = api.request()
        assert code == 202, accepted
        result = wait_operation(api, accepted["operation"])
        assert result["status"] == "done" and result["reply"] == "Ada owns the project."
        assert result["usage"] == {"input_tokens": 4, "output_tokens": 2, "known": True}
        assert result["accounting"] == "reported"
        first = events(root, api.config, 0)
        assert len(first) == 4
        aliases = api.config["automatic"]["participants"][0]["transactions"]
        assert [e["producer_source"] for e in first] == [aliases["interpret"]["worker"]["source_sha256"]] * 3 + [aliases["settle"]["worker"]["source_sha256"]]
        for event in first:
            assert event["schema"] == "sigil-pi/state-transition-audit/v1"
            assert event["event"] == "pure_transition_publication"
            assert event["net"] == event["fs"] == event["secret_names"] == []
            alias = "settle" if event is first[-1] else "interpret"
            assert event["storage_grants"] == aliases[alias]["grants"]
        assert len({e["execution_id"] for e in first}) == 4
        assert api.request()[1]["replayed"]
        assert events(root, api.config, 0) == first
        code, following = api.request(body={"session": "same-session", "message": "Who owns it?", "submission_key": "followup"})
        assert code == 202
        answer = wait_operation(api, following["operation"])
        assert answer["status"] == "done" and answer["reply"] == "Yes, Ada."
        final = events(root, api.config, 0)
        assert len(final) == 6 and final[:4] == first
        config = api.config
    assert len(scripted_llm.requests) == 3
    assert scripted_llm.requests[1]["messages"][-1]["content"][0]["content"] == "The project owner is Ada.\n"
    text = json.dumps(final) + json.dumps(scripted_llm.requests) + json.dumps(result)
    assert API_KEY not in text and audit_key(0) not in text
    assert "Ada" not in json.dumps(final) and "same-session" not in json.dumps(final)
    assert events(root, config, 0) == final
    with AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace, mode="open") as restarted:
        assert restarted.request("GET", accepted["status_url"])[1] == result
        assert restarted.request()[1]["replayed"]
        assert events(root, restarted.config, 0) == final
    assert len(scripted_llm.requests) == 3


def test_two_tenants_keep_identical_session_names_and_audit_authority_separate(
        readiness_host_binary, programs, scripted_llm, tmp_path):
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    areas = [tmp_path / "alice", tmp_path / "bob"]
    for area in areas:
        area.mkdir()
    root = tmp_path / "service"
    with AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, areas, rows=rows) as api:
        assert api.request(token="unknown")[0] == 401
        for index, token in enumerate([TOKEN_A, TOKEN_B]):
            scripted_llm.script.append(json.loads(text_reply(f"tenant-private-answer-{index}", usage={"input_tokens": 1, "output_tokens": 1})))
            code, accepted = api.request(token=token)
            assert code == 202
            assert api.request("GET", accepted["status_url"], token=TOKEN_B if index == 0 else TOKEN_A)[0] == 404
            done = wait_operation(api, accepted["operation"], token=token)
            assert done["reply"] == f"tenant-private-answer-{index}"
        for index in range(2):
            audit = events(root, api.config, index)
            assert len(audit) == 2
            assert all(e["chain"] == api.config["automatic"]["participants"][index]["transaction_audit"]["chain"] for e in audit)
            opposite = "b." if index == 0 else "a."
            assert all(not any(ns.startswith(opposite) or ns.startswith("audit") for ns in e["storage_grants"]) for e in audit)
            assert all(not any(ns.startswith(f"executor{1-index}.") for ns in e["storage_grants"]) for e in audit)
            assert "tenant-private-answer" not in json.dumps(audit)
            assert audit_key(1-index) not in json.dumps(audit)
    assert len(scripted_llm.requests) == 2


def test_actual_restart_after_send_preserves_uncertainty_with_audited_state_transitions(
        readiness_host_binary, programs, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    with hanging_provider() as (endpoint, arrived, requests):
        with AutomaticAuditApi(readiness_host_binary, root, programs, endpoint, workspace) as api:
            code, accepted = api.request()
            assert code == 202
            assert arrived.wait(15), "actual local provider send required"
            assert events(root, api.config, 0) == []  # No transition/result invented for a sent effect.
        with AutomaticAuditApi(readiness_host_binary, root, programs, endpoint, workspace, mode="open") as api:
            result = wait_operation(api, accepted["operation"])
            assert result["status"] == "uncertain" and result["usage"]["known"] is False
            assert result["accounting"] == "unknown"
            audit = events(root, api.config, 0)
            assert len(audit) == 2
            assert all(e["event"] == "pure_transition_publication" for e in audit)
            assert not any("effect_success" in e or "provider_response" in e for e in audit)
        assert len(requests) == 1


def test_audit_runtime_is_in_actual_read_only_runtime_inspection(
        readiness_host_binary, programs, scripted_llm, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    def patch(c):
        worker = c["automatic"]["participants"][0]["transaction_audit"]["worker"]
        copy = root / "private-audit-runtime"
        shutil.copy2(worker["runtime"], copy)
        worker["runtime"] = str(copy)
    with AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace, patch=patch, diagnostic=True) as api:
        def inspected():
            code, _, raw = api.exchange("GET", "/v1/ready")
            assert code == 218  # Explicit diagnostic, not product readiness.
            observation = fields(json.loads(raw)["diagnostic"], "AC1\n", 4)[1]
            return fields(fields(observation, "EI1\n", 2)[1], "RI1\n", 3)
        assert inspected() == ["ok", "", "0"]
        before = state(root)
        runtime = Path(api.config["automatic"]["participants"][0]["transaction_audit"]["worker"]["runtime"])
        # Keep an executable currently mapped by the cached pure worker intact;
        # replace the pathname with a different private file, never truncate it.
        replacement = root / "invalid-audit-runtime"
        replacement.write_text("changed runtime")
        replacement.chmod(0o700)
        replacement.replace(runtime)
        assert inspected() == ["error", "artifact_invalid", ""]
        assert before[1] != state(root)[1]  # The real readiness request is still accounted.
        assert events(root, api.config, 0) == []
    assert scripted_llm.requests == []


@pytest.mark.parametrize("kind", ["wrong_key", "head_tombstone", "historical_corruption", "orphan_entry"])
def test_restart_refuses_invalid_existing_audit_before_any_automatic_work(
        readiness_host_binary, programs, scripted_llm, tmp_path, monkeypatch, kind):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    scripted_llm.script = [json.loads(text_reply("completed before restart", usage={"input_tokens": 1, "output_tokens": 1}))]
    with AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace) as api:
        code, accepted = api.request()
        assert code == 202
        assert wait_operation(api, accepted["operation"])["status"] == "done"
        assert len(events(root, api.config, 0)) == 2
        audit = api.config["automatic"]["participants"][0]["transaction_audit"]
    if kind == "wrong_key":
        monkeypatch.setenv(audit["key_env"], "valid-length-but-different-fixture-key-0123456789")
    else:
        with NativeStore(STAGE / "native/store/target/debug/sigil-store", root / "records",
                         {audit["heads"]: "read_write", audit["records"]: "read_write"}) as store:
            namespace = audit["heads"] if kind == "head_tombstone" else audit["records"]
            key = audit["chain"] if kind == "head_tombstone" else audit["chain"] + (
                ".0000000000000000" if kind == "historical_corruption" else ".0000000000000007")
            record = store.get(namespace, key)
            # A real Store commit repairs its unkeyed checksum; the authenticated
            # history check, including a non-tail entry, must independently fail.
            value = None if kind == "head_tombstone" else "not an authenticated entry"
            store.commit([mutation(namespace, key, value, record["revision"])])
    before = state(root)
    with pytest.raises(AssertionError, match="audit_verification"):
        AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace, mode="open")
    assert state(root) == before
    assert len(scripted_llm.requests) == 1
    if kind == "wrong_key":
        monkeypatch.setenv(audit["key_env"], audit_key(0))
        with AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace, mode="open") as api:
            assert api.request()[1]["replayed"]
            assert len(events(root, api.config, 0)) == 2
        assert len(scripted_llm.requests) == 1
