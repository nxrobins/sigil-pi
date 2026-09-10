"""Inject public cancellation at actual committed automatic-turn boundaries.

The read-only observer chooses injection timing. It never writes application
state, steps the coordinator or supplies an effect/claim/result to the service.
"""
import json
import time

import pytest

from automatic_support import service, wait_operation
from conftest import API_KEY
from test_automatic_preclaim import retained
from test_turn_execution import programs as programs
from turn_support import fields, tool_reply


def wait_committed_boundary(root, operation, phase, sequence):
    until = time.monotonic() + 100
    last = None
    while time.monotonic() < until:
        rows = retained(root, "a.state")
        matching = [row for row in rows if row[0] == "same-session"]
        assert len(matching) == 1, rows
        state = fields(matching[0][2], "PT1\n", 17)
        assert state[0] == operation
        last = state[1:3]
        if last == [phase, str(sequence)]:
            # The cancellation, not deadline expiry, must prevent the next action.
            assert time.time() < int(state[13])
            return state
        assert int(state[2]) < sequence or last == [phase, str(sequence)], last
        assert state[1] in {"model", "tool"}, state
        time.sleep(0.01)
    raise AssertionError(f"automatic service did not commit {phase}/{sequence}: {last}")


@pytest.mark.parametrize("phase,sequence", [("tool", 2), ("model", 3)])
def test_cancel_after_observed_action_preserves_usage_and_prevents_next_dispatch(
        native_release_service_binary, programs, scripted_llm, tmp_path, monkeypatch, phase, sequence):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "permitted"
    workspace.mkdir()
    canary = "boundary-file-observation-canary"
    (workspace / "README.md").write_text(canary)
    scripted_llm.script = [json.loads(tool_reply("read_file", usage={"input_tokens": 7, "output_tokens": 3}))]
    root = tmp_path / "service"
    with service(native_release_service_binary, root, programs, scripted_llm.url, workspace) as api:
        status, accepted = api.request()
        assert status == 202, accepted
        snapshot = wait_committed_boundary(root, accepted["operation"], phase, sequence)
        assert snapshot[10:13] == ["7", "3", "1"]
        if phase == "model":
            assert canary in snapshot[5]
        status, requested = api.request("POST", accepted["status_url"] + "/cancel", body={})
        assert status == 202 and requested["cancellation_status"] == "requested", (status, requested)
        result = wait_operation(api, accepted["operation"])
        assert result["status"] == "cancelled" and result["error"] == "cancelled", result
        assert result["usage"] == {"input_tokens": 7, "output_tokens": 3, "known": True}
        assert result["accounting"] == "reported"
    expected = [accepted["operation"] + ":" + str(index) for index in range(1, sequence)]
    assert [row[0] for row in retained(root, "executor0.claim")] == expected
    assert [row[0] for row in retained(root, "executor0.delivery")] == expected
    assert len(scripted_llm.requests) == 1
    assert fields(retained(root, "a.budget")[0][2], "BH1\n", 5)[2:] == ["0", "0", "0"]
    with service(native_release_service_binary, root, programs, scripted_llm.url, workspace, mode="open") as api:
        assert api.request("GET", accepted["status_url"])[1] == result
        assert api.request()[1]["operation"] == accepted["operation"]
    assert [row[0] for row in retained(root, "executor0.claim")] == expected
    assert [row[0] for row in retained(root, "executor0.delivery")] == expected
    assert len(scripted_llm.requests) == 1
    assert fields(retained(root, "a.budget")[0][2], "BH1\n", 5)[2:] == ["0", "0", "0"]
