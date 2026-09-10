"""Real native accounting and observations, with an explicitly test-only reply."""

import json
import os
import signal

import pytest

from api_support import BODY, TOKEN_B, credential
from automatic_support import wait_operation
from http_service_support import http_fixture_secret as http_fixture_secret, state
from readiness_admission_host_support import AdmissionHostApi, compose_admission_probe
from readiness_host_support import readiness_host_binary as readiness_host_binary
from request_api_checks import counters, only_accounting_changed, window_headroom
from test_turn_execution import hanging_provider, programs as programs
from turn_support import fields, record


pytestmark = pytest.mark.usefixtures("http_fixture_secret")


def inspect(api, *, failed, draining=0, in_flight=0, **kwargs):
    status, headers, body = api.exchange("GET", "/v1/ready", **kwargs)
    assert status == 218, (status, body)  # NOT a product-ready success status.
    assert dict(headers)["cache-control"] == "no-store"
    held, observed, clock, label = fields(json.loads(body)["diagnostic"], "AC1\n", 4)
    assert fields(held, "RN1\n", 1) == [str(failed)]
    storage, runtime = fields(observed, "EI1\n", 2)
    assert fields(runtime, "RI1\n", 3) == ["ok", "", str(in_flight)]
    clock, lifecycle = fields(clock, "PF1\n", 2)
    assert fields(lifecycle, "LF1\n", 3) == ["ok", "", str(draining)]
    return fields(storage, "SI1\n", 3), fields(clock, "MC1\n", 2), label


def denied(api, status, code, *, method="GET", path="/v1/ready", **kwargs):
    actual, headers, body = api.exchange(method, path, **kwargs)
    assert actual == status and json.loads(body) == {"error": {"code": code}}
    return dict(headers)


def test_conformance_reply_does_not_replace_any_admission_decision():
    built = compose_admission_probe(2)
    assert built == compose_admission_probe(2)
    assert len(built.text.encode()) <= 65536
    assert len(compose_admission_probe(9223372036854775807).text.encode()) <= 65536
    assert 'text("218")' in built.text and '"AC1\\n"' in built.text
    assert "readiness_project(" not in built.text
    assert 'command("temporary_write"' in built.text and 'command("inspect_execution"' in built.text


def test_actual_normal_accounting_and_v9_http_metadata(readiness_host_binary, programs, scripted_llm, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    with AdmissionHostApi(readiness_host_binary, root, programs, scripted_llm.url, workspace, limit=2) as api:
        before = state(root)
        denied(api, 401, "authentication_required", token=None)
        denied(api, 401, "invalid_credential", token="unknown")
        assert state(root) == before
        window_headroom()
        forged = record("VC1\n", ["ok", "999", ""]).encode()
        storage, clock, label = inspect(api, failed=0, raw=forged, hints=["actual-v9-request"])
        assert storage == ["ok", "", ""] and label == "actual-v9-request"
        storage, following, _ = inspect(api, failed=0)
        assert storage == ["ok", "", ""] and following[0] == clock[0]
        counted = state(root)
        headers = denied(api, 429, "rate_limited")
        assert 1 <= int(headers["retry-after"]) <= 60
        assert state(root) == counted
        changed = [row for row in counted[1] if row not in before[1]]
        assert len(changed) == 1 and changed[0][:2] == ("a.budget", "request-window")
    assert scripted_llm.requests == []


def test_actual_operator_signal_is_one_way_process_wide_and_not_caller_controlled(
        readiness_host_binary, programs, scripted_llm, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    with AdmissionHostApi(readiness_host_binary, root, programs, scripted_llm.url, workspace, limit=32, rows=rows) as api:
        window_headroom()
        forged = record("PF1\n", [record("MC1\n", ["f" * 64, "1"]), record("LF1\n", ["ok", "", "1"])])
        _, before_clock, _ = inspect(api, failed=0, raw=forged.encode())
        os.kill(api.proc.pid, signal.SIGUSR1)
        _, latched_clock, _ = inspect(api, failed=0, draining=1)
        assert latched_clock[0] == before_clock[0]
        before = state(root)
        denied(api, 503, "service_draining", method="POST", path="/v1/operations", body=BODY)
        only_accounting_changed(before, state(root), 1)
        before = state(root)
        denied(api, 503, "service_draining", method="POST", path="/v1/operations", body=BODY, token=TOKEN_B)
        only_accounting_changed(before, state(root), 1)
        os.kill(api.proc.pid, signal.SIGUSR1)
        _, _, _ = inspect(api, failed=0, draining=1,
            raw=record("LF1\n", ["ok", "", "0"]).encode())
        assert api.exchange("GET", "/v1/sessions")[0] == 200
        retained = state(root)
        old_counters = counters(root)
        assert api.proc.poll() is None  # A signal is not native cancellation/exit.
    with AdmissionHostApi(readiness_host_binary, root, programs, scripted_llm.url, workspace,
                          limit=32, rows=rows, mode="open") as api:
        assert state(root) == retained and counters(root) == old_counters
        _, restarted_clock, _ = inspect(api, failed=0, draining=0)
        assert restarted_clock[0] != latched_clock[0]  # New process, not a reset API.
    assert scripted_llm.requests == []


def test_drain_preserves_existing_acceptance_replay_and_explicit_cancellation_of_a_sent_effect(
        readiness_host_binary, programs, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    with hanging_provider() as (endpoint, arrived, requests):
        with AdmissionHostApi(readiness_host_binary, root, programs, endpoint, workspace, limit=1024, rows=rows) as api:
            status, accepted = api.request()
            assert status == 202
            assert arrived.wait(15), "the local provider must actually receive the effect"
            os.kill(api.proc.pid, signal.SIGUSR1)
            inspect(api, failed=0, draining=1, in_flight=1)
            # Retry of the original immutable payload still discovers acceptance.
            status, replay = api.request()
            assert status == 202 and replay["operation"] == accepted["operation"] and replay["replayed"]
            denied(api, 409, "submission_conflict", method="POST", path="/v1/operations",
                   body={**BODY, "message": "changed payload"})
            denied(api, 503, "service_draining", method="POST", path="/v1/operations",
                   body={**BODY, "submission_key": "new-key"})
            status, pending = api.request("GET", accepted["status_url"])
            assert status == 200 and not pending.get("cancellation_requested", False)
            assert api.request("GET", accepted["status_url"], token=TOKEN_B)[0] == 404
            path = accepted["status_url"] + "/cancel"
            assert api.request("POST", path, body={}, token=TOKEN_B)[0] == 404
            status, cancellation = api.request("POST", path, body={})
            assert status == 202 and cancellation["cancellation_status"] == "requested"
            result = wait_operation(api, accepted["operation"])
            assert result["status"] == "uncertain" and result["usage"]["known"] is False
            assert result["accounting"] == "unknown"
        with AdmissionHostApi(readiness_host_binary, root, programs, endpoint, workspace,
                              limit=1024, rows=rows, mode="open") as api:
            assert api.request("GET", accepted["status_url"])[1] == result
        assert len(requests) == 1


def test_actual_outage_scoped_emergency_cas_exhaustion_and_recovery_do_not_reset_accounting(
        readiness_host_binary, programs, scripted_llm, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    restricted = "no-ops-" + "d" * 40
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b"),
            credential(restricted, principal="carol", tenant="tenant-c", prefix="c", scopes=["chat"])]
    with AdmissionHostApi(readiness_host_binary, root, programs, scripted_llm.url, workspace, limit=2, rows=rows) as api:
        window_headroom()
        initial = state(root)
        os.rename(root / "records", root / "parked")
        try:
            denied(api, 401, "authentication_required", token=None)
            denied(api, 401, "invalid_credential", token="unknown")
            denied(api, 403, "permission_denied", token=restricted)
            denied(api, 503, "storage_unavailable", path="/v1/health")
            denied(api, 503, "storage_unavailable", method="POST")
            for token in (None, TOKEN_B):
                kwargs = {} if token is None else {"token": token}
                first, clock, _ = inspect(api, failed=1, **kwargs)
                second, following, _ = inspect(api, failed=1, **kwargs)
                assert first == second == ["error", "storage", "storage"]
                assert clock[0] == following[0] and int(following[1]) >= int(clock[1])
                headers = denied(api, 429, "rate_limited", **kwargs)
                assert 1 <= int(headers["retry-after"]) <= 61
        finally:
            os.rename(root / "parked", root / "records")
        assert state(root) == initial  # Emergency receipts never became durable rows.
        inspect(api, failed=0)
        inspect(api, failed=0)
        counted = state(root)
        denied(api, 429, "rate_limited")
        os.rename(root / "records", root / "parked")
        try:
            denied(api, 429, "rate_limited")  # Recovery did not clear the emergency cell.
        finally:
            os.rename(root / "parked", root / "records")
        assert state(root) == counted
    assert scripted_llm.requests == []
