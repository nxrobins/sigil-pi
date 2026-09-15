"""Actual shared SIGIL codec conformance; not HTTP or request-policy parity."""

import hashlib
import json

import pytest

from conftest import forge_ok, mcp as mcp, needs_toolchain
from readset_support import ROOT, compose_probe, incoming, observed
from turn_support import FUEL, record, refused


@pytest.fixture(scope="module")
def program():
    needs_toolchain()
    return compose_probe().text


def test_readset_probe_has_one_entry_and_fits_the_unchanged_source_limit():
    built = compose_probe()
    assert 0 < len(built.text.encode()) <= 65536
    assert built.text.count("pub fn tool_main(") == 1
    assert built.text.count("fn readset_query(") == 1
    assert built.text.count("fn readset_rows(") == 1


def test_probe_recipe_binds_the_actual_codec_and_reference_inputs():
    first, second = compose_probe(), compose_probe()
    assert first == second
    assert set(first.input_hashes) == {
        "app/shared/record_helpers.sigil", "app/shared/utf8.sigil",
        "app/shared/store_protocol.sigil", "app/shared/read_set.sigil",
        "tests/fixtures/readset_probe.sigil", "tests/readset_support.py",
    }
    for relative in ("app/shared/read_set.sigil", "tests/fixtures/readset_probe.sigil",
                     "tests/readset_support.py"):
        assert first.input_hashes[relative] == hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
    assert first.compiler_input_sha256 == hashlib.sha256(first.text.encode()).hexdigest()


@pytest.mark.parametrize("keys", [
    (("app", "one"),),
    (("app", "one"), ("other", "one")),
    (("a" * 128, "k" * 256), ("other", "one"), ("third._:-9", "key._:-9")),
])
def test_sigil_query_selects_exact_addresses_without_adding_authority(mcp, program, keys):
    raw = forge_ok(mcp, program, incoming(keys=keys, mode="query"), fuel=FUEL)
    assert len(raw.encode()) <= 4096
    assert raw == json.dumps([{"namespace": ns, "key": key} for ns, key in keys],
                            separators=(",", ":"))


@pytest.mark.parametrize("keys,count", [
    ((), None), ((("app", "one"),), "01"), ((("app", "one"),), "4"),
    ((("app", "one"),), "-1"), ((("", "one"),), None),
    ((("app", ""),), None), ((("../app", "one"),), None),
    ((("app", "é"),), None), ((("a" * 129, "one"),), None),
    ((("app", "k" * 257),), None),
    ((("app", "one"), ("app", "one")), None),
    ((("app", "one"), ("other", "two")), "1"),
])
def test_sigil_refuses_invalid_or_ambiguous_expected_addresses(mcp, program, keys, count):
    refused(mcp, program, incoming(keys=keys, count=count, mode="query"), 400)


@pytest.mark.parametrize("revision,presence,value", [
    ("0", "0", ""), ("1", "1", ""), ("2", "0", ""),
    ("9223372036854775807", "1", "retained"),
    ("9", "1", "é😀\nRR1\n00000004data\0"),
])
def test_sigil_preserves_missing_empty_tombstone_full_revision_and_opaque_bytes(
        mcp, program, revision, presence, value):
    raw = observed([("app", "one", revision, presence, value)])
    assert forge_ok(mcp, program, incoming(raw), fuel=FUEL) == raw


def test_sigil_matches_all_three_coordinates_in_caller_order(mcp, program):
    rows = [("app", "one", "1", "1", "first"),
            ("other", "one", "2", "0", ""),
            ("app", "three", "0", "0", "")]
    keys = tuple(row[:2] for row in rows)
    raw = observed(rows)
    assert forge_ok(mcp, program, incoming(raw, keys=keys), fuel=FUEL) == raw
    for changed in ([rows[1], rows[0], rows[2]], [rows[0], rows[0], rows[2]]):
        refused(mcp, program, incoming(observed(changed), keys=keys), 400)


@pytest.mark.parametrize("index,replacement", [
    (0, "foreign"), (1, "different"),
    (2, ""), (2, "-1"), (2, "01"), (2, "1.0"), (2, "1e1"),
    (2, "9223372036854775808"), (2, "99999999999999999999"),
    (2, "0"), (3, ""), (3, "true"), (3, "01"), (3, "2"),
    (3, "0"),
])
def test_sigil_refuses_wrong_coordinates_and_impossible_observation_shapes(
        mcp, program, index, replacement):
    row = ["app", "one", "1", "1", "payload"]
    row[index] = replacement
    refused(mcp, program, incoming(observed([row])), 400)


def test_explicit_store_failure_is_distinct_from_a_malformed_or_partial_response(mcp, program):
    refused(mcp, program, incoming(record("RM1\n", ["error", "0", ""])), 503)
    for raw in (
            record("RM1\n", ["error", "1", ""]),
            record("RM1\n", ["error", "00", ""]),
            record("RM1\n", ["error", "0", "partial-prefix"]),
            record("RM1\n", ["denied", "0", ""]),
            record("RM1\n", ["ok", "1", ""])):
        refused(mcp, program, incoming(raw), 400)


@pytest.mark.parametrize("count", ["0", "2", "4", "01", "-1", "", "1.0"])
def test_observed_count_must_canonically_match_the_requested_count(mcp, program, count):
    refused(mcp, program, incoming(observed([("app", "one", "0", "0", "")], count=count)), 400)


def test_legacy_budget_marker_truncation_and_nonempty_unused_slots_are_rejected(mcp, program):
    row = ("app", "one", "1", "1", "retained")
    good = observed([row])
    bad = [observed([row], batch_marker="BR1\n"), good[:-1], good + "trailing",
           observed([row, ("other", "unused", "0", "0", "")], count="1")]
    for raw in bad:
        refused(mcp, program, incoming(raw), 400)


@pytest.mark.parametrize("extra", [0, 1])
def test_sigil_enforces_the_complete_two_mib_observation_bound(mcp, program, extra):
    overhead = len(observed([("app", "one", "1", "1", "")]).encode())
    value = "x" * (2097152 - overhead + extra)
    raw = observed([("app", "one", "1", "1", value)])
    assert len(raw.encode()) == 2097152 + extra
    if extra:
        refused(mcp, program, incoming(raw), 400)
    else:
        assert forge_ok(mcp, program, incoming(raw), fuel=FUEL) == raw
