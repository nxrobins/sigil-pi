"""Independent wire codec; all settlement decisions and commit bytes come from SIGIL."""
from dataclasses import dataclass
import json

from conftest import SIGIL_ROOT, forge_ok
from scripts.compose_application import compose_application
from store_support import LIMITS
from turn_support import FUEL, fields, record
from worker_support import NativeWorker


def snapshot(value, revision=1):
    return record("SR1\n", ["ok", str(revision), value])


def incoming(row, records, *, operation="a" * 64, session="same-session", now=160, bundle="b" * 64):
    return record("SF1\n", [row["facts"], bundle, operation, session, str(now), *records])


@dataclass
class Settlement:
    phase: str
    accounting: str
    raw: str

    @property
    def request(self):
        return json.loads(self.raw) if self.raw else None

    def commit(self, store):
        response = store.raw(self.raw)
        assert response["status"] == "ok", response
        return response["receipt"]


def plan(mcp, source, row, records, **kwargs):
    return Settlement(*fields(forge_ok(mcp, source, incoming(row, records, **kwargs), fuel=FUEL), "TX1\n", 3))


class NativeSettlement(NativeWorker):
    """Trusted fixture bootstrap; requests contain only operation/session lookups."""
    def __init__(self, binary, root, row, *, bundle="b" * 64, patch=None, source=None, prefix="a"):
        def config(worker, directory):
            def value(index):
                return {"kind": "value", "index": index}

            def literal(value):
                return {"kind": "literal", "value": value}

            def read(namespace, key):
                return {"kind": "read", "namespace": f"{prefix}.{namespace}", "key": key}

            result = {"version": 1, "state_root": str(root), "limits": LIMITS,
                "marker": "SF1\n", "values": 2,
                "grants": {f"{prefix}.{n}": "read_write" for n in ("operations", "reservation", "budget")},
                "inputs": [literal(row["facts"]), literal(bundle), value(0), value(1), {"kind": "clock"},
                    read("operations", value(0)), read("state", value(1)), read("reservation", value(0)),
                    read("budget", literal("active"))]}
            result["grants"][f"{prefix}.state"] = "read"
            result["worker"] = worker
            if patch:
                patch(result)
            return result
        super().__init__(binary, source or compose_application("settlement", SIGIL_ROOT).text, storage=config)
        assert self.ready["protocol"] == "sigil-transaction/v1"

    def apply(self, operation="a" * 64, session="same-session"):
        response = self.request({"op": "apply", "values": [operation, session]})
        assert response["status"] == "ok", response
        return response["applied"]
