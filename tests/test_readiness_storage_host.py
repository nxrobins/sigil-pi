"""Real staged v9 bootstrap, held scopes, storage observations and clock facts."""

from contextlib import closing
import os
import re
import sqlite3

import pytest

from api_support import TOKEN_B, credential
from http_service_support import http_fixture_secret as http_fixture_secret, state
from readiness_host_support import ReadinessHostApi, compose_storage_probe, readiness_host_binary as readiness_host_binary
from test_turn_execution import programs as programs
from turn_support import fields, record


pytestmark = pytest.mark.usefixtures("http_fixture_secret")


def result(api, path="/probe/storage", *, marker="SI1\n", count=3, **kwargs):
    status, headers, raw = api.exchange("POST", path, **kwargs)
    assert status == 200
    assert dict(headers)["cache-control"] == "no-store"
    seen, before, after = fields(raw.decode(), "IO1\n", 3)
    first, last = fields(before, "MC1\n", 2), fields(after, "MC1\n", 2)
    assert re.fullmatch("[0-9a-f]{64}", first[0])
    assert first[0] == last[0] and 0 <= int(first[1]) <= int(last[1]) <= 9223372036854775807
    return fields(seen, marker, count), last


def test_probe_recipe_keeps_full_bootstrap_and_original_source_bounds():
    source = compose_storage_probe()
    assert source == compose_storage_probe()
    assert len(source.text.encode()) <= 65536
    assert source.text.count("pub fn tool_main(") == 1
    assert '"profiles_validated"' in source.text
    assert '"AH6\\n"' not in source.text and '"HC6\\n"' not in source.text
    assert source.text.count('"AH7\\n", 18') == 3
    assert '"inspect_storage"' in source.text
    assert '"/v1/ops/ready"' not in source.text


def test_actual_v9_bootstrap_inspection_held_scopes_and_native_clock(
        readiness_host_binary, programs, scripted_llm, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    with ReadinessHostApi(readiness_host_binary, root, programs, scripted_llm.url, workspace, rows=rows) as api:
        before = state(root)
        forged = record("SI1\n", ["error", "corrupt", "storage"])
        seen, clock = result(api, raw=forged.encode())
        assert seen == ["ok", "", ""]
        seen, following = result(api, "/probe/denied")
        assert seen == ["error", "denied", "precheck"]
        assert clock[0] == following[0] and int(clock[1]) <= int(following[1])
        seen, _ = result(api, token=TOKEN_B)
        assert seen == ["error", "denied", "precheck"]
        status, _, raw = api.exchange("POST", "/probe/storage", token="unknown")
        assert (status, raw) == (401, b"unmatched")
        assert state(root) == before
    assert scripted_llm.requests == []


def test_actual_v9_storage_outage_recovery_and_new_process_clock_domain(
        readiness_host_binary, programs, scripted_llm, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    with ReadinessHostApi(readiness_host_binary, root, programs, scripted_llm.url, workspace) as api:
        before = state(root)
        _, initial = result(api)
        os.rename(root / "records", root / "parked")
        try:
            seen, lost = result(api)
            assert seen == ["error", "storage", "storage"] and lost[0] == initial[0]
            seen, _ = result(api, "/probe/denied")
            assert seen == ["error", "denied", "precheck"]
        finally:
            os.rename(root / "parked", root / "records")
        seen, recovered = result(api)
        assert seen == ["ok", "", ""] and recovered[0] == initial[0]
        assert state(root) == before
    with ReadinessHostApi(readiness_host_binary, root, programs, scripted_llm.url, workspace, mode="open") as api:
        seen, restarted = result(api)
        assert seen == ["ok", "", ""] and restarted[0] != initial[0]
        assert state(root) == before
    assert scripted_llm.requests == []


def test_actual_v9_reports_real_schema_damage_and_lock_without_exposing_diagnostics(
        readiness_host_binary, programs, scripted_llm, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    with ReadinessHostApi(readiness_host_binary, root, programs, scripted_llm.url, workspace) as api:
        before = state(root)
        with closing(sqlite3.connect(root / "records/records.sqlite")) as db:
            db.execute("BEGIN EXCLUSIVE")
            seen, _ = result(api)
            assert seen == ["error", "busy", "storage"]
            db.rollback()
            seen, _ = result(api)
            assert seen == ["ok", "", ""]
            db.execute("PRAGMA user_version=2")
            seen, _ = result(api)
            assert seen == ["error", "corrupt", "storage"]
            db.execute("PRAGMA user_version=1")
            seen, _ = result(api)
            assert seen == ["ok", "", ""]
        assert state(root) == before
    assert scripted_llm.requests == []


def execution_result(api, path="/probe/execution", **kwargs):
    seen, clock = result(api, path, marker="EI1\n", count=2, **kwargs)
    return fields(seen[0], "SI1\n", 3), fields(seen[1], "RI1\n", 3), clock


def test_actual_execution_inspection_separates_storage_failure_from_runtime_and_scope(
        readiness_host_binary, programs, scripted_llm, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    with ReadinessHostApi(readiness_host_binary, root, programs, scripted_llm.url, workspace) as api:
        before = state(root)
        storage, runtime, _ = execution_result(api, raw=record("EI1\n", ["forged-storage", "forged-runtime"]).encode())
        assert storage == ["ok", "", ""] and runtime == ["ok", "", "0"]
        os.rename(root / "records", root / "parked")
        try:
            storage, runtime, _ = execution_result(api)
            assert storage == ["error", "storage", "storage"]
            assert runtime == ["ok", "", "0"]
            storage, runtime, _ = execution_result(api, "/probe/execution-denied")
            assert storage == ["error", "denied", "precheck"]
            assert runtime == ["error", "scope_not_checked", ""]
        finally:
            os.rename(root / "parked", root / "records")
        storage, runtime, _ = execution_result(api)
        assert storage == ["ok", "", ""] and runtime == ["ok", "", "0"]
        assert state(root) == before
    assert scripted_llm.requests == []


def test_actual_execution_inspection_detects_an_unusable_effect_runtime_without_dispatch(
        readiness_host_binary, programs, scripted_llm, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    with ReadinessHostApi(readiness_host_binary, root, programs, scripted_llm.url, workspace,
                          isolate_effect_runtime=True) as api:
        before = state(root)
        original = api.effect_runtime.read_bytes()
        storage, runtime, _ = execution_result(api)
        assert storage == ["ok", "", ""] and runtime == ["ok", "", "0"]
        api.effect_runtime.write_bytes(b"damaged private test runtime")
        storage, runtime, _ = execution_result(api)
        assert storage == ["ok", "", ""]
        assert runtime == ["error", "artifact_invalid", ""]
        storage, runtime, _ = execution_result(api, "/probe/execution-denied")
        assert storage == ["error", "denied", "precheck"]
        assert runtime == ["error", "scope_not_checked", ""]
        api.effect_runtime.write_bytes(original)
        storage, runtime, _ = execution_result(api)
        assert storage == ["ok", "", ""] and runtime == ["ok", "", "0"]
        assert state(root) == before
    assert scripted_llm.requests == []
