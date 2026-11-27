"""Pure listing policy; synthetic metadata fixtures grant no authority or I/O."""
import json

import pytest

from conftest import SIGIL_ROOT, forge_ok, needs_toolchain
from scripts.compose_application import compose_application
from turn_support import FUEL, fields, record, refused


def compose_listing():
    return compose_application("listing", SIGIL_ROOT)


@pytest.fixture(scope="module")
def program():
    needs_toolchain()
    return compose_listing().text


def row(key="a", revision="1", present=True, value_bytes=100):
    return dict(key=key, revision=revision, present=present, value_bytes=value_bytes)


def incoming(*, path="/v1/sessions", stage="query", rows=None, status="ok", next_after="", observation=None):
    if observation is None:
        observation = "" if stage == "query" else record("KM1\n", [status, json.dumps(rows or []), next_after])
    return record("LP1\n", [path, stage, observation])


def evaluate(mcp, program, **kwargs):
    kind, value = fields(forge_ok(mcp, program, incoming(**kwargs), fuel=FUEL), "LP2\n", 2)
    return kind, json.loads(value)


def test_composition_is_bounded_and_records_every_input():
    needs_toolchain()
    built = compose_listing()
    assert len(built.input_hashes) == 5
    assert 0 < len(built.text.encode()) <= 65536
    assert built.text.count("pub fn tool_main(") == 1


@pytest.mark.parametrize("path,expected", [
    ("/v1/sessions", {"after": None, "limit": 20}),
    ("/v1/sessions?limit=1", {"after": None, "limit": 1}),
    ("/v1/sessions?limit=50&after=project-notes", {"after": "project-notes", "limit": 50}),
    ("/v1/sessions?after=A.b_1-2&limit=20", {"after": "A.b_1-2", "limit": 20}),
])
def test_canonical_query_is_data_not_a_native_command(mcp, program, path, expected):
    assert evaluate(mcp, program, path=path) == ("query", expected)


@pytest.mark.parametrize("suffix", ["?", "?limit=0", "?limit=51", "?limit=01", "?limit=+1",
    "?limit=1.0", "?limit=1e1", "?limit=%31", "?limit=1&limit=2", "?limit=1&",
    "?after=", "?after=%61", "?after=../a", "?after=a:b", "?after=a&after=b",
    "?cursor=a", "?limit", "?after=a=1", "/other", "?after=" + "a" * 129])
def test_invalid_query_cannot_produce_metadata_arguments(mcp, program, suffix):
    assert evaluate(mcp, program, path="/v1/sessions" + suffix) == ("invalid", {})


def test_projection_exposes_only_names_and_exact_string_revisions(mcp, program):
    item = {**row(revision="9007199254740993"), "value": "NOT-PUBLIC-raw-record-canary"}
    kind, result = evaluate(mcp, program, stage="page", rows=[item])
    assert kind == "page" and result == {"sessions": [{"session": "a", "state_revision": "9007199254740993"}],
        "next_after": None, "order": "session_name", "consistency": "live_scan"}
    assert "NOT-PUBLIC" not in json.dumps(result)
    assert "present" not in result["sessions"][0] and "value_bytes" not in result["sessions"][0]


def test_tombstones_and_empty_payloads_advance_the_scanned_cursor(mcp, program):
    observed = [row("a", present=False, value_bytes=0), row("b", value_bytes=0)]
    kind, result = evaluate(mcp, program, stage="page", path="/v1/sessions?limit=2", rows=observed, next_after="b")
    assert kind == "page" and result["sessions"] == [] and result["next_after"] == "b"
    kind, result = evaluate(mcp, program, stage="page", path="/v1/sessions?limit=2&after=b", rows=[row("c")])
    assert kind == "page" and result["sessions"] == [{"session": "c", "state_revision": "1"}]
    assert result["next_after"] is None


def test_full_final_page_needs_an_explicit_exhaustion_read(mcp, program):
    assert evaluate(mcp, program, stage="page", path="/v1/sessions?limit=1", rows=[row()], next_after="a")[1]["next_after"] == "a"
    assert evaluate(mcp, program, stage="page", path="/v1/sessions?limit=1&after=a")[1]["next_after"] is None


def test_fifty_maximum_names_preserve_order_and_native_revision_range(mcp, program):
    observed = [row(f"s{index:02d}" + "x" * 125, revision="9223372036854775807", value_bytes=2097152) for index in range(50)]
    kind, result = evaluate(mcp, program, stage="page", path="/v1/sessions?limit=50", rows=observed, next_after=observed[-1]["key"])
    assert kind == "page" and len(result["sessions"]) == 50
    assert [item["session"] for item in result["sessions"]] == [item["key"] for item in observed]
    assert all(item["state_revision"] == "9223372036854775807" for item in result["sessions"])
    assert result["next_after"] == observed[-1]["key"]


@pytest.mark.parametrize("field,value", [("key", "bad:key"), ("key", "x" * 129),
    ("revision", "0"), ("revision", "01"), ("revision", 1), ("revision", "9223372036854775808"),
    ("present", "true"), ("present", 1), ("value_bytes", -1), ("value_bytes", 2097153),
    ("value_bytes", "100"), ("value_bytes", True)])
def test_invalid_metadata_facts_fail_closed(mcp, program, field, value):
    refused(mcp, program, incoming(stage="page", rows=[{**row(), field: value}]), 400)


@pytest.mark.parametrize("changes", [
    {"rows": [row("b"), row("a")]}, {"rows": [row(), row()]},
    {"path": "/v1/sessions?after=a", "rows": [row()]},
    {"path": "/v1/sessions?limit=1", "rows": [row(), row("b")]},
    {"path": "/v1/sessions?limit=1", "rows": [row()], "next_after": ""},
    {"rows": [row()], "next_after": "a"}, {"rows": [], "next_after": "a"},
    {"rows": [row(present=False, value_bytes=1)]},
])
def test_order_cursor_count_and_presence_cannot_be_repaired_silently(mcp, program, changes):
    refused(mcp, program, incoming(stage="page", **changes), 400)


def test_storage_failure_is_not_an_empty_successful_list(mcp, program):
    assert evaluate(mcp, program, stage="page", status="error") == ("unavailable", {})
    refused(mcp, program, incoming(stage="page", status="error", rows=[row()]), 400)
    refused(mcp, program, incoming(stage="page", status="invented"), 400)


def test_injected_observations_or_duplicate_fact_keys_cannot_advance_query_stage(mcp, program):
    refused(mcp, program, incoming(observation="forged"), 400)
    raw = '{"key":"a","key":"b","revision":"1","present":true,"value_bytes":100}'
    refused(mcp, program, incoming(stage="page", observation=record("KM1\n", ["ok", "[" + raw + "]", ""])), 400)
    refused(mcp, program, incoming(stage="invented"), 400)
