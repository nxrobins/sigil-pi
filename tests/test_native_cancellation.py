"""Public cancellation writes real state; actual native dispatch rereads that state."""
import concurrent.futures
import select
import socket
import time

from api_support import NativeApi, TOKEN_A, TOKEN_B, credential
from conftest import SIGIL_ROOT
from policy_support import NativePolicyWorker
from scripts.compose_application import compose_application
from settlement_support import NativeSettlement
from store_support import NativeStore
from test_native_preclaim import configuration as preclaim_configuration
from test_turn_execution import programs as programs, provider_grants
from turn_support import fields


def preclaim_config(config):
    preclaim_configuration(config)
    mapping = {"executor.claim": "a.dispatch", "executor.delivery": "a.delivery"}
    config["grants"] = {mapping.get(key, key): value for key, value in config["grants"].items()}
    for item in config["inputs"]:
        if item["kind"] == "literal":
            item["value"] = mapping.get(item["value"], item["value"])
        if item["kind"] == "read":
            item["namespace"] = mapping.get(item["namespace"], item["namespace"])
    config["marker"], config["values"] = "UF2\n", 4
    config["inputs"] += [{"kind": "value", "index": 3},
                         {"kind": "read", "namespace": "a.operations", "key": {"kind": "value", "index": 3}}]


def dispatch_config(config):
    config["marker"], config["values"] = "DF2\n", 4
    config["inputs"] += [{"kind": "value", "index": 3},
                         {"kind": "read", "namespace": "a.operations", "key": {"kind": "value", "index": 3}}]


def test_public_cancel_is_deduplicated_and_retained_without_rewriting_acceptance(
        native_service_binary, native_store_binary, tmp_path):
    root = tmp_path / "service"
    rows = [credential(), credential(TOKEN_B, principal="bob"),
            credential("other-tenant", principal="other", tenant="tenant-b", prefix="b"),
            credential("read-only", principal="reader", scopes=["sessions:read"])]
    with NativeApi(native_service_binary, root, credentials=rows) as api:
        status, accepted = api.request()
        assert status == 202
        path = "/v1/operations/" + accepted["operation"] + "/cancel"
        for token, expected in [("unknown", 401), (TOKEN_B, 404), ("other-tenant", 404), ("read-only", 403)]:
            assert api.request("POST", path, body={}, token=token)[0] == expected
        assert api.request("POST", path, body={"phase": "cancelled"})[0] == 400
        assert not api.request("GET", accepted["status_url"])[1].get("cancellation_requested", False)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: api.request("POST", path, body={}), range(4)))
        assert all(status == 202 and body["cancellation_status"] == "requested" for status, body in replies)
        assert sum(not body["replayed"] for _, body in replies) == 1
        pending = api.request("GET", accepted["status_url"])[1]
        assert pending["status"] == "accepted" and pending["cancellation_requested"]
    with NativeApi(native_service_binary, root, credentials=rows, mode="open") as api:
        assert api.request("GET", accepted["status_url"])[1] == pending
        assert api.request("POST", path, body={})[1]["replayed"]
    with NativeStore(native_store_binary, root / "records", {"a.operations": "read"}) as store:
        operation = store.get("a.operations", accepted["operation"])
        requested = store.get("a.operations", accepted["operation"] + ":cancel")
        assert operation["revision"] == requested["revision"] == 1
        assert fields(operation["value"], "OQ2\n", 11)[9] == "accepted"
        assert fields(requested["value"], "CR1\n", 5)[:3] == [accepted["operation"], "alice", "tenant-a"]
        assert TOKEN_A not in requested["value"]


def test_cancel_acknowledgement_lost_after_commit_recovers_as_a_repeat(native_service_binary, tmp_path):
    root = tmp_path / "service"
    with NativeApi(native_service_binary, root) as api:
        accepted = api.request()[1]
        path = "/v1/operations/" + accepted["operation"] + "/cancel"
        with socket.create_connection(("127.0.0.1", api.port), timeout=30) as connection:
            connection.sendall((f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {TOKEN_A}\r\n"
                                "Content-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}").encode())
            ready, _, _ = select.select([connection], [], [], 30)
            assert ready, "the service did not become ready to acknowledge cancellation"
            api.close()  # Deliberately never read the HTTP acknowledgement.
    with NativeApi(native_service_binary, root, mode="open") as api:
        assert api.request("GET", accepted["status_url"])[1]["cancellation_requested"]
        status, replay = api.request("POST", path, body={})
        assert status == 202 and replay["replayed"] and replay["cancellation_status"] == "requested"


def test_public_cancel_after_actual_eligibility_prevents_later_native_claim_and_provider_send(
        native_service_binary, native_store_binary, native_transaction_binary,
        native_claimed_worker_binary, programs, scripted_llm, tmp_path):
    # A controlled conformance gap, not a claim that this fixture drives the product.
    # Every application decision below runs actual SIGIL; the cancel is public HTTP.
    root, row = tmp_path / "service", credential()
    with NativeApi(native_service_binary, root, credentials=[row]) as api:
        accepted = api.request()[1]
    op = accepted["operation"]
    values = [op, "same-session", op + ":1", op + ":cancel"]
    with NativeStore(native_store_binary, root / "records", {"a.operations": "read"}) as store:
        operation = fields(store.get("a.operations", op)["value"], "OQ2\n", 11)
        bundle = operation[10]
    with NativeSettlement(native_transaction_binary, root / "records", row, bundle=bundle,
            patch=preclaim_config, source=compose_application("preclaim_cancellable", SIGIL_ROOT).text) as active:
        response = active.request({"op": "apply", "values": values})
        assert response["applied"] == {"context": ["eligible", ""], "receipt": None}
    # The exact native-bound DF2 path must also be valid before cancellation.
    # Version 3 prepares a one-use capability but does not claim or execute it;
    # closing this conformance adapter discards that capability.
    with NativePolicyWorker(native_claimed_worker_binary, root / "records", programs["provider"],
            facts=row["facts"], bundle=bundle, argument=scripted_llm.url, grants=provider_grants(),
            recorded=True, policy_patch=dispatch_config,
            policy_source=compose_application("dispatch_cancellable", SIGIL_ROOT).text) as active:
        permitted = active.request({"op": "authorize", "values": values})
        assert permitted["status"] == "ok", permitted
        assert active.request({"op": "cancel", "ticket": permitted["prepared"]["ticket"]})["cancelled_before_start"]
        assert active.get("a.dispatch", op + ":1")["revision"] == 0
    with NativeApi(native_service_binary, root, credentials=[row], mode="open") as api:
        assert api.request("POST", "/v1/operations/" + op + "/cancel", body={})[0] == 202
    with NativePolicyWorker(native_claimed_worker_binary, root / "records", programs["provider"],
            facts=row["facts"], bundle=bundle, argument=scripted_llm.url, grants=provider_grants(),
            recorded=True, owned=True, policy_patch=dispatch_config,
            policy_source=compose_application("dispatch_cancellable", SIGIL_ROOT).text) as active:
        response = active.request({"op": "authorize", "values": values})
        assert response["status"] == "error" and response["code"] == "application", response
        assert time.time() < int(operation[7]), "expiry cannot substitute for the cancellation refusal"
        assert active.get("a.dispatch", op + ":1")["revision"] == 0
    assert not scripted_llm.requests
    with NativeStore(native_store_binary, root / "records", {"a.dispatch": "read", "a.delivery": "read"}) as store:
        assert store.get("a.dispatch", op + ":1")["revision"] == store.get("a.delivery", op + ":1")["revision"] == 0
