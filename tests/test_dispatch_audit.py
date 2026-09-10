"""Compiled dispatch-evidence projection; synthetic facts, not native provenance."""
import json

import pytest

from conftest import forge_ok
from test_effect_audit import effect_audit_source as effect_audit_source, observation, project
from test_transaction_audit import manifest
from turn_support import FUEL, fields, record


def dispatch():
    prepared = fields(observation()[15], "WP1\n", 5)
    return [manifest("1" * 64), "2" * 64, "3" * 64, "1024", "4" * 64,
            "200", "5" * 64, "6", "90", "91", "200", "14900", "provider",
            prepared[0], prepared[4], "7" * 64]


def with_dispatch(values=None, policy=None):
    values = observation() if values is None else values
    wp = fields(values[15], "WP1\n", 5)
    values[15] = record("WP2\n", wp + [record("DP1\n", dispatch() if policy is None else policy)])
    return values


@pytest.mark.parametrize("kind", ["prepared", "observed", "refused"])
def test_dispatch_metadata_is_retained_without_claiming_execution_success(mcp, effect_audit_source, kind):
    event = project(mcp, effect_audit_source, with_dispatch(observation(kind)))
    assert event["dispatch_policy"] == {
        "source": "1" * 64, "runtime": "d" * 64,
        "bound_config_sha256": "2" * 64, "input_sha256": "3" * 64, "input_bytes": 1024,
        "output_sha256": "4" * 64, "output_bytes": 200,
        "read_set_sha256": "5" * 64, "read_count": 6,
        "input_clock": 90, "after_clock": 91, "elapsed_ms": 200,
        "selected_fuel": FUEL, "selected_timeout_ms": 14900,
        "alias": "provider", "context_sha256": "7" * 64,
    }
    assert event["prepared"]["input_sha256"] == dispatch()[13]
    assert not {"authorized", "business_success", "delivered", "credential", "input", "output"} & event["dispatch_policy"].keys()


@pytest.mark.parametrize("index,value", [
    (0, "unapproved manifest"), (1, "raw config"), (2, "input"), (3, "4194305"),
    (3, "3"), (4, "output"), (5, "2097153"), (5, "3"), (6, "raw snapshot"),
    (7, "0"), (7, "33"), (8, "090"), (9, "89"), (10, "30001"),
    (11, "0"), (11, "15001"), (12, "bad/alias"), (12, "x" * 65),
    (13, "e" * 64), (14, record("TG1\n", ["1", "200"])), (15, "raw context"),
])
def test_invalid_or_misbound_dispatch_evidence_is_rejected(mcp, effect_audit_source, index, value):
    policy = dispatch()
    policy[index] = value
    result = mcp.forge(effect_audit_source, input=record("WA1\n", with_dispatch(policy=policy)), fuel=FUEL)
    assert result["status"] == "error" and not result.get("data", {}).get("output_text")


@pytest.mark.parametrize("kind", ["prepared", "observed", "abandoned"])
def test_original_manual_and_abandoned_paths_do_not_invent_dispatch_policy(mcp, effect_audit_source, kind):
    event = project(mcp, effect_audit_source, observation(kind))
    assert event["dispatch_policy"] is None


def test_recovery_cannot_substitute_current_dispatch_evidence_for_lost_history(mcp, effect_audit_source):
    values = observation("abandoned")
    values[15] = with_dispatch()[15]
    result = mcp.forge(effect_audit_source, input=record("WA1\n", values), fuel=FUEL)
    assert result["status"] == "error" and not result.get("data", {}).get("output_text")


@pytest.mark.parametrize("which", ["wp_extra", "wp_missing", "dp_extra", "dp_missing"])
def test_dispatch_frames_require_exact_versioned_field_counts(mcp, effect_audit_source, which):
    values = with_dispatch()
    wp = fields(values[15], "WP2\n", 6)
    if which.startswith("dp"):
        policy = dispatch() + (["extra"] if which == "dp_extra" else [])
        if which == "dp_missing": policy.pop()
        wp[5] = record("DP1\n", policy)
    elif which == "wp_extra": wp.append("extra")
    else: wp.pop()
    values[15] = record("WP2\n", wp)
    result = mcp.forge(effect_audit_source, input=record("WA1\n", values), fuel=FUEL)
    assert result["status"] == "error" and not result.get("data", {}).get("output_text")


def test_policy_output_remains_bounded_and_contains_no_raw_authority(mcp, effect_audit_source):
    output = forge_ok(mcp, effect_audit_source, record("WA1\n", with_dispatch()), fuel=FUEL)
    kind, payload = fields(output, "AR1\n", 2)
    assert kind == "publish" and len(payload.encode()) <= 16384
    assert json.loads(payload)["schema"] == "sigil-pi/worker-lifecycle-audit/v1"
    assert "DP1" not in payload and "TG1" not in payload and "WP2" not in payload
