"""The product runtime must never inherit the benchmark's verification bypass."""

import json
import subprocess
from collections import deque
from types import SimpleNamespace

import pytest

import runtime_client


class _Stream:
    closed = False

    def __iter__(self):
        return iter(())

    def close(self):
        self.closed = True


def test_product_spawn_strips_unverified_certificate_override(tmp_path, monkeypatch):
    binary = tmp_path / "sigil-mcp"
    binary.write_bytes(b"placeholder")
    monkeypatch.setenv("SIGIL_ALLOW_UNVERIFIED_CERT", "1")
    captured = {}
    proc = SimpleNamespace(stdin=_Stream(), stdout=_Stream(), stderr=["diagnostic\n"])

    def popen(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return proc

    monkeypatch.setattr(runtime_client.subprocess, "Popen", popen)
    client = runtime_client.ProductionSigilMCP.spawn(binary)
    client._reader.join(1)
    assert captured["args"] == [str(binary)]
    assert "SIGIL_ALLOW_UNVERIFIED_CERT" not in captured["env"]
    assert client._timeout_s == 90.0
    assert client.stderr_log == ["diagnostic"]


def test_product_runtime_has_a_positive_mcp_deadline(tmp_path):
    binary = tmp_path / "sigil-mcp"
    binary.write_bytes(b"placeholder")
    try:
        runtime_client.ProductionSigilMCP.spawn(binary, timeout_s=0)
    except ValueError as error:
        assert "positive" in str(error)
    else:
        raise AssertionError("zero MCP deadline was accepted")


class _ProtocolStream:
    def __init__(self, lines=(), *, write_error=False, close_error=False):
        self.lines = deque(lines)
        self.writes = []
        self.closed = False
        self.write_error = write_error
        self.close_error = close_error

    def write(self, value):
        if self.write_error:
            raise BrokenPipeError("closed")
        self.writes.append(value)

    def flush(self):
        return None

    def readline(self):
        return self.lines.popleft() if self.lines else ""

    def close(self):
        if self.close_error:
            raise BrokenPipeError("closed")
        self.closed = True


class _Proc:
    def __init__(self, lines=(), *, write_error=False, timeout_on_wait=False):
        self.stdin = _ProtocolStream(write_error=write_error)
        self.stdout = _ProtocolStream(lines)
        self.stderr = _ProtocolStream()
        self.timeout_on_wait = timeout_on_wait
        self.killed = False

    def wait(self, timeout):
        if self.timeout_on_wait and not self.killed:
            raise subprocess.TimeoutExpired("sigil-mcp", timeout)
        return 0

    def kill(self):
        self.killed = True

    def poll(self):
        return 1 if self.killed else None


def _client(response, **proc_kwargs):
    proc = _Proc([json.dumps(response) + "\n"], **proc_kwargs)
    return runtime_client.ProductionSigilMCP(proc, deque(maxlen=2), None, timeout_s=1), proc


def test_protocol_success_forge_and_context_close(monkeypatch):
    result = {"content": [{"text": json.dumps({"status": "ok", "data": {}})}]}
    client, proc = _client({"jsonrpc": "2.0", "id": 1, "result": result})
    monkeypatch.setattr(runtime_client.select, "select", lambda *args: ([proc.stdout], [], []))
    with client as entered:
        assert entered.forge("source", input="value", fuel=7, grants={"net": ["x"]}) == {
            "status": "ok", "data": {}}
    request = json.loads(proc.stdin.writes[0])
    assert request["method"] == "tools/call"
    assert request["params"]["arguments"]["grants"] == {"net": ["x"]}
    assert proc.stdin.closed is True


@pytest.mark.parametrize("response,match", [
    ("not-json\n", "malformed JSON"),
    (json.dumps({"jsonrpc": "2.0", "id": 99, "result": {}}) + "\n", "mismatched"),
    (json.dumps({"jsonrpc": "2.0", "id": 1, "error": {}}) + "\n", "protocol error"),
    (json.dumps({"jsonrpc": "2.0", "id": 1}) + "\n", "no result"),
    ("", "closed its response stream"),
])
def test_protocol_rejects_invalid_responses(monkeypatch, response, match):
    proc = _Proc([response] if response else [])
    client = runtime_client.ProductionSigilMCP(proc, deque(), None, timeout_s=1)
    monkeypatch.setattr(runtime_client.select, "select", lambda *args: ([proc.stdout], [], []))
    with pytest.raises(runtime_client.RuntimeClientError, match=match):
        client.initialize()


def test_protocol_deadline_kills_runtime(monkeypatch):
    proc = _Proc(timeout_on_wait=True)
    client = runtime_client.ProductionSigilMCP(proc, deque(), None, timeout_s=0.25)
    monkeypatch.setattr(runtime_client.select, "select", lambda *args: ([], [], []))
    with pytest.raises(runtime_client.RuntimeClientTimeout, match="was killed"):
        client.initialize()
    assert proc.killed is True and client._closed is True


def test_request_deadline_override_and_health_are_enforced(monkeypatch):
    client, proc = _client({"jsonrpc": "2.0", "id": 1, "result": {}})
    observed = []

    def ready(*args):
        observed.append(args[-1])
        return [proc.stdout], [], []

    monkeypatch.setattr(runtime_client.select, "select", ready)
    assert client.is_healthy is True
    assert client._request("initialize", timeout_s=0.25) == {}
    assert observed == [0.25]
    client.close()
    assert client.is_healthy is False


def test_elapsed_request_deadline_kills_without_waiting(monkeypatch):
    proc = _Proc()
    client = runtime_client.ProductionSigilMCP(proc, deque(), None, timeout_s=5)
    called = []
    monkeypatch.setattr(runtime_client.select, "select", lambda *args: called.append(args))
    with pytest.raises(runtime_client.RuntimeClientTimeout, match="elapsed before"):
        client._request("initialize", timeout_s=0)
    assert proc.killed is True and called == []


def test_protocol_write_failure_closed_client_and_invalid_tool_result(monkeypatch):
    broken = _Proc(write_error=True)
    client = runtime_client.ProductionSigilMCP(broken, deque(), None)
    with pytest.raises(runtime_client.RuntimeClientError, match="failed to write"):
        client.initialize()
    client._closed = True
    with pytest.raises(runtime_client.RuntimeClientError, match="closed"):
        client.initialize()

    valid_response = {"jsonrpc": "2.0", "id": 1,
                      "result": {"content": [{"text": "not-json"}]}}
    invalid, proc = _client(valid_response)
    monkeypatch.setattr(runtime_client.select, "select", lambda *args: ([proc.stdout], [], []))
    with pytest.raises(runtime_client.RuntimeClientError, match="invalid sigil_forge"):
        invalid.forge("source")


def test_close_kills_process_that_does_not_exit_and_is_idempotent():
    proc = _Proc(timeout_on_wait=True)
    client = runtime_client.ProductionSigilMCP(proc, deque(["diagnostic"]), None)
    assert client.stderr_log == ["diagnostic"]
    client.close()
    client.close()
    assert proc.killed is True


def test_close_contains_stdin_close_failure_and_missing_stream():
    proc = _Proc()
    proc.stdin = _ProtocolStream(close_error=True)
    client = runtime_client.ProductionSigilMCP(proc, deque(), None)
    client.close()
    assert client._closed is True

    no_stdin = _Proc()
    no_stdin.stdin = None
    runtime_client.ProductionSigilMCP(no_stdin, deque(), None).close()


def test_timeout_contains_process_wait_failure(monkeypatch):
    proc = _Proc()

    def never_reaps(timeout):
        raise subprocess.TimeoutExpired("sigil-mcp", timeout)

    proc.wait = never_reaps
    client = runtime_client.ProductionSigilMCP(proc, deque(), None, timeout_s=0.01)
    monkeypatch.setattr(runtime_client.select, "select", lambda *args: ([], [], []))
    with pytest.raises(runtime_client.RuntimeClientTimeout, match="was killed"):
        client.initialize()
    assert proc.killed is True and client._closed is True


def test_spawn_requires_existing_binary(tmp_path):
    with pytest.raises(FileNotFoundError, match="not found"):
        runtime_client.ProductionSigilMCP.spawn(tmp_path / "missing")


SOLVER_PROBE = """
pub fn tool_main(input_ptr: i64, input_len: i64) -> i64 ! { Alloc } {
    return esc_json(input_ptr, input_len);
}
"""


def test_the_resolved_compiler_verifies_with_the_solver(mcp):
    """The session `mcp` fixture IS the product client: it strips the
    benchmark-only SIGIL_ALLOW_UNVERIFIED_CERT override. Against a solver-off
    compiler every forge then fails closed with R817, which is what happened
    to the whole suite on 2026-08-22 — so name that failure here, in one
    place, rather than as 200 identical tracebacks."""
    from conftest import build_probe
    r = mcp.forge(build_probe(SOLVER_PROBE), input="x", fuel=20_000_000)
    codes = [d.get("code") for d in (r.get("diagnostics") or [])]
    assert "R817" not in codes, (
        "the resolved sigil-mcp is a solver-OFF build: rebuild it with "
        "--features sigil-mcp/solver (ci.sh step 1 does) — the product client "
        "never sets SIGIL_ALLOW_UNVERIFIED_CERT, so this binary cannot forge")
    assert r.get("status") == "ok", r
