"""Required compiled SIGIL checks. Draft: UNEXECUTED until the live source gate ends.

Wire observations below are test fixtures, not authority or real temporary-store
receipts. These tests alone cannot qualify an actual readiness endpoint.
"""

import json

import pytest

from api_support import credential
from conftest import forge_ok, needs_toolchain
from emergency_support import DOMAIN, NATIVE_ERRORS, cause, incoming, retained, snapshot, source, window
from product_service import FixedWindowRateLimiter
from turn_support import FUEL, fields, record, refused


@pytest.fixture(scope="module")
def program():
    needs_toolchain()
    return source().text


def proposal(mcp, program, **kwargs):
    return fields(forge_ok(mcp, program, incoming(**kwargs), fuel=FUEL), "EA1\n", 4)


@pytest.mark.parametrize("tenant,prefix", [("tenant-a", "a"), ("tenant-b", "b"), ("tenant Ω", "c")])
def test_admission_is_only_one_tenant_bound_temporary_mutation_with_credential_guard(mcp, program, tenant, prefix):
    row = credential(tenant=tenant, prefix=prefix, expires=200)
    result = proposal(mcp, program, row=row)
    assert result[0] == "admit" and result[3] == ""
    assert fields(result[2], "TG1\n", 2) == ["100", "200"]
    assert json.loads(result[1]) == {
        "namespace": prefix + ".budget", "key": "readiness-window", "revision": 0,
        "value": window([120_250_000_000], tenant=tenant),
    }


@pytest.mark.parametrize("marker", ["DR2\n", "DC2\n"])
@pytest.mark.parametrize("origin", ["precheck", "storage"])
@pytest.mark.parametrize("label", NATIVE_ERRORS)
def test_sigil_alone_classifies_the_exact_native_admission_failure(mcp, program, marker, origin, label):
    result = proposal(mcp, program, failed=cause(label, marker, origin=origin))
    if origin == "storage" and label in {"busy", "corrupt", "storage", "commit_uncertain", "reopen_required", "denied", "limit"}:
        assert result[0] == "admit"
    else:
        assert result == ["unavailable", "", "", ""]


@pytest.mark.parametrize("delta", [0, 0.25, 1, 1.25, 59, 59.75, 60, 60.25])
def test_exact_rolling_window_and_retry_guidance_match_the_actual_reference(mcp, program, delta):
    clock = [120.25]
    reference = FixedWindowRateLimiter(1, clock=lambda: clock[0])
    assert reference.admit("tenant-a") == (True, 0)
    clock[0] += delta
    admitted, retry = reference.admit("tenant-a")
    now = int(clock[0] * 1_000_000_000)
    result = proposal(mcp, program, limit=1, clock_ns=now,
                      observed=snapshot(revision=7, events=[120_250_000_000]))
    if admitted:
        write = json.loads(result[1])
        assert result[0] == "admit" and result[3] == ""
        assert write["revision"] == 7
        assert retained(write["value"]) == ("tenant-a", DOMAIN, [now])
    else:
        assert result == ["limited", "", "", str(retry)]


def test_a_long_trace_preserves_each_real_reference_event_without_epoch_resets(mcp, program):
    clock = [0.0]
    reference = FixedWindowRateLimiter(2, clock=lambda: clock[0])
    seen, revision = snapshot(), 0
    for at in [0, 0, 0, 0.25, 59.75, 60, 60, 60.25, 119.75, 120, 120.25, 180.25, 181, 1000]:
        clock[0] = at
        admitted, retry = reference.admit("tenant-a")
        result = proposal(mcp, program, clock_ns=int(at * 1_000_000_000), observed=seen)
        if admitted:
            assert result[0] == "admit" and result[3] == ""
            write = json.loads(result[1])
            assert write["revision"] == revision
            tenant, domain, events = retained(write["value"])
            assert (tenant, domain) == ("tenant-a", DOMAIN)
            assert events == [int(value * 1_000_000_000) for value in reference._events["tenant-a"]]
            # Pure-policy round-trip fixture only. This is not a native receipt or
            # evidence that a guest can construct the host's actual observation.
            revision += 1
            seen = snapshot(revision=revision, raw=write["value"])
        else:
            assert result == ["limited", "", "", str(retry)]


@pytest.mark.parametrize("clock_ns,events,retry", [
    (60_000_000_000, [59_750_000_000], "60"),
    (159_750_000_000, [100_000_000_000, 110_000_000_000], "1"),
    (9223372036854775807, [9223372036854775807], "61"),
    (9223372036854775807, [9223372036854775806], "60"),
])
def test_exact_ns_boundaries_and_large_integers_do_not_round_or_overflow(mcp, program, clock_ns, events, retry):
    assert proposal(mcp, program, limit=len(events), clock_ns=clock_ns,
                    observed=snapshot(revision=1, events=events)) == ["limited", "", "", retry]


@pytest.mark.parametrize("change,expected", [
    ({"wall": 99}, "expired"), ({"wall": 200, "row": credential(expires=200)}, "expired"),
    ({"method": "POST"}, "not_applicable"), ({"path": "/v1/health"}, "not_applicable"),
    ({"path": "/v1/ready?x=1"}, "not_applicable"),
    ({"row": credential(scopes=["chat"])}, "forbidden"),
    ({"failed": cause("denied", origin="precheck")}, "unavailable"),
])
def test_refused_policy_does_not_interpret_or_charge_a_temporary_window(mcp, program, change, expected):
    assert proposal(mcp, program, observed="unread-private-data", clock="unread-clock", **change) == [expected, "", "", ""]


@pytest.mark.parametrize("label", NATIVE_ERRORS)
def test_temporary_mechanism_failures_never_reset_accounting_or_claim_admission(mcp, program, label):
    seen = snapshot(status="error", error=label)
    assert proposal(mcp, program, observed=seen) == ["unavailable", "", "", ""]


@pytest.mark.parametrize("change", [
    {"limit": 0}, {"limit": -1}, {"limit": "01"}, {"limit": "1.0"}, {"limit": 9223372036854775808},
    {"wall": -1}, {"wall": "0150"}, {"wall": 9007199254740001},
    {"clock_ns": -1}, {"clock_ns": "01"}, {"clock_ns": "0.25"}, {"clock_ns": 9223372036854775808},
    {"domain": "c" * 63}, {"domain": "c" * 65}, {"domain": "C" * 64}, {"domain": "g" * 64},
    {"clock": record("MC1\n", [DOMAIN, "0", "extra"])},
    {"clock": record("MC1\n", [DOMAIN, "0"]) + "trailing"},
])
def test_malformed_limits_or_clock_facts_do_not_produce_a_mutation(mcp, program, change):
    refused(mcp, program, incoming(**change), 400)


@pytest.mark.parametrize("failed", [
    cause("unknown"), cause("private diagnostic"), cause(marker="SC1\n"),
    record("DR2\n", ["ok", "0", "0", "", "", ""]),
    record("DR2\n", ["error", "1", "0", "", "storage", "storage"]),
    record("DR2\n", ["error", "0", "1", "private-prefix", "storage", "storage"]),
    record("DC2\n", ["ok", "1", "", ""]), record("DC2\n", ["error", "1", "storage", "storage"]),
    cause() + "extra", "x" * 129,
    cause(origin=""), cause(origin="unknown"), cause(origin="precheck\n"),
    record("DR2\n", ["error", "0", "0", "", "storage"]),
    record("DC2\n", ["error", "0", "storage"]),
    record("DR1\n", ["error", "0", "0", "", "storage"]),
    record("DC1\n", ["error", "0", "storage"]),
], ids=["unknown-error", "diagnostic-not-error", "legacy-receipt", "successful-read",
        "failed-read-revision", "failed-read-payload", "successful-commit",
        "failed-commit-revision", "trailing-data", "oversize-failure",
        "empty-origin", "unknown-origin", "noncanonical-origin", "missing-read-origin",
        "missing-commit-origin", "old-read-draft", "old-commit-draft"])
def test_only_canonical_actual_failure_layouts_can_authorize_the_emergency_proposal(mcp, program, failed):
    refused(mcp, program, incoming(failed=failed), 400)


@pytest.mark.parametrize("seen", [
    snapshot(revision=0, presence=1), snapshot(revision=0, raw="forged"),
    snapshot(revision=1, raw=""), snapshot(revision=1, presence=0),
    snapshot(revision="01"), snapshot(revision=-1), snapshot(revision=9223372036854775808),
    snapshot(revision=1, tenant="other-tenant"), snapshot(revision=1, domain="BAD"),
    snapshot(revision=1, events=[]), snapshot(revision=1, events=["01"]),
    snapshot(revision=1, events=[-1]), snapshot(revision=1, events=[9223372036854775808]),
    snapshot(revision=1, events=[110_000_000_000, 100_000_000_000]),
    snapshot(error="storage"), snapshot(status="unknown"),
    snapshot(status="error", error="unknown"), snapshot(status="error", error="storage", raw="prefix"),
    snapshot(status="error", error="storage", revision=1),
    snapshot(revision=1, raw="x" * 65537),
    snapshot(revision=1, raw=record("EW1\n", ["tenant-a", DOMAIN, "1", record("ET1\n", ["0", "0"])])),
    snapshot(revision=1, raw=record("EW1\n", ["tenant-a", DOMAIN, "7282", record("ET1\n", [])])),
    record("SR1\n", ["ok", "0", ""]), snapshot() + "extra",
], ids=["missing-present", "missing-payload", "empty-retained", "tombstone",
        "padded-revision", "negative-revision", "overflow-revision", "foreign-tenant",
        "invalid-domain", "empty-events", "padded-event", "negative-event", "overflow-event",
        "unsorted-events", "success-with-error", "unknown-status", "unknown-error",
        "failure-payload", "failure-revision", "oversize-cell", "extra-events",
        "count-bound", "legacy-read", "trailing-data"])
def test_malformed_foreign_or_tombstoned_windows_cannot_be_treated_as_fresh(mcp, program, seen):
    refused(mcp, program, incoming(observed=seen), 400)


@pytest.mark.parametrize("seen", [
    snapshot(revision=1, domain="d" * 64), snapshot(revision=1, events=[120_250_000_001]),
], ids=["changed-domain", "future-event"])
def test_changed_clock_domain_and_backward_clock_observations_refuse_without_reset(mcp, program, seen):
    assert proposal(mcp, program, observed=seen) == ["clock", "", "", ""]


def test_native_record_revision_exhaustion_is_not_a_quota_refusal_or_reset(mcp, program):
    assert proposal(mcp, program, limit=3, observed=snapshot(revision=9223372036854775807)) == ["capacity", "", "", ""]


def test_full_temporary_cell_cannot_be_evicted_to_make_room_for_another_event(mcp, program):
    overhead = len(window([]).encode()) + 3  # four count digits rather than one
    count = (65536 - overhead) // 9
    value = window([0] * count)
    assert len(value.encode()) <= 65536 < len(window([0] * (count + 1)).encode())
    assert proposal(mcp, program, limit=9223372036854775807, clock_ns=0,
                    observed=snapshot(revision=1, raw=value)) == ["capacity", "", "", ""]


def test_fully_expired_events_are_removed_before_appending_one_current_observation(mcp, program):
    result = proposal(mcp, program, clock_ns=180_000_000_000,
                      observed=snapshot(revision=9, events=[0, 100_000_000_000, 120_000_000_000]))
    assert result[0] == "admit"
    write = json.loads(result[1])
    assert write["revision"] == 9
    assert retained(write["value"]) == ("tenant-a", DOMAIN, [180_000_000_000])
