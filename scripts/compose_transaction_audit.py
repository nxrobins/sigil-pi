"""Build-only composition of the fixed pure transaction-audit policy."""
import hashlib
from pathlib import Path

from scripts.compose_application import ApplicationSource, strip_line_comments
from sigil_compose import compose_with_stdlib

ROOT = Path(__file__).resolve().parent.parent


def compose_transaction_audit(repo, stdlib):
    inputs = {}

    def read(root, relative):
        raw = (Path(root) / relative).read_bytes()
        inputs[relative] = hashlib.sha256(raw).hexdigest()
        return raw.decode()

    helpers = read(repo, "app/shared/record_helpers.sigil")
    if helpers.count("// UTF8_VALIDATOR") != 1:
        raise ValueError("require one original UTF-8 helper marker")
    helpers = helpers.replace("// UTF8_VALIDATOR", read(repo, "app/shared/utf8.sigil"))
    storage = read(repo, "app/shared/store_protocol.sigil")
    begin, end = "// STORE_READ_HELPERS_BEGIN", "// STORE_READ_HELPERS_END"
    if storage.count(begin) != 1 or storage.count(end) != 1 or storage.index(begin) >= storage.index(end):
        raise ValueError("require one ordered original storage helper region")
    helpers += storage.split(begin)[1].split(end)[0]
    source = "module pi_transaction_audit;\nuse sigil::json;\n" + helpers
    source += read(ROOT, "app/pi/transaction_audit.sigil")
    built = compose_with_stdlib(source, ["json"], stdlib)
    text = strip_line_comments(built.text, compact_indent=True)
    if len(text.encode()) > 65536 or text.count("pub fn tool_main(") != 1:
        raise ValueError("preserve original compiler source and entry limits")
    inputs["scripts/compose_transaction_audit.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return ApplicationSource(text, inputs, built.stdlib_hash)
