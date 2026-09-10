"""An old authenticated response cannot replace a newer browser connection."""
import json
import os
from pathlib import Path
import subprocess

from api_support import TOKEN_A, TOKEN_B, credential
from browser_support import BrowserApi
from conftest import API_KEY
from test_automatic_preclaim import retained
from test_turn_execution import programs as programs
from turn_support import text_reply


def test_browser_discards_actual_previous_tenant_history_after_reconnection(
        browser_service_binary, programs, scripted_llm, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    canaries = {"a": "ALICE stale-connection response canary.", "b": "BOB active-connection response canary."}
    scripted_llm.script = [json.loads(text_reply(canaries[key], usage={"input_tokens": 2, "output_tokens": 1}))
                           for key in ["a", "b"]]
    root = tmp_path / "service"
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    with BrowserApi(browser_service_binary, root, programs, scripted_llm.url, workspace, rows=rows) as api:
        config = {"url": "http://" + api.ready["address"], "tokens": {"a": TOKEN_A, "b": TOKEN_B},
                  "canaries": canaries, "screenshots": str(tmp_path / "screenshots")}
        command = ["node", str(Path(__file__).with_name("browser_connections.mjs"))]
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=os.environ.copy()) as proc:
            try:
                output, errors = proc.communicate(json.dumps(config), timeout=240)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
        print(output)
        assert proc.returncode == 0, errors
        result = json.loads(output.splitlines()[-1])
        assert result["result"] == "passed"
        assert result["submissions"] == result["cancellations"] == 0
        assert result["directApiSubmissions"] == 2
        assert result["heldActualResponses"] == 1
        assert result["pageErrors"] == result["foreignRequests"] == 0
        assert len(set(result["operations"].values())) == 2
        for owner, token, other in [("a", TOKEN_A, TOKEN_B), ("b", TOKEN_B, TOKEN_A)]:
            path = "/v1/operations/" + result["operations"][owner]
            status, value = api.request("GET", path, token=token, raw=b"")
            assert status == 200 and value["status"] == "done" and value["reply"] == canaries[owner]
            assert api.request("GET", path, token=other, raw=b"")[0] == 404
    assert len(scripted_llm.requests) == 2
    for index, owner in enumerate(["a", "b"]):
        expected = [result["operations"][owner] + ":1"]
        assert [row[0] for row in retained(root, f"executor{index}.claim")] == expected
        assert [row[0] for row in retained(root, f"executor{index}.delivery")] == expected
    assert (tmp_path / "screenshots/02-new-connection.png").is_file()
    assert (tmp_path / "screenshots/03-old-response-released.png").is_file()
