"""Trusted test bootstrap for native-bound SIGIL policy, not a product dispatcher."""
import hashlib

from claimed_support import NativeClaimedWorker
from conftest import SIGIL_ROOT
from scripts.compose_application import compose_application
from store_support import LIMITS
from turn_support import record
from worker_support import NativeWorker


class NativePolicyWorker(NativeClaimedWorker):
    def __init__(self, binary, root, source, *, facts, argument, grants, role="model",
                 alias="provider", prefix="a", tenant="tenant-a", binding_patch=None,
                 policy_patch=None, timeout_ms=15000, policy_source=None, bundle="b" * 64,
                 recorded=False, recorder_source=None, recorder_patch=None, owned=False):
        assert not owned or recorded, "owned execution requires native recording"
        self.operation = "a" * 64
        self.prefix = prefix

        def bootstrap(worker, directory):
            text = policy_source or compose_application("dispatch", SIGIL_ROOT).text
            path = directory / "policy.sigil"
            path.write_text(text)
            policy_worker = {**worker, "source": str(path),
                "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "net": [], "fs": [], "secret_env": {}}
            selected_grant = "127.0.0.1" if role == "model" else argument
            bound = [tenant, role, worker["source_sha256"], worker["runtime_sha256"],
                     argument, selected_grant, "anthropic" if role == "model" else ""]
            if binding_patch:
                for index, value in binding_patch.items():
                    bound[index] = value
            reads = {f"{prefix}.{n}": "read" for n in ("operations", "reservation", "state", "intent", "budget")}

            def value(index):
                return {"kind": "value", "index": index}

            def literal(text):
                return {"kind": "literal", "value": text}

            def read(namespace, key):
                return {"kind": "read", "namespace": f"{prefix}.{namespace}", "key": key}

            policy = {"worker": policy_worker, "marker": "DF1\n", "values": 3,
                "inputs": [literal(facts), literal(bundle), value(0), {"kind": "clock"},
                    read("operations", value(0)), read("reservation", value(0)),
                    read("state", value(1)), read("intent", value(2)),
                    read("budget", literal("active")), value(2), {"kind": "worker_facts"}],
                "read_grants": reads, "alias": alias, "binding": record("EB1\n", bound),
                "claim_namespace": f"{prefix}.dispatch"}
            if policy_patch:
                policy_patch(policy)
            config = {"version": 4 if owned else 3 if recorded else 2, "state_root": str(root), "limits": LIMITS,
                "grants": {f"{prefix}.intent": "read", f"{prefix}.dispatch": "read_write",
                           f"{prefix}.delivery": "create_only"}, "policy": policy}
            if recorded:
                code = recorder_source or compose_application("worker_completion", SIGIL_ROOT).text
                record_path = directory / "recorder.sigil"
                record_path.write_text(code)
                config["recorder"] = {"delivery_namespace": f"{prefix}.delivery", "worker": {
                    **policy_worker, "source": str(record_path),
                    "source_sha256": hashlib.sha256(code.encode()).hexdigest()}}
                if recorder_patch:
                    recorder_patch(config)
            return config

        NativeWorker.__init__(self, binary, source, grants=grants,
                              timeout_ms=timeout_ms, storage=bootstrap)
        assert self.ready["protocol"] == f"sigil-claimed-worker/v{4 if owned else 3 if recorded else 2}"

    def authorize_request(self, *, operation=None, session="same-session", sequence="1"):
        operation = self.operation if operation is None else operation
        return {"op": "authorize", "values": [operation, session, f"{operation}:{sequence}"]}

    def authorize(self, **kwargs):
        response = self.request(self.authorize_request(**kwargs))
        assert response["status"] == "ok", response
        self.intent = response["intent"]
        self.context = response["context"]
        return response["prepared"]

    def run_recorded(self, ready):
        response = self.request({"op": "run", "ticket": ready["ticket"]})
        assert response["status"] == "ok", response
        return response["completion"]
