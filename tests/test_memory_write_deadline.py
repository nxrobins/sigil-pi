"""Memory writes observed at real pipes across controlled readiness delays."""
import json
import os
import sys
import time

import pytest

import agent as host
from agent import PiMemory


PEER = r'''
import json, sys
mode, log = sys.argv[1:3]
model = sys.argv[sys.argv.index("--embedding-model") + 1]

def append(suffix, value):
    with open(log + suffix, "a") as file:
        file.write(json.dumps(value) + "\n")

for line in sys.stdin:
    req = json.loads(line)
    append(".requests", req)
    op = req["op"]
    result = {}
    if op == "hello":
        result = {"protocol": 2, "embedder": {"mode": "callback", "model_id": model},
                  "fact_grounding": {"enabled": True, "roles": ["fact_support", "fact_revision"],
                  "min_confidence": 0.8, "quarantine": True, "replay_barriers": True,
                  "extraction_enabled": True, "current_fact_retrieval_enabled": True}}
    elif op == "consolidate_all" and mode.endswith("_request"):
        print(json.dumps({"callback": mode, "call_id": 1, "model": model, "texts": ["evidence"],
                          "request": {"role": "Judge", "state": {"task": "verify_fact_support"}}}), flush=True)
        answer = sys.stdin.readline()
        if not answer:
            break
        append(".effects", json.loads(answer))
    elif op in ("record", "forget"):
        append(".effects", req)
        result = {"forgotten": True} if op == "forget" else {"cycle": req["id"]}
    print(json.dumps({"id": req["id"], "ok": result}), flush=True)
    if op == "shutdown":
        break
'''


def memory_peer(tmp_path, mode="normal"):
    peer = tmp_path / "peer.py"
    peer.write_text(PEER)
    return PiMemory([sys.executable, str(peer), mode, str(tmp_path / "wire")], tmp_path / "memory",
                    fact_grounding=True, complete=lambda request: {"unavailable": True},
                    judge=lambda request: {"unavailable": True},
                    embed=lambda texts, model, **kwargs: [[1.0, 0.0] for _ in texts])


def observed(tmp_path, suffix):
    path = tmp_path / ("wire." + suffix)
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def delay_write_readiness(monkeypatch, memory, progress, target, expiry):
    clock = [100.0]
    writes, late_writes, ready_calls = [], [], []
    select, write = host.select.select, os.write
    pipe = memory._proc.stdin
    fd = pipe.fileno()
    intercepted = False

    def ready(readable, writable, errors, timeout):
        result = select(readable, writable, errors, timeout)
        if pipe in result[1]:
            ready_calls.append(timeout)
            limit = target + (progress != "whole")
            if expiry is not None and len(ready_calls) == limit:
                clock[0] = expiry
        return result

    def send(descriptor, data):
        nonlocal intercepted
        if descriptor != fd:
            return write(descriptor, data)
        if len(ready_calls) == target and not intercepted:
            intercepted = True
            if progress == "blocked":
                raise BlockingIOError()
            if progress == "partial":
                data = data[:7]
        size = write(descriptor, data)
        writes.append(bytes(data[:size]))
        if expiry is not None and clock[0] >= expiry:
            late_writes.append(bytes(data[:size]))
        return size

    monkeypatch.setattr(host.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(host.select, "select", ready)
    monkeypatch.setattr(host.os, "write", send)
    return writes, late_writes, ready_calls


@pytest.mark.parametrize("path", ["record", "action", "pending_user", "pending_action", "forget",
                                  "model_request", "embedding_request", "judgment_request"])
@pytest.mark.parametrize("progress", ["whole", "partial", "blocked"])
def test_expired_readiness_sends_no_memory_bytes_or_effects(tmp_path, monkeypatch, path, progress):
    callback = path.endswith("_request")
    memory = memory_peer(tmp_path, path if callback else "normal")
    original_receipt = None
    if path.startswith("pending_"):
        memory.recall_lock_timeout = 0
        memory._lock.acquire()
        try:
            original_receipt = memory.record("s", "pending evidence", "user_message" if path == "pending_user" else "agent_action")
        finally:
            memory._lock.release()
        assert original_receipt["status"] == "buffered"
        memory.recall_lock_timeout = 2
    callback_calls = []
    if callback:
        handler = {"model_request": "complete", "embedding_request": "embed", "judgment_request": "judge"}[path]
        original = getattr(memory, handler)

        def answer(*args, **kwargs):
            callback_calls.append(args)
            return original(*args, **kwargs)

        setattr(memory, handler, answer)
    try:
        with monkeypatch.context() as patch:
            writes, late, waits = delay_write_readiness(patch, memory, progress, 2 if callback else 1, 101.0)
            with memory.turn_context(101.0), memory.turn_context(110.0):
                if callback:
                    result = memory.consolidate_now()
                    assert result["err"]["code"] == "deadline"
                    assert result["review_receipt"]["storage_commit"] == "unconfirmed"
                elif path == "forget":
                    with pytest.raises(RuntimeError, match="deadline"):
                        memory.forget("s")
                else:
                    receipt = (memory.record_reply("s", lambda: "final action") if path == "action" else
                               memory.record("s", "new evidence", "user_message"))
                    assert receipt["recorded"] is False and receipt["reason"] == "deadline"
            assert late == []
            assert len(writes) == int(callback) + int(progress == "partial")
            if progress == "partial":
                assert len(writes[-1]) == 7 and b"\n" not in writes[-1]
            assert waits and all(0 < wait <= 1 for wait in waits)
        memory._proc.wait(timeout=2)
        assert observed(tmp_path, "effects") == []
        operations = [req["op"] for req in observed(tmp_path, "requests")]
        assert operations == ["hello", "probe_embedding"] + (["consolidate_all"] if callback else [])
        if callback:
            assert len(callback_calls) == 1
        elif path != "forget":
            assert memory._pending[-1][1] is receipt
            assert memory._pending[-1][0]["text"] == ("final action" if path == "action" else "new evidence")
            if original_receipt is not None:
                assert memory._pending[0][1] is original_receipt
                assert original_receipt["recorded"] is False and original_receipt["reason"] == "deadline"
    finally:
        memory.stop()
    restarted = memory_peer(tmp_path)
    try:
        with restarted.turn_context(time.monotonic() + 2):
            assert restarted.record("fresh", "fresh operation", "user_message")["recorded"] is True
        effects = observed(tmp_path, "effects")
        assert len(effects) == 1
        assert effects[0]["text"] == "fresh operation"
    finally:
        restarted.stop()


@pytest.mark.parametrize("progress", ["partial", "blocked"])
@pytest.mark.parametrize("deadline", [None, 101.0])
def test_in_budget_memory_writes_finish_after_partial_or_blocked_writes(tmp_path, monkeypatch, progress, deadline):
    memory = memory_peer(tmp_path)
    try:
        with monkeypatch.context() as patch:
            writes, late, waits = delay_write_readiness(patch, memory, progress, 1, None)
            with memory.turn_context(deadline):
                receipt = memory.record("s", "complete evidence", "user_message")
            assert receipt["recorded"] is True
            assert json.loads(b"".join(writes))["text"] == "complete evidence"
            assert late == [] and len(waits) == 2
            assert waits == ([None, None] if deadline is None else [1.0, 1.0])
        assert [effect["text"] for effect in observed(tmp_path, "effects")] == ["complete evidence"]
    finally:
        memory.stop()
