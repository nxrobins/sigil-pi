"""Build-only SIGIL accounting probe and independent fixture envelopes."""

import hashlib
from pathlib import Path

from api_support import credential
from conftest import PI_ROOT, SIGIL_ROOT
from scripts.compose_application import ApplicationSource, strip_line_comments
from sigil_compose import compose_with_stdlib
from turn_support import record


ROOT = Path(__file__).resolve().parent.parent


def compose_rate_probe(*, fragment_root=ROOT, baseline_root=PI_ROOT, stdlib=SIGIL_ROOT):
    inputs = {}

    def read(root, relative):
        raw = (Path(root) / relative).read_bytes()
        if relative in inputs:
            raise ValueError("duplicate request-rate input")
        inputs[relative] = hashlib.sha256(raw).hexdigest()
        return raw.decode()

    def region(source, name):
        begin, end = f"// {name}_BEGIN", f"// {name}_END"
        if source.count(begin) != 1 or source.count(end) != 1 or source.index(begin) >= source.index(end):
            raise ValueError(f"expected one ordered {name} region")
        return source.split(begin)[1].split(end)[0]

    helpers = read(baseline_root, "app/shared/record_helpers.sigil")
    if helpers.count("// UTF8_VALIDATOR") != 1:
        raise ValueError("expected one UTF-8 composition marker")
    helpers = helpers.replace("// UTF8_VALIDATOR", read(baseline_root, "app/shared/utf8.sigil"))
    storage = read(baseline_root, "app/shared/store_protocol.sigil")
    authority = region(read(baseline_root, "app/pi/api.sigil"), "AUTHORITY_FACTS")
    decoder = read(baseline_root, "app/pi/submission.sigil")
    lexers = "".join(region(decoder, name) for name in
                     ("WHITESPACE_HELPER", "STRING_END_HELPER", "IDENTIFIER_HELPER"))
    source = "module request_rate_probe;\nuse sigil::json;\n" + helpers + storage + lexers + authority
    source += read(fragment_root, "app/pi/request_rate.sigil")
    source += read(fragment_root, "tests/fixtures/request_rate_probe.sigil")
    composed = compose_with_stdlib(source, ["json"], stdlib)
    compact = strip_line_comments(composed.text, compact_indent=True)
    if len(compact.encode()) > 65536 or compact.count("pub fn tool_main(") != 1:
        raise ValueError("request-rate probe must retain the existing source/entry limits")
    inputs["tests/request_rate_support.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return ApplicationSource(compact, inputs, composed.stdlib_hash)


def snapshot(*, revision=0, tenant="tenant-a", window=120, count=1, raw=None, status="ok"):
    value = "" if revision == 0 else record("RW1\n", [tenant, str(window), str(count)])
    return record("SR1\n", [status, str(revision), value if raw is None else raw])


def incoming(*, row=None, limit=2, now=150, fractional=0, observed=None):
    row = credential() if row is None else row
    return record("RV2\n", [row["facts"], str(limit), str(now), str(fractional),
                            snapshot() if observed is None else observed])
