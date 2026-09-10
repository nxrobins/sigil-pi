"""Real native-bound settlement and public status, not automatic effect dispatch."""
import json
import select

import pytest

from api_support import NativeApi, TOKEN_B, credential
from conftest import SIGIL_ROOT
from scripts.compose_application import compose_application
from settlement_support import NativeSettlement
from store_support import NativeStore, mutation
from test_settlement import terminal_records as terminal_records
from turn_support import fields


@pytest.fixture
def retained(native_store_binary, terminal_records, tmp_path):
    row, records, writes = terminal_records
    root = tmp_path / "records"
    with NativeStore(native_store_binary, root, row["grants"], initialize=True) as store:
        store.commit(writes)
        store.commit([mutation("a.state", "same-session", fields(records[1], "SR1\n", 3)[2], 1)])
    return root, row


def saved(binary, retained):
    root, row = retained
    with NativeStore(binary, root, row["grants"]) as store:
        return [store.get(ns, key) for ns, key in [("a.operations", "a" * 64),
            ("a.reservation", "a" * 64), ("a.budget", "active"), ("a.state", "same-session")]]


def test_actual_native_reads_and_commit_receipt_settle_once_across_restart(
        retained, native_transaction_binary, native_store_binary):
    with NativeSettlement(native_transaction_binary, *retained) as active:
        result = active.apply()
        assert result["context"] == ["done", "reported"]
        assert result["receipt"]["revision"] == 3
        assert active.apply() == {"context": ["done", "reported"], "receipt": None}
    before = saved(native_store_binary, retained)
    assert [r["revision"] for r in before] == [2, 2, 2, 2]
    assert fields(before[2]["value"], "BH1\n", 5)[2:] == ["0", "0", "0"]
    with NativeSettlement(native_transaction_binary, *retained) as active:
        assert active.apply() == {"context": ["done", "reported"], "receipt": None}
    assert saved(native_store_binary, retained) == before


def test_lost_native_settlement_acknowledgement_does_not_release_reservations_twice(
        retained, native_transaction_binary, native_store_binary):
    with NativeSettlement(native_transaction_binary, *retained) as active:
        active.proc.stdin.write(json.dumps({"op": "apply", "values": ["a" * 64, "same-session"]}) + "\n")
        active.proc.stdin.flush()
        ready, _, _ = select.select([active.proc.stdout], [], [], 30)
        assert ready, "settlement did not become ready for acknowledgement"
        active.proc.kill()
        active.proc.wait(timeout=5)  # Deliberately never read the acknowledgement.
    before = saved(native_store_binary, retained)
    assert [r["revision"] for r in before] == [2, 2, 2, 2]
    with NativeSettlement(native_transaction_binary, *retained) as active:
        assert active.apply() == {"context": ["done", "reported"], "receipt": None}
    assert saved(native_store_binary, retained) == before


def test_public_api_retains_terminal_result_and_preserves_it_after_followup_admission(
        retained, native_transaction_binary, native_store_binary, native_service_binary):
    root, row = retained
    # A terminal conversation alone is not a completed operation and cannot be replaced.
    with NativeApi(native_service_binary, root.parent, mode="open", credentials=[row]) as api:
        assert api.request("GET", "/v1/operations/" + "a" * 64)[1]["status"] == "accepted"
        status, _ = api.request(body={"session": "same-session", "message": "Follow up", "submission_key": "next"})
        assert status == 409
    with NativeSettlement(native_transaction_binary, root, row) as active:
        assert active.apply()["receipt"]
    peer = credential(TOKEN_B, principal="bob")
    with NativeApi(native_service_binary, root.parent, mode="open", credentials=[row, peer]) as api:
        path = "/v1/operations/" + "a" * 64
        status, before = api.request("GET", path)
        assert status == 200 and before["status"] == "done" and before["reply"] == "Retained answer 😀"
        assert before["usage"] == {"input_tokens": 4, "output_tokens": 3, "known": True}
        assert before["accounting"] == "reported"
        assert api.request("GET", path, token=TOKEN_B)[0] == 404
        status, next_op = api.request(body={"session": "same-session", "message": "Follow up", "submission_key": "next"})
        assert status == 202 and next_op["operation"] != "a" * 64
        assert api.request("GET", path) == (200, before)
    # The next conversation state is no longer the old terminal snapshot. A retry
    # still finds the already settled pair and must not release its allowance again.
    with NativeSettlement(native_transaction_binary, root, row) as active:
        assert active.apply()["receipt"] is None
    retained_now = saved(native_store_binary, retained)
    assert fields(retained_now[2]["value"], "BH1\n", 5)[2:] == ["1", "20000", "4096"]


@pytest.mark.parametrize("field", ["facts", "snapshots", "clock", "batch", "receipt", "phase", "grants"])
def test_requests_cannot_supply_settlement_facts_or_commit_commands(retained, native_transaction_binary, native_store_binary, field):
    before = saved(native_store_binary, retained)
    with NativeSettlement(native_transaction_binary, *retained) as active:
        response = active.request({"op": "apply", "values": ["a" * 64, "same-session"], field: "forged"})
        assert response["code"] == "protocol"
        assert active.request({"op": "commit", "checks": [], "writes": []})["code"] == "protocol"
    assert saved(native_store_binary, retained) == before


@pytest.mark.parametrize("values", [["a" * 64], ["a" * 64, "same-session", "extra"],
    ["x" * 513, "same-session"], [{"facts": "forged"}, "same-session"], ["c" * 64, "same-session"],
    ["a" * 64, "other-session"]])
def test_invalid_or_mismatched_lookups_cannot_change_retained_state(
        retained, native_transaction_binary, native_store_binary, values):
    before = saved(native_store_binary, retained)
    with NativeSettlement(native_transaction_binary, *retained) as active:
        assert active.request({"op": "apply", "values": values})["status"] == "error"
    assert saved(native_store_binary, retained) == before


@pytest.mark.parametrize("kind", ["missing_check", "wrong_coordinate", "wrong_revision"])
def test_installed_producer_cannot_bypass_actual_snapshot_preconditions(
        retained, native_transaction_binary, native_store_binary, kind):
    code = compose_application("settlement", SIGIL_ROOT).text
    needle, replacement = {
        "missing_check": ('store_commit(store_check(get(c, 10), get(x, 3), get(rs, 1)),', 'store_commit(text(""),'),
        "wrong_coordinate": ('store_write(get(c, 8), get(x, 2), get(ro, 1),', 'store_write(text("unread"), get(x, 2), get(ro, 1),'),
        "wrong_revision": ('store_write(get(c, 8), get(x, 2), get(ro, 1),', 'store_write(get(c, 8), get(x, 2), text("0"),'),
    }[kind]
    assert code.count(needle) == 1
    before = saved(native_store_binary, retained)
    with NativeSettlement(native_transaction_binary, *retained, source=code.replace(needle, replacement)) as active:
        assert active.request({"op": "apply", "values": ["a" * 64, "same-session"]})["code"] == "binding"
    assert saved(native_store_binary, retained) == before


def test_actual_read_only_budget_scope_cannot_be_widened_by_sigil_output(retained, native_transaction_binary, native_store_binary):
    def patch(config):
        config["grants"]["a.budget"] = "read"
    before = saved(native_store_binary, retained)
    with NativeSettlement(native_transaction_binary, *retained, patch=patch) as active:
        assert active.request({"op": "apply", "values": ["a" * 64, "same-session"]})["code"] == "storage"
    assert saved(native_store_binary, retained) == before


@pytest.mark.parametrize("kind", ["net", "fs", "secret", "worker_facts", "missing_read_grant", "index", "marker", "literal"])
def test_transaction_bootstrap_is_fixed_grantless_and_bounded(retained, native_transaction_binary, kind):
    def patch(config):
        if kind in {"net", "fs"}:
            config["worker"][kind] = ["127.0.0.1" if kind == "net" else "/tmp"]
        elif kind == "secret":
            config["worker"]["secret_env"] = {"secret": "UNUSED"}
        elif kind == "worker_facts":
            config["inputs"][0] = {"kind": "worker_facts"}
        elif kind == "missing_read_grant":
            del config["grants"]["a.state"]
        elif kind == "index":
            config["inputs"][2]["index"] = 2
        elif kind == "marker":
            config["marker"] = "SF1 "
        else:
            config["inputs"][0]["value"] = "x" * 16385
    with pytest.raises(AssertionError, match="config"):
        NativeSettlement(native_transaction_binary, *retained, patch=patch)
