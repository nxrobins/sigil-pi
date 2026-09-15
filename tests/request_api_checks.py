"""Real v8 API checks, imported where original native fixtures are registered."""

from contextlib import closing
import json
import sqlite3
import time

from api_support import BODY, TOKEN_A, TOKEN_B, credential
from automatic_support import wait_operation
from conftest import API_KEY, PI_ROOT
from http_service_support import configure as configure_v7, effect_counts, state
from request_api_support import RequestApi, configure
from test_automatic_cancellation import terminal_control
from test_turn_execution import hanging_provider
from turn_support import fields, text_reply, tool_reply


def window_headroom():
    # Coordinate this non-load boundary test with the real epoch window; do not
    # inject a fake host clock or change any existing execution deadline.
    left = 60 - time.time() % 60
    if left < 30:
        time.sleep(left + 0.05)
    return int(time.time()) // 60 * 60


def counters(root):
    with closing(sqlite3.connect(f"file:{root / 'records/records.sqlite'}?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT namespace,revision,value FROM records WHERE key='request-window' ORDER BY namespace").fetchall()
    return {namespace: (revision, *fields(bytes(value).decode(), "RW1\n", 3))
            for namespace, revision, value in rows}


def only_accounting_changed(before, after, increments):
    allowed = {("a.budget", "request-window"), ("b.budget", "request-window")}
    assert [(row[0], row[2]) for row in before[0]] == [(row[0], row[2]) for row in after[0]]
    assert len(before[0]) == len(after[0]) == 1
    assert after[0][0][1] == before[0][0][1] + increments
    assert [row for row in after[1] if row[:2] not in allowed] == [row for row in before[1] if row[:2] not in allowed]


def test_v8_configuration_preserves_native_and_effect_limits_and_adds_only_a_grantless_policy(programs, tmp_path):
    root, workspace = tmp_path / "service", tmp_path / "workspace"
    workspace.mkdir()
    before, _ = configure_v7(root, programs, "http://127.0.0.1:1", workspace)
    after, _ = configure(root, programs, "http://127.0.0.1:1", workspace, limit=2)
    assert after["version"] == 8
    for key in before.keys() - {"version", "worker", "functions"}:
        assert after[key] == before[key], key
    for key in before["worker"].keys() - {"source", "source_sha256"}:
        assert after["worker"][key] == before["worker"][key], key
    assert {key: value for key, value in after["functions"].items() if key != "request_policy"} == before["functions"]
    policy = after["functions"]["request_policy"]
    for key in before["worker"].keys() - {"source", "source_sha256"}:
        assert policy[key] == before["worker"][key], key
    assert policy["net"] == policy["fs"] == [] and policy["secret_env"] == {}
    assert policy["max_timeout_ms"] == 15000


def test_real_requests_commit_accounting_before_route_errors_and_stop_at_the_limit(
        request_host_binary, programs, scripted_llm, tmp_path):
    root, workspace = tmp_path / "service", tmp_path / "workspace"
    workspace.mkdir()
    with RequestApi(request_host_binary, root, programs, scripted_llm.url, workspace, limit=2) as api:
        before = state(root)
        for token, code in [(None, "authentication_required"), ("unknown", "invalid_credential")]:
            status, headers, body = api.exchange("POST", "/v1/operations", token=token, raw=b"{")
            assert status == 401 and json.loads(body) == {"error": {"code": code}}
            assert "x-request-id" in dict(headers)
            assert state(root) == before
        window = window_headroom()
        status, _, body = api.exchange("GET", "/unknown")
        assert status == 404 and json.loads(body) == {"error": {"code": "not_found"}}
        assert counters(root) == {"a.budget": (1, "tenant-a", str(window), "1")}
        only_accounting_changed(before, state(root), 1)
        status, _, body = api.exchange("POST", "/v1/operations", raw=b"{")
        assert status == 400 and json.loads(body) == {"error": {"code": "invalid_request"}}
        assert counters(root) == {"a.budget": (2, "tenant-a", str(window), "2")}
        only_accounting_changed(before, state(root), 2)
        exhausted = state(root)
        status, headers, body = api.exchange("GET", "/v1/sessions")
        assert status == 429 and json.loads(body) == {"error": {"code": "rate_limited"}}
        assert 1 <= int(dict(headers)["retry-after"]) <= 60
        assert state(root) == exhausted
        assert effect_counts(root) == {}
    assert scripted_llm.requests == []


def test_request_quota_survives_restart_and_credential_rotation_without_cross_tenant_reset(
        request_host_binary, programs, scripted_llm, tmp_path):
    root, workspace = tmp_path / "service", tmp_path / "workspace"
    workspace.mkdir()
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    with RequestApi(request_host_binary, root, programs, scripted_llm.url, workspace, rows=rows, limit=2) as api:
        window = window_headroom()
        for _ in range(2):
            assert api.exchange("GET", "/unknown")[0] == 404
        assert counters(root)["a.budget"] == (2, "tenant-a", str(window), "2")
        retained = state(root)
    rotated = "rotated-credential-canary-" + "r" * 40
    rows[0] = credential(rotated)
    with RequestApi(request_host_binary, root, programs, scripted_llm.url, workspace, rows=rows, limit=2, mode="open") as api:
        assert state(root) == retained
        assert api.exchange("GET", "/v1/sessions", token=TOKEN_A)[0] == 401
        assert state(root) == retained
        status, headers, body = api.exchange("GET", "/v1/sessions", token=rotated)
        assert status == 429 and json.loads(body) == {"error": {"code": "rate_limited"}}
        assert "retry-after" in dict(headers) and state(root) == retained
        assert api.exchange("GET", "/v1/sessions", token=TOKEN_B)[0] == 200
        assert counters(root) == {"a.budget": (2, "tenant-a", str(window), "2"),
                                  "b.budget": (1, "tenant-b", str(window), "1")}
        only_accounting_changed(retained, state(root), 1)
        assert effect_counts(root) == {}
    assert scripted_llm.requests == []


def test_all_route_scope_denials_count_requests_without_parsing_bodies_or_changing_domain_state(
        request_host_binary, programs, scripted_llm, tmp_path):
    root, workspace = tmp_path / "service", tmp_path / "workspace"
    workspace.mkdir()
    inventory = json.loads((PI_ROOT / "config/api-migration.json").read_text())
    legacy = inventory["legacy_routes"]
    assert len(legacy) == 11
    operation = "a" * 64
    routes = [(row["method"], row["sample_path"]) for row in legacy] + [
        ("POST", "/v1/operations"), ("GET", "/v1/operations/" + operation),
        ("POST", "/v1/operations/" + operation + "/cancel"),
        ("GET", "/v1/sessions"), ("GET", "/v1/sessions/same-session/messages?limit=0"),
    ]
    with RequestApi(request_host_binary, root, programs, scripted_llm.url, workspace,
                    rows=[credential(scopes=[])], limit=10000) as api:
        before = state(root)
        for count, (method, path) in enumerate(routes, 1):
            status, headers, body = api.exchange(method, path, raw=b"{")
            assert (status, json.loads(body)) == (403, {"error": {"code": "permission_denied"}}), (method, path)
            assert "x-request-id" in dict(headers) and "retry-after" not in dict(headers)
            assert set(counters(root)) == {"a.budget"}
            assert counters(root)["a.budget"][0] == count
            only_accounting_changed(before, state(root), count)
        assert effect_counts(root) == {}
    assert scripted_llm.requests == []


def test_rate_admitted_cancellation_after_delivery_preserves_uncertainty_across_restart(
        request_host_binary, programs, tmp_path):
    root, workspace = tmp_path / "service", tmp_path / "workspace"
    workspace.mkdir()
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    with hanging_provider() as (endpoint, arrived, requests):
        with RequestApi(request_host_binary, root, programs, endpoint, workspace, rows=rows, limit=10000) as api:
            status, accepted = api.request()
            assert status == 202
            assert arrived.wait(60), "the actual provider request must arrive before cancellation"
            path = accepted["status_url"] + "/cancel"
            assert api.request("POST", path, body={}, token=TOKEN_B)[0] == 404
            status, requested = api.request("POST", path, body={})
            assert status == 202 and requested["cancellation_status"] == "requested"
            result = wait_operation(api, accepted["operation"])
            assert result["status"] == "uncertain" and result["usage"]["known"] is False
            assert result["accounting"] == "unknown"
            before = state(root)
            assert api.request("GET", accepted["status_url"], token=TOKEN_B)[0] == 404
            assert api.request("GET", "/v1/sessions/same-session/messages", token=TOKEN_B)[0] == 404
            status, page = api.request("GET", "/v1/sessions", token=TOKEN_B)
            assert status == 200 and page["sessions"] == []
            only_accounting_changed(before, state(root), 3)
            counts = effect_counts(root)
            assert counts == {"executor0.claim": 1, "executor0.delivery": 1}
            retained = state(root)
        with RequestApi(request_host_binary, root, programs, endpoint, workspace,
                        rows=rows, limit=10000, mode="open") as api:
            assert state(root) == retained
            assert api.request("GET", accepted["status_url"])[1] == result
            assert api.request("POST", path, body={}) == (200, terminal_control(result))
            assert api.request()[1] == {**accepted, "replayed": True}
            only_accounting_changed(retained, state(root), 3)
            assert effect_counts(root) == counts
        assert len(requests) == 1


def test_real_rate_admitted_tool_turn_followup_replay_and_restart_fit_the_unchanged_host(
        request_host_binary, programs, scripted_llm, tmp_path):
    root, workspace = tmp_path / "service", tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("The owner is Ada.\n")
    usage = {"input_tokens": 2, "output_tokens": 1}
    scripted_llm.script = [json.loads(tool_reply("read_file", usage=usage)),
        json.loads(text_reply("Ada owns this project.", usage=usage)),
        json.loads(text_reply("Yes, Ada is still the owner.", usage=usage))]
    # Explicit high fixture allowance isolates the turn lifecycle; the separate
    # tests above exercise exhaustion. This is not a pilot limit approval.
    with RequestApi(request_host_binary, root, programs, scripted_llm.url, workspace, limit=10000) as api:
        status, first = api.request(body=BODY)
        assert status == 202 and first["replayed"] is False
        result = wait_operation(api, first["operation"])
        assert result["status"] == "done" and result["reply"] == "Ada owns this project."
        assert result["usage"] == {"input_tokens": 4, "output_tokens": 2, "known": True}
        followup_body = {**BODY, "message": "Who owns it again?", "submission_key": "follow-up"}
        status, followup = api.request(body=followup_body)
        # This requires the additional prior-operation read: the real native
        # host still permits only eight entry evaluations, not a test override.
        assert status == 202 and followup["operation"] != first["operation"]
        final = wait_operation(api, followup["operation"])
        assert final["status"] == "done" and final["reply"] == "Yes, Ada is still the owner."
        assert api.request("GET", "/v1/operations/" + first["operation"])[1] == result
        # Existing browser-facing read/control routes keep their real entry
        # paths; accounting must not make them exceed the native step ceiling.
        before_reads = state(root)
        status, history = api.request("GET", "/v1/sessions/same-session/messages")
        assert status == 200 and len(history["messages"]) == 6
        assert history["messages"][2]["content"][0]["text"] == "The owner is Ada.\n"
        assert history["messages"][-1]["content"][0]["text"] == final["reply"]
        status, listing = api.request("GET", "/v1/sessions")
        assert status == 200 and [item["session"] for item in listing["sessions"]] == ["same-session"]
        assert listing["sessions"][0]["state_revision"] == history["state_revision"]
        assert api.request("POST", followup["status_url"] + "/cancel", body={}) == (200, terminal_control(final))
        only_accounting_changed(before_reads, state(root), 3)
        before = state(root)
        before_counter = counters(root)["a.budget"][0]
        assert api.request(body=BODY)[1] == {**first, "replayed": True}
        assert api.request(body=followup_body)[1] == {**followup, "replayed": True}
        assert api.request(body={**BODY, "message": "different payload"})[0] == 409
        only_accounting_changed(before, state(root), 3)
        assert counters(root)["a.budget"][0] == before_counter + 3
        counts = effect_counts(root)
        assert counts == {"executor0.claim": 4, "executor0.delivery": 4}
        before_restart = state(root)
    with RequestApi(request_host_binary, root, programs, scripted_llm.url, workspace, limit=10000, mode="open") as api:
        assert state(root) == before_restart
        assert api.request("GET", "/v1/operations/" + first["operation"])[1] == result
        assert api.request("GET", "/v1/operations/" + followup["operation"])[1] == final
        assert api.request(body=BODY)[1] == {**first, "replayed": True}
        assert api.request("GET", "/v1/sessions/same-session/messages") == (200, history)
        assert api.request("GET", "/v1/sessions") == (200, listing)
        assert api.request("POST", followup["status_url"] + "/cancel", body={}) == (200, terminal_control(final))
        only_accounting_changed(before_restart, state(root), 6)
        assert effect_counts(root) == counts
    assert len(scripted_llm.requests) == 3
    assert scripted_llm.requests[1]["messages"][-1]["content"][0]["content"] == "The owner is Ada.\n"
    assert "Ada owns this project." in json.dumps(scripted_llm.requests[2]["messages"])
    assert API_KEY not in json.dumps(scripted_llm.requests) + json.dumps(result) + json.dumps(final) + json.dumps(history)
