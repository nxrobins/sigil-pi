"""HTTP-only qualification of automatic never-claimed terminal publication."""
from contextlib import closing
import json
import sqlite3
import time

import pytest

from api_support import credential
from automatic_support import service, wait_operation
from conftest import API_KEY
from test_turn_execution import programs as programs
from turn_support import fields, tool_reply


def retained(root, namespace):
    with closing(sqlite3.connect(f"file:{root / 'records/records.sqlite'}?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT key,revision,value FROM records WHERE namespace=? ORDER BY key", [namespace]).fetchall()
        return [(key, revision, None if value is None else value.decode("utf-8")) for key, revision, value in rows]


def test_expiry_before_claim_across_restart_publishes_a_never_dispatched_terminal_result(
        native_release_service_binary, programs, scripted_llm, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "permitted"
    workspace.mkdir()
    root = tmp_path / "service"
    rows = [credential(turn_seconds=10)]
    with service(native_release_service_binary, root, programs, scripted_llm.url, workspace, rows=rows) as api:
        status, accepted = api.request()
        assert status == 202, accepted
    assert retained(root, "executor0.claim") == []
    assert retained(root, "executor0.delivery") == []
    op = fields(retained(root, "a.operations")[0][2], "OQ2\n", 11)
    until = int(op[7])
    delay = until + 1 - time.time()
    assert delay < 12
    if delay > 0:
        time.sleep(delay)
    with service(native_release_service_binary, root, programs, scripted_llm.url, workspace, rows=rows, mode="open") as api:
        result = wait_operation(api, accepted["operation"])
        assert result["status"] == "failed" and result["error"] == "deadline_exceeded"
        assert result["usage"] == {"input_tokens": 0, "output_tokens": 0, "known": True}
        assert result["accounting"] == "reported"
        status, duplicate = api.request()
        assert status == 202 and duplicate["replayed"] and duplicate["operation"] == accepted["operation"]
    assert not scripted_llm.requests
    assert retained(root, "executor0.claim") == retained(root, "executor0.delivery") == []
    assert fields(retained(root, "a.budget")[0][2], "BH1\n", 5)[2:] == ["0", "0", "0"]
    with service(native_release_service_binary, root, programs, scripted_llm.url, workspace, rows=rows, mode="open") as api:
        assert api.request("GET", "/v1/operations/" + accepted["operation"])[1] == result
    assert not scripted_llm.requests


@pytest.mark.parametrize("usage,error,accounting,hold", [
    (None, "usage_unknown", "unknown", ["0", "20000", "4096"]),
    ({"input_tokens": 20000, "output_tokens": 1}, "quota_exhausted", "reported", ["0", "0", "0"])])
def test_refused_next_model_is_finalized_without_replay_or_invented_delivery(
        native_release_service_binary, programs, scripted_llm, tmp_path, monkeypatch, usage, error, accounting, hold):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "permitted"
    workspace.mkdir()
    (workspace / "README.md").write_text("Read through the allowed file lane only.")
    scripted_llm.script = [json.loads(tool_reply("read_file", usage=usage))]
    root = tmp_path / "service"
    with service(native_release_service_binary, root, programs, scripted_llm.url, workspace) as api:
        status, accepted = api.request()
        assert status == 202, accepted
        result = wait_operation(api, accepted["operation"])
        assert result["status"] == "failed" and result["error"] == error
        assert result["accounting"] == accounting
        assert result["usage"] == {"input_tokens": 0 if usage is None else usage["input_tokens"],
                                   "output_tokens": 0 if usage is None else usage["output_tokens"],
                                   "known": usage is not None}
        assert api.request("GET", "/v1/operations/" + accepted["operation"])[1] == result
    # The initial model and permitted file completed; the next model never claimed.
    keys = [accepted["operation"] + ":1", accepted["operation"] + ":2"]
    assert [r[0] for r in retained(root, "executor0.claim")] == keys
    assert [r[0] for r in retained(root, "executor0.delivery")] == keys
    assert fields(retained(root, "a.budget")[0][2], "BH1\n", 5)[2:] == hold
    assert len(scripted_llm.requests) == 1
    with service(native_release_service_binary, root, programs, scripted_llm.url, workspace, mode="open") as api:
        assert api.request("GET", "/v1/operations/" + accepted["operation"])[1] == result
        assert api.request()[1]["operation"] == accepted["operation"]
    assert len(scripted_llm.requests) == 1
