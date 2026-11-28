"""Build-only staged AV2 pairing; no runtime authority policy in Python.

Copy exact existing SIGIL helpers; version the expanded bootstrap protocol.
The existing production/v8 recipes and AV1/AP2 semantics remain unchanged.
"""

import hashlib
from pathlib import Path

from scripts import sigil_layout
from scripts.compose_application import ApplicationSource, compose_application, strip_line_comments
from scripts.sigil_layout import compact_layout
from scripts.sigil_omit import _functions, _tokens


ROOT = Path(__file__).resolve().parent.parent
MOVED = {
    "configured": "128c39054889f0f696319a24631cae6a43e881d94e9fd7c3e5528ae796a8afa3",
    "same_tools": "f28763db72996b730a621111b4bbd142ee109d40ab835d330c2ec3b68ea98fa7",
}
BOOT = "fb041f5d27e0618d102ae5cd9ac76974d8d928769edd761da5fd1fb43ebdb539"


def compact(raw):
    return compact_layout(strip_line_comments(raw, compact_indent=True))


def definition(source, name, digest):
    start, end, public = _functions(_tokens(source))[name]
    result = source[start:end]
    if public or hashlib.sha256(result.encode()).hexdigest() != digest:
        raise ValueError(f"reviewed bootstrap definition changed: {name}")
    return result


def replace_entry_bootstrap(body, inputs):
    original = definition(body, "boot", BOOT)
    path = ROOT / "app/pi/bootstrap_entry.sigil"
    raw = path.read_bytes()
    inputs["app/pi/bootstrap_entry.sigil"] = hashlib.sha256(raw).hexdigest()
    inputs["scripts/compose_bootstrap_admission.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if body.count(original) != 1:
        raise ValueError("one complete original boot definition required")
    return body.replace(original, compact(raw.decode()))


def compose_bootstrap_admission(repo, stdlib):
    baseline = compose_application("admission", stdlib, root=repo)
    inputs = dict(baseline.input_hashes)

    def read(root, name):
        raw = (Path(root) / name).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if name in inputs and inputs[name] != digest:
            raise ValueError("bootstrap composition input collision")
        inputs[name] = digest
        return raw.decode()

    def region(raw, name):
        begin, end = f"// {name}_BEGIN", f"// {name}_END"
        if raw.count(begin) != 1 or raw.count(end) != 1 or raw.index(begin) >= raw.index(end):
            raise ValueError(f"one ordered bootstrap helper region required: {name}")
        return raw.split(begin)[1].split(end)[0]

    api = read(repo, "app/pi/api.sigil")
    canonical = compact(api)
    helpers = region(api, "AUTHORITY_FACTS")
    decoder = read(repo, "app/pi/submission.sigil")
    helpers += "".join(region(decoder, name) for name in
                       ("WHITESPACE_HELPER", "STRING_END_HELPER", "IDENTIFIER_HELPER"))
    helpers += "".join(definition(canonical, name, digest) for name, digest in MOVED.items())
    body = baseline.text
    signature = "pub fn tool_main(input_ptr: i64, input_len: i64)"
    if body.count(signature) != 1:
        raise ValueError("exact original admission entry required")
    body = body.replace(signature, "fn admission_original(input_ptr: i64 @Internal, input_len: i64 @Internal)")
    text = compact(body + helpers + read(ROOT, "app/pi/bootstrap_validation.sigil"))
    if len(text.encode()) > 65536 or text.count("pub fn tool_main(") != 1:
        raise ValueError("bootstrap admission must preserve original source/entry ceilings")
    inputs["scripts/compose_bootstrap_admission.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    inputs["scripts/sigil_layout.py"] = hashlib.sha256(Path(sigil_layout.__file__).read_bytes()).hexdigest()
    inputs["scripts/sigil_omit.py"] = hashlib.sha256((ROOT / "scripts/sigil_omit.py").read_bytes()).hexdigest()
    return ApplicationSource(text, inputs, baseline.stdlib_hash)
