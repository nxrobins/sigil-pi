"""Test-only transport over the native durable-claim mechanism.

The native mechanism owns real snapshots/receipts and freezes the worker. Test
callers still stand in for authenticated dispatch policy and fixed artifact choice.
"""
from store_support import LIMITS
from turn_support import FUEL, record
from worker_support import NativeWorker


class NativeClaimedWorker(NativeWorker):
    def __init__(self, binary, root, source, access, *, grants=None, timeout_ms=15000):
        super().__init__(binary, source, grants=grants, timeout_ms=timeout_ms,
                         storage={"state_root": str(root), "grants": access, "limits": LIMITS})
        assert self.ready["protocol"] == "sigil-claimed-worker/v1"

    def prepare_request(self, payload, *, key="operation-1:1", prefix="pi-a",
                        timeout_ms=None, fuel=FUEL, time_guard=None):
        return {"op": "prepare", "intent_namespace": f"{prefix}.intent",
                "claim_namespace": f"{prefix}.dispatch", "key": key,
                "input": payload, "fuel": fuel,
                "timeout_ms": self.timeout_ms if timeout_ms is None else timeout_ms,
                "time_guard": time_guard or record("TG1\n", ["0", "9007199254740991"])}

    def prepare(self, payload, **kwargs):
        response = self.request(self.prepare_request(payload, **kwargs))
        assert response["status"] == "ok", response
        self.intent = response["intent"]
        return response["prepared"]

    def claim(self, raw):
        response = self.request({"op": "claim", "batch": raw})
        assert response["status"] == "ok", response
        return response["receipt"]

    def get(self, namespace, key):
        response = self.request({"op": "get", "namespace": namespace, "key": key})
        assert response["status"] == "ok", response
        return response["record"]

    def kill(self):
        self.proc.kill()
        self.proc.wait(timeout=5)
