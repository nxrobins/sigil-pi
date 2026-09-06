"""The product runtime must never inherit the benchmark's verification bypass."""

import json
import subprocess
import time
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
        self.waits = []

    def wait(self, timeout):
        self.waits.append(timeout)
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


def test_forge_declares_the_ephemeral_host_profile_on_every_call(monkeypatch):
    """Since SIGIL's CSIR v9 verifier an undeclared host's tools are refused
    (I013) — the tools were never wrong, the host had not said what it was.
    The declaration must ride EVERY forge, with or without grants, and must be
    the one name sigil-mcp's executor answers to (see HOST_PROFILE)."""
    result = {"content": [{"text": json.dumps({"status": "ok", "data": {}})}]}
    for grants in (None, {"net": ["x"]}):
        client, proc = _client({"jsonrpc": "2.0", "id": 1, "result": result})
        monkeypatch.setattr(
            runtime_client.select, "select", lambda *args, p=proc: ([p.stdout], [], []))
        client.forge("source", grants=grants)
        arguments = json.loads(proc.stdin.writes[0])["params"]["arguments"]
        assert arguments["host_profile"] == "ephemeral" == runtime_client.HOST_PROFILE
        assert ("grants" in arguments) is (grants is not None)


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
    """The RAW client's contract: a killed connection is terminal, by design.
    Recovery is the supervisor's job, not this object's — see
    test_expired_turn_does_not_end_forging_for_other_tenants. This test is
    therefore NOT evidence that the product path survives a kill."""
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
    # Reaped, not merely killed: a zombie per expired turn is a slow leak.
    assert proc.waits, "the elapsed-deadline path must reap the child it killed"


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


# ── generational recovery (the shared compiler survives one tenant) ──────
#
# THE BUG CLASS, reproduced against the real toolchain on 2026-08-23:
# PiAgent._forge hands the product turn's REMAINING deadline to the runtime as
# the protocol timeout (agent.py:1301). On expiry ProductionSigilMCP kills the
# compiler and latches _closed, and there is exactly ONE client per product
# process. So one tenant's expired turn ended forging for EVERY tenant, for the
# life of the process: tenant B's next turn returned 502 agent_failure forever
# while /v1/ready reported not-ready and nothing exited or respawned.
#
# The kill stays — the pinned sigil-mcp has no cancel method, so killing the
# child is the only thing that bounds forge EXECUTION, which is what the hard
# turn deadline promises. What changes is ownership: SupervisedRuntime treats a
# kill as a generation bump rather than a funeral.


class _FakeClock:
    """Monotonic, advanced explicitly. Real sleeps make window tests flaky."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _ScriptedClient:
    """A ProductionSigilMCP-shaped double: scripted forges, a real _proc fake."""

    def __init__(self, script=(), *, handshake=None, on_close=None):
        self.script = list(script)
        self.handshake = handshake
        self.on_close = on_close
        self.initialized_with = []
        self.forges = []
        self.closed = False
        self._proc = _Proc()

    def initialize(self, timeout_s=None):
        self.initialized_with.append(timeout_s)
        if self.handshake is not None:
            raise self.handshake
        return {}

    def forge(self, source, *, input="", fuel=100_000, grants=None, timeout_s=None):
        self.forges.append(timeout_s)
        step = self.script.pop(0) if self.script else {"status": "ok", "data": {}}
        if isinstance(step, Exception):
            raise step
        return step

    def close(self):
        self.closed = True
        if self.on_close is not None:
            raise self.on_close


def _handing_out(*clients):
    """A spawn callable returning each client in turn; records the calls."""
    made = []

    def spawn():
        client = clients[len(made)] if len(made) < len(clients) else _ScriptedClient()
        made.append(client)
        return client

    spawn.made = made
    return spawn


def test_expired_turn_does_not_end_forging_for_other_tenants():
    """THE GUARD for the bug class above. One tenant's expired turn must not be
    able to end forging for a different tenant."""
    killed_a = runtime_client.RuntimeClientTimeout(
        "sigil-mcp did not answer tools/call within 0.5s and was killed",
        budget_expired=True)
    a = _ScriptedClient([{"status": "ok"}, killed_a])   # serves, then is killed
    b = _ScriptedClient([{"status": "ok", "data": {"output_text": "tenant B"}}])
    spawn = _handing_out(a, b)
    with runtime_client.SupervisedRuntime(spawn) as runtime:
        assert runtime.forge("warm", timeout_s=30)["status"] == "ok"
        with pytest.raises(runtime_client.RuntimeClientTimeout):
            runtime.forge("tenant A, last 0.5s of its budget", timeout_s=30)
        # The next tenant is served by a fresh generation, not a corpse.
        assert runtime.forge("tenant B", timeout_s=30)["data"]["output_text"] == "tenant B"
        assert runtime.generation == 2
        assert runtime.is_healthy is True
    assert a._proc.killed is True, "the retired compiler must be reaped"


def test_deadline_kill_retires_generation_without_degrading_readiness():
    """A turn killing its own compiler is EXPECTED traffic, not sickness: the
    deadline did exactly what it promises. Readiness must not flap for it."""
    a = _ScriptedClient([{"status": "ok"}, runtime_client.RuntimeClientTimeout(
        "was killed", budget_expired=True)])
    with runtime_client.SupervisedRuntime(_handing_out(a, _ScriptedClient())) as runtime:
        runtime.forge("served first", timeout_s=30)
        with pytest.raises(runtime_client.RuntimeClientTimeout):
            runtime.forge("expired", timeout_s=30)
        assert runtime.is_healthy is True
        assert runtime.unhealthy_replacements == 0


def test_repeated_unhealthy_replacements_latch_unrecoverable():
    """A compiler that keeps dying for reasons that are NOT a caller's budget is
    sick. Readiness must fail and stay failed — an alternating good/bad compiler
    must not reset the count and hide a broken host forever."""
    clock = _FakeClock()
    broken = runtime_client.RuntimeClientError("sigil-mcp closed its response stream")
    spawn = _handing_out(*[_ScriptedClient([broken]) for _ in range(6)])
    runtime = runtime_client.SupervisedRuntime(
        spawn, unhealthy_budget=3, unhealthy_window_s=120.0, clock=clock)
    with runtime:
        for _ in range(4):
            with pytest.raises(runtime_client.RuntimeClientError):
                runtime.forge("x", timeout_s=30)
            clock.advance(1.0)
        assert runtime.unhealthy_replacements == 4
        assert runtime.is_healthy is False
        # Latched: a later success cannot un-fail readiness.
        with pytest.raises(runtime_client.RuntimeClientError, match="could not be replaced"):
            runtime.forge("x", timeout_s=30)
        assert runtime.is_healthy is False


def test_a_stale_sickness_mark_falls_out_of_the_window():
    """Two unrelated failures a day apart are not a sick compiler."""
    clock = _FakeClock()
    broken = runtime_client.RuntimeClientError("sigil-mcp closed its response stream")
    spawn = _handing_out(*[_ScriptedClient([broken]) for _ in range(4)])
    runtime = runtime_client.SupervisedRuntime(
        spawn, unhealthy_budget=2, unhealthy_window_s=60.0, clock=clock)
    with runtime:
        for _ in range(3):
            with pytest.raises(runtime_client.RuntimeClientError):
                runtime.forge("x", timeout_s=30)
            clock.advance(3600.0)
        assert runtime.is_healthy is True


def test_handshake_failure_at_entry_closes_the_orphan_and_raises():
    """A compiler that starts but never answers initialize must not be left
    running with nobody holding a reference to it."""
    orphan = _ScriptedClient(handshake=runtime_client.RuntimeClientTimeout("no handshake"))
    with pytest.raises(runtime_client.RuntimeClientTimeout):
        with runtime_client.SupervisedRuntime(_handing_out(orphan)):
            pass
    assert orphan._proc.killed is True


@pytest.mark.parametrize("caller_timeout,expected", [
    (0.25, True),    # the caller's own turn budget won the min(): its fault
    (None, False),   # the base watchdog fired: the compiler's fault
    (900.0, False),  # caller had ages left; the base watchdog still won
])
def test_base_watchdog_timeout_is_sickness_and_caller_budget_is_not(
        monkeypatch, caller_timeout, expected):
    """Whose deadline killed the compiler decides whether this is routine or a
    fault. Without this distinction readiness either never fails or always does."""
    proc = _Proc(timeout_on_wait=True)
    client = runtime_client.ProductionSigilMCP(proc, deque(), None, timeout_s=1.0)
    monkeypatch.setattr(runtime_client.select, "select", lambda *args: ([], [], []))
    with pytest.raises(runtime_client.RuntimeClientTimeout) as caught:
        client._request("initialize", timeout_s=caller_timeout)
    assert caught.value.budget_expired is expected


def test_a_caller_with_no_budget_neither_spawns_nor_kills_a_fresh_generation():
    """Spawning a compiler for a caller that cannot wait for the handshake is a
    spawn-kill treadmill: it burns the host and serves nobody."""
    spawn = _handing_out(_ScriptedClient())
    runtime = runtime_client.SupervisedRuntime(spawn, min_forge_budget_s=0.5)
    with pytest.raises(runtime_client.RuntimeClientTimeout, match="budget"):
        runtime.forge("no time left", timeout_s=0.05)
    assert spawn.made == [], "must not spawn a compiler nobody can wait for"
    assert runtime.generation == 0


def test_a_generation_that_outlives_its_spawning_caller_is_kept():
    """If spawn+handshake eats the caller's budget, the caller fails — but the
    compiler it paid for stays for the next one. Killing it would guarantee that
    a busy host never keeps a compiler."""
    clock = _FakeClock()
    fresh = _ScriptedClient([{"status": "ok", "data": {"output_text": "next caller"}}])

    def spawn():
        clock.advance(5.0)          # a slow, real spawn
        return fresh

    runtime = runtime_client.SupervisedRuntime(
        spawn, min_forge_budget_s=0.5, clock=clock)
    with pytest.raises(runtime_client.RuntimeClientTimeout):
        runtime.forge("pays for the spawn", timeout_s=2.0)
    assert fresh._proc.killed is False, "the fresh generation must be kept, not killed"
    assert runtime.generation == 1
    assert runtime.forge("next caller", timeout_s=30)["data"]["output_text"] == "next caller"


def test_a_disposal_that_raises_still_retires_the_generation():
    """Reaping is best-effort; failing to reap must never wedge the host again."""
    class _Stubborn(_ScriptedClient):
        def __init__(self):
            super().__init__([runtime_client.RuntimeClientError("stream")])
            self._proc = _Proc(timeout_on_wait=True)

        def close(self):
            raise subprocess.TimeoutExpired("sigil-mcp", 5)

    stubborn = _Stubborn()
    stubborn._proc.kill = lambda: (_ for _ in ()).throw(OSError("no such process"))
    with runtime_client.SupervisedRuntime(_handing_out(stubborn, _ScriptedClient())) as runtime:
        with pytest.raises(runtime_client.RuntimeClientError):
            runtime.forge("x", timeout_s=30)
        assert runtime.forge("recovered", timeout_s=30)["status"] == "ok"


@pytest.mark.parametrize("error,retires", [
    (runtime_client.RuntimeProtocolError("sigil-mcp returned a protocol error"), False),
    (runtime_client.RuntimeProtocolError("sigil-mcp response has no result"), False),
    (runtime_client.RuntimeClientError("sigil-mcp returned malformed JSON"), True),
    (runtime_client.RuntimeClientError("sigil-mcp returned a mismatched response"), True),
    (runtime_client.RuntimeClientError("sigil-mcp closed its response stream"), True),
])
def test_protocol_desync_retires_but_a_well_formed_error_does_not(error, retires):
    """A well-formed error consumed its frame and left the stream in position —
    throwing the compiler away for it is pure churn. A desync did not, and the
    next caller would read someone else's response."""
    first = _ScriptedClient([error, {"status": "ok"}])
    with runtime_client.SupervisedRuntime(_handing_out(first, _ScriptedClient())) as runtime:
        with pytest.raises(runtime_client.RuntimeClientError):
            runtime.forge("x", timeout_s=30)
        runtime.forge("y", timeout_s=30)
    assert runtime.generation == (2 if retires else 1)


def test_close_returns_while_a_forge_is_in_flight():
    """Shutdown must not hang behind a forge that is mid-select. The drain has a
    bounded timeout and product_main must not block past it."""
    import threading as _threading
    started, release = _threading.Event(), _threading.Event()

    class _Blocking(_ScriptedClient):
        def forge(self, source, **kwargs):
            started.set()
            release.wait(5)
            return {"status": "ok"}

    blocking = _Blocking()
    runtime = runtime_client.SupervisedRuntime(
        _handing_out(blocking), close_timeout_s=0.2)
    runtime.__enter__()
    worker = _threading.Thread(target=lambda: runtime.forge("slow", timeout_s=30))
    worker.start()
    assert started.wait(5)
    began = time.monotonic()
    runtime.close()
    assert time.monotonic() - began < 2.0, "close() blocked behind an in-flight forge"
    assert blocking._proc.killed is True, "the in-flight compiler must be killed out of band"
    release.set()
    worker.join(5)


def test_is_healthy_does_not_block_behind_an_active_forge():
    """Readiness is polled by a supervisor; it must never queue behind a turn."""
    import threading as _threading
    started, release = _threading.Event(), _threading.Event()

    class _Blocking(_ScriptedClient):
        def forge(self, source, **kwargs):
            started.set()
            release.wait(5)
            return {"status": "ok"}

    runtime = runtime_client.SupervisedRuntime(_handing_out(_Blocking()))
    with runtime:
        worker = _threading.Thread(target=lambda: runtime.forge("slow", timeout_s=30))
        worker.start()
        assert started.wait(5)
        began = time.monotonic()
        assert runtime.is_healthy is True
        assert time.monotonic() - began < 0.5
        release.set()
        worker.join(5)


def test_a_closed_supervisor_refuses_to_forge():
    runtime = runtime_client.SupervisedRuntime(_handing_out(_ScriptedClient()))
    with runtime:
        runtime.forge("x", timeout_s=30)
    with pytest.raises(runtime_client.RuntimeClientError, match="closed"):
        runtime.forge("after close", timeout_s=30)


def test_a_first_forge_killed_by_its_callers_budget_is_not_sickness():
    """Regression on the classification rule itself.

    Requiring a generation to have served something before its budget-kill
    counts as benign is a trap: on a busy host the forge that gets killed is
    routinely a fresh generation's first, so a handful of expired turns inside
    the window would latch `_unrecoverable` and fail readiness permanently with
    no compiler at fault — reinventing the outage this class exists to prevent.
    Whose clock ran out is the whole test.
    """
    clock = _FakeClock()
    killed = runtime_client.RuntimeClientTimeout("was killed", budget_expired=True)
    spawn = _handing_out(*[_ScriptedClient([killed]) for _ in range(8)])
    runtime = runtime_client.SupervisedRuntime(
        spawn, unhealthy_budget=3, unhealthy_window_s=120.0, clock=clock)
    with runtime:
        for _ in range(6):
            with pytest.raises(runtime_client.RuntimeClientTimeout):
                runtime.forge("a turn that outran its budget", timeout_s=30)
            clock.advance(1.0)
        assert runtime.unhealthy_replacements == 0
        assert runtime.is_healthy is True, (
            "impatient callers must never be able to mark the host unrecoverable")


def test_the_elapsed_deadline_reap_tolerates_a_child_that_will_not_die():
    """Reaping is best effort. A child that ignores SIGKILL long enough to
    outlast the wait must not turn an expired turn into an exception from the
    cleanup path — the caller's own timeout is the answer it needs."""
    class _Stuck(_Proc):
        def wait(self, timeout):
            self.waits.append(timeout)
            raise subprocess.TimeoutExpired("sigil-mcp", timeout)

    proc = _Stuck()
    client = runtime_client.ProductionSigilMCP(proc, deque(), None, timeout_s=5)
    with pytest.raises(runtime_client.RuntimeClientTimeout, match="elapsed before"):
        client._request("initialize", timeout_s=0)
    assert proc.killed is True and proc.waits


def test_a_spawn_racing_a_close_is_reaped_not_orphaned():
    """A drain that lands while a replacement is being spawned must not leave a
    compiler running with nobody holding a reference to it. close() cannot take
    the lock here — the forge holds it — so the racing spawn checks for itself."""
    fresh = _ScriptedClient()
    box = {}

    def spawn():
        box["runtime"].close()      # the drain arrives mid-spawn
        return fresh

    runtime = runtime_client.SupervisedRuntime(spawn, close_timeout_s=0.05)
    box["runtime"] = runtime
    with pytest.raises(runtime_client.RuntimeClientError, match="closed"):
        runtime.forge("x", timeout_s=30)
    assert fresh._proc.killed is True, "the racing generation must be reaped"
    assert runtime._client is None
