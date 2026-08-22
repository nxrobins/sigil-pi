"""GA evidence must bind objective pilot/review results to the exact artifact."""

import hashlib
import json

import pytest

from conftest import PI_ROOT
from scripts.check_readiness_evidence import EvidenceError, REQUIRED_REPORTS, validate


def _fixture(tmp_path):
    artifact = tmp_path / "sigil-pi-1.0.0.tar.gz"
    artifact.write_bytes(b"exact signed candidate")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    for name in REQUIRED_REPORTS:
        (evidence / name).write_text((f"# {name}\n" + "objective evidence\n" * 10))
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    profile = {
        "requests": 1000,
        "successes": 1000,
        "latency": {"samples": 1000, "p50_ms": 100, "p95_ms": 200,
                    "p99_ms": 300},
        "queue_wait": {"samples": 1000, "p50_ms": 10, "p95_ms": 20,
                       "p99_ms": 30},
        "tool_contract_violations": 0,
        "continuity_checks": 100,
        "continuity_failures": 0,
        "isolation_violations": 0,
    }
    load_report = {
        "schema_version": 1,
        "test": "sigil-pi-v1-launch-capacity",
        "artifact": {"version": "1.0.0", "sha256": digest},
        "qualification_eligible": True,
        "planned_duration_seconds": 3600,
        "actual_wall_seconds": 3601,
        "topology": {
            "service_workers": 1,
            "offered_concurrency": 50,
            "tenant_credentials": 25,
            "max_concurrent_per_tenant": 2,
        },
        "forecast": {"peak_concurrent_turns": 25, "approved_by": "Named owner"},
        "bounds": {
            "memory_growth_bytes": 512 * 1024 * 1024,
            "thread_growth": 64,
            "fd_growth": 256,
            "state_growth_bytes": 10 * 1024 * 1024 * 1024,
            "audit_growth_bytes": 512 * 1024 * 1024,
            "minimum_disk_free_ratio": 0.2,
        },
        "profiles": {"single_step": dict(profile), "tool_using": dict(profile)},
        "seed_failures": 0,
        "metrics_sample_failures": 0,
        "resource_samples": [{"resources": {}}, {"resources": {}}],
        "evaluation": {
            "request_error_rate": 0.0,
            "worst_profile_queue_p95_ms": 20,
            "resources": {
                "memory_growth_bytes": 1,
                "thread_growth": 1,
                "fd_growth": 1,
                "state_growth_bytes": 1,
                "audit_growth_bytes": 1,
                "minimum_disk_free_ratio": 0.8,
            },
            "passed": True,
            "failures": [],
        },
    }
    (evidence / "load-test.json").write_text(json.dumps(load_report))
    recovery_report = {
        "schema_version": 1,
        "test": "sigil-pi-v1-release-recovery",
        "qualification_eligible": True,
        "qualification_failures": [],
        "artifacts": {
            "old": {"version": "0.9.0", "sha256": "b" * 64},
            "new": {"version": "1.0.0", "sha256": digest},
        },
        "backup": {"age_seconds": 100},
        "topology": {
            "workers": 1, "state_filesystem": "local-posix",
            "real_service_probe": True,
        },
        "thresholds": {
            "maximum_backup_age_seconds": 900,
            "maximum_restore_seconds": 4 * 3600,
            "maximum_rollback_seconds": 15 * 60,
        },
        "phases": [
            {"name": name, "version": artifact_info["version"],
             "artifact_sha256": artifact_info["sha256"], "ready_seconds": 1}
            for name, artifact_info in (
                ("clean_install", {"version": "0.9.0", "sha256": "b" * 64}),
                ("restore_old", {"version": "0.9.0", "sha256": "b" * 64}),
                ("upgrade_new", {"version": "1.0.0", "sha256": digest}),
                ("rollback_old", {"version": "0.9.0", "sha256": "b" * 64}),
            )
        ],
        "evaluation": {
            "mechanics_passed": True,
            "failures": [],
            "restore_rto_seconds": 120,
            "rollback_seconds": 30,
            "continuity": {
                "committed_session_files": 1,
                "preserved": True,
                "lost_or_changed": [],
            },
        },
    }
    (evidence / "recovery-drill.json").write_text(json.dumps(recovery_report))
    threat_model = evidence.parent / "security" / "threat-model.md"
    threat_model.parent.mkdir()
    threat_model.write_bytes(
        (PI_ROOT / "docs" / "security" / "threat-model.md").read_bytes())
    security_report = {
        "schema_version": 1,
        "test": "sigil-pi-v1-security-assurance",
        "artifact": {"version": "1.0.0", "sha256": digest},
        "independent_review": {
            "completed": True,
            "independent": True,
            "reviewer": "Independent Reviewer",
            "organization": "External Security Lab",
            "completed_date": "2026-07-01",
        },
        "unresolved_findings": {"critical": 0, "high": 0, "medium": 1, "low": 2},
        "exceptions": [],
        "threat_model": {
            "reviewed": True,
            "sha256": hashlib.sha256(threat_model.read_bytes()).hexdigest(),
        },
        "release_scans": {
            "dependencies": {
                "status": "passed", "scanner": "scanner-a",
                "database_version": "2026-07-01",
                "report_url": "https://example.invalid/scans/dependencies/1",
            },
            "artifact": {
                "status": "passed", "scanner": "scanner-b",
                "database_version": "2026-07-01",
                "report_url": "https://example.invalid/scans/artifact/1",
                "artifact_sha256": digest,
            },
        },
    }
    (evidence / "security-review.json").write_text(json.dumps(security_report))
    category_result = {
        "passed": True,
        "state_integrity_verified": True,
        "service_recovered": True,
        "injection": "controlled production-equivalent fault",
        "expected_behavior": "stable bounded failure",
        "observed_behavior": "stable bounded failure and recovery",
        "evidence_url": "https://example.invalid/failure/raw/1",
    }
    failure_report = {
        "schema_version": 1,
        "test": "sigil-pi-v1-failure-injection",
        "artifact": {"version": "1.0.0", "sha256": digest},
        "qualification_eligible": True,
        "qualification_failures": [],
        "topology": {
            "workers": 1, "state_filesystem": "local-posix",
            "production_artifact": True,
        },
        "started_unix": 100,
        "completed_unix": 200,
        "categories": {
            name: dict(category_result) for name in (
                "runtime_crashes", "unavailable_model_providers", "full_disks",
                "corrupt_state", "network_failures", "interrupted_writes")
        },
    }
    (evidence / "failure-injection.json").write_text(json.dumps(failure_report))
    signoff = {
        "schema_version": 1,
        "version": "1.0.0",
        "artifact_sha256": digest,
        "mandatory_ci_run": "https://example.invalid/ci/1",
        "provenance_attestation": "https://example.invalid/provenance/1",
        "sbom_attestation": "https://example.invalid/sbom/1",
        "pilot_dashboard": "https://example.invalid/dashboard/1",
        "unresolved_severity_1": 0,
        "unresolved_severity_2": 0,
        "pilot": {
            "start": "2026-06-01", "end": "2026-07-01",
            "service_availability_percent": 99.91,
            "successful_turns_percent": 99.6,
            "no_cross_tenant_access": True,
            "no_data_corruption": True,
            "no_unresolved_high_security_incident": True,
            "restore_drill_passed": True,
            "rollback_drill_passed": True,
            "resources_within_envelope": True,
            "simulated_incident_runbook_used": True,
        },
        "signoffs": [
            {"role": role, "name": f"Named {role}", "date": "2026-07-02"}
            for role in ("engineering", "security", "operations")
        ],
    }
    (evidence / "release-signoff.json").write_text(json.dumps(signoff))
    return evidence, artifact, signoff


def test_complete_digest_bound_evidence_passes(tmp_path):
    evidence, artifact, signoff = _fixture(tmp_path)
    assert validate(evidence, artifact, "1.0.0") == signoff


@pytest.mark.parametrize("mutation,match", [
    ("dev-version", "non-development"),
    ("digest", "digest does not match"),
    ("short-pilot", "shorter than 30"),
    ("availability", "below 99.9"),
    ("incident", "not proven"),
    ("defect", "unresolved severity"),
    ("role", "sign-offs are all required"),
])
def test_each_blocking_launch_assertion_fails_closed(tmp_path, mutation, match):
    evidence, artifact, signoff = _fixture(tmp_path)
    version = "1.0.0"
    if mutation == "dev-version":
        version = "1.0.0-dev"
    elif mutation == "digest":
        signoff["artifact_sha256"] = "0" * 64
    elif mutation == "short-pilot":
        signoff["pilot"]["end"] = "2026-06-29"
    elif mutation == "availability":
        signoff["pilot"]["service_availability_percent"] = 99.89
    elif mutation == "incident":
        signoff["pilot"]["simulated_incident_runbook_used"] = False
    elif mutation == "defect":
        signoff["unresolved_severity_2"] = 1
    elif mutation == "role":
        signoff["signoffs"] = signoff["signoffs"][:2]
    (evidence / "release-signoff.json").write_text(json.dumps(signoff))
    with pytest.raises(EvidenceError, match=match):
        validate(evidence, artifact, version)


def test_missing_or_placeholder_report_never_counts_as_evidence(tmp_path):
    evidence, artifact, _ = _fixture(tmp_path)
    (evidence / "load-test.md").write_text("placeholder")
    with pytest.raises(EvidenceError, match="substantive report"):
        validate(evidence, artifact, "1.0.0")


@pytest.mark.parametrize("mutation,match", [
    ("digest", "exact candidate"),
    ("duration", "below 3600"),
    ("concurrency", "below max"),
    ("owner", "forecast approval"),
    ("queue", "queue p95"),
    ("isolation", "isolation_violations"),
    ("resource", "memory_growth_bytes"),
    ("evaluation", "evaluation did not pass"),
])
def test_load_evidence_is_independently_rechecked(tmp_path, mutation, match):
    evidence, artifact, _ = _fixture(tmp_path)
    path = evidence / "load-test.json"
    report = json.loads(path.read_text())
    if mutation == "digest":
        report["artifact"]["sha256"] = "0" * 64
    elif mutation == "duration":
        report["planned_duration_seconds"] = 3599
    elif mutation == "concurrency":
        report["topology"]["offered_concurrency"] = 49
    elif mutation == "owner":
        report["forecast"]["approved_by"] = ""
    elif mutation == "queue":
        report["profiles"]["tool_using"]["queue_wait"]["p95_ms"] = 2000
    elif mutation == "isolation":
        report["profiles"]["single_step"]["isolation_violations"] = 1
    elif mutation == "resource":
        report["evaluation"]["resources"]["memory_growth_bytes"] = 10**12
    elif mutation == "evaluation":
        report["evaluation"]["passed"] = False
    path.write_text(json.dumps(report))
    with pytest.raises(EvidenceError, match=match):
        validate(evidence, artifact, "1.0.0")


@pytest.mark.parametrize("mutation,match", [
    ("digest", "exact candidate"),
    ("same-release", "distinct valid rollback"),
    ("probe", "topology/probe"),
    ("rpo", "RPO"),
    ("rto", "RTO"),
    ("rollback", "rollback threshold"),
    ("vacuous", "committed-session"),
    ("phases", "phases"),
])
def test_recovery_evidence_is_independently_rechecked(tmp_path, mutation, match):
    evidence, artifact, _ = _fixture(tmp_path)
    path = evidence / "recovery-drill.json"
    report = json.loads(path.read_text())
    if mutation == "digest":
        report["artifacts"]["new"]["sha256"] = "0" * 64
    elif mutation == "same-release":
        report["artifacts"]["old"] = dict(report["artifacts"]["new"])
    elif mutation == "probe":
        report["topology"]["real_service_probe"] = False
    elif mutation == "rpo":
        report["backup"]["age_seconds"] = 901
    elif mutation == "rto":
        report["evaluation"]["restore_rto_seconds"] = 4 * 3600 + 1
    elif mutation == "rollback":
        report["evaluation"]["rollback_seconds"] = 901
    elif mutation == "vacuous":
        report["evaluation"]["continuity"]["committed_session_files"] = 0
    elif mutation == "phases":
        report["phases"] = report["phases"][:-1]
    path.write_text(json.dumps(report))
    with pytest.raises(EvidenceError, match=match):
        validate(evidence, artifact, "1.0.0")


@pytest.mark.parametrize("mutation,match", [
    ("digest", "exact candidate"),
    ("independent", "independent review"),
    ("date", "completion date"),
    ("high", "critical or high"),
    ("exception", "exceptions"),
    ("threat", "threat model"),
    ("scan", "dependency and artifact scans"),
    ("scan-digest", "artifact scan"),
])
def test_security_evidence_is_independently_rechecked(tmp_path, mutation, match):
    evidence, artifact, _ = _fixture(tmp_path)
    path = evidence / "security-review.json"
    report = json.loads(path.read_text())
    if mutation == "digest":
        report["artifact"]["sha256"] = "0" * 64
    elif mutation == "independent":
        report["independent_review"]["independent"] = False
    elif mutation == "date":
        report["independent_review"]["completed_date"] = "not-a-date"
    elif mutation == "high":
        report["unresolved_findings"]["high"] = 1
    elif mutation == "exception":
        report["exceptions"] = ["waived without approval"]
    elif mutation == "threat":
        report["threat_model"]["sha256"] = "0" * 64
    elif mutation == "scan":
        del report["release_scans"]["dependencies"]
    elif mutation == "scan-digest":
        report["release_scans"]["artifact"]["artifact_sha256"] = "0" * 64
    path.write_text(json.dumps(report))
    with pytest.raises(EvidenceError, match=match):
        validate(evidence, artifact, "1.0.0")


@pytest.mark.parametrize("mutation,match", [
    ("digest", "exact candidate"),
    ("qualification", "qualification-eligible"),
    ("topology", "topology"),
    ("timing", "timing"),
    ("missing", "exactly six"),
    ("integrity", "category is incomplete"),
    ("recovery", "category is incomplete"),
    ("url", "category is incomplete"),
])
def test_failure_injection_evidence_is_independently_rechecked(
        tmp_path, mutation, match):
    evidence, artifact, _ = _fixture(tmp_path)
    path = evidence / "failure-injection.json"
    report = json.loads(path.read_text())
    if mutation == "digest":
        report["artifact"]["sha256"] = "0" * 64
    elif mutation == "qualification":
        report["qualification_eligible"] = False
    elif mutation == "topology":
        report["topology"]["production_artifact"] = False
    elif mutation == "timing":
        report["completed_unix"] = 99
    elif mutation == "missing":
        del report["categories"]["full_disks"]
    elif mutation == "integrity":
        report["categories"]["corrupt_state"]["state_integrity_verified"] = False
    elif mutation == "recovery":
        report["categories"]["runtime_crashes"]["service_recovered"] = False
    elif mutation == "url":
        report["categories"]["network_failures"]["evidence_url"] = "local"
    path.write_text(json.dumps(report))
    with pytest.raises(EvidenceError, match=match):
        validate(evidence, artifact, "1.0.0")
