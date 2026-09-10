"""Additional actual audited-publication failures and lost acknowledgements."""
import json
import select

import pytest

from audited_transaction_support import (CHAIN, ENTRIES, HEADS, KEY_ENV, NativeAudit,
    audited_native as audited_native, inspect, retained_audit as retained_audit,
    readiness_host_binary as readiness_host_binary)
from store_support import LIMITS, NativeStore, mutation
from turn_support import fields
from test_audited_transaction import native_key as native_key
from test_settlement import terminal_records as terminal_records


def test_acknowledgement_lost_after_commit_does_not_repeat_settlement_or_audit(audited_native, retained_audit):
    root, row = retained_audit
    with NativeAudit(audited_native[0], root, row) as worker:
        worker.proc.stdin.write(json.dumps({"op": "apply", "values": ["a" * 64, "same-session"]}) + "\n")
        worker.proc.stdin.flush()
        readable, _, _ = select.select([worker.proc.stdout], [], [], 30)
        assert readable, "no native publication acknowledgement became available"
        # The actual commit acknowledgement exists, but the client never consumes
        # it. This is an after-commit kill, not an injected pre-commit crash test.
        worker.proc.kill()
        worker.proc.wait(timeout=5)
    saved = inspect(audited_native[1], root, row, count=2)
    assert [v["revision"] for v in saved[0]] == [2, 2, 2, 2]
    assert saved[1]["revision"] == 1 and saved[2][0]["revision"] == 1
    assert saved[2][1]["revision"] == 0
    with NativeAudit(audited_native[0], root, row) as worker:
        assert worker.apply()["applied"]["receipt"] is None
        assert worker.request({"op": "verify_audit"})["checkpoint"]["count"] == 1
    assert inspect(audited_native[1], root, row, count=2) == saved


@pytest.mark.parametrize("kind", ["denied", "orphan", "tombstone", "changed_store_limit"])
def test_related_and_log_preconditions_remain_one_all_or_nothing_commit(audited_native, retained_audit, kind):
    root, row = retained_audit
    if kind in {"orphan", "tombstone"}:
        with NativeStore(audited_native[1], root, {HEADS: "read_write", ENTRIES: "read_write"}) as store:
            if kind == "orphan":
                store.commit([mutation(ENTRIES, CHAIN + ".0000000000000000", "pre-existing")])
            else:
                store.commit([mutation(HEADS, CHAIN, "old-head")])
                store.commit([mutation(HEADS, CHAIN, None, 1)])
    before = inspect(audited_native[1], root, row)
    def patch(config):
        if kind == "denied":
            config["grants"]["a.budget"] = "read"
        elif kind == "changed_store_limit":
            # The stored original limit cannot be changed by a reopen profile.
            # Admission refusal is distinct from an in-transaction capacity fault.
            config["limits"] = {**config["limits"], "records": 1}
    if kind == "changed_store_limit":
        with pytest.raises(AssertionError, match="storage"):
            NativeAudit(audited_native[0], root, row, patch=patch)
    else:
        with NativeAudit(audited_native[0], root, row, patch=patch) as worker:
            result = worker.apply()
            assert result["status"] == "error" and "applied" not in result
    assert inspect(audited_native[1], root, row) == before


def test_actual_store_capacity_includes_both_audit_records_atomically(audited_native, terminal_records, tmp_path):
    row, records, writes = terminal_records
    root = tmp_path / "records"
    # A deliberately smaller installed store, not a weakened production ceiling.
    # Six existing records + two new log records cannot fit seven slots.
    limits = {**LIMITS, "records": 7}
    config = {"version": 1, "limits": limits, "grants": [
        {"namespace": ns, "access": access} for ns, access in
        {**row["grants"], HEADS: "read", ENTRIES: "read"}.items()]}
    with NativeStore(audited_native[1], root, {}, initialize=True, config=config) as store:
        store.commit(writes)
        store.commit([mutation("a.state", "same-session", fields(records[1], "SR1\n", 3)[2], 1)])
        before = [store.get(w["namespace"], w["key"]) for w in writes]
    def patch(c):
        c["limits"] = limits
        c["audit"]["limits"]["records"] = 1
    with NativeAudit(audited_native[0], root, row, patch=patch) as worker:
        assert worker.apply() == {"status": "error", "code": "storage"}
    with NativeStore(audited_native[1], root, {}, config=config) as store:
        assert [store.get(w["namespace"], w["key"]) for w in writes] == before
        assert store.get(HEADS, CHAIN) == store.get(ENTRIES, CHAIN + ".0000000000000000") == {"revision": 0, "value": None}


def test_verification_cannot_accept_caller_selected_chain_or_checkpoint(audited_native, retained_audit):
    root, row = retained_audit
    with NativeAudit(audited_native[0], root, row) as worker:
        assert worker.apply()["status"] == "ok"
        for key in ["chain", "checkpoint", "key_env", "payload", "expected"]:
            assert worker.request({"op": "verify_audit", key: "forged"})["code"] == "protocol"


def test_wrong_reopen_key_fails_verification_without_repair(audited_native, retained_audit, monkeypatch):
    root, row = retained_audit
    with NativeAudit(audited_native[0], root, row) as worker:
        assert worker.apply()["status"] == "ok"
    before = inspect(audited_native[1], root, row)
    monkeypatch.setenv(KEY_ENV, "a-different-native-only-fixture-key-0123456789")
    with NativeAudit(audited_native[0], root, row) as worker:
        assert worker.request({"op": "verify_audit"})["code"] == "audit_verification"
    assert inspect(audited_native[1], root, row) == before
