"""Actual browser-to-native-to-SIGIL flow. Local model fixture, not M4 live-model acceptance."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from api_support import TOKEN_A, TOKEN_B, credential
from browser_support import BrowserApi
from conftest import API_KEY
from test_turn_execution import programs as programs
from turn_support import text_reply, tool_reply


@pytest.mark.parametrize("mode", ["basic", "lost_ack"])
def test_real_browser_shared_service_flow(browser_service_binary, programs, scripted_llm, tmp_path, monkeypatch, mode):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("The owner is Ada.\n")
    usage = {"input_tokens": 2, "output_tokens": 1}
    scripted_llm.script = [json.loads(tool_reply("read_file", usage=usage)), json.loads(text_reply("ALICE Ada canary.", usage=usage))]
    if mode == "basic":
        scripted_llm.script += [json.loads(text_reply("ALICE follow-up canary.", usage=usage)),
                               json.loads(text_reply("ALICE direct API canary.", usage=usage)),
                               json.loads(text_reply("BOB private canary.", usage=usage))]
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    screenshots = tmp_path / "screenshots"
    with BrowserApi(browser_service_binary, tmp_path / "service", programs, scripted_llm.url, workspace, rows=rows) as api:
        config = {"url": "http://" + api.ready["address"], "tokens": {"a": TOKEN_A, "b": TOKEN_B},
                  "mode": mode, "screenshots": str(screenshots)}
        result = subprocess.run(["node", str(Path(__file__).with_name("browser_flow.mjs"))],
            input=json.dumps(config), capture_output=True, text=True, timeout=420, env=os.environ.copy())
        print(result.stdout)
        assert result.returncode == 0, result.stderr
        summary = json.loads(result.stdout.splitlines()[-1])
        assert summary["result"] == "passed"
        assert summary["submissions"] == (3 if mode == "basic" else 2)
        assert summary["uniqueSubmissionKeys"] == (3 if mode == "basic" else 1)
        assert summary["directApiSubmissions"] == (1 if mode == "basic" else 0)
        assert summary["pageErrors"] == summary["foreignRequests"] == 0
        assert len(scripted_llm.requests) == (5 if mode == "basic" else 2)
        assert scripted_llm.requests[1]["messages"][-1]["content"][0]["content"] == "The owner is Ada.\n"
        assert (screenshots / "01-connect.png").is_file()
        if mode == "basic":
            assert "ALICE Ada canary." in json.dumps(scripted_llm.requests[2])
            assert "ALICE follow-up canary." in json.dumps(scripted_llm.requests[3])
            assert "ALICE" not in json.dumps(scripted_llm.requests[4])
            assert (screenshots / "03-reopened-mobile.png").is_file()
