"""Ancestor deadlines survive scheduling pauses across runtime ownership changes."""
import json
from collections import deque

import pytest

import runtime_client
from test_runtime_client import _FakeClock, _Proc


SCHEMA = {"tools": [{"name": "sigil_forge", "inputSchema": {"properties": {
    "grants": {"properties": {"http_timeout_ms": {"type": "integer"}}}}}}]}


def _wire_client():
    results = [{}, SCHEMA, {"content": [{"text": '{"status":"ok"}'}]}]
    proc = _Proc([json.dumps({"jsonrpc": "2.0", "id": i, "result": result}) + "\n"
                  for i, result in enumerate(results, 1)])
    return runtime_client.ProductionSigilMCP(proc, deque(), None), proc


@pytest.mark.parametrize("phase", ["supervisor_lock", "supervisor_handoff", "before_spawn",
                                    "after_spawn", "initialize_handoff", "initialize_request",
                                    "before_capability", "after_capability", "before_tool"])
def test_handoffs_never_extend_the_original_deadline(monkeypatch, phase):
    clock = _FakeClock()
    monkeypatch.setattr(runtime_client.time, "monotonic", clock)
    monkeypatch.setattr(runtime_client.select, "select", lambda r, w, *args: (r, w, []))
    clients, procs = [], []
    paused = []

    def pause():
        if not paused:
            paused.append(True)
            clock.advance(1.01)

    def spawn():
        client, proc = _wire_client()
        clients.append(client)
        procs.append(proc)
        if len(clients) == 1:
            initialize, request, forge, tool = client.initialize, client._request, client.forge, client._tool

            def initialize_late(*args, **kwargs):
                if phase == "initialize_handoff":
                    pause()
                return initialize(*args, **kwargs)

            def request_late(method, *args, **kwargs):
                if (phase == "initialize_request" and method == "initialize") or (
                        phase == "before_capability" and method == "tools/list"):
                    pause()
                result = request(method, *args, **kwargs)
                if phase == "after_capability" and method == "tools/list":
                    pause()
                return result

            def forge_late(*args, **kwargs):
                if phase == "supervisor_handoff":
                    pause()
                return forge(*args, **kwargs)

            def tool_late(*args, **kwargs):
                if phase == "before_tool":
                    pause()
                return tool(*args, **kwargs)

            client.initialize, client._request = initialize_late, request_late
            client.forge, client._tool = forge_late, tool_late
            if phase == "after_spawn":
                pause()
        return client

    runtime = runtime_client.SupervisedRuntime(spawn, clock=clock, min_forge_budget_s=0.01)
    if phase == "before_spawn":
        start = runtime._start_generation

        def start_late(deadline):
            pause()
            return start(deadline)

        runtime._start_generation = start_late
    if phase == "supervisor_lock":
        lock = runtime._lock

        class DelayedLock:
            def acquire(self, *args, **kwargs):
                result = lock.acquire(*args, **kwargs)
                pause()
                return result

            def release(self):
                lock.release()

        runtime._lock = DelayedLock()
    try:
        with pytest.raises(runtime_client.RuntimeClientTimeout) as error:
            runtime.forge("expired effect", grants={"http_timeout_ms": 1000},
                          deadline_monotonic=clock() + 1)
        assert error.value.budget_expired
        wire = [json.loads(line) for proc in procs for line in proc.stdin.writes]
        expected = (["initialize", "tools/list"] if phase in ("after_capability", "before_tool") else
                    ["initialize"] if phase in ("supervisor_handoff", "before_capability") else [])
        assert [frame["method"] for frame in wire] == expected
        assert all(proc.killed for proc in procs)
        assert runtime.is_healthy and runtime.unhealthy_replacements == 0
        assert runtime.forge("fresh effect", grants={"http_timeout_ms": 1000},
                             deadline_monotonic=clock() + 5) == {"status": "ok"}
        effects = [json.loads(line)["params"]["arguments"]["source"]
                   for proc in procs for line in proc.stdin.writes
                   if json.loads(line)["method"] == "tools/call"]
        assert effects == ["fresh effect"]
    finally:
        runtime.close()


@pytest.mark.parametrize("boundary", ["request", "initialize", "forge", "supervisor"])
@pytest.mark.parametrize("relative,absolute", [(0, 1010), (10, 1000)])
def test_combined_relative_and_absolute_budgets_keep_the_earliest(monkeypatch, boundary, relative, absolute):
    clock = _FakeClock()
    monkeypatch.setattr(runtime_client.time, "monotonic", clock)
    client, proc = _wire_client()
    runtime = runtime_client.SupervisedRuntime(lambda: client, clock=clock)
    try:
        with pytest.raises(runtime_client.RuntimeClientTimeout) as error:
            kwargs = {"timeout_s": relative, "deadline_monotonic": absolute}
            if boundary == "request":
                client._request("initialize", **kwargs)
            elif boundary == "initialize":
                client.initialize(**kwargs)
            elif boundary == "forge":
                client.forge("effect", **kwargs)
            else:
                runtime.forge("effect", **kwargs)
        assert error.value.budget_expired
        assert proc.stdin.writes == []
        assert runtime.unhealthy_replacements == 0
    finally:
        runtime.close()


@pytest.mark.parametrize("caller,watchdog,expected", [(1000, 1010, True), (1010, 1000, False),
                                                     (None, 1000, False), (1000, 1000, True)])
def test_handshake_watchdog_and_caller_expiry_remain_distinct(monkeypatch, caller, watchdog, expected):
    clock = _FakeClock()
    monkeypatch.setattr(runtime_client.time, "monotonic", clock)
    client, proc = _wire_client()
    with pytest.raises(runtime_client.RuntimeClientTimeout) as error:
        client.initialize(deadline_monotonic=caller, watchdog_deadline_monotonic=watchdog)
    assert error.value.budget_expired is expected
    assert proc.stdin.writes == [] and proc.killed


def test_slow_replacement_spawn_consumes_the_handshake_watchdog(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(runtime_client.time, "monotonic", clock)
    client, proc = _wire_client()

    def spawn():
        clock.advance(6)
        return client

    runtime = runtime_client.SupervisedRuntime(spawn, clock=clock)
    try:
        with pytest.raises(runtime_client.RuntimeClientTimeout) as error:
            runtime.forge("effect", deadline_monotonic=clock() + 20)
        assert not error.value.budget_expired
        assert proc.stdin.writes == [] and proc.killed
        assert runtime.unhealthy_replacements == 1
    finally:
        runtime.close()
