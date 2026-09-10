"""Real HTTP -> SIGIL service -> model/file/model -> terminal HTTP. No step driver."""
import json
import sqlite3
import subprocess

import pytest

from api_support import TOKEN_A, TOKEN_B, credential
from automatic_support import configuration, service, wait_operation
from conftest import API_KEY
from test_turn_execution import hanging_provider
from test_turn_execution import programs as programs
from turn_support import fields, text_reply, tool_reply


def test_http_service_automatically_completes_a_durable_tool_turn_and_followup(
        native_release_service_binary, programs, scripted_llm, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "permitted"
    workspace.mkdir()
    (workspace / "README.md").write_text("The project owner is Ada.\n")
    scripted_llm.script = [json.loads(tool_reply("read_file", usage={"input_tokens": 11, "output_tokens": 4})),
        json.loads(text_reply("The project owner is Ada.", usage={"input_tokens": 7, "output_tokens": 5})),
        json.loads(text_reply("Yes, Ada owns the project.", usage={"input_tokens": 2, "output_tokens": 1}))]
    with service(native_release_service_binary, tmp_path / "service", programs, scripted_llm.url, workspace) as api:
        status, accepted = api.request()
        assert status == 202, accepted
        result = wait_operation(api, accepted["operation"])
        assert result["status"] == "done" and result["reply"] == "The project owner is Ada."
        assert result["usage"] == {"input_tokens": 18, "output_tokens": 9, "known": True}
        assert result["accounting"] == "reported"
        status, followup = api.request(body={"session": "same-session", "message": "Who owns it again?", "submission_key": "followup"})
        assert status == 202 and followup["operation"] != accepted["operation"]
        final = wait_operation(api, followup["operation"])
        assert final["status"] == "done" and final["reply"] == "Yes, Ada owns the project."
        assert api.request("GET", "/v1/operations/" + accepted["operation"])[1] == result
    assert len(scripted_llm.requests) == 3
    content = scripted_llm.requests[1]["messages"][-1]["content"][0]
    assert content["type"] == "tool_result" and content["content"] == "The project owner is Ada.\n"
    assert "The project owner is Ada." in json.dumps(scripted_llm.requests[2]["messages"])
    assert API_KEY not in json.dumps(scripted_llm.requests) + json.dumps(result) + json.dumps(final)


def test_identical_session_and_submission_names_remain_isolated_across_tenants(
        native_release_service_binary, programs, scripted_llm, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    areas = [tmp_path / "alice", tmp_path / "bob"]
    canaries = ["ALICE-private-file-913", "BOB-private-file-728"]
    for area, canary in zip(areas, canaries):
        area.mkdir()
        (area / "README.md").write_text(canary)
    results, operations = [], []
    with service(native_release_service_binary, tmp_path / "service", programs,
                 scripted_llm.url, areas, rows=rows) as api:
        assert api.request(token="unknown-credential")[0] == 401
        for index, token in enumerate([TOKEN_A, TOKEN_B]):
            # Sequential turns qualify isolation, not the separate concurrent-load gate.
            usage = {"input_tokens": 1, "output_tokens": 1}
            scripted_llm.script.extend([json.loads(tool_reply("read_file", usage=usage)),
                                       json.loads(text_reply(canaries[index], usage=usage))])
            status, accepted = api.request(token=token)
            assert status == 202, accepted
            operations.append(accepted["operation"])
            other = TOKEN_B if index == 0 else TOKEN_A
            assert api.request("GET", "/v1/operations/" + accepted["operation"], token=other)[0] == 404
            result = wait_operation(api, accepted["operation"], token=token)
            assert result["status"] == "done" and result["reply"] == canaries[index]
            assert canaries[1 - index] not in json.dumps(result)
            results.append(result)
        assert operations[0] != operations[1]
        for index, token in enumerate([TOKEN_A, TOKEN_B]):
            assert api.request("GET", "/v1/operations/" + operations[index], token=token)[1] == results[index]
            status, duplicate = api.request(token=token)
            assert status == 202 and duplicate["replayed"] and duplicate["operation"] == operations[index]
    assert len(scripted_llm.requests) == 4
    for index in range(2):
        messages = scripted_llm.requests[2 * index + 1]["messages"]
        assert messages[-1]["content"][0]["content"] == canaries[index]
        assert canaries[1 - index] not in json.dumps(messages)
    assert API_KEY not in json.dumps(scripted_llm.requests) + json.dumps(results)


def test_restart_after_actual_provider_send_publishes_uncertainty_without_resending(
        native_release_service_binary, programs, tmp_path, monkeypatch):
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)
    workspace = tmp_path / "permitted"
    workspace.mkdir()
    root = tmp_path / "service"
    with hanging_provider() as (endpoint, received, requests):
        with service(native_release_service_binary, root, programs, endpoint, workspace) as api:
            status, accepted = api.request()
            assert status == 202, accepted
            assert received.wait(60), "automatic service never sent the provider request"
            # Kill the actual owner while the response is held; no fixture result,
            # recovery command, native claim, or settlement is supplied.
        with sqlite3.connect(f"file:{root / 'records/records.sqlite'}?mode=ro", uri=True) as db:
            assert db.execute("SELECT revision FROM records WHERE namespace='executor0.claim'").fetchall() == [(1,)]
            assert db.execute("SELECT revision FROM records WHERE namespace='executor0.delivery'").fetchall() == []
        with service(native_release_service_binary, root, programs, endpoint, workspace, mode="open") as api:
            result = wait_operation(api, accepted["operation"])
            assert result["status"] == "uncertain"
            assert result["usage"]["known"] is False and result["accounting"] == "unknown"
            assert api.request()[1]["operation"] == accepted["operation"]
        # A second restart and duplicate submission cannot reopen a terminal claim.
        with service(native_release_service_binary, root, programs, endpoint, workspace, mode="open") as api:
            assert api.request("GET", "/v1/operations/" + accepted["operation"])[1] == result
            assert api.request()[1]["operation"] == accepted["operation"]
        assert len(requests) == 1


@pytest.mark.parametrize("case", ["literal_credential", "literal_bundle", "literal_worker", "duplicate_credential",
    "bad_policy_marker", "bad_policy_lookup", "bad_policy_literal_key", "bad_transaction_marker",
    "bad_transaction_lookup", "bad_transaction_literal_key", "transaction_worker_facts",
    "effect_writable_domain", "transaction_foreign_namespace", "coordinator_writable", "reserved_domain",
    "coordinator_effect_grant", "unregistered_owner", "claim_read_write", "claim_create_only",
    "delivery_read_write", "delivery_create_only"])
def test_invalid_automatic_bootstrap_is_refused_before_storage_or_effects(
        native_release_service_binary, programs, tmp_path, case):
    root = tmp_path / "service"
    config = configuration(root, programs, "http://127.0.0.1:1/messages", tmp_path)
    row = config["automatic"]["participants"][0]
    effect = row["effects"]["provider"]
    policy = effect["policy"]
    transaction = row["transactions"]["interpret"]
    if case.startswith("literal_"):
        index = {"literal_credential": 0, "literal_bundle": 1, "literal_worker": 10}[case]
        policy["inputs"][index] = {"kind": "literal", "value": config["credentials"][0]["facts"]}
    elif case == "duplicate_credential":
        policy["inputs"].append({"kind": "credential_facts"})
    elif case == "bad_policy_marker":
        policy["marker"] = "bad-marker"
    elif case == "bad_policy_lookup":
        policy["inputs"][2] = {"kind": "value", "index": 4}
    elif case == "bad_policy_literal_key":
        policy["inputs"][8]["key"] = {"kind": "literal", "value": "invalid/key"}
    elif case == "bad_transaction_marker":
        transaction["marker"] = "bad-marker"
    elif case == "bad_transaction_lookup":
        transaction["inputs"][3] = {"kind": "value", "index": 4}
    elif case == "bad_transaction_literal_key":
        transaction["inputs"][8]["key"] = {"kind": "literal", "value": "bad\nkey"}
    elif case == "transaction_worker_facts":
        transaction["inputs"][0] = {"kind": "worker_facts"}
    elif case == "effect_writable_domain":
        effect["grants"]["a.state"] = "read_write"
    elif case == "transaction_foreign_namespace":
        transaction["grants"]["another.state"] = "read_write"
    elif case.startswith(("claim_", "delivery_")):
        namespace, access = case.split("_", 1)
        row["transactions"]["preclaim"]["grants"][row[namespace + "_namespace"]] = access
    elif case == "coordinator_writable":
        row["read_grants"]["a.state"] = "read_write"
    elif case == "reserved_domain":
        row["claim_namespace"] = "a.state"
    elif case == "coordinator_effect_grant":
        row["worker"]["net"] = ["127.0.0.1"]
    elif case == "unregistered_owner":
        row["credential_sha256"] = "f" * 64
    else:
        raise AssertionError(case)
    path = root / "service.json"
    path.write_text(json.dumps(config))
    run = subprocess.run([str(native_release_service_binary), "init", str(path), "0"],
                         capture_output=True, text=True, timeout=40)
    assert run.returncode != 0 and not run.stdout, (run.stdout, run.stderr)
    assert not (root / "records").exists(), "rejected bootstrap initialized application storage"


def test_preclaim_template_uses_actual_facts_time_and_eight_reads(programs, tmp_path):
    config = configuration(tmp_path / "service", programs, "http://127.0.0.1:1/messages", tmp_path)
    row = config["automatic"]["participants"][0]
    assert fields(row["binding"], "LB3\n", 5) == ["provider", "reader", "interpret", "settle", "preclaim"]
    tx = row["transactions"]["preclaim"]
    assert tx["marker"] == "UF2\n" and tx["values"] == 4
    assert [v["kind"] for v in tx["inputs"][:2]] == ["credential_facts", "bundle"]
    assert tx["inputs"][7] == {"kind": "clock"}
    assert len(tx["inputs"]) == 17
    assert len([v for v in tx["inputs"] if v["kind"] == "read"]) == 8
    assert tx["inputs"][-2:] == [{"kind": "value", "index": 3},
                                 {"kind": "read", "namespace": "a.operations", "key": {"kind": "value", "index": 3}}]
    assert {v["namespace"] for v in tx["inputs"] if v["kind"] == "read"} == set(tx["grants"])
    assert tx["grants"][row["claim_namespace"]] == tx["grants"][row["delivery_namespace"]] == "read"
