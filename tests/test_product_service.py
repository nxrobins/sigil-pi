"""The v1 product boundary: authentication, tenancy, scopes, and safe errors."""

import hashlib
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import PI_ROOT

import product_service
from product_service import (
    AuthRegistry,
    AuditVerificationMonitor,
    ConfigError,
    DurableQuotaStore,
    FixedWindowRateLimiter,
    ProductError,
    ProductScheduler,
    ProductScheduleStore,
    ProductDataManager,
    ProductRetentionMonitor,
    ProductService,
    SessionOperationLocks,
    TenantConcurrency,
    _internal_session,
    serve_product,
    validate_transport,
)


CHAT_TOKEN = "chat-token-with-enough-entropy-for-a-test-only-fixture"
OPS_TOKEN = "ops-token-with-enough-entropy-for-a-test-only-fixture"
OTHER_TOKEN = "other-token-with-enough-entropy-for-a-test-only-fixture"
SCHEDULE_TOKEN = "schedule-token-with-enough-entropy-for-a-test-only-fixture"
OTHER_SCHEDULE_TOKEN = "other-schedule-token-with-enough-entropy-for-a-test-fixture"
DATA_TOKEN = "data-lifecycle-token-with-enough-entropy-for-a-test-fixture"


def _entry(token, principal, tenant, scopes, tools=None):
    return {
        "sha256": hashlib.sha256(token.encode()).hexdigest(),
        "principal": principal,
        "tenant": tenant,
        "scopes": scopes,
        "tools": tools or [],
    }


def _auth():
    return AuthRegistry([
        _entry(CHAT_TOKEN, "alice", "tenant-a", ["chat"], ["read_file"]),
        _entry(OPS_TOKEN, "operator", "ops", ["ops:read"]),
        _entry(OTHER_TOKEN, "bob", "tenant-b", ["chat"], ["write_file"]),
        _entry(SCHEDULE_TOKEN, "planner-a", "tenant-a",
               ["schedules:read", "schedules:write"], ["read_file"]),
        _entry(OTHER_SCHEDULE_TOKEN, "planner-b", "tenant-b",
               ["schedules:read", "schedules:write"], ["write_file"]),
        _entry(DATA_TOKEN, "data-admin-a", "tenant-a",
               ["sessions:read", "sessions:delete"]),
    ])


def _headers(token):
    return {"Authorization": f"Bearer {token}", "X-Request-ID": "request-1234"}


class RecordingAgent:
    manifest = {"read_file": {}, "write_file": {}}
    memory = None
    _mcp = object()

    def __init__(self):
        self.calls = []

    def turn_with_usage(self, session, message, *, allowed_tools=None,
                        deadline_monotonic=None):
        assert deadline_monotonic is None or deadline_monotonic > 0
        self.calls.append((session, message, set(allowed_tools or [])))
        return "answer", {"input_tokens": 2, "output_tokens": 1}

    def turn_telemetry(self):
        return {"forge_queue_wait_ms": 1.25, "tool_calls": 2, "retries": 1}


def _service(agent=None, **kwargs):
    return ProductService(agent or RecordingAgent(), _auth(), log_sink=lambda record: None,
                          **kwargs)


@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/chat"),
    ("GET", "/v1/health"),
    ("GET", "/v1/ready"),
    ("GET", "/v1/version"),
    ("GET", "/v1/metrics"),
    ("GET", "/v1/metrics/prometheus"),
    ("GET", "/does-not-exist"),
])
def test_every_route_authenticates_before_dispatch(method, path):
    status, headers, body = _service().dispatch(method, path, {}, b"{}")
    assert status == 401
    assert body["error"]["code"] == "authentication_required"
    assert headers["WWW-Authenticate"].startswith("Bearer ")


def test_invalid_token_is_indistinguishable_from_unknown_token():
    service = _service()
    a = service.dispatch("GET", "/v1/version", _headers("wrong-one"))[2]
    b = service.dispatch("GET", "/v1/version", _headers("wrong-two"))[2]
    assert a["error"] == b["error"] == {
        "code": "invalid_credential", "message": "invalid bearer credential"}


def test_chat_identity_comes_from_credential_and_tools_are_scoped():
    agent = RecordingAgent()
    status, headers, body = _service(agent).dispatch(
        "POST", "/v1/chat", _headers(CHAT_TOKEN),
        json.dumps({"session": "project", "message": "hello"}).encode())
    assert status == 200
    assert headers["Server-Timing"].startswith("queue;dur=")
    assert headers["X-Sigil-Tool-Calls"] == "2"
    assert headers["X-Sigil-Retries"] == "1"
    assert body["session"] == "project"
    [(internal, message, tools)] = agent.calls
    assert internal == _internal_session("tenant-a", "project")
    assert "tenant-a" not in internal and "project" not in internal
    assert message == "hello"
    assert tools == {"read_file"}


def test_same_external_session_in_two_tenants_never_shares_internal_identity():
    agent = RecordingAgent()
    service = _service(agent)
    payload = json.dumps({"session": "same-name", "message": "hello"}).encode()
    assert service.dispatch("POST", "/v1/chat", _headers(CHAT_TOKEN), payload)[0] == 200
    assert service.dispatch("POST", "/v1/chat", _headers(OTHER_TOKEN), payload)[0] == 200
    assert agent.calls[0][0] != agent.calls[1][0]
    assert agent.calls[0][2] == {"read_file"}
    assert agent.calls[1][2] == {"write_file"}


def test_scopes_separate_chat_from_operations():
    service = _service()
    chat = json.dumps({"session": "s", "message": "hi"}).encode()
    assert service.dispatch("GET", "/v1/health", _headers(CHAT_TOKEN))[0] == 403
    assert service.dispatch("POST", "/v1/chat", _headers(OPS_TOKEN), chat)[0] == 403
    assert service.dispatch("GET", "/v1/health", _headers(OPS_TOKEN))[0] == 200
    assert service.dispatch("GET", "/v1/version", _headers(OPS_TOKEN))[2]["api_version"] == "v1"


def test_schedule_read_and_write_have_separate_permissions(tmp_path):
    reader_token = "reader-only-schedule-token-with-test-entropy"
    auth = AuthRegistry([
        _entry(reader_token, "reader", "tenant-a", ["schedules:read"]),
        _entry(SCHEDULE_TOKEN, "writer", "tenant-a", ["schedules:write"]),
    ])
    store = ProductScheduleStore(tmp_path / "schedules.json")
    service = ProductService(RecordingAgent(), auth, schedule_store=store,
                             log_sink=lambda record: None)
    payload = json.dumps({"name": "daily", "session": "work", "message": "report",
                          "every_ms": 60_000}).encode()
    assert service.dispatch("POST", "/v1/schedules", _headers(reader_token), payload)[0] == 403
    assert service.dispatch("POST", "/v1/schedules", _headers(SCHEDULE_TOKEN), payload)[0] == 201
    assert service.dispatch("GET", "/v1/schedules", _headers(SCHEDULE_TOKEN))[0] == 403
    status, _, body = service.dispatch("GET", "/v1/schedules", _headers(reader_token))
    assert status == 200 and body["schedules"][0]["name"] == "daily"


@pytest.mark.parametrize("payload,code", [
    ({"session": "../escape", "message": "hi"}, "invalid_session"),
    ({"session": "s", "message": ""}, "invalid_message"),
    ({"session": "s", "message": "hi", "tenant": "tenant-b"}, "invalid_request"),
    ({"message": "hi"}, "invalid_request"),
])
def test_chat_schema_is_narrow_and_cannot_override_tenant(payload, code):
    status, _, body = _service().dispatch(
        "POST", "/v1/chat", _headers(CHAT_TOKEN), json.dumps(payload).encode())
    assert status == 400
    assert body["error"]["code"] == code


def test_agent_errors_are_stable_and_do_not_leak_diagnostics():
    class Broken(RecordingAgent):
        def turn_with_usage(self, *args, **kwargs):
            raise RuntimeError("secret compiler path and customer data")

    status, _, body = _service(Broken()).dispatch(
        "POST", "/v1/chat", _headers(CHAT_TOKEN),
        json.dumps({"session": "s", "message": "hi"}).encode())
    assert status == 502
    rendered = json.dumps(body)
    assert body["error"] == {"code": "agent_failure", "message": "agent turn failed"}
    assert "secret" not in rendered and "compiler" not in rendered


def test_hard_turn_deadline_has_stable_error_and_releases_capacity():
    class TimedOut(RecordingAgent):
        def turn_with_usage(self, *args, **kwargs):
            raise TimeoutError("private runtime timeout detail")

    service = _service(TimedOut(), max_concurrent_turns=1, turn_deadline_s=1)
    payload = json.dumps({"session": "s", "message": "hi"}).encode()
    for _ in range(2):
        status, headers, body = service.dispatch(
            "POST", "/v1/chat", _headers(CHAT_TOKEN), payload)
        assert status == 504
        assert body["error"] == {
            "code": "turn_deadline_exceeded", "message": "turn deadline exceeded"}
        assert headers["Server-Timing"].startswith("queue;dur=")
    assert service.metrics.snapshot()["active_turns"] == 0


def test_product_service_rejects_nonpositive_turn_deadline():
    with pytest.raises(ConfigError, match="turn_deadline_s"):
        _service(turn_deadline_s=0)


def test_per_tenant_rate_limit_is_enforced_with_retry_after():
    service = _service(requests_per_minute=1)
    assert service.dispatch("GET", "/v1/health", _headers(OPS_TOKEN))[0] == 200
    status, headers, body = service.dispatch("GET", "/v1/health", _headers(OPS_TOKEN))
    assert status == 429 and body["error"]["code"] == "rate_limited"
    assert int(headers["Retry-After"]) >= 1


def test_concurrency_limit_is_tenant_local():
    entered = threading.Event()
    release = threading.Event()

    class Blocking(RecordingAgent):
        def turn_with_usage(self, session, message, *, allowed_tools=None,
                            deadline_monotonic=None):
            entered.set()
            assert release.wait(5)
            return "done", {}

    service = _service(Blocking(), max_concurrent_turns=1)
    payload = json.dumps({"session": "s", "message": "hi"}).encode()
    first = []
    thread = threading.Thread(target=lambda: first.append(
        service.dispatch("POST", "/v1/chat", _headers(CHAT_TOKEN), payload)[0]))
    thread.start()
    assert entered.wait(5)
    try:
        status, headers, body = service.dispatch(
            "POST", "/v1/chat", _headers(CHAT_TOKEN), payload)
        assert status == 429 and body["error"]["code"] == "tenant_busy"
        assert headers["Retry-After"] == "1"
    finally:
        release.set()
        thread.join(5)
    assert first == [200]


def test_durable_request_quota_is_shared_across_workers(tmp_path):
    path = tmp_path / "quotas.sqlite3"
    clock = lambda: 120.0
    one = DurableQuotaStore(path, requests_per_minute=1, clock=clock)
    two = DurableQuotaStore(path, requests_per_minute=1, clock=clock)

    assert one.admit_request("tenant-a") == (True, 0)
    admitted, retry_after = two.admit_request("tenant-a")
    assert admitted is False and retry_after == 60
    assert two.admit_request("tenant-b") == (True, 0)


def test_durable_turn_quota_is_shared_across_workers_and_expires(tmp_path):
    path = tmp_path / "quotas.sqlite3"
    now = [120.0]
    one = DurableQuotaStore(
        path, max_concurrent_turns=1, turn_lease_s=10, clock=lambda: now[0])
    two = DurableQuotaStore(
        path, max_concurrent_turns=1, turn_lease_s=10, clock=lambda: now[0])

    lease = one.acquire_turn("tenant-a")
    with pytest.raises(ProductError, match="concurrency limit"):
        two.acquire_turn("tenant-a")
    other_tenant = two.acquire_turn("tenant-b")
    other_tenant.release()

    now[0] = 131.0
    replacement = two.acquire_turn("tenant-a")
    replacement.release()
    lease.release()  # releasing an already-expired lease is idempotent and safe


def _resource_quota(path, **overrides):
    values = {
        "max_concurrent_turns": 10,
        "tokens_per_day": 1000,
        "token_reservation_per_turn": 10,
        "storage_bytes_per_tenant": 1000,
        "storage_reservation_per_turn": 10,
        "audit_bytes_per_tenant": 1000,
        "audit_reservation_per_turn": 10,
    }
    values.update(overrides)
    return DurableQuotaStore(path, **values)


def test_resource_quota_configuration_and_schema_migration_fail_closed(tmp_path):
    with pytest.raises(ConfigError, match="all durable"):
        DurableQuotaStore(tmp_path / "partial.db", tokens_per_day=10)
    with pytest.raises(ConfigError, match="must not exceed"):
        _resource_quota(
            tmp_path / "oversized.db", tokens_per_day=9,
            token_reservation_per_turn=10)

    legacy = tmp_path / "legacy.db"
    with closing(sqlite3.connect(legacy)) as db:
        db.execute("CREATE TABLE turn_leases ("
                   "lease_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, expires REAL NOT NULL)")
    store = _resource_quota(legacy)
    lease = store.acquire_turn("tenant-a")
    usage = store.tenant_usage("tenant-a")
    assert usage["reserved_tokens"] == 10
    lease.release()


def test_token_quota_reservations_are_cross_worker_and_reset_daily(tmp_path):
    now = [100.0]
    path = tmp_path / "quota.db"
    one = _resource_quota(
        path, tokens_per_day=20, token_reservation_per_turn=10,
        clock=lambda: now[0])
    two = _resource_quota(
        path, tokens_per_day=20, token_reservation_per_turn=10,
        clock=lambda: now[0])
    first = one.acquire_turn("tenant-a")
    second = two.acquire_turn("tenant-a")
    with pytest.raises(ProductError) as error:
        one.acquire_turn("tenant-a")
    assert error.value.code == "token_quota_exceeded"
    assert error.value.retry_after == 86300
    assert two.acquire_turn("tenant-b")

    first.release(usage={"input_tokens": 2, "output_tokens": 3})
    second.release(usage={"input_tokens": 2, "output_tokens": 3})
    third = one.acquire_turn("tenant-a")
    third.release(usage={"input_tokens": 5, "output_tokens": 5})
    with pytest.raises(ProductError) as error:
        two.acquire_turn("tenant-a")
    assert error.value.code == "token_quota_exceeded"
    assert one.tenant_usage("tenant-a")["tokens_today"] == 20

    now[0] = 86401.0
    replacement = two.acquire_turn("tenant-a")
    replacement.release()
    assert two.tenant_usage("tenant-a")["tokens_today"] == 0


def test_storage_and_audit_quota_settlement_and_deletion_credit(tmp_path):
    store = _resource_quota(
        tmp_path / "quota.db", storage_bytes_per_tenant=30,
        storage_reservation_per_turn=10, audit_bytes_per_tenant=30,
        audit_reservation_per_turn=10)
    lease = store.acquire_turn("tenant-a")
    lease.release(
        session_id="internal-one", storage_bytes=25, audit_bytes=5)
    with pytest.raises(ProductError) as error:
        store.acquire_turn("tenant-a")
    assert error.value.code == "storage_quota_exceeded"
    store.remove_session_usage("tenant-a", "internal-one")

    lease = store.acquire_turn("tenant-a")
    lease.release(
        session_id="internal-two", storage_bytes=5, audit_bytes=25)
    with pytest.raises(ProductError) as error:
        store.acquire_turn("tenant-a")
    assert error.value.code == "audit_quota_exceeded"
    store.remove_session_usage("tenant-a", "internal-two")
    assert store.tenant_usage("tenant-a")["storage_bytes"] == 0


def test_expired_crashed_turn_is_reconciled_from_durable_session_registry(tmp_path):
    now = [100.0]
    path = tmp_path / "quota.db"
    internal = _internal_session("tenant-a", "project")
    crashed = _resource_quota(path, turn_lease_s=10, clock=lambda: now[0])
    crashed.acquire_turn("tenant-a", session_id=internal)
    assert crashed.tenant_usage("tenant-a")["reserved_storage_bytes"] == 10

    now[0] = 111.0
    restarted = _resource_quota(path, turn_lease_s=10, clock=lambda: now[0])
    assert restarted.tenant_usage("tenant-a")["reserved_storage_bytes"] == 0
    assert restarted.reconcile_registered(
        lambda session: {"storage_bytes": 77, "audit_bytes": 33}) == 1
    usage = restarted.tenant_usage("tenant-a")
    assert usage["storage_bytes"] == 77 and usage["audit_bytes"] == 33


def test_registered_session_can_be_removed_unconditionally_by_hashed_identity(tmp_path):
    quota = _resource_quota(tmp_path / "quota.db")
    internal = _internal_session("tenant-a", "project")
    lease = quota.acquire_turn("tenant-a", session_id=internal)
    lease.release(session_id=internal, storage_bytes=1, audit_bytes=1)
    [(tenant_hash, session_hash, registered)] = quota.registered_sessions()
    assert registered == internal
    assert quota.remove_registered_session(tenant_hash, session_hash) is True
    assert quota.remove_registered_session(tenant_hash, session_hash) is False


def test_product_chat_settles_resource_usage_and_returns_stable_quota_error(tmp_path):
    quota = _resource_quota(
        tmp_path / "quota.db", tokens_per_day=10,
        token_reservation_per_turn=5)

    class Footprints:
        @staticmethod
        def footprint(internal_session):
            assert internal_session.startswith("v1:")
            return {"storage_bytes": 4, "audit_bytes": 5}

    service = ProductService(
        RecordingAgent(), _auth(), quota_store=quota, data_manager=Footprints(),
        log_sink=lambda record: None)
    payload = json.dumps({"session": "quota", "message": "hello"}).encode()
    assert service.dispatch("POST", "/v1/chat", _headers(CHAT_TOKEN), payload)[0] == 200
    assert service.dispatch("POST", "/v1/chat", _headers(CHAT_TOKEN), payload)[0] == 200
    status, headers, body = service.dispatch(
        "POST", "/v1/chat", _headers(CHAT_TOKEN), payload)
    assert status == 429 and body["error"]["code"] == "token_quota_exceeded"
    assert int(headers["Retry-After"]) > 0
    usage = quota.tenant_usage("tenant-a")
    assert usage["tokens_today"] == 6
    assert usage["storage_bytes"] == 4 and usage["audit_bytes"] == 5
    aggregate = quota.aggregate_usage()
    assert aggregate["tokens_today"] == 6
    assert aggregate["active_leases"] == 0
    metrics = service.dispatch("GET", "/v1/metrics", _headers(OPS_TOKEN))[2]["metrics"]
    assert metrics["tenant_quota_totals"]["tokens_today"] == 6


def test_failed_chat_settles_tokens_from_thread_local_telemetry(tmp_path):
    class Failed(RecordingAgent):
        def turn_with_usage(self, *args, **kwargs):
            raise RuntimeError("provider failed")

        def turn_telemetry(self):
            return {
                "forge_queue_wait_ms": 0, "tool_calls": 0, "retries": 1,
                "input_tokens": 2, "output_tokens": 3,
            }

    class Footprints:
        @staticmethod
        def footprint(internal_session):
            return {"storage_bytes": 1, "audit_bytes": 2}

    quota = _resource_quota(tmp_path / "quota.db")
    service = ProductService(
        Failed(), _auth(), quota_store=quota, data_manager=Footprints(),
        log_sink=lambda record: None)
    payload = json.dumps({"session": "failed", "message": "hello"}).encode()
    assert service.dispatch("POST", "/v1/chat", _headers(CHAT_TOKEN), payload)[0] == 502
    assert quota.tenant_usage("tenant-a")["tokens_today"] == 5


def test_resource_quotas_require_footprint_accounting(tmp_path):
    quota = _resource_quota(tmp_path / "quota.db")
    with pytest.raises(ConfigError, match="data manager"):
        ProductService(RecordingAgent(), _auth(), quota_store=quota)
    with pytest.raises(ConfigError, match="data manager"):
        ProductScheduler(
            ProductScheduleStore(tmp_path / "schedules.json"), RecordingAgent(),
            _auth(), quota_store=quota)


def test_graceful_drain_rejects_new_turns_and_waits_for_active_turn():
    entered = threading.Event()
    release = threading.Event()

    class Blocking(RecordingAgent):
        def turn_with_usage(self, session, message, *, allowed_tools=None,
                            deadline_monotonic=None):
            entered.set()
            assert release.wait(5)
            return "done", {}

    service = _service(Blocking())
    payload = json.dumps({"session": "s", "message": "hi"}).encode()
    first = []
    thread = threading.Thread(target=lambda: first.append(
        service.dispatch("POST", "/v1/chat", _headers(CHAT_TOKEN), payload)[0]))
    thread.start()
    assert entered.wait(5)
    service.start_draining()
    try:
        assert service.is_ready() is False
        assert service.wait_for_drain(0) is False
        status, _, body = service.dispatch(
            "POST", "/v1/chat", _headers(OTHER_TOKEN), payload)
        assert status == 503 and body["error"]["code"] == "service_draining"
    finally:
        release.set()
        thread.join(5)
    assert first == [200]
    assert service.wait_for_drain(0.1) is True


def test_structured_log_excludes_token_message_and_session():
    logs = []
    service = ProductService(RecordingAgent(), _auth(), log_sink=logs.append)
    message = "conversation-secret-value"
    session = "private-session-name"
    service.dispatch("POST", "/v1/chat", _headers(CHAT_TOKEN),
                     json.dumps({"session": session, "message": message}).encode())
    rendered = json.dumps(logs)
    assert CHAT_TOKEN not in rendered
    assert message not in rendered
    assert session not in rendered
    assert "tenant-a" not in rendered
    assert logs[-1]["principal"] == "alice"
    assert len(logs[-1]["tenant_sha256"]) == 64
    assert logs[-1]["failure_classification"] is None

    service.dispatch("POST", "/v1/chat", _headers(CHAT_TOKEN), b"not-json")
    assert logs[-1]["failure_classification"] == "invalid_json"


def test_metrics_have_no_tenant_or_principal_labels(tmp_path):
    (tmp_path / "audit").mkdir()
    (tmp_path / "audit" / "record").write_bytes(b"audit")
    service = _service(resource_root=tmp_path)
    service.dispatch(
        "POST", "/v1/chat", _headers(CHAT_TOKEN),
        json.dumps({"session": "metrics", "message": "hello"}).encode())
    service.dispatch("GET", "/v1/health", _headers(OPS_TOKEN))
    status, _, body = service.dispatch("GET", "/v1/metrics", _headers(OPS_TOKEN))
    assert status == 200
    rendered = json.dumps(body)
    assert "tenant-a" not in rendered and "alice" not in rendered
    assert body["metrics"]["requests"]
    assert body["metrics"]["input_tokens_total"] == 2
    assert body["metrics"]["output_tokens_total"] == 1
    assert body["metrics"]["tool_calls_total"] == 2
    assert body["metrics"]["retries_total"] == 1
    assert body["metrics"]["queue_wait_ms_total"] >= 0
    assert body["metrics"]["dependency_ready"] is True
    resources = body["metrics"]["resources"]
    assert resources["memory_peak_bytes"] > 0
    assert resources["thread_count"] >= 1
    assert resources["state_storage_bytes"] >= 5
    assert resources["audit_storage_bytes"] == 5


def test_audit_monitor_drives_readiness_metrics_and_safe_critical_alerts():
    reports = iter([
        {"ok": True, "chains": 2, "records": 9, "problems": []},
        {"ok": False, "chains": 2, "records": 8,
         "problems": [("secret-file", "sensitive diagnostic")]},
    ])
    logs = []
    monitor = AuditVerificationMonitor(
        lambda: next(reports), clock=lambda: 123.0, log_sink=logs.append)
    assert monitor.check() is True
    service = _service(audit_monitor=monitor)
    assert service.is_ready() is True
    assert monitor.check() is False
    assert service.is_ready() is False
    snapshot = monitor.snapshot()
    assert snapshot == {
        "checks_total": 2, "failures_total": 1, "healthy": False,
        "last_check_unix": 123.0, "chains": 2, "records": 8,
    }
    assert logs[-1]["severity"] == "critical"
    assert "secret-file" not in json.dumps(logs) and "sensitive" not in json.dumps(logs)


def test_audit_monitor_contains_verifier_exceptions_and_lifecycle():
    with pytest.raises(ConfigError, match="interval"):
        AuditVerificationMonitor(lambda: {}, interval_s=0)

    entered_in_background = threading.Event()
    calls = [0]

    def broken():
        calls[0] += 1
        if calls[0] >= 2:
            entered_in_background.set()
        raise RuntimeError("do not log me")

    logs = []
    monitor = AuditVerificationMonitor(broken, interval_s=0.01, log_sink=logs.append)
    assert AuditVerificationMonitor(lambda: {}).stop(0) is True
    assert monitor.check() is False
    monitor.start()
    monitor.start()  # starting twice must not create a second verifier thread
    assert entered_in_background.wait(1)
    assert monitor.stop(1) is True
    assert monitor.stop(1) is True
    assert monitor.snapshot()["failures_total"] >= 1
    assert "do not log me" not in json.dumps(logs)


def test_non_loopback_plaintext_is_a_startup_error():
    with pytest.raises(ConfigError, match="require TLS"):
        serve_product(_service(), host="0.0.0.0", port=0)


def test_http_adapter_preserves_auth_and_stable_product_shape():
    service = _service()
    server = serve_product(service, port=0)
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def get(path, token=None):
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        req = urllib.request.Request(base + path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, dict(response.headers), json.loads(response.read())
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), json.loads(e.read())

    try:
        assert get("/v1/version")[0] == 401
        status, headers, body = get("/v1/version", OPS_TOKEN)
        assert status == 200 and body["api_version"] == "v1"
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Request-ID"] == body["request_id"]
    finally:
        server.shutdown()
        server.server_close()


def test_shared_memory_is_rejected_for_the_product_boundary():
    agent = RecordingAgent()
    agent.memory = SimpleNamespace(scope="shared")
    with pytest.raises(ConfigError, match="session-scoped"):
        _service(agent)


def test_auth_file_contains_hashes_and_validates_policy(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"tokens": [
        _entry(CHAT_TOKEN, "alice", "tenant-a", ["chat"], ["read_file"]),
    ]}))
    registry = AuthRegistry.from_file(path)
    assert registry.authenticate(f"Bearer {CHAT_TOKEN}").tenant_id == "tenant-a"
    assert CHAT_TOKEN not in path.read_text()

    path.write_text(json.dumps({"tokens": [
        _entry(CHAT_TOKEN, "alice", "tenant-a", ["root"], []),
    ]}))
    with pytest.raises(ConfigError, match="unknown scopes"):
        AuthRegistry.from_file(path)


@pytest.mark.parametrize("entries,match", [
    (["not-an-object"], "must be an object"),
    ([{"sha256": "bad", "principal": "p", "tenant": "t", "scopes": []}], "64 lowercase"),
    ([_entry(CHAT_TOKEN, "", "t", [])], "principal must be"),
    ([_entry(CHAT_TOKEN, "p", "", [])], "tenant must be"),
    ([dict(_entry(CHAT_TOKEN, "p", "t", []), scopes="chat")], "scopes must be"),
    ([dict(_entry(CHAT_TOKEN, "p", "t", []), tools="read_file")], "tools must be"),
    ([dict(_entry(CHAT_TOKEN, "p", "t", []), tools=["*", "read_file"])], "appear alone"),
    ([_entry(CHAT_TOKEN, "p", "t", []), _entry(CHAT_TOKEN, "q", "t", [])],
     "duplicates a token digest"),
    ([], "at least one credential"),
])
def test_auth_registry_rejects_every_malformed_policy_shape(entries, match):
    with pytest.raises(ConfigError, match=match):
        AuthRegistry(entries)


def test_auth_file_missing_invalid_json_and_wrong_document_shape(tmp_path):
    with pytest.raises(ConfigError, match="does not exist"):
        AuthRegistry.from_file(tmp_path / "missing.json")
    path = tmp_path / "auth.json"
    path.write_text("{")
    with pytest.raises(ConfigError, match="not valid JSON"):
        AuthRegistry.from_file(path)
    path.write_text("[]")
    with pytest.raises(ConfigError, match="tokens list"):
        AuthRegistry.from_file(path)


def test_auth_rejects_empty_and_oversized_bearer_values():
    auth = _auth()
    for value in ("Bearer ", "Bearer " + "x" * 4097):
        with pytest.raises(Exception) as error:
            auth.authenticate(value)
        assert getattr(error.value, "code", None) == "invalid_credential"


def test_expired_and_future_credentials_are_indistinguishable_from_invalid():
    now = [100]
    active = dict(
        _entry(CHAT_TOKEN, "alice", "tenant-a", ["chat"], ["read_file"]),
        not_before_unix=90, expires_unix=110)
    future = dict(
        _entry(OTHER_TOKEN, "alice", "tenant-a", ["chat"], ["read_file"]),
        not_before_unix=110, expires_unix=120)
    auth = AuthRegistry(
        [active, future], clock=lambda: now[0], require_expiry=True,
        max_lifetime_s=30)
    assert auth.authenticate(f"Bearer {CHAT_TOKEN}").principal_id == "alice"
    for token in (OTHER_TOKEN, "unknown"):
        with pytest.raises(ProductError) as error:
            auth.authenticate(f"Bearer {token}")
        assert error.value.code == "invalid_credential"
    now[0] = 115
    assert auth.authenticate(f"Bearer {OTHER_TOKEN}").principal_id == "alice"
    assert auth.policy(hashlib.sha256(b"tenant-a").hexdigest(), "alice") is not None
    with pytest.raises(ProductError) as error:
        auth.authenticate(f"Bearer {CHAT_TOKEN}")
    assert error.value.code == "invalid_credential"


def test_overlapping_rotation_requires_identical_policy_and_bounded_lifetime():
    first = dict(
        _entry(CHAT_TOKEN, "alice", "tenant-a", ["chat"], ["read_file"]),
        not_before_unix=90, expires_unix=110)
    second = dict(
        _entry(OTHER_TOKEN, "alice", "tenant-a", ["chat"], ["read_file"]),
        not_before_unix=100, expires_unix=120)
    auth = AuthRegistry(
        [first, second], clock=lambda: 105, require_expiry=True,
        max_lifetime_s=30)
    assert auth.authenticate(f"Bearer {CHAT_TOKEN}") == auth.authenticate(
        f"Bearer {OTHER_TOKEN}")

    conflicting = dict(second, scopes=["ops:read"])
    with pytest.raises(ConfigError, match="conflicts with another credential policy"):
        AuthRegistry([first, conflicting], clock=lambda: 105, require_expiry=True)
    with pytest.raises(ConfigError, match="maximum credential lifetime"):
        AuthRegistry(
            [first], clock=lambda: 100, require_expiry=True, max_lifetime_s=10)


@pytest.mark.parametrize("entry,required,match", [
    (_entry(CHAT_TOKEN, "alice", "tenant-a", ["chat"]), True, "requires"),
    (dict(_entry(CHAT_TOKEN, "alice", "tenant-a", ["chat"]),
          not_before_unix=1), False, "both"),
    (dict(_entry(CHAT_TOKEN, "alice", "tenant-a", ["chat"]),
          not_before_unix=True, expires_unix=2), True, "integer Unix"),
    (dict(_entry(CHAT_TOKEN, "alice", "tenant-a", ["chat"]),
          not_before_unix=2, expires_unix=2), True, "must be after"),
])
def test_production_credential_validity_shape_fails_closed(entry, required, match):
    with pytest.raises(ConfigError, match=match):
        AuthRegistry([entry], clock=lambda: 1, require_expiry=required)


def test_production_registry_requires_at_least_one_current_credential():
    expired = dict(
        _entry(CHAT_TOKEN, "alice", "tenant-a", ["chat"]),
        not_before_unix=1, expires_unix=2)
    with pytest.raises(ConfigError, match="no currently active"):
        AuthRegistry([expired], clock=lambda: 3, require_expiry=True)


def test_wildcard_tool_policy_expands_to_current_manifest():
    wildcard_token = "wildcard-policy-token-with-test-only-entropy"
    auth = AuthRegistry([
        _entry(wildcard_token, "wild", "tenant-a", ["chat"], ["*"]),
    ])
    agent = RecordingAgent()
    service = ProductService(agent, auth, log_sink=lambda record: None)
    status, _, _ = service.dispatch(
        "POST", "/v1/chat", _headers(wildcard_token),
        json.dumps({"session": "s", "message": "hello"}).encode())
    assert status == 200 and agent.calls[0][2] == {"read_file", "write_file"}


def test_quota_constructors_and_expired_rate_window_are_fail_closed(tmp_path):
    with pytest.raises(ConfigError, match="requests_per_minute"):
        FixedWindowRateLimiter(0)
    with pytest.raises(ConfigError, match="max_concurrent_turns"):
        TenantConcurrency(0)
    with pytest.raises(ConfigError, match="durable quota"):
        DurableQuotaStore(tmp_path / "quota.db", turn_lease_s=0)
    now = [0.0]
    limiter = FixedWindowRateLimiter(1, clock=lambda: now[0])
    assert limiter.admit("tenant") == (True, 0)
    now[0] = 61.0
    assert limiter.admit("tenant") == (True, 0)


def test_unknown_tools_and_conflicting_rotation_policy_fail_at_startup():
    bad_tool = AuthRegistry([
        _entry(CHAT_TOKEN, "alice", "tenant-a", ["chat"], ["does_not_exist"]),
    ])
    with pytest.raises(ConfigError, match="unknown tools"):
        ProductService(RecordingAgent(), bad_tool)

    with pytest.raises(ConfigError, match="conflicts with another credential policy"):
        AuthRegistry([
            _entry(CHAT_TOKEN, "alice", "tenant-a", ["chat"]),
            _entry(OTHER_TOKEN, "alice", "tenant-a", ["ops:read"]),
        ])


def test_per_request_tool_policy_and_defensive_size_bounds_fail_closed(monkeypatch):
    service = _service()
    principal = _auth().authenticate(f"Bearer {CHAT_TOKEN}")
    service.agent.manifest = {"write_file": {}}
    with pytest.raises(ProductError) as error:
        service._allowed_tools(principal)
    assert error.value.code == "configuration_error"

    monkeypatch.setattr(product_service, "MAX_SESSION_BYTES", 0)
    with pytest.raises(ProductError) as error:
        ProductService._validate_session("a")
    assert error.value.code == "invalid_session"
    with pytest.raises(ProductError) as error:
        ProductService._validate_message(
            "x" * (product_service.MAX_MESSAGE_BYTES + 1))
    assert error.value.code == "message_too_large"


def test_schedule_names_and_listing_are_tenant_isolated(tmp_path):
    store = ProductScheduleStore(tmp_path / "schedules.json", max_per_tenant=1)
    service = ProductService(RecordingAgent(), _auth(), schedule_store=store,
                             log_sink=lambda record: None)

    def create(token, name="daily"):
        return service.dispatch(
            "POST", "/v1/schedules", _headers(token),
            json.dumps({"name": name, "session": "work", "message": "report",
                        "every_ms": 60_000}).encode())

    assert create(SCHEDULE_TOKEN)[0] == 201
    assert create(OTHER_SCHEDULE_TOKEN)[0] == 201  # same public name, other tenant
    status, _, body = create(SCHEDULE_TOKEN, "second")
    assert status == 409 and body["error"]["code"] == "schedule_quota_exceeded"

    a = service.dispatch("GET", "/v1/schedules", _headers(SCHEDULE_TOKEN))[2]["schedules"]
    b = service.dispatch("GET", "/v1/schedules", _headers(OTHER_SCHEDULE_TOKEN))[2]["schedules"]
    assert [e["name"] for e in a] == ["daily"]
    assert [e["name"] for e in b] == ["daily"]
    assert store.entries()[0][1]["internal_session"] != store.entries()[1][1]["internal_session"]

    assert service.dispatch(
        "DELETE", "/v1/schedules/daily", _headers(SCHEDULE_TOKEN))[0] == 200
    assert service.dispatch("GET", "/v1/schedules", _headers(SCHEDULE_TOKEN))[2]["schedules"] == []
    assert len(service.dispatch(
        "GET", "/v1/schedules", _headers(OTHER_SCHEDULE_TOKEN))[2]["schedules"]) == 1


@pytest.mark.parametrize("overrides,code", [
    ({"name": "../bad"}, "invalid_schedule_name"),
    ({"every_ms": True}, "invalid_interval"),
    ({"every_ms": "not-a-number"}, "invalid_interval"),
    ({"every_ms": 0}, "invalid_interval"),
])
def test_schedule_store_rejects_invalid_persistent_inputs(tmp_path, overrides, code):
    store = ProductScheduleStore(tmp_path / "schedules.json")
    principal = _auth().authenticate(f"Bearer {SCHEDULE_TOKEN}")
    values = {"name": "daily", "session": "work", "message": "report", "every_ms": 1}
    values.update(overrides)
    with pytest.raises(ProductError) as error:
        store.put(principal, **values)
    assert error.value.code == code


def test_schedule_store_corruption_missing_mutations_and_invalid_quota(tmp_path):
    with pytest.raises(ConfigError, match="max_schedules"):
        ProductScheduleStore(tmp_path / "bad.json", max_per_tenant=0)
    path = tmp_path / "schedules.json"
    path.write_text("[")
    store = ProductScheduleStore(path)
    with pytest.raises(RuntimeError, match="corrupt"):
        store.entries()
    path.write_text("[]")
    with pytest.raises(RuntimeError, match="corrupt"):
        store.entries()
    path.write_text("{}")
    assert store.remove("tenant-a", "missing") is False
    store.mark_run("missing", 1.0, "ok")
    assert store.entries() == []


def test_schedule_retention_respects_active_leases_and_legacy_store_mtime(tmp_path):
    now = [100.0]
    path = tmp_path / "schedules.json"
    store = ProductScheduleStore(path, clock=lambda: now[0])
    principal = _auth().authenticate(f"Bearer {SCHEDULE_TOKEN}")
    store.put(principal, name="daily", session="work", message="report", every_ms=1)
    [(key, entry)] = store.entries()
    assert entry["created_unix"] == entry["updated_unix"] == 100.0
    assert store.claim_due(key, 100.0, "worker", 10) is not None
    tenant_hash = hashlib.sha256("tenant-a".encode()).hexdigest()
    internal = _internal_session("tenant-a", "work")
    assert store.internal_active(tenant_hash, internal, 500.0, 105.0) is True
    assert store.internal_active(tenant_hash, internal, 500.0, 111.0) is False
    assert store.remove_inactive(500.0, 105.0) == 0
    assert store.remove_inactive(500.0, 111.0) == 1

    # A pre-retention release had no per-entry timestamps. The store mtime is
    # its conservative migration boundary.
    path.write_text(json.dumps({"legacy": {"name": "legacy"}}))
    os.utime(path, (800.0, 800.0))
    assert store.remove_inactive(500.0, 1000.0) == 0
    os.utime(path, (100.0, 100.0))
    assert store.remove_inactive(500.0, 1000.0) == 1


def test_schedule_scheduler_rejects_invalid_lease_and_marks_invalid_tool_policy(tmp_path):
    with pytest.raises(ConfigError, match="lease_s"):
        ProductScheduler(
            ProductScheduleStore(tmp_path / "none.json"), RecordingAgent(), _auth(), lease_s=0)

    token = "invalid-scheduler-policy-token-with-test-entropy"
    auth = AuthRegistry([
        _entry(token, "planner", "tenant-a", ["schedules:write"], ["missing-tool"]),
    ])
    principal = auth.authenticate(f"Bearer {token}")
    store = ProductScheduleStore(tmp_path / "schedules.json")
    store.put(principal, name="daily", session="work", message="report", every_ms=1)
    scheduler = ProductScheduler(
        store, RecordingAgent(), auth, clock=SimpleNamespace(time=lambda: 100.0))
    scheduler.tick()
    assert store.entries()[0][1]["last_status"] == "policy_invalid"


def test_unconfigured_optional_routes_and_readiness_fail_stably():
    service = _service(readiness=lambda: False)
    assert service.dispatch("GET", "/v1/ready", _headers(OPS_TOKEN))[0] == 503
    calls = [
        ("GET", "/v1/schedules", SCHEDULE_TOKEN, b""),
        ("POST", "/v1/schedules", SCHEDULE_TOKEN,
         json.dumps({"name": "n", "session": "s", "message": "m", "every_ms": 1}).encode()),
        ("DELETE", "/v1/schedules/n", SCHEDULE_TOKEN, b""),
        ("GET", "/v1/sessions/s/export", DATA_TOKEN, b""),
        ("DELETE", "/v1/sessions/s", DATA_TOKEN, b""),
    ]
    for method, path, token, body in calls:
        status, _, payload = service.dispatch(method, path, _headers(token), body)
        assert status == 503 and payload["error"]["code"].endswith("unavailable")


def test_invalid_routes_names_waits_and_transport_fail_closed(tmp_path):
    store = ProductScheduleStore(tmp_path / "schedules.json")
    service = _service(schedule_store=store)
    assert service.dispatch(
        "DELETE", "/v1/schedules/%2E%2E", _headers(SCHEDULE_TOKEN))[2]["error"][
            "code"] == "invalid_schedule_name"
    assert service.dispatch(
        "DELETE", "/v1/schedules/missing", _headers(SCHEDULE_TOKEN))[2]["error"][
            "code"] == "schedule_not_found"
    with pytest.raises(ValueError, match="non-negative"):
        service.wait_for_drain(-1)
    with pytest.raises(ConfigError, match="non-empty"):
        validate_transport("", None)
    validate_transport("localhost", None)
    with pytest.raises(ConfigError, match="socket_timeout"):
        serve_product(service, port=0, socket_timeout_s=0)


def test_unexpected_readiness_failure_fails_closed_without_diagnostics():
    def broken():
        raise RuntimeError("secret dependency diagnostic")

    status, _, body = _service(readiness=broken).dispatch(
        "GET", "/v1/ready", _headers(OPS_TOKEN))
    assert status == 503
    assert body["status"] == "not_ready"
    assert body["dependencies"] == {"runtime": False}
    assert body["draining"] is False
    assert "secret" not in json.dumps(body)


def test_readiness_names_every_configured_dependency_and_fails_conjunctively(tmp_path):
    class Probe:
        def __init__(self, healthy=True):
            self.healthy = healthy

        def is_healthy(self):
            return self.healthy

        def snapshot(self):
            return {"healthy": self.healthy}

    quota = DurableQuotaStore(tmp_path / "quota.sqlite3")
    schedules = ProductScheduleStore(tmp_path / "schedules.json")
    audit, retention, state = Probe(), Probe(), Probe()
    service = _service(
        quota_store=quota, schedule_store=schedules, data_manager=state,
        audit_monitor=audit, retention_monitor=retention)
    expected = {
        "audit_verification": True,
        "quota_store": True,
        "retention": True,
        "runtime": True,
        "schedule_store": True,
        "state_storage": True,
    }
    assert service.readiness_snapshot() == {
        "ready": True, "draining": False, "dependencies": expected}
    status, _, body = service.dispatch("GET", "/v1/ready", _headers(OPS_TOKEN))
    assert status == 200 and body["dependencies"] == expected

    state.healthy = False
    snapshot = service.readiness_snapshot()
    assert snapshot["ready"] is False
    assert snapshot["dependencies"]["state_storage"] is False
    metrics = service.operational_metrics()
    assert metrics["dependency_ready"] is False
    assert metrics["dependency_components"] == snapshot["dependencies"]

    service.start_draining()
    snapshot = service.readiness_snapshot()
    assert snapshot["draining"] is True and snapshot["ready"] is False


def test_durable_dependency_probes_detect_corrupt_operational_stores(tmp_path):
    quota_path = tmp_path / "quota.sqlite3"
    quota = DurableQuotaStore(quota_path)
    assert quota.is_healthy() is True
    quota_path.write_bytes(b"not a sqlite database")
    assert quota.is_healthy() is False

    schedule_path = tmp_path / "schedules.json"
    schedules = ProductScheduleStore(schedule_path)
    assert schedules.is_healthy() is True
    schedule_path.write_text("[]")
    assert schedules.is_healthy() is False
    schedule_path.write_text("{")
    assert schedules.is_healthy() is False
    schedule_path.write_bytes(b"\xff")
    assert schedules.is_healthy() is False
    schedule_path.unlink()
    schedule_path.symlink_to(tmp_path / "missing-schedules")
    assert schedules.is_healthy() is False


def test_scheduler_runs_under_creators_current_tool_policy(tmp_path):
    auth = _auth()
    store = ProductScheduleStore(tmp_path / "schedules.json")
    principal = auth.authenticate(f"Bearer {SCHEDULE_TOKEN}")
    store.put(principal, name="daily", session="work", message="report", every_ms=60_000)
    agent = RecordingAgent()
    clock = SimpleNamespace(time=lambda: 100.0)
    scheduler = ProductScheduler(store, agent, auth, clock=clock)
    scheduler.tick()
    [(session, message, tools)] = agent.calls
    assert session == _internal_session("tenant-a", "work")
    assert message == "report" and tools == {"read_file"}
    [(key, entry)] = store.entries()
    assert key.endswith(":daily") and entry["last_status"] == "ok"


def test_scheduler_settles_resources_and_marks_quota_exhaustion(tmp_path):
    now = [100.0]
    auth = _auth()
    store = ProductScheduleStore(tmp_path / "schedules.json")
    principal = auth.authenticate(f"Bearer {SCHEDULE_TOKEN}")
    store.put(principal, name="daily", session="work", message="report", every_ms=1)
    quota = _resource_quota(
        tmp_path / "quota.db", tokens_per_day=5,
        token_reservation_per_turn=5, clock=lambda: now[0])

    class Footprints:
        @staticmethod
        def footprint(internal_session):
            return {"storage_bytes": 7, "audit_bytes": 8}

    agent = RecordingAgent()
    scheduler = ProductScheduler(
        store, agent, auth, clock=SimpleNamespace(time=lambda: now[0]),
        quota_store=quota, data_manager=Footprints())
    scheduler.tick()
    assert quota.tenant_usage("tenant-a") == {
        "tokens_today": 3,
        "storage_bytes": 7,
        "audit_bytes": 8,
        "reserved_tokens": 0,
        "reserved_storage_bytes": 0,
        "reserved_audit_bytes": 0,
    }
    now[0] = 101.0
    scheduler.tick()
    assert len(agent.calls) == 1
    assert store.entries()[0][1]["last_status"] == "quota_exceeded"


def test_scheduler_does_not_run_after_creator_policy_is_revoked(tmp_path):
    original = _auth()
    store = ProductScheduleStore(tmp_path / "schedules.json")
    principal = original.authenticate(f"Bearer {SCHEDULE_TOKEN}")
    store.put(principal, name="daily", session="work", message="report", every_ms=60_000)
    revoked = AuthRegistry([
        _entry(OPS_TOKEN, "operator", "ops", ["ops:read"]),
    ])
    agent = RecordingAgent()
    scheduler = ProductScheduler(
        store, agent, revoked, clock=SimpleNamespace(time=lambda: 100.0))
    scheduler.tick()
    assert agent.calls == []
    assert store.entries()[0][1]["last_status"] == "policy_revoked"


def _data_agent(tmp_path):
    from agent import AuditLog, SessionStore

    return SimpleNamespace(
        manifest={"read_file": {}, "write_file": {}},
        memory=None,
        _mcp=object(),
        store=SessionStore(tmp_path / "sessions"),
        sandbox_root=tmp_path / "sandboxes",
        audit=AuditLog(tmp_path / "audit", key=b"audit-test-key"),
    )


def _seed_session(agent, tenant, external, marker):
    internal = _internal_session(tenant, external)
    agent.store.save(internal, [{"role": "user", "content": marker}])
    sandbox = (agent.sandbox_root
               / hashlib.sha256(internal.encode()).hexdigest()[:16])
    sandbox.mkdir(parents=True)
    (sandbox / "note.txt").write_text(marker)
    agent.audit.record(internal, "test", "source", marker, "output", None, None, 10)
    return internal


def test_export_and_delete_cover_supported_state_without_crossing_tenants(tmp_path):
    agent = _data_agent(tmp_path)
    a_internal = _seed_session(agent, "tenant-a", "project", "A-private")
    b_internal = _seed_session(agent, "tenant-b", "project", "B-private")
    store = ProductScheduleStore(tmp_path / "schedules.json")
    schedule_principal = _auth().authenticate(f"Bearer {SCHEDULE_TOKEN}")
    store.put(schedule_principal, name="daily", session="project",
              message="report", every_ms=60_000)
    locks = SessionOperationLocks()
    manager = ProductDataManager(agent, store, locks)
    principal = _auth().authenticate(f"Bearer {DATA_TOKEN}")

    exported = manager.export(principal, "project")
    rendered = json.dumps(exported)
    assert exported["schema_version"] == 1
    assert "A-private" in rendered and "B-private" not in rendered
    assert exported["files"][0]["path"] == "note.txt"
    assert exported["schedules"][0]["name"] == "daily"
    assert exported["audit"]

    deleted = manager.delete(principal, "project")
    assert deleted == {"conversation": True, "sandbox": True,
                       "audit": True, "schedules": 1}
    assert agent.store.load(a_internal) == []
    assert agent.store.load(b_internal)[0]["content"] == "B-private"
    assert store.for_tenant("tenant-a") == []
    with pytest.raises(Exception) as error:
        manager.export(principal, "project")
    assert getattr(error.value, "code", None) == "session_not_found"
    with pytest.raises(ProductError) as error:
        manager.delete(principal, "project")
    assert error.value.code == "session_not_found"
    assert locks.size() == 0


def test_data_manager_health_requires_real_accessible_state_roots(tmp_path):
    agent = _data_agent(tmp_path)
    _seed_session(agent, "tenant-a", "project", "private")
    manager = ProductDataManager(
        agent, ProductScheduleStore(tmp_path / "schedules.json"),
        SessionOperationLocks())
    assert manager.is_healthy() is True
    audit_dir = Path(agent.audit.dir)
    offline = tmp_path / "audit-offline"
    audit_dir.rename(offline)
    assert manager.is_healthy() is False
    audit_dir.symlink_to(offline, target_is_directory=True)
    assert manager.is_healthy() is False


def test_export_bounds_nested_directories_and_non_sandbox_state(tmp_path):
    agent = _data_agent(tmp_path)
    internal = _seed_session(agent, "tenant-a", "project", "private")
    sandbox = (Path(agent.sandbox_root)
               / hashlib.sha256(internal.encode()).hexdigest()[:16])
    (sandbox / "empty-directory").mkdir()
    store = ProductScheduleStore(tmp_path / "schedules.json")
    locks = SessionOperationLocks()
    principal = _auth().authenticate(f"Bearer {DATA_TOKEN}")
    manager = ProductDataManager(agent, store, locks)
    assert manager.export(principal, "project")["files"][0]["path"] == "note.txt"

    bounded = ProductDataManager(agent, store, locks, max_export_bytes=1)
    with pytest.raises(ProductError) as error:
        bounded.export(principal, "project")
    assert error.value.code == "export_too_large"

    conversation_only = _internal_session("tenant-a", "conversation-only")
    agent.store.save(conversation_only, [{"role": "user", "content": "too large"}])
    with pytest.raises(ProductError) as error:
        bounded.export(principal, "conversation-only")
    assert error.value.code == "export_too_large"


def test_data_manager_footprint_is_exact_and_delete_credits_quota(tmp_path):
    agent = _data_agent(tmp_path)
    internal = _seed_session(agent, "tenant-a", "project", "private")
    quota = _resource_quota(tmp_path / "quota.db")
    store = ProductScheduleStore(tmp_path / "schedules.json")
    locks = SessionOperationLocks()
    manager = ProductDataManager(
        agent, store, locks, quota_store=quota)
    footprint = manager.footprint(internal)
    expected_storage = agent.store._path(internal).stat().st_size
    sandbox = (agent.sandbox_root
               / hashlib.sha256(internal.encode()).hexdigest()[:16])
    expected_storage += (sandbox / "note.txt").stat().st_size
    assert footprint == {
        "storage_bytes": expected_storage,
        "audit_bytes": agent.audit._path(internal).stat().st_size,
        "last_activity_unix": max(
            agent.store._path(internal).stat().st_mtime,
            (sandbox / "note.txt").stat().st_mtime,
            agent.audit._path(internal).stat().st_mtime),
    }
    lease = quota.acquire_turn("tenant-a")
    lease.release(session_id=internal, **footprint)
    assert quota.tenant_usage("tenant-a")["storage_bytes"] == expected_storage

    principal = _auth().authenticate(f"Bearer {DATA_TOKEN}")
    manager.delete(principal, "project")
    usage = quota.tenant_usage("tenant-a")
    assert usage["storage_bytes"] == 0 and usage["audit_bytes"] == 0


def test_data_manager_refuses_unregistered_customer_state(tmp_path):
    agent = _data_agent(tmp_path)
    internal = _seed_session(agent, "tenant-a", "project", "private")
    quota = _resource_quota(tmp_path / "quota.db")
    manager = ProductDataManager(
        agent, ProductScheduleStore(tmp_path / "schedules.json"),
        SessionOperationLocks(), quota_store=quota)
    with pytest.raises(ConfigError, match="supported state migration"):
        manager.verify_quota_registry(quota.registered_sessions())

    lease = quota.acquire_turn("tenant-a", session_id=internal)
    lease.release(session_id=internal, **manager.footprint(internal))
    manager.verify_quota_registry(quota.registered_sessions())


def _set_session_mtime(agent, internal, when):
    session = agent.store._path(internal)
    sandbox_file = (Path(agent.sandbox_root)
                    / hashlib.sha256(internal.encode()).hexdigest()[:16]
                    / "note.txt")
    audit = agent.audit._path(internal)
    for path in (session, sandbox_file, audit):
        os.utime(path, (when, when))


def test_retention_deletes_only_inactive_registered_and_schedule_only_state(tmp_path):
    now = [100.0]
    agent = _data_agent(tmp_path)
    schedule_store = ProductScheduleStore(
        tmp_path / "schedules.json", clock=lambda: now[0])
    locks = SessionOperationLocks(tmp_path / "locks")
    quota = _resource_quota(
        tmp_path / "quota.db", clock=lambda: now[0],
        storage_bytes_per_tenant=10_000, audit_bytes_per_tenant=10_000)
    manager = ProductDataManager(
        agent, schedule_store, locks, quota_store=quota)
    principal = _auth().authenticate(f"Bearer {SCHEDULE_TOKEN}")

    old = _seed_session(agent, "tenant-a", "old", "expired")
    _set_session_mtime(agent, old, 100.0)
    schedule_store.put(principal, name="old-job", session="old",
                       message="report", every_ms=60_000)
    old_lease = quota.acquire_turn("tenant-a", session_id=old)
    old_lease.release(session_id=old, **manager.footprint(old))

    old_two = _seed_session(agent, "tenant-a", "old-two", "also-expired")
    _set_session_mtime(agent, old_two, 100.0)
    old_two_lease = quota.acquire_turn("tenant-a", session_id=old_two)
    old_two_lease.release(session_id=old_two, **manager.footprint(old_two))

    # This entry has never executed and therefore has no quota registry row.
    schedule_store.put(principal, name="schedule-only", session="never-ran",
                       message="report", every_ms=60_000)

    now[0] = 800.0
    recent = _seed_session(agent, "tenant-a", "recent", "keep")
    _set_session_mtime(agent, recent, 800.0)
    recent_lease = quota.acquire_turn("tenant-a", session_id=recent)
    recent_lease.release(session_id=recent, **manager.footprint(recent))

    scheduled = _seed_session(agent, "tenant-a", "scheduled", "keep-by-schedule")
    _set_session_mtime(agent, scheduled, 100.0)
    now[0] = 100.0
    scheduled_lease = quota.acquire_turn("tenant-a", session_id=scheduled)
    scheduled_lease.release(session_id=scheduled, **manager.footprint(scheduled))
    now[0] = 800.0
    schedule_store.put(principal, name="fresh-job", session="scheduled",
                       message="report", every_ms=60_000)

    now[0] = 1000.0
    logs = []
    monitor = ProductRetentionMonitor(
        quota, manager, schedule_store, locks, retention_s=500,
        clock=lambda: now[0], log_sink=logs.append)
    assert monitor.tick() is True
    assert not agent.store._path(old).exists()
    assert not agent.store._path(old_two).exists()
    assert agent.store._path(recent).exists()
    assert agent.store._path(scheduled).exists()
    assert {entry[1]["name"] for entry in schedule_store.entries()} == {"fresh-job"}
    assert {row[2] for row in quota.registered_sessions()} == {recent, scheduled}
    assert monitor.snapshot()["deleted_sessions_total"] == 2
    assert monitor.snapshot()["deleted_schedules_total"] == 2
    assert "tenant-a" not in json.dumps(logs)
    assert old not in json.dumps(logs)


def test_retention_rechecks_activity_after_waiting_for_session_lock(tmp_path):
    now = [100.0]
    agent = _data_agent(tmp_path)
    schedules = ProductScheduleStore(tmp_path / "schedules.json")
    locks = SessionOperationLocks(tmp_path / "locks")
    quota = _resource_quota(tmp_path / "quota.db", clock=lambda: now[0])
    manager = ProductDataManager(agent, schedules, locks, quota_store=quota)
    internal = _seed_session(agent, "tenant-a", "project", "keep")
    _set_session_mtime(agent, internal, 100.0)
    lease = quota.acquire_turn("tenant-a", session_id=internal)
    lease.release(session_id=internal, **manager.footprint(internal))

    original_candidates = quota.inactive_sessions

    def refresh_after_snapshot(cutoff):
        candidates = original_candidates(cutoff)
        refresh = quota.acquire_turn("tenant-a", session_id=internal)
        refresh.release()
        return candidates

    quota.inactive_sessions = refresh_after_snapshot
    now[0] = 1000.0
    monitor = ProductRetentionMonitor(
        quota, manager, schedules, locks, retention_s=500,
        clock=lambda: now[0], log_sink=lambda record: None)
    assert monitor.tick() is True
    assert agent.store._path(internal).exists()
    assert quota.registered_sessions()[0][2] == internal
    assert monitor.snapshot()["deleted_sessions_total"] == 0


def test_retention_late_refresh_keeps_registry_after_expired_files_are_committed(
        tmp_path, monkeypatch):
    now = [100.0]
    agent = _data_agent(tmp_path)
    schedules = ProductScheduleStore(tmp_path / "schedules.json")
    locks = SessionOperationLocks(tmp_path / "locks")
    quota = _resource_quota(tmp_path / "quota.db", clock=lambda: now[0])
    manager = ProductDataManager(agent, schedules, locks, quota_store=quota)
    internal = _seed_session(agent, "tenant-a", "project", "expired")
    _set_session_mtime(agent, internal, 100.0)
    lease = quota.acquire_turn("tenant-a", session_id=internal)
    lease.release(session_id=internal, **manager.footprint(internal))
    now[0] = 1000.0
    original_remove = quota.remove_registered_session

    def refresh_then_remove(tenant, session, *, cutoff_unix=None):
        refresh = quota.acquire_turn("tenant-a", session_id=internal)
        refresh.release()
        return original_remove(
            tenant, session, cutoff_unix=cutoff_unix)

    monkeypatch.setattr(quota, "remove_registered_session", refresh_then_remove)
    monitor = ProductRetentionMonitor(
        quota, manager, schedules, locks, retention_s=500,
        clock=lambda: now[0], log_sink=lambda record: None)
    assert monitor.tick() is True
    assert not agent.store._path(internal).exists()
    assert quota.registered_sessions()[0][2] == internal
    assert monitor.snapshot()["deleted_sessions_total"] == 0


def test_retention_failure_keeps_registry_for_safe_retry_and_drives_readiness(
        tmp_path, monkeypatch):
    now = [100.0]
    agent = _data_agent(tmp_path)
    schedules = ProductScheduleStore(tmp_path / "schedules.json")
    locks = SessionOperationLocks(tmp_path / "locks")
    quota = _resource_quota(tmp_path / "quota.db", clock=lambda: now[0])
    manager = ProductDataManager(agent, schedules, locks, quota_store=quota)
    internal = _seed_session(agent, "tenant-a", "project", "expired")
    _set_session_mtime(agent, internal, 100.0)
    lease = quota.acquire_turn("tenant-a", session_id=internal)
    lease.release(session_id=internal, **manager.footprint(internal))
    now[0] = 1000.0
    logs = []
    monitor = ProductRetentionMonitor(
        quota, manager, schedules, locks, retention_s=500,
        clock=lambda: now[0], log_sink=logs.append, precheck=lambda: True)
    service = ProductService(
        agent, _auth(), quota_store=quota, data_manager=manager,
        retention_monitor=monitor, log_sink=lambda record: None)
    original_remove = quota.remove_registered_session

    def broken_remove(*args, **kwargs):
        raise RuntimeError("sensitive cleanup detail")

    monkeypatch.setattr(quota, "remove_registered_session", broken_remove)
    assert monitor.tick() is False
    assert service.is_ready() is False
    assert not agent.store._path(internal).exists()
    assert quota.registered_sessions()[0][2] == internal
    assert logs[-1]["severity"] == "critical"
    assert "sensitive cleanup detail" not in json.dumps(logs)

    monkeypatch.setattr(quota, "remove_registered_session", original_remove)
    assert monitor.tick() is True
    assert service.is_ready() is True
    status, _, body = service.dispatch(
        "GET", "/v1/metrics", _headers(OPS_TOKEN))
    assert status == 200
    assert body["metrics"]["retention"]["failures_total"] == 1
    assert quota.registered_sessions() == ()


def test_retention_configuration_precheck_and_lifecycle(tmp_path):
    agent = _data_agent(tmp_path)
    schedules = ProductScheduleStore(tmp_path / "schedules.json")
    locks = SessionOperationLocks(tmp_path / "locks")
    quota = _resource_quota(tmp_path / "quota.db")
    manager = ProductDataManager(agent, schedules, locks, quota_store=quota)
    with pytest.raises(ConfigError, match="retention_s"):
        ProductRetentionMonitor(
            quota, manager, schedules, locks, retention_s=0)
    with pytest.raises(ConfigError, match="interval"):
        ProductRetentionMonitor(
            quota, manager, schedules, locks, retention_s=1, interval_s=0)

    monitor = ProductRetentionMonitor(
        quota, manager, schedules, locks, retention_s=1, interval_s=0.01,
        precheck=lambda: False, log_sink=lambda record: None)
    assert monitor.tick() is False
    monitor.precheck = lambda: True
    monitor.start()
    monitor.start()
    deadline = time.monotonic() + 1
    while monitor.snapshot()["checks_total"] < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert monitor.stop(1) is True
    assert monitor.stop(1) is True
    assert monitor.snapshot()["healthy"] is True


def test_session_lifecycle_routes_have_distinct_scopes(tmp_path):
    agent = _data_agent(tmp_path)
    _seed_session(agent, "tenant-a", "project", "A-private")
    schedules = ProductScheduleStore(tmp_path / "schedules.json")
    locks = SessionOperationLocks()
    manager = ProductDataManager(agent, schedules, locks)
    service = ProductService(agent, _auth(), schedule_store=schedules,
                             session_locks=locks, data_manager=manager,
                             log_sink=lambda record: None)

    export_path = "/v1/sessions/project/export"
    assert service.dispatch("GET", export_path, _headers(CHAT_TOKEN))[0] == 403
    status, _, body = service.dispatch("GET", export_path, _headers(DATA_TOKEN))
    assert status == 200 and body["data"]["session"] == "project"
    assert service.dispatch(
        "DELETE", "/v1/sessions/project", _headers(CHAT_TOKEN))[0] == 403
    assert service.dispatch(
        "DELETE", "/v1/sessions/project", _headers(DATA_TOKEN))[0] == 200


def test_stale_schedule_snapshot_cannot_resurrect_deleted_session(tmp_path):
    auth = _auth()
    store = ProductScheduleStore(tmp_path / "schedules.json")
    principal = auth.authenticate(f"Bearer {SCHEDULE_TOKEN}")
    store.put(principal, name="daily", session="work", message="report", every_ms=1)
    stale = store.entries()
    assert store.remove_session("tenant-a", "work") == 1
    store.entries = lambda: stale
    agent = RecordingAgent()
    scheduler = ProductScheduler(
        store, agent, auth, clock=SimpleNamespace(time=lambda: 100.0))
    scheduler.tick()
    assert agent.calls == []


def test_schedule_lease_allows_only_one_worker_to_claim_due_entry(tmp_path):
    auth = _auth()
    store = ProductScheduleStore(tmp_path / "schedules.json")
    principal = auth.authenticate(f"Bearer {SCHEDULE_TOKEN}")
    store.put(principal, name="daily", session="work", message="report", every_ms=1)
    [(key, _)] = store.entries()
    assert store.claim_due(key, 100.0, "worker-one", 300) is not None

    agent = RecordingAgent()
    worker_two = ProductScheduler(
        store, agent, auth, clock=SimpleNamespace(time=lambda: 100.0))
    worker_two.tick()
    assert agent.calls == []
    assert store.complete_claim(key, "worker-two", 100.0, "ok") is False
    assert store.complete_claim(key, "worker-one", 100.0, "ok") is True


def test_cross_worker_session_file_lock_serializes_operation(tmp_path):
    first = SessionOperationLocks(tmp_path / "locks")
    second = SessionOperationLocks(tmp_path / "locks")
    entered = threading.Event()

    def wait_for_lock():
        with second.hold("same-internal-session"):
            entered.set()

    with first.hold("same-internal-session"):
        thread = threading.Thread(target=wait_for_lock)
        thread.start()
        assert entered.wait(0.05) is False
    assert entered.wait(2)
    thread.join(2)
    assert first.size() == second.size() == 0


def test_session_operation_wait_respects_hard_deadline(tmp_path):
    locks = SessionOperationLocks(tmp_path / "locks")
    with locks.hold("session"):
        with pytest.raises(TimeoutError, match="session operation"):
            with locks.hold("session", deadline_monotonic=time.monotonic() + 0.01):
                raise AssertionError("deadline lock unexpectedly acquired")
    assert locks.size() == 0


def test_two_schedule_store_instances_do_not_lose_updates(tmp_path):
    path = tmp_path / "schedules.json"
    one = ProductScheduleStore(path)
    two = ProductScheduleStore(path)
    auth = _auth()
    a = auth.authenticate(f"Bearer {SCHEDULE_TOKEN}")
    b = auth.authenticate(f"Bearer {OTHER_SCHEDULE_TOKEN}")
    barrier = threading.Barrier(2)

    def put(store, principal, name):
        barrier.wait()
        store.put(principal, name=name, session="work", message="report", every_ms=1)

    threads = [threading.Thread(target=put, args=(one, a, "a")),
               threading.Thread(target=put, args=(two, b, "b"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    assert sorted(entry["name"] for _, entry in one.entries()) == ["a", "b"]


def test_export_refuses_symlinks_instead_of_following_them(tmp_path):
    agent = _data_agent(tmp_path)
    internal = _seed_session(agent, "tenant-a", "project", "private")
    sandbox = (agent.sandbox_root
               / hashlib.sha256(internal.encode()).hexdigest()[:16])
    (sandbox / "link").symlink_to(tmp_path / "outside")
    manager = ProductDataManager(
        agent, ProductScheduleStore(tmp_path / "schedules.json"),
        SessionOperationLocks())
    principal = _auth().authenticate(f"Bearer {DATA_TOKEN}")
    with pytest.raises(Exception) as error:
        manager.export(principal, "project")
    assert getattr(error.value, "code", None) == "unsafe_session_data"


def test_model_cannot_dispatch_a_tool_hidden_by_product_authorization(tmp_path):
    """Tool filtering is not merely advisory: hostile model output naming a
    hidden tool must become an error result without invoking dispatch."""
    from agent import PiAgent, SessionStore

    agent = PiAgent("http://127.0.0.1:9/v1/messages", "unused",
                    store=SessionStore(tmp_path / "sessions"),
                    sandbox_root=tmp_path / "sandboxes", mcp=None)
    payloads = []
    agent._llm = lambda payload, session=None: payloads.append(payload) or "raw"
    replies = iter([
        [("tool_use", "t1", "read_file", {"path": "secret.txt"})],
        [("text", "denied as expected")],
    ])
    agent._parse = lambda raw, session=None: next(replies)
    called = []
    agent._dispatch = lambda *args, **kwargs: called.append(args) or ("bad", False)

    reply, _ = agent.turn_with_usage("tenant-session", "try it", allowed_tools=set())
    assert reply == "denied as expected"
    assert called == []
    assert "tools" not in payloads[0]
    result_carrier = next(
        m for m in reversed(payloads[1]["messages"])
        if m["role"] == "user" and isinstance(m["content"], list))
    result = result_carrier["content"][0]
    assert result["is_error"] is True
    assert result["content"] == "tool not authorized: read_file"


def test_real_agent_deadline_bounds_forge_and_persists_partial_turn(tmp_path):
    """Cancellation kills work at the forge boundary while the turn's finally
    still commits the bounded partial transcript."""
    from agent import PiAgent, SessionStore

    class SlowRuntime:
        def forge(self, *args, timeout_s=None, **kwargs):
            assert timeout_s is not None and timeout_s > 0
            time.sleep(timeout_s + 0.005)
            raise RuntimeError("simulated wedged compiler")

    store = SessionStore(tmp_path / "sessions")
    agent = PiAgent(
        "http://127.0.0.1:9/v1/messages", "unused", store=store,
        sandbox_root=tmp_path / "sandboxes", mcp=SlowRuntime())
    # SlowRuntime ignores its source, so composing the real one would only
    # couple this deadline test to a stdlib on disk (the research host's
    # retry probes take the same shortcut). The deadline path under test is
    # _forge's, which runs regardless.
    agent._llm_src = "stub: SlowRuntime ignores its source"
    with pytest.raises(TimeoutError, match="deadline exceeded"):
        agent.turn_with_usage(
            "internal-session", "committed before cancellation",
            allowed_tools=set(), deadline_monotonic=time.monotonic() + 0.01)
    assert store.load("internal-session")[0]["content"] == "committed before cancellation"
    assert agent.audit.read("internal-session")[0]["error"] == "turn_deadline_exceeded"
    assert agent.turn_telemetry()["forge_queue_wait_ms"] >= 0


def test_one_tenants_expired_turn_does_not_end_forging_for_another_tenant(tmp_path):
    """END TO END, against a REAL compiler, for the bug reproduced 2026-08-23.

    Tenant A's turn expires while a forge is genuinely IN FLIGHT — the provider
    never answers, so the kill lands mid-forge, which is the only path that
    reaches it (a provider that merely refuses returns a fast 502 and the
    compiler is never touched). The hard deadline does what it promises and A
    gets its 504; tenant B, who did nothing wrong, must still be served.

    Before SupervisedRuntime, B got `502 agent_failure` here forever: the single
    killed client latched `_closed` and nothing respawned.
    """
    import toolchain
    from conftest import needs_toolchain
    from runtime_client import ProductionSigilMCP, SupervisedRuntime
    from agent import PiAgent, SessionStore

    needs_toolchain()
    forge_bin = toolchain.resolve().forge_bin

    class Blackhole(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            time.sleep(30)          # never answers within the turn's budget

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Blackhole)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}/v1/messages"

    runtime = SupervisedRuntime(
        lambda: ProductionSigilMCP.spawn(forge_bin, timeout_s=30.0))
    try:
        with runtime:
            agent = PiAgent(
                endpoint, "unused", store=SessionStore(tmp_path / "sessions"),
                sandbox_root=tmp_path / "sandboxes", mcp=runtime,
                net_allowlist=["127.0.0.1"], llm_retries=0)

            with pytest.raises(TimeoutError, match="deadline exceeded"):
                agent.turn_with_usage("tenant-a", "outruns its budget",
                                      allowed_tools=set(),
                                      deadline_monotonic=time.monotonic() + 1.5)
            assert runtime.generation == 1, "the killed compiler is retired lazily"

            # Tenant B: a real forge, no deadline pressure, fresh generation.
            parse_src = agent._compose(
                (PI_ROOT / "tools" / "parse_reply.sigil").read_text(), ["json"])
            reply = json.dumps({
                "id": "m", "type": "message", "role": "assistant", "model": "m",
                "content": [{"type": "text", "text": "tenant B is served"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1}})
            out, err = agent._forge(parse_src, reply, None, kind="parse",
                                    session="tenant-b")
            assert err is None, f"tenant B was refused by a dead compiler: {err}"
            assert "tenant B is served" in out
            assert runtime.generation == 2, "B must be served by a NEW generation"
            assert runtime.is_healthy is True
            assert runtime.unhealthy_replacements == 0, (
                "a turn killing its own compiler is the deadline working, "
                "not a fault")
    finally:
        server.shutdown()
        server.server_close()
