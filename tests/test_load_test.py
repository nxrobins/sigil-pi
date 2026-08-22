"""Capacity harness correctness, safety, and fail-closed qualification tests."""

import io
import json
import stat
import time
import urllib.error
from types import SimpleNamespace

import pytest

from scripts import load_test


def _args(**overrides):
    values = {
        "forecast_peak": 25,
        "concurrency": 50,
        "duration": 3600,
        "max_concurrent_per_tenant": 2,
        "forecast_approved_by": "Product Owner",
        "artifact_digest": "a" * 64,
        "service_workers": 1,
        "smoke_test": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _profile(name, *, requests=100, successes=100, queue=10, violations=0):
    return {
        "requests": requests,
        "successes": successes,
        "statuses": {"200": successes},
        "failure_classes": {},
        "latency": {"samples": requests, "p50_ms": 10, "p95_ms": 20,
                    "p99_ms": 30},
        "queue_wait": {"samples": requests, "p50_ms": queue, "p95_ms": queue,
                       "p99_ms": queue},
        "tool_calls_total": requests if name == "tool_using" else 0,
        "retries_total": 0,
        "tool_contract_violations": violations,
        "continuity_checks": 10,
        "continuity_failures": 0,
        "isolation_violations": 0,
    }


def _resources(memory=100, threads=5, fds=8, state=10, audit=4, free=900, total=1000):
    return {
        "memory_peak_bytes": memory,
        "thread_count": threads,
        "open_fd_count": fds,
        "state_storage_bytes": state,
        "audit_storage_bytes": audit,
        "disk_free_bytes": free,
        "disk_total_bytes": total,
    }


BOUNDS = {
    "memory_growth_bytes": 512,
    "thread_growth": 64,
    "fd_growth": 256,
    "state_growth_bytes": 1024,
    "audit_growth_bytes": 512,
    "minimum_disk_free_ratio": 0.2,
}


def test_histogram_uses_nearest_rank_and_empty_is_explicit():
    histogram = load_test.MillisecondHistogram()
    assert histogram.summary() == {
        "samples": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None}
    for value in range(1, 101):
        histogram.add(value)
    assert histogram.percentile(50) == 50
    assert histogram.percentile(95) == 95
    assert histogram.percentile(99) == 99


def test_credentials_require_private_regular_file_and_valid_shape(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({
        "ops_token": "ops-secret",
        "tenants": [{"tenant": "a", "chat_token": "chat-secret"}],
    }))
    path.chmod(0o600)
    assert load_test.load_credentials(path) == (
        "ops-secret", [("a", "chat-secret")])

    path.chmod(0o640)
    with pytest.raises(load_test.LoadConfigurationError, match="group/other"):
        load_test.load_credentials(path)


@pytest.mark.parametrize("document,match", [
    ({"ops_token": "x"}, "exactly"),
    ({"ops_token": "", "tenants": []}, "ops_token"),
    ({"ops_token": "x", "tenants": []}, "non-empty list"),
    ({"ops_token": "x", "tenants": [{"tenant": "a"}]}, "exactly"),
    ({"ops_token": "x", "tenants": [
        {"tenant": "a", "chat_token": "x"},
        {"tenant": "a", "chat_token": "y"}]}, "invalid identity"),
])
def test_malformed_credentials_fail_closed(tmp_path, document, match):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    with pytest.raises(load_test.LoadConfigurationError, match=match):
        load_test.load_credentials(path)


def test_credentials_symlink_is_refused(tmp_path):
    target = tmp_path / "real"
    target.write_text("{}")
    target.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(load_test.LoadConfigurationError, match="non-symlink"):
        load_test.load_credentials(link)


def test_qualification_requires_full_duration_capacity_tenants_and_binding():
    assert load_test.validate_run(_args(), 25) == (True, [])
    with pytest.raises(load_test.LoadConfigurationError, match="prerequisites") as error:
        load_test.validate_run(_args(
            duration=3599, concurrency=49, forecast_approved_by=None,
            artifact_digest="bad", service_workers=2), 1)
    rendered = str(error.value)
    assert "3600" in rendered and "50" in rendered and "tenant" in rendered
    assert "approval" in rendered and "SHA-256" in rendered and "one measured" in rendered


def test_smoke_run_is_always_ineligible_and_explains_shortfall():
    eligible, notes = load_test.validate_run(
        _args(smoke_test=True, duration=1, concurrency=2,
              forecast_approved_by=None, artifact_digest=None), 1)
    assert eligible is False
    assert any("3600" in note for note in notes)


@pytest.mark.parametrize("target", [
    "ftp://example.com", "https://user@example.com", "https://example.com/path",
    "https://example.com?token=secret",
])
def test_target_rejects_unsafe_or_ambiguous_origins(target):
    with pytest.raises(load_test.LoadConfigurationError):
        load_test._safe_target(target)


def test_request_parses_success_telemetry_without_exposing_token(monkeypatch):
    class Response:
        status = 200
        headers = {
            "Server-Timing": "queue;dur=12.5",
            "X-Sigil-Tool-Calls": "1",
            "X-Sigil-Retries": "2",
        }

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"reply":"ok"}'

    observed = []

    def open_request(request, **kwargs):
        observed.append(request)
        return Response()

    monkeypatch.setattr(load_test.urllib.request, "urlopen", open_request)
    result = load_test._request(
        "https://service.example", "/v1/chat", "private-token", 1,
        payload={"session": "s", "message": "hello"})
    assert result["status"] == 200 and result["failure_class"] == "none"
    assert result["queue_ms"] == 12.5
    assert result["tool_calls"] == 1 and result["retries"] == 2
    assert observed[0].get_header("Authorization") == "Bearer private-token"
    assert "private-token" not in json.dumps(result)


def test_request_classifies_stable_http_and_transport_failures(monkeypatch):
    http_error = urllib.error.HTTPError(
        "https://service.example/v1/chat", 504, "timeout", {},
        io.BytesIO(b'{"error":{"code":"turn_deadline_exceeded"}}'))
    monkeypatch.setattr(
        load_test.urllib.request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(http_error))
    result = load_test._request("https://service.example", "/v1/chat", "token", 1)
    assert result["status"] == 504
    assert result["failure_class"] == "turn_deadline_exceeded"

    monkeypatch.setattr(
        load_test.urllib.request, "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(urllib.error.URLError("down")))
    result = load_test._request("https://service.example", "/v1/chat", "token", 1)
    assert result["status"] == 0 and result["failure_class"] == "transport_error"


def test_run_statistics_detects_tool_continuity_and_cross_marker_violations():
    own, other = "SIGILLOAD_own", "SIGILLOAD_other"
    stats = load_test.RunStatistics([own, other])
    stats.record("tool_using", {
        "status": 200, "failure_class": "none", "latency_ms": 1,
        "queue_ms": 0, "tool_calls": 0, "retries": 0,
        "reply": other,
    }, own, continuity=True)
    report, _ = stats.report()
    tool = report["tool_using"]
    assert tool["tool_contract_violations"] == 1
    assert tool["continuity_failures"] == 1
    assert tool["isolation_violations"] == 1


def test_run_statistics_counts_failed_seed_missing_queue_and_single_tool_violation():
    stats = load_test.RunStatistics(["mine"])
    stats.seed(False)
    stats.record("single_step", {
        "status": 503, "failure_class": "not_ready", "latency_ms": 2,
        "queue_ms": None, "tool_calls": 1, "retries": 1, "reply": "",
    }, "mine")
    report, seed_failures = stats.report()
    assert seed_failures == 1
    assert report["single_step"]["failure_classes"] == {"not_ready": 1}
    assert report["single_step"]["queue_wait"]["samples"] == 0
    assert report["single_step"]["tool_contract_violations"] == 1


def test_evaluation_passes_only_below_strict_thresholds_and_with_resources():
    profiles = {name: _profile(name) for name in load_test.PROFILES}
    samples = [
        {"resources": _resources()},
        {"resources": _resources(memory=110, threads=6, fds=9,
                                  state=20, audit=8, free=850)},
    ]
    evaluation = load_test.evaluate_report(profiles, 0, samples, BOUNDS)
    assert evaluation["passed"] is True
    assert evaluation["request_error_rate"] == 0

    profiles["single_step"] = _profile(
        "single_step", requests=200, successes=198, queue=2000, violations=1)
    failed = load_test.evaluate_report(profiles, 1, samples, BOUNDS)
    assert failed["passed"] is False
    assert any("error rate" in failure for failure in failed["failures"])
    assert any("queue p95" in failure for failure in failed["failures"])


def test_resource_bounds_fail_when_growth_or_disk_limit_is_exceeded():
    profiles = {name: _profile(name) for name in load_test.PROFILES}
    samples = [
        {"resources": _resources()},
        {"resources": _resources(memory=1000, threads=100, fds=300,
                                  state=2000, audit=1000, free=100)},
    ]
    result = load_test.evaluate_report(profiles, 0, samples, BOUNDS)
    assert result["passed"] is False
    assert len([f for f in result["failures"] if "exceeds" in f]) == 5
    assert any("disk free" in failure for failure in result["failures"])


def test_report_is_private_atomic_and_never_overwritten(tmp_path):
    path = tmp_path / "report.json"
    load_test._write_report(path, {"safe": True})
    assert json.loads(path.read_text()) == {"safe": True}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        load_test._write_report(path, {"safe": False})


def test_missing_invalid_json_and_empty_chat_token_credentials_fail(tmp_path):
    with pytest.raises(load_test.LoadConfigurationError, match="does not exist"):
        load_test.load_credentials(tmp_path / "missing")
    path = tmp_path / "credentials"
    path.write_text("{")
    path.chmod(0o600)
    with pytest.raises(load_test.LoadConfigurationError, match="valid JSON"):
        load_test.load_credentials(path)
    path.write_text(json.dumps({
        "ops_token": "ops", "tenants": [{"tenant": "a", "chat_token": ""}]}))
    with pytest.raises(load_test.LoadConfigurationError, match="invalid token"):
        load_test.load_credentials(path)


def test_request_detects_invalid_json_and_telemetry(monkeypatch):
    class Response:
        status = 200
        headers = {"X-Sigil-Tool-Calls": "not-an-integer"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b"not-json"

    monkeypatch.setattr(load_test.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    result = load_test._request("https://service.example", "/v1/chat", "token", 1)
    assert result["status"] == 200
    assert result["failure_class"] == "invalid_telemetry"
    assert result["body"] is None and result["queue_ms"] is None


def test_metrics_requires_successful_resource_shape(monkeypatch):
    monkeypatch.setattr(load_test, "_request", lambda *args, **kwargs: {
        "status": 503, "failure_class": "not_ready", "body": {}})
    with pytest.raises(RuntimeError, match="not_ready"):
        load_test._metrics("https://service.example", "token", 1)
    monkeypatch.setattr(load_test, "_request", lambda *args, **kwargs: {
        "status": 200, "failure_class": "none", "body": {"metrics": {}}})
    with pytest.raises(RuntimeError, match="resource snapshot"):
        load_test._metrics("https://service.example", "token", 1)


def test_resource_evaluation_requires_two_complete_samples():
    summary, failures = load_test._resource_evaluation(
        [{"resources": _resources()}], BOUNDS)
    assert summary == {} and "fewer than two" in failures[0]
    summary, failures = load_test._resource_evaluation(
        [{"resources": {}}, {"resources": {}}], BOUNDS)
    assert summary["memory_growth_bytes"] is None
    assert any("could not be measured" in failure for failure in failures)
    assert any("disk free ratio" in failure for failure in failures)


def test_evaluation_rejects_empty_profiles_and_missing_telemetry():
    profiles = {name: _profile(name, requests=0, successes=0) for name in load_test.PROFILES}
    for profile in profiles.values():
        profile["queue_wait"] = {
            "samples": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None}
        profile["continuity_checks"] = 0
    result = load_test.evaluate_report(
        profiles, 0,
        [{"resources": _resources()}, {"resources": _resources()}], BOUNDS)
    assert result["passed"] is False
    assert any("no measured requests" in failure for failure in result["failures"])
    assert sum("has no measured requests" in failure for failure in result["failures"]) == 2
    assert sum("no continuity checks" in failure for failure in result["failures"]) == 2


def test_main_smoke_exercises_concurrent_profiles_but_never_qualifies(
        tmp_path, monkeypatch, capsys):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({
        "ops_token": "ops-private",
        "tenants": [{"tenant": "tenant-private", "chat_token": "chat-private"}],
    }))
    credentials.chmod(0o600)
    output = tmp_path / "smoke.json"
    remembered = {}

    def fake_request(origin, path, token, timeout, *, payload=None):
        assert origin == "http://127.0.0.1:8080"
        assert path == "/v1/chat" and token == "chat-private" and timeout == 1
        session, message = payload["session"], payload["message"]
        if message.startswith("Remember this readiness marker"):
            marker = message.split(": ", 1)[1].split(".", 1)[0]
            remembered[session] = marker
            reply, tool_calls = "READY", 0
        else:
            tool_calls = 1 if session.endswith("tool-using") else 0
            reply = remembered[session] if "readiness marker" in message else "OK"
        time.sleep(0.001)
        return {
            "status": 200, "failure_class": "none", "latency_ms": 1,
            "queue_ms": 0, "tool_calls": tool_calls, "retries": 0,
            "reply": reply, "body": {"reply": reply},
        }

    metric_calls = []

    def fake_metrics(origin, token, timeout):
        metric_calls.append((origin, token, timeout))
        return {"resources": _resources()}

    monkeypatch.setattr(load_test, "_request", fake_request)
    monkeypatch.setattr(load_test, "_metrics", fake_metrics)
    result = load_test.main([
        "--target", "http://127.0.0.1:8080",
        "--credentials", str(credentials),
        "--output", str(output),
        "--forecast-peak", "1",
        "--concurrency", "2",
        "--duration", "1",
        "--request-timeout", "1",
        "--metrics-interval", "0.05",
        "--continuity-every", "1",
        "--smoke-test",
    ])
    assert result == 1
    report = json.loads(output.read_text())
    assert report["qualification_eligible"] is False
    assert report["evaluation"]["passed"] is True
    assert report["profiles"]["single_step"]["continuity_checks"] > 0
    assert report["profiles"]["tool_using"]["tool_calls_total"] > 0
    assert len(metric_calls) >= 2
    assert "chat-private" not in output.read_text()
    assert '"qualification_eligible": false' in capsys.readouterr().out


def test_main_rejects_nonpositive_and_nonloopback_plaintext(tmp_path):
    with pytest.raises(SystemExit, match="concurrency.*positive"):
        load_test.main([
            "--target", "http://127.0.0.1:8080", "--credentials", "unused",
            "--output", str(tmp_path / "out"), "--concurrency", "0",
        ])
    with pytest.raises(SystemExit, match="plaintext target must be loopback"):
        load_test.main([
            "--target", "http://example.com", "--credentials", "unused",
            "--output", str(tmp_path / "out"),
        ])
