"""Verify the recorded engineering benchmark while preserving a failing capacity verdict."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.load_test import client_request_floor, evaluate_report
from inkling import MODEL, MODEL_DIGEST

ARCHIVE = "136dc95ac105f76ffb46a67b27a83ef8029a965140e303c6dd8a5d97f8dbb5eb"
HARNESS = "bfbc5adc11c2744350079c0115e5be21bad2aa396613d77e01b6d0037d54f0a9"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify(folder, preflight):
    raw = folder / "load-test.json"
    report = json.loads(raw.read_bytes())
    context = json.loads((folder / "run-context.json").read_bytes())
    require(context["report_sha256"] == sha256(raw.read_bytes()).hexdigest(), "raw report digest mismatch")
    require(
        context["artifact_sha256"] == ARCHIVE
        and report["artifact"] == {"version": "0.5.0", "sha256": ARCHIVE}
        and context["release_tag"] == "v0.5.0",
        "release identity mismatch",
    )
    inputs = [
        "scripts/load_test.py",
        "scripts/failure_drill.py",
        "scripts/release_drill.py",
        "scripts/build_release.py",
        "qualification/load-v0.5.0/run.py",
        "qualification/load-v0.5.0/inkling.py",
        "qualification/load-v0.5.0/verify.py",
    ]
    sources = {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in inputs}
    require(
        context["source_sha256"] == sources and sources["scripts/load_test.py"] == HARNESS, "source mismatch"
    )
    require(context["run_error"] is None, "measurement runner failed")
    require(
        context["preflight"] is preflight and context["architecture"] in ("x86_64", "amd64"),
        "unexpected run type",
    )
    provider = context["provider_before"]
    require(
        provider == context.get("provider_after")
        and provider["model_digest"] == MODEL_DIGEST
        and provider["model"] == MODEL + ":latest",
        "provider identity mismatch",
    )
    require(
        context["service_setting_overrides"] == {"PI_MODEL": MODEL} and context["product_workers"] == 1,
        "unexpected service overrides",
    )
    configuration = report["service_configuration"]
    require(
        configuration["model"] == MODEL and configuration["provider"] == {"endpoint_host": "127.0.0.1"},
        "service model or endpoint mismatch",
    )
    shipped_limits = {
        "compiler_pool_size": 4,
        "requests_per_minute": 60,
        "max_concurrent_turns": 2,
        "llm_retries": 2,
        "model_http_timeout_ms": 60_000,
        "turn_deadline_seconds": 120,
        "max_tokens": 16_000,
        "turn_lease_seconds": 300,
    }
    require(
        all(configuration["limits"].get(key) == value for key, value in shipped_limits.items()),
        "service limits differ from v0.5.0",
    )
    concurrency, duration = (2, 75) if preflight else (50, 3600)
    require(
        report["topology"]
        == {
            "service_workers": 1,
            "offered_concurrency": concurrency,
            "tenant_credentials": 25,
            "max_concurrent_per_tenant": 2,
        },
        "unexpected topology",
    )
    require(
        report["planned_duration_seconds"] == duration and report["actual_wall_seconds"] >= duration,
        "incomplete measurement duration",
    )
    wall = context["harness_elapsed_monotonic_seconds"]
    require(wall + 1 >= report["actual_wall_seconds"], "inconsistent measurement clocks")
    require(
        report["forecast"] == {"peak_concurrent_turns": 25, "approved_by": None}
        and report["qualification_eligible"] is False
        and context["harness_exit"] == 1,
        "unexpected launch qualification claim",
    )
    notes = report["qualification_prerequisite_notes"]
    require(
        "forecast approval identity is missing" in notes and (preflight or len(notes) == 1),
        "unexpected prerequisites",
    )
    expected_bounds = {
        "memory_growth_bytes": 512 * 1024**2,
        "thread_growth": 64,
        "fd_growth": 256,
        "state_growth_bytes": 10 * 1024**3,
        "audit_growth_bytes": 512 * 1024**2,
        "minimum_disk_free_ratio": 0.2,
    }
    require(report["bounds"] == expected_bounds, "unexpected performance bounds")
    clients = report["clients"]["records"]
    floor = client_request_floor(duration)
    require(
        report["clients"]["request_floor"] == floor
        and sorted(c["client"] for c in clients) == list(range(concurrency)),
        "client accounts or floor missing",
    )
    profiles = report["profiles"]
    require(set(profiles) == {"single_step", "tool_using"}, "unexpected profiles")
    for name, profile in profiles.items():
        members = [c for c in clients if c["profile"] == name]
        count = sum(c["requests"] for c in members)
        success = sum(c["successes"] for c in members)
        require(
            len(members) == concurrency // 2
            and count == profile["requests"] == sum(profile["statuses"].values())
            and success == profile["successes"]
            and count - success == sum(profile["failure_classes"].values()),
            f"inconsistent {name} client and request accounting",
        )
        require(
            profile["latency"]["samples"] == count and profile["queue_wait"]["samples"] <= count,
            f"inconsistent {name} latency accounting",
        )
    samples = report["resource_samples"]
    require(
        len(samples) >= 2
        and all(a["elapsed_seconds"] <= b["elapsed_seconds"] for a, b in zip(samples, samples[1:])),
        "missing or nonmonotonic resource samples",
    )
    result = evaluate_report(
        profiles, report["seed_failures"], samples, report["bounds"], clients=clients, request_floor=floor
    )
    observed = report["evaluation"]
    for key in (
        "clients",
        "resources",
        "compiler_replacements",
        "worst_profile_queue_p95_ms",
        "request_error_rate",
    ):
        require(result[key] == observed[key], f"verdict mismatch for {key}")
    failures = list(result["failures"])
    if report["metrics_sample_failures"]:
        failures.append(f"{report['metrics_sample_failures']} authenticated metrics samples failed")
    require(
        all(f in observed["failures"] for f in failures) and observed["passed"] is (not observed["failures"]),
        "benchmark verdict omits measured failures",
    )
    counts = context["provider_counts"]
    for kind in ("count", "generation"):
        require(
            counts.get(kind + "_upstream_http_200", 0) > 0
            and counts[kind + "_requests"] >= counts[kind + "_upstream_http_200"],
            "no confirmed successful provider traffic for " + kind,
        )
    if preflight:
        require(
            report["seed_failures"] == 0
            and profiles["tool_using"]["tool_calls_total"] > 0
            and all(
                p["successes"] > 0
                and p["continuity_checks"] > p["continuity_failures"]
                and not p["isolation_violations"]
                for p in profiles.values()
            ),
            "preflight did not demonstrate both live profiles",
        )
    measurements = context["process_samples"]
    good = [s for s in measurements if "error" not in s]
    require(good and all(s["compilers"] for s in good), "no service and compiler measurements")

    def summed(sample, field):
        return sample["host"][field] + sum(c[field] for c in sample["compilers"])

    pool = {}
    for field, bound in [
        ("rss_bytes", "memory_growth_bytes"),
        ("threads", "thread_growth"),
        ("fds", "fd_growth"),
    ]:
        values = [summed(s, field) for s in good]
        growth = max(values) - values[0]
        pool[field] = {
            "first": values[0],
            "peak": max(values),
            "growth": growth,
            "within_numeric_bound": growth <= expected_bounds[bound],
        }
    elapsed = [s["time_unix"] for s in good]
    max_gap = max((b - a for a, b in zip(elapsed, elapsed[1:])), default=None)
    resource_failures = [
        f"host and compiler {field} growth exceeds the declared numeric bound"
        for field, values in pool.items()
        if not values["within_numeric_bound"]
    ]
    if (
        len(measurements) != len(good)
        or not max_gap
        or max_gap > 2 * context["rss_sample_period_seconds"] + 1
    ):
        resource_failures.append("incomplete process-tree resource sampling")
    if elapsed[-1] - elapsed[0] + 6 < duration or len({s["host"]["pid"] for s in good}) != 1:
        resource_failures.append("process sampling does not cover the expected host and duration")
    if any(len(s["compilers"]) != 4 for s in good) or any(
        s["resources"].get("compiler_process_count") != 4 for s in samples
    ):
        resource_failures.append("service or process samples differ from the four-compiler topology")
    if context.get("cleanup_error"):
        resource_failures.append("service cleanup failed: " + context["cleanup_error"])
    return {
        "raw_sha256": context["report_sha256"],
        "release_sha256": ARCHIVE,
        "completed_at": report["generated_at"],
        "planned_seconds": duration,
        "elapsed_seconds": report["actual_wall_seconds"],
        "client_count": len(clients),
        "clients_at_deadline": observed["clients"]["alive_at_deadline"],
        "clients_below_floor": sum(c["successes"] < floor for c in clients),
        "requests": sum(p["requests"] for p in profiles.values()),
        "successes": sum(p["successes"] for p in profiles.values()),
        "request_error_rate": observed["request_error_rate"],
        "shipped_harness_passed": observed["passed"],
        "engineering_passed": observed["passed"] and not resource_failures,
        "failures": observed["failures"],
        "supplemental_failures": resource_failures,
        "provider_counts": counts,
        "qualification_eligible": False,
        "service_metrics_samples": len(samples),
        "failed_service_metrics": report["metrics_sample_failures"],
        "first_resource_sample_s": samples[0]["elapsed_seconds"],
        "last_resource_sample_s": samples[-1]["elapsed_seconds"],
        "process_samples": len(good),
        "failed_process_samples": len(measurements) - len(good),
        "max_process_sample_gap_s": max_gap,
        "process_sample_duration_s": elapsed[-1] - elapsed[0],
        "compiler_pool_sizes": sorted({len(s["compilers"]) for s in good}),
        "compiler_pids_seen": len({c["pid"] for s in good for c in s["compilers"]}),
        "sampled_host_plus_compilers": pool,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("folder", type=Path)
    p.add_argument("--preflight", action="store_true")
    args = p.parse_args()
    print(json.dumps(verify(args.folder, args.preflight), indent=2))
