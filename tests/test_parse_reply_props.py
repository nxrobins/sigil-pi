"""Property tests for parse_reply.sigil — the M3 ring bridge.

An INNER-RING pure tool (composes stdlib `json`, forged as its own program —
the two-forge bridge; `grant(&cap,...)` is capability machinery, not a
cross-ring call path). Input: a raw Anthropic /v1/messages response. Output:
one frame per content block, in order:

    tag byte        't' text | 'u' tool_use | 'k' thinking | '?' other
    8 ASCII digits  zero-padded decimal payload length
    payload         t: DECODED text bytes (json v2 unescaping, incl \\uXXXX)
                    u: id + 0x1F + name + 0x1F + raw balanced input JSON
                       (id first — tool_result messages must echo it)
                    k: the raw balanced JSON of a whole `thinking` or
                       `redacted_thinking` block, so the host can pass it
                       back unchanged (audit F-10)
                    ?: the raw type string bytes

then, after the content frames, a 'g' usage frame (input|output token digits)
when usage is present, and LAST an 's' frame carrying the DECODED stop_reason
when that field is present. Each trailing frame is absent when its field is,
so a payload without the field keeps its exact old frames.

Errors: json codes propagate (-404 no content field, -400 malformed — a
stop_reason that is present but unreadable is malformed, never "absent").
Unlike M2's find_text scanner this handles whitespace, multiple blocks,
and tool_use — property-tested against a Python reference codec.
"""
import json

from hypothesis import example, given, settings, strategies as st

from conftest import PI_ROOT, SIGIL_ROOT, forge_err, forge_ok

TEXT = st.text(alphabet=st.characters(exclude_categories=("Cs",)), max_size=120)
NAME = st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=20)
# tool_use inputs: modest JSON objects (nested one level, unicode values)
JSON_VAL = st.one_of(st.integers(-1000, 1000), TEXT, st.booleans(), st.none())
TOOL_INPUT = st.dictionaries(NAME, st.one_of(JSON_VAL, st.dictionaries(NAME, JSON_VAL, max_size=2)), max_size=4)
# thinking blocks carry an opaque base64 signature (redacted ones a `data`
# field); with display "omitted", the provider's default on its current
# models, the `thinking` text is empty and only the signature holds anything
OPAQUE = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=",
                 max_size=200)

BLOCK = st.one_of(
    st.tuples(st.just("text"), TEXT),
    st.tuples(st.just("tool_use"), st.tuples(NAME, TOOL_INPUT)),
    st.tuples(st.just("thinking"), st.tuples(st.one_of(st.just(""), TEXT), OPAQUE)),
    st.tuples(st.just("redacted_thinking"), OPAQUE),
)
BLOCKS = st.lists(BLOCK, min_size=1, max_size=4)

# Every value the Messages API documents for stop_reason, JSON null, any other
# string, and the field left out altogether (ABSENT).
ABSENT = object()
DOCUMENTED_STOP_REASONS = ("end_turn", "max_tokens", "stop_sequence", "tool_use",
                           "pause_turn", "refusal", "model_context_window_exceeded")
STOP = st.one_of(st.just(ABSENT), st.none(), st.sampled_from(DOCUMENTED_STOP_REASONS), TEXT)


def parse_reply_source() -> str:
    """The uncomposed source, composed with stdlib json at forge time."""
    from sigil_compose import compose_with_stdlib
    src_path = PI_ROOT / "tools" / "parse_reply.sigil"
    assert src_path.exists(), "tools/parse_reply.sigil missing (v14 authors it — M3)"
    return compose_with_stdlib(src_path.read_text(), ["json"], SIGIL_ROOT).text


def make_response(blocks, spaced=False, stop_reason="end_turn") -> str:
    content = []
    for kind, payload in blocks:
        if kind == "text":
            content.append({"type": "text", "text": payload})
        elif kind == "thinking":
            thought, signature = payload
            content.append({"type": "thinking", "thinking": thought,
                            "signature": signature})
        elif kind == "redacted_thinking":
            content.append({"type": "redacted_thinking", "data": payload})
        else:
            name, tool_input = payload
            content.append({"type": "tool_use", "id": f"tu_{len(content)}",
                            "name": name, "input": tool_input})
    doc = {"id": "msg_1", "type": "message", "role": "assistant",
           "model": "claude-mock", "content": content,
           "stop_reason": stop_reason, "usage": {"input_tokens": 1, "output_tokens": 1}}
    if stop_reason is ABSENT:
        del doc["stop_reason"]
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
        elif block["type"] in ("thinking", "redacted_thinking"):
            # the WHOLE block, as the raw slice the provider sent: the host
            # passes it back unchanged (compact-only, like tool_use above)
            payload = json.dumps(block, separators=(",", ":"), ensure_ascii=False).encode()
            tag = b"k"
        else:
            payload = block["type"].encode()
            tag = b"?"
        out += tag + str(len(payload)).zfill(8).encode() + payload
    # usage rides the SAME bridge as content — one parser, one frame stream
    # (tag 'g'): the raw input_tokens/output_tokens digit slices joined by
    # '|'. Absent or partial usage emits nothing, so pre-usage payloads stay
    # byte-identical.
    usage = doc.get("usage")
    if isinstance(usage, dict) and "input_tokens" in usage and "output_tokens" in usage:
        payload = f"{usage['input_tokens']}|{usage['output_tokens']}".encode()
        out += b"g" + str(len(payload)).zfill(8).encode() + payload
    # why the reply ended (tag 's'), LAST: a string decoded like text, any
    # other JSON value (null) as its literal. Absent emits nothing, so
    # payloads without the field stay byte-identical.
    if "stop_reason" in doc:
        reason = doc["stop_reason"]
        payload = (reason if isinstance(reason, str) else json.dumps(reason)).encode()
        out += b"s" + str(len(payload)).zfill(8).encode() + payload
    return out


@settings(max_examples=60, deadline=None)
@given(BLOCKS, STOP)
@example([("text", "")], "end_turn")
@example([("text", 'he said "hi"\n\ttab'), ("tool_use", ("read_file", {"path": "/tmp/x"}))],
         "tool_use")
@example([("tool_use", ("search", {"q": "emoji 😀", "n": 3})), ("text", "done")], ABSENT)
@example([("thinking", ("", "c2lnbmF0dXJl")), ("redacted_thinking", "ZGF0YQ=="),
          ("tool_use", ("read_file", {"path": "a"}))], "max_tokens")
@example([("text", "partial")], None)
def test_frames_match_reference(mcp, blocks, stop_reason):
    resp = make_response(blocks, stop_reason=stop_reason)
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


def test_usage_frame_is_emitted_after_content(mcp):
    resp = ('{"content":[{"type":"text","text":"hi"}],'
            '"usage":{"input_tokens":1234,"output_tokens":56}}')
    out = forge_ok(mcp, parse_reply_source(), resp).encode()
    assert out == (b"t" + b"00000002" + b"hi"
                   + b"g" + b"00000007" + b"1234|56")


def test_absent_usage_emits_no_frame(mcp):
    """Pre-usage responses (and mocks) must keep their exact old frames."""
    resp = '{"content":[{"type":"text","text":"hi"}]}'
    out = forge_ok(mcp, parse_reply_source(), resp).encode()
    assert out == b"t" + b"00000002" + b"hi"


def test_usage_with_empty_content_still_emits(mcp):
    """content may be empty while usage is present — the frame buffer must
    have room for a usage frame even with zero content frames."""
    resp = '{"content":[],"usage":{"input_tokens":9,"output_tokens":9}}'
    out = forge_ok(mcp, parse_reply_source(), resp).encode()
    assert out == b"g" + b"00000003" + b"9|9"


def test_missing_content_is_404(mcp):
    message = forge_err(mcp, parse_reply_source(), '{"id":"x","role":"assistant"}')
    assert "404" in message


def test_malformed_json_is_400(mcp):
    message = forge_err(mcp, parse_reply_source(), '{"content":[{"type":"text",')
    assert "400" in message


# ── why the reply ended, and the reasoning behind it (audit F-10) ───────


def test_stop_reason_is_the_last_frame(mcp):
    """The host must know why a reply ended before it acts on any of it, so
    stop_reason crosses the ring like usage does: its own frame, after usage."""
    resp = ('{"content":[{"type":"text","text":"hi"}],'
            '"stop_reason":"max_tokens","usage":{"input_tokens":12,"output_tokens":7}}')
    out = forge_ok(mcp, parse_reply_source(), resp).encode()
    assert out == (b"t" + b"00000002" + b"hi"
                   + b"g" + b"00000004" + b"12|7"
                   + b"s" + b"00000010" + b"max_tokens"), (
        f"parse_reply dropped the reply's stop_reason: {out!r}")


def test_a_refusal_with_empty_content_still_says_why(mcp):
    """A refusal is HTTP 200 with empty content (the provider's documented
    shape). Without its stop frame it is indistinguishable from an empty
    answer, which the loop returned as a successful empty reply."""
    resp = ('{"content":[],"stop_reason":"refusal","stop_details":{"type":"refusal",'
            '"category":"cyber","explanation":"declined"},'
            '"usage":{"input_tokens":412,"output_tokens":0}}')
    out = forge_ok(mcp, parse_reply_source(), resp).encode()
    assert out == (b"g" + b"00000005" + b"412|0"
                   + b"s" + b"00000007" + b"refusal"), (
        f"a refusal crossed the bridge as {out!r}")


def test_an_escaped_stop_reason_is_decoded(mcp):
    resp = '{"content":[],"stop_reason":"end\\u005fturn"}'
    out = forge_ok(mcp, parse_reply_source(), resp).encode()
    assert out == b"s" + b"00000008" + b"end_turn"


def test_an_unreadable_stop_reason_fails_the_parse(mcp):
    """Present but undecodable is MALFORMED, never 'absent': a frame dropped
    here would make a reply of unknown completeness look like one that never
    said, and the host would act on it."""
    message = forge_err(mcp, parse_reply_source(),
                        '{"content":[],"stop_reason":"\\ud800"}')
    assert "400" in message


def test_a_reply_malformed_after_its_content_is_refused(mcp):
    """Looking for stop_reason reads past `content`, so a document that breaks
    off after it is now malformed (-400) where it used to cross the bridge as
    its content frames alone: a reply nobody can call finished is not handed
    to the host as if it were."""
    message = forge_err(mcp, parse_reply_source(),
                        '{"content":[{"type":"text","text":"hi"}],'
                        '"usage":{"input_tokens":1')
    assert "400" in message


def test_a_long_stop_reason_fits_the_frame_buffer(mcp):
    """The frame buffer was sized for content plus a 64-byte usage reserve; a
    trailing frame longer than that reserve must still land inside it. The
    length matters: a few kilobytes past the buffer land in memory the guest
    already holds and nothing shows, while 200,000 bytes run off the end of
    it and trap (R803) unless the buffer was sized for the frame."""
    reason = "x" * 200_000
    resp = ('{"content":[],"usage":{"input_tokens":1,"output_tokens":1},'
            f'"stop_reason":"{reason}"}}')
    out = forge_ok(mcp, parse_reply_source(), resp).encode()
    assert out == (b"g" + b"00000003" + b"1|1"
                   + b"s" + b"00200000" + reason.encode())


def test_thinking_blocks_cross_whole_and_in_order(mcp):
    """A thinking block must go back to the provider complete and unmodified,
    with the tool call it accompanied. The bridge used to reduce it to its
    type name ('?thinking'), which nothing can pass back."""
    thinking = '{"type":"thinking","thinking":"","signature":"EqQBCkgIARABGAIi"}'
    redacted = '{"type": "redacted_thinking", "data": "ZW5jcnlwdGVk"}'
    resp = ('{"content":[' + thinking + ',' + redacted + ','
            '{"type":"tool_use","id":"tu_1","name":"read_file","input":{"path":"a"}}],'
            '"stop_reason":"tool_use"}')
    out = forge_ok(mcp, parse_reply_source(), resp).encode()
    tool = b'tu_1\x1fread_file\x1f{"path":"a"}'
    assert out == (b"k" + str(len(thinking)).zfill(8).encode() + thinking.encode()
                   + b"k" + str(len(redacted)).zfill(8).encode() + redacted.encode()
                   + b"u" + str(len(tool)).zfill(8).encode() + tool
                   + b"s" + b"00000008" + b"tool_use"), (
        f"thinking blocks did not cross the bridge whole: {out!r}")
