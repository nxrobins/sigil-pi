"""Static test deployment configuration only; never advances an application turn."""
import hashlib
import select
import time

from api_support import LIMITS, NativeApi, credential
from conftest import FIXED_EVALUATOR_BIN, MCP_BIN, SIGIL_ROOT
from scripts.compose_application import compose_application
from turn_support import FUEL, fields, record
from worker_support import runtime_digest


def configuration(root, programs, endpoint, workspace, rows=None):
    root.mkdir(parents=True, exist_ok=True)

    def worker(name, source, *, net=None, fs=None, secrets=None, runtime=MCP_BIN):
        path = root / (name + ".sigil")
        path.write_text(source)
        return {"version": 1, "runtime": str(runtime), "runtime_sha256": runtime_digest(str(runtime)),
            "source": str(path), "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "max_fuel": FUEL, "max_timeout_ms": 15000, "net": net or [], "fs": fs or [], "secret_env": secrets or {}}

    def component(name):
        runtime = FIXED_EVALUATOR_BIN if name in {"api", "admission", "history", "coordinator"} else MCP_BIN
        return worker(name, compose_application(name, SIGIL_ROOT).text, runtime=runtime)

    def value(index):
        return {"kind": "value", "index": index}

    def literal(value):
        return {"kind": "literal", "value": value}

    def read(ns, key):
        return {"kind": "read", "namespace": ns, "key": key}

    rows = rows or [credential()]
    result = {"version": 4, "state_root": str(root / "records"), "limits": LIMITS,
              "credentials": rows, "worker": component("api"),
              "functions": {name: component(name) for name in ("admission", "history")}}
    pure = {name: component(name) for name in ("dispatch_cancellable", "worker_completion", "turn_completion", "settlement", "coordinator", "preclaim_cancellable")}
    participants = []
    for index, row in enumerate(rows):
        facts = fields(row["facts"], "CF2\n", 15)
        ops, state, intent, budget, reservation = facts[8], facts[10], facts[11], facts[12], facts[13]
        claim, delivery = f"executor{index}.claim", f"executor{index}.delivery"
        area = workspace[index] if isinstance(workspace, list) else workspace
        area = str(area)
        effects = {}
        for alias, role in [("provider", "model"), ("reader", "read_file")]:
            effect_worker = worker(f"effect-{index}-{alias}", programs["provider" if role == "model" else "read_file"],
                net=["127.0.0.1"] if role == "model" else [], fs=[area] if role == "read_file" else [],
                secrets={"anthropic": "PI_AUTOMATIC_FIXTURE_SECRET"} if role == "model" else {})
            binding = record("EB1\n", [facts[1], role, effect_worker["source_sha256"], effect_worker["runtime_sha256"],
                endpoint if role == "model" else area, "127.0.0.1" if role == "model" else area,
                "anthropic" if role == "model" else ""])
            policy = {"worker": pure["dispatch_cancellable"], "marker": "DF2\n", "values": 4,
                "inputs": [{"kind": "credential_facts"}, {"kind": "bundle"}, value(0), {"kind": "clock"},
                    read(ops, value(0)), read(reservation, value(0)), read(state, value(1)), read(intent, value(2)),
                    read(budget, literal("active")), value(2), {"kind": "worker_facts"}, value(3), read(ops, value(3))],
                "read_grants": dict.fromkeys([ops, reservation, state, intent, budget], "read"),
                "alias": alias, "binding": binding, "claim_namespace": claim}
            effects[alias] = {"worker": effect_worker, "policy": policy,
                "recorder": {"worker": pure["worker_completion"], "delivery_namespace": delivery},
                "grants": {intent: "read", claim: "read_write", delivery: "create_only"}}
        interpret = {"worker": pure["turn_completion"], "marker": "TF1\n", "values": 4,
            "grants": {state: "read_write", intent: "read_write", delivery: "read"},
            "inputs": [literal(state), literal(intent), literal(delivery), value(0), value(1), value(2), value(3),
                {"kind": "clock"}, read(state, value(0)), read(intent, value(2)), read(delivery, value(2)), read(intent, value(3))]}
        settle = {"worker": pure["settlement"], "marker": "SF1\n", "values": 2,
            "grants": {state: "read", ops: "read_write", reservation: "read_write", budget: "read_write"},
            "inputs": [{"kind": "credential_facts"}, {"kind": "bundle"}, value(0), value(1), {"kind": "clock"},
                read(ops, value(0)), read(state, value(1)), read(reservation, value(0)), read(budget, literal("active"))]}
        preclaim = {"worker": pure["preclaim_cancellable"], "marker": "UF2\n", "values": 4,
            "grants": {ops: "read_write", reservation: "read_write", state: "read_write", budget: "read_write",
                       intent: "read", claim: "read", delivery: "read"},
            "inputs": [{"kind": "credential_facts"}, {"kind": "bundle"}, value(0), value(1), value(2),
                literal(claim), literal(delivery), {"kind": "clock"}, read(ops, value(0)), read(reservation, value(0)),
                read(state, value(1)), read(intent, value(2)), read(budget, literal("active")), read(claim, value(2)), read(delivery, value(2)),
                value(3), read(ops, value(3))]}
        participants.append({"credential_sha256": row["sha256"], "worker": pure["coordinator"],
            "binding": record("LB3\n", ["provider", "reader", "interpret", "settle", "preclaim"]),
            "claim_namespace": claim, "delivery_namespace": delivery,
            "read_grants": dict.fromkeys([*row["grants"], claim, delivery], "read"),
            "effects": effects, "transactions": {"interpret": interpret, "settle": settle, "preclaim": preclaim}})
    result["automatic"] = {"participants": participants}
    return result


def service(binary, root, programs, endpoint, workspace, *, rows=None, mode="init", patch=None):
    config = configuration(root, programs, endpoint, workspace, rows)
    if patch:
        patch(config)
    return NativeApi(binary, root, config=config, mode=mode)


def wait_operation(api, operation, *, token=None):
    deadline = time.monotonic() + 115
    while time.monotonic() < deadline:
        kwargs = {} if token is None else {"token": token}
        status, body = api.request("GET", "/v1/operations/" + operation, **kwargs)
        assert status == 200, (status, body)
        if body["status"] != "accepted":
            return body
        ready, _, _ = select.select([api.proc.stderr], [], [], 0)
        if ready:
            error = api.proc.stderr.readline()
            raise AssertionError("automatic service refused a mechanism: " + error)
        time.sleep(0.05)
    raise AssertionError("automatic service did not publish a terminal operation")
