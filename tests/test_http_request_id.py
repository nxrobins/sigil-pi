"""Actual pinned SIGIL correlation policy versus the legacy reference contract."""
import pytest

from conftest import PI_ROOT, SIGIL_ROOT, forge_ok, needs_toolchain
from http_compat_support import FRESH, INVALID, VALID, compose_request_id, legacy_request_id, record, request_facts
from turn_support import FUEL


@pytest.fixture(scope="module")
def request_id_source():
    needs_toolchain()
    source = compose_request_id(PI_ROOT, SIGIL_ROOT)
    assert len(source.encode()) <= 65536
    return source


@pytest.mark.parametrize("hints", [[value] for value in VALID + INVALID] + [
    [], [b"", b"later-valid-id"], [b"first-valid-id", b"second-valid-id"],
    [b"first-invalid/", b"later-valid-id"], [b"x" * 65536], [b"valid-id"] * 64,
])
def test_actual_sigil_matches_legacy_correlation_without_conferring_authority(mcp, request_id_source, hints):
    expected = record("RI1\n", [legacy_request_id(hints)])
    assert forge_ok(mcp, request_id_source, request_facts(hints), fuel=FUEL) == expected


@pytest.mark.parametrize("raw", [
    "", "{}", record("RF2\n", [FRESH, "0", ""]), request_facts() + "trailing",
    record("RF1\n", [FRESH, "0"]), record("RF1\n", [FRESH, "0", "", "extra"]),
    record("RF1\n", ["", "0", ""]), record("RF1\n", ["a" * 31, "0", ""]),
    record("RF1\n", ["a" * 33, "0", ""]), record("RF1\n", ["A" * 32, "0", ""]),
    record("RF1\n", ["g" * 32, "0", ""]), record("RF1\n", [FRESH, "00", ""]),
    record("RF1\n", [FRESH, "-1", ""]), record("RF1\n", [FRESH, "65", ""]),
    record("RF1\n", [FRESH, "1.0", ""]), record("RF1\n", [FRESH, " 1", ""]),
    record("RF1\n", [FRESH, "0", "61"]), record("RF1\n", [FRESH, "1", "a"]),
    record("RF1\n", [FRESH, "1", "AA"]), record("RF1\n", [FRESH, "1", "gg"]),
    record("RF1\n", [FRESH, "1", "61" * 65537]),
])
def test_actual_sigil_refuses_malformed_native_metadata_instead_of_making_it_a_hint(mcp, request_id_source, raw):
    result = mcp.forge(request_id_source, input=raw, fuel=FUEL)
    assert result["status"] == "error", result
    diagnostic = result["diagnostics"][0]
    assert diagnostic["code"] == "R803", diagnostic
    assert diagnostic["message"] == "tool trapped: tool returned error (400)", diagnostic


def test_actual_sigil_correlation_is_deterministic_for_the_same_facts(mcp, request_id_source):
    raw = request_facts([b"invalid/value"])
    expected = record("RI1\n", [FRESH])
    assert forge_ok(mcp, request_id_source, raw, fuel=FUEL) == expected
    assert forge_ok(mcp, request_id_source, raw, fuel=FUEL) == expected
