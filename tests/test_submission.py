"""Real SIGIL operation-submission decoding, with an independent byte oracle.

This covers the proposed new route's body, not authentication, durable acceptance,
or a production HTTP handler. The legacy /v1/chat contract is unchanged.
"""

import itertools
import json
import re

import pytest
from hypothesis import example, given, settings, strategies as st

from conftest import PI_ROOT, SIGIL_ROOT, forge_ok, needs_toolchain
from sigil_compose import compose_with_stdlib


RAW_SOURCE = (PI_ROOT / "app/pi/submission.sigil").read_text().replace(
    "// UTF8_VALIDATOR", (PI_ROOT / "app/shared/utf8.sigil").read_text())
FIELDS = ("session", "message", "submission_key")
GOOD = {"session": "project.session-1", "message": "Read the permitted file.",
        "submission_key": "submission-1"}
FUEL = 300_000_000
TEXT = st.text(alphabet=st.characters(exclude_categories=("Cs",)),
               min_size=1, max_size=500)
IDENTIFIER = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", fullmatch=True)


@pytest.fixture(scope="module")
def submission_source():
    needs_toolchain()
    return compose_with_stdlib(RAW_SOURCE, ["json"], SIGIL_ROOT).text


def canonical_bytes(fields):
    values = [fields[key].encode("utf-8") for key in FIELDS]
    return b"PS1\n" + b"".join(f"{len(value):08d}".encode() + value for value in values)


def encode(fields, **kwargs):
    return json.dumps(fields, ensure_ascii=False, **kwargs)


def forge_err(mcp, source, payload, *, fuel):
    result = mcp.forge(source, input=payload, fuel=fuel)
    assert result["status"] == "error", result
    diagnostic = result["diagnostics"][0]
    assert diagnostic["code"] == "R803", diagnostic
    match = re.fullmatch(r"tool trapped: tool returned error \((\d+)\)",
                         diagnostic["message"])
    assert match, diagnostic  # Compile errors and fuel traps are not validation.
    return str(-int(match[1]))


@settings(max_examples=60, deadline=None)
@given(session=IDENTIFIER, message=TEXT, key=IDENTIFIER,
       ascii_only=st.booleans(), spaced=st.booleans())
@example(session="s", message='\x00\n\t"\\ 😀 café', key="k",
         ascii_only=True, spaced=True)
def test_submission_matches_independent_frame_oracle(
        mcp, submission_source, session, message, key, ascii_only, spaced):
    fields = {"session": session, "message": message, "submission_key": key}
    payload = json.dumps(fields, ensure_ascii=ascii_only,
                         indent=2 if spaced else None)
    out = forge_ok(mcp, submission_source, payload, fuel=FUEL)
    assert out.encode("utf-8") == canonical_bytes(fields)


@pytest.mark.parametrize("order", list(itertools.permutations(FIELDS)))
def test_field_order_and_escaped_keys_do_not_change_identity(mcp, submission_source, order):
    fields = {key: GOOD[key] for key in order}
    payload = encode(fields).replace('"session":', '"s\\u0065ssion":')
    payload = payload.replace('"message":', '"messa\\u0067e":')
    payload = payload.replace('"submission_key":', '"submission\\u005fkey":')
    assert forge_ok(mcp, submission_source, payload, fuel=FUEL).encode() == canonical_bytes(GOOD)


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("escaped", [False, True])
def test_duplicate_decoded_fields_are_rejected(mcp, submission_source, field, escaped):
    duplicate = field if not escaped else "\\u%04x%s" % (ord(field[0]), field[1:])
    payload = encode(GOOD)[:-1] + ',"' + duplicate + '":"different"}'
    assert "-40006" in forge_err(mcp, submission_source, payload, fuel=FUEL)


@pytest.mark.parametrize("field", FIELDS)
def test_missing_field_is_rejected(mcp, submission_source, field):
    fields = {key: value for key, value in GOOD.items() if key != field}
    assert "-40002" in forge_err(mcp, submission_source, encode(fields), fuel=FUEL)


@pytest.mark.parametrize("field", ["tenant", "principal", "grants", "deadline",
                                    "tools", "operation", "message ", "Message"])
def test_caller_cannot_add_authority_or_unknown_fields(mcp, submission_source, field):
    fields = {**GOOD, field: "not-authority"}
    assert "-40002" in forge_err(mcp, submission_source, encode(fields), fuel=FUEL)


@pytest.mark.parametrize("field,error", [("session", -40003), ("message", -40004),
                                         ("submission_key", -40005)])
@pytest.mark.parametrize("value", [None, False, 0, 1.25, [], {}])
def test_field_types_are_not_coerced(mcp, submission_source, field, error, value):
    assert str(error) in forge_err(
        mcp, submission_source, encode({**GOOD, field: value}), fuel=FUEL)


@pytest.mark.parametrize("field,error", [("session", -40003), ("submission_key", -40005)])
@pytest.mark.parametrize("value", ["", ".dot", "_prefix", "-prefix", "a/b", "a\\b",
                                    "a b", "é", "a\n", "a\x00", "a" * 129])
def test_identifiers_are_bounded_ascii(mcp, submission_source, field, error, value):
    assert str(error) in forge_err(
        mcp, submission_source, encode({**GOOD, field: value}), fuel=FUEL)


@pytest.mark.parametrize("payload", ["", " ", "{", "{}x", encode(GOOD) + "x",
    encode(GOOD)[:-1], encode(GOOD)[:-1] + ",}", encode(GOOD).replace(",", "", 1),
    encode(GOOD).replace(":", "", 1), encode(GOOD).replace(":", ":\x0b", 1),
    encode(GOOD).replace("Read the permitted file.", "bad\\q"),
    encode(GOOD).replace("Read the permitted file.", "bad\\uXXXX"),
    encode(GOOD).replace("Read the permitted file.", "bad\\ud800"),
    encode(GOOD).replace("Read the permitted file.", "bad\\udc00"),
    encode(GOOD).replace("Read the permitted file.", "bad\\ud800\\u0061"),
    encode(GOOD).replace("Read the permitted file.", "bad\ntext"),
])
def test_malformed_input_is_never_accepted(mcp, submission_source, payload):
    # An empty object plus garbage may be diagnosed as invalid schema first;
    # in either case the decoder must reject the entire request.
    error = forge_err(mcp, submission_source, payload, fuel=FUEL)
    assert "-40001" in error or "-40002" in error


@pytest.mark.parametrize("payload", ["{}", "[]", "null", "true", "42", '"text"'])
def test_non_submission_documents_are_rejected(mcp, submission_source, payload):
    assert "-40002" in forge_err(mcp, submission_source, payload, fuel=FUEL)


@pytest.mark.parametrize("message", ["x" * 262144, "😀" * 65536])
def test_maximum_message_bytes_are_accepted(mcp, submission_source, message):
    fields = {**GOOD, "session": "s" * 128, "submission_key": "k" * 128,
              "message": message}
    assert forge_ok(mcp, submission_source, encode(fields), fuel=FUEL).encode() == canonical_bytes(fields)


@pytest.mark.parametrize("message", ["x" * 262145, "😀" * 65536 + "x"])
def test_message_limit_is_decoded_utf8_bytes(mcp, submission_source, message):
    assert "-41302" in forge_err(
        mcp, submission_source, encode({**GOOD, "message": message}), fuel=FUEL)


def test_empty_message_is_rejected_but_whitespace_is_preserved(mcp, submission_source):
    assert "-40004" in forge_err(
        mcp, submission_source, encode({**GOOD, "message": ""}), fuel=FUEL)
    fields = {**GOOD, "message": " \n\t"}
    assert forge_ok(mcp, submission_source, encode(fields), fuel=FUEL).encode() == canonical_bytes(fields)


def test_request_limit_includes_encoded_bytes_and_whitespace(mcp, submission_source):
    doc = encode(GOOD)
    exact = doc + " " * (1048576 - len(doc.encode()))
    assert forge_ok(mcp, submission_source, exact, fuel=FUEL).encode() == canonical_bytes(GOOD)
    assert "-41301" in forge_err(mcp, submission_source, exact + " ", fuel=FUEL)
    # Decoded content may fit while its JSON escape representation does not.
    escaped = json.dumps({**GOOD, "message": "\x00" * 200000})
    assert "-41301" in forge_err(mcp, submission_source, escaped, fuel=FUEL)


def test_normalization_does_not_erase_distinct_payloads(mcp, submission_source):
    a = forge_ok(mcp, submission_source, encode({**GOOD, "message": "é"}), fuel=FUEL)
    b = forge_ok(mcp, submission_source, encode({**GOOD, "message": "e\u0301"}), fuel=FUEL)
    assert a != b


# MCP transports UTF-8 strings. This TEST-ONLY adapter reconstructs raw octets in
# guest memory so malformed UTF-8 reaches the production decoder unmodified.
HEX_MAIN = r'''
pub fn tool_main(input_ptr: i64, input_len: i64) -> i64 @Internal ! { Alloc } {
    let out: i64 @Internal = alloc(input_len / 2);
    let mut i: i64 @Internal = 0;
    while i < input_len {
        let a: i64 @Internal = load8(input_ptr + i);
        let b: i64 @Internal = load8(input_ptr + i + 1);
        let mut hi: i64 @Internal = a - 48;
        let mut lo: i64 @Internal = b - 48;
        if a >= 97 { hi = a - 87; }
        if b >= 97 { lo = b - 87; }
        store8(out + i / 2, hi * 16 + lo);
        i += 2;
    }
    return decode_submission(out, input_len / 2);
}
'''


@pytest.mark.parametrize("bad", [b"\x80", b"\xc0\xaf", b"\xc1\xbf", b"\xc2",
    b"\xc2x", b"\xe0\x9f\xbf", b"\xed\xa0\x80", b"\xed\xbf\xbf",
    b"\xe2\x82", b"\xf0\x8f\xbf\xbf", b"\xf4\x90\x80\x80",
    b"\xf5\x80\x80\x80", b"\xf8\x80\x80\x80\x80", b"\xff"])
def test_malformed_utf8_reaches_and_is_rejected_by_sigil(mcp, submission_source, bad):
    assert RAW_SOURCE.count("pub fn tool_main(") == 1
    raw = RAW_SOURCE.split("pub fn tool_main(", 1)[0] + HEX_MAIN
    source = compose_with_stdlib(raw, ["json"], SIGIL_ROOT).text
    payload = encode(GOOD).encode().replace(b"Read the permitted file.", bad)
    assert "-40001" in forge_err(mcp, source, payload.hex(), fuel=FUEL)


def test_submission_component_remains_sigil_and_grantless():
    code = "\n".join(line.split("//", 1)[0] for line in RAW_SOURCE.splitlines())
    assert 'extern "C"' not in code
    assert "#[trusted]" not in code
    assert "use sigil::json;" in code
    assert "AUTHORSHIP: hand-authored" in RAW_SOURCE
    assert "! { Alloc }" in code
