"""Fixed native worker mechanism, not authenticated HTTP or artifact provenance."""

import hashlib
import json
import time

import pytest

from conftest import API_KEY, SIGIL_ROOT, needs_toolchain
from scripts.compose_application import compose_application
from test_turn_execution import hanging_provider, provider_grants, provider_input
from sigil_compose import compose_with_stdlib
from conftest import PI_ROOT
from turn_support import event, fields, text_reply
from worker_support import NativeWorker


@pytest.fixture(scope="module")
def program():
    needs_toolchain()
    return compose_application("turn", SIGIL_ROOT).text


def test_native_worker_runs_verified_sigil_and_rejects_ticket_replay(native_worker_binary, program):
    with NativeWorker(native_worker_binary, program) as worker:
        payload = event()
        prepared = worker.prepare(payload)
        assert prepared["input_sha256"] == hashlib.sha256(payload.encode()).hexdigest()
        result = worker.execute(prepared)
        assert result["fault"] is None and result["worker_reaped"] and result["request_may_have_run"]
        assert result["generation"] == prepared["generation"]
        assert result["result"]["status"] == "ok", result
        assert fields(result["result"]["data"]["output_text"], "PD1\n", 5)[1] == "model"
        assert worker.request({"op": "execute", "ticket": prepared["ticket"]})["code"] == "ticket"


@pytest.mark.parametrize("raw", [
    '{"op":"prepare","input":"","fuel":10,"timeout_ms":1000,"grants":{"fs":["/"]}}',
    '{"op":"prepare","input":"","fuel":10,"timeout_ms":1000,"source":"replacement"}',
    '{"op":"prepare","input":"a","input":"b","fuel":10,"timeout_ms":1000}',
    '{"op":"prepare","input":"","fuel":true,"timeout_ms":1000}',
    '{"op":"prepare","input":"","fuel":1.0,"timeout_ms":1000}',
    '{"op":"execute","ticket":"wrong","input":"replacement"}',
    '{"op":"prepare","input":"","fuel":10,"timeout_ms":0}',
    '[]', '{}', '{} {}',
])
def test_request_cannot_change_artifact_grants_or_ambiguous_fields(native_worker_binary, program, raw):
    with NativeWorker(native_worker_binary, program) as worker:
        assert worker.raw(raw)["status"] == "error"


def test_cancelled_and_expired_preparations_never_forge(native_worker_binary, program):
    with NativeWorker(native_worker_binary, program) as worker:
        prepared = worker.prepare(event())
        assert worker.request({"op": "cancel", "ticket": prepared["ticket"]})["cancelled_before_start"]
        assert worker.request({"op": "execute", "ticket": prepared["ticket"]})["code"] == "ticket"
        expired = worker.prepare(event(), timeout_ms=1)
        time.sleep(0.02)
        result = worker.execute(expired)
        assert result["fault"] == "deadline" and not result["request_may_have_run"]
        assert result["worker_reaped"]


def test_native_hard_timeout_kills_real_sigil_after_provider_receives_one_request(native_worker_binary, scripted_llm):
    needs_toolchain()
    provider = compose_with_stdlib((PI_ROOT / "tools/agent_turn.sigil").read_text(), ["http"], SIGIL_ROOT).text
    with NativeWorker(native_worker_binary, provider, grants=provider_grants()) as worker:
        with hanging_provider() as (endpoint, received, requests):
            prepared = worker.prepare(provider_input(endpoint, "{}"), timeout_ms=1500)
            result = worker.execute(prepared)
            assert received.is_set() and len(requests) == 1
            assert result["fault"] == "deadline" and result["worker_reaped"] and result["request_may_have_run"]
            assert worker.request({"op": "execute", "ticket": prepared["ticket"]})["code"] == "ticket"
            assert API_KEY not in json.dumps(result)
        scripted_llm.script = [json.loads(text_reply("recovered worker"))]
        fresh = worker.prepare(provider_input(scripted_llm.url, "{}"))
        recovered = worker.execute(fresh)
        assert recovered["fault"] is None and recovered["result"]["status"] == "ok", recovered
        assert fresh["generation"] != prepared["generation"] and recovered["worker_reaped"]
    assert len(requests) == 1


def test_input_and_source_are_frozen_before_ticket_execution(native_worker_binary, program):
    from pathlib import Path
    with NativeWorker(native_worker_binary, program) as worker:
        prepared = worker.prepare(event())
        assert worker.request({"op": "execute", "ticket": prepared["ticket"], "input": "changed"})["code"] == "invalid"
        # The prepared artifact is the byte-checked in-memory source, not a file
        # reloaded after a caller has durably claimed its ticket.
        Path(worker.temp.name, "program.sigil").write_text("invalid replacement source")
        result = worker.execute(prepared)
        assert result["fault"] is None and result["result"]["status"] == "ok", result


def test_native_secret_injection_stays_outside_guest_and_next_worker_recovers(native_worker_binary, scripted_llm):
    needs_toolchain()
    provider = compose_with_stdlib((PI_ROOT / "tools/agent_turn.sigil").read_text(), ["http"], SIGIL_ROOT).text
    scripted_llm.script = [json.loads(text_reply("first")), json.loads(text_reply("second"))]
    with NativeWorker(native_worker_binary, provider, grants=provider_grants()) as worker:
        identities = []
        for _ in range(2):
            prepared = worker.prepare(provider_input(scripted_llm.url, "{}"))
            result = worker.execute(prepared)
            assert result["fault"] is None and result["result"]["status"] == "ok", result
            assert result["worker_reaped"] and API_KEY not in json.dumps(result)
            identities.append(result["generation"])
        assert identities[0] != identities[1]
    assert len(scripted_llm.requests) == 2
