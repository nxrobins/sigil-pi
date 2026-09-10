"""Test-only wire codec and caller for the SIGIL conversation reducer.

No production state, credentials or application decisions live here. The encoder
is an independent byte oracle and the decoder rejects malformed reducer output.
"""

from dataclasses import dataclass
import json
import re

from conftest import PI_ROOT, SIGIL_ROOT, needs_toolchain
from scripts.compose_application import compose_application


READ_SPEC = {"name": "read_file", "description": "Read one permitted workspace file",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                              "required": ["path"]}}
FUEL = 300_000_000


def record(marker, values):
    assert len(marker.encode()) == 4
    return marker + "".join(f"{len(value.encode()):08d}" + value for value in values)


def fields(value, marker, count):
    raw = value.encode()
    assert raw[:4] == marker.encode()
    at, out = 4, []
    for _ in range(count):
        prefix = raw[at:at + 8]
        assert len(prefix) == 8 and all(48 <= byte <= 57 for byte in prefix)
        length = int(prefix)
        at += 8
        assert at + length <= len(raw)
        out.append(raw[at:at + length].decode())
        at += length
    assert at == len(raw)
    return out


def source():
    needs_toolchain()
    return compose_application("turn", SIGIL_ROOT, root=PI_ROOT).text


def configuration(*, tools=None, max_steps=4, max_tokens=1024, history_cap=1048576,
                  result_cap=65536, model="fixture-model", system="Read only permitted files."):
    return record("PC1\n", [model, str(max_tokens), str(max_steps), system,
                            json.dumps([READ_SPEC] if tools is None else tools),
                            str(history_cap), str(result_cap)])


def submission(message="Read README.md", session="conversation", key="submission-1"):
    return record("PS1\n", [session, message, key])


def event(*, prior="", kind="start", operation="operation-1", sequence=0,
          payload=None, config=None, now=100, deadline=None):
    start = kind == "start"
    return record("PE1\n", [prior, kind, operation, str(sequence),
        submission() if payload is None and start else payload or "",
        configuration() if config is None and start else config or "", str(now),
        "10000" if deadline is None and start else "" if deadline is None else str(deadline)])


def model_reply(content, *, usage=None):
    doc = {"content": content}
    if usage is not None:
        doc["usage"] = usage
    return json.dumps(doc, ensure_ascii=False)


def text_reply(text="The file says hello.", *, usage=None):
    return model_reply([{"type": "text", "text": text}], usage=usage)


def tool_reply(*names, usage=None):
    return model_reply([{"type": "tool_use", "id": f"tool-{i}", "name": name,
                         "input": {"path": "README.md"}} for i, name in enumerate(names)],
                       usage=usage)


@dataclass
class Decision:
    state: str
    action: str
    sequence: str
    tool: str
    input: str

    @property
    def values(self):
        return fields(self.state, "PT1\n", 17)

    def result(self, payload="", *, kind="ok", now=101, **overrides):
        args = {"prior": self.state, "kind": kind, "operation": self.values[0],
                "sequence": self.sequence, "payload": payload, "now": now}
        args.update(overrides)
        return event(**args)


def run(mcp, program, payload):
    result = mcp.forge(program, input=payload, fuel=FUEL)
    assert result["status"] == "ok", result.get("diagnostics")
    decision = Decision(*fields(result["data"]["output_text"], "PD1\n", 5))
    assert decision.sequence == decision.values[2]
    assert decision.action == decision.values[1]
    if decision.action not in {"model", "tool"}:
        assert decision.tool == decision.input == ""
    return decision


def refused(mcp, program, payload, code):
    result = mcp.forge(program, input=payload, fuel=FUEL)
    assert result["status"] == "error", result
    diagnostic = result["diagnostics"][0]
    assert diagnostic["code"] == "R803", diagnostic
    assert re.fullmatch(rf"tool trapped: tool returned error \({code}\)",
                        diagnostic["message"]), diagnostic
