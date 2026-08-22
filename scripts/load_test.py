#!/usr/bin/env python3
"""Bounded, tenant-isolating capacity test for the sigil-pi v1 API.

The default shape is the launch qualification: 50 concurrent clients for 60
minutes, split evenly between single-step and tool-using turns. Shorter runs
must opt into ``--smoke-test`` and are labelled ineligible in the report.
Credentials are read from a 0600 JSON file and are never copied to output.
"""

from __future__ import annotations

import argparse
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
            }
        self.seed_failures = 0

    def seed(self, success):
        with self._lock:
            if not success:
                self.seed_failures += 1

    def record(self, profile, result, marker, *, continuity=False):
        with self._lock:
            stats = self.profiles[profile]
            stats["requests"] += 1
            stats["statuses"][str(result["status"])] += 1
            stats["latency"].add(result["latency_ms"])
            if result["queue_ms"] is not None:
                stats["queue"].add(result["queue_ms"])
            stats["tool_calls"] += result["tool_calls"]
            stats["retries"] += result["retries"]
            if result["status"] == 200 and result["failure_class"] == "none":
                stats["successes"] += 1
            else:
                stats["failure_classes"][result["failure_class"]] += 1

            if profile == "tool_using" and result["tool_calls"] < 1:
                stats["tool_contract_violations"] += 1
            if profile == "single_step" and result["tool_calls"] != 0:
                stats["tool_contract_violations"] += 1

            reply = result.get("reply") or ""
            if any(other != marker and other in reply for other in self._markers):
                stats["isolation_violations"] += 1
            if continuity:
                stats["continuity_checks"] += 1
                if result["status"] != 200 or marker not in reply:
                    stats["continuity_failures"] += 1

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


def _request(origin, path, token, timeout, *, payload=None):
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
    started = time.monotonic()
    response_headers = {}
    body = b""
    status_code = 0
    failure_class = "transport_error"
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
            status_code = response.status
            response_headers = response.headers
            body = response.read()
    except urllib.error.HTTPError as error:
        status_code = error.code
        response_headers = error.headers
        body = error.read()
        failure_class = "http_error"
    except (OSError, TimeoutError, urllib.error.URLError):
        return {
            "status": 0,
            "failure_class": "transport_error",
            "latency_ms": (time.monotonic() - started) * 1000,
            "queue_ms": None,
            "tool_calls": 0,
            "retries": 0,
            "reply": "",
            "body": None,
        }
    latency = (time.monotonic() - started) * 1000
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        decoded = None
        failure_class = "invalid_response"
    if status_code == 200 and isinstance(decoded, dict):
        failure_class = "none"
    elif isinstance(decoded, dict):
        failure_class = str(decoded.get("error", {}).get("code") or failure_class)
    timing = response_headers.get("Server-Timing", "")
    match = QUEUE_RE.search(timing)
    try:
        tool_calls = max(0, int(response_headers.get("X-Sigil-Tool-Calls", "0")))
        retries = max(0, int(response_headers.get("X-Sigil-Retries", "0")))
    except ValueError:
        tool_calls = retries = 0
        failure_class = "invalid_telemetry"
    return {
        "status": status_code,
        "failure_class": failure_class,
        "latency_ms": latency,
        "queue_ms": float(match.group(1)) if match else None,
        "tool_calls": tool_calls,
        "retries": retries,
        "reply": decoded.get("reply", "") if isinstance(decoded, dict) else "",
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


def _worker(index, profile, token, marker, origin, args, stats, ready, start, shared):
    # Every tenant uses the same two external names. Isolation therefore
    # depends on credential-derived namespacing, never friendly client IDs.
    session = f"readiness-{profile.replace('_', '-')}"
    seed = _request(
        origin, "/v1/chat", token, args.request_timeout,
        payload={
            "session": session,
            "message": (
                f"Remember this readiness marker for later: {marker}. "
                "Reply with READY and do not use a tool."),
        })
    stats.seed(seed["status"] == 200 and seed["tool_calls"] == 0)
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
        result = _request(
            origin, "/v1/chat", token, args.request_timeout,
            payload={"session": session, "message": message})
        stats.record(profile, result, marker, continuity=continuity)
        call_number += 1


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


def evaluate_report(profiles, seed_failures, resource_samples, bounds):
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
                       "isolation_violations"):
            if profile[metric]:
                failures.append(f"{name} has {profile[metric]} {metric}")
        if profile["continuity_checks"] == 0:
            failures.append(f"{name} performed no continuity checks")
    resource_summary, resource_failures = _resource_evaluation(resource_samples, bounds)
    failures.extend(resource_failures)
    return {
        "request_error_rate": error_rate,
        "worst_profile_queue_p95_ms": worst_queue_p95,
        "resources": resource_summary,
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
    markers = ["SIGILLOAD_" + uuid.uuid4().hex for _ in range(args.concurrency)]
    stats = RunStatistics(markers)
    ready = threading.Condition()
    start = threading.Event()
    shared = {"ready": 0, "deadline": float("inf")}
    threads = []
    for index in range(args.concurrency):
        _, token = tenant_credentials[
            (index // args.max_concurrent_per_tenant) % len(tenant_credentials)]
        profile = PROFILES[index % len(PROFILES)]
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
    try:
        baseline = _metrics(origin, ops_token, args.request_timeout)
        resource_samples.append({"elapsed_seconds": 0.0,
                                 "resources": baseline["resources"]})
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
                    resource_samples.append({
                        "elapsed_seconds": round(time.monotonic() - run_started, 3),
                        "resources": metrics["resources"],
                    })
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
        resource_samples.append({
            "elapsed_seconds": round(time.monotonic() - run_started, 3),
            "resources": final_metrics["resources"],
        })
    except RuntimeError as error:
        metrics_failures.append(str(error))

    profiles, seed_failures = stats.report()
    evaluation = evaluate_report(profiles, seed_failures, resource_samples, bounds)
    if metrics_failures:
        evaluation["failures"].append(
            f"{len(metrics_failures)} authenticated metrics samples failed")
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
        "qualification_eligible": qualification_eligible,
        "qualification_prerequisite_notes": prerequisite_notes,
        "percentile_method": "nearest-rank, latencies rounded to 1 ms",
        "bounds": bounds,
        "profiles": profiles,
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
