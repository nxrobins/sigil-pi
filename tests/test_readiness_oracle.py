"""Exercise the existing product oracle; these are NOT SIGIL execution passes."""

import hashlib
import json

import pytest

from readiness_support import NAMES, ROOT, legacy, source


def test_draft_readiness_recipe_is_bounded_deterministic_and_byte_bound():
    built = source()
    assert built == source()
    assert len(built.text.encode()) <= 65536
    assert built.text.count("pub fn tool_main(") == 1
    assert len(built.input_hashes) == 6
    for name, digest in built.input_hashes.items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("draining,failed", [(0, 0), (1, 0), (0, 1), (1, 1)])
def test_reference_full_dependency_truth_table_and_emergency_override(draining, failed):
    for healthy in range(64):
        status, body, records = legacy(healthy=healthy, draining=draining, failed=failed)
        good = healthy == 63 and not draining and not failed
        expected = {name: bool(healthy & (1 << i)) for i, name in enumerate(NAMES)}
        if failed:
            expected["quota_store"] = False
        assert status == (200 if good else 503)
        assert body == {"status": "ready" if good else "not_ready", "dependencies": expected,
                        "draining": bool(draining), "request_id": "request-1234"}
        assert "private" not in json.dumps([body, records])


def test_reference_preserves_every_optional_dependency_combination():
    for configured in range(64):
        if not configured & 8:
            continue
        for healthy in (0, configured):
            status, body, _ = legacy(configured=configured, healthy=healthy)
            assert set(body["dependencies"]) == {name for i, name in enumerate(NAMES) if configured & (1 << i)}
            assert status == (200 if healthy == configured else 503)


@pytest.mark.parametrize("name", NAMES)
def test_reference_probe_exceptions_become_content_free_unhealthy_observations(name):
    status, body, records = legacy(raises=name)
    assert status == 503
    assert body["dependencies"] == {candidate: candidate != name for candidate in NAMES}
    assert "private" not in json.dumps([body, records])
