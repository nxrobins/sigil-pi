"""Prometheus export and the packaged SLO dashboard form one bounded contract."""

import hashlib
import json
import math
import re
import urllib.error
import urllib.request

from conftest import PI_ROOT

from product_service import (
    AuthRegistry,
    ProductService,
    PrometheusPayload,
    ServiceMetrics,
    render_prometheus_metrics,
    serve_product,
)


OPS_TOKEN = "prometheus-operator-token-with-test-only-entropy"
CHAT_TOKEN = "prometheus-chat-token-with-test-only-entropy"


class Agent:
    memory = None
    _mcp = object()

    def __init__(self):
        self.manifest = {}

    @staticmethod
    def turn_with_usage(session, message, *, allowed_tools=None,
                        deadline_monotonic=None):
        return "ok", {"input_tokens": 3, "output_tokens": 2}

    @staticmethod
    def turn_telemetry():
        return {"forge_queue_wait_ms": 4, "tool_calls": 0, "retries": 1}


def _entry(token, principal, tenant, scopes):
    return {
        "sha256": hashlib.sha256(token.encode()).hexdigest(),
        "principal": principal,
        "tenant": tenant,
        "scopes": scopes,
        "tools": [],
    }


def _service(**kwargs):
    auth = AuthRegistry([
        _entry(OPS_TOKEN, "operator", "operations", ["ops:read"]),
        _entry(CHAT_TOKEN, "customer", "secret-tenant", ["chat"]),
    ])
    return ProductService(Agent(), auth, log_sink=lambda record: None, **kwargs)


def _ops_headers():
    return {"Authorization": f"Bearer {OPS_TOKEN}"}


def _complete_snapshot(tmp_path):
    class Monitor:
        def __init__(self, snapshot):
            self._snapshot = snapshot

        @staticmethod
        def is_healthy():
            return True

        def snapshot(self):
            return dict(self._snapshot)

    audit = Monitor({
        "checks_total": 1, "failures_total": 0, "healthy": True,
        "last_check_unix": 100, "chains": 2, "records": 3,
    })
    retention = Monitor({
        "checks_total": 1, "failures_total": 0, "healthy": True,
        "last_check_unix": 100, "deleted_sessions_total": 0,
        "deleted_schedules_total": 0, "retention_seconds": 90,
    })
    service = _service(
        audit_monitor=audit, retention_monitor=retention,
        resource_root=tmp_path)
    service.dispatch(
        "POST", "/v1/chat", {"Authorization": f"Bearer {CHAT_TOKEN}"},
        json.dumps({"session": "private", "message": "secret"}).encode())
    return service.operational_metrics()


def test_histograms_are_cumulative_bounded_and_sanitize_invalid_samples():
    metrics = ServiceMetrics()
    for duration, queue in ((25, 5), (1000, 2000), (math.nan, -5)):
        metrics.turn_start()
        metrics.turn_finish(duration, queue_wait_ms=queue)
    snapshot = metrics.snapshot()
    durations = snapshot["turn_duration_ms"]
    queues = snapshot["queue_wait_ms"]
    assert durations["count"] == queues["count"] == 3
    assert durations["sum"] == 1025
    assert queues["sum"] == 2005
    assert [entry["count"] for entry in durations["buckets"]] == sorted(
        entry["count"] for entry in durations["buckets"])
    assert [entry["count"] for entry in queues["buckets"]] == sorted(
        entry["count"] for entry in queues["buckets"])
    assert durations["buckets"][0]["count"] == 1
    assert durations["buckets"][1]["count"] == 2
    assert durations["buckets"][-1]["count"] == 3
    assert queues["buckets"][0]["count"] == 2
    assert queues["buckets"][-1]["count"] == 3


def test_prometheus_route_is_authenticated_scoped_and_content_safe(tmp_path):
    service = _service(resource_root=tmp_path, version='1.0"\nrelease')
    unauthenticated = service.dispatch("GET", "/v1/metrics/prometheus", {})
    assert unauthenticated[0] == 401
    assert isinstance(unauthenticated[2], dict)
    forbidden = service.dispatch(
        "GET", "/v1/metrics/prometheus",
        {"Authorization": f"Bearer {CHAT_TOKEN}"})
    assert forbidden[0] == 403
    assert isinstance(forbidden[2], dict)

    status, _, payload = service.dispatch(
        "GET", "/v1/metrics/prometheus", _ops_headers())
    assert status == 200 and isinstance(payload, PrometheusPayload)
    assert '# TYPE sigil_pi_turn_duration_ms histogram' in payload.body
    assert 'version="1.0\\"\\nrelease"' in payload.body
    assert "secret-tenant" not in payload.body
    label_keys = set(re.findall(r'(\w+)="', payload.body))
    assert label_keys <= {
        "api_version", "version", "method", "route", "status", "class", "le",
        "dependency"}
    assert ('sigil_pi_dependency_component_ready{dependency="runtime"} 1'
            in payload.body)
    assert "sigil_pi_service_draining 0" in payload.body


def test_http_adapter_serves_prometheus_text_but_keeps_errors_json(tmp_path):
    server = serve_product(_service(resource_root=tmp_path), port=0)
    endpoint = f"http://127.0.0.1:{server.server_address[1]}/v1/metrics/prometheus"
    try:
        try:
            urllib.request.urlopen(endpoint, timeout=5)
            raise AssertionError("unauthenticated scrape unexpectedly succeeded")
        except urllib.error.HTTPError as error:
            assert error.code == 401
            assert error.headers.get_content_type() == "application/json"
            assert json.loads(error.read())["error"]["code"] == "authentication_required"

        request = urllib.request.Request(endpoint, headers={
            "Authorization": f"Bearer {OPS_TOKEN}"})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == (
                "text/plain; version=0.0.4; charset=utf-8")
            assert response.read().startswith(b"# HELP sigil_pi_info ")
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_queries_resolve_to_exported_low_cardinality_metrics(tmp_path):
    dashboard = json.loads(
        (PI_ROOT / "config" / "grafana-slo-dashboard.json").read_text())
    expressions = [target["expr"] for panel in dashboard["panels"]
                   for target in panel["targets"]]
    referenced = set(re.findall(r"sigil_pi_[a-z0-9_]+", "\n".join(expressions)))
    rendered = render_prometheus_metrics(_complete_snapshot(tmp_path), "1.0.0")
    emitted = set(re.findall(
        r"^(sigil_pi_[a-z0-9_]+)(?:\{| )", rendered, re.MULTILINE))
    assert referenced <= emitted

    titles = {panel["title"] for panel in dashboard["panels"]}
    assert titles == {
        "Dependency readiness", "Service 5xx ratio", "Successful turns",
        "Disk free ratio", "Lifecycle integrity", "Turn latency", "Queue wait",
        "Turn activity", "Resource consumption",
    }
    by_title = {panel["title"]: panel for panel in dashboard["panels"]}
    assert (by_title["Service 5xx ratio"]["fieldConfig"]["defaults"]
            ["thresholds"]["steps"][1]["value"]) == 0.005
    assert (by_title["Successful turns"]["fieldConfig"]["defaults"]
            ["thresholds"]["steps"][1]["value"]) == 0.995
    assert (by_title["Queue wait"]["fieldConfig"]["defaults"]
            ["thresholds"]["steps"][1]["value"]) == 2000
    assert any("histogram_quantile(0.99" in expression for expression in expressions)
    assert any("sigil_pi_queue_wait_ms_bucket" in expression for expression in expressions)
    serialized = json.dumps(dashboard).lower()
    assert all(value not in serialized for value in (
        "tenant_id", "principal_id", "session_id", "message", "reply"))


def test_runtime_generation_and_replacements_are_exported_without_labels():
    """After a replacement an operator needs to see THAT it happened; a host
    that silently swaps compilers is exactly as opaque as one that wedges.
    Label-free, because a per-tenant label here would be unbounded."""
    class Replacing:
        generation = 3
        unhealthy_replacements = 1
        is_healthy = True

    agent = Agent()
    agent._mcp = Replacing()
    service = _service()
    service.agent = agent
    metrics = service.operational_metrics()
    assert metrics["runtime_generation"] == 3
    assert metrics["runtime_unhealthy_replacements_total"] == 1

    body = render_prometheus_metrics(metrics, "1.0.0")
    assert "sigil_pi_runtime_generation 3" in body
    assert "sigil_pi_runtime_unhealthy_replacements_total 1" in body
    for line in body.splitlines():
        if line.startswith("sigil_pi_runtime_") and not line.startswith("#"):
            assert "{" not in line, f"runtime metrics must be label-free: {line}"


def test_a_raw_client_runtime_exports_no_generation_metrics():
    """The research host and every scripted double hold a plain client, which
    has no generations. The exporter must omit the series rather than invent a
    zero that an alert could read as a real measurement."""
    metrics = _service().operational_metrics()
    assert "runtime_generation" not in metrics
    body = render_prometheus_metrics(metrics, "1.0.0")
    assert "sigil_pi_runtime_generation" not in body
