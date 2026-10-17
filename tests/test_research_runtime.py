"""Research runtime ownership through real subprocesses and HTTP turns."""
import json
import sys
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

import agent as host
from agent import PiAgent, SessionStore, build_runtime, serve


PEER = r'''
import json, pathlib, sys, time, urllib.request
root = pathlib.Path(__file__).parent
counter = root / "generation"
generation = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(generation))
mode = (root / "mode").read_text()
for line in sys.stdin:
    req = json.loads(line)
    method = req["method"]
    with (root / "wire.jsonl").open("a") as log:
        log.write(json.dumps(dict(req, generation=generation)) + "\n")
    if generation == 1 and ((mode == "capability" and method == "tools/list") or
                            (mode in ("forge", "malformed") and method == "tools/call")):
        if mode == "malformed":
            print("invalid json", flush=True)
            continue
        time.sleep(10)
    if method == "initialize":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": "sigil_forge", "inputSchema": {"properties": {
            "grants": {"properties": {"http_timeout_ms": {"type": "integer"}}}}}}]}
    else:
        if mode == "effect":
            with urllib.request.urlopen(req["params"]["arguments"]["input"], data=b"effect") as response:
                response.read()
        source = req["params"]["arguments"]["source"]
        result = {"content": [{"text": json.dumps({"status": "ok", "data": {
            "output_text": "raw" if source == "llm" else "t00000005ready"}})}]}
    print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}), flush=True)
'''


@pytest.mark.parametrize("mode", ["capability", "forge", "malformed"])
def test_research_http_recovers_on_the_next_operation_with_a_fresh_verified_child(tmp_path, monkeypatch, mode):
    peer = tmp_path / "sigil-peer"
    peer.write_text(f"#!{sys.executable}\n" + PEER)
    peer.chmod(0o755)
    (tmp_path / "mode").write_text(mode)
    verified = []
    monkeypatch.setattr(host.toolchain, "verify_binary", lambda path: verified.append(path))
    records = []
    receipt = {"recorded": True, "status": "awaiting_review", "fact_verified": False}
    memory = SimpleNamespace(fact_grounding=True, complete=lambda r: {}, judge=lambda r: {},
        recall=lambda *a: "", record=lambda *a: (records.append(a), receipt)[1])
    with build_runtime(peer) as runtime:
        agent = PiAgent("http://localhost/messages", "synthetic", SessionStore(tmp_path / "sessions"),
                        tmp_path / "sandboxes", memory=memory, mcp=runtime, turn_deadline_s=1.2)
        agent._llm_src, agent._parse_src = "llm", "parse"
        first = runtime._client._proc
        server = serve(agent, port=0)

        def request(session, message):
            req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/chat",
                data=json.dumps({"session": session, "message": message}).encode(),
                headers={"Content-Type": "application/json"})
            return urllib.request.urlopen(req, timeout=5)

        try:
            with pytest.raises(urllib.error.HTTPError) as error:
                request("failed", "Correction: Nadia no longer lives in Oslo.")
            assert error.value.code == 500
            failure = json.load(error.value)
            assert failure["memory_receipt"] == receipt
            assert first.poll() is not None, failure
            agent.turn_deadline_s = 5
            with request("next", "hello") as response:
                assert json.load(response)["reply"] == "ready"
            second = runtime._client._proc
            assert second is not first and second.poll() is None
            assert verified == [peer, peer]
            assert runtime.generation == 2
            wire = [json.loads(line) for line in (tmp_path / "wire.jsonl").read_text().splitlines()]
            assert [r["method"] for r in wire if r["generation"] == 2] == [
                "initialize", "tools/list", "tools/call", "tools/call"]
            assert len([r for r in wire if r["generation"] == 1 and r["method"] == "tools/call"]) == (0 if mode == "capability" else 1)
            assert agent.store.load("failed")[0]["content"].startswith("Correction:")
            assert records[-1] == ("next", "ready", "agent_action")
        finally:
            server.shutdown()
            server.server_close()
    assert first.poll() is not None and second.poll() is not None


def test_research_supervisor_rechecks_the_binary_before_replacement(tmp_path, monkeypatch):
    peer = tmp_path / "sigil-peer"
    peer.write_text(f"#!{sys.executable}\n" + PEER)
    peer.chmod(0o755)
    (tmp_path / "mode").write_text("malformed")
    monkeypatch.setattr(host.toolchain, "verify_binary", lambda path: None)
    with build_runtime(peer) as runtime:
        with pytest.raises(RuntimeError, match="malformed JSON"):
            runtime.forge("llm", timeout_s=3)
        monkeypatch.setattr(host.toolchain, "verify_binary", lambda path: "changed digest")
        with pytest.raises(RuntimeError, match="does not match SIGIL_REV"):
            runtime.forge("llm", timeout_s=3)
        assert runtime.generation == 1
        assert (tmp_path / "generation").read_text() == "1"


def test_expired_capability_reply_never_reaches_provider_endpoint(tmp_path, monkeypatch):
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import runtime_client
    peer = tmp_path / "sigil-peer"
    peer.write_text(f"#!{sys.executable}\n" + PEER)
    peer.chmod(0o755)
    (tmp_path / "mode").write_text("effect")
    hits, outbound = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    class Writer:
        def __init__(self, stream):
            self.stream = stream

        def write(self, line):
            outbound.append(line)
            return self.stream.write(line)

        def __getattr__(self, name):
            return getattr(self.stream, name)

    endpoint = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=endpoint.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{endpoint.server_port}/provider"
    monotonic, loads = time.monotonic, json.loads
    offset = [0]

    def parse(raw):
        result = loads(raw)
        if isinstance(result, dict) and "tools" in result.get("result", {}):
            offset[0] += 2
        return result

    monkeypatch.setattr(runtime_client.time, "monotonic", lambda: monotonic() + offset[0])
    monkeypatch.setattr(runtime_client.json, "loads", parse)
    try:
        with runtime_client.ProductionSigilMCP.spawn(peer) as client:
            client._proc.stdin = Writer(client._proc.stdin)
            client.initialize()
            with pytest.raises(runtime_client.RuntimeClientTimeout):
                client.forge("llm", input=url, grants={"http_timeout_ms": 1000}, timeout_s=1)
            assert [loads(line)["method"] for line in outbound] == ["initialize", "tools/list"]
            assert hits == []
        with runtime_client.ProductionSigilMCP.spawn(peer) as client:
            client.initialize()
            assert client.forge("llm", input=url)["status"] == "ok"
            assert hits == [b"effect"]
    finally:
        endpoint.shutdown()
        endpoint.server_close()
