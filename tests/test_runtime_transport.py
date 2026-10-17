"""Deadline and framing behavior against isolated compiler processes."""
import json
import socket
import sys
from collections import deque

import pytest

from runtime_client import ProductionSigilMCP, RuntimeClientTimeout, SupervisedRuntime
from test_runtime_client import _FakeClock, _Proc


PEER = r'''
import json, pathlib, socket, sys
root = pathlib.Path(__file__).parent
counter = root / "generation"
generation = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(generation))
mode = (root / "mode").read_text()
control = socket.create_connection(("127.0.0.1", int((root / "control-port").read_text())))
control.settimeout(5)
control.sendall(b"ready\n")


def phase(name):
    control.sendall((name + "\n").encode())


def send(text):
    sys.stdout.write(text)
    sys.stdout.flush()


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
            phase("handshake")
            control.recv(1)
            break
        if method == "tools/call":
            if mode in ("partial", "capability"):
                send(response[:5])
                phase("partial")
                control.recv(1)
                break
            if mode == "dribble":
                send(response[0])
                phase("dribble")
                for char in response[1:]:
                    if not control.recv(1):
                        break
                    send(char)
                continue
            if mode == "header":
                phase("header")
                control.recv(1)
                break
    send(response)
    if generation == 1 and mode == "blocked" and method == "initialize":
        phase("blocked")
        control.recv(1)
        break
    phase("capability" if generation == 1 and method == "tools/list" else "response")
'''


class _PeerTransport:
    def __init__(self, tmp_path, monkeypatch, mode):
        import runtime_client
        self.clock = _FakeClock()
        self.clients, self.controls, self.phases = [], [], []
        self.writes, self.reads, self.waits = [], [], []
        self.mode = mode
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(5)
        self.peer = tmp_path / "compiler"
        self.peer.write_text(f"#!{sys.executable}\n" + PEER)
        self.peer.chmod(0o755)
        (tmp_path / "mode").write_text(mode)
        (tmp_path / "control-port").write_text(str(self.listener.getsockname()[1]))
        select = runtime_client.select.select

        def wait(readable, writable, errors, timeout):
            self.waits.append(timeout)
            result = select(readable, writable, errors, timeout)
            if not result[0] and not result[1]:
                self.clock.advance(timeout)
            return result

        monkeypatch.setattr(runtime_client.time, "monotonic", self.clock)
        monkeypatch.setattr(runtime_client.select, "select", wait)
        self.runtime = SupervisedRuntime(self.spawn, min_forge_budget_s=0.01, clock=self.clock)

    def spawn(self):
        client = ProductionSigilMCP.spawn(self.peer)
        self.clients.append(client)
        control, _ = self.listener.accept()
        control.settimeout(5)
        messages = control.makefile("rb", buffering=0)
        self.controls.append((control, messages))
        assert messages.readline() == b"ready\n"
        generation = len(self.clients)
        self.phases.append((generation, "ready"))
        transport = self

        class Input:
            def __init__(self, stream):
                self.stream = stream
                self.phase = None

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def write(self, data):
                written = self.stream.write(data)
                if written:
                    transport.writes.append((generation, bytes(data[:written])))
                    if bytes(data[:written]).endswith(b"\n"):
                        self.phase = messages.readline().decode().strip()
                        assert self.phase
                        transport.phases.append((generation, self.phase))
                return written

        class Output:
            def __init__(self, stream):
                self.stream = stream

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def read(self, size):
                data = self.stream.read(size)
                if data:
                    transport.reads.append((generation, client._proc.stdin.phase, data))
                    if generation == 1 and client._proc.stdin.phase in ("partial", "dribble", "capability"):
                        transport.clock.advance(0.125)
                        if client._proc.stdin.phase == "dribble":
                            control.sendall(b"x")
                return data

        client._proc.stdin = Input(client._proc.stdin)
        client._proc.stdout = Output(client._proc.stdout)
        if self.mode == "startup" and generation == 1:
            self.clock.advance(0.5)
        return client

    def close(self):
        self.runtime.close()
        for client in self.clients:
            client.close()
        for control, messages in self.controls:
            messages.close()
            control.close()
        self.listener.close()


def _assert_expiry_and_recovery(transport, tmp_path, mode, budget_form):
    started = transport.clock()
    wait_start = len(transport.waits)
    with pytest.raises(RuntimeClientTimeout) as error:
        transport.runtime.forge("failed", input="x" * (1024 * 1024) if mode == "blocked" else "",
                                grants={"http_timeout_ms": 1000} if mode == "capability" else None,
                                **({"timeout_s": 0.5} if budget_form == "relative" else
                                   {"deadline_monotonic": started + 0.5}))
    assert error.value.budget_expired
    assert transport.clock() - started == pytest.approx(0.5)
    waits = transport.waits[wait_start:]
    assert all(0 < timeout <= 0.5 for timeout in waits)
    if mode != "startup":
        assert waits
    assert transport.runtime._client is None
    client = transport.clients[0]
    client._reader.join(2)
    assert not client._reader.is_alive()
    assert client._proc.poll() is not None
    phases = [phase for generation, phase in transport.phases if generation == 1]
    assert phases == {
        "startup": ["ready"], "handshake": ["ready", "handshake"],
        "blocked": ["ready", "blocked"], "partial": ["ready", "response", "partial"],
        "header": ["ready", "response", "header"], "dribble": ["ready", "response", "dribble"],
        "capability": ["ready", "response", "capability", "partial"],
    }[mode]
    fragments = [data for generation, phase, data in transport.reads
                 if generation == 1 and phase in ("partial", "dribble")]
    if mode in ("partial", "capability"):
        assert fragments == [b'{"jso']
    elif mode == "dribble":
        assert len(fragments) == 4 and all(len(chunk) == 1 for chunk in fragments)
    else:
        assert fragments == []
    if mode == "blocked":
        sent = b"".join(data for generation, data in transport.writes if generation == 1)
        assert b'"source":"failed"' in sent
        assert 0 < len(sent.split(b"\n", 1)[1]) < 1024 * 1024
    assert transport.runtime.forge("next", timeout_s=2) == {"status": "ok"}
    wire = [json.loads(row) for row in (tmp_path / "wire.jsonl").read_text().splitlines()]
    assert [r["source"] for r in wire if r["generation"] == 2] == [None, "next"]
    assert len([r for r in wire if r["source"] == "failed"]) == (0 if mode in ("blocked", "handshake", "startup") else 1)
    assert (tmp_path / "generation").read_text() == "2"
    assert transport.runtime.unhealthy_replacements == 0


@pytest.fixture
def transport_peer(tmp_path, monkeypatch, mode):
    transport = _PeerTransport(tmp_path, monkeypatch, mode)
    try:
        yield transport
    finally:
        transport.close()


@pytest.mark.parametrize("mode", ["blocked", "partial", "dribble", "header"])
@pytest.mark.parametrize("budget_form", ["relative", "absolute"])
def test_absolute_pipe_deadline_and_next_generation_recovery(tmp_path, transport_peer, mode, budget_form):
    transport_peer.runtime.__enter__()
    _assert_expiry_and_recovery(transport_peer, tmp_path, mode, budget_form)


@pytest.mark.parametrize("mode", ["startup", "handshake", "capability"])
@pytest.mark.parametrize("budget_form", ["relative", "absolute"])
def test_startup_and_negotiation_consume_the_original_pipe_budget(tmp_path, transport_peer, mode, budget_form):
    _assert_expiry_and_recovery(transport_peer, tmp_path, mode, budget_form)


@pytest.mark.parametrize("phase", ["serialization", "write_readiness", "read_readiness", "parsing"])
@pytest.mark.parametrize("budget_form", ["relative", "absolute"])
def test_elapsed_processing_cannot_dispatch_or_accept_a_late_response(monkeypatch, phase, budget_form):
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
        client.initialize(**({"timeout_s": 1} if budget_form == "relative" else
                             {"deadline_monotonic": clock() + 1}))
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
    assert client.initialize(deadline_monotonic=clock() + 1) == {}
    assert written[0] == written[1]
    assert all(a > b for a, b in zip(waits, waits[1:]))
    assert proc.stdin.flushes == 0


@pytest.mark.parametrize("phase", ["before_capability", "before_tool"])
def test_scheduling_between_forge_phases_never_renews_the_deadline(monkeypatch, phase):
    import runtime_client
    clock = _FakeClock()
    schema = {"tools": [{"name": "sigil_forge", "inputSchema": {"properties": {
        "grants": {"properties": {"http_timeout_ms": {"type": "integer"}}}}}}]}
    result = schema if phase == "before_capability" else {"content": [{"text": '{"status":"ok"}'}]}
    proc = _Proc([json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}) + "\n"])
    client = ProductionSigilMCP(proc, deque(), None)
    tool = client._tool

    class Grants(dict):
        def __contains__(self, key):
            if phase == "before_capability":
                clock.advance(1)
            return super().__contains__(key)

    def delayed_tool(*args, **kwargs):
        clock.advance(1)
        return tool(*args, **kwargs)

    if phase == "before_tool":
        client._http_budget_supported = True
        client._tool = delayed_tool
    monkeypatch.setattr(runtime_client.time, "monotonic", clock)
    monkeypatch.setattr(runtime_client.select, "select", lambda r, w, *args: (r, w, []))
    with pytest.raises(RuntimeClientTimeout) as error:
        client.forge("stateful", grants=Grants(http_timeout_ms=1000), timeout_s=1)
    assert error.value.budget_expired
    assert proc.stdin.writes == []
    assert proc.killed
