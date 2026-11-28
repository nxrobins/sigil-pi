"""Real HTTP/SIGIL/owned-worker audit lifecycle, without a fixture step driver."""
import hashlib
import json
from pathlib import Path
import shutil
import time

import pytest

from api_support import TOKEN_A, TOKEN_B, credential
from automatic_audit_support import AutomaticAuditApi, audit_key, effect_audit_key, events
from automatic_support import wait_operation
from conftest import API_KEY
from http_service_support import state
from readiness_host_support import ROOT as STAGE, readiness_host_binary as readiness_host_binary
from store_support import NativeStore, mutation
from test_automatic_audit import audit_secrets as audit_secrets
from test_automatic_preclaim import retained
from test_turn_execution import hanging_provider, programs as programs
from turn_support import text_reply, tool_reply


@pytest.fixture(autouse=True)
def effect_secrets(monkeypatch):
    for index in range(2):
        monkeypatch.setenv(f"PI_AUTOMATIC_EFFECT_AUDIT_FIXTURE_KEY_{index}", effect_audit_key(index))


def logged(root, config, index=0):
    return events(root, config, index, effects=True)


def test_actual_model_tool_response_claims_and_observations_are_correlated_and_retained(
        readiness_host_binary, programs, scripted_llm, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("Private project owner: Ada.\n")
    usage = {"input_tokens": 1, "output_tokens": 1}
    scripted_llm.script = [json.loads(tool_reply("read_file", usage=usage)),
                          json.loads(text_reply("Ada owns it.", usage=usage)),
                          json.loads(text_reply("Still Ada.", usage=usage))]
    root = tmp_path / "service"
    with AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace, audit_effects=True) as api:
        assert logged(root, api.config) == []
        code, accepted = api.request()
        assert code == 202
        done = wait_operation(api, accepted["operation"])
        assert done["status"] == "done" and done["reply"] == "Ada owns it."
        audit = logged(root, api.config)
        assert [e["event"] for e in audit] == ["prepared", "observed"] * 3
        assert [e["phase"] for e in audit] == [1, 2] * 3
        assert [e["step"] for e in audit] == [1, 1, 2, 2, 3, 3]
        assert {e["operation"] for e in audit} == {accepted["operation"]}
        assert {e["principal"] for e in audit} == {"alice"}
        assert {e["tenant"] for e in audit} == {"tenant-a"}
        participant = api.config["automatic"]["participants"][0]
        for pair, alias in zip([audit[:2], audit[2:4], audit[4:6]], ["provider", "reader", "provider"]):
            configured = participant["effects"][alias]
            first, observed = pair
            assert first["claim_generation"] == observed["claim_generation"] == observed["observer_generation"]
            assert first["prepared"] == observed["prepared"]
            decision = first["dispatch_policy"]
            assert decision == observed["dispatch_policy"]
            policy = configured["policy"]
            assert decision["source"] == policy["worker"]["source_sha256"]
            assert decision["runtime"] == policy["worker"]["runtime_sha256"]
            assert decision["alias"] == alias
            assert decision["read_count"] == sum(item["kind"] == "read" for item in policy["inputs"])
            assert decision["selected_fuel"] == policy["worker"]["max_fuel"]
            assert 0 < decision["selected_timeout_ms"] <= policy["worker"]["max_timeout_ms"]
            assert decision["input_clock"] <= decision["after_clock"] <= first["recording_clock_before"]
            assert 0 <= decision["elapsed_ms"] < policy["worker"]["max_timeout_ms"]
            assert decision["input_bytes"] > 4 and decision["output_bytes"] > 4
            for name in ["bound_config_sha256", "input_sha256", "output_sha256", "read_set_sha256", "context_sha256"]:
                assert len(decision[name]) == 64 and set(decision[name]) <= set("0123456789abcdef")
            assert first["effect"] == observed["effect"] == {
                "source": configured["worker"]["source_sha256"], "runtime": configured["worker"]["runtime_sha256"],
                "net": configured["worker"]["net"], "fs": configured["worker"]["fs"],
                "secret_names": sorted(configured["worker"]["secret_env"])}
            assert first["recording_storage_grants"] == observed["recording_storage_grants"] == configured["grants"]
            assert first["retained_output_sha256"] is None and first["request_may_have_run"] == 0
            assert observed["request_may_have_run"] == observed["worker_reaped"] == 1
            assert observed["runtime_status"] == "ok" and len(observed["retained_output_sha256"]) == 64
            assert all(not key.startswith("effects") and not key.startswith("audit") for key in first["recording_storage_grants"])
        assert audit[3]["retained_output_sha256"] == hashlib.sha256(b"Private project owner: Ada.\n").hexdigest()
        assert len(events(root, api.config, 0)) == 4
        assert api.request()[1]["replayed"]
        assert logged(root, api.config) == audit
        following = api.request(body={"session": "same-session", "message": "Who owns it?", "submission_key": "next"})[1]
        assert wait_operation(api, following["operation"])["reply"] == "Still Ada."
        final = logged(root, api.config)
        assert len(final) == 8 and final[:6] == audit
        assert {e["operation"] for e in final[6:]} == {following["operation"]}
        assert [e["step"] for e in final[6:]] == [1, 1]
        decisions = [event["dispatch_policy"] for event in final[::2]]
        # Same held provider policy, different actual state/input evaluations.
        assert decisions[0]["bound_config_sha256"] == decisions[2]["bound_config_sha256"] == decisions[3]["bound_config_sha256"]
        assert decisions[1]["bound_config_sha256"] != decisions[0]["bound_config_sha256"]
        assert len({decision["input_sha256"] for decision in decisions}) == 4
        assert len({decision["read_set_sha256"] for decision in decisions}) == 4
        assert all(e["schema"] == "sigil-pi/worker-lifecycle-audit/v1" for e in final)
    text = json.dumps(final)
    assert all(value not in text for value in [API_KEY, audit_key(0), effect_audit_key(0), "Ada", "same-session"])
    with AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace, audit_effects=True, mode="open") as api:
        assert api.request("GET", accepted["status_url"])[1] == done
        assert logged(root, api.config) == final
        assert api.request()[1]["replayed"]
    assert len(scripted_llm.requests) == 3


def test_two_tenants_cannot_cross_effect_audit_history_or_authority(
        readiness_host_binary, programs, scripted_llm, tmp_path):
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    with AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace, rows=rows, audit_effects=True) as api:
        assert api.request(token="invalid")[0] == 401
        for index, token in enumerate([TOKEN_A, TOKEN_B]):
            scripted_llm.script.append(json.loads(text_reply("private reply", usage={"input_tokens": 1, "output_tokens": 1})))
            accepted = api.request(token=token)[1]
            assert wait_operation(api, accepted["operation"], token=token)["status"] == "done"
            assert api.request("GET", accepted["status_url"], token=TOKEN_B if index == 0 else TOKEN_A)[0] == 404
            audit = logged(root, api.config, index)
            assert len(audit) == 2 and {e["operation"] for e in audit} == {accepted["operation"]}
            assert {e["tenant"] for e in audit} == {"tenant-b" if index else "tenant-a"}
            assert all(e["intent_namespace"] == ("b.intent" if index else "a.intent") for e in audit)
            assert all(e["claim_namespace"] == f"executor{index}.claim" for e in audit)
        assert logged(root, api.config, 0)[0]["chain"] != logged(root, api.config, 1)[0]["chain"]
        assert logged(root, api.config, 0)[0]["dispatch_policy"]["bound_config_sha256"] != logged(root, api.config, 1)[0]["dispatch_policy"]["bound_config_sha256"]
    assert len(scripted_llm.requests) == 2


@pytest.mark.parametrize("cancel", [False, True])
def test_sent_request_restart_or_cancel_has_truthful_correlated_audit(
        readiness_host_binary, programs, tmp_path, cancel):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    with hanging_provider() as (endpoint, arrived, requests):
        with AutomaticAuditApi(readiness_host_binary, root, programs, endpoint, workspace, audit_effects=True) as api:
            accepted = api.request()[1]
            assert arrived.wait(15)
            claim = logged(root, api.config)
            assert len(claim) == 1 and claim[0]["event"] == "prepared"
            assert claim[0]["dispatch_policy"]["alias"] == "provider"
            if cancel:
                code, control = api.request("POST", accepted["status_url"] + "/cancel", body={})
                assert code == 202 and control["cancellation_status"] == "requested"
                result = wait_operation(api, accepted["operation"])
                assert result["status"] == "uncertain"
        with AutomaticAuditApi(readiness_host_binary, root, programs, endpoint, workspace, audit_effects=True, mode="open") as api:
            result = wait_operation(api, accepted["operation"])
            assert result["status"] == "uncertain" and result["usage"]["known"] is False
            audit = logged(root, api.config)
            assert len(audit) == 2 and audit[0] == claim[0]
            assert audit[1]["phase"] == 4 and audit[1]["request_may_have_run"] == 1
            assert audit[1]["claim_generation"] == audit[0]["claim_generation"]
            assert audit[1]["operation"] == accepted["operation"]
            assert audit[1]["retained_output_sha256"] is None
            if cancel:
                assert audit[1]["event"] == "observed" and audit[1]["worker_reaped"] == 1
                assert audit[1]["dispatch_policy"] == audit[0]["dispatch_policy"]
                # The pinned HTTP shim has an independent two-second timeout.
                # Public acknowledgement means requested, not that cancellation
                # beat an already observed transport error. Neither observation
                # may be rewritten to the other or into a successful delivery.
                assert (audit[1]["fault"], audit[1]["runtime_status"]) in {
                    ("cancelled", ""), ("", "error")}
                assert audit[1]["observer_generation"] == audit[0]["claim_generation"]
            else:
                assert audit[1]["event"] == "abandoned" and audit[1]["worker_reaped"] == 0
                assert audit[1]["dispatch_policy"] is None
                assert audit[1]["observer_generation"] != audit[0]["claim_generation"]
                assert audit[1]["effect"] is audit[1]["prepared"] is None
        assert len(requests) == 1


def test_uncancelled_provider_timeout_retains_runtime_error_not_a_cancellation_claim(
        readiness_host_binary, programs, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    with hanging_provider() as (endpoint, arrived, requests):
        with AutomaticAuditApi(readiness_host_binary, root, programs, endpoint, workspace, audit_effects=True) as api:
            code, accepted = api.request()
            assert code == 202 and arrived.wait(15)
            done = wait_operation(api, accepted["operation"])
            assert done["status"] == "uncertain" and done["usage"]["known"] is False
            audit = logged(root, api.config)
            assert [e["event"] for e in audit] == ["prepared", "observed"]
            assert audit[1]["phase"] == 4 and audit[1]["runtime_status"] == "error"
            assert audit[1]["fault"] == "" and audit[1]["retained_output_sha256"] is None
            assert audit[1]["request_may_have_run"] == audit[1]["worker_reaped"] == 1
            assert audit[0]["claim_generation"] == audit[1]["observer_generation"]
            assert len(requests) == 1


@pytest.mark.parametrize("kind", ["wrong_key", "historical_corruption", "head_tombstone", "orphan"])
def test_effect_audit_restart_refuses_bad_history_before_new_dispatch(
        readiness_host_binary, programs, scripted_llm, tmp_path, monkeypatch, kind):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    scripted_llm.script = [json.loads(text_reply("done", usage={"input_tokens": 1, "output_tokens": 1}))]
    with AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace, audit_effects=True) as api:
        accepted = api.request()[1]
        assert wait_operation(api, accepted["operation"])["status"] == "done"
        assert len(logged(root, api.config)) == 2
        audit = api.config["automatic"]["participants"][0]["effect_audit"]
    if kind == "wrong_key":
        monkeypatch.setenv(audit["key_env"], "different-native-effect-audit-key-0123456789")
    else:
        with NativeStore(STAGE / "native/store/target/debug/sigil-store", root / "records",
                         {audit["heads"]: "read_write", audit["records"]: "read_write"}) as store:
            namespace = audit["heads"] if kind == "head_tombstone" else audit["records"]
            key = audit["chain"] if kind == "head_tombstone" else audit["chain"] + (
                ".0000000000000000" if kind == "historical_corruption" else ".0000000000000007")
            record = store.get(namespace, key)
            store.commit([mutation(namespace, key, None if kind == "head_tombstone" else "corrupted with valid Store checksum", record["revision"])])
    before = state(root)
    with pytest.raises(AssertionError, match="audit_verification"):
        AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace, audit_effects=True, mode="open")
    assert state(root) == before and len(scripted_llm.requests) == 1


@pytest.mark.parametrize("kind", ["skip", "empty", "runtime_changed"])
def test_failed_claim_audit_never_starts_the_actual_provider(
        readiness_host_binary, programs, scripted_llm, tmp_path, kind):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    def patch(c):
        worker = c["automatic"]["participants"][0]["effect_audit"]["worker"]
        if kind == "runtime_changed":
            copy = root / "private-effect-audit-runtime"
            shutil.copy2(worker["runtime"], copy)
            worker["runtime"] = str(copy)
        else:
            source = Path(worker["source"])
            body = source.read_text()
            needle = 'put(result, 0, text("publish")); put(result, 1, out);'
            assert body.count(needle) == 1
            replacement = 'put(result, 0, text("none")); put(result, 1, text(""));' if kind == "skip" else 'put(result, 0, text("publish")); put(result, 1, text(""));'
            body = body.replace(needle, replacement)
            source.write_text(body)
            worker["source_sha256"] = hashlib.sha256(body.encode()).hexdigest()
    with AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace, audit_effects=True, patch=patch) as api:
        if kind == "runtime_changed":
            runtime = Path(api.config["automatic"]["participants"][0]["effect_audit"]["worker"]["runtime"])
            replacement = root / "invalid-runtime"
            replacement.write_text("not the admitted runtime")
            replacement.chmod(0o700)
            replacement.replace(runtime)
        accepted = api.request()[1]
        until = time.monotonic() + 3
        while time.monotonic() < until:
            status = api.request("GET", accepted["status_url"])[1]
            assert status["status"] not in {"done", "uncertain"}
            time.sleep(0.05)
        assert retained(root, "executor0.claim") == retained(root, "executor0.delivery") == []
        assert logged(root, api.config) == []
    assert scripted_llm.requests == []
