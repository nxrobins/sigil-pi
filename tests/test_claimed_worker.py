"""Actual native claim/worker boundary with solver-verified SIGIL and real storage.

No external provider or product dispatcher: the test supplies trusted selection.
"""
import copy
import json

import pytest

from claimed_support import NativeClaimedWorker
from executor_support import executor_plan
from store_support import NativeStore, mutation
from test_turn_execution import programs as programs
from turn_support import fields, record


ACCESS = {"pi-a.intent": "read", "pi-a.dispatch": "read_write", "pi-a.delivery": "create_only"}


@pytest.fixture
def root(native_store_binary, tmp_path):
    root = tmp_path / "records"
    with NativeStore(native_store_binary, root, {"pi-a.intent": "read_write"}, initialize=True) as store:
        store.commit([mutation("pi-a.intent", "operation-1:1", "opaque committed intent")])
    return root


def new_worker(binary, root, programs, tmp_path, *, access=None):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "README.md").write_text("frozen input selects this file\n")
    return NativeClaimedWorker(binary, root, programs["read_file"], ACCESS if access is None else access,
                               grants={"fs": [str(workspace)]})


def prepare(worker, tmp_path):
    return worker.prepare(str(tmp_path / "workspace/README.md"))


def claim(mcp, programs, worker, prepared):
    return executor_plan(mcp, programs["executor_transaction"], worker.intent,
                         generation=prepared["generation"])


def test_execute_needs_a_real_local_claim_not_a_receipt_or_hint(
        mcp, programs, native_claimed_worker_binary, root, tmp_path):
    with new_worker(native_claimed_worker_binary, root, programs, tmp_path) as worker:
        ready = prepare(worker, tmp_path)
        planned = claim(mcp, programs, worker, ready)
        assert planned.continuation == "dispatch_after_commit"
        assert worker.request({"op": "execute", "ticket": ready["ticket"], "receipt": {"revision": 2}})["code"] == "protocol"
        assert worker.request({"op": "execute", "ticket": ready["ticket"]})["code"] == "unclaimed"
        assert worker.request({"op": "claim", "batch": planned.raw})["code"] == "ticket"
        assert worker.get("pi-a.dispatch", "operation-1:1")["revision"] == 0


@pytest.mark.parametrize("change", ["intent_namespace", "intent_key", "intent_revision", "missing_check",
    "slot_namespace", "slot_key", "slot_revision", "empty_record", "generation", "phase", "extra_write", "extra_check"])
def test_actual_native_gate_refuses_misbound_or_expanded_sigil_claims(
        mcp, programs, native_claimed_worker_binary, root, tmp_path, change):
    with new_worker(native_claimed_worker_binary, root, programs, tmp_path) as worker:
        ready = prepare(worker, tmp_path)
        batch = copy.deepcopy(claim(mcp, programs, worker, ready).request)
        if change == "intent_namespace": batch["checks"][0]["namespace"] = "pi-b.intent"
        if change == "intent_key": batch["checks"][0]["key"] = "other"
        if change == "intent_revision": batch["checks"][0]["revision"] += 1
        if change == "missing_check": batch["checks"] = []
        if change == "slot_namespace": batch["writes"][0]["namespace"] = "pi-b.dispatch"
        if change == "slot_key": batch["writes"][0]["key"] = "other"
        if change == "slot_revision": batch["writes"][0]["revision"] = 1
        if change == "empty_record": batch["writes"][0]["value"] = ""
        if change in {"generation", "phase"}:
            values = fields(batch["writes"][0]["value"], "SD1\n", 5)
            values[3 if change == "generation" else 4] = "replacement" if change == "generation" else "4"
            batch["writes"][0]["value"] = record("SD1\n", values)
        if change == "extra_write": batch["writes"].append(mutation("pi-a.delivery", "operation-1:1", "injected"))
        if change == "extra_check": batch["checks"].append({"namespace": "pi-a.dispatch", "key": "other", "revision": 0})
        assert worker.request({"op": "claim", "batch": json.dumps(batch)})["status"] == "error"
        assert worker.request({"op": "execute", "ticket": ready["ticket"]})["code"] == "ticket"
        assert worker.get("pi-a.dispatch", "operation-1:1")["revision"] == 0


def test_valid_claim_executes_the_frozen_input_once_and_reopen_cannot_execute_it_again(
        mcp, programs, native_claimed_worker_binary, root, tmp_path):
    with new_worker(native_claimed_worker_binary, root, programs, tmp_path) as worker:
        ready = prepare(worker, tmp_path)
        assert worker.claim(claim(mcp, programs, worker, ready).raw)["revision"] == 2
        observed = worker.execute(ready)
        assert observed["fault"] is None and observed["worker_reaped"]
        assert observed["generation"] == ready["generation"]
        assert observed["result"]["data"]["output_text"] == "frozen input selects this file\n"
        assert worker.request({"op": "execute", "ticket": ready["ticket"]})["code"] == "ticket"
        worker.kill()  # no delivery written; durable claim must not be repeated
    with new_worker(native_claimed_worker_binary, root, programs, tmp_path) as worker:
        assert worker.request(worker.prepare_request("different input"))["code"] == "claimed"
        assert worker.get("pi-a.dispatch", "operation-1:1")["revision"] == 1


def test_cancel_before_execution_keeps_committed_claim_without_an_effect(
        mcp, programs, native_claimed_worker_binary, root, tmp_path):
    with new_worker(native_claimed_worker_binary, root, programs, tmp_path) as worker:
        ready = prepare(worker, tmp_path)
        worker.claim(claim(mcp, programs, worker, ready).raw)
        assert worker.request({"op": "cancel", "ticket": ready["ticket"]}) == {"status": "ok", "cancelled_before_start": True}
        assert worker.request({"op": "execute", "ticket": ready["ticket"]})["code"] == "ticket"
        assert worker.request(worker.prepare_request("other"))["code"] == "claimed"


def test_scope_denied_claim_cannot_turn_a_store_error_into_worker_permission(
        mcp, programs, native_claimed_worker_binary, root, tmp_path):
    with new_worker(native_claimed_worker_binary, root, programs, tmp_path,
                    access={"pi-a.intent": "read", "pi-a.dispatch": "read"}) as worker:
        ready = prepare(worker, tmp_path)
        assert worker.request({"op": "claim", "batch": claim(mcp, programs, worker, ready).raw})["code"] == "storage"
        assert worker.request({"op": "execute", "ticket": ready["ticket"]})["code"] == "ticket"
        assert worker.get("pi-a.dispatch", "operation-1:1")["revision"] == 0


@pytest.mark.parametrize("window", [["0", "1"], ["8999999999", "9000000000"]])
def test_time_refusal_does_not_prepare_an_effect_or_create_a_claim(
        programs, native_claimed_worker_binary, root, tmp_path, window):
    with new_worker(native_claimed_worker_binary, root, programs, tmp_path) as worker:
        request = worker.prepare_request("payload", time_guard=record("TG1\n", window))
        assert worker.request(request)["code"] == "time_guard"
        assert worker.get("pi-a.dispatch", "operation-1:1")["revision"] == 0


def test_caller_cannot_supply_snapshots_grants_or_cross_tenant_coordinates(
        programs, native_claimed_worker_binary, root, tmp_path):
    with new_worker(native_claimed_worker_binary, root, programs, tmp_path) as worker:
        for field in ["snapshot", "grants", "runtime", "source", "receipt"]:
            assert worker.request({**worker.prepare_request("input"), field: "forged"})["code"] == "protocol"
        raw = json.dumps(worker.prepare_request("input"))
        assert worker.raw(raw[:-1] + ',"f\\u0075el":1}')["code"] == "protocol"
        assert worker.raw(raw[:-1] + ',"unexpected":{"a":1,"a":2}}')["code"] == "protocol"
        assert worker.request(worker.prepare_request("input", prefix="pi-b"))["code"] == "storage"
        assert worker.request(worker.prepare_request("input", key="missing"))["code"] == "intent"


def test_an_external_commit_is_not_a_resumable_execution_receipt(
        mcp, programs, native_claimed_worker_binary, root, tmp_path):
    with new_worker(native_claimed_worker_binary, root, programs, tmp_path) as worker:
        ready = prepare(worker, tmp_path)
        planned = claim(mcp, programs, worker, ready)
        assert worker.request({"op": "cancel", "ticket": ready["ticket"]})["status"] == "ok"
        # This is a real native commit, but not a live receipt held by an attempt.
        assert worker.raw(planned.raw)["status"] == "ok"
        assert worker.request({"op": "execute", "ticket": ready["ticket"]})["code"] == "ticket"
        assert worker.request(worker.prepare_request("input"))["code"] == "claimed"
