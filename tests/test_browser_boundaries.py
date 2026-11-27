"""Additional actual-browser boundaries; no candidate or live-model claims."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import subprocess
import threading
import time

import pytest

from api_support import TOKEN_A, TOKEN_B, credential
from browser_support import BrowserApi
from conftest import API_KEY
from test_public_assets_http import state
from test_turn_execution import programs as programs
from turn_support import text_reply


def browser(config, on_accepted=None):
    """Drive only the client; a test-provider event supplies observed dispatch."""
    command = ["node", str(Path(__file__).with_name("browser_boundaries.mjs"))]
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0, env=os.environ.copy())
    events = []
    try:
        proc.stdin.write((json.dumps(config) + "\n").encode())
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], max(0, deadline - time.monotonic()))
            assert ready, "browser boundary exceeded the test observation window"
            line = proc.stdout.readline()
            if not line:
                break
            event = json.loads(line)
            events.append(event)
            print(json.dumps(event))
            if event.get("event") == "submission_accepted":
                assert on_accepted is not None
                on_accepted(event["operation"])
                proc.stdin.write(b'{"event":"effect_observed"}\n')
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
            proc.terminate()  # The driver closes its own browser on SIGTERM.
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        for stream in [proc.stdin, proc.stdout, proc.stderr]:
            stream.close()


def client_config(api, token, mode, tmp_path):
    return {"url": "http://" + api.ready["address"], "token": token,
            "mode": mode, "screenshots": str(tmp_path / "screenshots")}


@pytest.mark.parametrize("mode", ["unknown_credential", "expired_credential", "denied_submission"])
def test_browser_refusal_does_not_change_state_or_start_provider_work(
        browser_service_binary, programs, scripted_llm, tmp_path, monkeypatch, mode):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    row = credential(expires=200) if mode == "expired_credential" else (
        credential(scopes=["sessions:read"]) if mode == "denied_submission" else credential())
    rows = [row]
    if mode == "expired_credential":
        # SIGIL bootstrap requires at least one active credential. A disjoint
        # control tenant keeps that rule intact while the browser presents A's
        # expired credential; B's token is never supplied to this browser.
        rows.append(credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b"))
    token = TOKEN_B if mode == "unknown_credential" else TOKEN_A
    root = tmp_path / "service"
    with BrowserApi(browser_service_binary, root, programs, scripted_llm.url, workspace, rows=rows) as api:
        before = state(root)
        result = browser(client_config(api, token, mode, tmp_path))
        assert result["submissions"] == (1 if mode == "denied_submission" else 0)
        assert result["cancellations"] == 0
        assert result["pageErrors"] == result["foreignRequests"] == 0
        assert state(root) == before
        assert scripted_llm.requests == []
    assert (tmp_path / "screenshots/02-refusal.png").is_file()


@contextmanager
def outstanding_provider():
    received, release = threading.Event(), threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(self.rfile.read(int(self.headers["Content-Length"])))
            received.set()
            # Withhold a response beyond the unchanged 15s worker ceiling.
            # This is a fault injection, not an extended execution deadline.
            release.wait(20)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(text_reply("Late provider response.").encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/messages", received, requests
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_browser_refresh_after_actual_send_then_cancel_preserves_uncertainty_and_restart(
        browser_service_binary, programs, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    with outstanding_provider() as (endpoint, received, requests):
        def observed(operation):
            assert received.wait(60), "actual provider request never arrived"
            assert len(requests) == 1
            assert len(operation) == 64

        with BrowserApi(browser_service_binary, root, programs, endpoint, workspace, rows=rows) as api:
            result = browser(client_config(api, TOKEN_A, "sent_refresh_cancel", tmp_path), observed)
            assert result["submissions"] == result["cancellations"] == 1
            assert result["pageErrors"] == result["foreignRequests"] == 0
            operation = result["operation"]
            status, outcome = api.request("GET", "/v1/operations/" + operation, raw=b"")
            assert status == 200 and outcome["status"] == "uncertain"
            assert outcome["usage"]["known"] is False and outcome["accounting"] == "unknown"
            assert api.request("GET", "/v1/operations/" + operation, token=TOKEN_B, raw=b"")[0] == 404
            asset_path = api.asset_path
        with BrowserApi(browser_service_binary, root, programs, endpoint, workspace,
                        rows=rows, mode="open", asset_path=asset_path) as api:
            assert api.request("GET", "/v1/operations/" + operation, raw=b"") == (200, outcome)
        assert len(requests) == 1, "refresh/cancellation/restart repeated the external request"
    assert (tmp_path / "screenshots/03-uncertain.png").is_file()
