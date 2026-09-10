"""Real v8/SIGIL browser admission, exact accounting and rendered recovery UX."""

import json
import time

import pytest

from api_support import BODY, TOKEN_A, TOKEN_B, credential
from automatic_support import wait_operation
# Native/compiler/browser/provider fixtures come from the shared conftest plugin
# in an isolated run, or ordinary root conftest discovery after integration.
# Re-exporting them here creates duplicate module-local session registrations.
from http_service_support import (effect_counts, state,
                                 http_fixture_secret as http_fixture_secret)
from request_api_checks import counters, only_accounting_changed, window_headroom
from request_browser_support import RequestBrowserApi, browser
from request_host_support import request_host_binary as request_host_binary
from test_automatic_preclaim import retained
from test_turn_execution import programs as programs
from turn_support import fields, text_reply, tool_reply

pytestmark = pytest.mark.usefixtures("http_fixture_secret")


@pytest.fixture(scope="session")
def request_browser_binary(request_host_binary, browser_runtime):
    # No stage-only executable or bypass of native/evaluator/browser prerequisites.
    assert browser_runtime["playwright"]
    return request_host_binary


@pytest.mark.parametrize("mode", ["quota_read", "quota_submit"])
def test_real_browser_rate_refusal_is_explicit_preserves_submission_and_cannot_dispatch(
        request_browser_binary, programs, scripted_llm, tmp_path, mode):
    root, workspace = tmp_path / "service", tmp_path / "workspace"
    workspace.mkdir()
    with RequestBrowserApi(request_browser_binary, root, programs, scripted_llm.url,
                           workspace, limit=1) as api:
        before = state(root)
        window = window_headroom()
        result = browser({"url": "http://" + api.ready["address"], "token": TOKEN_A,
                          "mode": mode, "screenshots": str(tmp_path / "screenshots")})
        expected = [200, 429] if mode == "quota_read" else [200, 429, 429]
        assert result["statuses"] == expected
        assert result["calls"] == len(expected)
        assert result["submissions"] == (0 if mode == "quota_read" else 2)
        assert result["pageErrors"] == result["foreignRequests"] == 0
        assert counters(root) == {"a.budget": (1, "tenant-a", str(window), "1")}
        only_accounting_changed(before, state(root), 1)
        assert effect_counts(root) == {}
    assert scripted_llm.requests == []
    assert (tmp_path / "screenshots/01-connect.png").is_file()
    assert (tmp_path / "screenshots/02-rate-limited.png").is_file()


def test_actual_browser_poll_refusal_keeps_accepted_identity_without_cancelling_or_repeating_work(
        request_browser_binary, programs, scripted_llm, tmp_path):
    root, workspace = tmp_path / "service", tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("The owner is Ada.\n")
    reply = "Ada owns the project, even if browser status reads are rate limited."
    usage = {"input_tokens": 2, "output_tokens": 1}
    scripted_llm.script = [json.loads(tool_reply("read_file", usage=usage)),
                          json.loads(text_reply(reply, usage=usage))]
    with RequestBrowserApi(request_browser_binary, root, programs, scripted_llm.url,
                           workspace, limit=2) as api:
        window = window_headroom()
        result = browser({"url": "http://" + api.ready["address"], "token": TOKEN_A,
                          "mode": "quota_poll", "screenshots": str(tmp_path / "screenshots")})
        assert result["statuses"] == [200, 202, 429, 429, 429, 429]
        assert result["calls"] == 6 and result["submissions"] == 1
        assert result["pageErrors"] == result["foreignRequests"] == 0
        assert counters(root) == {"a.budget": (2, "tenant-a", str(window), "2")}
        # Read-only test observation cannot spend or bypass the API allowance.
        # The browser has NOT observed completion; the independent store check
        # proves its refused reads did not cancel or duplicate accepted work.
        deadline = time.monotonic() + 115
        while time.monotonic() < deadline:
            operations = retained(root, "a.operations")
            assert len(operations) == 1 and operations[0][0] == result["operation"]
            if operations[0][2].startswith("OQ3\n"):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("accepted turn did not publish its terminal record")
        operation = fields(operations[0][2], "OQ3\n", 13)
        assert operation[4] == result["operation"] and operation[9] == "done"
        outcome = fields(operation[11], "OR1\n", 8)
        assert outcome[:5] == [reply, "4", "2", "1", ""] and outcome[7] == "reported"
        assert counters(root) == {"a.budget": (2, "tenant-a", str(window), "2")}
        keys = [result["operation"] + f":{index}" for index in (1, 2, 3)]
        assert [row[0] for row in retained(root, "executor0.claim")] == keys
        assert [row[0] for row in retained(root, "executor0.delivery")] == keys
        assert effect_counts(root) == {"executor0.claim": 3, "executor0.delivery": 3}
    assert len(scripted_llm.requests) == 2
    assert (tmp_path / "screenshots/02-rate-limited-accepted.png").is_file()


def test_real_expiry_clears_retained_conversation_without_work_or_accounting_then_reconnects_tenant(
        request_browser_binary, programs, scripted_llm, tmp_path):
    root, workspace = tmp_path / "service", tmp_path / "workspace"
    workspace.mkdir()
    reply = "ALICE expiry canary: Ada owns this project."
    (workspace / "README.md").write_text("The owner is Ada.\n")
    usage = {"input_tokens": 2, "output_tokens": 1}
    scripted_llm.script = [json.loads(tool_reply("read_file", usage=usage)),
                          json.loads(text_reply(reply, usage=usage))]
    expires = int(time.time()) + 75
    rows = [credential(expires=expires),
            credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    with RequestBrowserApi(request_browser_binary, root, programs, scripted_llm.url,
                           workspace, rows=rows, limit=10000) as api:
        status, accepted = api.request(body=BODY)
        assert status == 202
        completed = wait_operation(api, accepted["operation"])
        assert completed["status"] == "done" and completed["reply"] == reply
        before = state(root)
        before_counter = counters(root)["a.budget"][0]
        effects = effect_counts(root)
        assert effects == {"executor0.claim": 3, "executor0.delivery": 3}
        connected = []

        def expire_after_real_browser_reads():
            # Observe real clock expiry after loading actual retained content.
            # No clock injection, host deadline change, or runtime restart.
            assert time.time() < expires, "credential must be active through initial browser reads"
            assert counters(root)["a.budget"][0] == before_counter + 4
            only_accounting_changed(before, state(root), 4)
            connected.append(state(root))
            observation_deadline = time.monotonic() + 75
            while time.time() < expires:
                assert time.monotonic() < observation_deadline, "real clock did not reach fixture expiry"
                time.sleep(min(0.2, max(0.001, expires - time.time())))
            assert state(root) == connected[0]

        result = browser({"url": "http://" + api.ready["address"], "token": TOKEN_A,
                          "other_token": TOKEN_B, "mode": "expiry_after_connect",
                          "session": BODY["session"], "operation": accepted["operation"],
                          "reply": reply, "screenshots": str(tmp_path / "screenshots")},
                         on_connected=expire_after_real_browser_reads)
        assert len(connected) == 1
        assert result["statuses"] == [200, 200, 200, 200, 401, 200]
        assert result["calls"] == 6 and result["submissions"] == 0
        assert result["pageErrors"] == result["foreignRequests"] == 0
        after = counters(root)
        assert set(after) == {"a.budget", "b.budget"}
        assert after["a.budget"][0] == before_counter + 4
        assert after["b.budget"][0] == 1 and after["b.budget"][1] == "tenant-b"
        assert after["b.budget"][3] == "1"
        only_accounting_changed(connected[0], state(root), 1)
        only_accounting_changed(before, state(root), 5)
        assert effect_counts(root) == effects
    assert len(scripted_llm.requests) == 2
    for name in ["01-connect", "02-retained-conversation", "03-expired", "04-other-tenant"]:
        assert (tmp_path / f"screenshots/{name}.png").is_file()
