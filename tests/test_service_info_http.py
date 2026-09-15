"""Actual HTTP, request state and model/tool regression for the SIGIL entry."""

import json
import os
from pathlib import Path
import signal

import pytest

from api_support import BODY, TOKEN_A, TOKEN_B, credential
from automatic_audit_support import events
from automatic_support import wait_operation
from conftest import API_KEY
from http_service_support import effect_counts, state
from readiness_host_support import readiness_host_binary as readiness_host_binary
from request_api_checks import counters, only_accounting_changed, window_headroom
from service_info_support import ServiceInfoApi
from test_automatic_audit import audit_secrets as audit_secrets
from test_automatic_effect_audit import effect_secrets as effect_secrets
from test_service_info_entry import LABEL, PATHS, VERSION, legacy
from test_turn_execution import programs as programs
from turn_support import text_reply, tool_reply


pytestmark = pytest.mark.usefixtures("audit_secrets", "effect_secrets")


def response(api, path, **kwargs):
    code, headers, raw = api.exchange("GET", path, hints=[LABEL], **kwargs)
    headers = dict(headers)
    assert headers["cache-control"] == "no-store"
    assert headers["content-type"] == "application/json"
    selected = {k: v for k, v in headers.items() if k in {"x-request-id", "retry-after", "www-authenticate"}}
    return code, selected, json.loads(raw)


def workspace(tmp_path):
    path = tmp_path / "workspace"
    path.mkdir()
    return path


def test_actual_responses_two_tenants_count_requests_without_provider_or_domain_work(
        readiness_host_binary, programs, scripted_llm, tmp_path):
    root, work = tmp_path / "service", workspace(tmp_path)
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    with ServiceInfoApi(readiness_host_binary, root, programs, scripted_llm.url, work, rows=rows, limit=2) as api:
        source = Path(api.config["worker"]["source"]).read_text()
        assert "admission_probe_terminal" not in source and 'text("218")' not in source
        before = state(root)
        for path in PATHS:
            assert response(api, path, token=None) == legacy(path, token=None)
            assert response(api, path, token="unknown") == legacy(path, token="unknown")
            assert state(root) == before
        window = window_headroom()
        for token in (TOKEN_A, TOKEN_B):
            for path in PATHS:
                assert response(api, path, token=token, raw=b'{"version":"forged","tenant":"other"}') == legacy(path)
        assert counters(root) == {"a.budget": (2, "tenant-a", str(window), "2"),
                                  "b.budget": (2, "tenant-b", str(window), "2")}
        counted = state(root)
        only_accounting_changed(before, counted, 4)
        for token in (TOKEN_A, TOKEN_B):
            code, headers, body = response(api, "/v1/health", token=token)
            assert code == 429 and body == {"error": {"code": "rate_limited", "message": "tenant request rate exceeded"}, "request_id": LABEL}
            assert 1 <= int(headers["retry-after"]) <= 60
            assert state(root) == counted
        assert effect_counts(root) == {}
        for index in range(2):
            assert events(root, api.config, index) == events(root, api.config, index, effects=True) == []
    assert scripted_llm.requests == []


def test_actual_permission_errors_are_counted_and_have_legacy_envelopes(
        readiness_host_binary, programs, scripted_llm, tmp_path):
    root, work = tmp_path / "service", workspace(tmp_path)
    with ServiceInfoApi(readiness_host_binary, root, programs, scripted_llm.url, work,
                        rows=[credential(scopes=["chat"])]) as api:
        before = state(root)
        for count, path in enumerate(PATHS, 1):
            assert response(api, path) == legacy(path, scopes=["chat"])
            only_accounting_changed(before, state(root), count)
        assert effect_counts(root) == {}
    assert scripted_llm.requests == []


def test_actual_storage_outage_never_uses_emergency_accounting_for_info_routes(
        readiness_host_binary, programs, scripted_llm, tmp_path):
    root, work = tmp_path / "service", workspace(tmp_path)
    class Broken:
        def admit_request(self, tenant):
            raise OSError("private path")
    with ServiceInfoApi(readiness_host_binary, root, programs, scripted_llm.url, work) as api:
        before = state(root)
        os.rename(root / "records", root / "parked")
        try:
            for path in PATHS:
                assert response(api, path, token=None) == legacy(path, token=None)
                assert response(api, path) == legacy(path, quota=Broken())
        finally:
            os.rename(root / "parked", root / "records")
        assert state(root) == before
        assert response(api, "/v1/health") == legacy("/v1/health")
        only_accounting_changed(before, state(root), 1)
        assert effect_counts(root) == {}
    assert scripted_llm.requests == []


def test_restart_retains_actual_request_accounting_and_declared_version(
        readiness_host_binary, programs, scripted_llm, tmp_path):
    root, work = tmp_path / "service", workspace(tmp_path)
    with ServiceInfoApi(readiness_host_binary, root, programs, scripted_llm.url, work, limit=2) as api:
        window_headroom()
        assert response(api, "/v1/version") == legacy("/v1/version")
        saved = state(root)
        prior = counters(root)
    with ServiceInfoApi(readiness_host_binary, root, programs, scripted_llm.url, work, limit=2, mode="open") as api:
        assert state(root) == saved and counters(root) == prior
        assert response(api, "/v1/version")[2]["version"] == VERSION
        counted = state(root)
        assert response(api, "/v1/health")[0] == 429
        assert state(root) == counted and effect_counts(root) == {}
    assert scripted_llm.requests == []


def test_draining_keeps_info_routes_live_while_stopping_new_turns(
        readiness_host_binary, programs, scripted_llm, tmp_path):
    root, work = tmp_path / "service", workspace(tmp_path)
    with ServiceInfoApi(readiness_host_binary, root, programs, scripted_llm.url, work) as api:
        os.kill(api.proc.pid, signal.SIGUSR1)
        for path in PATHS:
            assert response(api, path) == legacy(path, draining=True)
        code, denied = api.request()
        assert code == 503 and denied == {"error": {"code": "service_draining"}}
        assert api.exchange("GET", "/v1/sessions")[0] == 200
        assert effect_counts(root) == {}
    assert scripted_llm.requests == []


def test_new_entry_retains_audited_model_tool_followup_two_tenants_and_restart(
        readiness_host_binary, programs, scripted_llm, tmp_path):
    root, work = tmp_path / "service", workspace(tmp_path)
    (work / "README.md").write_text("Private owner: Ada.\n")
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    usage = {"input_tokens": 1, "output_tokens": 1}
    scripted_llm.script = [json.loads(tool_reply("read_file", usage=usage)),
        json.loads(text_reply("Ada owns it.", usage=usage)),
        json.loads(text_reply("Bob's independent answer.", usage=usage)),
        json.loads(text_reply("Still Ada.", usage=usage))]
    with ServiceInfoApi(readiness_host_binary, root, programs, scripted_llm.url, work, rows=rows) as api:
        assert response(api, "/v1/version")[2]["version"] == VERSION
        accepted, answers = [], []
        for index, token in enumerate((TOKEN_A, TOKEN_B)):
            code, submission = api.request(token=token)
            assert code == 202
            result = wait_operation(api, submission["operation"], token=token)
            assert result["status"] == "done" and result["reply"] == ["Ada owns it.", "Bob's independent answer."][index]
            assert api.request("GET", submission["status_url"], token=[TOKEN_B, TOKEN_A][index])[0] == 404
            lifecycle = events(root, api.config, index, effects=True)
            assert [e["event"] for e in lifecycle] == ["prepared", "observed"] * [3, 1][index]
            assert {e["operation"] for e in lifecycle} == {submission["operation"]}
            # Keep the actual namespace/operation binding and independently
            # verified MACs returned by events; do not infer audit from replies.
            assert {e["tenant"] for e in lifecycle} == {"tenant-a" if index == 0 else "tenant-b"}
            accepted.append(submission)
            answers.append(result)
        code, following = api.request(body={**BODY, "message": "Who owns it?", "submission_key": "followup"})
        assert code == 202
        followed = wait_operation(api, following["operation"])
        assert followed["status"] == "done" and followed["reply"] == "Still Ada."
        assert api.exchange("GET", "/v1/sessions")[0] == 200
        audits = [(events(root, api.config, i), events(root, api.config, i, effects=True)) for i in range(2)]
        assert [len(v) for v in audits[0]] == [6, 8]
        assert [len(v) for v in audits[1]] == [2, 2]
        assert API_KEY not in json.dumps([answers, followed, audits])
    assert len(scripted_llm.requests) == 4
    assert scripted_llm.requests[1]["messages"][-1]["content"][0]["content"] == "Private owner: Ada.\n"
    with ServiceInfoApi(readiness_host_binary, root, programs, scripted_llm.url, work, rows=rows, mode="open") as api:
        for index, token in enumerate((TOKEN_A, TOKEN_B)):
            assert api.request("GET", accepted[index]["status_url"], token=token)[1] == answers[index]
            assert api.request(token=token)[1]["replayed"] is True
            assert events(root, api.config, index) == audits[index][0]
            assert events(root, api.config, index, effects=True) == audits[index][1]
        assert api.request("GET", following["status_url"])[1] == followed
        assert response(api, "/v1/health") == legacy("/v1/health")
    assert len(scripted_llm.requests) == 4
