"""Required actual pinned SIGIL projection versus the product reference.

Draft suite: do not call the Python-only oracle checks an execution pass.
This is not route admission, real dependency probing, or migrated readiness.
"""

import json

import pytest

from conftest import forge_ok, needs_toolchain
from readiness_support import NAMES, incoming, legacy, source
from turn_support import FUEL, fields, record, refused


@pytest.fixture(scope="module")
def program():
    needs_toolchain()
    return source().text


def compare(mcp, program, **kwargs):
    expected_status, expected_body, _ = legacy(**kwargs)
    result = fields(forge_ok(mcp, program, incoming(**kwargs), fuel=FUEL), "RY1\n", 2)
    assert result[0] == str(expected_status)
    assert json.loads(result[1]) == expected_body
    assert len(result[1].encode()) <= 512


@pytest.mark.parametrize("draining,failed", [(0, 0), (1, 0), (0, 1), (1, 1)])
def test_actual_readiness_policy_matches_every_health_combination(mcp, program, draining, failed):
    for healthy in range(64):
        compare(mcp, program, healthy=healthy, draining=draining, failed=failed)


def test_actual_readiness_policy_preserves_optional_dependencies(mcp, program):
    for configured in range(64):
        if configured & 8:
            for healthy in (0, configured):
                compare(mcp, program, configured=configured, healthy=healthy)


@pytest.mark.parametrize("name", NAMES)
def test_actual_projection_uses_false_for_a_failed_probe_without_echoing_diagnostics(mcp, program, name):
    expected_status, expected_body, _ = legacy(raises=name)
    healthy = 63 ^ (1 << NAMES.index(name))
    result = fields(forge_ok(mcp, program, incoming(healthy=healthy), fuel=FUEL), "RY1\n", 2)
    assert result[0] == str(expected_status) and json.loads(result[1]) == expected_body
    assert "private" not in result[1]


@pytest.mark.parametrize("label", ["01234567", "a" * 64, ".:_-AB09", "request-1234"])
def test_actual_projection_preserves_the_already_selected_correlation_label(mcp, program, label):
    compare(mcp, program, label=label)


@pytest.mark.parametrize("raw", ["", "{}", incoming() + "x", "x" * 257,
    record("RQ2\n", ["request-1234", "0", "0", "63", "63"]),
    record("RQ1\n", ["request-1234", "0", "0", "63"]),
    record("RQ1\n", ["request-1234", "0", "0", "63", "63", "extra"]),
    *[incoming(label=value) for value in ("", "short", "a" * 65, "bad/label", "bad\nlabel", "café-label", 'quote"label')],
    *[incoming(draining=value) for value in (-1, 2, "00", "true", "1.0")],
    *[incoming(failed=value) for value in (-1, 2, "00", "true", "1.0")],
    *[incoming(configured=value) for value in (0, 1, 7, 64, "063", -1)],
    *[incoming(healthy=value) for value in (64, "063", -1)],
    incoming(configured=8, healthy=63), incoming(configured=8, healthy=8, failed=1)])
def test_actual_projection_rejects_malformed_or_inconsistent_bound_snapshots(mcp, program, raw):
    refused(mcp, program, raw, 400)
