"""Actual HTTP/SIGIL/owned-worker cancellation, without a fixture step driver."""
import json

from api_support import TOKEN_B, credential
from automatic_support import service, wait_operation
from conftest import API_KEY
from test_automatic_preclaim import retained
from test_turn_execution import hanging_provider, programs as programs
from turn_support import fields, text_reply


def terminal_control(result):
    return {"operation": result["operation"], "status": result["status"], "cancellation_status": "terminal",
            "status_url": "/v1/operations/" + result["operation"]}


def test_cancel_before_claim_survives_restart_without_an_effect_or_double_release(
        native_release_service_binary, programs, scripted_llm, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "permitted"
    workspace.mkdir()
    root = tmp_path / "service"
    with service(native_release_service_binary, root, programs, scripted_llm.url, workspace) as api:
        status, accepted = api.request()
        assert status == 202
        path = "/v1/operations/" + accepted["operation"] + "/cancel"
        status, request = api.request("POST", path, body={})
        assert status == 202 and request["status"] == "accepted" and request["cancellation_requested"]
    assert retained(root, "executor0.claim") == retained(root, "executor0.delivery") == []
    with service(native_release_service_binary, root, programs, scripted_llm.url, workspace, mode="open") as api:
        result = wait_operation(api, accepted["operation"])
        assert result["status"] == "cancelled" and result["error"] == "cancelled"
        assert result["usage"] == {"input_tokens": 0, "output_tokens": 0, "known": True}
        assert result["accounting"] == "reported"
        assert api.request("POST", path, body={}) == (200, terminal_control(result))
        assert api.request()[1]["operation"] == accepted["operation"]
    with service(native_release_service_binary, root, programs, scripted_llm.url, workspace, mode="open") as api:
        assert api.request("GET", accepted["status_url"])[1] == result
    assert not scripted_llm.requests
    assert retained(root, "executor0.claim") == retained(root, "executor0.delivery") == []
    assert fields(retained(root, "a.budget")[0][2], "BH1\n", 5)[2:] == ["0", "0", "0"]


def test_public_cancel_during_a_sent_request_preserves_uncertainty_and_other_tenant_access(
        native_release_service_binary, programs, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    root, workspace = tmp_path / "service", tmp_path / "permitted"
    workspace.mkdir()
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    with hanging_provider() as (endpoint, arrived, requests):
        with service(native_release_service_binary, root, programs, endpoint, workspace, rows=rows) as api:
            accepted = api.request()[1]
            assert arrived.wait(60), "the real provider request did not arrive"
            path = "/v1/operations/" + accepted["operation"] + "/cancel"
            assert api.request("POST", path, body={}, token=TOKEN_B)[0] == 404
            status, requested = api.request("POST", path, body={})
            assert status == 202 and requested["cancellation_status"] == "requested"
            result = wait_operation(api, accepted["operation"])
            assert result["status"] == "uncertain" and result["usage"]["known"] is False
            assert result["accounting"] == "unknown"
            assert api.request("GET", accepted["status_url"], token=TOKEN_B)[0] == 404
        with service(native_release_service_binary, root, programs, endpoint, workspace, rows=rows, mode="open") as api:
            assert api.request("GET", accepted["status_url"])[1] == result
            assert api.request("POST", path, body={}) == (200, terminal_control(result))
        assert len(requests) == 1


def test_cancel_after_observed_final_answer_does_not_rewrite_the_result(
        native_release_service_binary, programs, scripted_llm, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "permitted"
    workspace.mkdir()
    root = tmp_path / "service"
    scripted_llm.script = [json.loads(text_reply("Already finished.", usage={"input_tokens": 5, "output_tokens": 2}))]
    with service(native_release_service_binary, root, programs, scripted_llm.url, workspace) as api:
        accepted = api.request()[1]
        result = wait_operation(api, accepted["operation"])
        assert result["status"] == "done"
        assert api.request("POST", accepted["status_url"] + "/cancel", body={}) == (200, terminal_control(result))
        assert api.request("GET", accepted["status_url"])[1] == result
    with service(native_release_service_binary, root, programs, scripted_llm.url, workspace,
                 rows=[credential(scopes=["chat"])], mode="open") as api:
        assert api.request("GET", accepted["status_url"])[0] == 403
        status, control = api.request("POST", accepted["status_url"] + "/cancel", body={})
        assert (status, control) == (200, terminal_control(result))
        assert "Already finished." not in json.dumps(control) and "reply" not in control and "usage" not in control
    assert len(scripted_llm.requests) == 1
    assert [r[0] for r in retained(root, "a.operations")] == [accepted["operation"]]
