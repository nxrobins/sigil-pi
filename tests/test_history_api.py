"""Actual SIGIL history API using the production-native development mechanisms."""
from contextlib import closing
import json
import sqlite3

import pytest

from api_support import TOKEN_A, TOKEN_B, credential
from automatic_support import service as history_service, wait_operation
from conftest import API_KEY, SIGIL_ROOT, needs_toolchain
from scripts.compose_application import compose_application
from test_turn_execution import hanging_provider, programs as programs
from turn_support import text_reply, tool_reply


def get_history(api, suffix="", *, token=TOKEN_A, session="same-session", body=None):
    return api.request("GET", f"/v1/sessions/{session}/messages" + suffix, token=token,
                       raw=b"" if body is None else json.dumps(body).encode())


def state_rows(root):
    with closing(sqlite3.connect(f"file:{root / 'records/records.sqlite'}?mode=ro", uri=True)) as db:
        return db.execute("SELECT namespace,key,revision,value FROM records ORDER BY namespace,key").fetchall()


def test_history_api_composition_fits_the_unchanged_source_limit():
    needs_toolchain()
    built = compose_application("api", SIGIL_ROOT)
    assert 0 < len(built.text.encode()) <= 65536
    assert built.text.count("fn history_api(") == built.text.count("fn history_parameters(") == 1
    assert built.text.count("pub fn tool_main(") == 1


def test_actual_http_history_preserves_tool_turn_followup_and_restart(
        native_release_service_binary, programs, scripted_llm, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("The project owner is Ada.\n")
    usage = {"input_tokens": 2, "output_tokens": 1}
    scripted_llm.script = [json.loads(tool_reply("read_file", usage=usage)),
                          json.loads(text_reply("Ada owns the project.", usage=usage)),
                          json.loads(text_reply("Yes, Ada.", usage=usage))]
    root = tmp_path / "service"
    with history_service(native_release_service_binary, root, programs, scripted_llm.url, workspace) as api:
        assert get_history(api)[0] == 404
        status, accepted = api.request()
        assert status == 202
        assert wait_operation(api, accepted["operation"])["status"] == "done"
        status, first = get_history(api, "?limit=2")
        assert status == 200 and first["next_offset"] == 2
        assert first["messages"][0]["content"][0]["text"] == "Read README.md"
        assert first["messages"][1]["content"][0]["type"] == "tool_call"
        assert first["messages"][1]["content"][0]["origin"] == "model_request"
        revision = first["state_revision"]
        status, second = get_history(api, f"?revision={revision}&offset=2&limit=2")
        assert status == 200 and second["next_offset"] is None
        assert second["messages"][0]["content"][0]["text"] == "The project owner is Ada.\n"
        assert second["messages"][1]["content"][0]["text"] == "Ada owns the project."
        before = state_rows(root)
        assert get_history(api, "?limit=2")[1] == first
        assert state_rows(root) == before, "history retrieval wrote product state"
        status, followup = api.request(body={"session": "same-session", "message": "Who again?", "submission_key": "followup"})
        assert status == 202 and wait_operation(api, followup["operation"])["status"] == "done"
        assert get_history(api, f"?revision={revision}&offset=2") == (409, {"error": {"code": "history_changed"}})
        status, final = get_history(api)
        assert status == 200 and len(final["messages"]) == 6
        assert final["messages"][-2]["content"][0]["text"] == "Who again?"
        assert final["messages"][-1]["content"][0]["text"] == "Yes, Ada."
        assert API_KEY not in json.dumps(final)
    with history_service(native_release_service_binary, root, programs, scripted_llm.url, workspace, mode="open") as api:
        assert get_history(api) == (200, final)
        assert api.request()[1]["operation"] == accepted["operation"]
    assert len(scripted_llm.requests) == 3


def test_actual_history_is_tenant_shared_but_operation_results_remain_owner_scoped(
        native_release_service_binary, programs, scripted_llm, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    viewer = "same-tenant-history-viewer-" + "v" * 40
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b"),
            credential(viewer, principal="viewer", scopes=["sessions:read"])]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    canary = "ALICE-tenant-A-history-931"
    other_canary = "BOB-tenant-B-history-814"
    usage = {"input_tokens": 1, "output_tokens": 1}
    scripted_llm.script = [json.loads(text_reply(canary, usage=usage)),
                          json.loads(text_reply(other_canary, usage=usage))]
    root = tmp_path / "service"
    with history_service(native_release_service_binary, root, programs, scripted_llm.url, workspace, rows=rows) as api:
        status, accepted = api.request()
        assert status == 202 and wait_operation(api, accepted["operation"])["reply"] == canary
        assert get_history(api, token=TOKEN_B) == (404, {"error": {"code": "session_not_found"}})
        status, other = api.request(token=TOKEN_B)
        assert status == 202 and other["operation"] != accepted["operation"]
        assert wait_operation(api, other["operation"], token=TOKEN_B)["reply"] == other_canary
        status, isolated = get_history(api, token=TOKEN_B)
        assert status == 200 and isolated["messages"][-1]["content"][0]["text"] == other_canary
        assert canary not in json.dumps(isolated)
        assert get_history(api, token="unknown-credential")[0] == 401
        assert get_history(api, token=None)[0] == 401
        status, shared = get_history(api, token=viewer)
        assert status == 200 and shared["messages"][-1]["content"][0]["text"] == canary
        assert other_canary not in json.dumps(shared)
        assert api.request("GET", "/v1/operations/" + accepted["operation"], token=viewer)[0] == 404
        assert api.request("POST", "/v1/operations/" + accepted["operation"] + "/cancel", token=viewer, body={})[0] == 403
        before = state_rows(root)
        assert get_history(api, body={"tenant": "tenant-b", "session": "forged", "observation": "forged", "command": "commit"}) == (200, shared)
        assert state_rows(root) == before
    assert len(scripted_llm.requests) == 2


def test_actual_history_query_errors_are_bounded_and_never_start_work(
        native_release_service_binary, programs, scripted_llm, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    invalid = ["?", "?limit=0", "?limit=51", "?limit=01", "?limit=1&limit=2", "?limit=1&",
               "?offset=0", "?offset=1", "?revision=0", "?revision=-1", "?revision=9223372036854775808",
               "?limit=1e1", "?limit=%31", "?limit=1=2", "?cursor=anything", "?revision=1&offset=32769"]
    with history_service(native_release_service_binary, root, programs, scripted_llm.url, workspace) as api:
        before = state_rows(root)
        for query in invalid:
            assert get_history(api, query) == (400, {"error": {"code": "invalid_history_request"}}), query
        for session in ["%2e%2e", "../another", "bad:key", "a" * 129]:
            assert get_history(api, session=session)[0] == 404, session
        assert get_history(api, "?offset=0&limit=1&revision=1")[0] == 404
        assert state_rows(root) == before
    assert scripted_llm.requests == []


def test_chat_scope_does_not_grant_history_even_when_query_is_invalid(
        native_release_service_binary, programs, scripted_llm, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with history_service(native_release_service_binary, tmp_path / "service", programs,
                         scripted_llm.url, workspace, rows=[credential(scopes=["chat"])]) as api:
        assert get_history(api)[0] == 403
        assert get_history(api, "?limit=0")[0] == 403
    assert scripted_llm.requests == []


@pytest.mark.parametrize("unicode_message", [False, True], ids=["ascii", "utf8"])
def test_largest_admitted_user_message_is_readable_through_actual_http(
        native_release_service_binary, programs, tmp_path, monkeypatch, unicode_message):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    message = ("é" * 131072 if unicode_message else
               "retained-user-canary-" + "x" * (262144 - len("retained-user-canary-")))
    assert len(message.encode()) == 262144  # existing inclusive submission bound
    root = tmp_path / "service"
    with hanging_provider() as (endpoint, _, _requests):
        with history_service(native_release_service_binary, root, programs, endpoint, workspace) as api:
            status, accepted = api.request(body={"session": "large-session", "message": message,
                                                "submission_key": "large-key"})
            assert status == 202, accepted
            status, page = api.request("GET", "/v1/sessions/large-session/messages?limit=1", raw=b"")
            assert status == 200, page
            assert page["operation"] == accepted["operation"]
            assert page["messages"] == [{"index": 0, "role": "user", "content": [{"type": "text", "text": message}]}]
            assert page["next_offset"] is None and page["history_kind"] == "retained_context"
            assert API_KEY not in json.dumps(page)
        with history_service(native_release_service_binary, root, programs, endpoint, workspace, mode="open") as api:
            status, reopened = api.request("GET", "/v1/sessions/large-session/messages?limit=1", raw=b"")
            assert status == 200 and reopened["messages"] == page["messages"]
            assert reopened["operation"] == accepted["operation"]
    # No fixture state, outcome, quota, grant, fuel or deadline is substituted.
    # A changing execution phase is not a claim that history lookup dispatched it.
