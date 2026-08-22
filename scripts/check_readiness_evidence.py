#!/usr/bin/env python3
"""Validate the fail-closed external evidence manifest for a GA release."""

import argparse
import hashlib
import json
import math
from datetime import date
from pathlib import Path


REQUIRED_REPORTS = (
    "security-review.md",
    "load-test.md",
    "failure-injection.md",
    "recovery-drills.md",
    "pilot.md",
)
REQUIRED_ROLES = {"engineering", "security", "operations"}
LOAD_PROFILES = {"single_step", "tool_using"}
FAILURE_CATEGORIES = {
    "runtime_crashes",
    "unavailable_model_providers",
    "full_disks",
    "corrupt_state",
    "network_failures",
    "interrupted_writes",
}


class EvidenceError(RuntimeError):
    pass


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path, label):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise EvidenceError(f"{label} is missing or invalid") from error


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _https(value):
    return _nonempty(value) and value.startswith("https://")


def _validate_security_report(evidence_dir, *, artifact_sha256, version):
    report = _read_json(
        Path(evidence_dir) / "security-review.json", "security-review.json")
    if (report.get("schema_version") != 1
            or report.get("test") != "sigil-pi-v1-security-assurance"):
        raise EvidenceError("security report schema/test identity is invalid")
    artifact = report.get("artifact", {})
    if artifact.get("version") != version or artifact.get("sha256") != artifact_sha256:
        raise EvidenceError("security report is not bound to the exact candidate")
    review = report.get("independent_review", {})
    if (review.get("completed") is not True or review.get("independent") is not True
            or not _nonempty(review.get("reviewer"))
            or not _nonempty(review.get("organization"))):
        raise EvidenceError("security report lacks a completed independent review")
    try:
        date.fromisoformat(review["completed_date"])
    except (KeyError, TypeError, ValueError) as error:
        raise EvidenceError("security review completion date is invalid") from error
    findings = report.get("unresolved_findings", {})
    for severity in ("critical", "high", "medium", "low"):
        if not isinstance(findings.get(severity), int) or findings[severity] < 0:
            raise EvidenceError("security report finding counts are invalid")
    if findings["critical"] or findings["high"]:
        raise EvidenceError("security report has unresolved critical or high findings")
    if report.get("exceptions") != []:
        raise EvidenceError("security report contains unapproved exceptions")
    threat = report.get("threat_model", {})
    threat_path = Path(evidence_dir).parent / "security" / "threat-model.md"
    if (threat.get("reviewed") is not True or not threat_path.is_file()
            or threat.get("sha256") != _sha256(threat_path)):
        raise EvidenceError("security report is not bound to the reviewed threat model")
    scans = report.get("release_scans", {})
    if set(scans) != {"dependencies", "artifact"}:
        raise EvidenceError("security report lacks dependency and artifact scans")
    for name, scan in scans.items():
        if (not isinstance(scan, dict) or scan.get("status") != "passed"
                or not _nonempty(scan.get("scanner"))
                or not _nonempty(scan.get("database_version"))
                or not _https(scan.get("report_url"))):
            raise EvidenceError(f"security {name} scan evidence is incomplete")
        if name == "artifact" and scan.get("artifact_sha256") != artifact_sha256:
            raise EvidenceError("artifact scan is not bound to the exact candidate")
    return report


def _validate_failure_report(evidence_dir, *, artifact_sha256, version):
    report = _read_json(
        Path(evidence_dir) / "failure-injection.json", "failure-injection.json")
    if (report.get("schema_version") != 1
            or report.get("test") != "sigil-pi-v1-failure-injection"):
        raise EvidenceError("failure-injection report schema/test identity is invalid")
    artifact = report.get("artifact", {})
    if artifact.get("version") != version or artifact.get("sha256") != artifact_sha256:
        raise EvidenceError("failure-injection report is not bound to the exact candidate")
    if (report.get("qualification_eligible") is not True
            or report.get("qualification_failures") != []):
        raise EvidenceError("failure-injection report is not qualification-eligible")
    topology = report.get("topology", {})
    if (topology.get("workers") != 1
            or topology.get("state_filesystem") != "local-posix"
            or topology.get("production_artifact") is not True):
        raise EvidenceError("failure-injection report topology is not supported")
    started, completed = report.get("started_unix"), report.get("completed_unix")
    if (not isinstance(started, (int, float)) or not isinstance(completed, (int, float))
            or started < 0 or completed < started):
        raise EvidenceError("failure-injection report timing is invalid")
    categories = report.get("categories")
    if not isinstance(categories, dict) or set(categories) != FAILURE_CATEGORIES:
        raise EvidenceError("failure-injection report does not cover exactly six categories")
    for name, result in categories.items():
        if (not isinstance(result, dict) or result.get("passed") is not True
                or result.get("state_integrity_verified") is not True
                or result.get("service_recovered") is not True
                or not _nonempty(result.get("injection"))
                or not _nonempty(result.get("expected_behavior"))
                or not _nonempty(result.get("observed_behavior"))
                or not _https(result.get("evidence_url"))):
            raise EvidenceError(f"failure-injection category is incomplete: {name}")
    return report


def _validate_load_report(evidence_dir, *, artifact_sha256, version):
    path = Path(evidence_dir) / "load-test.json"
    try:
        report = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise EvidenceError("load-test.json is missing or invalid") from error
    if report.get("schema_version") != 1 or report.get("test") != (
            "sigil-pi-v1-launch-capacity"):
        raise EvidenceError("load report schema/test identity is invalid")
    artifact = report.get("artifact", {})
    if artifact.get("version") != version or artifact.get("sha256") != artifact_sha256:
        raise EvidenceError("load report is not bound to the exact candidate")
    if report.get("qualification_eligible") is not True:
        raise EvidenceError("load report is not qualification-eligible")
    planned = report.get("planned_duration_seconds")
    actual = report.get("actual_wall_seconds")
    if not isinstance(planned, (int, float)) or planned < 3600:
        raise EvidenceError("load report duration is below 3600 seconds")
    if not isinstance(actual, (int, float)) or actual < planned:
        raise EvidenceError("load report did not run for its planned duration")
    topology, forecast = report.get("topology", {}), report.get("forecast", {})
    concurrency = topology.get("offered_concurrency")
    peak = forecast.get("peak_concurrent_turns")
    per_tenant = topology.get("max_concurrent_per_tenant")
    tenants = topology.get("tenant_credentials")
    if not all(isinstance(value, int) and value > 0
               for value in (concurrency, peak, per_tenant, tenants)):
        raise EvidenceError("load report topology/forecast is invalid")
    if concurrency < max(2 * peak, 50):
        raise EvidenceError("load report concurrency is below max(2x forecast, 50)")
    if tenants < math.ceil(concurrency / per_tenant):
        raise EvidenceError("load report has too few tenant credentials")
    if topology.get("service_workers") != 1:
        raise EvidenceError("load report is outside the measured one-worker topology")
    if not isinstance(forecast.get("approved_by"), str) or not forecast["approved_by"].strip():
        raise EvidenceError("load report lacks forecast approval")

    bounds = report.get("bounds", {})
    maximums = {
        "memory_growth_bytes": 512 * 1024 * 1024,
        "thread_growth": 64,
        "fd_growth": 256,
        "state_growth_bytes": 10 * 1024 * 1024 * 1024,
        "audit_growth_bytes": 512 * 1024 * 1024,
    }
    for field, maximum in maximums.items():
        value = bounds.get(field)
        if not isinstance(value, (int, float)) or value < 0 or value > maximum:
            raise EvidenceError(f"load report weakens the approved {field} bound")
    disk_bound = bounds.get("minimum_disk_free_ratio")
    if not isinstance(disk_bound, (int, float)) or disk_bound < 0.2 or disk_bound > 1:
        raise EvidenceError("load report weakens the approved disk-free bound")

    profiles = report.get("profiles", {})
    if not isinstance(profiles, dict) or not LOAD_PROFILES <= set(profiles):
        raise EvidenceError("load report does not contain both required profiles")
    total_requests = total_successes = 0
    for name in LOAD_PROFILES:
        profile = profiles[name]
        requests, successes = profile.get("requests"), profile.get("successes")
        if not isinstance(requests, int) or requests <= 0:
            raise EvidenceError(f"load profile {name} has no requests")
        if not isinstance(successes, int) or not 0 <= successes <= requests:
            raise EvidenceError(f"load profile {name} successes are invalid")
        total_requests += requests
        total_successes += successes
        for distribution in ("latency", "queue_wait"):
            values = profile.get(distribution, {})
            if values.get("samples") != requests or not all(
                    isinstance(values.get(field), (int, float))
                    for field in ("p50_ms", "p95_ms", "p99_ms")):
                raise EvidenceError(
                    f"load profile {name} has incomplete {distribution} percentiles")
        for field in ("tool_contract_violations", "continuity_failures",
                      "isolation_violations"):
            if profile.get(field) != 0:
                raise EvidenceError(f"load profile {name} has {field}")
        if not isinstance(profile.get("continuity_checks"), int) or (
                profile["continuity_checks"] <= 0):
            raise EvidenceError(f"load profile {name} has no continuity checks")
    error_rate = 1 - total_successes / total_requests
    evaluation = report.get("evaluation", {})
    if error_rate >= 0.005 or evaluation.get("request_error_rate") != error_rate:
        raise EvidenceError("load report request error rate is not below 0.5%")
    worst_queue = max(profiles[name]["queue_wait"]["p95_ms"] for name in LOAD_PROFILES)
    if worst_queue >= 2000 or evaluation.get("worst_profile_queue_p95_ms") != worst_queue:
        raise EvidenceError("load report queue p95 is not below 2000 ms")
    if report.get("seed_failures") != 0 or report.get("metrics_sample_failures") != 0:
        raise EvidenceError("load report contains setup or metrics failures")
    if not isinstance(report.get("resource_samples"), list) or len(report["resource_samples"]) < 2:
        raise EvidenceError("load report lacks resource samples")
    resources = evaluation.get("resources", {})
    for field in maximums:
        value = resources.get(field)
        if not isinstance(value, (int, float)) or value < 0 or value > bounds[field]:
            raise EvidenceError(f"load report exceeds or omits {field}")
    ratio = resources.get("minimum_disk_free_ratio")
    if not isinstance(ratio, (int, float)) or ratio < disk_bound:
        raise EvidenceError("load report exceeds or omits the disk-free bound")
    if evaluation.get("passed") is not True or evaluation.get("failures") != []:
        raise EvidenceError("load report conjunctive evaluation did not pass")
    return report


def _validate_recovery_report(evidence_dir, *, artifact_sha256, version):
    path = Path(evidence_dir) / "recovery-drill.json"
    try:
        report = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise EvidenceError("recovery-drill.json is missing or invalid") from error
    if (report.get("schema_version") != 1
            or report.get("test") != "sigil-pi-v1-release-recovery"):
        raise EvidenceError("recovery report schema/test identity is invalid")
    artifacts = report.get("artifacts", {})
    old, new = artifacts.get("old", {}), artifacts.get("new", {})
    if new.get("version") != version or new.get("sha256") != artifact_sha256:
        raise EvidenceError("recovery report is not bound to the exact candidate")
    if (not isinstance(old.get("version"), str)
            or not _full_sha(old.get("sha256"))
            or old.get("version") == version
            or old.get("sha256") == artifact_sha256):
        raise EvidenceError("recovery report lacks a distinct valid rollback release")
    if report.get("qualification_eligible") is not True:
        raise EvidenceError("recovery report is not qualification-eligible")
    if report.get("qualification_failures") != []:
        raise EvidenceError("recovery report contains qualification failures")
    topology = report.get("topology", {})
    if (topology.get("workers") != 1
            or topology.get("state_filesystem") != "local-posix"
            or topology.get("real_service_probe") is not True):
        raise EvidenceError("recovery report topology/probe is not the supported drill")
    thresholds = report.get("thresholds", {})
    maximum_backup_age = thresholds.get("maximum_backup_age_seconds")
    maximum_restore = thresholds.get("maximum_restore_seconds")
    maximum_rollback = thresholds.get("maximum_rollback_seconds")
    if (not isinstance(maximum_backup_age, (int, float))
            or not 0 < maximum_backup_age <= 900):
        raise EvidenceError("recovery report weakens the 15-minute RPO threshold")
    if (not isinstance(maximum_restore, (int, float))
            or not 0 < maximum_restore <= 4 * 3600):
        raise EvidenceError("recovery report weakens the four-hour RTO threshold")
    if (not isinstance(maximum_rollback, (int, float))
            or not 0 < maximum_rollback <= 15 * 60):
        raise EvidenceError("recovery report weakens the 15-minute rollback threshold")
    backup_age = report.get("backup", {}).get("age_seconds")
    if (not isinstance(backup_age, (int, float))
            or not 0 <= backup_age <= maximum_backup_age):
        raise EvidenceError("recovery report backup age does not prove RPO")
    evaluation = report.get("evaluation", {})
    if evaluation.get("mechanics_passed") is not True or evaluation.get("failures") != []:
        raise EvidenceError("recovery report mechanics did not pass")
    rto, rollback = evaluation.get("restore_rto_seconds"), evaluation.get("rollback_seconds")
    if (not isinstance(rto, (int, float)) or rto < 0
            or rto > maximum_restore or rto > 4 * 3600):
        raise EvidenceError("recovery report does not prove the RTO threshold")
    if (not isinstance(rollback, (int, float)) or rollback < 0
            or rollback > maximum_rollback or rollback > 15 * 60):
        raise EvidenceError("recovery report does not prove the rollback threshold")
    continuity = evaluation.get("continuity", {})
    if (not isinstance(continuity.get("committed_session_files"), int)
            or continuity["committed_session_files"] <= 0
            or continuity.get("preserved") is not True
            or continuity.get("lost_or_changed") != []):
        raise EvidenceError("recovery report has no preserved committed-session evidence")
    phases = report.get("phases")
    expected = ["clean_install", "restore_old", "upgrade_new", "rollback_old"]
    if (not isinstance(phases, list)
            or [phase.get("name") for phase in phases if isinstance(phase, dict)] != expected
            or len(phases) != len(expected)):
        raise EvidenceError("recovery report phases are incomplete or out of order")
    expected_artifacts = [old, old, new, old]
    for phase, expected_artifact in zip(phases, expected_artifacts):
        if (phase.get("artifact_sha256") != expected_artifact["sha256"]
                or phase.get("version") != expected_artifact["version"]
                or not isinstance(phase.get("ready_seconds"), (int, float))
                or phase["ready_seconds"] < 0):
            raise EvidenceError("recovery report phase is not bound to its release")
    return report


def _full_sha(value):
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value)


def validate(evidence_dir, artifact, version):
    evidence_dir, artifact = Path(evidence_dir), Path(artifact)
    if not version or version.endswith("-dev"):
        raise EvidenceError("GA evidence requires a non-development VERSION")
    for name in REQUIRED_REPORTS:
        path = evidence_dir / name
        if not path.is_file() or path.stat().st_size < 100:
            raise EvidenceError(f"required substantive report is missing: {name}")
    artifact_sha256 = _sha256(artifact) if artifact.is_file() else None
    _validate_security_report(
        evidence_dir, artifact_sha256=artifact_sha256, version=version)
    _validate_load_report(
        evidence_dir, artifact_sha256=artifact_sha256, version=version)
    _validate_failure_report(
        evidence_dir, artifact_sha256=artifact_sha256, version=version)
    _validate_recovery_report(
        evidence_dir, artifact_sha256=artifact_sha256, version=version)
    signoff_path = evidence_dir / "release-signoff.json"
    try:
        signoff = json.loads(signoff_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise EvidenceError("release-signoff.json is missing or invalid") from error
    if signoff.get("schema_version") != 1 or signoff.get("version") != version:
        raise EvidenceError("release sign-off schema/version does not match")
    if not artifact.is_file() or signoff.get("artifact_sha256") != artifact_sha256:
        raise EvidenceError("signed-off artifact digest does not match the candidate")
    for field in ("mandatory_ci_run", "provenance_attestation", "sbom_attestation",
                  "pilot_dashboard"):
        value = signoff.get(field)
        if not isinstance(value, str) or not value.startswith("https://"):
            raise EvidenceError(f"release sign-off requires an HTTPS {field} reference")
    if signoff.get("unresolved_severity_1") != 0 or signoff.get("unresolved_severity_2") != 0:
        raise EvidenceError("release has unresolved severity-1 or severity-2 defects")
    pilot = signoff.get("pilot", {})
    try:
        start = date.fromisoformat(pilot["start"])
        end = date.fromisoformat(pilot["end"])
    except (KeyError, TypeError, ValueError) as error:
        raise EvidenceError("pilot start/end dates are missing or invalid") from error
    if (end - start).days < 30:
        raise EvidenceError("production-equivalent pilot is shorter than 30 days")
    thresholds = {
        "service_availability_percent": 99.9,
        "successful_turns_percent": 99.5,
    }
    for field, minimum in thresholds.items():
        value = pilot.get(field)
        if not isinstance(value, (int, float)) or value < minimum:
            raise EvidenceError(f"pilot {field} is below {minimum}")
    for field in ("no_cross_tenant_access", "no_data_corruption",
                  "no_unresolved_high_security_incident", "restore_drill_passed",
                  "rollback_drill_passed", "resources_within_envelope",
                  "simulated_incident_runbook_used"):
        if pilot.get(field) is not True:
            raise EvidenceError(f"pilot assertion is not proven: {field}")
    signoffs = signoff.get("signoffs")
    if not isinstance(signoffs, list):
        raise EvidenceError("named sign-offs are missing")
    roles = set()
    for item in signoffs:
        if not isinstance(item, dict) or not all(
                isinstance(item.get(field), str) and item[field].strip()
                for field in ("role", "name", "date")):
            raise EvidenceError("each sign-off requires non-empty role, name, and date")
        try:
            date.fromisoformat(item["date"])
        except ValueError as error:
            raise EvidenceError("sign-off date is invalid") from error
        roles.add(item["role"])
    if not REQUIRED_ROLES <= roles:
        raise EvidenceError("engineering, security, and operations sign-offs are all required")
    return signoff


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    try:
        signoff = validate(args.evidence, args.artifact, args.version)
    except EvidenceError as error:
        parser.exit(1, f"readiness evidence gate failed: {error}\n")
    print(json.dumps({"ready": True, "version": signoff["version"],
                      "artifact_sha256": signoff["artifact_sha256"]}, sort_keys=True))


if __name__ == "__main__":
    main()
