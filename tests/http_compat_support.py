"""Independent reference inputs plus build-only SIGIL composition."""
from http.client import HTTPMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from product_service import _request_id
from scripts.compose_application import strip_line_comments
from sigil_compose import compose_with_stdlib

FRESH = "0123456789abcdef0123456789abcdef"
VALID = [b"ABCDEFGH", b"01234567", b"a" * 64, b"mixed.Valid_Trace:Id-0001",
         b"__--..::", b"A" * 8, b"0" * 64]
INVALID = [b"", b"short", b"1234567", b"a" * 65, b"leading space",
           b" whitespace", b"whitespace ", b"bad/tab\t", b"bad/newline\n",
           b"bad/carriage\r", b"bad/null\0", b"bad/slash", b"bad\\slash",
           b"<script>", b"a" * 8 + b"\x7f", b"a" * 8 + b"\xff", "é😀label".encode()]


def legacy_request_id(hints):
    headers = HTTPMessage()
    for hint in hints:
        headers.add_header("X-Request-ID", hint.decode("latin1"))
    with patch("product_service.uuid.uuid4", return_value=SimpleNamespace(hex=FRESH)):
        return _request_id(headers)


def record(marker, fields):
    return marker + "".join(f"{len(value.encode()):08d}{value}" for value in fields)


def request_facts(hints=(), *, fresh=FRESH):
    return record("RF1\n", [fresh, str(len(hints)), hints[0].hex() if hints else ""])


def compose_request_id(repo, stdlib):
    source = (Path(repo) / "app/pi/http_request_id.sigil").read_text()
    helpers = (Path(repo) / "app/shared/record_helpers.sigil").read_text()
    utf8 = (Path(repo) / "app/shared/utf8.sigil").read_text()
    assert source.count("// RECORD_HELPERS") == helpers.count("// UTF8_VALIDATOR") == 1
    source = source.replace("// RECORD_HELPERS", helpers.replace("// UTF8_VALIDATOR", utf8))
    return strip_line_comments(compose_with_stdlib(source, ["json"], stdlib).text)
