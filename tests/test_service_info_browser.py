"""Rendered browser + actual info routes + independent state/audit observations."""

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from api_support import TOKEN_A, TOKEN_B, credential
from automatic_audit_support import events
from http_service_support import effect_counts, state
from readiness_host_support import readiness_host_binary as readiness_host_binary
from request_api_checks import counters, only_accounting_changed
from service_info_support import ServiceInfoBrowserApi
from test_automatic_audit import audit_secrets as audit_secrets
from test_automatic_effect_audit import effect_secrets as effect_secrets
from test_automatic_preclaim import retained
from test_browser_accounting import run_browser
from test_service_info_entry import LABEL, PATHS, legacy
from test_turn_execution import programs as programs
from turn_support import fields, tool_reply

pytestmark = pytest.mark.usefixtures("audit_secrets", "effect_secrets")


def test_browser_information_replies_match_oracle_without_effects_or_domain_changes(
        readiness_host_binary, browser_runtime, programs, scripted_llm, tmp_path):
    assert browser_runtime["playwright"]
    root, workspace = tmp_path / "service", tmp_path / "workspace"
    workspace.mkdir()
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b",
                                     scopes=["sessions:read"])]
    with ServiceInfoBrowserApi(readiness_host_binary, root, programs, scripted_llm.url, workspace, rows=rows) as api:
        source = Path(api.config["worker"]["source"]).read_text()
        assert "admission_probe_terminal" not in source and 'text("218")' not in source
        before = state(root)
        config = {"url": "http://" + api.ready["address"],
            "tokens": {"a": TOKEN_A, "b": TOKEN_B, "unknown": "unknown"},
            "requestId": LABEL, "screenshots": str(tmp_path / "screenshots")}
        result = subprocess.run(["node", str(Path(__file__).with_name("browser_service_info.mjs"))],
            input=json.dumps(config), capture_output=True, text=True, timeout=240, env=os.environ.copy())
        print(result.stdout)
        assert result.returncode == 0, result.stderr
        outcome = json.loads(result.stdout.splitlines()[-1])
        assert outcome["result"] == "passed"
        assert outcome["pageErrors"] == outcome["foreignRequests"] == outcome["failedRequests"] == outcome["writes"] == 0
        observations = outcome["observed"]
        assert [(r["name"], r["pathname"]) for r in observations] == [(name, path) for name in config["tokens"] for path in PATHS]
        for row in observations:
            options = {"scopes": ["sessions:read"]} if row["name"] == "b" else (
                {"token": "unknown"} if row["name"] == "unknown" else {})
            code, headers, body = legacy(row["pathname"], **options)
            assert (row["status"], row["body"]) == (code, body)
            assert row["headers"]["challenge"] == headers.get("www-authenticate")
        # One connect/list + two info requests for A; two denied info requests
        # for B. Invalid auth and public assets cannot consume tenant capacity.
        only_accounting_changed(before, state(root), 5)
        counts = counters(root)
        assert set(counts) == {"a.budget", "b.budget"}
        assert counts["a.budget"][:2] == (3, "tenant-a")
        assert counts["b.budget"][:2] == (2, "tenant-b")
        assert effect_counts(root) == {}
        for index in range(2):
            assert events(root, api.config, index) == events(root, api.config, index, effects=True) == []
        assert scripted_llm.requests == []
    assert (tmp_path / "screenshots/01-connect.png").is_file()
    assert (tmp_path / "screenshots/02-connected-info.png").is_file()


def test_browser_retains_reported_subtotal_without_claiming_unknown_usage_is_a_total(
        readiness_host_binary, browser_runtime, programs, scripted_llm, tmp_path):
    assert browser_runtime["playwright"]
    root, workspace = tmp_path / "service", tmp_path / "workspace"
    workspace.mkdir()
    content = "Earlier observed usage remains a subtotal, not a final bill.\n"
    (workspace / "README.md").write_text(content)
    second = json.loads(tool_reply("read_file"))
    second["content"][0]["id"] = "tool-next"
    scripted_llm.script = [json.loads(tool_reply("read_file", usage={"input_tokens": 2, "output_tokens": 1})), second]
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    with ServiceInfoBrowserApi(readiness_host_binary, root, programs, scripted_llm.url, workspace, rows=rows) as api:
        result = run_browser({"url": "http://" + api.ready["address"], "token": TOKEN_A,
            "error": "usage_unknown", "accounting": "unknown", "known": False,
            "reportedSubtotal": [2, 1], "screenshots": str(tmp_path / "screenshots")})
        print(result.stdout)
        assert result.returncode == 0, result.stderr
        observed = json.loads(result.stdout.splitlines()[-1])
        assert observed["result"] == "passed"
        assert observed["submissions"] == 1 and observed["cancellations"] == 0
        assert observed["pageErrors"] == observed["foreignRequests"] == 0
        operation = observed["operation"]
        path = "/v1/operations/" + operation
        status, terminal = api.request("GET", path, raw=b"")
        assert status == 200 and terminal["status"] == "failed" and terminal["error"] == "usage_unknown"
        assert terminal["usage"] == {"input_tokens": 2, "output_tokens": 1, "known": False}
        assert terminal["accounting"] == "unknown" and terminal["reply"] == ""
        assert api.request("GET", path, token=TOKEN_B, raw=b"")[0] == 404
        # Unknown usage prohibits another billable model request, not the
        # already-authorized file read. Both model/file pairs really completed;
        # the following model intent was refused before a fifth claim.
        keys = [operation + ":" + str(sequence) for sequence in range(1, 5)]
        for namespace in ("executor0.claim", "executor0.delivery"):
            assert [row[0] for row in retained(root, namespace)] == keys
        audit = events(root, api.config, 0, effects=True)
        assert [event["event"] for event in audit] == ["prepared", "observed"] * 4
        assert [event["dispatch_policy"]["alias"] for event in audit] == ["provider"] * 2 + ["reader"] * 2 + ["provider"] * 2 + ["reader"] * 2
        assert {event["operation"] for event in audit} == {operation}
        for index in (3, 7):
            assert audit[index]["retained_output_sha256"] == hashlib.sha256(content.encode()).hexdigest()
        assert events(root, api.config, 1) == events(root, api.config, 1, effects=True) == []
        assert fields(retained(root, "a.budget")[0][2], "BH1\n", 5)[2:] == ["0", "20000", "4096"]
        assert len(scripted_llm.requests) == 2
        assert scripted_llm.requests[1]["messages"][-1]["content"][0]["content"] == content
        asset_path = api.asset_path
    with ServiceInfoBrowserApi(readiness_host_binary, root, programs, scripted_llm.url, workspace,
                               rows=rows, mode="open", asset_path=asset_path) as api:
        assert api.request("GET", path, raw=b"") == (200, terminal)
        assert events(root, api.config, 0, effects=True) == audit
        for namespace in ("executor0.claim", "executor0.delivery"):
            assert [row[0] for row in retained(root, namespace)] == keys
        assert fields(retained(root, "a.budget")[0][2], "BH1\n", 5)[2:] == ["0", "20000", "4096"]
    assert len(scripted_llm.requests) == 2
    assert (tmp_path / "screenshots/02-accounting-stop.png").is_file()
    assert (tmp_path / "screenshots/03-reopened.png").is_file()
