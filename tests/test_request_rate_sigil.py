"""Real SIGIL rate proposals; full HTTP admission and pilot policy remain open."""

from contextlib import closing
import hashlib
import json
import sqlite3

import pytest

from api_support import credential
from conftest import forge_ok, mcp as mcp, native_store_binary as native_store_binary, needs_toolchain
from product_service import DurableQuotaStore
from request_rate_support import ROOT, compose_rate_probe, incoming, snapshot
from store_support import NativeStore
from turn_support import FUEL, fields, record, refused


@pytest.fixture(scope="module")
def program():
    needs_toolchain()
    return compose_rate_probe().text


def proposal(mcp, program, **kwargs):
    return fields(forge_ok(mcp, program, incoming(**kwargs), fuel=FUEL), "RA1\n", 4)


def test_rate_probe_binds_its_inputs_and_fits_the_original_source_ceiling():
    built = compose_rate_probe()
    assert built == compose_rate_probe()
    assert 0 < len(built.text.encode()) <= 65536
    assert built.text.count("pub fn tool_main(") == 1
    assert built.text.count("fn request_rate(") == 1
    assert len(built.input_hashes) == 8
    for relative in ("app/pi/request_rate.sigil", "tests/fixtures/request_rate_probe.sigil",
                     "tests/request_rate_support.py"):
        assert built.input_hashes[relative] == hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()


@pytest.mark.parametrize("tenant,prefix", [("tenant-a", "a"), ("tenant-b", "b"), ("tenant Ω", "c")])
def test_first_admission_proposes_only_a_bound_request_window_and_credential_guard(
        mcp, program, tenant, prefix):
    row = credential(tenant=tenant, prefix=prefix, expires=200)
    result = proposal(mcp, program, row=row)
    assert result[0] == "admit" and result[3] == ""
    assert fields(result[2], "TG1\n", 2) == ["100", "200"]
    assert json.loads(result[1]) == {
        "op": "commit", "checks": [], "writes": [{
            "namespace": prefix + ".budget", "key": "request-window", "revision": 0,
            "value": record("RW1\n", [tenant, "120", "1"]),
        }],
    }


def test_existing_count_is_incremented_using_the_exact_observed_revision(mcp, program):
    result = proposal(mcp, program, limit=5, observed=snapshot(revision=9, count=3))
    assert result[0] == "admit"
    batch = json.loads(result[1])
    assert batch["checks"] == [] and len(batch["writes"]) == 1
    assert batch["writes"][0] == {
        "namespace": "a.budget", "key": "request-window", "revision": 9,
        "value": record("RW1\n", ["tenant-a", "120", "4"]),
    }


@pytest.mark.parametrize("now", [120.0, 120.1, 150.0, 150.1, 178.999, 179.0, 179.999])
def test_limited_retry_matches_the_real_legacy_durable_store(mcp, program, tmp_path, now):
    legacy = DurableQuotaStore(tmp_path / "legacy.sqlite", requests_per_minute=1, clock=lambda: now)
    assert legacy.admit_request("tenant-a") == (True, 0)
    admitted, retry = legacy.admit_request("tenant-a")
    assert not admitted
    with closing(sqlite3.connect(legacy.path)) as db:
        window, count = db.execute("SELECT window_start,count FROM request_windows").fetchone()
    result = proposal(mcp, program, limit=1, now=int(now), fractional=int(now != int(now)),
                      observed=snapshot(revision=1, window=window, count=count))
    assert result == ["limited", "", "", str(retry)]


@pytest.mark.parametrize("now,expected", [(179, "limited"), (180, "admit"), (600, "admit")])
def test_forward_window_changes_match_legacy_count_reset(mcp, program, now, expected):
    result = proposal(mcp, program, now=now, limit=1, observed=snapshot(revision=8, count=1))
    assert result[0] == expected
    if expected == "admit":
        write = json.loads(result[1])["writes"][0]
        assert write["revision"] == 8
        assert fields(write["value"], "RW1\n", 3) == ["tenant-a", str(now // 60 * 60), "1"]


@pytest.mark.parametrize("now", [99, 200])
def test_credential_time_refusal_produces_no_quota_update(mcp, program, now):
    assert proposal(mcp, program, row=credential(expires=200), now=now) == ["expired", "", "", ""]


def test_backward_window_is_an_explicit_fail_closed_difference_not_parity(mcp, program):
    assert proposal(mcp, program, now=179, observed=snapshot(revision=1, window=180)) == ["clock", "", "", ""]


def test_storage_failure_or_exhausted_record_revision_never_claims_admission(mcp, program):
    assert proposal(mcp, program, observed=snapshot(status="error")) == ["storage", "", "", ""]
    assert proposal(mcp, program, observed=snapshot(revision=9223372036854775807)) == ["storage", "", "", ""]


@pytest.mark.parametrize("change", [
    {"limit": 0}, {"limit": -1}, {"limit": "01"}, {"limit": "1.0"},
    {"limit": ""}, {"limit": 9223372036854775808},
    {"fractional": 2}, {"fractional": ""}, {"fractional": "true"}, {"fractional": "00"},
    {"now": -1}, {"now": "0150"}, {"now": "150.1"}, {"now": 9007199254740001},
])
def test_invalid_policy_or_clock_values_cannot_produce_an_update(mcp, program, change):
    refused(mcp, program, incoming(**change), 400)


@pytest.mark.parametrize("seen", [
    snapshot(revision=1, tenant="foreign-tenant"),
    snapshot(revision=1, raw=""), snapshot(revision=0, raw="forged-missing-value"),
    snapshot(revision=1, window=121), snapshot(revision=1, window="0120"),
    snapshot(revision=1, count=0), snapshot(revision=1, count=-1),
    snapshot(revision=1, count="01"), snapshot(revision=1, count=9223372036854775808),
    snapshot(revision=1, status="error"), snapshot(status="unknown"),
    record("SR1\n", ["error", "0", "partial"]),
    record("SR1\n", ["ok", "01", ""]),
])
def test_bad_record_binding_or_shape_is_not_a_fresh_allowance(mcp, program, seen):
    refused(mcp, program, incoming(observed=seen), 400)


def actual_snapshot(store, namespace):
    seen = store.get(namespace, "request-window")
    return seen, record("SR1\n", ["ok", str(seen["revision"]), seen["value"] or ""])


def test_actual_native_accounting_survives_restart_and_remains_tenant_scoped(
        mcp, program, native_store_binary, tmp_path):
    root = tmp_path / "accounting"
    grants = {"a.budget": "read_write", "b.budget": "read_write"}
    with NativeStore(native_store_binary, root, grants, initialize=True) as store:
        for expected in (1, 2):
            _, seen = actual_snapshot(store, "a.budget")
            result = proposal(mcp, program, observed=seen)
            assert result[0] == "admit"
            assert store.request(json.loads(result[1]))["status"] == "ok"
            retained = store.get("a.budget", "request-window")
            assert fields(retained["value"], "RW1\n", 3) == ["tenant-a", "120", str(expected)]
        before, seen = actual_snapshot(store, "a.budget")
        assert proposal(mcp, program, observed=seen) == ["limited", "", "", "30"]
        assert store.get("a.budget", "request-window") == before
        _, other_seen = actual_snapshot(store, "b.budget")
        other = credential(tenant="tenant-b", prefix="b")
        result = proposal(mcp, program, row=other, observed=other_seen)
        assert result[0] == "admit" and store.request(json.loads(result[1]))["status"] == "ok"
        assert store.get("a.budget", "request-window") == before
        assert fields(store.get("b.budget", "request-window")["value"], "RW1\n", 3) == ["tenant-b", "120", "1"]
    with NativeStore(native_store_binary, root, grants) as store:
        reopened, seen = actual_snapshot(store, "a.budget")
        assert reopened == before
        peer = credential("inert-alternate-credential", principal="same-tenant-peer")
        assert proposal(mcp, program, row=peer, observed=seen) == ["limited", "", "", "30"]
        assert store.get("a.budget", "request-window") == before


def test_actual_native_compare_and_set_rejects_a_stale_admission_proposal(
        mcp, program, native_store_binary, tmp_path):
    with NativeStore(native_store_binary, tmp_path / "conditional",
                     {"a.budget": "read_write"}, initialize=True) as store:
        _, seen = actual_snapshot(store, "a.budget")
        first = proposal(mcp, program, observed=seen)
        stale = proposal(mcp, program, observed=seen)
        assert first == stale and first[0] == "admit"
        assert store.request(json.loads(first[1]))["status"] == "ok"
        retained = store.get("a.budget", "request-window")
        assert store.request(json.loads(stale[1])) == {"status": "error", "code": "conflict"}
        assert store.get("a.budget", "request-window") == retained
        _, current = actual_snapshot(store, "a.budget")
        next_result = proposal(mcp, program, observed=current)
        assert next_result[0] == "admit"
        assert store.request(json.loads(next_result[1]))["status"] == "ok"
        assert fields(store.get("a.budget", "request-window")["value"], "RW1\n", 3)[2] == "2"
