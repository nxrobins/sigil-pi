"""Actual browser accounting outcomes and independent durable-state observations."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from api_support import TOKEN_A, TOKEN_B, credential
from browser_support import BrowserApi
from conftest import API_KEY
from test_automatic_preclaim import retained
from test_turn_execution import programs as programs
from turn_support import fields, tool_reply


def run_browser(config):
    command = ["node", str(Path(__file__).with_name("browser_accounting.mjs"))]
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, env=os.environ.copy()) as proc:
        try:
            output, errors = proc.communicate(json.dumps(config), timeout=240)
            return subprocess.CompletedProcess(command, proc.returncode, output, errors)
        finally:
            if proc.poll() is None:
                proc.terminate()  # Give the driver a chance to close its browser.
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


@pytest.mark.parametrize("usage,error,accounting,hold", [
    (None, "usage_unknown", "unknown", ["0", "20000", "4096"]),
    ({"input_tokens": 20000, "output_tokens": 1}, "quota_exhausted", "reported", ["0", "0", "0"]),
])
def test_browser_midturn_accounting_stop_is_truthful_and_does_not_repeat_work(
        browser_service_binary, programs, scripted_llm, tmp_path, monkeypatch,
        usage, error, accounting, hold):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("Accounting-boundary permitted file canary.\n")
    scripted_llm.script = [json.loads(tool_reply("read_file", usage=usage))]
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    root = tmp_path / "service"
    with BrowserApi(browser_service_binary, root, programs, scripted_llm.url, workspace, rows=rows) as api:
        config = {"url": "http://" + api.ready["address"], "token": TOKEN_A,
                  "error": error, "accounting": accounting, "known": usage is not None,
                  "screenshots": str(tmp_path / "screenshots")}
        result = run_browser(config)
        print(result.stdout)
        assert result.returncode == 0, result.stderr
        outcome = json.loads(result.stdout.splitlines()[-1])
        assert outcome["result"] == "passed"
        assert outcome["submissions"] == 1 and outcome["cancellations"] == 0
        assert outcome["pageErrors"] == outcome["foreignRequests"] == 0
        operation = outcome["operation"]
        status, terminal = api.request("GET", "/v1/operations/" + operation, raw=b"")
        assert status == 200 and terminal["status"] == "failed"
        assert terminal["error"] == error and terminal["accounting"] == accounting
        assert terminal["usage"] == {"input_tokens": 0 if usage is None else usage["input_tokens"],
                                     "output_tokens": 0 if usage is None else usage["output_tokens"],
                                     "known": usage is not None}
        assert api.request("GET", "/v1/operations/" + operation, token=TOKEN_B, raw=b"")[0] == 404
        asset_path = api.asset_path
    # Real committed observations, not native facts manufactured by the client.
    expected = [operation + ":1", operation + ":2"]
    assert [row[0] for row in retained(root, "executor0.claim")] == expected
    assert [row[0] for row in retained(root, "executor0.delivery")] == expected
    assert fields(retained(root, "a.budget")[0][2], "BH1\n", 5)[2:] == hold
    assert len(scripted_llm.requests) == 1
    with BrowserApi(browser_service_binary, root, programs, scripted_llm.url, workspace,
                    rows=rows, mode="open", asset_path=asset_path) as api:
        assert api.request("GET", "/v1/operations/" + operation, raw=b"") == (200, terminal)
    assert [row[0] for row in retained(root, "executor0.claim")] == expected
    assert [row[0] for row in retained(root, "executor0.delivery")] == expected
    assert fields(retained(root, "a.budget")[0][2], "BH1\n", 5)[2:] == hold
    assert len(scripted_llm.requests) == 1, "refresh or reopen repeated model work"
    assert (tmp_path / "screenshots/02-accounting-stop.png").is_file()
    assert (tmp_path / "screenshots/03-reopened.png").is_file()
