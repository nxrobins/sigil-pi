"""Test-only framing/forwarding. Shared SIGIL owns executor transaction decisions."""

from dataclasses import dataclass
import json

from conftest import forge_ok
from turn_support import FUEL, fields, record


def executor_snapshot(intent, dispatch=None, *, event=0, generation="executor-1",
                      outcome="", payload="", prefix="pi-a", key="operation-1:1"):
    dispatch = {"revision": 0, "value": None} if dispatch is None else dispatch
    return record("EX1\n", [f"{prefix}.intent", f"{prefix}.dispatch", f"{prefix}.delivery", key,
                            str(intent["revision"]), intent["value"] or "", str(dispatch["revision"]),
                            dispatch["value"] or "", str(event), generation, outcome, payload])


@dataclass
class ExecutorDecision:
    phase: str
    continuation: str
    raw: str

    @property
    def request(self):
        return json.loads(self.raw) if self.raw else None

    def commit(self, store):
        assert self.raw, "a no-op has no commit or execution permission"
        result = store.raw(self.raw)
        assert result["status"] == "ok", result
        return result["receipt"]


def executor_plan(mcp, program, intent, dispatch=None, **kwargs):
    raw = forge_ok(mcp, program, executor_snapshot(intent, dispatch, **kwargs), fuel=FUEL)
    return ExecutorDecision(*fields(raw, "ER1\n", 3))
