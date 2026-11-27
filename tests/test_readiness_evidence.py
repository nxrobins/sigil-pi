"""GA evidence must bind objective pilot/review results to the exact artifact."""

import hashlib
import json

import pytest

from conftest import PI_ROOT
from scripts.check_readiness_evidence import EvidenceError, REQUIRED_REPORTS, validate


def _turn(before, *, read):
    """A phase's turn as the recovery drill records it: it read ``before``
    messages and as many audit records, sent the provider all of them, and
    committed one exchange."""
    return {"session": "drill-session", "read_before_turn": read,
            "messages_before": before, "messages_after": before + 2,
            "audit_records_before": before, "audit_records_after": before + 2,
            "provider_requests": 1, "history_messages_sent": before, "seconds": 0.4}


def _failure_state(category):
    """The report contract: three seeded sessions and two completed schedules."""
    fault = category + "-committed"
    bystanders = [category + "-bystander-daily", category + "-bystander-weekly"]
    tenant = hashlib.sha256(b"failure-drill-tenant").hexdigest()
    rows, turns, held, sandboxes = {}, [], [], {}
    for session in [fault, *bystanders]:
        files = {name: {"size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
                 for name, body in (("seed-one.txt", (session + " first").encode()),
                                    ("seed-two.txt", (session + " second").encode()))}
        sandboxes[session] = {"layout_before": 2, "layout_after": 2,
                             "files_before": files, "files_after": files,
                             "preserved": True, "missing": [], "changed": [], "unexpected": []}
    for session, name, interval in zip(bystanders, ("daily", "weekly"),
                                       (86400000, 604800000)):
        held.append({"session": session, "committed_messages": 4,
                     "committed_audit_records": 8, "conversation_identical": True,
                     "audit_identical": True, "preserved": True,
                     **{f"{part}_sha256_{when}": hashlib.sha256((session + part).encode()).hexdigest()
                        for part in ("conversation", "audit") for when in ("before", "after")}})
        row = {"tenant_sha256": tenant, "principal": "failure-drill-operator", "name": name,
               "session": session, "internal_session":
               f"v1:{tenant}:{hashlib.sha256(session.encode()).hexdigest()}",
               "message": name + " scheduled message", "every_ms": interval,
               "created_unix": 90, "updated_unix": 95, "last_run": 95,
               "last_status": "ok", "lease_owner": None, "lease_until": None}
        rows[tenant + ":" + name] = row
        turns.append({**{field: row[field] for field in
                         ("name", "session", "message", "every_ms", "last_run", "last_status")},
                      "provider_requests": 1, "committed_reply": True, "lease_cleared": True})
    return {"preserved": True, "lost_or_changed": [], "committed_customer_files": 12,
            "committed_session_files": 3,
            "fault_session": {"session": fault, "held_committed_content": True, "preserved": True,
                              "committed_messages": 2, "committed_audit_records": 4},
            "bystanders": held, "sandboxes": sandboxes,
            "schedules": {"schema_before": 2, "schema_after": 2, "before": rows, "after": rows,
                          "preserved": True, "differences": []}, "scheduled_turns": turns}


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
        "incomplete_telemetry": 0,
    }
    # What the harness records since audit 2026-11-10 (F-18): one account per
    # load client, the compiler-replacement counter beside every resource
    # sample, and the configuration the service reported. Fifty clients at 40
    # successful requests each are the two profiles' 1,000.
    clients = [
        {"client": index, "profile": ("single_step", "tool_using")[index % 2],
         "requests": 40, "successes": 40, "alive_at_deadline": True,
         "stopped_by": "deadline"}
        for index in range(50)]
    resources = {
        "memory_peak_bytes": 100_000_000, "thread_count": 8, "open_fd_count": 16,
        "state_storage_bytes": 1_000_000, "audit_storage_bytes": 100_000,
        "disk_free_bytes": 800, "disk_total_bytes": 1000,
    }
    grown = {name: value if name.startswith("disk_") else value + 1
             for name, value in resources.items()}
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
        "request_timeout_seconds": 135.0,
        "clients": {"request_floor": 26, "records": clients},
        "service_configuration": {
            "provider": {"endpoint_host": "provider.example"},
            "model": "named-model",
            "limits": {"turn_deadline_seconds": 120, "max_concurrent_turns": 2},
        },
        "resource_samples": [
            {"elapsed_seconds": 0.0, "resources": resources,
             "runtime_generation": 1, "runtime_unhealthy_replacements_total": 0},
            {"elapsed_seconds": 3600.5, "resources": grown,
             "runtime_generation": 1, "runtime_unhealthy_replacements_total": 0},
        ],
        "evaluation": {
            "request_error_rate": 0.0,
            "worst_profile_queue_p95_ms": 20,
            "clients": {"offered": 50, "alive_at_deadline": 50,
                        "fewest_successes": 40},
            "compiler_replacements": {"first": 0, "last": 0, "rise": 0},
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
        # Every phase read the session through the release it started and ran
        # one turn in it (H2): the clean install had none to read, the others
        # read 2, 4 and 6 messages and each left two more.
        "phases": [
            {"name": name, "version": artifact_info["version"],
             "artifact_sha256": artifact_info["sha256"], "ready_seconds": 1,
             "first_turn_seconds": 2, "turn": _turn(before, read=before > 0)}
            for name, artifact_info, before in (
                ("clean_install", {"version": "0.9.0", "sha256": "b" * 64}, 0),
                ("restore_old", {"version": "0.9.0", "sha256": "b" * 64}, 2),
                ("upgrade_new", {"version": "1.0.0", "sha256": digest}, 4),
                ("rollback_old", {"version": "0.9.0", "sha256": "b" * 64}, 6),
            )
        ],
        # The candidate's own backup, taken while it served turns, and the
        # start restored from it (H5).
        "online_backup": {
            "release": {"version": "1.0.0", "sha256": digest},
            "mode": "online",
            "age_at_restore_seconds": 1.5,
            "max_session_lock_hold_ms": 1.8,
            "restore_rto_seconds": 0.5,
            "measured_to": "the first successful turn in the restored session",
            "turns_while_backing_up": {
                "turns": 2, "answered": 2, "in_flight_when_backup_started": True,
                "overlapping_backup": 1, "held_by_backup": 1,
            },
            "session_in_backup": {"messages": 12, "audit_records": 12},
            "continuity": {"committed_customer_files": 2, "preserved": True,
                           "lost_or_changed": []},
            "phases": [
                {"name": name, "version": "1.0.0", "artifact_sha256": digest,
                 "ready_seconds": 1, "first_turn_seconds": 2,
                 "turn": _turn(before, read=True)}
                for name, before in (("backup_online_new", 8), ("restore_online_new", 12))
            ],
        },
        "evaluation": {
            "mechanics_passed": True,
            "failures": [],
            "restore_rto_seconds": 120,
            "rollback_seconds": 30,
            "measured_to": "the first successful turn in the restored session",
            "continuity": {
                "committed_session_files": 1,
                "preserved": True,
                "lost_or_changed": [],
                "session": {"tenant": "drill-fixture-tenant", "session": "drill-session",
                            "committed_messages": 2, "messages_after": 8},
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
        "evidence_url": "https://github.com/nxrobins/sigil-pi/actions/runs/7001",
    }
    failure_report = {
        "schema_version": 1,
        "test": "sigil-pi-v1-failure-injection",
        "artifact": {"version": "1.0.0", "sha256": digest},
        "qualification_eligible": True,
        "qualification_failures": [],
        "topology": {
            "workers": 1, "state_filesystem": "local-posix",
            "production_artifact": True, "bounded_filesystem": "tmpfs",
        },
        "started_unix": 100,
        "completed_unix": 200,
        "categories": {
            name: {**category_result, "continuity": _failure_state(name)} for name in (
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
    # The frozen candidate record: which published bytes this evidence is about,
    # and the release a rollback drill must restore. Its rollback_from matches
    # the recovery report's `old` artifact by construction.
    (evidence / "candidate.json").write_text(json.dumps({
        "schema_version": 1,
        "version": "1.0.0",
        "tag": "v1.0.0",
        "platform_tag": "linux-x86_64",
        "file": artifact.name,
        "sha256": digest,
        "sigil_ref": "a" * 40,
        "crates_tree": "c" * 40,
        "stdlib_tree": "d" * 40,
        "release_asset": ("https://github.com/nxrobins/sigil-pi/releases/"
                          f"download/v1.0.0/{artifact.name}"),
        "rollback_from": {"version": "0.9.0", "sha256": "b" * 64},
    }))
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


@pytest.mark.parametrize("filesystem", [None, "", "ext4", "missing"])
def test_full_disk_evidence_requires_the_bounded_filesystem(tmp_path, filesystem):
    evidence, artifact, _ = _fixture(tmp_path)
    path = evidence / "failure-injection.json"
    report = json.loads(path.read_text())
    if filesystem == "missing":
        del report["topology"]["bounded_filesystem"]
    else:
        report["topology"]["bounded_filesystem"] = filesystem
    path.write_text(json.dumps(report))
    with pytest.raises(EvidenceError, match="topology"):
        validate(evidence, artifact, "1.0.0")


@pytest.mark.parametrize("damage", [
    "missing state", "state from another category", "bystanders from another category",
    "lost committed file", "empty fault session", "one bystander",
    "bystander grew", "different bystander hash", "duplicate session", "empty sandbox",
    "sandbox changed", "sandbox gained file", "sandbox moved", "schedule dropped",
    "schedule changed", "unknown schedule identity", "different tenant", "invalid schedule time",
    "missing scheduled run", "no provider request", "different scheduled message",
    "different scheduled session", "different completion time", "malformed run name",
])
def test_expanded_failure_evidence_cannot_hide_missing_or_changed_state(tmp_path, damage):
    evidence, artifact, _ = _fixture(tmp_path)
    path = evidence / "failure-injection.json"
    report = json.loads(path.read_text())
    category = report["categories"]["full_disks"]
    state = category["continuity"]
    sandbox = next(iter(state["sandboxes"].values()))
    key = next(iter(state["schedules"]["before"]))
    turn = state["scheduled_turns"][0]
    if damage == "missing state":
        del category["continuity"]
    elif damage == "state from another category":
        category["continuity"] = report["categories"]["runtime_crashes"]["continuity"]
    elif damage == "bystanders from another category":
        foreign = report["categories"]["runtime_crashes"]["continuity"]
        for field in ("bystanders", "schedules", "scheduled_turns"):
            state[field] = foreign[field]
        for session in [b["session"] for b in foreign["bystanders"]]:
            local = session.replace("runtime_crashes", "full_disks")
            del state["sandboxes"][local]
            state["sandboxes"][session] = foreign["sandboxes"][session]
    elif damage == "lost committed file":
        state["lost_or_changed"] = ["sessions/lost.kv"]
    elif damage == "empty fault session":
        state["fault_session"]["committed_messages"] = 0
    elif damage == "one bystander":
        state["bystanders"].pop()
    elif damage == "bystander grew":
        state["bystanders"][0]["conversation_identical"] = False
    elif damage == "different bystander hash":
        state["bystanders"][0]["audit_sha256_after"] = "a" * 64
    elif damage == "duplicate session":
        state["bystanders"][1]["session"] = state["bystanders"][0]["session"]
    elif damage == "empty sandbox":
        sandbox["files_before"] = sandbox["files_after"] = {}
    elif damage == "sandbox changed":
        sandbox["files_after"]["seed-one.txt"]["size"] += 1
    elif damage == "sandbox gained file":
        sandbox["files_after"]["unintended.txt"] = {"size": 1, "sha256": "a" * 64}
    elif damage == "sandbox moved":
        sandbox["layout_after"] = 1
    elif damage == "schedule dropped":
        del state["schedules"]["after"][key]
    elif damage == "schedule changed":
        state["schedules"]["after"][key]["message"] += " changed"
    elif damage == "unknown schedule identity":
        for when in ("before", "after"):
            state["schedules"][when][key]["internal_session"] = "someone else's session"
    elif damage == "different tenant":
        tenant = hashlib.sha256(b"another tenant").hexdigest()
        for when in ("before", "after"):
            rows = state["schedules"][when]
            for old_key in list(rows):
                row = rows.pop(old_key)
                row["tenant_sha256"] = tenant
                row["internal_session"] = f"v1:{tenant}:{hashlib.sha256(row['session'].encode()).hexdigest()}"
                rows[f"{tenant}:{row['name']}"] = row
    elif damage == "invalid schedule time":
        for when in ("before", "after"):
            state["schedules"][when][key]["last_run"] = float("nan")
    elif damage == "missing scheduled run":
        state["scheduled_turns"].pop()
    elif damage == "no provider request":
        turn["provider_requests"] = 0
    elif damage == "different scheduled message":
        turn["message"] = "unrelated run"
    elif damage == "different scheduled session":
        turn["session"] = state["fault_session"]["session"]
    elif damage == "different completion time":
        turn["last_run"] += 1
    elif damage == "malformed run name":
        turn["name"] = []
    path.write_text(json.dumps(report))
    with pytest.raises(EvidenceError, match="failure-injection"):
        validate(evidence, artifact, "1.0.0")


@pytest.mark.parametrize("field", ["principal", "tenant_sha256", "internal_session",
                                   "created_unix", "updated_unix", "lease_owner", "lease_until"])
def test_schedule_comparison_requires_all_persisted_metadata(tmp_path, field):
    evidence, artifact, _ = _fixture(tmp_path)
    path = evidence / "failure-injection.json"
    report = json.loads(path.read_text())
    schedules = report["categories"]["full_disks"]["continuity"]["schedules"]
    for when in ("before", "after"):
        for row in schedules[when].values():
            del row[field]
    path.write_text(json.dumps(report))
    with pytest.raises(EvidenceError, match="schedule row is incomplete"):
        validate(evidence, artifact, "1.0.0")


def test_a_missing_candidate_artifact_fails_closed_by_name(tmp_path):
    """`artifact_sha256 = _sha256(artifact) if artifact.is_file() else None`
    let four bindings compare against None before anything complained, and the
    error that finally surfaced blamed the sign-off. A missing candidate is its
    own failure and must say so."""
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    for name in REQUIRED_REPORTS:
        (evidence / name).write_text("substantive placeholder " * 10)
    with pytest.raises(EvidenceError, match="candidate artifact is missing"):
        validate(evidence, tmp_path / "absent.tar.gz", "1.0.0")


@pytest.mark.parametrize("mutation,match", [
    ("absent", "missing or invalid"),
    ("tag", "tag must be"),
    ("file", "different artifact file"),
    ("sha256", "digest does not match"),
    ("asset", "published HTTPS asset"),
    ("rollback", "distinct published release"),
    ("drift", "does not name in rollback_from"),
])
def test_the_candidate_record_binds_the_artifact_and_its_rollback(
        tmp_path, mutation, match):
    """The gate verifies a PUBLISHED candidate; the record is what says which
    one. Without these bindings the gate would validate evidence against
    whatever archive it happened to be handed, and a rollback drill could prove
    recovery to a release this candidate never promises."""
    evidence, artifact, _ = _fixture(tmp_path)
    path = evidence / "candidate.json"
    record = json.loads(path.read_text())
    if mutation == "absent":
        path.unlink()
    elif mutation == "tag":
        record["tag"] = "release-1.0.0"
    elif mutation == "file":
        record["file"] = "sigil-pi-1.0.0-other.tar.gz"
    elif mutation == "sha256":
        record["sha256"] = "f" * 64
    elif mutation == "asset":
        record["release_asset"] = "https://example.invalid/some/other/path.tar.gz"
    elif mutation == "rollback":
        record["rollback_from"] = {"version": "0.9.0", "sha256": record["sha256"]}
    elif mutation == "drift":
        record["rollback_from"] = {"version": "0.8.0", "sha256": "e" * 64}
    if mutation != "absent":
        path.write_text(json.dumps(record))
    with pytest.raises(EvidenceError, match=match):
        validate(evidence, artifact, "1.0.0")


DELETE = object()
TURNS = ("clean_install", "restore_old", "upgrade_new", "rollback_old")
WHILE_BACKING_UP = ("online_backup", "turns_while_backing_up")
ONLINE = ("online_backup", "phases")
# What the recovery drill shows since H2 and H5, each as changes to a report
# that is otherwise valid, and the refusal each must meet. A path names keys,
# or a phase by its name.
RECOVERY_RECHECKS = [
    # H2: every phase runs a turn in the session it restored, and recovery is
    # measured to that turn. The drill before H2 started each release, waited
    # for readiness and ran nothing in it, and its report still validated.
    ("a boot-only report from before H2",
     [(("phases", name, "turn"), DELETE) for name in TURNS]
     + [(("evaluation", "measured_to"), DELETE)],
     "clean_install records no turn"),
    ("a phase without its turn", [(("phases", "upgrade_new", "turn"), DELETE)],
     "upgrade_new records no turn"),
    ("a count that is not a count",
     [(("phases", "restore_old", "turn", "messages_after"), True)],
     "restore_old records no turn"),
    ("recovery measured to readiness", [(("evaluation", "measured_to"), DELETE)],
     "first successful turn in the restored session"),
    ("recovery measured to something else", [(("evaluation", "measured_to"), "ready")],
     "first successful turn in the restored session"),
    ("a clean install that found the session",
     [(("phases", "clean_install", "turn", "read_before_turn"), True)],
     "clean_install found a session on a clean install"),
    ("a phase that did not read the session",
     [(("phases", "rollback_old", "turn", "read_before_turn"), False)],
     "rollback_old did not read the committed session"),
    ("the candidate read an empty session",
     [(("phases", "upgrade_new", "turn", key), value) for key, value in (
         ("messages_before", 0), ("history_messages_sent", 0), ("messages_after", 2),
         ("audit_records_before", 0), ("audit_records_after", 2))],
     "upgrade_new did not read the committed session"),
    ("the provider was sent none of the session",
     [(("phases", "restore_old", "turn", "history_messages_sent"), 0)],
     "restore_old sent the provider none of the session"),
    # Review R1 (2026-11-17): a count of one or more passed, so a truncated
    # history did too. All the session held is what a turn sends.
    ("the provider was sent part of the session",
     [(("phases", "restore_old", "turn", "history_messages_sent"), 1)],
     r"restore_old sent the provider 1 message\(s\) of history where the session held 2"),
    ("the provider was sent more than the session",
     [(("phases", "rollback_old", "turn", "history_messages_sent"), 7)],
     r"rollback_old sent the provider 7 message\(s\) of history where the session held 6"),
    ("a clean install sent the provider a history",
     [(("phases", "clean_install", "turn", "history_messages_sent"), 2)],
     r"clean_install sent the provider 2 message\(s\) of history where the session held 0"),
    ("a turn that was not committed",
     [(("phases", "upgrade_new", "turn", "messages_after"), 5)],
     "upgrade_new did not commit its turn"),
    ("a turn that left no audit record",
     [(("phases", "upgrade_new", "turn", "audit_records_after"), 4)],
     "upgrade_new did not commit its turn"),
    ("a phase that failed", [(("phases", "upgrade_new", "failed"), True)],
     "upgrade_new failed"),
    # H5: the candidate's own tool backs the state up while the candidate
    # serves turns, and a start restored from that backup continues the
    # session. The drill before H5 restored only a backup taken offline.
    ("no online backup, from before H5", [(("online_backup",), DELETE)],
     "no online backup taken while the candidate served turns"),
    ("an offline backup", [(("online_backup", "mode"), "snapshot")],
     "no online backup taken while the candidate served turns"),
    ("backed up by another release",
     [(("online_backup", "release", "sha256"), "b" * 64)],
     "online backup was not taken by the exact candidate"),
    ("a backup older than the RPO", [(("online_backup", "age_at_restore_seconds"), 901)],
     "online backup age at restore does not prove the RPO"),
    ("a backup age from the future", [(("online_backup", "age_at_restore_seconds"), -1)],
     "online backup age at restore does not prove the RPO"),
    ("a backup age that is not a number",
     [(("online_backup", "age_at_restore_seconds"), True)],
     "online backup age at restore does not prove the RPO"),
    ("no longest lock hold", [(("online_backup", "max_session_lock_hold_ms"), DELETE)],
     "longest session lock hold"),
    ("a lock hold that is not a number",
     [(("online_backup", "max_session_lock_hold_ms"), True)],
     "longest session lock hold"),
    ("a negative lock hold", [(("online_backup", "max_session_lock_hold_ms"), -0.5)],
     "longest session lock hold"),
    ("an online restore slower than the RTO",
     [(("online_backup", "restore_rto_seconds"), 4 * 3600 + 1)],
     "restore from the online backup does not prove the RTO"),
    ("an online restore time that is not a number",
     [(("online_backup", "restore_rto_seconds"), None)],
     "restore from the online backup does not prove the RTO"),
    ("an online restore measured to readiness",
     [(("online_backup", "measured_to"), DELETE)],
     "restore from the online backup is not measured to the first successful turn"),
    ("one turn while backing up",
     [((*WHILE_BACKING_UP, "turns"), 1), ((*WHILE_BACKING_UP, "answered"), 1)],
     "online backup was not taken while turns ran"),
    ("a turn left unanswered", [((*WHILE_BACKING_UP, "answered"), 1)],
     "online backup was not taken while turns ran"),
    ("no turn in flight when the backup started",
     [((*WHILE_BACKING_UP, "in_flight_when_backup_started"), False)],
     "online backup was not taken while turns ran"),
    ("no turn overlapping the backup", [((*WHILE_BACKING_UP, "overlapping_backup"), 0)],
     "online backup was not taken while turns ran"),
    ("an archive holding none of those turns", [((*WHILE_BACKING_UP, "held_by_backup"), 0)],
     "online backup was not taken while turns ran"),
    ("no record of the turns", [(WHILE_BACKING_UP, DELETE)],
     "online backup was not taken while turns ran"),
    ("an online backup that lost a session",
     [(("online_backup", "continuity", "preserved"), False)],
     "online backup did not preserve the committed sessions"),
    ("an online backup that changed a file",
     [(("online_backup", "continuity", "lost_or_changed"), ["tenants/t/sessions/s.json"])],
     "online backup did not preserve the committed sessions"),
    ("an online phase missing", [((*ONLINE, "restore_online_new"), DELETE)],
     "online backup phases are incomplete or out of order"),
    ("online phases out of order",
     [(ONLINE, [{"name": "restore_online_new"}, {"name": "backup_online_new"}])],
     "online backup phases are incomplete or out of order"),
    ("an online phase of the old release",
     [((*ONLINE, "backup_online_new", "artifact_sha256"), "b" * 64)],
     "online backup phase is not bound to the candidate"),
    ("an online phase that failed", [((*ONLINE, "restore_online_new", "failed"), True)],
     "online backup phase is not bound to the candidate"),
    ("an online phase never ready", [((*ONLINE, "restore_online_new", "ready_seconds"), DELETE)],
     "online backup phase is not bound to the candidate"),
    ("an online phase without its turn", [((*ONLINE, "restore_online_new", "turn"), DELETE)],
     "restore_online_new records no turn"),
    ("an online phase that did not read the session",
     [((*ONLINE, "backup_online_new", "turn", "read_before_turn"), False)],
     "backup_online_new did not read the committed session"),
    ("a restore that read another session than the backup holds",
     [((*ONLINE, "restore_online_new", "turn", key), value) for key, value in (
         ("messages_before", 10), ("history_messages_sent", 10), ("messages_after", 12))],
     "did not read the session the backup holds"),
]


def _change(report, path, value):
    """Set or delete what ``path`` names in ``report``."""
    target = report
    for key in path[:-1]:
        target = (next(item for item in target if item.get("name") == key)
                  if isinstance(target, list) else target[key])
    last = path[-1]
    if isinstance(target, list):
        index = next(n for n, item in enumerate(target) if item.get("name") == last)
        if value is DELETE:
            del target[index]
        else:
            target[index] = value
    elif value is DELETE:
        del target[last]
    else:
        target[last] = value


@pytest.mark.parametrize("changes,match", [case[1:] for case in RECOVERY_RECHECKS],
                         ids=[case[0] for case in RECOVERY_RECHECKS])
def test_recovery_evidence_shows_a_turn_in_every_start_and_an_online_backup(
        tmp_path, changes, match):
    """H2's sweep and H5's notes for this validator: the report must show
    what the drill now proves, or a report from the drill before them, or one
    edited to claim it, passes the gate."""
    evidence, artifact, _ = _fixture(tmp_path)
    path = evidence / "recovery-drill.json"
    report = json.loads(path.read_text())
    for where, value in changes:
        _change(report, where, value)
    path.write_text(json.dumps(report))
    with pytest.raises(EvidenceError, match=match):
        validate(evidence, artifact, "1.0.0")
