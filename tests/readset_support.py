"""Build-only shared-codec probe and independent test wire encoder."""

import hashlib
from pathlib import Path

from conftest import PI_ROOT, SIGIL_ROOT
from scripts.compose_application import ApplicationSource, strip_line_comments
from sigil_compose import compose_with_stdlib
from turn_support import record


ROOT = Path(__file__).resolve().parent.parent


def compose_probe(*, fragment_root=ROOT, baseline_root=PI_ROOT, stdlib=SIGIL_ROOT):
    inputs = {}

    def read(root, relative):
        raw = (Path(root) / relative).read_bytes()
        if relative in inputs:
            raise ValueError("duplicate codec build input")
        inputs[relative] = hashlib.sha256(raw).hexdigest()
        return raw.decode()

    helpers = read(baseline_root, "app/shared/record_helpers.sigil")
    if helpers.count("// UTF8_VALIDATOR") != 1:
        raise ValueError("shared codec requires one UTF-8 composition marker")
    helpers = helpers.replace("// UTF8_VALIDATOR", read(baseline_root, "app/shared/utf8.sigil"))
    storage = read(baseline_root, "app/shared/store_protocol.sigil")
    begin, end = "// STORE_READ_HELPERS_BEGIN", "// STORE_READ_HELPERS_END"
    if storage.count(begin) != 1 or storage.count(end) != 1 or storage.index(begin) >= storage.index(end):
        raise ValueError("read helper region must be unique and ordered")
    storage = storage.split(begin)[1].split(end)[0]
    source = "module readset_probe;\nuse sigil::json;\n" + helpers + storage
    source += read(fragment_root, "app/shared/read_set.sigil")
    source += read(fragment_root, "tests/fixtures/readset_probe.sigil")
    full = compose_with_stdlib(source, ["json"], stdlib)
    compact = strip_line_comments(full.text, compact_indent=True)
    if len(compact.encode()) > 65536 or compact.count("pub fn tool_main(") != 1:
        raise ValueError("read-set probe must retain the existing source/entry bounds")
    inputs["tests/readset_support.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return ApplicationSource(compact, inputs, full.stdlib_hash)


def observed(rows, *, status="ok", count=None, batch_marker="RB1\n"):
    """Independent fixture encoding, not a guest decoder or native read."""
    assert 0 <= len(rows) <= 3
    encoded = [record("RR1\n", list(row)) for row in rows]
    batch = record(batch_marker, encoded + [""] * (3 - len(encoded)))
    return record("RM1\n", [status, str(len(rows)) if count is None else count, batch])


def incoming(value="", *, keys=(("app", "one"),), mode="validate", count=None):
    assert 0 <= len(keys) <= 3
    coordinates = [piece for pair in keys for piece in pair]
    coordinates += [""] * (6 - len(coordinates))
    return record("RV1\n", [mode, str(len(keys)) if count is None else count,
                            *coordinates, value])
