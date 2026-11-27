"""Independent Python-product oracle and fixed wire fixtures; no production use."""

from pathlib import Path
from types import SimpleNamespace
import sqlite3

from conftest import SIGIL_ROOT
from product_service import ProductService
from scripts.compose_readiness_policy import compose_readiness_policy
from test_product_service import RecordingAgent, OPS_TOKEN, _auth, _headers
from turn_support import record

ROOT = Path(__file__).resolve().parent.parent
NAMES = ("audit_verification", "quota_store", "retention", "runtime", "schedule_store", "state_storage")
ATTRIBUTES = {"audit_verification": "audit_monitor", "quota_store": "quota_store",
              "retention": "retention_monitor", "schedule_store": "schedule_store",
              "state_storage": "data_manager"}


def source():
    return compose_readiness_policy(ROOT, SIGIL_ROOT)


def incoming(configured=63, healthy=63, *, draining=0, failed=0, label="request-1234"):
    return record("RQ1\n", [label, str(draining), str(failed), str(configured), str(healthy)])


def legacy(configured=63, healthy=63, *, draining=0, failed=0, label="request-1234", raises=None):
    """Call the real legacy dispatcher; only dependency observations are doubles.

    This deliberately does not reimplement readiness conjunction/response policy.
    It is neither real dependency-probe evidence nor emergency-limiter conformance.
    """
    assert type(configured) is int and 0 <= configured <= 63 and configured & 8
    assert type(healthy) is int and 0 <= healthy <= 63 and healthy & configured == healthy
    assert draining in (0, 1) and failed in (0, 1) and (not failed or configured & 2)
    records, probes, admissions = [], [], []

    def probe(name, good):
        def observed():
            probes.append(name)
            if name == raises:
                raise RuntimeError("private probe diagnostic canary")
            return good
        return observed

    kwargs = {"readiness": probe("runtime", bool(healthy & 8)), "log_sink": records.append}
    for index, name in enumerate(NAMES):
        if name == "runtime" or not configured & (1 << index):
            continue
        component = SimpleNamespace(is_healthy=probe(name, bool(healthy & (1 << index))))
        if name == "quota_store":
            def admit(tenant):
                admissions.append(tenant)
                if failed:
                    raise sqlite3.OperationalError("private admission diagnostic canary")
                return True, 0
            component.resource_limits_enabled = False
            component.requests_per_minute = 2
            component.admit_request = admit
        kwargs[ATTRIBUTES[name]] = component
    service = ProductService(RecordingAgent(), _auth(), **kwargs)
    if draining:
        service.start_draining()
    headers = {**_headers(OPS_TOKEN), "X-Request-ID": label}
    status, response_headers, body = service.dispatch("GET", "/v1/ready", headers)
    assert service.agent.calls == []
    assert response_headers["X-Request-ID"] == label
    assert response_headers["Cache-Control"] == "no-store"
    assert probes == [name for index, name in enumerate(NAMES) if configured & (1 << index)]
    assert admissions == (["ops"] if configured & 2 else [])
    assert records[-1]["route"] == "/v1/ready"
    return status, body, records
