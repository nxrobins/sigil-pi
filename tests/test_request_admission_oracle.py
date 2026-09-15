"""Legacy request-admission observations, NOT a migrated-service qualification.

These use the real Python dispatcher and its real temporary durable quota store.
No provider, native worker, external credential, or production data is involved.
Numbers are boundary fixtures, not an approved pilot configuration.
"""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

import product_service
from product_service import AuthRegistry, DurableQuotaStore, ProductService


INVENTORY = json.loads(
    (Path(product_service.__file__).parent / "config/api-migration.json").read_text())
ROUTES = INVENTORY["legacy_routes"]
TOKEN_A = "request-admission-inert-tenant-a-original-no-external-authority"
TOKEN_A_ROTATED = "request-admission-inert-tenant-a-rotated-no-external-authority"
TOKEN_B = "request-admission-inert-tenant-b-no-external-authority"
REQUEST_ID = "request-rate-oracle"


class Clock:
    def __init__(self, now=150.0):
        self.now = now

    def __call__(self):
        return self.now


class NoWorkAgent:
    manifest = {"read_file": {}}
    memory = None
    _mcp = object()

    def __init__(self):
        self.calls = []

    def turn_with_usage(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("reference request-admission cases must not run a turn")


def oracle(path, *, scopes=(), limit=2, clock=None):
    clock = clock or Clock()
    rows = []
    for token, tenant, principal in (
            (TOKEN_A, "tenant-a", "principal-a"),
            (TOKEN_A_ROTATED, "tenant-a", "principal-a"),
            (TOKEN_B, "tenant-b", "principal-b")):
        rows.append({
            "sha256": hashlib.sha256(token.encode()).hexdigest(),
            "tenant": tenant, "principal": principal,
            "scopes": list(scopes), "tools": ["read_file"],
            "not_before_unix": 100, "expires_unix": 10000,
        })
    registry = AuthRegistry(rows, clock=clock, require_expiry=True)
    quota = DurableQuotaStore(path, requests_per_minute=limit, clock=clock)
    agent = NoWorkAgent()
    records = []
    service = ProductService(agent, registry, quota_store=quota,
                             version="request-rate-oracle", log_sink=records.append)
    # Exercise the existing emergency limiter with its actual implementation,
    # using deterministic clock facts rather than sleeps or a replacement policy.
    service._readiness_emergency_rate.clock = clock
    return SimpleNamespace(service=service, quota=quota, agent=agent,
                           records=records, clock=clock)


def request(case, method="GET", path="/v1/health", *, token=TOKEN_A,
            authorization=None, body=b""):
    headers = {"X-Request-ID": REQUEST_ID}
    if authorization is not None:
        headers["Authorization"] = authorization
    elif token is not None:
        headers["Authorization"] = f"Bearer {token}"
    response = case.service.dispatch(method, path, headers, body)
    status, response_headers, payload = response
    assert response_headers["Cache-Control"] == "no-store"
    assert response_headers["X-Request-ID"] == REQUEST_ID
    assert payload["request_id"] == REQUEST_ID
    record = case.records[-1]
    assert record["request_id"] == REQUEST_ID
    assert record["status"] == status
    assert record["method"] == method
    assert record["failure_classification"] == payload.get("error", {}).get("code")
    assert not case.agent.calls
    serialized = json.dumps([response_headers, payload, case.records])
    for credential in (TOKEN_A, TOKEN_A_ROTATED, TOKEN_B):
        assert credential not in serialized
    return response


def state(case):
    """Exact accounting rows; opening these test connections does not mutate."""
    with closing(sqlite3.connect(case.quota.path)) as db:
        return {
            "requests": db.execute(
                "SELECT tenant, window_start, count FROM request_windows ORDER BY tenant"
            ).fetchall(),
            "leases": db.execute("SELECT * FROM turn_leases ORDER BY lease_id").fetchall(),
            "tokens": db.execute("SELECT * FROM token_windows ORDER BY tenant").fetchall(),
            "sessions": db.execute(
                "SELECT * FROM session_usage ORDER BY tenant, session").fetchall(),
        }


def tenant_digest(tenant):
    return hashlib.sha256(tenant.encode()).hexdigest()


def only_request_count(case, count, *, window=120, tenant="tenant-a"):
    assert state(case) == {
        "requests": [(tenant_digest(tenant), window, count)],
        "leases": [], "tokens": [], "sessions": [],
    }


def assert_error(response, status, code, *, retry=None):
    actual, headers, payload = response
    assert actual == status
    assert set(payload) == {"request_id", "error"}
    assert set(payload["error"]) == {"code", "message"}
    assert payload["error"]["code"] == code
    if retry is None:
        assert "Retry-After" not in headers
    else:
        assert headers["Retry-After"] == str(retry)
    if status == 401:
        assert headers["WWW-Authenticate"] == 'Bearer realm="sigil-pi"'
    else:
        assert "WWW-Authenticate" not in headers


@pytest.mark.parametrize("row", ROUTES, ids=lambda row: f"{row['method']} {row['path']}")
def test_every_legacy_route_counts_authenticated_scope_denials_before_routing(tmp_path, row):
    case = oracle(tmp_path / "quota.sqlite")
    body = b"" if row["body"] is None else json.dumps(row["body"]).encode()
    for count in (1, 2):
        assert_error(request(case, row["method"], row["sample_path"], body=body),
                     403, "permission_denied")
        only_request_count(case, count)
        assert case.records[-1]["route"] == row["path"]
    before = state(case)
    assert_error(request(case, row["method"], row["sample_path"], body=body),
                 429, "rate_limited", retry=30)
    assert state(case) == before
    assert case.records[-1]["route"] == "unknown"
    assert case.records[-1]["principal"] == "principal-a"
    assert case.records[-1]["tenant_sha256"] == tenant_digest("tenant-a")
    assert len(case.records) == 3


@pytest.mark.parametrize("authorization,code", [
    (None, "authentication_required"),
    ("", "authentication_required"),
    ("Bearer", "authentication_required"),
    (f"bearer {TOKEN_A}", "authentication_required"),
    (f"Basic {TOKEN_A}", "authentication_required"),
    ("Bearer ", "invalid_credential"),
    ("Bearer unknown-inert-credential", "invalid_credential"),
    ("Bearer " + "x" * 4097, "invalid_credential"),
    ("Bearer " + "é" * 2049, "invalid_credential"),
])
def test_unauthenticated_requests_never_enter_durable_admission(
        tmp_path, monkeypatch, authorization, code):
    case = oracle(tmp_path / "quota.sqlite")

    def unexpected_admission(_tenant):
        pytest.fail("unauthenticated request reached durable admission")

    monkeypatch.setattr(case.quota, "admit_request", unexpected_admission)
    assert_error(request(case, token=None, authorization=authorization), 401, code)
    assert state(case) == {"requests": [], "leases": [], "tokens": [], "sessions": []}
    assert case.records[-1]["principal"] is None
    assert case.records[-1]["tenant_sha256"] is None
    assert case.records[-1]["route"] == "unknown"


@pytest.mark.parametrize("now", [99.999, 10000.0])
def test_inactive_credentials_do_not_consume_a_request(tmp_path, now):
    case = oracle(tmp_path / "quota.sqlite")
    case.clock.now = now
    assert_error(request(case), 401, "invalid_credential")
    assert state(case)["requests"] == []


@pytest.mark.parametrize("method,path,body,scopes,status,code", [
    ("GET", "/v1/health", b"", ["ops:read"], 200, None),
    ("GET", "/v1/version", b"", ["ops:read"], 200, None),
    ("GET", "/v1/ready", b"", ["ops:read"], 200, None),
    ("GET", "/not-a-product-route", b"", [], 404, "not_found"),
    ("PATCH", "/v1/health", b"", [], 404, "not_found"),
    ("POST", "/v1/chat", b"{", ["chat"], 400, "invalid_json"),
    ("POST", "/v1/chat", b"[]", ["chat"], 400, "invalid_request"),
    ("POST", "/v1/chat", b"{}", ["chat"], 400, "invalid_request"),
    ("GET", "/v1/schedules", b"", ["schedules:read"], 503, "schedules_unavailable"),
])
def test_admission_precedes_success_body_validation_route_and_dependency_outcomes(
        tmp_path, method, path, body, scopes, status, code):
    case = oracle(tmp_path / "quota.sqlite", scopes=scopes, limit=1)
    response = request(case, method, path, body=body)
    if code is None:
        assert response[0] == status
    else:
        assert_error(response, status, code)
    only_request_count(case, 1)
    assert_error(request(case, method, path, body=body), 429, "rate_limited", retry=30)
    only_request_count(case, 1)


def test_rotated_credentials_share_the_same_tenant_window_and_other_tenant_is_independent(
        tmp_path):
    case = oracle(tmp_path / "quota.sqlite", scopes=["ops:read"])
    assert request(case, token=TOKEN_A)[0] == 200
    assert request(case, token=TOKEN_A_ROTATED)[0] == 200
    assert_error(request(case, token=TOKEN_A), 429, "rate_limited", retry=30)
    assert request(case, token=TOKEN_B)[0] == 200
    assert state(case)["requests"] == sorted([
        (tenant_digest("tenant-a"), 120, 2), (tenant_digest("tenant-b"), 120, 1)])


def test_restart_preserves_admitted_counts_and_rejected_requests_do_not_add_to_them(tmp_path):
    path = tmp_path / "quota.sqlite"
    original = oracle(path, scopes=["ops:read"], limit=1)
    assert request(original)[0] == 200
    restarted = oracle(path, scopes=["ops:read"], limit=1, clock=original.clock)
    for _ in range(3):
        assert_error(request(restarted, token=TOKEN_A_ROTATED),
                     429, "rate_limited", retry=30)
    only_request_count(restarted, 1)
    assert not original.agent.calls and not restarted.agent.calls


@pytest.mark.parametrize("now,retry", [
    (120.0, 60), (120.1, 59), (150.0, 30), (150.1, 29),
    (178.999, 1), (179.0, 1), (179.999, 1),
])
def test_durable_window_retry_after_is_floored_and_at_least_one_second(tmp_path, now, retry):
    case = oracle(tmp_path / "quota.sqlite", scopes=["ops:read"], limit=1, clock=Clock(now))
    assert request(case)[0] == 200
    assert_error(request(case), 429, "rate_limited", retry=retry)
    only_request_count(case, 1)


def test_exact_next_minute_starts_a_new_durable_window(tmp_path):
    case = oracle(tmp_path / "quota.sqlite", scopes=["ops:read"], limit=1,
                  clock=Clock(179.999))
    assert request(case)[0] == 200
    assert_error(request(case), 429, "rate_limited", retry=1)
    case.clock.now = 180.0
    assert request(case)[0] == 200
    only_request_count(case, 1, window=180)
    assert_error(request(case), 429, "rate_limited", retry=60)


def test_legacy_backward_window_change_is_recorded_not_silently_corrected(tmp_path):
    """This records the oracle, not an endorsement of a final clock policy."""
    case = oracle(tmp_path / "quota.sqlite", scopes=["ops:read"], limit=1,
                  clock=Clock(180.0))
    assert request(case)[0] == 200
    case.clock.now = 179.0
    assert request(case)[0] == 200
    only_request_count(case, 1, window=120)
    case.clock.now = 180.0
    assert request(case)[0] == 200
    only_request_count(case, 1, window=180)


@pytest.mark.parametrize("failure", ["missing-table", "unavailable-connection"])
def test_durable_failure_has_a_scoped_bounded_readiness_fallback_only(
        tmp_path, monkeypatch, failure):
    case = oracle(tmp_path / "quota.sqlite", scopes=["ops:read"], limit=2)
    diagnostic = "private-storage-diagnostic-must-not-enter-api-or-log"
    if failure == "missing-table":
        with closing(sqlite3.connect(case.quota.path)) as db:
            db.execute("DROP TABLE request_windows")
            db.commit()
    else:
        def unavailable():
            raise OSError(diagnostic)
        monkeypatch.setattr(case.quota, "_connect", unavailable)

    # Other endpoints do not borrow emergency readiness capacity.
    assert_error(request(case), 500, "internal_error")
    assert_error(request(case, "POST", "/v1/ready"), 500, "internal_error")
    for _ in range(2):
        status, headers, payload = request(case, path="/v1/ready")
        assert status == 503
        assert payload == {
            "status": "not_ready", "dependencies": {"quota_store": False, "runtime": True},
            "draining": False, "request_id": REQUEST_ID,
        }
        assert "Retry-After" not in headers
    # The existing in-memory emergency limiter uses a rolling 60-second queue
    # and its own retry formula; it is NOT the durable epoch-window algorithm.
    assert_error(request(case, path="/v1/ready"), 429, "rate_limited", retry=61)
    assert request(case, path="/v1/ready", token=TOKEN_B)[0] == 503
    case.clock.now += 60
    assert request(case, path="/v1/ready")[0] == 503
    assert diagnostic not in json.dumps(case.records)


def test_durable_failure_does_not_make_readiness_public_or_unscoped(tmp_path):
    case = oracle(tmp_path / "quota.sqlite", scopes=[], limit=1)
    with closing(sqlite3.connect(case.quota.path)) as db:
        db.execute("DROP TABLE request_windows")
        db.commit()
    assert_error(request(case, path="/v1/ready", token=None),
                 401, "authentication_required")
    for _ in range(3):
        assert_error(request(case, path="/v1/ready"), 403, "permission_denied")
    assert dict(case.service._readiness_emergency_rate._events) == {}
