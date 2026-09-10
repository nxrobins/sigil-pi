"""Real HTTP/SIGIL checks; these do not claim a migrated legacy route."""
import json
import re
import subprocess

import pytest

from api_support import BODY, TOKEN_A, TOKEN_B, credential
from automatic_support import wait_operation
from conftest import API_KEY
from http_service_support import (HttpApi, configure, effect_counts,
                                  http_fixture_secret as http_fixture_secret,
                                  http_service_binary as http_service_binary, state)
from test_turn_execution import programs as programs
from turn_support import text_reply, tool_reply


pytestmark = pytest.mark.usefixtures("http_fixture_secret")


def correlation(pairs, expected=None):
    values = [value for key, value in pairs if key == "x-request-id"]
    assert len(values) == 1
    if expected is None:
        assert re.fullmatch("[0-9a-f]{32}", values[0])
    else:
        assert values == [expected]
    headers = dict(pairs)
    assert headers["content-type"] == "application/json"
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["connection"] == "close"
    assert not ({"set-cookie", "location", "access-control-allow-origin"} & headers.keys())
    return values[0]


def test_actual_http_correlation_preserves_first_value_without_state_or_authority(
        http_service_binary, programs, scripted_llm, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    with HttpApi(http_service_binary, root, programs, scripted_llm.url, workspace) as api:
        before = state(root)
        fresh = []
        for hints, expected in [([], None), ([b""], None), ([b"short"], None),
                ([b"invalid space"], None), ([b"\xff"], None), ([b"x" * 65], None),
                ([b"first-valid-id", b"second-valid-id"], "first-valid-id"),
                ([b"bad", b"second-valid-id"], None), ([b"._:-Ab09"], "._:-Ab09"),
                ([b"x" * 64], "x" * 64), ([b"valid-id"] * 60, "valid-id")]:
            status, pairs, raw = api.exchange(hints=hints)
            assert status == 200
            assert json.loads(raw) == {"sessions": [], "next_after": None,
                                      "order": "session_name", "consistency": "live_scan"}
            chosen = correlation(pairs, expected)
            if expected is None:
                fresh.append(chosen)
        assert len(set(fresh)) == len(fresh)
        # The native HTTP transport counts ALL fields: this client contributes
        # Host, Content-Type, Authorization and Content-Length before its hints.
        # Its existing 64-field cap precedes the separate 64-hint RF1 mechanism.
        for count in [61, 64, 65]:
            status, pairs, raw = api.exchange(hints=[b"valid-id"] * count)
            assert status == 431
            assert "x-request-id" not in dict(pairs)
            assert TOKEN_A.encode() not in raw and API_KEY.encode() not in raw
            assert state(root) == before
        for token in [None, "invalid-credential-canary"]:
            status, pairs, raw = api.exchange("POST", "/v1/operations", token=token,
                body={**BODY, "request_id": TOKEN_A}, hints=[b"caller-refusal-id"],
                extra_headers=[("Cookie", "credential=" + TOKEN_A)])
            assert status == 401
            correlation(pairs, "caller-refusal-id")
            assert TOKEN_A.encode() not in raw and API_KEY.encode() not in raw
        status, pairs, raw = api.exchange("POST", "/v1/operations",
            body={**BODY, "request_id": "forged-body-id"}, hints=[b"actual-header-id"])
        assert status == 400 and b"forged-body-id" not in raw
        correlation(pairs, "actual-header-id")
        # The wrapper does not fabricate a migrated readiness route.
        status, pairs, raw = api.exchange(path="/v1/health", hints=[b"health-trace-id"])
        assert status == 501
        correlation(pairs, "health-trace-id")
        assert state(root) == before
    assert scripted_llm.requests == []


def test_http_labels_do_not_change_durable_turn_replay_tenant_or_restart_semantics(
        http_service_binary, programs, scripted_llm, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("The owner is Ada.\n")
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    usage = {"input_tokens": 2, "output_tokens": 1}
    scripted_llm.script = [json.loads(tool_reply("read_file", usage=usage)),
        json.loads(text_reply("ALICE Ada canary.", usage=usage)),
        json.loads(text_reply("ALICE follow-up canary.", usage=usage)),
        json.loads(text_reply("BOB private canary.", usage=usage))]
    root = tmp_path / "service"
    bodies = [BODY, {**BODY, "message": "Who owns it?", "submission_key": "followup"}, BODY]
    tokens = [TOKEN_A, TOKEN_A, TOKEN_B]
    results = []
    with HttpApi(http_service_binary, root, programs, scripted_llm.url, workspace, rows=rows) as api:
        for body, token in zip(bodies, tokens):
            status, pairs, raw = api.exchange("POST", "/v1/operations", body=body,
                token=token, hints=[b"identical-trace-label"])
            assert status == 202
            correlation(pairs, "identical-trace-label")
            accepted = json.loads(raw)
            assert re.fullmatch("[0-9a-f]{64}", accepted["operation"])
            result = wait_operation(api, accepted["operation"], token=token)
            assert result["status"] == "done"
            results.append(result)
        assert len({result["operation"] for result in results}) == 3
        assert len(scripted_llm.requests) == 4
        assert scripted_llm.requests[1]["messages"][-1]["content"][0]["content"] == "The owner is Ada.\n"
        assert "ALICE Ada canary." in json.dumps(scripted_llm.requests[2])
        assert "ALICE" not in json.dumps(scripted_llm.requests[3])
        pages = {token: api.request("GET", "/v1/sessions/same-session/messages", token=token, raw=b"")
                 for token in [TOKEN_A, TOKEN_B]}
        assert pages[TOKEN_A][0] == pages[TOKEN_B][0] == 200
        assert "ALICE follow-up canary." in json.dumps(pages[TOKEN_A]) and "BOB" not in json.dumps(pages[TOKEN_A])
        assert "BOB private canary." in json.dumps(pages[TOKEN_B]) and "ALICE" not in json.dumps(pages[TOKEN_B])
        before = state(root)
        for body, token, original in zip(bodies, tokens, results):
            status, pairs, raw = api.exchange("POST", "/v1/operations", body=body, token=token,
                hints=[b"changed-replay-label"])
            assert status == 202
            correlation(pairs, "changed-replay-label")
            replay = json.loads(raw)
            assert replay["replayed"] and replay["operation"] == original["operation"]
        assert api.request(body={**BODY, "message": "different payload"}, hints=[b"identical-trace-label"])[0] == 409
        assert api.request("GET", "/v1/operations/" + results[0]["operation"], token=TOKEN_B, raw=b"")[0] == 404
        assert state(root) == before
        counts = effect_counts(root)
        assert counts == {"executor0.claim": 4, "executor0.delivery": 4,
                          "executor1.claim": 1, "executor1.delivery": 1}
    with HttpApi(http_service_binary, root, programs, scripted_llm.url, workspace, rows=rows, mode="open") as api:
        for body, token, original in zip(bodies, tokens, results):
            assert api.request("GET", "/v1/operations/" + original["operation"], token=token, raw=b"")[1] == original
            status, replay = api.request(body=body, token=token, hints=[b"restart-replay-label"])
            assert status == 202 and replay["replayed"] and replay["operation"] == original["operation"]
        for token in [TOKEN_A, TOKEN_B]:
            assert api.request("GET", "/v1/sessions/same-session/messages", token=token, raw=b"") == pages[token]
        assert state(root) == before and effect_counts(root) == counts
    assert len(scripted_llm.requests) == 4


@pytest.mark.parametrize("older", ["frozen-v6", "frozen-v4"])
def test_actual_older_hosts_reject_new_http_profile_before_state_opening(
        legacy_http_service_binary, legacy_native_release_service_binary,
        programs, scripted_llm, tmp_path, older):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    _, path = configure(root, programs, scripted_llm.url, workspace)
    binary = legacy_http_service_binary if older == "frozen-v6" else legacy_native_release_service_binary
    result = subprocess.run([str(binary), "init", str(path), "0"],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and result.stdout == ""
    assert json.loads(result.stderr) == {"status": "error", "code": "config"}
    assert not (root / "records").exists()
    assert scripted_llm.requests == []


@pytest.mark.parametrize("change", ["v6-with-http", "missing-http", "null-http", "unknown-http",
                                   "duplicate-name", "security-header", "missing-name", "extra-name"])
def test_new_profile_refuses_unadmitted_configuration_before_state_creation(
        http_service_binary, programs, scripted_llm, tmp_path, change):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    config, path = configure(root, programs, scripted_llm.url, workspace)
    if change == "v6-with-http":
        config["version"] = 6
    elif change == "missing-http":
        del config["http"]
    elif change == "null-http":
        config["http"] = None
    elif change == "unknown-http":
        config["http"]["allow_all"] = True
    else:
        names = config["http"]["response_headers"]
        if change == "duplicate-name":
            names.append("x-request-id")
        elif change == "security-header":
            names.append("x-frame-options")
        elif change == "missing-name":
            names.remove("x-request-id")
        else:
            names.append("x-extra-admitted-name")
    path.write_text(json.dumps(config))
    result = subprocess.run([str(http_service_binary), "init", str(path), "0"],
        capture_output=True, text=True, timeout=40)
    assert result.returncode == 2 and result.stdout == ""
    assert json.loads(result.stderr) == {"status": "error",
        "code": "application" if change in {"missing-name", "extra-name"} else "config"}
    assert not (root / "records").exists()
    assert scripted_llm.requests == []


def test_v6_profile_stays_compatible_between_actual_old_and_current_host(
        http_service_binary, legacy_http_service_binary, programs, scripted_llm, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    scripted_llm.script = [json.loads(text_reply("Retained v6 answer.",
        usage={"input_tokens": 2, "output_tokens": 1}))]
    with HttpApi(legacy_http_service_binary, root, programs, scripted_llm.url, workspace, version=6) as api:
        accepted_status, accepted = api.request()
        assert accepted_status == 202
        original = wait_operation(api, accepted["operation"])
        assert original["status"] == "done" and original["reply"] == "Retained v6 answer."
        status, pairs, body = api.exchange(hints=[b"ignored-old-profile"])
        assert status == 200 and "x-request-id" not in dict(pairs)
        before = state(root)
    with HttpApi(http_service_binary, root, programs, scripted_llm.url, workspace, version=6, mode="open") as api:
        got_status, got_pairs, got_body = api.exchange(hints=[b"still-ignored-v6-id"])
        assert (got_status, got_body) == (status, body)
        assert "x-request-id" not in dict(got_pairs)
        assert api.request("GET", "/v1/operations/" + original["operation"], raw=b"")[1] == original
        replay_status, replay = api.request(hints=[b"new-host-old-profile"])
        assert replay_status == 202 and replay["replayed"] and replay["operation"] == original["operation"]
        assert state(root) == before
        assert effect_counts(root) == {"executor0.claim": 1, "executor0.delivery": 1}
    assert len(scripted_llm.requests) == 1
