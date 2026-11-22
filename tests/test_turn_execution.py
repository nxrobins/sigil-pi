"""Actual isolated model/file effects driven by SIGIL proposals, not PiAgent.

This test-only executor does not implement conversation decisions. It has a fixed
artifact registry and feeds observations back to the reducer. Its in-memory state
does NOT claim crash-durable acceptance or authenticated per-operation grants.
"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from conftest import API_KEY, MCP_BIN, PI_ROOT, SIGIL_ROOT, forge_ok, needs_toolchain
from runtime_client import ProductionSigilMCP, RuntimeClientTimeout, SupervisedRuntime
from scripts.compose_application import compose_application
from sigil_compose import compose_with_stdlib
from turn_support import (FUEL, event, model_reply, record, refused, run,
                          source, text_reply, tool_reply)


@pytest.fixture(scope="module")
def programs():
    needs_toolchain()
    return {
        "turn": source(),
        "turn_transaction": compose_application("turn_transaction", SIGIL_ROOT).text,
        "executor_transaction": compose_application("executor_transaction", SIGIL_ROOT).text,
        "submission": compose_application("submission", SIGIL_ROOT).text,
        "read_request": compose_application("read_request", SIGIL_ROOT).text,
        "read_file": (PI_ROOT / "tools/read_file.sigil").read_text(),
        "provider": compose_with_stdlib(
            (PI_ROOT / "tools/agent_turn.sigil").read_text(), ["http"], SIGIL_ROOT).text,
    }


def provider_input(endpoint, body):
    # A fixed fixture artifact binding, not a model-selectable endpoint/key.
    headers = ("x-api-key: {{secret:anthropic}}\n"
               "anthropic-version: 2023-06-01\ncontent-type: application/json")
    return f"{endpoint}|{headers}|{body}"


def provider_grants():
    return {"net": ["127.0.0.1"], "secret": [f"anthropic={API_KEY}"]}


def execute_fixture(mcp, programs, first, endpoint, workspace):
    """Protocol pump over a fixed registry. SIGIL decides every next action."""
    current, trace = first, []
    for _ in range(20):  # independent TEST ceiling; the SIGIL step cap is tested separately
        if current.action not in {"model", "tool"}:
            return current, trace
        if current.action == "model":
            name = "provider"
            payload = provider_input(endpoint, current.input)
            grants = provider_grants()
        else:
            # This independent fixture registry contains only one approved tool.
            # It cannot execute an arbitrary artifact/path supplied by a proposal.
            assert current.tool == "read_file", "not in the fixture's admitted registry"
            binding = record("PF1\n", [str(workspace), current.input])
            payload = forge_ok(mcp, programs["read_request"], binding, fuel=FUEL)
            trace.append({"artifact": "read_request", "grants": None, "input": binding})
            name, grants = "read_file", {"fs": [str(workspace)]}
        result = mcp.forge(programs[name], input=payload, grants=grants, fuel=FUEL)
        trace.append({"artifact": name, "grants": grants, "input": payload,
                      "status": result["status"]})
        if result["status"] == "ok":
            observation = current.result(result["data"]["output_text"])
        else:
            # Read failure is an error observation, never successful file bytes.
            # A provider transport failure is conservatively uncertain; no retry.
            observation = current.result("fixture effect failed",
                kind="unknown" if name == "provider" else "tool_error")
        trace.append({"artifact": "turn", "grants": None, "input": observation})
        current = run(mcp, programs["turn"], observation)
    raise AssertionError("fixture ceiling exceeded; SIGIL did not stop")


def test_sigil_turn_runs_real_model_file_and_response_with_separate_grants(
        mcp, programs, scripted_llm, tmp_path):
    (tmp_path / "README.md").write_text("The project owner is Ada.\n")
    scripted_llm.script = [json.loads(tool_reply("read_file",
        usage={"input_tokens": 11, "output_tokens": 4})),
        json.loads(text_reply("The project owner is Ada.",
            usage={"input_tokens": 7, "output_tokens": 5}))]
    accepted_body = forge_ok(mcp, programs["submission"], json.dumps({
        "session": "same-name", "message": "Who owns the project?", "submission_key": "key-1"}), fuel=FUEL)
    first = run(mcp, programs["turn"], event(payload=accepted_body))
    final, trace = execute_fixture(mcp, programs, first, scripted_llm.url, tmp_path)
    assert final.action == "done" and final.values[9] == "The project owner is Ada."
    assert final.values[10:13] == ["18", "9", "1"]
    assert [row["artifact"] for row in trace] == [
        "provider", "turn", "read_request", "read_file", "turn", "provider", "turn"]
    assert len(scripted_llm.requests) == 2
    assert scripted_llm.requests[1]["messages"][-1]["content"][0]["content"] == "The project owner is Ada.\n"
    for row in trace:
        assert API_KEY not in row["input"]
        if row["artifact"] in {"turn", "read_request"}:
            assert row["grants"] is None
        elif row["artifact"] == "read_file":
            assert row["grants"] == {"fs": [str(tmp_path)]}
        else:
            assert set(row["grants"]) == {"net", "secret"}
    assert API_KEY not in final.state


@pytest.mark.parametrize("path_mode", ["parent", "absolute", "symlink"])
def test_sigil_selected_read_still_cannot_escape_its_filesystem_grant(
        mcp, programs, scripted_llm, tmp_path, path_mode):
    workspace = tmp_path / "permitted"
    workspace.mkdir()
    forbidden = tmp_path / "outside.txt"
    forbidden.write_text("outside-workspace-canary")
    path = "../outside.txt"
    if path_mode == "absolute":
        path = str(forbidden)
    elif path_mode == "symlink":
        (workspace / "link.txt").symlink_to(forbidden)
        path = "link.txt"
    scripted_llm.script = [json.loads(model_reply([{
        "type": "tool_use", "id": "read-1", "name": "read_file", "input": {"path": path}}])),
        json.loads(text_reply("The read was denied."))]
    first = run(mcp, programs["turn"], event())
    final, trace = execute_fixture(mcp, programs, first, scripted_llm.url, workspace)
    read = next(row for row in trace if row["artifact"] == "read_file")
    assert read["status"] == "error"
    assert "outside-workspace-canary" not in json.dumps(scripted_llm.requests)
    assert "outside-workspace-canary" not in final.state
    assert scripted_llm.requests[1]["messages"][-1]["content"][0]["is_error"] is True


@pytest.mark.parametrize("value", [None, 7, {}, [], "", "bad\x00path", "bad\npath", "bad\rpath"])
def test_read_adapter_rejects_bad_paths_before_filesystem_work(mcp, programs, value):
    binding = record("PF1\n", ["/fixture/permitted", json.dumps({"path": value})])
    refused(mcp, programs["read_request"], binding, 400)


@contextmanager
def hanging_provider():
    received, release = threading.Event(), threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(payload)
            received.set()
            release.wait(10)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(text_reply().encode())
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


def test_worker_killed_after_request_arrives_recovers_as_uncertain_without_replay(
        mcp, programs, tmp_path):
    first = run(mcp, programs["turn"], event())
    saved = tmp_path / "observed-proposal.txt"
    saved.write_text(first.state)  # fixture snapshot, NOT a crash-durability claim
    spawned = []

    def spawn():
        client = ProductionSigilMCP.spawn(MCP_BIN, timeout_s=5)
        spawned.append(client)
        return client

    with hanging_provider() as (endpoint, received, requests):
        with SupervisedRuntime(spawn) as worker:
            with pytest.raises(RuntimeClientTimeout):
                worker.forge(programs["provider"],
                    input=provider_input(endpoint, first.input), grants=provider_grants(),
                    fuel=FUEL, timeout_s=1.5)
            assert received.is_set(), "the request must actually arrive before cancellation"
            assert len(requests) == 1
            assert spawned[0]._proc.poll() is not None, "local execution was not reaped"
            # The next forge uses a fresh supervised runtime generation and the
            # saved SIGIL proposal. It interprets unknown delivery, not a retry.
            recovered = run(worker, programs["turn"], first.result(
                prior=saved.read_text(), kind="unknown"))
            assert recovered.action == "uncertain"
            assert recovered.values[14] == "possibly_delivered"
            assert len(spawned) == 2 and len(requests) == 1
