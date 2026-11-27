"""Build the fixed grantless readiness projection; never probe or authorize."""

import hashlib
from pathlib import Path

from scripts.compose_application import ApplicationSource, strip_line_comments
from sigil_compose import compose_with_stdlib


def compose_readiness_policy(repo, stdlib):
    root, inputs = Path(repo), {}

    def read(relative):
        raw = (root / relative).read_bytes()
        inputs[relative] = hashlib.sha256(raw).hexdigest()
        return raw.decode("utf-8")

    helpers = read("app/shared/record_helpers.sigil")
    if helpers.count("// UTF8_VALIDATOR") != 1:
        raise ValueError("expected one shared UTF-8 marker")
    helpers = helpers.replace("// UTF8_VALIDATOR", read("app/shared/utf8.sigil"))
    correlation = read("app/pi/http_request_id.sigil")
    begin, end = "// REQUEST_ID_POLICY_BEGIN", "// REQUEST_ID_POLICY_END"
    if correlation.count(begin) != 1 or correlation.count(end) != 1 or correlation.index(begin) >= correlation.index(end):
        raise ValueError("expected one ordered correlation policy region")
    correlation = correlation.split(begin)[1].split(end)[0]
    source = "module pi_readiness_policy;\nuse sigil::json;\n" + helpers + correlation
    source += read("app/pi/readiness_projection.sigil")
    source += read("app/pi/readiness_policy.sigil")
    built = compose_with_stdlib(source, ["json"], stdlib)
    compact = strip_line_comments(built.text, compact_indent=True)
    if len(compact.encode()) > 65536 or compact.count("pub fn tool_main(") != 1:
        raise ValueError("readiness policy must preserve the original source and entry bounds")
    inputs["scripts/compose_readiness_policy.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return ApplicationSource(compact, inputs, built.stdlib_hash)
