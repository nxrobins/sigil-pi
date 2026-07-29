"""Property tests for parse_reply.sigil — the M3 ring bridge.

An INNER-RING pure tool (composes stdlib `json`, forged as its own program —
the two-forge bridge; `grant(&cap,...)` is capability machinery, not a
cross-ring call path). Input: a raw Anthropic /v1/messages response. Output:
one frame per content block, in order:

    tag byte        't' text | 'u' tool_use | '?' other
    8 ASCII digits  zero-padded decimal payload length
    payload         t: DECODED text bytes (json v2 unescaping, incl \\uXXXX)
                    u: id + 0x1F + name + 0x1F + raw balanced input JSON
                       (id first — tool_result messages must echo it)
                    ?: the raw type string bytes

Errors: json codes propagate (-404 no content field, -400 malformed).
Unlike M2's find_text scanner this handles whitespace, multiple blocks,
and tool_use — property-tested against a Python reference codec.
"""
import json

import pytest
from hypothesis import example, given, settings, strategies as st

from conftest import PI_ROOT, SIGIL_ROOT, forge_err, forge_ok

TEXT = st.text(alphabet=st.characters(exclude_categories=("Cs",)), max_size=120)
NAME = st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=20)
# tool_use inputs: modest JSON objects (nested one level, unicode values)
JSON_VAL = st.one_of(st.integers(-1000, 1000), TEXT, st.booleans(), st.none())
TOOL_INPUT = st.dictionaries(NAME, st.one_of(JSON_VAL, st.dictionaries(NAME, JSON_VAL, max_size=2)), max_size=4)

BLOCK = st.one_of(
    st.tuples(st.just("text"), TEXT),
    st.tuples(st.just("tool_use"), st.tuples(NAME, TOOL_INPUT)),
)
BLOCKS = st.lists(BLOCK, min_size=1, max_size=4)


def parse_reply_source() -> str:
    """The uncomposed source, composed with stdlib json at forge time."""
    import sys
    sys.path.insert(0, str(SIGIL_ROOT / "bench" / "src"))
    from sigil_bench.compose import compose_with_stdlib
    src_path = PI_ROOT / "tools" / "parse_reply.sigil"
    assert src_path.exists(), "tools/parse_reply.sigil missing (v14 authors it — M3)"
    return compose_with_stdlib(src_path.read_text(), ["json"], SIGIL_ROOT).text


def make_response(blocks, spaced=False) -> str:
    content = []
    for kind, payload in blocks:
        if kind == "text":
            content.append({"type": "text", "text": payload})
        else:
            name, tool_input = payload
            content.append({"type": "tool_use", "id": f"tu_{len(content)}",
                            "name": name, "input": tool_input})
    doc = {"id": "msg_1", "type": "message", "role": "assistant",
           "model": "claude-mock", "content": content,
           "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}}
    if spaced:  # the scanner-killer: non-compact JSON must work now
        return json.dumps(doc, indent=2, ensure_ascii=False)
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False)


def reference_frames(response_json: str) -> bytes:
    """The Python reference codec the guest must byte-match."""
    doc = json.loads(response_json)
    out = b""
    for block in doc["content"]:
        if block["type"] == "text":
            payload = block["text"].encode()
            tag = b"t"
        elif block["type"] == "tool_use":
            # json v2 returns the byte-exact raw slice. Re-dumping the block
            # input with the same separators/order as the enclosing compact
            # document reproduces those bytes exactly; spaced docs only assert
            # text frames, so this branch stays compact-only.
            payload = (block["id"].encode() + b"\x1f" + block["name"].encode()
                       + b"\x1f" + json.dumps(
                block["input"], separators=(",", ":"), ensure_ascii=False).encode())
            tag = b"u"
        else:
            payload = block["type"].encode()
            tag = b"?"
        out += tag + str(len(payload)).zfill(8).encode() + payload
    return out


@settings(max_examples=60, deadline=None)
@given(BLOCKS)
@example([("text", "")])
@example([("text", 'he said "hi"\n\ttab'), ("tool_use", ("read_file", {"path": "/tmp/x"}))])
@example([("tool_use", ("search", {"q": "emoji 😀", "n": 3})), ("text", "done")])
def test_frames_match_reference(mcp, blocks):
    resp = make_response(blocks)
    out = forge_ok(mcp, parse_reply_source(), resp).encode()
    assert out == reference_frames(resp)


@settings(max_examples=25, deadline=None)
@given(st.lists(st.tuples(st.just("text"), TEXT), min_size=1, max_size=3))
@example([("text", "spaced 😀 reply")])
def test_non_compact_json_finally_works(mcp, blocks):
    """The M2 scanner required compact JSON; the ring bridge must not."""
    resp = make_response(blocks, spaced=True)
    out = forge_ok(mcp, parse_reply_source(), resp).encode()
    assert out == reference_frames(make_response(blocks))  # frames are layout-independent


def test_unicode_escapes_are_decoded(mcp):
    """\\uXXXX (incl. surrogate pairs) decode to UTF-8 — impossible for the
    M2 scanner, table stakes for the bridge."""
    resp = ('{"content":[{"type":"text","text":"pile of \\ud83d\\udca9 and'
            ' caf\\u00e9"}]}')
    out = forge_ok(mcp, parse_reply_source(), resp).encode()
    assert out == b"t" + str(len("pile of 💩 and café".encode())).zfill(8).encode() \
        + "pile of 💩 and café".encode()


def test_unknown_block_type_is_tagged(mcp):
    """Future block kinds must degrade to '?' frames, not break the walk."""
    resp = ('{"content":[{"type":"server_tool_use","id":"x"},'
            '{"type":"text","text":"after"}]}')
    out = forge_ok(mcp, parse_reply_source(), resp).encode()
    assert out == (b"?" + b"00000015" + b"server_tool_use"
                   + b"t" + b"00000005" + b"after")


def test_missing_content_is_404(mcp):
    message = forge_err(mcp, parse_reply_source(), '{"id":"x","role":"assistant"}')
    assert "404" in message


def test_malformed_json_is_400(mcp):
    message = forge_err(mcp, parse_reply_source(), '{"content":[{"type":"text",')
    assert "400" in message
