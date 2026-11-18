"""The evidence validator re-checks a load report's clients, its compiler-
replacement counter and its recorded configuration (audit 2026-11-10, F-18).

Before that audit the validator, like the harness, asked only that each
profile had more than zero requests: a report from a run whose clients had
ended early, or whose compiler had been replaced, validated. Each case here
changes one thing in an otherwise complete evidence set.
"""

import hashlib
import json
import time

import pytest

from scripts import check_readiness_evidence, load_test
from scripts.check_readiness_evidence import EvidenceError, validate
from test_readiness_evidence import _fixture


REPLACEMENTS = "runtime_unhealthy_replacements_total"


def _client(index, **changes):
    return lambda report: report["clients"]["records"][index].update(changes)


def _sample(index, value):
    return lambda report: report["resource_samples"][index].update({REPLACEMENTS: value})


def _configuration(**changes):
    return lambda report: report["service_configuration"].update(changes)


def _starved(report):
    # Client 7's requests go to client 9, so the profile totals still add up.
    records = report["clients"]["records"]
    records[7].update(requests=25, successes=25)
    records[9].update(requests=55, successes=55)


def _longer_run(report):
    report.update(planned_duration_seconds=7200, actual_wall_seconds=7201)


def _longer_run_with_its_floor(report):
    _longer_run(report)
    report["clients"]["request_floor"] = 53
    for record in report["clients"]["records"]:
        record.update(requests=60, successes=60)
    for profile in report["profiles"].values():
        profile.update(requests=1500, successes=1500)
        profile["latency"]["samples"] = profile["queue_wait"]["samples"] = 1500


def _validate(tmp_path, change):
    evidence, artifact, signoff = _fixture(tmp_path)
    path = evidence / "load-test.json"
    report = json.loads(path.read_text())
    change(report)
    path.write_text(json.dumps(report))
    return validate(evidence, artifact, "1.0.0") == signoff


@pytest.mark.parametrize("change,match", [
    (lambda report: report.pop("clients"), "does not record its load clients"),
    (lambda report: report["clients"].update(records={}),
     "does not record its load clients"),
    (lambda report: report["clients"]["records"].pop(),
     "one record for each of its 50 offered clients"),
    (_client(1, client=0), "one record for each of its 50 offered clients"),
    (lambda report: report["clients"]["records"].append(
        dict(report["clients"]["records"][0])),
     "one record for each of its 50 offered clients"),
    (lambda report: report["clients"]["records"].__setitem__(4, "client 4"),
     "one record for each of its 50 offered clients"),
    (_client(7, alive_at_deadline=False, stopped_by="AttributeError"),
     "client that was not alive at the deadline"),
    (_client(7, alive_at_deadline="yes"), "client that was not alive at the deadline"),
    (lambda report: report["clients"]["records"][7].pop("alive_at_deadline"),
     "client that was not alive at the deadline"),
    (_starved, "client below the per-client floor of 26 successful requests"),
    (_client(7, successes=25), "client below the per-client floor of 26 successful"),
    (_client(3, successes=41), "client whose request counts are invalid"),
    (_client(3, requests=True, successes=True), "client whose request counts are invalid"),
    (_client(3, requests=41, successes=41), "clients do not add up to its profiles"),
    (_client(2, profile="tool_using"), "clients do not add up to its profiles"),
    (lambda report: report["clients"].update(request_floor=25),
     "weakens the per-client floor of 26"),
    (lambda report: report["clients"].pop("request_floor"),
     "weakens the per-client floor of 26"),
    (_longer_run, "weakens the per-client floor of 53"),
], ids=[
    "no-client-section", "records-not-a-list", "a-client-unrecorded",
    "a-client-recorded-twice", "a-fifty-first-record", "a-record-that-is-not-one",
    "a-client-stopped-early",
    "alive-is-not-true", "alive-not-recorded", "a-client-below-the-floor",
    "enough-requests-too-few-successes",
    "more-successes-than-requests", "counts-are-not-numbers",
    "clients-do-not-add-up", "a-client-in-the-wrong-profile", "a-lowered-floor",
    "no-floor", "a-longer-run-on-the-hour's-floor",
])
def test_a_load_report_must_account_for_every_client(tmp_path, change, match):
    with pytest.raises(EvidenceError, match=match):
        _validate(tmp_path, change)


def test_the_floor_grows_with_the_planned_duration(tmp_path):
    assert check_readiness_evidence.LOAD_CLIENT_FLOOR_PERIOD_SECONDS == 135
    assert (check_readiness_evidence.LOAD_CLIENT_FLOOR_PERIOD_SECONDS
            == load_test.CLIENT_FLOOR_PERIOD_SECONDS)
    assert _validate(tmp_path, _longer_run_with_its_floor)


def _replaced_and_restarted(report):
    first, last = report["resource_samples"]
    middle = json.loads(json.dumps(first))
    middle[REPLACEMENTS] = 1
    report["resource_samples"] = [first, middle, last]


@pytest.mark.parametrize("change", [
    _sample(1, 1),
    _replaced_and_restarted,
    _sample(0, 3),
    lambda report: report["resource_samples"][1].pop(REPLACEMENTS),
    _sample(1, True),
    _sample(1, None),
    _sample(0, -1),
    lambda report: report["resource_samples"].__setitem__(1, "sample"),
], ids=["rose", "rose-mid-run-only", "fell", "absent", "a-bool", "null", "negative",
        "not-a-sample"])
def test_a_load_report_must_show_that_no_compiler_was_replaced(tmp_path, change):
    with pytest.raises(EvidenceError, match="compiler-replacement counter"):
        _validate(tmp_path, change)


@pytest.mark.parametrize("change", [
    lambda report: report.pop("service_configuration"),
    lambda report: report.update(service_configuration=None),
    _configuration(model=""),
    _configuration(model=None),
    _configuration(provider={}),
    _configuration(provider={"endpoint_host": " "}),
    _configuration(provider="provider.example"),
    _configuration(limits={}),
    _configuration(limits=["turn_deadline_seconds"]),
], ids=["absent", "null", "empty-model", "no-model", "no-provider-host",
        "blank-provider-host", "provider-not-a-record", "no-limits",
        "limits-not-a-record"])
def test_a_load_report_must_record_what_the_service_ran_with(tmp_path, change):
    with pytest.raises(EvidenceError,
                       match="provider, the model and the limits in force"):
        _validate(tmp_path, change)


@pytest.mark.parametrize("change", [
    lambda report: report["profiles"]["single_step"].update(incomplete_telemetry=1),
    lambda report: report["profiles"]["tool_using"].pop("incomplete_telemetry"),
], ids=["a-turn-without-telemetry", "telemetry-not-counted"])
def test_a_load_report_must_show_telemetry_on_every_turn(tmp_path, change):
    with pytest.raises(EvidenceError, match="incomplete_telemetry"):
        _validate(tmp_path, change)


def _resource(index, **changes):
    return lambda report: report["resource_samples"][index]["resources"].update(changes)


def _peak_in_the_middle(report):
    first, last = report["resource_samples"]
    middle = json.loads(json.dumps(first))
    middle["resources"]["open_fd_count"] += 1000
    report["resource_samples"] = [first, middle, last]


@pytest.mark.parametrize("change,match", [
    (_resource(1, memory_peak_bytes=100_000_000 + 2**30), "memory_growth_bytes"),
    (_resource(1, thread_count=8 + 65), "thread_growth"),
    (_peak_in_the_middle, "fd_growth"),
    (_resource(1, state_storage_bytes=1_000_000 + 11 * 2**30), "state_growth_bytes"),
    (_resource(1, audit_storage_bytes=100_000 + 2**30), "audit_growth_bytes"),
    (_resource(1, disk_free_bytes=199), "disk-free bound"),
    (lambda report: [sample["resources"].pop("disk_total_bytes")
                     for sample in report["resource_samples"]], "disk-free bound"),
    # Within the bound, but not what the report's own summary says.
    (_resource(1, thread_count=8 + 5), "thread_growth"),
    (_resource(1, disk_free_bytes=700), "disk-free bound"),
    (lambda report: report["resource_samples"][0]["resources"].pop("memory_peak_bytes"),
     "memory_growth_bytes"),
    (_resource(0, open_fd_count=None), "fd_growth"),
    (lambda report: report["resource_samples"][1].update(resources=None),
     "resource samples"),
], ids=["memory", "threads", "descriptors-mid-run", "state", "audit", "disk",
        "no-disk-size", "threads-not-as-summarised", "disk-not-as-summarised",
        "no-memory-baseline", "no-descriptor-baseline", "a-sample-without-resources"])
def test_a_load_reports_resource_bounds_are_recomputed_from_its_samples(
        tmp_path, change, match):
    """The harness's own summary of its samples used to be taken on trust,
    where its error rate and its queue percentile were recomputed."""
    with pytest.raises(EvidenceError, match=match):
        _validate(tmp_path, change)


# ── the harness and the validator agree on what a report is ─────────────────

class _Clock:
    """The harness's clock, real until ``compress`` and ``factor`` times
    faster afterwards, so that an hour's run takes a couple of seconds."""

    def __init__(self, factor):
        self._factor = factor
        self._rate = 1.0
        self._base = self._since = time.monotonic()

    def compress(self):
        self._base, self._since = self.monotonic(), time.monotonic()
        self._rate = self._factor

    def monotonic(self):
        return self._base + (time.monotonic() - self._since) * self._rate

    def sleep(self, seconds):
        time.sleep(seconds / self._rate)


def _harness_report(tmp_path, monkeypatch, digest):
    """A qualifying report from the real main(): 50 clients, 25 tenants, a
    planned hour. Only the service and the clock's speed are stand-ins."""
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({
        "ops_token": "ops-token",
        "tenants": [{"tenant": f"tenant-{number}", "chat_token": f"chat-{number}"}
                    for number in range(25)],
    }))
    credentials.chmod(0o600)
    clock = _Clock(1800)
    remembered = {}

    def served(origin, path, token, timeout, *, payload=None):
        key = (token, payload["session"])
        message = payload["message"]
        tool_calls = 1 if "list_dir" in message else 0
        if message.startswith("Remember this readiness marker"):
            remembered[key] = message.split(": ", 1)[1].split(".", 1)[0]
            reply, tool_calls = "READY", 0
        else:
            reply = remembered[key] if "readiness marker" in message else "OK"
        time.sleep(0.0005)
        return {"status": 200, "failure_class": "none", "latency_ms": 1.0,
                "queue_ms": 0.0, "tool_calls": tool_calls, "retries": 0,
                "reply": reply, "body": {"reply": reply}}

    def metrics(origin, token, timeout):
        clock.compress()            # the first call is the baseline: the run starts
        return {
            "resources": {
                "memory_peak_bytes": 100, "thread_count": 5, "open_fd_count": 8,
                "state_storage_bytes": 10, "audit_storage_bytes": 4,
                "disk_free_bytes": 900, "disk_total_bytes": 1000},
            "runtime_generation": 1, REPLACEMENTS: 0,
            "effective_settings": {
                "model": "named-model", "endpoint_host": "provider.example",
                "turn_deadline_seconds": 120, "max_concurrent_turns": 2},
        }

    monkeypatch.setattr(load_test, "time", clock)
    monkeypatch.setattr(load_test, "_request", served)
    monkeypatch.setattr(load_test, "_metrics", metrics)
    output = tmp_path / "harness-report.json"
    code = load_test.main([
        "--target", "http://127.0.0.1:8080", "--credentials", str(credentials),
        "--output", str(output), "--artifact-digest", digest, "--version", "1.0.0",
        "--forecast-approved-by", "Named owner", "--metrics-interval", "120"])
    report = json.loads(output.read_text())
    assert code == 0, report["evaluation"]["failures"]
    return report


def _absent(fixture, written, path="report"):
    """Paths the hand-written fixture has and the harness's report does not."""
    if isinstance(fixture, dict) and isinstance(written, dict):
        return [missing for key, value in fixture.items()
                for missing in ([f"{path}.{key}"] if key not in written
                                else _absent(value, written[key], f"{path}.{key}"))]
    if isinstance(fixture, list) and isinstance(written, list) and fixture and written:
        return _absent(fixture[0], written[0], f"{path}[]")
    return []


def test_a_report_the_harness_writes_is_one_the_validator_accepts(
        tmp_path, monkeypatch):
    """The validator shares no code with the harness, on purpose. Without
    this, a field one of them renamed would be found by the maintainer, after
    the hour the qualifying run takes."""
    evidence, artifact, signoff = _fixture(tmp_path)
    fixture = json.loads((evidence / "load-test.json").read_text())
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    report = _harness_report(tmp_path, monkeypatch, digest)
    assert report["qualification_eligible"] is True
    assert report["planned_duration_seconds"] == 3600
    assert report["clients"]["request_floor"] == 26
    assert len(report["clients"]["records"]) == 50
    (evidence / "load-test.json").write_text(json.dumps(report))
    assert validate(evidence, artifact, "1.0.0") == signoff
    # And the fixture every other evidence test starts from is shaped like it.
    assert _absent(fixture, report) == []
