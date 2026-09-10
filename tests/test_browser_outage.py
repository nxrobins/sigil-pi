"""Actual service stop/reopen coordinated through private test-process pipes."""
import json
import os
from pathlib import Path
import select
import subprocess
import time

from api_support import TOKEN_A, TOKEN_B, credential
from browser_support import BrowserApi
from conftest import API_KEY
from test_automatic_preclaim import retained
from test_turn_execution import programs as programs
from turn_support import text_reply


def interactive_browser(config, control):
    command = ["node", str(Path(__file__).with_name("browser_outage.mjs"))]
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0, env=os.environ.copy())
    events = []
    try:
        proc.stdin.write((json.dumps(config) + "\n").encode())
        until = time.monotonic() + 240
        while time.monotonic() < until:
            ready, _, _ = select.select([proc.stdout], [], [], max(0, until - time.monotonic()))
            assert ready, "browser outage exceeded the test observation window"
            line = proc.stdout.readline()
            if not line:
                break
            event = json.loads(line)
            events.append(event)
            print(json.dumps(event))
            if event.get("event") in {"stop_service", "reopen_service"}:
                reply = control(event)
                proc.stdin.write((json.dumps(reply) + "\n").encode())
            if event.get("result") == "passed":
                break
        proc.stdin.close()
        code = proc.wait(timeout=15)
        errors = proc.stderr.read().decode()
        assert code == 0, errors
        assert events and events[-1].get("result") == "passed", (events, errors)
        return events[-1]
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        for stream in [proc.stdin, proc.stdout, proc.stderr]:
            stream.close()


def test_browser_real_service_outage_then_reopen_never_resubmits_committed_work(
        browser_service_binary, programs, scripted_llm, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    answer = "Committed answer survives this local service outage."
    scripted_llm.script = [json.loads(text_reply(answer, usage={"input_tokens": 2, "output_tokens": 1}))]
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    with BrowserApi(browser_service_binary, root, programs, scripted_llm.url, workspace, rows=rows) as api:
        args = list(api.proc.args)
        assert len(args) == 6 and args[1] == "init" and args[4] == "--public-assets"
        old_ready = dict(api.ready)
        events = []
        observed = {}

        def control(message):
            event = message["event"]
            events.append(event)
            if event == "stop_service":
                assert events == ["stop_service"]
                operation = message["operation"]
                status, terminal = api.request("GET", "/v1/operations/" + operation, raw=b"")
                assert status == 200 and terminal["status"] == "done" and terminal["reply"] == answer
                observed.update(operation=operation, terminal=terminal)
                api.close()
                assert api.proc.poll() is not None
                return {"event": "service_stopped"}
            assert events == ["stop_service", "reopen_service"]
            command = [args[0], "open", args[2], str(api.port), args[4], args[5]]
            api.proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            ready, _, _ = select.select([api.proc.stdout], [], [], 40)
            assert ready, "same-origin service reopen timed out"
            line = api.proc.stdout.readline()
            assert line, api.proc.stderr.read()
            api.ready = json.loads(line)
            assert api.ready["status"] == "ready"
            assert api.ready["protocol"] == old_ready["protocol"]
            assert api.ready["address"] == old_ready["address"]
            assert api.ready["public_assets_sha256"] == old_ready["public_assets_sha256"]
            return {"event": "service_reopened"}

        result = interactive_browser({"url": "http://" + api.ready["address"], "token": TOKEN_A,
            "answer": answer,
            "screenshots": str(tmp_path / "screenshots")}, control)
        assert events == ["stop_service", "reopen_service"]
        assert result["submissions"] == 1 and result["cancellations"] == 0
        assert result["pageErrors"] == result["foreignRequests"] == 0
        assert result["failedReads"] >= 1
        assert result["operation"] == observed["operation"]
        path = "/v1/operations/" + observed["operation"]
        assert api.request("GET", path, raw=b"") == (200, observed["terminal"])
        assert api.request("GET", path, token=TOKEN_B, raw=b"")[0] == 404
    assert len(scripted_llm.requests) == 1
    expected = [observed["operation"] + ":1"]
    assert [row[0] for row in retained(root, "executor0.claim")] == expected
    assert [row[0] for row in retained(root, "executor0.delivery")] == expected
    assert (tmp_path / "screenshots/02-outage.png").is_file()
    assert (tmp_path / "screenshots/03-restored.png").is_file()
