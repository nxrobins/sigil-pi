"""Independent dispatch framing; snapshots and bindings here are test substitutes."""
import json

from api_support import credential
from conftest import SIGIL_ROOT, forge_ok
from scripts.compose_application import compose_application
from test_admission import incoming
from turn_support import FUEL, fields, record


def effect(*, alias="provider", role="model", tenant="tenant-a", argument="http://127.0.0.1:1234/v1/messages",
           grant="127.0.0.1", secret="anthropic", source="c" * 64, runtime="d" * 64):
    bound = record("EB1\n", [tenant, role, source, runtime, argument, grant, secret])
    return record("EF1\n", [alias, bound, source, runtime, json.dumps([grant] if role == "model" else []),
        json.dumps([grant] if role == "read_file" else []), json.dumps([secret] if role == "model" else [])])


def initial(mcp, *, row=None, now=151, **kwargs):
    row = credential() if row is None else row
    built = compose_application("admission", SIGIL_ROOT)
    output = forge_ok(mcp, built.text, incoming(row=row, **kwargs), fuel=FUEL)
    result = fields(output, "AD1\n", 3)
    assert result[0] == "ok"
    writes = json.loads(result[1])["writes"]
    operation = writes[1]["key"]
    values = [row["facts"], "b" * 64, operation, str(now)]
    values += [record("SR1\n", ["ok", "1", writes[i]["value"]]) for i in (1, 5, 2, 3, 4)]
    return values + [operation + ":1", effect()]


def change_snapshot(values, index, marker, field, value, *, revision=None):
    seen = fields(values[index], "SR1\n", 3)
    counts = {"OQ2\n": 11, "BR1\n": 9, "PT1\n": 17, "SI1\n": 5, "BH1\n": 5}
    body = fields(seen[2], marker, counts[marker])
    body[field] = value
    seen[2] = record(marker, body)
    if revision is not None:
        seen[1] = str(revision)
    values[index] = record("SR1\n", seen)


def proposal(mcp, program, values):
    selected = fields(forge_ok(mcp, program, record("DF1\n", values), fuel=FUEL), "DW1\n", 7)
    return selected[:6] + fields(selected[6], "DX1\n", 5)
