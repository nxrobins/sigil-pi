"""Legacy emergency contract evidence only: no SIGIL/native execution claim.

Run against the unchanged real ProductService and its limiter. Only the durable
admission fault and the monotonic clock are injected. This prevents a new SIGIL
policy from silently substituting the normal epoch-window quota behavior.
"""

import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from product_service import AuthRegistry, DurableQuotaStore, FixedWindowRateLimiter, ProductService
from test_product_service import CHAT_TOKEN, OPS_TOKEN, RecordingAgent, _auth, _entry, _headers


@pytest.mark.parametrize("elapsed,expected", [
    (0, (False, 61)), (0.25, (False, 60)), (1, (False, 60)),
    (1.25, (False, 59)), (59, (False, 2)), (59.75, (False, 1)),
    (60, (True, 0)), (60.25, (True, 0)),
])
def test_actual_reference_limiter_exact_clock_and_retry_after_boundaries(elapsed, expected):
    clock = [120.25]
    limiter = FixedWindowRateLimiter(1, clock=lambda: clock[0])
    assert limiter.admit("tenant") == (True, 0)
    clock[0] += elapsed
    assert limiter.admit("tenant") == expected
    assert list(limiter._events["tenant"]) == ([clock[0]] if expected[0] else [120.25])


def test_emergency_window_does_not_reset_at_a_wall_clock_minute_boundary():
    clock = [59.75]
    limiter = FixedWindowRateLimiter(1, clock=lambda: clock[0])
    assert limiter.admit("tenant") == (True, 0)
    clock[0] = 60
    assert limiter.admit("tenant") == (False, 60)
    clock[0] = 119.75
    assert limiter.admit("tenant") == (True, 0)
    assert list(limiter._events["tenant"]) == [119.75]


def test_emergency_removes_each_old_event_at_its_own_sixty_second_boundary():
    clock = [100]
    limiter = FixedWindowRateLimiter(2, clock=lambda: clock[0])
    assert limiter.admit("tenant") == (True, 0)
    clock[0] = 110
    assert limiter.admit("tenant") == (True, 0)
    for at, expected, events in [
        (159.75, (False, 1), [100, 110]),
        (160, (True, 0), [110, 160]),
        (169.75, (False, 1), [110, 160]),
        (170, (True, 0), [160, 170]),
    ]:
        clock[0] = at
        assert limiter.admit("tenant") == expected
        assert list(limiter._events["tenant"]) == events


def fixture(tmp_path, monkeypatch, *, limit=2, auth=None):
    clock, records, admissions, probes = [120.25], [], [], []
    quota = DurableQuotaStore(tmp_path / "quota.sqlite3", requests_per_minute=limit)
    normal = quota.admit_request

    def unavailable(tenant):
        admissions.append(tenant)
        raise sqlite3.OperationalError("private-fault-diagnostic-canary")

    def runtime():
        probes.append("runtime")
        return True

    monkeypatch.setattr(quota, "admit_request", unavailable)
    service = ProductService(RecordingAgent(), auth or _auth(), quota_store=quota,
                             readiness=runtime, log_sink=records.append)
    # Preserve the actual limiter selected by ProductService and its actual bound.
    service._readiness_emergency_rate.clock = lambda: clock[0]
    assert service._readiness_emergency_rate.limit == limit
    return SimpleNamespace(service=service, quota=quota, normal=normal, clock=clock,
                           records=records, admissions=admissions, probes=probes,
                           unavailable=unavailable)


def request(state, *, token=OPS_TOKEN, method="GET", path="/v1/ready"):
    status, headers, body = state.service.dispatch(method, path, _headers(token))
    assert headers["X-Request-ID"] == "request-1234"
    assert headers["Cache-Control"] == "no-store"
    assert body["request_id"] == "request-1234"
    assert state.records[-1]["request_id"] == "request-1234"
    assert state.records[-1]["status"] == status
    assert "private-fault-diagnostic-canary" not in json.dumps([headers, body, state.records])
    assert state.service.agent.calls == []
    return status, headers, body


@pytest.mark.parametrize("token,method,path,status,admissions", [
    ("unknown", "GET", "/v1/ready", 401, []),
    (CHAT_TOKEN, "GET", "/v1/ready", 403, ["tenant-a"]),
    (OPS_TOKEN, "POST", "/v1/ready", 500, ["ops"]),
    (OPS_TOKEN, "GET", "/v1/health", 500, ["ops"]),
    (CHAT_TOKEN, "POST", "/v1/chat", 500, ["tenant-a"]),
])
def test_failed_auth_scope_or_nonreadiness_route_cannot_charge_emergency_allowance(
        tmp_path, monkeypatch, token, method, path, status, admissions):
    state = fixture(tmp_path, monkeypatch)
    assert request(state, token=token, method=method, path=path)[0] == status
    assert state.admissions == admissions
    assert dict(state.service._readiness_emergency_rate._events) == {}
    assert state.probes == []


def test_actual_dispatch_preserves_sixty_one_second_guidance_without_running_probes_again(tmp_path, monkeypatch):
    state = fixture(tmp_path, monkeypatch, limit=1)
    assert request(state)[0] == 503
    status, headers, body = request(state)
    assert status == 429
    assert headers["Retry-After"] == "61"
    assert body["error"] == {"code": "rate_limited", "message": "tenant request rate exceeded"}
    assert state.probes == ["runtime"]
    assert state.admissions == ["ops", "ops"]
    assert list(state.service._readiness_emergency_rate._events["ops"]) == [120.25]


def test_durable_quota_refusal_does_not_authorize_emergency_fallback(tmp_path, monkeypatch):
    state = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(state.quota, "admit_request", lambda tenant: (False, 29))
    status, headers, _ = request(state)
    assert status == 429 and headers["Retry-After"] == "29"
    assert dict(state.service._readiness_emergency_rate._events) == {}
    assert state.probes == []


def test_durable_recovery_does_not_reset_or_spend_the_existing_emergency_window(tmp_path, monkeypatch):
    state = fixture(tmp_path, monkeypatch, limit=2)
    assert [request(state)[0] for _ in range(3)] == [503, 503, 429]
    events = list(state.service._readiness_emergency_rate._events["ops"])
    monkeypatch.setattr(state.quota, "admit_request", state.normal)
    status, _, body = request(state)
    assert status == 200 and body["dependencies"]["quota_store"] is True
    assert list(state.service._readiness_emergency_rate._events["ops"]) == events
    monkeypatch.setattr(state.quota, "admit_request", state.unavailable)
    assert request(state)[0] == 429
    state.clock[0] += 60
    assert request(state)[0] == 503
    assert list(state.service._readiness_emergency_rate._events["ops"]) == [180.25]


def test_allowance_is_shared_by_tenant_not_credential_and_isolated_from_other_tenants(tmp_path, monkeypatch):
    tokens = ["readiness-test-key-" + name * 32 for name in ["a", "b", "c"]]
    auth = AuthRegistry([_entry(token, f"operator-{i}", tenant, ["ops:read"])
                         for i, (token, tenant) in enumerate(zip(tokens, ["one", "one", "two"]))])
    state = fixture(tmp_path, monkeypatch, limit=1, auth=auth)
    assert [request(state, token=token)[0] for token in tokens] == [503, 429, 503]
    assert state.admissions == ["one", "one", "two"]
    assert {tenant: list(events) for tenant, events in state.service._readiness_emergency_rate._events.items()} == {
        "one": [120.25], "two": [120.25]}
    assert state.probes == ["runtime", "runtime"]


def test_programming_failure_never_enters_or_spends_emergency_allowance(tmp_path, monkeypatch):
    state = fixture(tmp_path, monkeypatch)

    def broken(tenant):
        raise RuntimeError("private-fault-diagnostic-canary")

    monkeypatch.setattr(state.quota, "admit_request", broken)
    status, _, body = request(state)
    assert status == 500
    assert body["error"] == {"code": "internal_error", "message": "internal service error"}
    assert dict(state.service._readiness_emergency_rate._events) == {}
    assert state.probes == []


def test_actual_sqlite_path_outage_and_recovery_preserve_both_independent_allowances(tmp_path):
    """Actual filesystem fault; neither admit_request nor health is a double."""
    root = tmp_path / "quota-storage"
    root.mkdir()
    parked = tmp_path / "parked-quota-storage"
    quota = DurableQuotaStore(root / "quota.sqlite3", requests_per_minute=2, clock=lambda: 150.0)
    records = []
    service = ProductService(RecordingAgent(), _auth(), quota_store=quota, log_sink=records.append)
    service._readiness_emergency_rate.clock = lambda: 120.25
    state = SimpleNamespace(service=service, records=records)
    assert request(state)[0] == 200
    root.rename(parked)
    assert quota.is_healthy() is False
    assert request(state, token="unknown")[0] == 401
    assert request(state, token=CHAT_TOKEN)[0] == 403
    assert dict(service._readiness_emergency_rate._events) == {}
    for expected in [503, 503, 429]:
        status, _, body = request(state)
        assert status == expected
        if status == 503:
            assert body["dependencies"] == {"quota_store": False, "runtime": True}
    events = list(service._readiness_emergency_rate._events["ops"])
    assert events == [120.25, 120.25]
    parked.rename(root)
    assert quota.is_healthy() is True
    assert request(state)[0] == 200
    with closing(sqlite3.connect(quota.path)) as db:
        assert db.execute("SELECT window_start,count FROM request_windows").fetchall() == [(120, 2)]
    assert list(service._readiness_emergency_rate._events["ops"]) == events
    root.rename(parked)
    assert request(state)[0] == 429
    parked.rename(root)
