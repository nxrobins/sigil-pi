"""The load harness reports what it measured (audit 2026-11-10, F-18).

Before that audit a load client that met a response its classifier did not
expect (an ``error`` field that is a string, a body cut short) ended without
recording anything: the report still said every client had been offered, and
the only volume floor was "more than zero requests". Nothing read the
compiler-replacement counter that docs/capacity.md criterion 8 names, and the
report did not say which provider, model or limits the service ran with.

These tests run the harness itself, for a second or two, against services on
loopback: a stand-in that speaks just enough of the v1 API and can be told to
misbehave, and the real ProductService on its real listener.
"""

import hashlib
import http.client
import io
import json
import math
import threading
import time
import urllib.error
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from hypothesis import given, settings, strategies as st

from product_service import AuthRegistry, ProductService
from scripts import load_test


OPS_TOKEN = "stand-in-ops-token-with-enough-entropy-for-a-test-fixture"
SINGLE = "readiness-single-step"
TOOL = "readiness-tool-using"
SETTINGS = {
    "model": "stand-in-model",
    "endpoint_host": "provider.stand-in.invalid",
    "turn_deadline_seconds": 120,
    "max_steps": 8,
    "max_tokens": 16000,
    "thinking": "adaptive",
    "requests_per_minute": 60,
    "max_concurrent_turns": 2,
    "token_reservation_per_turn": 1000000,
}
CONFIGURATION = {
    "provider": {"endpoint_host": "provider.stand-in.invalid"},
    "model": "stand-in-model",
    "limits": {key: value for key, value in SETTINGS.items()
               if key not in ("model", "endpoint_host")},
}
RESOURCES = {
    "memory_peak_bytes": 50_000_000, "thread_count": 6, "open_fd_count": 12,
    "compiler_process_count": 1, "compiler_resident_memory_bytes": 90_000_000,
    "compiler_thread_count": 35, "compiler_open_fd_count": 7,
    "state_storage_bytes": 4096, "audit_storage_bytes": 1024,
    "sandbox_storage_bytes": 0, "disk_free_bytes": 900, "disk_total_bytes": 1000,
}
REPLACEMENTS = "runtime_unhealthy_replacements_total"
# How long a stand-in turn takes. The harness opens a connection for every
# request, and a service that answers at once is sent thousands a second:
# enough, over this file's runs, to use up the host's ephemeral ports (16,384
# on macOS, each held for 30 seconds after it closes) for every other test.
TURN_SECONDS = 0.01


# ── a stand-in v1 service that can be told to misbehave ─────────────────────

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        return

    def send(self, status, body, headers=(), *, declared_length=None):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(
            len(body) if declared_length is None else declared_length))
        self.send_header("Connection", "close")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        self.server.credentials_received.append(self.headers.get("Authorization"))
        if self.server.redirect_metrics_to is not None:
            self.send(302, b"", [("Location", self.server.redirect_metrics_to + self.path)])
            return
        if (self.path != "/v1/metrics"
                or self.headers.get("Authorization") != f"Bearer {OPS_TOKEN}"):
            self.send(404, b'{"error":{"code":"not_found"}}')
            return
        self.send(200, json.dumps({"metrics": self.server.metrics_body()}).encode())

    def do_POST(self):  # noqa: N802
        self.server.credentials_received.append(self.headers.get("Authorization"))
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.answer(self, self.headers.get("Authorization", ""),
                           payload["session"], payload["message"])


class StandInService(ThreadingHTTPServer):
    """Chat turns that honour the harness's two profiles and its continuity
    markers, and a metrics endpoint.

    ``fault(session, n)`` names what to do instead on a session's n-th
    measured request (0 is its setup request). ``edit_metrics(n, body)``
    edits the n-th metrics body in place (1 is the baseline the harness reads
    before the run; the last is the one it reads after its clients stopped).
    """

    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.origin = f"http://127.0.0.1:{self.server_address[1]}"
        self.fault = lambda session, n: None
        self.edit_metrics = lambda n, body: None
        self.slow_seconds = 0.5
        self.redirect_metrics_to = None
        self.redirect_turns_to = None
        self.credentials_received = []
        self._lock = threading.Lock()
        self._markers = {}
        self._measured = Counter()
        self._metrics_requests = 0

    def metrics_body(self):
        with self._lock:
            self._metrics_requests += 1
            number = self._metrics_requests
        body = {"resources": dict(RESOURCES), "runtime_generation": 1,
                REPLACEMENTS: 0, "effective_settings": dict(SETTINGS)}
        self.edit_metrics(number, body)
        return body

    def answer(self, handler, token, session, message):
        key = (token, session)
        tools = 1 if "list_dir" in message else 0
        with self._lock:
            if message.startswith("Remember this readiness marker"):
                self._markers[key] = message.split(": ", 1)[1].split(".", 1)[0]
                number, reply, tools = 0, "READY", 0
            else:
                self._measured[key] += 1
                number = self._measured[key]
                reply = (self._markers[key] if "readiness marker" in message
                         else "TOOL_OK" if tools else "SINGLE_OK")
        telemetry = [("Server-Timing", "queue;dur=0.250"),
                     ("X-Sigil-Tool-Calls", str(tools)), ("X-Sigil-Retries", "0")]
        body = json.dumps({"reply": reply}).encode()
        fault = self.fault(session, number)
        time.sleep(TURN_SECONDS)
        if fault is None:
            handler.send(200, body, telemetry)
        elif fault == "string_error":
            handler.send(500, b'{"error":"internal"}', telemetry)
        elif fault == "incomplete_read":
            handler.send(200, body, telemetry, declared_length=len(body) + 64)
        elif fault == "reply_is_a_number":
            handler.send(200, b'{"reply":7}', telemetry)
        elif fault == "not_json":
            handler.send(200, b"<html>busy</html>", telemetry)
        elif fault == "no_tool_count":
            handler.send(200, body, [telemetry[0], telemetry[2]])
        elif fault == "no_retry_count":
            handler.send(200, body, telemetry[:2])
        elif fault == "redirect":
            handler.send(302, b"", [("Location", self.redirect_turns_to + "/v1/chat")])
        elif fault == "slow":
            time.sleep(self.slow_seconds)
            handler.send(200, body, telemetry)
        elif fault == "slow_then_refused":
            time.sleep(self.slow_seconds)
            handler.send(503, b'{"error":{"code":"not_ready"}}', telemetry)
        else:
            raise AssertionError(f"unknown fault {fault!r}")


def _serving():
    server = StandInService()
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(5)


service = pytest.fixture(name="service")(_serving)
elsewhere = pytest.fixture(name="elsewhere")(_serving)


def _credentials(tmp_path, tokens):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({
        "ops_token": OPS_TOKEN,
        "tenants": [{"tenant": f"load-tenant-{number}", "chat_token": token}
                    for number, token in enumerate(tokens)],
    }))
    path.chmod(0o600)
    return path


def _run(tmp_path, origin, *extra, clients=2, tokens=None):
    """One smoke run of the real harness. It never qualifies, so it exits 1."""
    tokens = tokens or [f"stand-in-chat-token-{number}"
                        for number in range(math.ceil(clients / 2))]
    output = tmp_path / "report.json"
    code = load_test.main([
        "--target", origin, "--credentials", str(_credentials(tmp_path, tokens)),
        "--output", str(output), "--forecast-peak", "1",
        "--concurrency", str(clients), "--duration", "1",
        "--request-timeout", "5", "--metrics-interval", "0.1",
        "--continuity-every", "3", "--smoke-test", *extra])
    assert code == 1
    return json.loads(output.read_text())


def _fault_on(session, number, fault):
    return lambda asked, n: fault if (asked, n) == (session, number) else None


# ── an unexpected response is a failed request, and the client carries on ───

def test_a_clean_short_run_passes_and_accounts_for_every_client(tmp_path, service):
    report = _run(tmp_path, service.origin, clients=4)
    evaluation = report["evaluation"]
    assert evaluation["failures"] == [] and evaluation["passed"] is True
    assert report["qualification_eligible"] is False
    clients = report["clients"]
    records = clients["records"]
    assert [record["client"] for record in records] == [0, 1, 2, 3]
    assert [record["profile"] for record in records] == [
        "single_step", "tool_using", "single_step", "tool_using"]
    for record in records:
        assert set(record) == {"client", "profile", "requests", "successes",
                               "alive_at_deadline", "stopped_by"}
        assert record["alive_at_deadline"] is True and record["stopped_by"] == "deadline"
        assert record["successes"] == record["requests"] >= clients["request_floor"] == 1
    for name in load_test.PROFILES:
        mine = [record for record in records if record["profile"] == name]
        assert sum(record["requests"] for record in mine) == report["profiles"][name]["requests"]
        assert sum(record["successes"] for record in mine) == report["profiles"][name]["successes"]
        assert report["profiles"][name]["incomplete_telemetry"] == 0
    assert evaluation["clients"] == {
        "offered": 4, "alive_at_deadline": 4,
        "fewest_successes": min(record["successes"] for record in records)}
    assert evaluation["compiler_replacements"] == {"first": 0, "last": 0, "rise": 0}
    assert report["request_timeout_seconds"] == 5


@pytest.mark.parametrize("fault,failure_class,status", [
    ("string_error", "http_error", "500"),
    ("incomplete_read", "transport_error", "0"),
    ("reply_is_a_number", "invalid_response", "200"),
])
def test_an_unexpected_response_is_a_failed_request_and_the_client_carries_on(
        tmp_path, service, fault, failure_class, status):
    service.fault = _fault_on(TOOL, 2, fault)
    report = _run(tmp_path, service.origin)
    tool = report["profiles"]["tool_using"]
    assert tool["failure_classes"] == {failure_class: 1}, (
        f"the service answered the tool client's second request with {fault} and "
        f"the harness recorded failure classes {tool['failure_classes']} over "
        f"{tool['requests']} requests: the client ended without recording a "
        f"failure, and the evaluation says passed={report['evaluation']['passed']} "
        f"(audit F-18)")
    assert tool["requests"] > 2 and tool["successes"] == tool["requests"] - 1
    statuses = Counter({"200": tool["successes"]})
    statuses[status] += 1
    assert tool["statuses"] == dict(statuses)
    client = report["clients"]["records"][1]
    assert client["alive_at_deadline"] is True
    assert (client["requests"], client["successes"]) == (
        tool["requests"], tool["successes"])


def test_a_setup_request_that_fails_strangely_is_a_setup_failure_not_a_hang(
        tmp_path, service):
    # Until the audit this client ended before it reported ready, and the
    # harness waited out its setup timeout and wrote no report at all.
    service.fault = _fault_on(TOOL, 0, "string_error")
    started = time.monotonic()
    report = _run(tmp_path, service.origin)
    assert time.monotonic() - started < 20
    assert report["seed_failures"] == 1
    assert "1 session setup requests failed" in report["evaluation"]["failures"]
    assert all(record["alive_at_deadline"] for record in report["clients"]["records"])


def test_a_client_that_ends_during_setup_does_not_hold_up_the_run(
        tmp_path, service, monkeypatch):
    request = load_test._request

    def ends_the_client(origin, path, token, timeout, *, payload=None):
        if payload is not None and payload["session"] == TOOL:
            raise SystemExit("the client's thread is told to exit")
        return request(origin, path, token, timeout, payload=payload)

    monkeypatch.setattr(load_test, "_request", ends_the_client)
    started = time.monotonic()
    report = _run(tmp_path, service.origin)
    assert time.monotonic() - started < 20
    assert report["clients"]["records"][1] == {
        "client": 1, "profile": "tool_using", "requests": 0, "successes": 0,
        "alive_at_deadline": False, "stopped_by": "SystemExit"}
    assert "1 of 2 load clients were not alive at the deadline: client 1 (SystemExit)" in (
        report["evaluation"]["failures"])
    assert report["evaluation"]["passed"] is False


def test_a_malformed_setup_reply_is_a_setup_failure(tmp_path, service):
    service.fault = _fault_on(SINGLE, 0, "not_json")
    report = _run(tmp_path, service.origin)
    assert report["seed_failures"] == 1, (
        "the setup request was answered 200 with a body that is not JSON and "
        "the harness counted the session as set up")


# ── every client is alive at the deadline, or the run fails ─────────────────

def test_a_client_that_stops_early_is_reported_and_fails_the_run(
        tmp_path, service, monkeypatch):
    record = load_test.RunStatistics.record
    seen = Counter()

    def defective(self, profile, result, marker, **kwargs):
        seen[profile] += 1
        if profile == "tool_using" and seen[profile] == 3:
            raise RuntimeError("a defect in the harness itself")
        return record(self, profile, result, marker, **kwargs)

    monkeypatch.setattr(load_test.RunStatistics, "record", defective)
    report = _run(tmp_path, service.origin)
    evaluation = report["evaluation"]
    assert evaluation["passed"] is False, (
        f"the tool client stopped after two requests "
        f"({report['profiles']['tool_using']['requests']} recorded) and the "
        f"evaluation passed: nothing checks that the clients stayed alive "
        f"(audit F-18)")
    assert evaluation["failures"] == [
        "1 of 2 load clients were not alive at the deadline: client 1 (RuntimeError)"]
    assert report["clients"]["records"][1] == {
        "client": 1, "profile": "tool_using", "requests": 2, "successes": 2,
        "alive_at_deadline": False, "stopped_by": "RuntimeError"}
    assert report["clients"]["records"][0]["alive_at_deadline"] is True
    assert evaluation["clients"]["alive_at_deadline"] == 1


@pytest.mark.parametrize("error", [
    AttributeError("'str' object has no attribute 'get'"),
    KeyError("reply"),
    RecursionError("maximum recursion depth exceeded"),
    http.client.IncompleteRead(b""),
    MemoryError(),
    SystemExit(3),
    KeyboardInterrupt(),
    GeneratorExit(),
], ids=lambda error: type(error).__name__)
def test_whatever_ends_a_request_the_run_does_not_pass_silently(
        tmp_path, service, monkeypatch, error):
    """The class guard: an exception the classifier lets through is either a
    recorded failed request from a client that carries on, or a client the
    report names as stopped. It is never nothing."""
    request = load_test._request
    calls = Counter()

    def unexpected(origin, path, token, timeout, *, payload=None):
        if payload is not None and payload["session"] == TOOL:
            calls[TOOL] += 1
            if calls[TOOL] == 3:        # its setup request was the first
                raise error
        return request(origin, path, token, timeout, payload=payload)

    monkeypatch.setattr(load_test, "_request", unexpected)
    report = _run(tmp_path, service.origin)
    tool = report["profiles"]["tool_using"]
    client = report["clients"]["records"][1]
    if isinstance(error, Exception):
        assert tool["failure_classes"] == {"client_error": 1}
        assert tool["statuses"]["0"] == 1 and tool["requests"] > 2
        assert client["alive_at_deadline"] is True
        # It is also disqualifying outright: the failed request has no telemetry.
        assert "tool_using has 1 incomplete_telemetry" in report["evaluation"]["failures"]
    else:
        assert client == {
            "client": 1, "profile": "tool_using", "requests": 1, "successes": 1,
            "alive_at_deadline": False, "stopped_by": type(error).__name__}
        assert report["evaluation"]["failures"] == [
            "1 of 2 load clients were not alive at the deadline: "
            f"client 1 ({type(error).__name__})"]
    assert report["evaluation"]["passed"] is False


# ── every client carries a floor of successful requests ─────────────────────

def test_a_client_that_was_never_served_fails_the_run(tmp_path, service):
    # One slow refusal for the whole run: it barely moves the error rate.
    service.slow_seconds = 1.2
    service.fault = lambda session, n: (
        "slow_then_refused" if session == TOOL and n >= 1 else None)
    report = _run(tmp_path, service.origin)
    evaluation = report["evaluation"]
    floor = [failure for failure in evaluation["failures"] if "successful" in failure]
    assert floor == [
        "1 of 2 load clients completed fewer than 1 successful requests: client 1 (0)"], (
        f"the tool client was offered for the whole run and served nothing "
        f"({report['profiles']['tool_using']}), and the evaluation's failures "
        f"are {evaluation['failures']} at an error rate of "
        f"{evaluation['request_error_rate']:.4f}: the only volume floor is "
        f"more than zero requests (audit F-18)")
    assert report["clients"]["records"][1]["alive_at_deadline"] is True
    assert evaluation["clients"]["fewest_successes"] == 0


def test_the_floor_can_be_raised_and_is_recorded(tmp_path, service):
    service.fault = lambda session, n: "slow" if session == TOOL and n >= 1 else None
    report = _run(tmp_path, service.origin, "--minimum-requests-per-client", "10")
    assert report["clients"]["request_floor"] == 10
    slow = report["clients"]["records"][1]
    assert 1 <= slow["successes"] < 10 and slow["alive_at_deadline"] is True
    assert report["evaluation"]["failures"] == [
        "1 of 2 load clients completed fewer than 10 successful requests: "
        f"client 1 ({slow['successes']})"]


@pytest.mark.parametrize("duration,floor", [
    (1, 1), (134, 1), (135, 1), (269, 1), (270, 2), (3600, 26), (7200, 53)])
def test_the_floor_is_one_successful_request_per_request_timeout(duration, floor):
    # 135 s is the harness's default request timeout: a client that was
    # served for the whole run cannot have completed fewer.
    assert load_test.CLIENT_FLOOR_PERIOD_SECONDS == 135
    assert load_test._parser().get_default("request_timeout") == 135
    assert load_test.client_request_floor(duration) == floor


def test_the_floor_cannot_be_lowered_below_what_the_duration_implies(tmp_path):
    with pytest.raises(SystemExit, match="cannot be below 26"):
        load_test.main([
            "--target", "http://127.0.0.1:9", "--credentials", "unused",
            "--output", str(tmp_path / "out"), "--minimum-requests-per-client", "25"])


def _clients(count=4, successes=30, **overrides):
    records = [{"client": index, "profile": load_test.client_profile(index),
                "requests": successes, "successes": successes,
                "alive_at_deadline": True, "stopped_by": "deadline"}
               for index in range(count)]
    for index, changes in overrides.items():
        records[int(index.lstrip("c"))].update(changes)
    return records


def _profiles():
    def profile(tools):
        return {
            "requests": 60, "successes": 60, "statuses": {"200": 60},
            "failure_classes": {},
            "latency": {"samples": 60, "p50_ms": 10, "p95_ms": 20, "p99_ms": 30},
            "queue_wait": {"samples": 60, "p50_ms": 1, "p95_ms": 2, "p99_ms": 3},
            "tool_calls_total": tools, "retries_total": 0,
            "tool_contract_violations": 0, "continuity_checks": 6,
            "continuity_failures": 0, "isolation_violations": 0,
            "incomplete_telemetry": 0,
        }
    return {"single_step": profile(0), "tool_using": profile(60)}


def _samples(*counters):
    return [{"elapsed_seconds": float(index), "resources": dict(RESOURCES),
             "runtime_generation": 1, REPLACEMENTS: counter}
            for index, counter in enumerate(counters)]


BOUNDS = {
    "memory_growth_bytes": 512, "thread_growth": 64, "fd_growth": 256,
    "state_growth_bytes": 1024, "audit_growth_bytes": 512,
    "minimum_disk_free_ratio": 0.2,
}


def _evaluate(clients=None, samples=None, floor=26):
    return load_test.evaluate_report(
        _profiles(), 0, _samples(0, 0, 0) if samples is None else samples, BOUNDS,
        clients=_clients() if clients is None else clients, request_floor=floor)


def test_the_evaluation_requires_the_floor_of_every_client():
    assert _evaluate()["failures"] == []
    at_the_floor = _evaluate(_clients(c2={"successes": 26}))
    assert at_the_floor["passed"] is True
    assert at_the_floor["clients"] == {
        "offered": 4, "alive_at_deadline": 4, "fewest_successes": 26}
    below = _evaluate(_clients(c2={"successes": 25}, c3={"successes": 0}))
    assert below["passed"] is False
    assert below["failures"] == [
        "2 of 4 load clients completed fewer than 26 successful requests: "
        "client 2 (25), client 3 (0)"]
    # Failed requests do not count towards it.
    failing = _evaluate(_clients(c1={"requests": 4000, "successes": 3}))
    assert failing["failures"] == [
        "1 of 4 load clients completed fewer than 26 successful requests: client 1 (3)"]


def test_the_evaluation_requires_every_client_alive_at_the_deadline():
    stopped = _evaluate(_clients(
        c0={"alive_at_deadline": False, "stopped_by": "TypeError"},
        c3={"alive_at_deadline": False, "stopped_by": None}))
    assert stopped["passed"] is False
    assert stopped["failures"] == [
        "2 of 4 load clients were not alive at the deadline: "
        "client 0 (TypeError), client 3 (did not finish)"]
    assert stopped["clients"]["alive_at_deadline"] == 2
    # Only the harness's own True counts.
    assert _evaluate(_clients(c1={"alive_at_deadline": 1}))["passed"] is False
    nobody = _evaluate([])
    assert nobody["failures"] == ["no load client was recorded"]
    assert nobody["clients"] == {
        "offered": 0, "alive_at_deadline": 0, "fewest_successes": None}


def test_a_client_is_not_alive_at_the_deadline_until_it_says_so():
    # The default is the failing one: a client that hangs, or whose last
    # word is lost, is never counted as having reached the deadline.
    stats = load_test.RunStatistics(["SIGILLOAD_a", "SIGILLOAD_b", "SIGILLOAD_c"])
    stats.finish("SIGILLOAD_b")
    stats.finish("SIGILLOAD_c", ValueError("stopped"))
    assert stats.clients() == [
        {"client": 0, "profile": "single_step", "requests": 0, "successes": 0,
         "alive_at_deadline": False, "stopped_by": None},
        {"client": 1, "profile": "tool_using", "requests": 0, "successes": 0,
         "alive_at_deadline": True, "stopped_by": "deadline"},
        {"client": 2, "profile": "single_step", "requests": 0, "successes": 0,
         "alive_at_deadline": False, "stopped_by": "ValueError"},
    ]
    with pytest.raises(KeyError):
        stats.record("single_step", {}, "SIGILLOAD_unknown")


def test_a_long_list_of_failing_clients_stays_readable():
    everyone = _evaluate(_clients(count=50, successes=0))
    assert everyone["failures"] == [
        "50 of 50 load clients completed fewer than 26 successful requests: "
        + ", ".join(f"client {index} (0)" for index in range(10)) + " and 40 more"]


# ── criterion 8: the compiler-replacement counter must not climb ────────────

def test_a_compiler_replacement_during_the_run_fails_it(tmp_path, service):
    def replaced_once(number, body):
        body[REPLACEMENTS] = 2 if number == 1 else 3

    service.edit_metrics = replaced_once
    report = _run(tmp_path, service.origin)
    evaluation = report["evaluation"]
    assert evaluation["passed"] is False, (
        "the service's runtime_unhealthy_replacements_total went from 2 to 3 "
        "during the run and the evaluation passed: docs/capacity.md criterion 8 "
        "is not enforced (audit F-18)")
    assert evaluation["failures"] == [
        "the compiler-replacement counter rose from 2 to 3 during the run"]
    assert evaluation["compiler_replacements"] == {"first": 2, "last": 3, "rise": 1}
    counters = [sample[REPLACEMENTS] for sample in report["resource_samples"]]
    assert counters[0] == 2 and set(counters[1:]) == {3} and len(counters) >= 3
    assert {sample["runtime_generation"] for sample in report["resource_samples"]} == {1}


def test_a_service_that_reports_no_replacement_counter_cannot_pass(tmp_path, service):
    service.edit_metrics = lambda number, body: body.pop(REPLACEMENTS)
    report = _run(tmp_path, service.origin)
    assert report["evaluation"]["failures"] == [_NOT_REPORTED]
    assert report["evaluation"]["compiler_replacements"] is None


_NOT_REPORTED = (
    "the compiler-replacement counter (runtime_unhealthy_replacements_total) "
    "was not reported in every metrics sample")
# A counter that goes down belongs to a new process: what it counted before
# the restart is lost, so it cannot show that nothing was replaced.
_WENT_BACKWARDS = (
    "the compiler-replacement counter went backwards during the run: the "
    "service restarted, so the counter does not cover it")


@pytest.mark.parametrize("counters,failures,summary", [
    ((0, 0, 0), [], {"first": 0, "last": 0, "rise": 0}),
    ((4, 4), [], {"first": 4, "last": 4, "rise": 0}),
    ((0, 0, 1), ["the compiler-replacement counter rose from 0 to 1 during the run"],
     {"first": 0, "last": 1, "rise": 1}),
    ((1, 3, 3, 4), ["the compiler-replacement counter rose from 1 to 4 during the run"],
     {"first": 1, "last": 4, "rise": 3}),
    ((2, 0, 0), [_WENT_BACKWARDS], {"first": 2, "last": 0, "rise": 0}),
    ((1, 2, 0, 1), ["the compiler-replacement counter rose by 2 during the run",
                    _WENT_BACKWARDS],
     {"first": 1, "last": 1, "rise": 2}),
    ((0, None, 0), [_NOT_REPORTED], None),
    ((0, True), [_NOT_REPORTED], None),
    ((0, -1), [_NOT_REPORTED], None),
    ((0, 1.0), [_NOT_REPORTED], None),
    ((0,), ["fewer than two resource samples were captured", _NOT_REPORTED], None),
])
def test_the_replacement_counter_is_read_in_every_sample(counters, failures, summary):
    evaluation = _evaluate(samples=_samples(*counters))
    assert evaluation["failures"] == failures
    assert evaluation["compiler_replacements"] == summary
    assert evaluation["passed"] is (not failures)


# ── the report says what the service ran with ───────────────────────────────

def test_the_report_records_the_provider_the_model_and_the_limits_in_force(
        tmp_path, service):
    report = _run(tmp_path, service.origin)
    assert report.get("service_configuration") == CONFIGURATION, (
        "the service reports its model, its provider's host and its limits in "
        "/v1/metrics and the load report records none of them (audit F-18)")
    assert report["evaluation"]["failures"] == []


def test_a_service_that_does_not_report_its_settings_cannot_pass(tmp_path, service):
    service.edit_metrics = lambda number, body: body.pop("effective_settings")
    report = _run(tmp_path, service.origin)
    assert report["service_configuration"] is None
    assert report["evaluation"]["failures"] == [
        "the service does not report its effective settings, so the provider, "
        "the model and the limits in force are not recorded"]


@pytest.mark.parametrize("settings_given", [
    {"model": "", "endpoint_host": "provider.invalid", "max_steps": 8},
    {"model": "m", "endpoint_host": None, "max_steps": 8},
    {"model": "m", "max_steps": 8},
    {"model": "m", "endpoint_host": "provider.invalid"},
    ["model", "endpoint_host"],
])
def test_settings_without_a_provider_a_model_and_limits_are_not_a_record(settings_given):
    assert load_test.service_configuration({"effective_settings": settings_given}) is None


def test_settings_that_change_during_the_run_fail_it(tmp_path, service):
    def shortened_later(number, body):
        if number > 1:
            body["effective_settings"]["turn_deadline_seconds"] = 30

    service.edit_metrics = shortened_later
    report = _run(tmp_path, service.origin)
    assert report["service_configuration"] == CONFIGURATION
    assert report["evaluation"]["failures"] == [
        "the service's effective settings changed during the run"]


# ── criterion 7: every turn carries its queue, tool and retry telemetry ─────

@pytest.mark.parametrize("fault", ["no_tool_count", "no_retry_count"])
def test_a_served_turn_without_tool_or_retry_telemetry_disqualifies_the_run(
        tmp_path, service, fault):
    service.fault = _fault_on(SINGLE, 2, fault)
    report = _run(tmp_path, service.origin)
    single = report["profiles"]["single_step"]
    assert single.get("incomplete_telemetry") == 1, (
        f"a single-step turn was answered without its {fault} header and the "
        f"harness read the missing figure as zero: failures "
        f"{report['evaluation']['failures']} (docs/capacity.md criterion 7)")
    assert single["failure_classes"] == {"invalid_telemetry": 1}
    # One failed request in a short run can also exceed the error rate; the
    # telemetry failure is there whatever the volume.
    assert [failure for failure in report["evaluation"]["failures"]
            if "error rate" not in failure] == ["single_step has 1 incomplete_telemetry"]
    assert report["clients"]["records"][0]["alive_at_deadline"] is True


# ── a redirect is an answer too, and the credential stays with the target ───

def test_a_redirect_is_a_failed_request_and_the_token_goes_nowhere_else(
        service, elsewhere):
    """The harness refuses a plaintext target that is not loopback. Until the
    audit it then followed any redirect that target sent, to any origin,
    carrying the bearer token, and counted the answer as the target's."""
    service.redirect_metrics_to = service.redirect_turns_to = elsewhere.origin
    service.fault = lambda session, n: "redirect"
    metrics = load_test._request(service.origin, "/v1/metrics", OPS_TOKEN, 5)
    turn = load_test._request(service.origin, "/v1/chat", "tenant-chat-token", 5,
                              payload={"session": SINGLE, "message": "hello"})
    assert elsewhere.credentials_received == [], (
        f"the target answered 302 and the harness sent "
        f"{len(elsewhere.credentials_received)} request(s), with the bearer token, "
        f"to the origin the redirect named")
    assert service.credentials_received == [
        f"Bearer {OPS_TOKEN}", "Bearer tenant-chat-token"]
    for result in (metrics, turn):
        assert result["status"] == 302 and result["failure_class"] == "invalid_response"


def test_a_metrics_endpoint_that_redirects_fails_the_run(tmp_path, service, elsewhere):
    service.redirect_metrics_to = elsewhere.origin
    report = _run(tmp_path, service.origin)
    assert elsewhere.credentials_received == []
    assert report["metrics_sample_failures"] == len(service.credentials_received) - sum(
        profile["requests"] for profile in report["profiles"].values()) - 2
    assert report["resource_samples"] == [] and report["service_configuration"] is None
    assert report["evaluation"]["passed"] is False


# ── the latency reported is the service's ───────────────────────────────────

def test_the_latency_of_a_request_does_not_include_building_a_tls_context(
        service, monkeypatch):
    """Until the audit every request built its own verifying TLS context
    inside the latency it reported. That is 5 ms for one client and, because
    the clients contend for it, 70 ms at the median for 50 (measured on the
    host where F-18 was fixed): added to every latency figure, and a ceiling
    of about 500 requests a second on what the harness could offer."""
    create = load_test.ssl.create_default_context
    built = []

    def slow_to_build(*args, **kwargs):
        built.append(threading.current_thread().name)
        time.sleep(1.0)
        return create(*args, **kwargs)

    monkeypatch.setattr(load_test.ssl, "create_default_context", slow_to_build)
    cache = getattr(load_test, "_opener", None)
    if cache is not None:
        cache.cache_clear()
    try:
        latencies = [
            load_test._request(service.origin, "/v1/metrics", OPS_TOKEN, 5)["latency_ms"]
            for _ in range(3)]
    finally:
        if cache is not None:
            cache.cache_clear()
    assert len(built) == 1 and max(latencies) < 900, (
        f"three requests built {len(built)} TLS contexts of 1,000 ms each and "
        f"reported latencies of {[round(latency) for latency in latencies]} ms")


def test_the_run_builds_its_tls_context_before_the_clients_start(
        tmp_path, service, monkeypatch):
    create = load_test.ssl.create_default_context
    built = []
    monkeypatch.setattr(
        load_test.ssl, "create_default_context",
        lambda *args, **kwargs: built.append(threading.current_thread().name)
        or create(*args, **kwargs))
    load_test._opener.cache_clear()
    try:
        report = _run(tmp_path, service.origin, clients=4)
    finally:
        load_test._opener.cache_clear()
    assert built == [threading.main_thread().name]
    assert report["evaluation"]["failures"] == []


# ── the classifier, for anything a server can send ──────────────────────────

class _Reply:
    def __init__(self, status, headers, body, read_error):
        self.status, self.headers = status, headers
        self._body, self._read_error = body, read_error

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        if self._read_error is not None:
            raise self._read_error
        return self._body


_JSON = st.recursive(
    st.none() | st.booleans() | st.integers(-3, 3) | st.text(max_size=6)
    | st.sampled_from(["turn_deadline_exceeded", "SIGILLOAD_other"]),
    lambda children: st.lists(children, max_size=3) | st.dictionaries(
        st.sampled_from(["error", "code", "reply", "message"]), children, max_size=4),
    max_leaves=8)
_BODIES = _JSON.map(lambda value: json.dumps(value).encode()) | st.binary(max_size=16)
_COUNTS = st.none() | st.sampled_from(["0", "1", "7", "-1", "", "many", "1.5", " 2 "])
_TIMINGS = st.none() | st.sampled_from(
    ["queue;dur=12.5", "queue;dur=0", "db;dur=3, queue;dur=4", "queue", "", "queue;dur=x"])
_READ_ERRORS = st.sampled_from([
    None, None, None, http.client.IncompleteRead(b"{"), ConnectionResetError(),
    TimeoutError(), http.client.RemoteDisconnected("gone"), OSError("reset")])
_STATUSES = st.sampled_from([200, 200, 200, 201, 204, 400, 401, 429, 500, 502, 504])


@settings(max_examples=400, deadline=None)
@given(status=_STATUSES, body=_BODIES, tools=_COUNTS, retries=_COUNTS,
       timing=_TIMINGS, read_error=_READ_ERRORS, turn=st.booleans())
def test_the_classifier_never_raises_and_never_calls_a_broken_reply_a_success(
        status, body, tools, retries, timing, read_error, turn):
    headers = {name: value for name, value in (
        ("X-Sigil-Tool-Calls", tools), ("X-Sigil-Retries", retries),
        ("Server-Timing", timing)) if value is not None}

    def open_request(request, **_):
        if status < 300:        # urllib raises for everything else
            return _Reply(status, headers, body, read_error)
        stream = io.BytesIO(body)
        if read_error is not None:
            stream.read = lambda *_: (_ for _ in ()).throw(read_error)
        raise urllib.error.HTTPError(request.full_url, status, "status", headers, stream)

    original = load_test._opener
    load_test._opener = lambda: SimpleNamespace(open=open_request)
    try:
        result = load_test._request(
            "https://service.example", "/v1/chat" if turn else "/v1/metrics", "token", 1,
            payload={"session": "s", "message": "m"} if turn else None)
    finally:
        load_test._opener = original

    assert isinstance(result["failure_class"], str) and result["failure_class"]
    assert isinstance(result["reply"], str)
    assert isinstance(result["latency_ms"], float) and result["latency_ms"] >= 0
    for name in ("tool_calls", "retries"):
        assert result[name] is None or (type(result[name]) is int and result[name] >= 0)
    assert result["queue_ms"] is None or result["queue_ms"] >= 0
    succeeded = result["status"] == 200 and result["failure_class"] == "none"
    assert succeeded or result["failure_class"] != "none"
    if succeeded:
        assert status == 200 and read_error is None
        assert isinstance(json.loads(body), dict)
    if succeeded and turn:
        assert isinstance(json.loads(body).get("reply"), str)
        assert result["reply"] == json.loads(body)["reply"]
        assert None not in (result["queue_ms"], result["tool_calls"], result["retries"])
    if turn:
        # Whatever came back, the statistics take it: one request, a success
        # only when it was one, and incomplete telemetry never goes unseen.
        stats = load_test.RunStatistics(["SIGILLOAD_mine", "SIGILLOAD_other"])
        stats.record("single_step", result, "SIGILLOAD_mine", continuity=True)
        profile = stats.report()[0]["single_step"]
        assert profile["requests"] == 1
        assert profile["successes"] == (1 if succeeded else 0)
        assert sum(profile["failure_classes"].values()) == (0 if succeeded else 1)
        complete = None not in (
            result["queue_ms"], result["tool_calls"], result["retries"])
        assert profile["incomplete_telemetry"] == (0 if complete else 1)
        assert stats.clients()[0]["requests"] == 1
        assert stats.clients()[0]["successes"] == (1 if succeeded else 0)


@pytest.mark.parametrize("body,failure_class", [
    (b'{"error":"internal"}', "http_error"),
    (b'{"error":["a","b"]}', "http_error"),
    (b'{"error":{"code":{"nested":1}}}', "http_error"),
    (b'{"error":{"code":"Not A Stable Code!"}}', "http_error"),
    (b'{"error":{"code":""}}', "http_error"),
    (b'{"error":{"code":"turn_deadline_exceeded"}}', "turn_deadline_exceeded"),
    (b'["not","an","object"]', "http_error"),
    (b"<html>bad gateway</html>", "invalid_response"),
])
def test_an_error_response_is_classified_by_its_stable_code_or_as_an_http_error(
        monkeypatch, body, failure_class):
    def refuse(request, **_):
        raise urllib.error.HTTPError(request.full_url, 502, "bad", {}, io.BytesIO(body))

    monkeypatch.setattr(load_test, "_opener", lambda: SimpleNamespace(open=refuse))
    result = load_test._request("https://service.example", "/v1/chat", "token", 1,
                                payload={"session": "s", "message": "m"})
    assert (result["status"], result["failure_class"]) == (502, failure_class)


# ── the same run against the real service ──────────────────────────────────

class _Compiler:
    """What ProductService reads from a replacing runtime endpoint."""
    is_healthy = True
    generation = 1
    unhealthy_replacements = 0


class _LoadAgent:
    """An agent double that honours the harness's prompts, keyed by the
    internal session the service derives from the caller's credential."""

    manifest = {"list_dir": {}}
    memory = None

    def __init__(self):
        self._mcp = _Compiler()
        self._markers = {}
        self._turn = threading.local()
        self.replace_after = None
        self.turns = 0
        self._lock = threading.Lock()

    def turn_with_usage(self, session, message, *, allowed_tools=None,
                        deadline_monotonic=None, **_):
        tools = 1 if "list_dir" in message else 0
        with self._lock:
            self.turns += 1
            if self.replace_after is not None and self.turns == self.replace_after:
                self._mcp.unhealthy_replacements += 1
                self._mcp.generation += 1
            if message.startswith("Remember this readiness marker"):
                self._markers[session] = message.split(": ", 1)[1].split(".", 1)[0]
                reply, tools = "READY", 0
            elif "readiness marker" in message:
                reply = self._markers[session]
            else:
                reply = "TOOL_OK" if tools else "SINGLE_OK"
        if tools:
            assert "list_dir" in (allowed_tools or ())
        time.sleep(TURN_SECONDS)
        self._turn.tool_calls = tools
        return reply, {"input_tokens": 2, "output_tokens": 1}

    def turn_telemetry(self):
        return {"forge_queue_wait_ms": 0.5, "tool_calls": self._turn.tool_calls,
                "retries": 0}


def _product(tmp_path, tokens):
    def entry(token, principal, tenant, scopes, tools=()):
        return {"sha256": hashlib.sha256(token.encode()).hexdigest(),
                "principal": principal, "tenant": tenant, "scopes": scopes,
                "tools": list(tools)}

    auth = AuthRegistry(
        [entry(OPS_TOKEN, "operator", "ops", ["ops:read"])]
        + [entry(token, f"loader-{number}", f"load-tenant-{number}", ["chat"], ["list_dir"])
           for number, token in enumerate(tokens)])
    state = tmp_path / "state"
    state.mkdir()
    agent = _LoadAgent()
    product = ProductService(
        agent, auth, requests_per_minute=1_000_000, max_concurrent_turns=2,
        resource_root=state, effective_settings=SETTINGS,
        log_sink=lambda record: None)
    return product, agent


def test_the_harness_reads_the_real_services_metrics(tmp_path, product_server):
    tokens = [f"real-service-chat-token-with-test-only-entropy-{number}"
              for number in range(2)]
    product, agent = _product(tmp_path, tokens)
    server = product_server(product)
    origin = f"http://127.0.0.1:{server.port}"
    report = _run(tmp_path, origin, "--minimum-disk-free-percent", "0",
                  clients=4, tokens=tokens)
    assert report["evaluation"]["failures"] == []
    assert report["service_configuration"] == CONFIGURATION
    assert report["evaluation"]["compiler_replacements"] == {
        "first": 0, "last": 0, "rise": 0}
    assert all(record["alive_at_deadline"] and record["successes"] >= 1
               for record in report["clients"]["records"])
    assert len(report["clients"]["records"]) == 4
    for sample in report["resource_samples"]:
        assert sample[REPLACEMENTS] == 0 and sample["runtime_generation"] == 1
        assert sample["resources"]["memory_peak_bytes"] > 0
    assert all(profile["incomplete_telemetry"] == 0
               for profile in report["profiles"].values())
    for token in [OPS_TOKEN, *tokens]:
        assert token not in json.dumps(report)
    assert "load-tenant" not in json.dumps(report)


def test_a_replacement_in_the_real_service_fails_the_run(tmp_path, product_server):
    tokens = ["real-service-chat-token-with-test-only-entropy-0"]
    product, agent = _product(tmp_path, tokens)
    agent.replace_after = 5
    server = product_server(product)
    report = _run(tmp_path, f"http://127.0.0.1:{server.port}",
                  "--minimum-disk-free-percent", "0", tokens=tokens)
    assert report["evaluation"]["passed"] is False, (
        "the real service's runtime_unhealthy_replacements_total rose during "
        "the run and the evaluation passed (audit F-18, criterion 8)")
    assert report["evaluation"]["failures"] == [
        "the compiler-replacement counter rose from 0 to 1 during the run"]
    assert report["resource_samples"][-1]["runtime_generation"] == 2


def test_a_tls_target_is_verified_and_served_through_the_runs_one_opener(
        tmp_path, product_server, tls_material, monkeypatch):
    """The qualifying run targets https. The harness verifies the service's
    certificate with the default trust store, so the throwaway certificate is
    refused until that store is replaced by one that holds it."""
    tokens = ["tls-service-chat-token-with-test-only-entropy-0"]
    product, _ = _product(tmp_path, tokens)
    server = product_server(product, tls=True)
    origin = f"https://127.0.0.1:{server.port}"
    create = load_test.ssl.create_default_context
    load_test._opener.cache_clear()
    try:
        untrusted = load_test._request(origin, "/v1/metrics", OPS_TOKEN, 5)
        monkeypatch.setattr(load_test.ssl, "create_default_context",
                            lambda: create(cafile=str(tls_material.cert)))
        load_test._opener.cache_clear()
        report = _run(tmp_path, origin, "--minimum-disk-free-percent", "0", tokens=tokens)
    finally:
        load_test._opener.cache_clear()
    assert (untrusted["status"], untrusted["failure_class"]) == (0, "transport_error")
    assert report["target"] == origin
    assert report["evaluation"]["failures"] == []
    assert report["service_configuration"] == CONFIGURATION
    assert all(record["alive_at_deadline"] and record["successes"] >= 1
               for record in report["clients"]["records"])
