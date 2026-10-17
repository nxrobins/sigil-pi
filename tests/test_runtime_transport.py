"""Deadline and framing behavior against isolated compiler processes."""
import json
import sys
import time
from collections import deque

import pytest

from runtime_client import ProductionSigilMCP, RuntimeClientTimeout, SupervisedRuntime
from test_runtime_client import _FakeClock, _Proc


PEER = r'''
import json, pathlib, sys, time
root = pathlib.Path(__file__).parent
counter = root / "generation"
generation = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(generation))
mode = (root / "mode").read_text()
for line in sys.stdin:
    req = json.loads(line)
    method = req["method"]
    with (root / "wire.jsonl").open("a") as log:
        log.write(json.dumps({"generation": generation, "method": method,
                              "source": req["params"].get("arguments", {}).get("source")}) + "\n")
    if method == "initialize":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": "sigil_forge", "inputSchema": {"properties": {
            "grants": {"properties": {"http_timeout_ms": {"type": "integer"}}}}}}]}
    else:
        result = {"content": [{"text": json.dumps({"status": "ok"})}]}
    response = json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}) + "\n"
    if generation == 1:
        if mode == "handshake" and method == "initialize":
            time.sleep(1.2)
        if mode == "capability" and method == "tools/list":
            time.sleep(0.10)
        if method == "tools/call":
            if mode in ("partial", "capability"):
                sys.stdout.write(response[:5]); sys.stdout.flush()
                time.sleep(1.2)
                response = response[5:]
            elif mode == "dribble":
                for char in response:
                    sys.stdout.write(char); sys.stdout.flush()
                    time.sleep(0.03)
                continue
            elif mode == "header":
                time.sleep(1.2)
    sys.stdout.write(response); sys.stdout.flush()
    if generation == 1 and mode == "blocked" and method == "initialize":
        time.sleep(1.2)
'''


@pytest.mark.parametrize("mode", ["blocked", "partial", "dribble", "header", "handshake", "capability"])
def test_absolute_pipe_deadline_and_next_generation_recovery(tmp_path, mode):
    peer = tmp_path / "compiler"
    peer.write_text(f"#!{sys.executable}\n" + PEER)
    peer.chmod(0o755)
    (tmp_path / "mode").write_text(mode)
    clients = []

    def spawn():
        client = ProductionSigilMCP.spawn(peer)
        clients.append(client)
        return client

    runtime = SupervisedRuntime(spawn, min_forge_budget_s=0.01)
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeClientTimeout) as error:
            runtime.forge("failed", input="x" * (1024 * 1024) if mode == "blocked" else "",
                          grants={"http_timeout_ms": 1000} if mode == "capability" else None,
                          timeout_s=0.5)
        assert error.value.budget_expired
        assert time.monotonic() - started < 0.9
        assert runtime._client is None
        clients[0]._reader.join(2)
        assert not clients[0]._reader.is_alive()
        assert clients[0]._proc.poll() is not None
        assert runtime.forge("next", timeout_s=2) == {"status": "ok"}
        wire = [json.loads(row) for row in (tmp_path / "wire.jsonl").read_text().splitlines()]
        assert [r["source"] for r in wire if r["generation"] == 2] == [None, "next"]
        assert len([r for r in wire if r["source"] == "failed"]) == (0 if mode in ("blocked", "handshake") else 1)
        assert runtime.unhealthy_replacements == 0
    finally:
        runtime.close()


@pytest.mark.parametrize("phase", ["serialization", "write_readiness", "read_readiness", "parsing"])
def test_elapsed_processing_cannot_dispatch_or_accept_a_late_response(monkeypatch, phase):
    import runtime_client
    clock = _FakeClock()
    proc = _Proc([json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}) + "\n"])
    client = ProductionSigilMCP(proc, deque(), None)
    dumps, loads = json.dumps, json.loads
    monkeypatch.setattr(runtime_client.time, "monotonic", clock)

    def serialize(*args, **kwargs):
        value = dumps(*args, **kwargs)
        if phase == "serialization":
            clock.advance(1)
        return value

    def parse(*args):
        value = loads(*args)
        if phase == "parsing":
            clock.advance(1)
        return value

    def ready(readable, writable, *args):
        if (phase == "write_readiness" and writable) or (phase == "read_readiness" and readable):
            clock.advance(1)
        return readable, writable, []

    monkeypatch.setattr(runtime_client.json, "dumps", serialize)
    monkeypatch.setattr(runtime_client.json, "loads", parse)
    monkeypatch.setattr(runtime_client.select, "select", ready)
    with pytest.raises(RuntimeClientTimeout):
        client.initialize(timeout_s=1)
    assert len(proc.stdin.writes) == (1 if phase in ("read_readiness", "parsing") else 0)
    assert proc.killed and not client.is_healthy


def test_partial_nonblocking_io_reuses_one_deadline(monkeypatch):
    import runtime_client
    clock = _FakeClock()
    proc = _Proc()
    client = ProductionSigilMCP(proc, deque(), None)
    written, waits = [], []
    chunks = iter([None, b'{"jsonrpc":"2.0",', b'"id":1,"result":{}}\n'])

    def write(data):
        written.append(bytes(data))
        return None if len(written) == 1 else min(4, len(data))

    def ready(r, w, _, timeout):
        waits.append(timeout)
        clock.advance(0.01)
        return r, w, []

    proc.stdin.write = write
    proc.stdout.read = lambda size: next(chunks)
    monkeypatch.setattr(runtime_client.time, "monotonic", clock)
    monkeypatch.setattr(runtime_client.select, "select", ready)
    assert client.initialize(timeout_s=1) == {}
    assert written[0] == written[1]
    assert all(a > b for a, b in zip(waits, waits[1:]))
    assert proc.stdin.flushes == 0
