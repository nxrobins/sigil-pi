"""Actual native-bound policy/claim/effect checks, not automatic API dispatch."""
import hashlib
import json
import time

import pytest

from api_support import NativeApi, credential
from conftest import SIGIL_ROOT
from executor_support import executor_plan
from policy_support import NativePolicyWorker
from scripts.compose_application import compose_application
from settlement_support import NativeSettlement
from store_support import NativeStore, mutation
from test_admission import plan as admit
from test_turn_execution import programs as programs, provider_grants
from transaction_support import plan as interpret
from turn_support import Decision, fields, record, text_reply, tool_reply


@pytest.fixture
def seeded(mcp, native_store_binary, tmp_path):
    now = int(time.time())
    row = credential()
    outcome, raw, _ = admit(mcp, compose_application("admission", SIGIL_ROOT).text,
                           row=row, created=now, now=now, deadline=now + 120)
    assert outcome == "ok"
    root = tmp_path / "records"
    with NativeStore(native_store_binary, root, row["grants"], initialize=True) as store:
        assert store.raw(raw)["status"] == "ok"
    return root, row, json.loads(raw)["writes"]


def worker(binary, seeded, programs, **kwargs):
    root, row, _ = seeded
    return NativePolicyWorker(binary, root, programs["provider"], facts=row["facts"],
                              argument=kwargs.pop("argument", "http://127.0.0.1:1/messages"),
                              grants=kwargs.pop("grants", provider_grants()), **kwargs)


def claim(mcp, programs, active, prepared):
    proposed = executor_plan(mcp, programs["executor_transaction"], active.intent,
                            prefix="a", key="a" * 64 + ":1", generation=prepared["generation"])
    return active.claim(proposed.raw)


def test_native_policy_selects_actual_payload_and_claim_gates_one_real_model_call(
        mcp, programs, seeded, native_claimed_worker_binary, scripted_llm):
    scripted_llm.script = [json.loads(text_reply("Observed", usage={"input_tokens": 2, "output_tokens": 1}))]
    with worker(native_claimed_worker_binary, seeded, programs, argument=scripted_llm.url) as active:
        ready = active.authorize()
        body = fields(active.intent["value"], "SI1\n", 5)[4]
        headers = "|x-api-key: {{secret:anthropic}}\nanthropic-version: 2023-06-01\ncontent-type: application/json|"
        assert ready["input_sha256"] == hashlib.sha256((scripted_llm.url + headers + body).encode()).hexdigest()
        assert fields(active.context, "DX1\n", 5) == ["a" * 64, "1", "alice", "tenant-a", "b" * 64]
        # While authorized, no outer command may substitute records or policy.
        assert active.request({"op": "commit", "checks": [], "writes": []})["code"] == "protocol"
        claim(mcp, programs, active, ready)
        result = active.execute(ready)
        assert result["fault"] is None and result["worker_reaped"]
        assert result["result"]["status"] == "ok"
        assert active.request({"op": "execute", "ticket": ready["ticket"]})["code"] == "ticket"
    assert len(scripted_llm.requests) == 1
    assert scripted_llm.requests[0] == json.loads(body)
    # A new policy invocation on reopened storage cannot prepare an old claim.
    with worker(native_claimed_worker_binary, seeded, programs, argument=scripted_llm.url) as active:
        assert active.request(active.authorize_request())["code"] == "claimed"
    assert len(scripted_llm.requests) == 1


@pytest.mark.parametrize("kind", ["prepare", "facts", "snapshot", "worker", "time", "guard", "too_many", "oversize", "object"])
def test_requests_cannot_replace_bootstrap_facts_or_bypass_authorization(
        programs, seeded, native_claimed_worker_binary, kind):
    with worker(native_claimed_worker_binary, seeded, programs) as active:
        request = active.authorize_request()
        if kind == "prepare":
            request = active.prepare_request("forged", prefix="a")
        elif kind == "too_many":
            request["values"].append("forged")
        elif kind == "oversize":
            request["values"][0] = "x" * 513
        elif kind == "object":
            request["values"][0] = {"facts": "forged"}
        else:
            request[kind] = "forged"
        assert active.request(request)["code"] == "protocol"
        assert active.get("a.dispatch", "a" * 64 + ":1")["revision"] == 0


@pytest.mark.parametrize("index", [0, 1, 2])
def test_caller_lookup_identities_cannot_mix_missing_records(
        programs, seeded, native_claimed_worker_binary, index):
    with worker(native_claimed_worker_binary, seeded, programs) as active:
        request = active.authorize_request()
        request["values"][index] = "e" * 64 if index == 0 else "another"
        assert active.request(request)["status"] == "error"
        assert active.get("a.dispatch", "a" * 64 + ":1")["revision"] == 0


@pytest.mark.parametrize("change", ["scope", "epoch", "tenant", "bundle", "reservation", "intent", "state", "budget", "expiry"])
def test_current_bound_facts_and_actual_record_changes_are_checked_before_any_claim(
        programs, seeded, native_claimed_worker_binary, native_store_binary, change):
    root, row, writes = seeded
    if change in {"scope", "epoch", "tenant", "expiry"}:
        kwargs = {"scope": {"scopes": []}, "epoch": {"epoch": "new"},
                  "tenant": {"tenant": "other"}, "expiry": {"expires": int(time.time()) - 1}}[change]
        row = {**row, "facts": credential(**kwargs)["facts"]}
    else:
        index, marker, count, field, value = {
            "bundle": (1, "OQ2\n", 11, 10, "e" * 64),
            "reservation": (5, "BR1\n", 9, 7, "settled"),
            "intent": (3, "SI1\n", 5, 4, "forged"),
            "state": (2, "PT1\n", 17, 15, "other"),
            "budget": (4, "BH1\n", 5, 2, "0"),
        }[change]
        write = writes[index]
        changed = fields(write["value"], marker, count)
        changed[field] = value
        with NativeStore(native_store_binary, root, row["grants"]) as store:
            store.commit([mutation(write["namespace"], write["key"], record(marker, changed), 1)])
    with worker(native_claimed_worker_binary, (root, row, writes), programs) as active:
        assert active.request(active.authorize_request())["status"] == "error"
        assert active.get("a.dispatch", "a" * 64 + ":1")["revision"] == 0


@pytest.mark.parametrize("changed", ["net", "fs", "secret", "source", "runtime", "tenant", "endpoint"])
def test_operator_binding_is_compared_to_actual_held_effect_permissions(
        programs, seeded, native_claimed_worker_binary, changed):
    grants = provider_grants()
    binding_patch = None
    if changed in {"net", "fs", "secret"}:
        grants[changed] = grants.get(changed, []) + {"net": ["*"], "fs": ["/tmp"], "secret": ["extra=fake"]}[changed]
    else:
        binding_patch = {"source": {2: "e" * 64}, "runtime": {3: "e" * 64},
                         "tenant": {0: "other"}, "endpoint": {4: "https://other.test/messages"}}[changed]
    with worker(native_claimed_worker_binary, seeded, programs, grants=grants, binding_patch=binding_patch) as active:
        assert active.request(active.authorize_request())["status"] == "error"
        assert active.get("a.dispatch", "a" * 64 + ":1")["revision"] == 0


def test_policy_read_scope_is_not_exposed_to_executor_controller(programs, seeded, native_claimed_worker_binary):
    with worker(native_claimed_worker_binary, seeded, programs) as active:
        for namespace, key in [("a.operations", "a" * 64), ("a.state", "same-session"), ("a.budget", "active")]:
            assert active.request({"op": "get", "namespace": namespace, "key": key})["code"] == "storage"
        ready = active.authorize()
        assert active.request({"op": "cancel", "ticket": ready["ticket"]})["cancelled_before_start"]
        assert active.get("a.dispatch", "a" * 64 + ":1")["revision"] == 0


@pytest.mark.parametrize("kind", ["policy_grants", "write_scope", "missing_scope", "bad_index", "bad_marker", "alias"])
def test_invalid_or_effectful_policy_bootstrap_fails_closed(programs, seeded, native_claimed_worker_binary, kind):
    def patch(config):
        if kind == "policy_grants":
            config["worker"]["net"] = ["127.0.0.1"]
        elif kind == "write_scope":
            config["read_grants"]["a.state"] = "read_write"
        elif kind == "missing_scope":
            del config["read_grants"]["a.state"]
        elif kind == "bad_index":
            config["inputs"][2]["index"] = 3
        elif kind == "bad_marker":
            config["marker"] = "DF1 "
        else:
            config["alias"] = "bad/name"
    with pytest.raises(AssertionError, match="config"):
        worker(native_claimed_worker_binary, seeded, programs, policy_patch=patch)


def application_bundle(config):
    """Independent fixture calculation from held bootstrap, not stored OQ2 bytes."""
    def manifest(worker):
        return {"source": worker["source_sha256"], "runtime": worker["runtime_sha256"],
                "fuel": worker["max_fuel"], "timeout_ms": worker["max_timeout_ms"]}
    bundle = {"contract": "sigil-application-host/v3", "steps": 8,
              "entry": manifest(config["worker"]),
              "functions": {name: manifest(worker) for name, worker in config["functions"].items()}}
    return hashlib.sha256(json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.mark.parametrize("kind,code", [("alias", "binding"), ("namespace", "binding"), ("key", "binding"),
    ("revision", "binding"), ("noncanonical_revision", "binding"), ("expired", "time_guard"),
    ("unguarded", "protocol"), ("legacy", "protocol"), ("valid", None)])
def test_native_envelope_binding_is_enforced_even_for_an_owner_installed_policy_probe(
        programs, seeded, native_claimed_worker_binary, kind, code):
    parts = ["provider", "probe payload", "a.intent", "a" * 64 + ":1", "1",
             record("TG1\n", ["0", "9007199254740991"]), "opaque context"]
    changes = {"alias": (0, "other"), "namespace": (2, "a.state"), "key": (3, "other"),
               "revision": (4, "2"), "noncanonical_revision": (4, "01"),
               "expired": (5, record("TG1\n", ["0", "1"])), "unguarded": (5, "")}
    if kind in changes:
        index, value = changes[kind]
        parts[index] = value
    output = record("DX1\n" if kind == "legacy" else "DW1\n", parts)
    source = """module native_dispatch_probe;
pub fn tool_main(input_ptr: i64, input_len: i64) -> i64 @Internal ! { Alloc } {
    let value: str @Internal = OUTPUT;
    let out: i64 @Internal = alloc(value.len());
    let mut i: i64 @Internal = 0;
    while i < value.len() { store8(out + i, value.byte_at(i)); i += 1; }
    return out << 32 | value.len();
}
""".replace("OUTPUT", json.dumps(output))
    with worker(native_claimed_worker_binary, seeded, programs, policy_source=source) as active:
        result = active.request(active.authorize_request())
        if code:
            assert result["code"] == code
            assert active.get("a.dispatch", "a" * 64 + ":1")["revision"] == 0
        else:
            assert result["status"] == "ok"
            assert result["context"] == "opaque context"
            assert active.request({"op": "cancel", "ticket": result["prepared"]["ticket"]})["cancelled_before_start"]


@pytest.mark.parametrize("admission", ["component", "http"])
@pytest.mark.parametrize("recorded", [False, True])
def test_durable_turn_uses_native_bound_policy_for_every_model_and_file_effect(
        mcp, programs, seeded, native_claimed_worker_binary, native_store_binary,
        native_service_binary, native_transaction_binary, scripted_llm, tmp_path, admission, recorded):
    root, row, writes = seeded
    bundle = "b" * 64
    operation = "a" * 64
    if admission == "http":
        with NativeApi(native_service_binary, tmp_path / "api", credentials=[row]) as api:
            status, accepted = api.request()
            assert status == 202 and not accepted["replayed"]
            bundle = application_bundle(api.config)
            operation = accepted["operation"]
        root = tmp_path / "api" / "records"
    access = {**row["grants"], "a.delivery": "read"}
    with NativeStore(native_store_binary, root, access) as store:
        actual = store.get("a.state", "same-session")
        intent = fields(store.get("a.intent", operation + ":1")["value"], "SI1\n", 5)
        assert fields(store.get("a.operations", operation)["value"], "OQ2\n", 11)[10] == bundle
        current = Decision(actual["value"], intent[2], intent[1], intent[3], intent[4])
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("Ada owns this project.\n")
    scripted_llm.script = [json.loads(tool_reply("read_file", usage={"input_tokens": 2, "output_tokens": 1})),
                          json.loads(text_reply("Ada owns this project.", usage={"input_tokens": 3, "output_tokens": 2}))]
    trace = []
    for _ in range(6):
        kind = current.action
        assert kind in {"model", "tool"}
        role = "model" if kind == "model" else "read_file"
        source = programs["provider"] if kind == "model" else programs["read_file"]
        argument = scripted_llm.url if kind == "model" else str(workspace)
        grants = provider_grants() if kind == "model" else {"fs": [str(workspace)]}
        key = operation + ":" + current.sequence
        # Trusted fixture still chooses the operator-installed worker binding;
        # SIGIL now checks that choice against actual native facts and state.
        with NativePolicyWorker(native_claimed_worker_binary, root, source,
                facts=row["facts"], argument=argument, grants=grants, role=role,
                alias="provider" if kind == "model" else "file", bundle=bundle, recorded=recorded) as active:
            ready = active.authorize(operation=operation, sequence=current.sequence)
            loaded = active.intent
            if recorded:
                completion = active.run_recorded(ready)
                assert completion["delivery_receipt"] and completion["phase"] == "2", completion
                assert completion["recording_error"] is None and completion["refused"] is None
                observed = completion["observation"]
            else:
                decision = executor_plan(mcp, programs["executor_transaction"], loaded, prefix="a",
                                         key=key, generation=ready["generation"])
                active.claim(decision.raw)
                observed = active.execute(ready)
            assert observed["fault"] is None and observed["worker_reaped"]
            assert observed["result"]["status"] == "ok"
            payload = observed["result"]["data"]["output_text"]
            if not recorded:
                delivered = executor_plan(mcp, programs["executor_transaction"], loaded,
                    active.get("a.dispatch", key), prefix="a", key=key, generation=observed["generation"],
                    event=1, outcome="returned", payload=payload)
                delivered.commit(active)
            active.kill()  # durable result exists; application has not consumed it
        with NativeStore(native_store_binary, root, access) as store:
            prior = store.get("a.state", "same-session")
            result = store.get("a.delivery", key)
            pending = interpret(mcp, programs["turn_transaction"],
                current.result(payload, prior=prior["value"], now=int(time.time())), prior["revision"],
                prefix="a", key="same-session", delivery=result["value"], delivery_revision=result["revision"])
            pending.commit(store)
        trace.append(kind)
        current = pending.decision
        if current.action == "done":
            break
    assert trace == ["model", "tool", "model"]
    assert len(scripted_llm.requests) == 2
    assert scripted_llm.requests[1]["messages"][-1]["content"][0]["content"] == "Ada owns this project.\n"
    assert current.values[9:13] == ["Ada owns this project.", "5", "3", "1"]
    # Application interpretation is still fixture-driven. Until settlement,
    # completing PT1 alone is deliberately not terminal API publication.
    with NativeStore(native_store_binary, root, access) as store:
        assert fields(store.get("a.operations", operation)["value"], "OQ2\n", 11)[9] == "accepted"
        assert fields(store.get("a.reservation", operation)["value"], "BR1\n", 9)[7] == "reserved"
    if recorded:
        with NativeSettlement(native_transaction_binary, root, row, bundle=bundle) as settlement:
            result = settlement.apply(operation)
            assert result["context"] == ["done", "reported"] and result["receipt"]
            assert settlement.apply(operation)["receipt"] is None
        with NativeApi(native_service_binary, root.parent, mode="open", credentials=[row]) as api:
            status, result = api.request("GET", "/v1/operations/" + operation)
            assert status == 200 and result["status"] == "done"
            assert result["reply"] == "Ada owns this project."
            assert result["usage"] == {"input_tokens": 5, "output_tokens": 3, "known": True}
            assert result["accounting"] == "reported"
        with NativeStore(native_store_binary, root, access) as store:
            assert fields(store.get("a.reservation", operation)["value"], "BR2\n", 13)[7] == "reported"
            assert fields(store.get("a.budget", "active")["value"], "BH1\n", 5)[2:] == ["0", "0", "0"]
