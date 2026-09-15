"""No caller-supplied result/claim/delivery can enter the recorded execution path."""
import json
import select
import time

import pytest

from conftest import SIGIL_ROOT
from scripts.compose_application import compose_application
from store_support import NativeStore, mutation
from test_native_dispatch import seeded as seeded, worker
from test_turn_execution import hanging_provider, programs as programs
from transaction_support import plan as interpret
from turn_support import Decision, fields, record, text_reply


def delivery(binary, root):
    with NativeStore(binary, root, {"a.dispatch": "read", "a.delivery": "read"}) as store:
        return store.get("a.dispatch", "a" * 64 + ":1"), store.get("a.delivery", "a" * 64 + ":1")


def test_actual_worker_observation_is_recorded_without_caller_claims_or_outcomes(
        programs, seeded, native_claimed_worker_binary, native_store_binary, scripted_llm):
    scripted_llm.script = [json.loads(text_reply("Actual observed body"))]
    with worker(native_claimed_worker_binary, seeded, programs, recorded=True, argument=scripted_llm.url) as active:
        ready = active.authorize()
        result = active.run_recorded(ready)
        assert result["phase"] == "2" and result["recording_error"] is None and result["refused"] is None
        assert result["claim_receipt"]["revision"] == 2 and result["delivery_receipt"]["revision"] == 3
        assert result["observation"]["generation"] == ready["generation"]
        payload = result["observation"]["result"]["data"]["output_text"]
        assert active.request({"op": "run", "ticket": ready["ticket"]})["code"] == "ticket"
    dispatch, saved = delivery(native_store_binary, seeded[0])
    assert dispatch["revision"] == 2 and saved["revision"] == 1
    assert fields(saved["value"], "DR1\n", 7) == ["a.intent", "a" * 64 + ":1", "1", ready["generation"], "2", "returned", payload]
    with worker(native_claimed_worker_binary, seeded, programs, recorded=True, argument=scripted_llm.url) as active:
        assert active.request(active.authorize_request())["code"] == "claimed"
    assert len(scripted_llm.requests) == 1


@pytest.mark.parametrize("kind", ["commit", "claim", "execute", "outcome", "observation", "phase", "receipt", "payload"])
def test_caller_cannot_supply_a_claim_or_result_or_bypass_recording(programs, seeded, native_claimed_worker_binary, kind):
    with worker(native_claimed_worker_binary, seeded, programs, recorded=True) as active:
        assert active.request({"op": "commit", "checks": [], "writes": []})["code"] == "protocol"
        ready = active.authorize()
        request = {"op": "run", "ticket": ready["ticket"]}
        if kind == "commit":
            request = {"op": "commit", "checks": [], "writes": []}
        elif kind == "claim":
            request = {"op": "claim", "batch": "forged"}
        elif kind == "execute":
            request = {"op": "execute", "ticket": ready["ticket"]}
        else:
            request[kind] = "forged"
        assert active.request(request)["code"] == "protocol"
        assert active.request({"op": "cancel", "ticket": ready["ticket"]})["cancelled_before_start"]
        assert active.get("a.dispatch", "a" * 64 + ":1")["revision"] == 0


@pytest.mark.parametrize("tombstone", [False, True])
def test_retained_delivery_without_a_claim_prevents_effect_initiation(
        programs, seeded, native_claimed_worker_binary, native_store_binary, scripted_llm, tombstone):
    root = seeded[0]
    key = "a" * 64 + ":1"
    with NativeStore(native_store_binary, root, {"a.delivery": "read_write"}) as store:
        store.commit([mutation("a.delivery", key, "retained")])
        if tombstone:
            store.commit([mutation("a.delivery", key, None, 1)])
    with worker(native_claimed_worker_binary, seeded, programs, recorded=True, argument=scripted_llm.url) as active:
        ready = active.authorize()
        assert active.request({"op": "run", "ticket": ready["ticket"]})["code"] == "delivery"
        assert active.get("a.dispatch", key)["revision"] == 0
    assert scripted_llm.requests == []


def test_failed_observed_provider_request_is_persisted_as_uncertain_and_never_replayed(
        programs, seeded, native_claimed_worker_binary, native_store_binary):
    with hanging_provider() as (endpoint, arrived, requests):
        # The pinned runtime's HTTP timeout provides a real error observation.
        # A tiny outer deadline could expire while compiling the claim producer,
        # before reaching this provider boundary; do not relabel that as a send.
        with worker(native_claimed_worker_binary, seeded, programs, recorded=True, argument=endpoint) as active:
            ready = active.authorize()
            result = active.run_recorded(ready)
            assert arrived.is_set() and len(requests) == 1
            assert result["phase"] == "4" and result["delivery_receipt"] and result["recording_error"] is None
            assert result["observation"]["request_may_have_run"] and result["observation"]["worker_reaped"]
            assert result["observation"]["fault"] is None
            assert result["observation"]["result"]["status"] == "error"
        with worker(native_claimed_worker_binary, seeded, programs, recorded=True, argument=endpoint) as active:
            assert active.request(active.authorize_request())["code"] == "claimed"
        assert len(requests) == 1
    _, saved = delivery(native_store_binary, seeded[0])
    assert fields(saved["value"], "DR1\n", 7)[4:] == ["4", "", ""]


@pytest.mark.parametrize("kind", ["wrong_generation", "wrong_namespace", "malformed_output"])
def test_bad_owner_installed_recorder_output_cannot_fabricate_durable_acknowledgement(
        programs, seeded, native_claimed_worker_binary, native_store_binary, scripted_llm, kind):
    code = compose_application("worker_completion", SIGIL_ROOT).text
    if kind == "wrong_generation":
        needle = "put(t, 5, get(x, 10));"
        assert code.count(needle) == 1
        code = code.replace(needle, 'put(t, 3, text("wrong-worker")); ' + needle)
    elif kind == "wrong_namespace":
        needle = 'store_write(get(x, 2), get(x, 3), text("0"), delivery)'
        assert code.count(needle) == 1
        code = code.replace(needle, 'store_write(text("other.delivery"), get(x, 3), text("0"), delivery)')
    else:
        needle = 'return executor_result(following, "record_after_commit", request);'
        assert code.count(needle) == 1
        code = code.replace(needle, 'return text("bad result envelope");')
    scripted_llm.script = [json.loads(text_reply("Observed even if recording fails"))]
    with worker(native_claimed_worker_binary, seeded, programs, recorded=True,
                argument=scripted_llm.url, recorder_source=code) as active:
        ready = active.authorize()
        result = active.run_recorded(ready)
        assert result["claim_receipt"] and result["delivery_receipt"] is None and result["phase"] is None
        assert result["recording_error"] == ("protocol" if kind == "malformed_output" else "recording")
        assert result["observation"]["result"]["status"] == "ok"
    claim, saved = delivery(native_store_binary, seeded[0])
    assert fields(claim["value"], "SD1\n", 5)[4] == "1" and saved["revision"] == 0
    assert len(scripted_llm.requests) == 1
    with worker(native_claimed_worker_binary, seeded, programs, recorded=True,
                argument=scripted_llm.url, recorder_source=code) as active:
        response = active.request(recovery_request())
        assert response["code"] == ("protocol" if kind == "malformed_output" else "recording")
    assert delivery(native_store_binary, seeded[0]) == (claim, saved)
    with worker(native_claimed_worker_binary, seeded, programs, recorded=True, argument=scripted_llm.url) as active:
        assert active.request(active.authorize_request())["code"] == "claimed"
        response = active.request(recovery_request())
        assert response["status"] == "ok" and response["recovery"]["phase"] == "4", response
        assert response["recovery"]["claim_generation"] == ready["generation"]
    _, recovered = delivery(native_store_binary, seeded[0])
    assert fields(recovered["value"], "DR1\n", 7)[3:] == [ready["generation"], "4", "", ""]
    assert len(scripted_llm.requests) == 1


@pytest.mark.parametrize("kind", ["network", "filesystem", "secrets", "delivery_read", "claim_read", "domain_write", "alias"])
def test_recorder_bootstrap_requires_a_pure_worker_and_restricted_actual_write_scope(
        programs, seeded, native_claimed_worker_binary, kind):
    def patch(config):
        if kind == "network":
            config["recorder"]["worker"]["net"] = ["127.0.0.1"]
        elif kind == "filesystem":
            config["recorder"]["worker"]["fs"] = ["/tmp"]
        elif kind == "secrets":
            config["recorder"]["worker"]["secret_env"] = {"secret": "UNUSED"}
        elif kind == "delivery_read":
            config["grants"]["a.delivery"] = "read"
        elif kind == "claim_read":
            config["grants"]["a.dispatch"] = "read"
        elif kind == "domain_write":
            config["grants"]["a.state"] = "read_write"
        else:
            config["recorder"]["delivery_namespace"] = "a.dispatch"
    with pytest.raises(AssertionError, match="config"):
        worker(native_claimed_worker_binary, seeded, programs, recorded=True, recorder_patch=patch)


def recovery_request(**kwargs):
    return {"op": "recover", "intent_namespace": "a.intent", "key": "a" * 64 + ":1", **kwargs}


def start_without_reading_ack(active, ready):
    active.proc.stdin.write(json.dumps({"op": "run", "ticket": ready["ticket"]}) + "\n")
    active.proc.stdin.flush()


def test_controller_death_after_provider_send_is_natively_recovered_without_replay(
        mcp, programs, seeded, native_claimed_worker_binary, native_store_binary):
    root, row, _ = seeded
    with hanging_provider() as (endpoint, arrived, requests):
        with worker(native_claimed_worker_binary, seeded, programs, recorded=True, argument=endpoint) as active:
            ready = active.authorize()
            start_without_reading_ack(active, ready)
            assert arrived.wait(20), "must observe the real request before killing its controller"
            active.kill()
        claim, saved = delivery(native_store_binary, root)
        assert fields(claim["value"], "SD1\n", 5)[3:] == [ready["generation"], "1"]
        assert saved["revision"] == 0
        with worker(native_claimed_worker_binary, seeded, programs, recorded=True, argument=endpoint) as active:
            assert active.request(active.authorize_request())["code"] == "claimed"
            response = active.request(recovery_request())
            assert response["status"] == "ok", response
            recovery = response["recovery"]
            assert recovery["phase"] == "4" and recovery["delivery_receipt"]["revision"] == 3
            assert recovery["claim_generation"] == ready["generation"]
            assert recovery["recovery_generation"] != ready["generation"]
            assert len(recovery["recovery_generation"]) == 64
            assert active.request(recovery_request())["code"] == "claim"
            assert active.request(active.authorize_request())["code"] == "claimed"
        assert len(requests) == 1
    claim, saved = delivery(native_store_binary, root)
    assert claim["revision"] == 2 and saved["revision"] == 1
    assert fields(saved["value"], "DR1\n", 7)[3:] == [ready["generation"], "4", "", ""]
    # The real shared record is interpreted by pi SIGIL, not a Python recovery table.
    # This is still fixture-driven invocation, not the automatic HTTP service.
    with NativeStore(native_store_binary, root, {**row["grants"], "a.delivery": "read"}) as store:
        prior = store.get("a.state", "same-session")
        intent = fields(store.get("a.intent", "a" * 64 + ":1")["value"], "SI1\n", 5)
        current = Decision(prior["value"], intent[2], intent[1], intent[3], intent[4])
        pending = interpret(mcp, programs["turn_transaction"],
            current.result(prior=prior["value"], kind="unknown", now=int(time.time())),
            prior["revision"], prefix="a", key="same-session",
            delivery=saved["value"], delivery_revision=saved["revision"])
        pending.commit(store)
        assert pending.decision.action == "uncertain"
        assert pending.decision.values[14] == "possibly_delivered"


def test_controller_death_after_result_commit_before_reading_ack_preserves_result(
        programs, seeded, native_claimed_worker_binary, native_store_binary, scripted_llm):
    scripted_llm.script = [json.loads(text_reply("Committed before client acknowledgement"))]
    with worker(native_claimed_worker_binary, seeded, programs, recorded=True, argument=scripted_llm.url) as active:
        ready = active.authorize()
        start_without_reading_ack(active, ready)
        readable, _, _ = select.select([active.proc.stdout], [], [], 30)
        assert readable, "result was not made ready for acknowledgement"
        active.kill()  # Do not read or interpret the completion acknowledgement.
    before = delivery(native_store_binary, seeded[0])
    assert fields(before[1]["value"], "DR1\n", 7)[3:6] == [ready["generation"], "2", "returned"]
    with worker(native_claimed_worker_binary, seeded, programs, recorded=True, argument=scripted_llm.url) as active:
        assert active.request(recovery_request())["code"] == "claim"
        assert active.request(active.authorize_request())["code"] == "claimed"
    assert delivery(native_store_binary, seeded[0]) == before
    assert len(scripted_llm.requests) == 1


@pytest.mark.parametrize("field", ["facts", "generation", "observation", "claim_namespace", "phase", "receipt"])
def test_recovery_accepts_no_caller_facts_and_cannot_interrupt_an_active_attempt(
        programs, seeded, native_claimed_worker_binary, field):
    with worker(native_claimed_worker_binary, seeded, programs, recorded=True) as active:
        assert active.request(recovery_request(**{field: "forged"}))["code"] == "protocol"
        ready = active.authorize()
        assert active.request(recovery_request())["code"] == "protocol"
        assert active.request({"op": "cancel", "ticket": ready["ticket"]})["cancelled_before_start"]
        assert active.request(recovery_request())["code"] == "claim"


@pytest.mark.parametrize("kind", ["missing", "tombstone", "wrong_owner_field", "wrong_intent_revision",
    "terminal", "delivery", "delivery_tombstone", "other_tenant", "other_key", "bad_frame"])
def test_recovery_refuses_inconsistent_or_out_of_scope_records_without_mutation(
        programs, seeded, native_claimed_worker_binary, native_store_binary, kind):
    root = seeded[0]
    key = "a" * 64 + ":1"
    original = ["a.intent", key, "1", "prior-owner", "1"]
    if kind == "wrong_owner_field":
        original[0] = "b.intent"
    elif kind == "wrong_intent_revision":
        original[2] = "2"
    elif kind == "terminal":
        original[4] = "2"
    raw = "bad" if kind == "bad_frame" else record("SD1\n", original)
    with NativeStore(native_store_binary, root, {"a.dispatch": "read_write", "a.delivery": "read_write"}) as store:
        if kind != "missing":
            store.commit([mutation("a.dispatch", key, raw)])
        if kind == "tombstone":
            store.commit([mutation("a.dispatch", key, None, 1)])
        if kind in {"delivery", "delivery_tombstone"}:
            store.commit([mutation("a.delivery", key, "retained")])
            if kind == "delivery_tombstone":
                store.commit([mutation("a.delivery", key, None, 1)])
    before = delivery(native_store_binary, root)
    request = recovery_request()
    if kind == "other_tenant":
        request["intent_namespace"] = "b.intent"
    elif kind == "other_key":
        request["key"] = "missing"
    with worker(native_claimed_worker_binary, seeded, programs, recorded=True) as active:
        assert active.request(request)["status"] == "error"
    assert delivery(native_store_binary, root) == before


def test_legacy_manual_protocol_cannot_access_recorded_recovery(programs, seeded, native_claimed_worker_binary):
    with worker(native_claimed_worker_binary, seeded, programs) as active:
        assert active.request(recovery_request())["code"] == "protocol"
