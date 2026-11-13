#!/usr/bin/env python3
"""Validate the fail-closed external evidence manifest for a GA release.

The drill reports count only as the bytes their own workflow run signed: the
check asks GitHub, through gh, for each one's attestation, so it needs gh,
signed in, and the network.
"""

import argparse
import hashlib
import json
import math
import subprocess
import sys
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
# What scripts/load_test.py requires of every client, restated here because
# this validator shares no code with the harness it checks: one successful
# measured request for each 135 seconds of the planned run.
LOAD_CLIENT_FLOOR_PERIOD_SECONDS = 135
# docs/capacity.md criterion 8, as /v1/metrics names the counter.
LOAD_REPLACEMENT_COUNTER = "runtime_unhealthy_replacements_total"
FAILURE_CATEGORIES = {
    "runtime_crashes",
    "unavailable_model_providers",
    "full_disks",
    "corrupt_state",
    "network_failures",
    "interrupted_writes",
}
# The drill reports are evidence only as the bytes their own workflow run
# signed (audit 2026-11-10, F-19). The gate used to check what a report said
# and nothing else, so a report typed by hand, or a real one edited after its
# run, passed. Each drill workflow now signs its report with actions/attest,
# in a job that runs nothing of the repository's, and the gate asks GitHub
# through gh for an attestation of the report's exact bytes, signed by that
# workflow of this repository, run from main on a GitHub-hosted runner, as
# SLSA build provenance (what actions/attest signs when given no predicate).
# The repository is the gate's, not the evidence's: a repository the evidence
# named could be anyone's fork running a copy of the workflow.
REPOSITORY = "nxrobins/sigil-pi"
ATTESTED_REPORTS = {
    "failure-injection.json": ".github/workflows/failure-drill.yml",
    "recovery-drill.json": ".github/workflows/recovery-drill.yml",
}
ATTESTATION_SOURCE_REF = "refs/heads/main"
ATTESTATION_PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
ATTESTATION_TIMEOUT_SECONDS = 120
# What scripts/release_drill.py records, restated because this validator
# shares no code with the harness it checks: recovery is timed to the first
# successful turn in the restored session (H2), and the candidate takes an
# online backup while it serves turns and is started on its restore (H5).
RECOVERY_MEASURED_TO = "the first successful turn in the restored session"
RECOVERY_ONLINE_PHASES = ("backup_online_new", "restore_online_new")


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


def _count(value):
    return type(value) is int and value >= 0


def _number(value):
    """A finite int or float; a bool is neither."""
    return type(value) in (int, float) and math.isfinite(value)


def attestation_command(path, workflow, *, repository=REPOSITORY):
    """The `gh attestation verify` the gate runs for one drill report. A
    maintainer checks a report by hand with the same command."""
    return ["gh", "attestation", "verify", str(path),
            "--repo", repository,
            "--signer-workflow", f"{repository}/{workflow}",
            "--source-ref", ATTESTATION_SOURCE_REF,
            "--predicate-type", ATTESTATION_PREDICATE_TYPE,
            "--deny-self-hosted-runners",
            "--format", "json"]


def _verified_parts(entry):
    """The statement and the signing certificate of one attestation gh
    printed as verified, or None for anything else."""
    result = entry.get("verificationResult") if isinstance(entry, dict) else None
    statement = result.get("statement") if isinstance(result, dict) else None
    signature = result.get("signature") if isinstance(result, dict) else None
    certificate = signature.get("certificate") if isinstance(signature, dict) else None
    if not isinstance(statement, dict) or not isinstance(certificate, dict):
        return None
    return statement, certificate


def verify_attestation(path, workflow, *, repository=REPOSITORY, run=None):
    """That GitHub holds an attestation of ``path``'s exact bytes signed by
    ``workflow`` of ``repository``, run from main on a GitHub-hosted runner,
    as SLSA provenance; returns the report, its digest, the signer and the
    runs that signed it.

    gh checks the signature and the policy the command states. The gate then
    reads what gh says it verified and requires exactly that identity of
    every attestation, so a gh that matched more loosely (its signer match
    is a prefix) cannot let another report through. ``run`` stands in for
    subprocess.run.
    """
    path = Path(path)
    name = path.name
    if not path.is_file():
        raise EvidenceError(f"{name} is missing: there is no drill report to verify")
    run = subprocess.run if run is None else run
    signer = f"https://github.com/{repository}/{workflow}@{ATTESTATION_SOURCE_REF}"
    try:
        answer = run(attestation_command(path, workflow, repository=repository),
                     capture_output=True, text=True, timeout=ATTESTATION_TIMEOUT_SECONDS,
                     check=False)
    except FileNotFoundError as error:
        raise EvidenceError(
            f"{name} cannot be checked: gh, the GitHub CLI, is not installed, and only "
            f"an attestation by {signer} makes the report evidence") from error
    except subprocess.TimeoutExpired as error:
        raise EvidenceError(
            f"{name}: gh attestation verify did not finish within "
            f"{ATTESTATION_TIMEOUT_SECONDS} s") from error
    if answer.returncode != 0:
        said = " ".join((answer.stderr or "").split())[-300:]
        raise EvidenceError(
            f"{name} carries no attestation by {signer} that verifies "
            f"(gh attestation verify exited {answer.returncode}: {said})")
    try:
        verified = json.loads(answer.stdout)
    except (TypeError, ValueError):
        verified = None
    parts = [_verified_parts(entry) for entry in verified] if isinstance(verified, list) else []
    if not parts or None in parts:
        raise EvidenceError(f"{name}: gh attestation verify printed no verified attestation")
    digest = _sha256(path)
    identity = {
        "subjectAlternativeName": signer,
        "sourceRepositoryURI": f"https://github.com/{repository}",
        "sourceRepositoryRef": ATTESTATION_SOURCE_REF,
        "runnerEnvironment": "github-hosted",
    }
    runs = []
    for statement, certificate in parts:
        subjects = statement.get("subject")
        digests = {subject["digest"].get("sha256") for subject in subjects
                   if isinstance(subject, dict) and isinstance(subject.get("digest"), dict)
                   } if isinstance(subjects, list) else set()
        if digest not in digests:
            raise EvidenceError(
                f"{name}: gh verified an attestation for other bytes than the "
                f"report's (sha256 {digest})")
        if statement.get("predicateType") != ATTESTATION_PREDICATE_TYPE:
            raise EvidenceError(
                f"{name}: gh verified an attestation of type "
                f"{statement.get('predicateType')!r}, not {ATTESTATION_PREDICATE_TYPE!r}")
        wrong = {key: certificate.get(key) for key, value in identity.items()
                 if certificate.get(key) != value}
        run_uri = certificate.get("runInvocationURI")
        if not (isinstance(run_uri, str)
                and run_uri.startswith(f"https://github.com/{repository}/actions/runs/")):
            wrong["runInvocationURI"] = run_uri
        if wrong:
            raise EvidenceError(
                f"{name}: gh verified an attestation not signed by {signer} on a "
                f"GitHub-hosted runner: {wrong}")
        runs.append(run_uri)
    return {"report": name, "sha256": digest, "signer": signer, "runs": runs}


def _validate_load_clients(report, profiles, *, planned, concurrency):
    """Every offered client has its own record, was still running at the
    deadline and completed its floor of successful requests (audit
    2026-11-10, F-18: a report used to validate with any number of its
    clients ended early, as long as each profile had one request)."""
    clients = report.get("clients")
    records = clients.get("records") if isinstance(clients, dict) else None
    if not isinstance(records, list):
        raise EvidenceError("load report does not record its load clients")
    if (len(records) != concurrency
            or not all(isinstance(record, dict) and type(record.get("client")) is int
                       for record in records)
            or {record["client"] for record in records} != set(range(concurrency))):
        raise EvidenceError(
            f"load report does not hold one record for each of its {concurrency} "
            "offered clients")
    floor = math.floor(planned / LOAD_CLIENT_FLOOR_PERIOD_SECONDS)
    reported_floor = clients.get("request_floor")
    if not _count(reported_floor) or reported_floor < floor:
        raise EvidenceError(
            f"load report weakens the per-client floor of {floor} successful requests")
    totals = {name: [0, 0] for name in LOAD_PROFILES}
    for record in records:
        requests, successes = record.get("requests"), record.get("successes")
        if not (_count(requests) and _count(successes) and successes <= requests):
            raise EvidenceError("load report has a client whose request counts are invalid")
        if record.get("alive_at_deadline") is not True:
            raise EvidenceError("load report has a client that was not alive at the deadline")
        if successes < reported_floor:
            raise EvidenceError(
                f"load report has a client below the per-client floor of "
                f"{reported_floor} successful requests")
        if record.get("profile") in totals:
            totals[record["profile"]][0] += requests
            totals[record["profile"]][1] += successes
    if any(totals[name] != [profiles[name]["requests"], profiles[name]["successes"]]
           for name in LOAD_PROFILES):
        raise EvidenceError("load report clients do not add up to its profiles")


def _validate_load_resources(report, bounds, disk_bound):
    """Criterion 6 from the samples themselves. The harness's summary of them
    must be what they show, as its error rate and queue percentile must be."""
    resources = [sample.get("resources") if isinstance(sample, dict) else None
                 for sample in report["resource_samples"]]
    if not all(isinstance(sample, dict) for sample in resources):
        raise EvidenceError("load report resource samples are invalid")
    summary = report.get("evaluation", {}).get("resources", {})

    def measured(name):
        return [sample[name] for sample in resources if _count(sample.get(name))]

    for field, source in (
            ("memory_growth_bytes", "memory_peak_bytes"),
            ("thread_growth", "thread_count"),
            ("fd_growth", "open_fd_count"),
            ("state_growth_bytes", "state_storage_bytes"),
            ("audit_growth_bytes", "audit_storage_bytes")):
        baseline = resources[0].get(source)
        if not _count(baseline):
            raise EvidenceError(f"load report exceeds or omits {field}")
        growth = max(measured(source)) - baseline
        if growth > bounds[field] or summary.get(field) != growth:
            raise EvidenceError(f"load report exceeds or omits {field}")
    free, total = measured("disk_free_bytes"), measured("disk_total_bytes")
    if not free or not total or not max(total):
        raise EvidenceError("load report exceeds or omits the disk-free bound")
    ratio = min(free) / max(total)
    if ratio < disk_bound or summary.get("minimum_disk_free_ratio") != ratio:
        raise EvidenceError("load report exceeds or omits the disk-free bound")


def _validate_load_service(report):
    """The compiler-replacement counter did not move in any sample, and the
    report says which provider, model and limits it measured."""
    counters = [sample.get(LOAD_REPLACEMENT_COUNTER) if isinstance(sample, dict) else None
                for sample in report["resource_samples"]]
    if not all(_count(value) for value in counters) or len(set(counters)) != 1:
        raise EvidenceError(
            "load report does not show an unchanged compiler-replacement counter "
            "in every resource sample")
    configuration = report.get("service_configuration")
    provider = configuration.get("provider") if isinstance(configuration, dict) else None
    if (not isinstance(provider, dict) or not _nonempty(provider.get("endpoint_host"))
            or not _nonempty(configuration.get("model"))
            or not isinstance(configuration.get("limits"), dict)
            or not configuration["limits"]):
        raise EvidenceError(
            "load report does not record the provider, the model and the limits in force")


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
                      "isolation_violations", "incomplete_telemetry"):
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
    _validate_load_clients(report, profiles, planned=planned, concurrency=concurrency)
    _validate_load_service(report)
    _validate_load_resources(report, bounds, disk_bound)
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


def _validate_recovery_report(evidence_dir, *, artifact_sha256, version,
                              rollback_from=None):
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
    # And it must be THE rollback target the candidate record names — not merely
    # some other release. A drill that rolled back to an arbitrary build proves
    # nothing about the rollback this candidate actually promises.
    if rollback_from is not None and (
            old.get("version") != rollback_from.get("version")
            or old.get("sha256") != rollback_from.get("sha256")):
        raise EvidenceError(
            "recovery drill rolled back to a release the candidate record does "
            "not name in rollback_from")
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
        if "failed" in phase:
            raise EvidenceError(f"recovery phase {phase['name']} failed")
        # Each start read the session and ran a turn in it (H2's sweep: a
        # report from the drill before H2, whose phases only waited for
        # readiness, still validated).
        _validate_turn(phase["name"], phase.get("turn"),
                       restored=phase["name"] != "clean_install")
    if evaluation.get("measured_to") != RECOVERY_MEASURED_TO:
        raise EvidenceError(
            "recovery report does not measure recovery to the first successful "
            "turn in the restored session")
    _validate_online_backup(
        report.get("online_backup"), artifact_sha256=artifact_sha256, version=version,
        maximum_backup_age=maximum_backup_age, maximum_restore=maximum_restore)
    return report


def _validate_turn(name, turn, *, restored):
    """The turn a recovery start ran, as the drill records it: it read the
    session it restored, sent the provider that conversation, and committed
    one exchange and its audit record. A clean install has none to read."""
    counts = ("messages_before", "messages_after", "audit_records_before",
              "audit_records_after", "history_messages_sent")
    if (not isinstance(turn, dict) or type(turn.get("read_before_turn")) is not bool
            or not all(_count(turn.get(key)) for key in counts)):
        raise EvidenceError(f"recovery phase {name} records no turn")
    if not restored:
        if turn["read_before_turn"] or turn["messages_before"] or turn["audit_records_before"]:
            raise EvidenceError(f"recovery phase {name} found a session on a clean install")
    elif not turn["read_before_turn"] or turn["messages_before"] < 1:
        raise EvidenceError(
            f"recovery phase {name} did not read the committed session before its turn")
    elif turn["history_messages_sent"] < 1:
        raise EvidenceError(
            f"recovery phase {name} sent the provider none of the session's conversation")
    if (turn["messages_after"] < turn["messages_before"] + 2
            or turn["audit_records_after"] <= turn["audit_records_before"]):
        raise EvidenceError(f"recovery phase {name} did not commit its turn")


def _validate_online_backup(online, *, artifact_sha256, version, maximum_backup_age,
                            maximum_restore):
    """The candidate's own tool backed the state up while the candidate
    served turns, and a start of the candidate restored from that backup
    continued the session (H5). The 15-minute objective rests on this path,
    the one production has; a report from the drill before H5 has none."""
    if not isinstance(online, dict) or online.get("mode") != "online":
        raise EvidenceError(
            "recovery report has no online backup taken while the candidate served turns")
    release = online.get("release")
    if (not isinstance(release, dict) or release.get("version") != version
            or release.get("sha256") != artifact_sha256):
        raise EvidenceError(
            "recovery report's online backup was not taken by the exact candidate")
    age = online.get("age_at_restore_seconds")
    if not _number(age) or not 0 <= age <= maximum_backup_age:
        raise EvidenceError(
            "recovery report's online backup age at restore does not prove the RPO")
    hold = online.get("max_session_lock_hold_ms")
    if not _number(hold) or hold < 0:
        raise EvidenceError(
            "recovery report does not record the longest session lock hold of its "
            "online backup")
    rto = online.get("restore_rto_seconds")
    if not _number(rto) or not 0 <= rto <= maximum_restore:
        raise EvidenceError(
            "recovery report's restore from the online backup does not prove the RTO")
    if online.get("measured_to") != RECOVERY_MEASURED_TO:
        raise EvidenceError(
            "recovery report's restore from the online backup is not measured to the "
            "first successful turn")
    load = online.get("turns_while_backing_up")
    counts = ("turns", "answered", "overlapping_backup", "held_by_backup")
    if (not isinstance(load, dict) or load.get("in_flight_when_backup_started") is not True
            or not all(_count(load.get(key)) for key in counts)
            or load["turns"] < 2 or load["answered"] != load["turns"]
            or load["overlapping_backup"] < 1 or load["held_by_backup"] < 1):
        raise EvidenceError("recovery report's online backup was not taken while turns ran")
    continuity = online.get("continuity")
    if (not isinstance(continuity, dict) or continuity.get("preserved") is not True
            or continuity.get("lost_or_changed") != []):
        raise EvidenceError(
            "recovery report's online backup did not preserve the committed sessions")
    phases = online.get("phases")
    if (not isinstance(phases, list) or not all(isinstance(phase, dict) for phase in phases)
            or tuple(phase.get("name") for phase in phases) != RECOVERY_ONLINE_PHASES):
        raise EvidenceError(
            "recovery report's online backup phases are incomplete or out of order")
    for phase in phases:
        if (phase.get("artifact_sha256") != artifact_sha256
                or phase.get("version") != version or "failed" in phase
                or not _number(phase.get("ready_seconds")) or phase["ready_seconds"] < 0):
            raise EvidenceError(
                "recovery report's online backup phase is not bound to the candidate")
        _validate_turn(phase["name"], phase.get("turn"), restored=True)
    held = online.get("session_in_backup")
    messages = held.get("messages") if isinstance(held, dict) else None
    if not _count(messages) or messages < 1 or phases[-1]["turn"]["messages_before"] != messages:
        raise EvidenceError(
            "recovery report's restore from the online backup did not read the session "
            "the backup holds")


def _full_sha(value):
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value)


def _validate_candidate(evidence_dir, *, artifact, artifact_sha256, version):
    """The frozen candidate's record must name the artifact being gated.

    Without this the gate would happily validate evidence against whatever
    archive it was handed. The record is the single place that says which bytes
    were published, and the rollback target every recovery drill must restore.
    """
    path = Path(evidence_dir) / "candidate.json"
    try:
        record = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise EvidenceError(
            "docs/evidence/candidate.json is missing or invalid") from error
    if record.get("schema_version") != 1 or record.get("version") != version:
        raise EvidenceError("candidate record schema/version does not match")
    if record.get("tag") != f"v{version}":
        raise EvidenceError("candidate tag must be v<version>")
    if record.get("file") != Path(artifact).name:
        raise EvidenceError("candidate record names a different artifact file")
    if record.get("sha256") != artifact_sha256:
        raise EvidenceError("candidate record digest does not match the artifact")
    asset = record.get("release_asset")
    expected = f"/releases/download/{record['tag']}/{record['file']}"
    if (not isinstance(asset, str) or not asset.startswith("https://")
            or expected not in asset):
        raise EvidenceError(
            "candidate release_asset must be the published HTTPS asset URL")
    rollback = record.get("rollback_from")
    if (not isinstance(rollback, dict) or not _full_sha(rollback.get("sha256"))
            or not _nonempty(rollback.get("version"))
            or rollback["sha256"] == artifact_sha256):
        raise EvidenceError(
            "candidate rollback_from must name a distinct published release")
    return record


def check(evidence_dir, artifact, version):
    """The readiness gate product-ci.sh runs: each drill report is the bytes
    its own workflow run signed, and the evidence says what a GA release
    needs (validate). The signatures come first, so a maintainer who has just
    run the drills sees the gate take or refuse them whatever else is still
    missing."""
    for name, workflow in ATTESTED_REPORTS.items():
        attested = verify_attestation(Path(evidence_dir) / name, workflow)
        print(f"attested: {name} (sha256 {attested['sha256']}) signed by "
              f"{attested['signer']} in {', '.join(attested['runs'])}", file=sys.stderr)
    return validate(evidence_dir, artifact, version)


def validate(evidence_dir, artifact, version):
    """What the evidence says, bound to the candidate. Whose the drill reports
    are is check()'s, which is the gate."""
    evidence_dir, artifact = Path(evidence_dir), Path(artifact)
    if not version or version.endswith("-dev"):
        raise EvidenceError("GA evidence requires a non-development VERSION")
    # Fail closed and BY NAME. This used to be `if artifact.is_file() else None`,
    # so a missing candidate let four bindings compare against None before the
    # sign-off check finally complained about the wrong thing.
    if not artifact.is_file():
        raise EvidenceError(f"candidate artifact is missing: {artifact}")
    artifact_sha256 = _sha256(artifact)
    record = _validate_candidate(
        evidence_dir, artifact=artifact, artifact_sha256=artifact_sha256,
        version=version)
    # The drill reports first: a workflow run produces them, so a maintainer
    # who has just run the drills sees the gate take or refuse them, whatever
    # else is still missing.
    _validate_failure_report(
        evidence_dir, artifact_sha256=artifact_sha256, version=version)
    _validate_recovery_report(
        evidence_dir, artifact_sha256=artifact_sha256, version=version,
        rollback_from=record["rollback_from"])
    for name in REQUIRED_REPORTS:
        path = evidence_dir / name
        if not path.is_file() or path.stat().st_size < 100:
            raise EvidenceError(f"required substantive report is missing: {name}")
    _validate_security_report(
        evidence_dir, artifact_sha256=artifact_sha256, version=version)
    _validate_load_report(
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


def _arguments():
    """The gate's command line: the evidence, the candidate and its version.
    Nothing turns the attestation check off."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--version", required=True)
    return parser


def main():
    parser = _arguments()
    args = parser.parse_args()
    try:
        signoff = check(args.evidence, args.artifact, args.version)
    except EvidenceError as error:
        parser.exit(1, f"readiness evidence gate failed: {error}\n")
    print(json.dumps({"ready": True, "version": signoff["version"],
                      "artifact_sha256": signoff["artifact_sha256"]}, sort_keys=True))


if __name__ == "__main__":
    main()
