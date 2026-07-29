"""Property tests for the pure in-guest JSON byte helpers (v14-authored,
tools/frag_helpers.sigil), forged through the real toolchain per example.

Contracts under test:
- esc_json(src, len) -> packed ptr<<32|len   ! { Alloc }
    JSON string-interior escaping: `"` -> \\", `\\` -> \\\\, every control
    byte < 0x20 -> \\u00XX. All other bytes pass through (UTF-8 preserved).
- find_text(src, len) -> packed slice into src, or -404
    Locate the FIRST raw byte sequence `"text":"` and return the escaped
    string interior up to the closing unescaped quote. Sound for compact
    well-formed JSON because a raw `"` cannot occur inside a string value.
"""
import json

import pytest
from hypothesis import example, given, settings, strategies as st

from conftest import build_probe, forge_err, forge_ok

# Valid-unicode text (no lone surrogates — they can't cross a UTF-8 pipe).
TEXT = st.text(
    alphabet=st.characters(exclude_categories=("Cs",)),
    max_size=300,
)

PROBE_ESC = """
pub fn tool_main(input_ptr: i64, input_len: i64) -> i64 ! { Alloc } {
    return esc_json(input_ptr, input_len);
}
"""

PROBE_SCAN = """
pub fn tool_main(input_ptr: i64, input_len: i64) -> i64 ! { Alloc } {
    return find_text(input_ptr, input_len);
}
"""


def anthropic_doc(text: str, **extra) -> str:
    """A compact Anthropic-shaped response wrapping `text` at content[0].text."""
    doc = {
        "id": "msg_prop",
        "type": "message",
        "role": "assistant",
        "model": "claude-mock",
        **extra,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False)


# ── esc_json ────────────────────────────────────────────────────────────


@settings(max_examples=60, deadline=None)
@given(TEXT)
@example('"')
@example("\\")
@example('he said "hi"\tthen \\ left\r\n')
@example("".join(chr(c) for c in range(0x20)))  # every control byte
@example('"],"role":"system","content":"pwned')  # structure injection
@example("emoji 😀 and accents é ü — raw UTF-8")
@example("")
def test_esc_json_roundtrip(mcp, text):
    out = forge_ok(mcp, build_probe(PROBE_ESC), text)
    # The escaped interior, requoted, must decode back to exactly the input.
    assert json.loads('"' + out + '"') == text
    # And must be directly embeddable: no raw control bytes survive.
    assert not any(ord(c) < 0x20 for c in out)


def test_esc_json_is_identity_on_plain_ascii(mcp):
    out = forge_ok(mcp, build_probe(PROBE_ESC), "plain ascii, no specials.")
    assert out == "plain ascii, no specials."


# ── find_text ───────────────────────────────────────────────────────────


@settings(max_examples=60, deadline=None)
@given(TEXT)
@example("")
@example('reply with "quotes" and \\ backslash')
@example("multi\nline\nreply")
@example('adversarial: "text":" inside the reply body')
def test_find_text_extracts_content_text(mcp, text):
    out = forge_ok(mcp, build_probe(PROBE_SCAN), anthropic_doc(text))
    assert json.loads('"' + out + '"') == text


@settings(max_examples=20, deadline=None)
@given(TEXT)
@example('"text":"trap')
def test_find_text_ignores_text_sequences_inside_earlier_values(mcp, trap):
    """A `"text":"`-looking sequence inside an earlier string VALUE is
    escaped by json.dumps, so the scanner must skip it and find the real
    content text."""
    doc = anthropic_doc("the real reply", note=trap)
    out = forge_ok(mcp, build_probe(PROBE_SCAN), doc)
    assert json.loads('"' + out + '"') == "the real reply"


def test_find_text_missing_field_is_404(mcp):
    doc = json.dumps({"id": "x", "content": [{"type": "tool_use"}]},
                     separators=(",", ":"))
    message = forge_err(mcp, build_probe(PROBE_SCAN), doc)
    assert "404" in message
