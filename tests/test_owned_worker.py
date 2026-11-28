"""Actual SIGIL policy/recording with an owned worker, not an automatic HTTP loop."""
import json
import time

import pytest

from test_native_dispatch import seeded as seeded, worker
from test_recorded_worker import delivery
from test_turn_execution import hanging_provider, programs as programs
from turn_support import fields, text_reply


def start(active):
    response = active.request(active.authorize_request())
    assert response["status"] == "ok", response
    return response["started"]


def poll(active, started):
    return active.request({"op": "poll", "generation": started["generation"]})


def finish(active, started):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        result = poll(active, started)
        assert result["status"] == "ok", result
        if result["completion"] is not None:
            return result["completion"]
        time.sleep(0.02)
    pytest.fail("owned worker did not produce a terminal observation")


def test_owned_public_start_uses_actual_sigil_policy_claim_and_result(
        programs, seeded, native_claimed_worker_binary, native_store_binary, scripted_llm):
    scripted_llm.script = [json.loads(text_reply("Owned worker observed this"))]
    with worker(native_claimed_worker_binary, seeded, programs, recorded=True, owned=True,
                argument=scripted_llm.url) as active:
        ready = start(active)
        assert ready["claim_receipt"]["revision"] == 2
        assert fields(ready["context"], "DX1\n", 5) == ["a" * 64, "1", "alice", "tenant-a", "b" * 64]
        # A lane remains busy even if the worker finished but its result was not
        # yet collected; a later start cannot discard that result channel.
        assert active.request(active.authorize_request())["code"] == "busy"
        assert active.request({"op": "poll", "generation": "forged"})["code"] == "ticket"
        assert active.request({"op": "cancel", "ticket": "forged"})["code"] == "ticket"
        result = finish(active, ready)
        assert not result["owner_lost"] and result["context"] == ready["context"]
        completion = result["completion"]
        assert completion["claim_receipt"] == ready["claim_receipt"]
        assert completion["phase"] == "2" and completion["delivery_receipt"]["revision"] == 3
        assert completion["recording_error"] is None and completion["refused"] is None
        seen = completion["observation"]
        assert seen["generation"] == ready["generation"]
        assert seen["request_may_have_run"] and seen["worker_reaped"] and seen["fault"] is None
        payload = seen["result"]["data"]["output_text"]
        assert poll(active, ready)["code"] == "ticket"
        assert active.request(active.authorize_request())["code"] == "claimed"
        assert active.request({"op": "get", "namespace": "a.delivery", "key": "a" * 64 + ":1"})["code"] == "storage"
    claim, saved = delivery(native_store_binary, seeded[0])
    assert claim["revision"] == 2 and saved["revision"] == 1
    assert fields(saved["value"], "DR1\n", 7)[3:] == [ready["generation"], "2", "returned", payload]
    assert len(scripted_llm.requests) == 1


def test_owned_worker_keeps_scoped_reads_and_cancellation_available_while_provider_is_held(
        programs, seeded, native_claimed_worker_binary, native_store_binary):
    with hanging_provider() as (endpoint, arrived, requests):
        with worker(native_claimed_worker_binary, seeded, programs, recorded=True, owned=True,
                    argument=endpoint) as active:
            ready = start(active)
            assert arrived.wait(5), "worker never reached controlled provider"
            assert len(requests) == 1
            assert poll(active, ready)["completion"] is None
            retained = active.get("a.dispatch", "a" * 64 + ":1")
            assert retained["revision"] == 1
            assert fields(retained["value"], "SD1\n", 5)[3:] == [ready["generation"], "1"]
            assert active.request({"op": "get", "namespace": "a.state", "key": "same-session"})["code"] == "storage"
            assert active.request({"op": "cancel", "ticket": ready["generation"]})["cancellation_requested"]
            result = finish(active, ready)["completion"]
            assert result["phase"] == "4" and result["delivery_receipt"] is not None
            assert result["recording_error"] is None
            assert result["observation"]["request_may_have_run"] and result["observation"]["worker_reaped"]
            assert result["observation"]["fault"] == "cancelled"
        with worker(native_claimed_worker_binary, seeded, programs, recorded=True, owned=True,
                    argument=endpoint) as active:
            assert active.request(active.authorize_request())["code"] == "claimed"
        assert len(requests) == 1
    _, saved = delivery(native_store_binary, seeded[0])
    assert fields(saved["value"], "DR1\n", 7)[4:] == ["4", "", ""]


@pytest.mark.parametrize("kind", ["observation", "receipt", "phase", "payload", "scope", "clock",
                                 "claim", "execute", "run", "recover", "commit"])
def test_owned_adapter_never_accepts_supplied_results_or_bypass_commands(
        programs, seeded, native_claimed_worker_binary, scripted_llm, kind):
    scripted_llm.script = [json.loads(text_reply("Still natively observed"))]
    with worker(native_claimed_worker_binary, seeded, programs, recorded=True, owned=True,
                argument=scripted_llm.url) as active:
        ready = start(active)
        request = {"op": "poll", "generation": ready["generation"], kind: "forged"}
        if kind in {"claim", "execute", "run", "recover", "commit"}:
            request = {
                "claim": {"op": "claim", "batch": "forged"},
                "execute": {"op": "execute", "ticket": ready["generation"]},
                "run": {"op": "run", "ticket": ready["generation"]},
                "recover": {"op": "recover", "intent_namespace": "a.intent", "key": "a" * 64 + ":1"},
                "commit": {"op": "commit", "checks": [], "writes": []},
            }[kind]
        assert active.request(request)["code"] == "protocol"
        result = finish(active, ready)["completion"]
        assert result["phase"] == "2" and result["delivery_receipt"] is not None
    assert len(scripted_llm.requests) == 1
