"""Alert policy and runbooks remain complete and tied to emitted telemetry."""

import json
import re
from types import SimpleNamespace

from conftest import PI_ROOT


REQUIRED_ALERTS = {
    "service_not_ready",
    "audit_verification_failed",
    "audit_write_failed",
    "retention_cleanup_failed",
    "scheduler_not_running",
    "disk_capacity_critical",
    "service_error_rate_slo",
    "compiler_replaced_unhealthy",
}
METRIC_PATHS = {
    "dependency_ready",
    "audit_verification.healthy",
    "audit_verification.writes_healthy",
    "retention.healthy",
    "scheduler.healthy",
    "resources.disk_free_bytes",
    "resources.disk_total_bytes",
    "runtime_unhealthy_replacements_total",
}


def _policy():
    return json.loads((PI_ROOT / "config" / "alert-policy.json").read_text())


def test_alert_policy_has_exact_actionable_principal_failure_set():
    policy = _policy()
    assert policy["schema_version"] == 1
    alerts = {entry["id"]: entry for entry in policy["alerts"]}
    assert set(alerts) == REQUIRED_ALERTS
    assert all(entry["severity"] in {"critical", "warning"} for entry in alerts.values())
    assert all(entry["owner_role"] in {"operations", "security"}
               for entry in alerts.values())
    assert alerts["disk_capacity_critical"]["condition"]["value"] == 0.2
    assert alerts["service_error_rate_slo"]["condition"]["value"] == 0.005
    assert alerts["service_error_rate_slo"]["condition"]["minimum_requests"] == 100
    assert alerts["service_error_rate_slo"]["window_seconds"] == 300


def test_metric_paths_events_and_runbook_links_resolve_to_real_contracts():
    policy = _policy()
    paths = set()
    for alert in policy["alerts"]:
        condition = alert["condition"]
        paths.update(value for key, value in condition.items()
                     if key in {"path", "numerator_path", "denominator_path"})
    assert paths == METRIC_PATHS
    source = (PI_ROOT / "product_service.py").read_text()
    events = {(entry["event"], entry["status"], entry["severity"])
              for entry in policy["critical_events"]}
    assert events == {
        ("audit_verification", "failed", "critical"),
        ("audit_write", "failed", "critical"),
        ("retention_cleanup", "failed", "critical"),
        ("scheduler_tick", "failed", "critical"),
        ("state_reconciliation", "failed", "critical"),
    }
    assert all(event in source for event, _, _ in events)

    runbooks = (PI_ROOT / "docs" / "runbooks.md").read_text()
    headings = {
        re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-")
        for heading in re.findall(r"^## (.+)$", runbooks, re.M)
    }
    for entry in [*policy["alerts"], *policy["critical_events"]]:
        path, separator, anchor = entry["runbook"].partition("#")
        assert separator and path == "docs/runbooks.md"
        assert anchor in headings, f"runbook anchor does not exist: {entry['runbook']}"


def _wired_service(tmp_path):
    """A service holding every component product_main gives it, each the
    product's own class, so its metrics and readiness have the product's
    shape."""
    from agent import AuditLog, SessionStore
    from product_service import (
        AuditVerificationMonitor, AuthRegistry, DurableQuotaStore,
        ProductDataManager, ProductRetentionMonitor, ProductScheduler,
        ProductScheduleStore, ProductService, SessionOperationLocks)
    from runtime_client import RuntimePool

    # The compiler pool product_main builds, never entered, so no compiler
    # starts: its replacement counter is what the replacement alert reads.
    agent = SimpleNamespace(
        manifest={}, memory=None, _mcp=RuntimePool(lambda: None, size=1),
        store=SessionStore(tmp_path / "sessions"),
        sandbox_root=tmp_path / "sandboxes",
        audit=AuditLog(tmp_path / "audit", key=b"alert-policy-test-audit-key-0123"))
    auth = AuthRegistry([{
        "sha256": "0" * 64, "principal": "operator", "tenant": "operations",
        "scopes": ["ops:read"], "tools": []}])
    schedules = ProductScheduleStore(tmp_path / "product-schedules.json")
    locks = SessionOperationLocks(tmp_path / "locks")
    quota = DurableQuotaStore(tmp_path / "product-quotas.sqlite3")
    data = ProductDataManager(agent, schedules, locks, quota_store=quota)
    audit = AuditVerificationMonitor(
        lambda: {"ok": True, "chains": 0, "records": 0}, log_sink=lambda record: None)
    audit.check()
    retention = ProductRetentionMonitor(
        quota, data, schedules, locks, retention_s=86400,
        log_sink=lambda record: None, precheck=audit.check)
    retention.tick()
    scheduler = ProductScheduler(
        schedules, agent, auth, session_locks=locks, quota_store=quota,
        data_manager=data)
    return ProductService(
        agent, auth, schedule_store=schedules, session_locks=locks,
        data_manager=data, quota_store=quota, audit_monitor=audit,
        retention_monitor=retention, scheduler=scheduler, resource_root=tmp_path,
        log_sink=lambda record: None)


def test_every_alert_condition_reads_a_metric_the_product_emits(tmp_path):
    """METRIC_PATHS above is a list of names, and an alert on a path the
    metrics do not carry can never fire. Each path must resolve in the
    metrics of a fully wired service, to a value of the kind its condition
    compares."""
    metrics = json.loads(json.dumps(_wired_service(tmp_path).operational_metrics()))
    checked = 0
    for alert in _policy()["alerts"]:
        condition = alert["condition"]
        for key in ("path", "numerator_path", "denominator_path"):
            if key not in condition:
                continue
            value = metrics
            for part in condition[key].split("."):
                assert isinstance(value, dict) and part in value, (
                    f"alert {alert['id']} reads {condition[key]!r}, which the "
                    f"product's metrics do not carry")
                value = value[part]
            kind = bool if isinstance(condition.get("value"), bool) else (int, float)
            assert isinstance(value, kind) and (kind is bool or not isinstance(value, bool)), (
                f"alert {alert['id']} compares {condition[key]!r} = {value!r} "
                f"with {condition.get('value')!r}")
            checked += 1
    assert checked == len(METRIC_PATHS), "an alert path was not checked"


def test_the_not_ready_runbook_routes_every_readiness_component(tmp_path):
    """GET /v1/ready names a failed component, and the "Service not ready"
    runbook is where an operator takes that name. A component added to
    readiness without a route there fails here."""
    dependencies = _wired_service(tmp_path).readiness_snapshot()["dependencies"]
    assert set(dependencies) == {
        "runtime", "quota_store", "schedule_store", "state_storage",
        "audit_verification", "retention", "scheduler"}
    runbooks = (PI_ROOT / "docs" / "runbooks.md").read_text()
    section = runbooks.split("\n## Service not ready\n", 1)[1].split("\n## ", 1)[0]
    unrouted = [name for name in sorted(dependencies) if f"`{name}`" not in section]
    assert not unrouted, (
        f"the Service not ready runbook does not say what to do when these "
        f"components fail: {unrouted}")


def test_the_audit_write_runbook_says_what_a_torn_chain_needs():
    """A torn final record fails every later strict append to that session's
    chain until the chain is archived, and fails verification, so a
    replacement refuses to start (audit F-04). The runbook says so and sends
    it through the security procedure, never an edit in place."""
    runbooks = (PI_ROOT / "docs" / "runbooks.md").read_text()
    section = runbooks.split("\n## Audit write failure\n", 1)[1].split("\n## ", 1)[0]
    assert "torn" in section and "until the chain is archived" in section
    assert "Never truncate, edit, or delete the chain in place" in section
    assert "(#audit-verification-failure)" in section
    assert "until the process is replaced" in section


def test_alert_and_runbook_contracts_contain_no_high_cardinality_customer_fields():
    rendered = json.dumps(_policy()).lower()
    forbidden = {"tenant_id", "principal_id", "session_id", "message", "reply", "token"}
    assert not any(field in rendered for field in forbidden)
    runbooks = (PI_ROOT / "docs" / "runbooks.md").read_text()
    assert "Following a document is not exercise evidence" in runbooks
    assert "Never bypass authentication" in runbooks
