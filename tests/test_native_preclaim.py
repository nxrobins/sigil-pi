"""Actual scoped reads and atomic never-claimed finalization through native storage."""
import json
import select
import time

import pytest

from api_support import credential
from conftest import SIGIL_ROOT, forge_ok
from scripts.compose_application import compose_application
from settlement_support import NativeSettlement
from store_support import NativeStore, mutation
from test_admission import incoming as admission_input
from turn_support import FUEL, fields, record

OP = "a" * 64
CLAIM, DELIVERY = "executor.claim", "executor.delivery"


def configuration(config):
    def literal(value):
        return {"kind": "literal", "value": value}

    def value(index):
        return {"kind": "value", "index": index}

    def read(namespace, key):
        return {"kind": "read", "namespace": namespace, "key": key}

    facts, bundle = config["inputs"][:2]
    config["marker"] = "UF1\n"
    config["values"] = 3
    config["grants"] = dict.fromkeys(["a.operations", "a.reservation", "a.state", "a.budget"], "read_write")
    config["grants"].update(dict.fromkeys(["a.intent", CLAIM, DELIVERY], "read"))
    config["inputs"] = [facts, bundle, value(0), value(1), value(2), literal(CLAIM), literal(DELIVERY),
        {"kind": "clock"}, read("a.operations", value(0)), read("a.reservation", value(0)), read("a.state", value(1)),
        read("a.intent", value(2)), read("a.budget", literal("active")), read(CLAIM, value(2)), read(DELIVERY, value(2))]


@pytest.fixture
def native(native_store_binary, native_transaction_binary):
    return native_store_binary, native_transaction_binary


def worker(root, row, native, *, source=None):
    return NativeSettlement(native[1], root, row, patch=configuration,
                            source=source or compose_application("preclaim", SIGIL_ROOT).text)


def seed(mcp, tmp_path, native, *, expired=False, unknown=False, claim=None):
    row = credential()
    at = int(time.time()) - (150 if expired else 0)
    outcome, raw, _ = fields(forge_ok(mcp, compose_application("admission", SIGIL_ROOT).text,
        admission_input(created=at, now=at, deadline=at + 120), fuel=FUEL), "AD1\n", 3)
    assert outcome == "ok"
    writes = json.loads(raw)["writes"]
    root = tmp_path / "records"
    grants = {**row["grants"], CLAIM: "read_write", DELIVERY: "read_write"}
    with NativeStore(native[0], root, grants, initialize=True) as store:
        store.commit(writes)
        if unknown:
            state = fields(writes[2]["value"], "PT1\n", 17)
            state[12] = "0"
            store.commit([mutation("a.state", "same-session", record("PT1\n", state), 1)])
        if claim is not None:
            store.commit([mutation(CLAIM, OP + ":1", claim)])
    return root, row, grants


def records(root, grants, native):
    with NativeStore(native[0], root, grants) as store:
        return {ns: store.get(ns, "same-session" if ns == "a.state" else "active" if ns == "a.budget" else
                            OP + ":1" if ns in {"a.intent", CLAIM, DELIVERY} else OP)
                for ns in ["a.operations", "a.reservation", "a.state", "a.intent", "a.budget", CLAIM, DELIVERY]}


def apply(active):
    return active.request({"op": "apply", "values": [OP, "same-session", OP + ":1"]})


@pytest.mark.parametrize("kind", ["eligible", "expired", "unknown"])
def test_actual_scoped_reads_and_one_commit_preserve_delivery_and_usage(mcp, tmp_path, kind, native):
    root, row, grants = seed(mcp, tmp_path, native, expired=kind == "expired", unknown=kind == "unknown")
    before = records(root, grants, native)
    with worker(root, row, native) as active:
        response = apply(active)
        assert response["status"] == "ok", response
        if kind == "eligible":
            assert response["applied"] == {"context": ["eligible", ""], "receipt": None}
        else:
            reason = "deadline_exceeded" if kind == "expired" else "usage_unknown"
            assert response["applied"]["context"] == ["failed", reason]
            assert response["applied"]["receipt"]
    after = records(root, grants, native)
    assert after[CLAIM] == before[CLAIM] and after[DELIVERY] == before[DELIVERY]
    assert after["a.intent"] == before["a.intent"]
    if kind == "eligible":
        assert after == before
    else:
        op = fields(after["a.operations"]["value"], "OQ3\n", 13)
        assert op[9] == "failed" and op[12] == str(after["a.state"]["revision"])
        assert fields(after["a.budget"]["value"], "BH1\n", 5)[2:] == (
            ["0", "20000", "4096"] if kind == "unknown" else ["0", "0", "0"])
        with worker(root, row, native) as active:
            assert apply(active)["status"] == "error"
        assert records(root, grants, native) == after


@pytest.mark.parametrize("claim", ["actual-claim", None])
def test_actual_retained_claim_and_tombstone_cannot_be_reclassified(mcp, tmp_path, claim, native):
    root, row, grants = seed(mcp, tmp_path, native, expired=True)
    with NativeStore(native[0], root, grants) as store:
        store.commit([mutation(CLAIM, OP + ":1", claim)])
    before = records(root, grants, native)
    with worker(root, row, native) as active:
        assert apply(active)["status"] == "error"
    assert records(root, grants, native) == before


@pytest.mark.parametrize("omitted", ["claim", "delivery"])
def test_installed_producer_cannot_omit_an_actual_absence_check(mcp, tmp_path, native, omitted):
    root, row, grants = seed(mcp, tmp_path, native, expired=True)
    source = compose_application("preclaim", SIGIL_ROOT).text
    needle = 'cat(cat(cat(cat(current_check, text(",")), claim_check), text(",")), delivery_check)'
    replacement = 'cat(cat(current_check, text(",")), ' + ("delivery_check" if omitted == "claim" else "claim_check") + ')'
    assert source.count(needle) == 1
    before = records(root, grants, native)
    with worker(root, row, native, source=source.replace(needle, replacement)) as active:
        assert apply(active)["code"] == "binding"
    assert records(root, grants, native) == before


def test_lost_acknowledgement_cannot_repeat_terminal_release(mcp, tmp_path, native):
    root, row, grants = seed(mcp, tmp_path, native, unknown=True)
    with worker(root, row, native) as active:
        active.proc.stdin.write(json.dumps({"op": "apply", "values": [OP, "same-session", OP + ":1"]}) + "\n")
        active.proc.stdin.flush()
        ready, _, _ = select.select([active.proc.stdout], [], [], 30)
        assert ready
        active.proc.kill()
        active.proc.wait(timeout=5)
    before = records(root, grants, native)
    assert fields(before["a.operations"]["value"], "OQ3\n", 13)[9] == "failed"
    with worker(root, row, native) as active:
        assert apply(active)["status"] == "error"
    assert records(root, grants, native) == before
