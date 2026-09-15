"""Independent framing of result-policy fixtures; the real host owns these facts."""
from conftest import forge_ok
from executor_support import ExecutorDecision
from turn_support import FUEL, fields, record


def facts(*, kind="observed", generation="worker-1", sent="1", reaped="1", fault="", status="ok",
          output_kind="string", payload="observed 😀", length=None):
    if length is None:
        length = len(payload.encode())
    return record("WF1\n", [kind, generation, sent, reaped, fault, status, output_kind, str(length), payload])


def snapshot(observed=None, *, generation="worker-1", claim_generation=None, phase="1", revision="1"):
    claim = "" if revision == "0" else record("SD1\n", ["pi-a.intent", "operation-1:1", "1", claim_generation or generation, phase])
    return record("WR1\n", ["pi-a.intent", "pi-a.dispatch", "pi-a.delivery", "operation-1:1", "1",
                            "opaque intent", revision, claim, generation, observed or facts(generation=generation)])


def completion(mcp, source, observed=None, **kwargs):
    return ExecutorDecision(*fields(forge_ok(mcp, source, snapshot(observed, **kwargs), fuel=FUEL), "ER1\n", 3))
