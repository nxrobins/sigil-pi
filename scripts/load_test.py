#!/usr/bin/env python3
"""Bounded, tenant-isolating capacity test for the sigil-pi v1 API.

The default shape is the launch qualification: 50 concurrent clients for 60
minutes, split evenly between single-step and tool-using turns. Shorter runs
must opt into ``--smoke-test`` and are labelled ineligible in the report.
Credentials are read from a 0600 JSON file and are never copied to output.

The report accounts for every client, not only for the totals (audit
2026-11-10, F-18). A response the classifier does not expect is a failed
request and the client carries on; a client that stops for any other reason
is named in the report, and the run cannot pass unless every client was
still running at the deadline and completed its floor of successful
requests. The compiler-replacement counter is read with every resource
sample and must not move, and the report records the provider, the model and
the limits the service says it ran with.
"""

from __future__ import annotations

import argparse
import functools
import http.client
import json
import math
import os
import re
import ssl
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


PROFILES = ("single_step", "tool_using")
QUEUE_RE = re.compile(r"(?:^|,)\s*queue;dur=([0-9]+(?:\.[0-9]+)?)")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# The service's error codes are short lowercase names. Anything else in that
# field is not a code, and is not allowed to become a failure class.
ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# docs/capacity.md criterion 8 names this counter, as /v1/metrics reports it.
REPLACEMENT_COUNTER = "runtime_unhealthy_replacements_total"
# Every client must complete one successful measured request for each this
# many seconds of the run (26 over the 60 minutes). It is the default request
# timeout, the 120-second turn deadline and a margin, so a client that was
# served for the whole run cannot have completed fewer.
CLIENT_FLOOR_PERIOD_SECONDS = 135


def client_profile(index):
    """Clients alternate between the two profiles, so that the two clients a
    tenant is allowed are one of each."""
    return PROFILES[index % len(PROFILES)]


def client_request_floor(duration_seconds):
    """Successful measured requests a run of this length requires of every
    client."""
    return max(1, int(duration_seconds // CLIENT_FLOOR_PERIOD_SECONDS))


class LoadConfigurationError(ValueError):
    pass


class MillisecondHistogram:
    """Exact-to-one-millisecond bounded-memory latency distribution."""

    def __init__(self):
        self.buckets = Counter()
        self.count = 0

    def add(self, milliseconds):
        value = max(0, int(round(float(milliseconds))))
        self.buckets[value] += 1
        self.count += 1

    def percentile(self, percentile):
        if not self.count:
            return None
        rank = max(1, math.ceil(self.count * float(percentile) / 100.0))
        seen = 0
        for value, count in sorted(self.buckets.items()):
            seen += count
            if seen >= rank:
                return value
        raise AssertionError("histogram rank was not found")

    def summary(self):
        return {
            "samples": self.count,
            "p50_ms": self.percentile(50),
            "p95_ms": self.percentile(95),
            "p99_ms": self.percentile(99),
        }


class RunStatistics:
    def __init__(self, markers):
        self._lock = threading.Lock()
        self._markers = frozenset(markers)
        self.profiles = {}
        for profile in PROFILES:
            self.profiles[profile] = {
                "requests": 0,
                "successes": 0,
                "statuses": Counter(),
                "failure_classes": Counter(),
                "latency": MillisecondHistogram(),
                "queue": MillisecondHistogram(),
                "tool_calls": 0,
                "retries": 0,
                "tool_contract_violations": 0,
                "continuity_checks": 0,
                "continuity_failures": 0,
                "isolation_violations": 0,
                "incomplete_telemetry": 0,
            }
        # One account for each load client, in client order. A client is
        # known by its marker, and is not alive at the deadline until it says
        # so itself (finish).
        self._clients = {
            marker: {"client": index, "profile": client_profile(index),
                     "requests": 0, "successes": 0,
                     "alive_at_deadline": False, "stopped_by": None}
            for index, marker in enumerate(markers)}
        self.seed_failures = 0

    def seed(self, success):
        with self._lock:
            if not success:
                self.seed_failures += 1

    def record(self, profile, result, marker, *, continuity=False):
        with self._lock:
            stats = self.profiles[profile]
            client = self._clients[marker]
            # A figure the response did not carry is None, never zero.
            tool_calls, retries = result["tool_calls"], result["retries"]
            stats["requests"] += 1
            client["requests"] += 1
            stats["statuses"][str(result["status"])] += 1
            stats["latency"].add(result["latency_ms"])
            if result["queue_ms"] is not None:
                stats["queue"].add(result["queue_ms"])
            if None in (result["queue_ms"], tool_calls, retries):
                stats["incomplete_telemetry"] += 1
            stats["tool_calls"] += tool_calls or 0
            stats["retries"] += retries or 0
            if result["status"] == 200 and result["failure_class"] == "none":
                stats["successes"] += 1
                client["successes"] += 1
            else:
                stats["failure_classes"][result["failure_class"]] += 1

            if profile == "tool_using" and (tool_calls or 0) < 1:
                stats["tool_contract_violations"] += 1
            if profile == "single_step" and (tool_calls or 0) != 0:
                stats["tool_contract_violations"] += 1

            reply = result.get("reply") or ""
            if any(other != marker and other in reply for other in self._markers):
                stats["isolation_violations"] += 1
            if continuity:
                stats["continuity_checks"] += 1
                if result["status"] != 200 or marker not in reply:
                    stats["continuity_failures"] += 1

    def finish(self, marker, error=None):
        """A client's last word: it saw the deadline arrive, or what stopped
        it before that."""
        with self._lock:
            client = self._clients[marker]
            client["alive_at_deadline"] = error is None
            client["stopped_by"] = "deadline" if error is None else type(error).__name__

    def clients(self):
        with self._lock:
            return [dict(client) for client in self._clients.values()]

    def report(self):
        with self._lock:
            output = {}
            for profile, stats in self.profiles.items():
                output[profile] = {
                    "requests": stats["requests"],
                    "successes": stats["successes"],
                    "statuses": dict(sorted(stats["statuses"].items())),
                    "failure_classes": dict(sorted(stats["failure_classes"].items())),
                    "latency": stats["latency"].summary(),
                    "queue_wait": stats["queue"].summary(),
                    "tool_calls_total": stats["tool_calls"],
                    "retries_total": stats["retries"],
                    "tool_contract_violations": stats["tool_contract_violations"],
                    "continuity_checks": stats["continuity_checks"],
                    "continuity_failures": stats["continuity_failures"],
                    "isolation_violations": stats["isolation_violations"],
                    "incomplete_telemetry": stats["incomplete_telemetry"],
                }
            return output, self.seed_failures


def _regular_private_file(path):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise LoadConfigurationError(f"credentials file does not exist: {path}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise LoadConfigurationError("credentials path must be a regular, non-symlink file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise LoadConfigurationError("credentials file must not grant group/other access")
    return path


def load_credentials(path):
    path = _regular_private_file(path)
    try:
        document = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise LoadConfigurationError("credentials file is not valid JSON") from error
    if not isinstance(document, dict) or set(document) != {"ops_token", "tenants"}:
        raise LoadConfigurationError("credentials must contain exactly ops_token and tenants")
    ops_token = document["ops_token"]
    tenants = document["tenants"]
    if not isinstance(ops_token, str) or not ops_token:
        raise LoadConfigurationError("ops_token must be a non-empty string")
    if not isinstance(tenants, list) or not tenants:
        raise LoadConfigurationError("tenants must be a non-empty list")
    parsed = []
    seen = set()
    for index, item in enumerate(tenants):
        if not isinstance(item, dict) or set(item) != {"tenant", "chat_token"}:
            raise LoadConfigurationError(
                f"tenant credential {index} must contain exactly tenant and chat_token")
        tenant, token = item["tenant"], item["chat_token"]
        if not isinstance(tenant, str) or not tenant or tenant in seen:
            raise LoadConfigurationError(f"tenant credential {index} has invalid identity")
        if not isinstance(token, str) or not token:
            raise LoadConfigurationError(f"tenant credential {index} has invalid token")
        seen.add(tenant)
        parsed.append((tenant, token))
    return ops_token, parsed


def _safe_target(target):
    parsed = urllib.parse.urlsplit(target)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LoadConfigurationError("target must be an http(s) origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LoadConfigurationError("target must not contain credentials, query, or fragment")
    path = parsed.path.rstrip("/")
    if path:
        raise LoadConfigurationError("target must be an origin without a path")
    port = f":{parsed.port}" if parsed.port is not None else ""
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme}://{host}{port}", parsed


def validate_run(args, tenant_count):
    problems = []
    required_concurrency = max(2 * args.forecast_peak, 50)
    required_tenants = math.ceil(args.concurrency / args.max_concurrent_per_tenant)
    if args.duration < 3600:
        problems.append("duration is below the required 3600 seconds")
    if args.concurrency < required_concurrency:
        problems.append(
            f"concurrency is below max(2x forecast, 50) = {required_concurrency}")
    if tenant_count < required_tenants:
        problems.append(
            f"{required_tenants} tenant credentials are required for the concurrency limit")
    if not args.forecast_approved_by:
        problems.append("forecast approval identity is missing")
    if not args.artifact_digest or not SHA256_RE.fullmatch(args.artifact_digest):
        problems.append("a lowercase SHA-256 artifact digest is required")
    if args.service_workers != 1:
        problems.append(
            "qualification currently supports one measured product worker; "
            "multi-worker metrics are not aggregate")
    if args.smoke_test:
        return False, problems
    if problems:
        raise LoadConfigurationError(
            "qualification prerequisites failed: " + "; ".join(problems))
    return True, []


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is the target's answer, and a failed request. Followed, it
    would send the bearer token to whatever origin it names, a plaintext one
    included, and report that origin's answer as the target's."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@functools.lru_cache(maxsize=None)
def _opener():
    """The run's one opener and its one verifying TLS context.

    A context built for every request cost each of them 5 ms, and 70 ms at
    the median once 50 clients contended for it, inside the latency the
    report gives as the service's.
    """
    return urllib.request.build_opener(
        _NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def _failed(failure_class, started):
    """A request that produced no response the harness can use."""
    return {
        "status": 0,
        "failure_class": failure_class,
        "latency_ms": (time.monotonic() - started) * 1000,
        "queue_ms": None,
        "tool_calls": None,
        "retries": None,
        "reply": "",
        "body": None,
    }


def _error_code(decoded):
    error = decoded.get("error") if isinstance(decoded, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and ERROR_CODE_RE.fullmatch(code) else None


def _request(origin, path, token, timeout, *, payload=None):
    """One request, classified. Whatever the service sends back is a result,
    never an exception: until audit 2026-11-10 (F-18) an ``error`` field that
    was not an object, or a body cut short, ended the calling client.

    A turn (a request with a payload) succeeds only when it is answered 200
    with a JSON object whose ``reply`` is a string and with its queue, tool
    and retry telemetry. A figure the response did not carry is None.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "X-Request-ID": "load-" + uuid.uuid4().hex,
    }
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = urllib.request.Request(origin + path, data=data, headers=headers, method=method)
    opener = _opener()
    started = time.monotonic()
    try:
        try:
            with opener.open(request, timeout=timeout) as response:
                status_code = response.status
                response_headers = response.headers
                body = response.read()
        except urllib.error.HTTPError as error:
            status_code = error.code
            response_headers = error.headers or {}
            body = error.read()
    except (OSError, http.client.HTTPException):
        # Refused, reset, timed out, or cut short in the status line, the
        # headers or the body (http.client's errors are not OSErrors).
        return _failed("transport_error", started)
    latency = (time.monotonic() - started) * 1000
    try:
        decoded = json.loads(body)
        readable = True
    except (ValueError, RecursionError):
        decoded = None
        readable = False
    reply = decoded.get("reply") if isinstance(decoded, dict) else None
    match = QUEUE_RE.search(response_headers.get("Server-Timing", ""))
    queue_ms = float(match.group(1)) if match else None
    counts = {}
    invalid_telemetry = False
    for name, header in (("tool_calls", "X-Sigil-Tool-Calls"),
                         ("retries", "X-Sigil-Retries")):
        value = response_headers.get(header)
        try:
            counts[name] = None if value is None else int(value)
        except ValueError:
            counts[name] = -1
        if counts[name] is not None and counts[name] < 0:
            counts[name] = None
            invalid_telemetry = True

    turn = payload is not None
    if not readable:
        failure_class = "invalid_response"
    elif status_code != 200:
        failure_class = _error_code(decoded) or "http_error"
    elif not isinstance(decoded, dict) or (turn and not isinstance(reply, str)):
        failure_class = "invalid_response"
    elif turn and None in (queue_ms, counts["tool_calls"], counts["retries"]):
        failure_class = "invalid_telemetry"
    else:
        failure_class = "none"
    if invalid_telemetry:
        failure_class = "invalid_telemetry"
    return {
        "status": status_code,
        "failure_class": failure_class,
        "latency_ms": latency,
        "queue_ms": queue_ms,
        "tool_calls": counts["tool_calls"],
        "retries": counts["retries"],
        "reply": reply if isinstance(reply, str) else "",
        "body": decoded,
    }


def _metrics(origin, token, timeout):
    result = _request(origin, "/v1/metrics", token, timeout)
    if result["status"] != 200 or not isinstance(result["body"], dict):
        raise RuntimeError(f"metrics request failed: {result['failure_class']}")
    metrics = result["body"].get("metrics")
    if not isinstance(metrics, dict) or not isinstance(metrics.get("resources"), dict):
        raise RuntimeError("metrics response has no resource snapshot")
    return metrics


def service_configuration(metrics):
    """The provider, the model and the limits the service says it runs with
    (``effective_settings`` in /v1/metrics), or None when it does not say."""
    settings = metrics.get("effective_settings") if isinstance(metrics, dict) else None
    if not isinstance(settings, dict):
        return None
    model, host = settings.get("model"), settings.get("endpoint_host")
    limits = {key: value for key, value in sorted(settings.items())
              if key not in ("model", "endpoint_host")}
    if not (isinstance(model, str) and model.strip()
            and isinstance(host, str) and host.strip() and limits):
        return None
    return {"provider": {"endpoint_host": host}, "model": model, "limits": limits}


def _sample(metrics, elapsed_seconds):
    return {
        "elapsed_seconds": elapsed_seconds,
        "resources": metrics["resources"],
        "runtime_generation": metrics.get("runtime_generation"),
        REPLACEMENT_COUNTER: metrics.get(REPLACEMENT_COUNTER),
    }


def _turn(origin, token, timeout, session, message):
    """One chat turn, classified. An exception from the classifier itself is
    a failed request too: it must not end the client."""
    started = time.monotonic()
    try:
        return _request(origin, "/v1/chat", token, timeout,
                        payload={"session": session, "message": message})
    except Exception:
        return _failed("client_error", started)


def _worker(index, profile, token, marker, origin, args, stats, ready, start, shared):
    # Every tenant uses the same two external names. Isolation therefore
    # depends on credential-derived namespacing, never friendly client IDs.
    session = f"readiness-{profile.replace('_', '-')}"
    try:
        try:
            seed = _turn(
                origin, token, args.request_timeout, session,
                f"Remember this readiness marker for later: {marker}. "
                "Reply with READY and do not use a tool.")
            stats.seed(seed["status"] == 200 and seed["failure_class"] == "none"
                       and seed["tool_calls"] == 0)
        finally:
            # Whatever happened to the setup request, the run must not wait
            # for this client until its setup timeout and then write nothing.
            with ready:
                shared["ready"] += 1
                ready.notify_all()
        start.wait()
        call_number = 0
        while time.monotonic() < shared["deadline"]:
            continuity = call_number % args.continuity_every == 0
            if profile == "single_step":
                if continuity:
                    message = (
                        "Without using any tool, reply with only the readiness marker "
                        "from this session's setup message.")
                else:
                    message = "Do not use a tool. Reply with exactly SINGLE_OK."
            else:
                if continuity:
                    message = (
                        "Use list_dir exactly once with path '.', then reply with the "
                        "readiness marker from this session's setup message.")
                else:
                    message = (
                        "Use list_dir exactly once with path '.', then reply with exactly TOOL_OK.")
            result = _turn(origin, token, args.request_timeout, session, message)
            stats.record(profile, result, marker, continuity=continuity)
            call_number += 1
    except BaseException as error:
        # Nothing may end a client unseen: the report names it, and the
        # evaluation then cannot pass.
        stats.finish(marker, error)
    else:
        stats.finish(marker)


def _resource_evaluation(samples, bounds):
    failures = []
    if len(samples) < 2:
        return {}, ["fewer than two resource samples were captured"]
    resources = [item["resources"] for item in samples]
    first = resources[0]

    def maximum(name):
        values = [item.get(name) for item in resources if isinstance(item.get(name), int)]
        return max(values) if values else None

    def minimum(name):
        values = [item.get(name) for item in resources if isinstance(item.get(name), int)]
        return min(values) if values else None

    summary = {}
    for name, bound_name in (
            ("memory_peak_bytes", "memory_growth_bytes"),
            ("thread_count", "thread_growth"),
            ("open_fd_count", "fd_growth"),
            ("state_storage_bytes", "state_growth_bytes"),
            ("audit_storage_bytes", "audit_growth_bytes")):
        peak = maximum(name)
        baseline = first.get(name)
        growth = None if peak is None or not isinstance(baseline, int) else max(0, peak - baseline)
        summary[bound_name] = growth
        limit = bounds[bound_name]
        if growth is None:
            failures.append(f"{bound_name} could not be measured")
        elif growth > limit:
            failures.append(f"{bound_name} {growth} exceeds {limit}")
    free = minimum("disk_free_bytes")
    total = maximum("disk_total_bytes")
    ratio = None if free is None or not total else free / total
    summary["minimum_disk_free_ratio"] = ratio
    if ratio is None:
        failures.append("disk free ratio could not be measured")
    elif ratio < bounds["minimum_disk_free_ratio"]:
        failures.append(
            f"minimum disk free ratio {ratio:.4f} is below "
            f"{bounds['minimum_disk_free_ratio']:.4f}")
    return summary, failures


def _replacement_evaluation(samples):
    """docs/capacity.md criterion 8: the compiler-replacement counter must
    not climb during the run. It is read with every resource sample."""
    values = [sample.get(REPLACEMENT_COUNTER) for sample in samples]
    if len(values) < 2 or not all(type(value) is int and value >= 0 for value in values):
        return None, [
            f"the compiler-replacement counter ({REPLACEMENT_COUNTER}) was not "
            "reported in every metrics sample"]
    steps = [later - earlier for earlier, later in zip(values, values[1:])]
    rise = sum(step for step in steps if step > 0)
    # A counter that goes down belongs to a new process. What it counted
    # before the restart is lost, so it cannot show that nothing was replaced.
    restarted = any(step < 0 for step in steps)
    failures = []
    if rise:
        failures.append(
            "the compiler-replacement counter rose "
            + (f"by {rise}" if restarted else f"from {values[0]} to {values[-1]}")
            + " during the run")
    if restarted:
        failures.append(
            "the compiler-replacement counter went backwards during the run: the "
            "service restarted, so the counter does not cover it")
    return {"first": values[0], "last": values[-1], "rise": rise}, failures


def _listed(clients, describe):
    shown = ", ".join(
        f"client {client['client']} ({describe(client)})" for client in clients[:10])
    return shown + (f" and {len(clients) - 10} more" if len(clients) > 10 else "")


def _client_evaluation(clients, request_floor):
    """Every offered client was still running at the deadline, by its own
    account, and completed its floor of successful requests."""
    stopped = [client for client in clients if client["alive_at_deadline"] is not True]
    starved = [client for client in clients if client["successes"] < request_floor]
    failures = []
    if not clients:
        failures.append("no load client was recorded")
    if stopped:
        failures.append(
            f"{len(stopped)} of {len(clients)} load clients were not alive at the "
            "deadline: "
            + _listed(stopped, lambda client: client["stopped_by"] or "did not finish"))
    if starved:
        failures.append(
            f"{len(starved)} of {len(clients)} load clients completed fewer than "
            f"{request_floor} successful requests: "
            + _listed(starved, lambda client: client["successes"]))
    return {
        "offered": len(clients),
        "alive_at_deadline": len(clients) - len(stopped),
        "fewest_successes": min((client["successes"] for client in clients), default=None),
    }, failures


def evaluate_report(profiles, seed_failures, resource_samples, bounds, *,
                    clients, request_floor):
    failures = []
    total_requests = sum(profile["requests"] for profile in profiles.values())
    total_successes = sum(profile["successes"] for profile in profiles.values())
    error_rate = 1.0 if not total_requests else 1.0 - total_successes / total_requests
    # Profile reports no longer contain their buckets, so the worst profile
    # p95 is the conservative qualification statistic.
    profile_queue_p95 = [profile["queue_wait"]["p95_ms"]
                         for profile in profiles.values()
                         if profile["queue_wait"]["p95_ms"] is not None]
    worst_queue_p95 = max(profile_queue_p95) if profile_queue_p95 else None
    if seed_failures:
        failures.append(f"{seed_failures} session setup requests failed")
    if total_requests == 0:
        failures.append("no measured requests completed")
    if error_rate >= 0.005:
        failures.append(f"request error rate {error_rate:.6f} is not below 0.005")
    if worst_queue_p95 is None or worst_queue_p95 >= 2000:
        failures.append("worst profile queue p95 is not below 2000 ms")
    for name, profile in profiles.items():
        if profile["requests"] == 0:
            failures.append(f"{name} has no measured requests")
        if profile["queue_wait"]["samples"] != profile["requests"]:
            failures.append(f"{name} is missing per-turn queue telemetry")
        for metric in ("tool_contract_violations", "continuity_failures",
                       "isolation_violations", "incomplete_telemetry"):
            if profile[metric]:
                failures.append(f"{name} has {profile[metric]} {metric}")
        if profile["continuity_checks"] == 0:
            failures.append(f"{name} performed no continuity checks")
    resource_summary, resource_failures = _resource_evaluation(resource_samples, bounds)
    failures.extend(resource_failures)
    replacement_summary, replacement_failures = _replacement_evaluation(resource_samples)
    failures.extend(replacement_failures)
    client_summary, client_failures = _client_evaluation(clients, request_floor)
    failures.extend(client_failures)
    return {
        "request_error_rate": error_rate,
        "worst_profile_queue_p95_ms": worst_queue_p95,
        "resources": resource_summary,
        "compiler_replacements": replacement_summary,
        "clients": client_summary,
        "passed": not failures,
        "failures": failures,
    }


def _write_report(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("report write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="v1 service origin")
    parser.add_argument("--credentials", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--artifact-digest")
    parser.add_argument("--version", default="unknown")
    parser.add_argument("--forecast-peak", type=int, default=25)
    parser.add_argument("--forecast-approved-by")
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--duration", type=int, default=3600)
    parser.add_argument("--max-concurrent-per-tenant", type=int, default=2)
    parser.add_argument("--service-workers", type=int, default=1)
    parser.add_argument("--request-timeout", type=float, default=135)
    parser.add_argument("--metrics-interval", type=float, default=5)
    parser.add_argument("--continuity-every", type=int, default=10)
    parser.add_argument(
        "--minimum-requests-per-client", type=int,
        help="raise the per-client floor of successful requests above one for "
             f"each {CLIENT_FLOOR_PERIOD_SECONDS} seconds of the run")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--memory-growth-mib", type=int, default=512)
    parser.add_argument("--thread-growth", type=int, default=64)
    parser.add_argument("--fd-growth", type=int, default=256)
    parser.add_argument("--state-growth-gib", type=int, default=10)
    parser.add_argument("--audit-growth-mib", type=int, default=512)
    parser.add_argument("--minimum-disk-free-percent", type=float, default=20)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    for name in ("forecast_peak", "concurrency", "duration",
                 "max_concurrent_per_tenant", "service_workers",
                 "continuity_every"):
        if getattr(args, name) <= 0:
            raise SystemExit(f"configuration error: --{name.replace('_', '-')} must be positive")
    if args.request_timeout <= 0 or args.metrics_interval <= 0:
        raise SystemExit("configuration error: timeouts and intervals must be positive")
    request_floor = client_request_floor(args.duration)
    if args.minimum_requests_per_client is not None:
        if args.minimum_requests_per_client < request_floor:
            raise SystemExit(
                "configuration error: --minimum-requests-per-client cannot be below "
                f"{request_floor} for a {args.duration}-second run")
        request_floor = args.minimum_requests_per_client
    origin, parsed_target = _safe_target(args.target)
    if parsed_target.scheme == "http" and parsed_target.hostname not in {
            "127.0.0.1", "::1", "localhost"}:
        raise SystemExit("configuration error: plaintext target must be loopback")
    try:
        ops_token, tenant_credentials = load_credentials(args.credentials)
        qualification_eligible, prerequisite_notes = validate_run(
            args, len(tenant_credentials))
    except LoadConfigurationError as error:
        raise SystemExit(f"configuration error: {error}") from error

    bounds = {
        "memory_growth_bytes": args.memory_growth_mib * 1024 * 1024,
        "thread_growth": args.thread_growth,
        "fd_growth": args.fd_growth,
        "state_growth_bytes": args.state_growth_gib * 1024 * 1024 * 1024,
        "audit_growth_bytes": args.audit_growth_mib * 1024 * 1024,
        "minimum_disk_free_ratio": args.minimum_disk_free_percent / 100.0,
    }
    _opener()   # built here, once, before any client can ask for it
    markers = ["SIGILLOAD_" + uuid.uuid4().hex for _ in range(args.concurrency)]
    stats = RunStatistics(markers)
    ready = threading.Condition()
    start = threading.Event()
    shared = {"ready": 0, "deadline": float("inf")}
    threads = []
    for index in range(args.concurrency):
        _, token = tenant_credentials[
            (index // args.max_concurrent_per_tenant) % len(tenant_credentials)]
        profile = client_profile(index)
        thread = threading.Thread(
            target=_worker,
            args=(index, profile, token, markers[index], origin, args, stats,
                  ready, start, shared),
            name=f"load-{index}", daemon=True)
        threads.append(thread)
        thread.start()

    with ready:
        seed_deadline = time.monotonic() + args.request_timeout + 30
        while shared["ready"] < len(threads):
            remaining = seed_deadline - time.monotonic()
            if remaining <= 0:
                raise SystemExit("load setup timed out before all clients became ready")
            ready.wait(remaining)

    resource_samples = []
    metrics_failures = []
    configuration = final_metrics = None
    try:
        baseline = _metrics(origin, ops_token, args.request_timeout)
        resource_samples.append(_sample(baseline, 0.0))
        configuration = service_configuration(baseline)
    except RuntimeError as error:
        metrics_failures.append(str(error))
    run_started = time.monotonic()
    shared["deadline"] = run_started + args.duration
    start.set()
    next_sample = run_started + args.metrics_interval
    interrupted = False
    try:
        while time.monotonic() < shared["deadline"]:
            wait = min(0.25, max(0.0, next_sample - time.monotonic()))
            time.sleep(wait)
            if time.monotonic() >= next_sample:
                try:
                    metrics = _metrics(origin, ops_token, args.request_timeout)
                    resource_samples.append(
                        _sample(metrics, round(time.monotonic() - run_started, 3)))
                except RuntimeError as error:
                    metrics_failures.append(str(error))
                next_sample += args.metrics_interval
    except KeyboardInterrupt:
        interrupted = True
        shared["deadline"] = time.monotonic()

    for thread in threads:
        thread.join(args.request_timeout + 5)
    live_threads = sum(thread.is_alive() for thread in threads)
    try:
        final_metrics = _metrics(origin, ops_token, args.request_timeout)
        resource_samples.append(
            _sample(final_metrics, round(time.monotonic() - run_started, 3)))
    except RuntimeError as error:
        metrics_failures.append(str(error))

    profiles, seed_failures = stats.report()
    clients = stats.clients()
    evaluation = evaluate_report(
        profiles, seed_failures, resource_samples, bounds,
        clients=clients, request_floor=request_floor)
    if metrics_failures:
        evaluation["failures"].append(
            f"{len(metrics_failures)} authenticated metrics samples failed")
    if configuration is None:
        evaluation["failures"].append(
            "the service does not report its effective settings, so the provider, "
            "the model and the limits in force are not recorded")
    elif final_metrics is not None and service_configuration(final_metrics) != configuration:
        evaluation["failures"].append(
            "the service's effective settings changed during the run")
    if live_threads:
        evaluation["failures"].append(f"{live_threads} load clients did not terminate")
    if interrupted:
        evaluation["failures"].append("run was interrupted")
    evaluation["passed"] = not evaluation["failures"]
    finished = datetime.now(timezone.utc)
    actual_duration = max(0.0, time.monotonic() - run_started)
    report = {
        "schema_version": 1,
        "test": "sigil-pi-v1-launch-capacity",
        "generated_at": finished.isoformat(),
        "artifact": {"version": args.version, "sha256": args.artifact_digest},
        "target": origin,
        "topology": {
            "service_workers": args.service_workers,
            "offered_concurrency": args.concurrency,
            "tenant_credentials": len(tenant_credentials),
            "max_concurrent_per_tenant": args.max_concurrent_per_tenant,
        },
        "forecast": {
            "peak_concurrent_turns": args.forecast_peak,
            "approved_by": args.forecast_approved_by,
        },
        "planned_duration_seconds": args.duration,
        "actual_wall_seconds": round(actual_duration, 3),
        "request_timeout_seconds": args.request_timeout,
        "qualification_eligible": qualification_eligible,
        "qualification_prerequisite_notes": prerequisite_notes,
        "percentile_method": "nearest-rank, latencies rounded to 1 ms",
        "bounds": bounds,
        "profiles": profiles,
        "clients": {"request_floor": request_floor, "records": clients},
        "service_configuration": configuration,
        "seed_failures": seed_failures,
        "resource_samples": resource_samples,
        "metrics_sample_failures": len(metrics_failures),
        "evaluation": evaluation,
    }
    try:
        _write_report(args.output, report)
    except FileExistsError as error:
        raise SystemExit(f"refusing to overwrite report: {args.output}") from error
    print(json.dumps({
        "report": str(args.output),
        "qualification_eligible": qualification_eligible,
        "passed": evaluation["passed"],
        "failures": evaluation["failures"],
    }, sort_keys=True))
    return 0 if qualification_eligible and evaluation["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
