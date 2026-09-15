"""Independent AH7 fixtures; caller-declared receipts here are NOT native proof."""

import json

from api_support import envelope as original_envelope
from conftest import PI_ROOT, SIGIL_ROOT, forge_ok
from emergency_support import DOMAIN
from http_compat_support import request_facts
from scripts.compose_http_entry import HEADER_NAMES
from scripts.compose_readiness_admission import COMMANDS, compose_readiness_admission
from turn_support import FUEL, fields, record


def source(limit=2):
    return compose_readiness_admission(PI_ROOT, SIGIL_ROOT, request_limit=limit)


def envelope(*, presentation="1", fraction="0", hints=(), clock_ns=120_250_000_000,
             domain=DOMAIN, lifecycle=None, process=None, **kwargs):
    values = fields(original_envelope(
        functions=["admission", "emergency_policy", "history", "listing", "request_policy"],
        **kwargs), "AH3\n", 12)
    booting = values[11] == "boot"
    process = record("PF1\n", [record("MC1\n", [domain, str(clock_ns)]),
        record("LF1\n", ["ok", "", "0"]) if lifecycle is None else lifecycle]) if process is None else process
    return record("AH7\n", [*values, COMMANDS, "" if booting else request_facts(hints),
        json.dumps(HEADER_NAMES, separators=(",", ":")), "" if booting else presentation, fraction,
        "" if booting else process])


def step(mcp, entry, **kwargs):
    return fields(forge_ok(mcp, entry, envelope(**kwargs), fuel=FUEL), "HC7\n", 5)


def durable_read(revision="0", presence="0", value="", error="", origin="", status="ok"):
    return record("DR2\n", [status, revision, presence, value, error, origin])


def durable_commit(revision="1", error="", origin="", status="ok"):
    return record("DC2\n", [status, revision, error, origin])
