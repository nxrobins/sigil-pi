"""Build the fixed grantless SIGIL emergency policy; no runtime policy in Python."""

import hashlib
from pathlib import Path

from scripts.compose_application import ApplicationSource, strip_line_comments
from sigil_compose import compose_with_stdlib


def compose_emergency_policy(repo, stdlib):
    root, inputs = Path(repo), {}

    def read(relative):
        if relative in inputs:
            raise ValueError("duplicate emergency-policy input")
        raw = (root / relative).read_bytes()
        inputs[relative] = hashlib.sha256(raw).hexdigest()
        return raw.decode("utf-8")

    def region(body, name):
        begin, end = f"// {name}_BEGIN", f"// {name}_END"
        if body.count(begin) != 1 or body.count(end) != 1 or body.index(begin) >= body.index(end):
            raise ValueError(f"expected one ordered {name} region")
        return body.split(begin)[1].split(end)[0]

    helpers = read("app/shared/record_helpers.sigil")
    if helpers.count("// UTF8_VALIDATOR") != 1:
        raise ValueError("expected one shared UTF-8 marker")
    helpers = helpers.replace("// UTF8_VALIDATOR", read("app/shared/utf8.sigil"))
    storage = read("app/shared/store_protocol.sigil")
    authority = region(read("app/pi/api.sigil"), "AUTHORITY_FACTS")
    decoder = read("app/pi/submission.sigil")
    lexers = "".join(region(decoder, name) for name in
                     ("WHITESPACE_HELPER", "STRING_END_HELPER", "IDENTIFIER_HELPER"))
    source = "module pi_emergency_policy;\nuse sigil::json;\n" + helpers + storage + lexers + authority
    policy = read("app/pi/emergency_policy.sigil")
    if policy.count("// EMERGENCY_GUARDS") != 1:
        raise ValueError("expected one shared emergency guard marker")
    source += policy.replace("// EMERGENCY_GUARDS", read("app/pi/emergency_guards.sigil"))
    built = compose_with_stdlib(source, ["json"], stdlib)
    compact = strip_line_comments(built.text, compact_indent=True)
    if len(compact.encode()) > 65536 or compact.count("pub fn tool_main(") != 1:
        raise ValueError("emergency policy must preserve the original source/entry limits")
    inputs["scripts/compose_emergency_policy.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return ApplicationSource(compact, inputs, built.stdlib_hash)
