"""Fail-closed evidence for every production-readiness failure category.

Specialized crash, provider, corruption, network, and audit tests live beside
their implementations. This module injects the previously missing filesystem
failures and verifies that the evidence matrix cannot silently lose a required
category or point at a test pytest no longer collects.
"""

import errno
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agent import SessionStore
from conftest import PI_ROOT
from product_service import AuthRegistry, Principal, ProductScheduleStore, ProductService


REQUIRED_FAILURES = {
    "runtime_crashes",
    "unavailable_model_providers",
    "full_disks",
    "corrupt_state",
    "network_failures",
    "interrupted_writes",
}
CHAT_TOKEN = "failure-injection-chat-token-with-test-only-entropy"


def _principal():
    return Principal(
        principal_id="planner",
        tenant_id="tenant-a",
        scopes=frozenset({"schedules:read", "schedules:write"}),
        tools=frozenset(),
    )


def test_full_disk_preserves_committed_session_state(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions")
    store.save("session", [{"role": "user", "content": "committed"}])
    committed_path = store._path("session")
    committed_bytes = committed_path.read_bytes()
    temp_path = committed_path.with_suffix(".kv.tmp")
    real_write = Path.write_bytes

    def no_space(path, data):
        if path == temp_path:
            raise OSError(errno.ENOSPC, "injected full disk")
        return real_write(path, data)

    monkeypatch.setattr(Path, "write_bytes", no_space)
    with pytest.raises(OSError) as error:
        store.save("session", [{"role": "user", "content": "uncommitted"}])
    assert error.value.errno == errno.ENOSPC
    assert committed_path.read_bytes() == committed_bytes
    assert store.load("session") == [{"role": "user", "content": "committed"}]


def test_full_disk_preserves_committed_schedule_state(tmp_path, monkeypatch):
    path = tmp_path / "schedules.json"
    store = ProductScheduleStore(path, clock=lambda: 100.0)
    principal = _principal()
    store.put(
        principal, name="daily", session="work", message="committed", every_ms=1000)
    committed_bytes = path.read_bytes()
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    real_write = Path.write_bytes

    def no_space(candidate, data):
        if candidate == temp_path:
            raise OSError(errno.ENOSPC, "injected full disk")
        return real_write(candidate, data)

    monkeypatch.setattr(Path, "write_bytes", no_space)
    with pytest.raises(OSError) as error:
        store.put(
            principal, name="daily", session="work", message="uncommitted",
            every_ms=1000)
    assert error.value.errno == errno.ENOSPC
    assert path.read_bytes() == committed_bytes
    [entry] = store.for_tenant("tenant-a")
    assert entry["message"] == "committed"


def test_full_disk_at_product_boundary_is_stable_and_releases_capacity():
    class FullDiskAgent:
        manifest = {}
        memory = None
        _mcp = object()

        def turn_with_usage(self, *args, **kwargs):
            raise OSError(errno.ENOSPC, "private state path: no space left")

        @staticmethod
        def turn_telemetry():
            return {}

    auth = AuthRegistry([{
        "sha256": hashlib.sha256(CHAT_TOKEN.encode()).hexdigest(),
        "principal": "chat-user",
        "tenant": "tenant-a",
        "scopes": ["chat"],
        "tools": [],
    }])
    service = ProductService(
        FullDiskAgent(), auth, max_concurrent_turns=1, log_sink=lambda record: None)
    headers = {"Authorization": f"Bearer {CHAT_TOKEN}", "X-Request-ID": "disk-full-test"}
    payload = json.dumps({"session": "work", "message": "hello"}).encode()

    for _ in range(2):
        status, _, body = service.dispatch("POST", "/v1/chat", headers, payload)
        assert status == 500
        assert body["error"] == {
            "code": "internal_error", "message": "internal service error"}
        rendered = json.dumps(body)
        assert "private" not in rendered and "space" not in rendered
    assert service.metrics.snapshot()["active_turns"] == 0


def test_interrupted_session_replace_preserves_committed_state(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions")
    store.save("session", [{"role": "user", "content": "committed"}])
    committed_path = store._path("session")
    committed_bytes = committed_path.read_bytes()
    temp_path = committed_path.with_suffix(".kv.tmp")
    real_replace = Path.replace

    def interrupted(candidate, target):
        if candidate == temp_path:
            raise OSError(errno.EINTR, "injected interruption before commit")
        return real_replace(candidate, target)

    monkeypatch.setattr(Path, "replace", interrupted)
    with pytest.raises(OSError) as error:
        store.save("session", [{"role": "user", "content": "uncommitted"}])
    assert error.value.errno == errno.EINTR
    assert temp_path.exists(), "injection did not reach the post-write commit boundary"
    assert committed_path.read_bytes() == committed_bytes
    assert SessionStore(store.dir).load("session") == [
        {"role": "user", "content": "committed"}]


def test_interrupted_schedule_replace_preserves_committed_state(tmp_path, monkeypatch):
    path = tmp_path / "schedules.json"
    store = ProductScheduleStore(path, clock=lambda: 100.0)
    principal = _principal()
    store.put(
        principal, name="daily", session="work", message="committed", every_ms=1000)
    committed_bytes = path.read_bytes()
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    real_replace = Path.replace

    def interrupted(candidate, target):
        if candidate == temp_path:
            raise OSError(errno.EINTR, "injected interruption before commit")
        return real_replace(candidate, target)

    monkeypatch.setattr(Path, "replace", interrupted)
    with pytest.raises(OSError) as error:
        store.put(
            principal, name="daily", session="work", message="uncommitted",
            every_ms=1000)
    assert error.value.errno == errno.EINTR
    assert temp_path.exists(), "injection did not reach the post-write commit boundary"
    assert path.read_bytes() == committed_bytes
    [entry] = ProductScheduleStore(path).for_tenant("tenant-a")
    assert entry["message"] == "committed"


def test_failure_injection_matrix_is_complete_and_points_to_collected_tests():
    matrix = json.loads(
        (PI_ROOT / "config" / "failure-injection-matrix.json").read_text())
    assert set(matrix) == REQUIRED_FAILURES
    for category, evidence in matrix.items():
        assert isinstance(evidence, list) and evidence, f"{category} has no evidence"
        assert all(isinstance(node, str) and node.startswith("tests/") for node in evidence)

    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only"],
        capture_output=True, text=True, cwd=PI_ROOT)
    assert collected.returncode == 0, collected.stdout + collected.stderr
    node_ids = set(re.findall(r"^(tests/\S+::test_\S+)$", collected.stdout, re.M))
    for category, evidence in matrix.items():
        for expected in evidence:
            assert any(node == expected or node.startswith(expected + "[")
                       for node in node_ids), (
                f"{category} points to an uncollected test: {expected}")
