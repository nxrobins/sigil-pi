"""SIGIL reducers + real scoped native transactions + real isolated effects.

One starting path uses real authenticated HTTP admission. The test driver still
selects effect artifacts/authority and routes results; it is not product dispatch.
Process-kill evidence is not a physical power-loss test.
"""

import json
import time
from contextlib import ExitStack

import pytest

from conftest import forge_ok
from api_support import NativeApi, credential
from claimed_support import NativeClaimedWorker
from executor_support import executor_plan
from store_support import NativeStore
from test_turn_execution import (hanging_provider, programs as programs,
                                 provider_grants, provider_input)
from transaction_support import plan
from turn_support import Decision, FUEL, event, fields, record, text_reply, tool_reply


APP = {"pi-a.state": "read_write", "pi-a.intent": "create_only", "pi-a.delivery": "read"}
EXECUTOR = {"pi-a.intent": "read", "pi-a.dispatch": "read_write", "pi-a.delivery": "create_only"}


def intent_fields(decision):
    # Only the effect's required payload crosses the intent boundary. In
    # particular, a filesystem intent must not copy the conversation state.
    return [decision.values[0], decision.sequence, decision.action, decision.tool, decision.input]


def intent_key(decision):
    return f"{decision.values[0]}:{decision.sequence}"


@pytest.mark.parametrize("admission", ["component", "http"])
def test_native_commits_hold_a_complete_sigil_model_tool_response_turn_across_restarts(
        mcp, programs, scripted_llm, native_store_binary, native_claimed_worker_binary,
        native_service_binary, tmp_path, admission):
    workspace = tmp_path / "permitted"
    workspace.mkdir()
    (workspace / "README.md").write_text("The owner is Ada.\n")
    scripted_llm.script = [json.loads(tool_reply("read_file")), json.loads(text_reply("Ada owns the project."))]
    root = tmp_path / "records"
    if admission == "http":
        service_root = tmp_path / "service"
        row = credential(prefix="pi-a")
        with NativeApi(native_service_binary, service_root, credentials=[row]) as api:
            status, accepted = api.request(body={"session": "conversation", "message": "Read README.md",
                                                 "submission_key": "submission-1"})
            assert status == 202 and not accepted["replayed"]
        root = service_root / "records"
        with NativeStore(native_store_binary, root, {"pi-a.state": "read", "pi-a.intent": "read"}) as store:
            state = store.get("pi-a.state", "conversation")["value"]
            intent = fields(store.get("pi-a.intent", accepted["operation"] + ":1")["value"], "SI1\n", 5)
            current = Decision(state, intent[2], intent[1], intent[3], intent[4])
    else:
        pending = plan(mcp, programs["turn_transaction"], event())
        current = pending.decision
        with NativeStore(native_store_binary, root, APP, initialize=True) as store:
            pending.commit(store)
            store.kill()  # acceptance committed, effect not yet claimed/dispatched

    actual_effects = []
    state_revision = 1
    for _ in range(6):
        key = intent_key(current)
        if current.action == "model":
            payload = provider_input(scripted_llm.url, current.input)
            effect_source, grants = programs["provider"], provider_grants()
        else:
            assert current.action == "tool" and current.tool == "read_file"
            payload = forge_ok(mcp, programs["read_request"], record("PF1\n", [str(workspace), current.input]), fuel=FUEL)
            effect_source, grants = programs["read_file"], {"fs": [str(workspace)]}
        with NativeClaimedWorker(native_claimed_worker_binary, root, effect_source, EXECUTOR,
                                 grants=grants) as executor:
            assert executor.request({"op": "get", "namespace": "pi-a.state", "key": "conversation"})["status"] == "error"
            guard = (record("TG1\n", [fields(row["facts"], "CF2\n", 15)[3], current.values[13]])
                     if admission == "http" else None)
            prepared = executor.prepare(payload, key=key, time_guard=guard)
            loaded = executor.intent  # actual native read, not a supplied snapshot
            proposed = fields(loaded["value"], "SI1\n", 5)
            assert proposed == intent_fields(current)
            if current.action == "tool":
                assert json.loads(proposed[4]) == {"path": "README.md"}
                assert current.state not in loaded["value"]
            claim = executor_plan(mcp, programs["executor_transaction"], loaded,
                                  key=key, generation=prepared["generation"])
            assert claim.phase == "1" and claim.continuation == "dispatch_after_commit"
            executor.claim(claim.raw)  # native owns the actual receipt and gates execution
            result = executor.execute(prepared)
            assert result["fault"] is None and result["worker_reaped"], result
            assert result["generation"] == prepared["generation"] and result["result"]["status"] == "ok", result
            observed = result["result"]["data"]["output_text"]
            assert executor.request({"op": "execute", "ticket": prepared["ticket"]})["code"] == "ticket"
            actual_effects.append(current.action)
            delivery = executor_plan(mcp, programs["executor_transaction"], loaded,
                                     executor.get("pi-a.dispatch", key), event=1,
                                     outcome="returned", payload=observed, key=key, generation=result["generation"])
            assert delivery.phase == "2"
            delivery.commit(executor)
            executor.kill()  # delivery persisted, application has not consumed it

        with NativeStore(native_store_binary, root, APP) as app:
            prior = app.get("pi-a.state", "conversation")
            delivered = app.get("pi-a.delivery", key)
            assert prior["revision"] == state_revision
            facts = fields(delivered["value"], "DR1\n", 7)
            assert facts[4:6] == ["2", "returned"]
            incoming = current.result(facts[6], prior=prior["value"],
                                      now=int(time.time()) if admission == "http" else 101)
            pending = plan(mcp, programs["turn_transaction"], incoming, state_revision,
                           delivery=delivered["value"], delivery_revision=delivered["revision"])
            pending.commit(app)
            state_revision += 1
            app.kill()  # interpretation committed, caller acknowledgement lost
        current = pending.decision
        if current.action == "done":
            break
    assert actual_effects == ["model", "tool", "model"]
    assert len(scripted_llm.requests) == 2
    if admission == "http":
        # This is transport-to-component integration, NOT a completed product
        # turn: no admitted dispatcher/result settlement has been connected yet.
        with NativeStore(native_store_binary, root, {"pi-a.operations": "read", "pi-a.reservation": "read"}) as store:
            assert fields(store.get("pi-a.operations", accepted["operation"])["value"], "OQ2\n", 11)[9] == "accepted"
            assert fields(store.get("pi-a.reservation", accepted["operation"])["value"], "BR1\n", 9)[7] == "reserved"
    assert scripted_llm.requests[1]["messages"][-1]["content"][0]["content"] == "The owner is Ada.\n"
    with NativeStore(native_store_binary, root, APP) as app:
        retained = app.get("pi-a.state", "conversation")
        values = fields(retained["value"], "PT1\n", 17)
        assert values[1] == "done" and values[9] == "Ada owns the project."
        assert retained["revision"] == 4
        # A stale interpretation cannot overwrite the response or make another intent.
        stale = app.raw(pending.raw)
        assert stale["code"] == "conflict"
    assert len(scripted_llm.requests) == 2


@pytest.mark.parametrize("boundary", ["before_claim", "after_claim", "after_request_arrives"])
def test_sigil_recovery_uses_durable_delivery_facts_and_never_replays_uncertain_effects(
        mcp, programs, native_store_binary, native_claimed_worker_binary, tmp_path, boundary):
    root = tmp_path / "records"
    pending = plan(mcp, programs["turn_transaction"], event())
    first = pending.decision
    key = intent_key(first)
    with NativeStore(native_store_binary, root, APP, initialize=True) as app:
        pending.commit(app)
        app.kill()
    requests = []
    with ExitStack() as stack:
        endpoint = "http://127.0.0.1:1/messages"  # never executed in no-dispatch cases
        if boundary == "after_request_arrives":
            endpoint, received, requests = stack.enter_context(hanging_provider())
        worker = stack.enter_context(NativeClaimedWorker(native_claimed_worker_binary, root,
            programs["provider"], EXECUTOR, grants=provider_grants()))
        prepared = worker.prepare(provider_input(endpoint, first.input),
                                  key=key,
                                  timeout_ms=1500 if boundary == "after_request_arrives" else 15000)
        if boundary != "before_claim":
            claim = executor_plan(mcp, programs["executor_transaction"], worker.intent,
                                  key=key, generation=prepared["generation"])
            worker.claim(claim.raw)
        if boundary == "after_request_arrives":
            result = worker.execute(prepared)
            assert result["fault"] == "deadline" and result["request_may_have_run"] and result["worker_reaped"], result
            assert received.is_set() and len(requests) == 1
            assert worker.request({"op": "execute", "ticket": prepared["ticket"]})["code"] == "ticket"
        worker.kill()  # lose the native ticket/receipt with or without possible delivery
    if boundary != "before_claim":
        with NativeClaimedWorker(native_claimed_worker_binary, root, programs["provider"],
                                 EXECUTOR, grants=provider_grants()) as replacement:
            assert replacement.request(replacement.prepare_request(
                provider_input(endpoint, first.input), key=key))["code"] == "claimed"
    with NativeStore(native_store_binary, root, EXECUTOR) as executor:
        persisted = executor.get("pi-a.dispatch", key)
        recovery = executor_plan(mcp, programs["executor_transaction"], executor.get("pi-a.intent", key),
                                 persisted, event=3, generation="replacement-worker", key=key)
        if boundary == "before_claim":
            assert recovery.phase == "0" and recovery.continuation == "none" and recovery.raw == ""
            assert executor.get("pi-a.intent", key)["revision"] == 1
        else:
            assert recovery.phase == "4" and recovery.continuation == "record_after_commit"
            recovery.commit(executor)
            executor.kill()
    if boundary != "before_claim":
        with NativeStore(native_store_binary, root, APP) as app:
            prior = app.get("pi-a.state", "conversation")
            delivered = app.get("pi-a.delivery", key)
            assert fields(delivered["value"], "DR1\n", 7)[3] == prepared["generation"]
            assert fields(delivered["value"], "DR1\n", 7)[4:] == ["4", "", ""]
            pending = plan(mcp, programs["turn_transaction"], first.result(kind="unknown", prior=prior["value"]),
                           prior["revision"], delivery=delivered["value"], delivery_revision=delivered["revision"])
            stopped = pending.decision
            assert stopped.action == "uncertain" and stopped.input == ""
            pending.commit(app)
        with NativeStore(native_store_binary, root, APP) as app:
            assert fields(app.get("pi-a.state", "conversation")["value"], "PT1\n", 17)[1] == "uncertain"
    assert len(requests) == (1 if boundary == "after_request_arrives" else 0)
