"""Build-only policy composition and independent wire fixtures; no runtime policy."""

from pathlib import Path

from api_support import credential
from conftest import SIGIL_ROOT
from scripts.compose_emergency_policy import compose_emergency_policy
from turn_support import fields, record

ROOT = Path(__file__).resolve().parent.parent
DOMAIN = "c" * 64
NATIVE_ERRORS = ("invalid", "denied", "conflict", "already_exists", "missing", "busy",
                 "corrupt", "storage", "limit", "commit_uncertain", "reopen_required")


def source():
    return compose_emergency_policy(ROOT, SIGIL_ROOT)


def cause(label="storage", marker="DR2\n", *, origin="storage"):
    if marker == "DR2\n":
        return record(marker, ["error", "0", "0", "", label, origin])
    return record(marker, ["error", "0", label, origin])


def window(events, *, tenant="tenant-a", domain=DOMAIN):
    return record("EW1\n", [tenant, domain, str(len(events)),
                            record("ET1\n", [str(at) for at in events])])


def snapshot(*, revision=0, events=None, tenant="tenant-a", domain=DOMAIN, raw=None,
             status="ok", presence=None, error=""):
    if raw is None:
        raw = "" if revision == 0 else window([100_000_000_000] if events is None else events,
                                             tenant=tenant, domain=domain)
    if presence is None:
        presence = "0" if revision == 0 else "1"
    return record("VR1\n", [status, str(revision), str(presence), raw, error])


def incoming(*, row=None, limit=2, wall=150, clock_ns=120_250_000_000, domain=DOMAIN,
             clock=None, failed=None, observed=None, method="GET", path="/v1/ready"):
    return record("ER1\n", [credential()["facts"] if row is None else row["facts"],
        str(limit), str(wall), record("MC1\n", [domain, str(clock_ns)]) if clock is None else clock,
        cause() if failed is None else failed, snapshot() if observed is None else observed, method, path])


def retained(value):
    tenant, domain, count, tape = fields(value, "EW1\n", 4)
    events = fields(tape, "ET1\n", int(count))
    return tenant, domain, [int(at) for at in events]
