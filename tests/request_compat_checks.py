"""Actual old/new executable compatibility; no claim of v7-to-v8 state migration."""

import json
import subprocess

from api_support import BODY, TOKEN_B, credential
from automatic_support import wait_operation
from http_service_support import HttpApi, effect_counts, state
from request_api_support import configure
from test_http_service import correlation
from turn_support import text_reply, tool_reply


def refuse_v8(binary, root, programs, endpoint, workspace, *, mode, rows=None):
    _, path = configure(root, programs, endpoint, workspace, rows, limit=2)
    result = subprocess.run([str(binary), mode, str(path), "0"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and result.stdout == ""
    assert json.loads(result.stderr) == {"status": "error", "code": "config"}


def test_actual_frozen_v7_rejects_request_admission_before_state_creation(
        legacy_request_service_binary, programs, scripted_llm, tmp_path):
    root, workspace = tmp_path / "service", tmp_path / "workspace"
    workspace.mkdir()
    refuse_v8(legacy_request_service_binary, root, programs, scripted_llm.url, workspace, mode="init")
    assert not (root / "records").exists()
    assert scripted_llm.requests == []


def test_unchanged_v7_profile_retains_tool_turn_and_new_followup_across_both_host_directions(
        legacy_request_service_binary, request_host_binary, programs, scripted_llm, tmp_path):
    root, workspace = tmp_path / "service", tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("The retained owner is Ada.\n")
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    usage = {"input_tokens": 2, "output_tokens": 1}
    scripted_llm.script = [json.loads(tool_reply("read_file", usage=usage)),
        json.loads(text_reply("The retained owner is Ada.", usage=usage)),
        json.loads(text_reply("Ada remains the owner after the host change.", usage=usage))]
    with HttpApi(legacy_request_service_binary, root, programs, scripted_llm.url, workspace, rows=rows) as api:
        original_config = api.config
        status, first = api.request()
        assert status == 202
        result = wait_operation(api, first["operation"])
        assert result["status"] == "done" and result["reply"] == "The retained owner is Ada."
        assert result["usage"] == {"input_tokens": 4, "output_tokens": 2, "known": True}
        history = api.request("GET", "/v1/sessions/same-session/messages", raw=b"")
        listing = api.request("GET", "/v1/sessions", raw=b"")
        assert history[0] == listing[0] == 200
        status, headers, _ = api.exchange(hints=[b"older-v7-trace"])
        assert status == 200
        correlation(headers, "older-v7-trace")
        original = state(root)
        counts = effect_counts(root)
        assert counts == {"executor0.claim": 3, "executor0.delivery": 3}

    # This time refusal is exercised against a real populated store, not just
    # a missing directory. The refused configuration cannot open/rewrite it.
    refuse_v8(legacy_request_service_binary, root, programs, scripted_llm.url, workspace, mode="open", rows=rows)
    assert state(root) == original and effect_counts(root) == counts
    assert len(scripted_llm.requests) == 2

    followup_body = {**BODY, "message": "Who owns it now?", "submission_key": "new-host-followup"}
    with HttpApi(request_host_binary, root, programs, scripted_llm.url, workspace, rows=rows, mode="open") as api:
        # The actual newer executable runs the identical old configuration and
        # application; there is no v8 opt-in, request counter, or bundle rebase.
        assert api.config == original_config and state(root) == original
        assert api.request("GET", first["status_url"], raw=b"")[1] == result
        assert api.request("GET", "/v1/sessions/same-session/messages", raw=b"") == history
        assert api.request("GET", "/v1/sessions", raw=b"") == listing
        status, headers, body = api.exchange("POST", "/v1/operations", body=BODY,
                                            hints=[b"new-host-v7-trace"])
        assert status == 202 and json.loads(body) == {**first, "replayed": True}
        correlation(headers, "new-host-v7-trace")
        assert api.request("GET", first["status_url"], token=TOKEN_B, raw=b"")[0] == 404
        assert api.request("GET", "/v1/sessions/same-session/messages", token=TOKEN_B, raw=b"")[0] == 404
        assert state(root) == original and effect_counts(root) == counts

        status, followup = api.request(body=followup_body)
        assert status == 202 and followup["operation"] != first["operation"]
        final = wait_operation(api, followup["operation"])
        assert final["status"] == "done" and final["reply"] == "Ada remains the owner after the host change."
        history_after = api.request("GET", "/v1/sessions/same-session/messages", raw=b"")
        listing_after = api.request("GET", "/v1/sessions", raw=b"")
        assert history_after[0] == listing_after[0] == 200
        assert len(history_after[1]["messages"]) == 6
        after = state(root)
        counts_after = effect_counts(root)
        assert counts_after == {"executor0.claim": 4, "executor0.delivery": 4}

    with HttpApi(legacy_request_service_binary, root, programs, scripted_llm.url, workspace, rows=rows, mode="open") as api:
        assert api.config == original_config and state(root) == after
        assert api.request("GET", first["status_url"], raw=b"")[1] == result
        assert api.request("GET", followup["status_url"], raw=b"")[1] == final
        assert api.request("GET", "/v1/sessions/same-session/messages", raw=b"") == history_after
        assert api.request("GET", "/v1/sessions", raw=b"") == listing_after
        assert api.request(body=followup_body)[1] == {**followup, "replayed": True}
        assert state(root) == after and effect_counts(root) == counts_after
    assert len(scripted_llm.requests) == 3
    assert scripted_llm.requests[1]["messages"][-1]["content"][0]["content"] == "The retained owner is Ada.\n"
    assert "The retained owner is Ada." in json.dumps(scripted_llm.requests[2]["messages"])
