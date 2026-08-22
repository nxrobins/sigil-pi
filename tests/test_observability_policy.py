"""Alert policy and runbooks remain complete and tied to emitted telemetry."""

import json
import re

from conftest import PI_ROOT


REQUIRED_ALERTS = {
    "service_not_ready",
    "audit_verification_failed",
    "retention_cleanup_failed",
    "disk_capacity_critical",
    "service_error_rate_slo",
}
METRIC_PATHS = {
    "dependency_ready",
    "audit_verification.healthy",
    "retention.healthy",
    "resources.disk_free_bytes",
    "resources.disk_total_bytes",
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
        ("retention_cleanup", "failed", "critical"),
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


def test_alert_and_runbook_contracts_contain_no_high_cardinality_customer_fields():
    rendered = json.dumps(_policy()).lower()
    forbidden = {"tenant_id", "principal_id", "session_id", "message", "reply", "token"}
    assert not any(field in rendered for field in forbidden)
    runbooks = (PI_ROOT / "docs" / "runbooks.md").read_text()
    assert "Following a document is not exercise evidence" in runbooks
    assert "Never bypass authentication" in runbooks
