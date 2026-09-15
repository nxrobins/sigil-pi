"""Build the actual pure policy with the existing unchanged submission decoder."""

import hashlib
from pathlib import Path

from scripts.compose_application import ApplicationSource, strip_line_comments
from sigil_compose import compose_with_stdlib



def compose_request_policy(repo, stdlib):
    baseline_root = fragment_root = Path(repo)
    inputs = {}

    def read(root, name):
        raw = (Path(root) / name).read_bytes()
        if name in inputs:
            raise ValueError("duplicate request-policy input")
        inputs[name] = hashlib.sha256(raw).hexdigest()
        return raw.decode()

    def region(body, name):
        begin, end = f"// {name}_BEGIN", f"// {name}_END"
        if body.count(begin) != 1 or body.count(end) != 1 or body.index(begin) >= body.index(end):
            raise ValueError(f"expected one ordered {name} region")
        return body.split(begin)[1].split(end)[0]

    helpers = read(baseline_root, "app/shared/record_helpers.sigil")
    if helpers.count("// UTF8_VALIDATOR") != 1:
        raise ValueError("expected one shared UTF-8 validator")
    helpers = helpers.replace("// UTF8_VALIDATOR", read(baseline_root, "app/shared/utf8.sigil"))
    storage = read(baseline_root, "app/shared/store_protocol.sigil")
    authority = region(read(baseline_root, "app/pi/api.sigil"), "AUTHORITY_FACTS")
    decoder = region(read(baseline_root, "app/pi/submission.sigil"), "SUBMISSION_DECODER")
    # UTF-8 is already supplied by shared helpers; preserve every decoder token.
    if decoder.count("// UTF8_VALIDATOR") != 1:
        raise ValueError("expected one decoder UTF-8 marker")
    decoder = decoder.replace("// UTF8_VALIDATOR", "")
    source = "module pi_request_policy;\nuse sigil::json;\n" + helpers + storage + decoder + authority
    source += read(fragment_root, "app/pi/request_rate.sigil")
    source += read(fragment_root, "app/pi/request_policy.sigil")
    composed = compose_with_stdlib(source, ["json"], stdlib)
    compact = strip_line_comments(composed.text, compact_indent=True)
    if len(compact.encode()) > 65536 or compact.count("pub fn tool_main(") != 1:
        raise ValueError("policy must retain the original source and entry limits")
    inputs["scripts/compose_request_policy.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return ApplicationSource(compact, inputs, composed.stdlib_hash)
