"""Test-only snapshot framing and inspection; SIGIL emits the native commit bytes."""

from dataclasses import dataclass
import json

from conftest import forge_ok
from turn_support import Decision, FUEL, fields, record


def snapshot(event, revision=0, *, delivery="", delivery_revision=None,
             prefix="pi-a", key="conversation"):
    if delivery_revision is None:
        delivery_revision = 1 if delivery else 0
    return record("PX2\n", [f"{prefix}.state", f"{prefix}.intent", f"{prefix}.delivery",
                            key, str(revision), event, str(delivery_revision), delivery])


def observation(event, *, prefix="pi-a", generation="executor-1"):
    values = fields(event, "PE1\n", 8)
    phase = {"ok": "2", "tool_error": "2", "error": "3", "unknown": "4",
             "cancelled_unsent": "5", "expired_unsent": "6"}[values[1]]
    outcome = "returned" if values[1] == "ok" else "failed" if values[1] == "tool_error" else ""
    return record("DR1\n", [f"{prefix}.intent", f"{values[2]}:{values[3]}", "1", generation,
                            phase, outcome, values[4]])


@dataclass
class Transaction:
    raw: str

    @property
    def request(self):
        return json.loads(self.raw)

    @property
    def decision(self):
        writes = self.request["writes"]
        state = writes[0]["value"]
        values = fields(state, "PT1\n", 17)
        name = payload = ""
        if len(writes) == 2:
            intent = fields(writes[1]["value"], "SI1\n", 5)
            assert intent[:3] == [values[0], values[2], values[1]]
            name, payload = intent[3:]
        return Decision(state, values[1], values[2], name, payload)

    def commit(self, store):
        # Crucially, do not reconstruct the native request or choose its checks.
        result = store.raw(self.raw)
        assert result["status"] == "ok", result
        return result["receipt"]


def plan(mcp, source, event, revision=0, **kwargs):
    return Transaction(forge_ok(mcp, source, snapshot(event, revision, **kwargs), fuel=FUEL))
