"""SIGIL retained-history projection; authentication is tested at the real API."""
import json

import pytest

from conftest import SIGIL_ROOT, needs_toolchain
from scripts.compose_application import compose_application
from turn_support import FUEL, configuration, fields, record, refused


def compose_history():
    return compose_application("history", SIGIL_ROOT)


@pytest.fixture(scope="module")
def history_program():
    needs_toolchain()
    return compose_history().text


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def state(messages=None, **overrides):
    values = ["a" * 64, "model", "1", "1",
              configuration(system="SYSTEM-private-canary-761"),
              json.dumps([user("Read README.md")] if messages is None else messages, ensure_ascii=False),
              "[]", "0", "[]", "", "0", "0", "1", "9000000000", "", "conversation", "key-private-canary-48"]
    for index, value in overrides.items():
        values[int(index)] = value
    return record("PT1\n", values)


def request(messages=None, *, session="conversation", revision="1", expected="0", offset="0", limit="20",
            value=None, status="ok"):
    value = state(messages) if value is None else value
    return record("HP1\n", [session, record("SR1\n", [status, revision, value]), expected, offset, limit])


def evaluate(mcp, program, payload):
    result = mcp.forge(program, input=payload, fuel=FUEL)
    assert result["status"] == "ok", result.get("diagnostics")
    status, raw = fields(result["data"]["output_text"], "HP2\n", 2)
    return status, json.loads(raw)


def test_build_is_grantless_and_fits_the_unchanged_source_limit():
    needs_toolchain()
    built = compose_history()
    assert len(built.input_hashes) == 7
    assert len(built.text.encode()) <= 65536
    assert built.text.count("pub fn tool_main(") == 1
    assert "fn state_record(" in built.text
    assert "fn store_revision(" in built.text
    for marker in ["// STATE_CODEC", "// TURN_HELPERS", "// STORE_READ_HELPERS", "// OPERATION_IDENTITY", "// UTF8_VALIDATOR"]:
        assert marker not in built.text


def test_first_page_projects_only_retained_message_content(mcp, history_program):
    status, out = evaluate(mcp, history_program, request([user("User-owned secret stays text 😀"), assistant("A grounded answer.")]))
    assert status == "ok"
    assert out == {"session": "conversation", "operation": "a" * 64, "state_revision": "1",
                   "state_phase": "model", "history_kind": "retained_context", "offset": 0,
                   "messages": [{"index": 0, "role": "user", "content": [{"type": "text", "text": "User-owned secret stays text 😀"}]},
                                {"index": 1, "role": "assistant", "content": [{"type": "text", "text": "A grounded answer."}]}],
                   "next_offset": None}
    assert "SYSTEM-private-canary-761" not in json.dumps(out)
    assert "key-private-canary-48" not in json.dumps(out)


def test_tool_requests_are_not_labeled_as_approved_or_dispatched(mcp, history_program):
    messages = [user("Read the file"),
                {"role": "assistant", "content": [{"type": "text", "text": "I'll inspect it."},
                    {"type": "tool_use", "id": "call-1", "name": "read_file", "input": {"path": "README.md"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "File value 😀"},
                    {"type": "tool_result", "tool_use_id": "call-2", "content": "tool not authorized: write_file", "is_error": True}]}]
    status, out = evaluate(mcp, history_program, request(messages))
    assert status == "ok"
    assert out["messages"][1]["content"][1] == {"type": "tool_call", "call_id": "call-1", "name": "read_file",
        "arguments": {"path": "README.md"}, "origin": "model_request"}
    assert out["messages"][2] == {"index": 2, "role": "tool", "content": [
        {"type": "tool_result", "call_id": "call-1", "text": "File value 😀", "is_error": False},
        {"type": "tool_result", "call_id": "call-2", "text": "tool not authorized: write_file", "is_error": True}]}


def test_revision_bound_pagination_retains_order_and_exact_content(mcp, history_program):
    messages = [user("one"), assistant("two"), user("three"), assistant("four")]
    seen = []
    for offset in range(4):
        status, out = evaluate(mcp, history_program, request(messages, revision="9007199254740993",
            expected="0" if offset == 0 else "9007199254740993", offset=str(offset), limit="1"))
        assert status == "ok" and out["state_revision"] == "9007199254740993"
        assert len(out["messages"]) == 1 and out["messages"][0]["index"] == offset
        assert out["next_offset"] == (offset + 1 if offset < 3 else None)
        seen.append(out["messages"][0]["content"][0]["text"])
    assert seen == ["one", "two", "three", "four"]
    assert evaluate(mcp, history_program, request(messages, revision="2", expected="1", offset="1")) == ("stale", {})


@pytest.mark.parametrize("revision", ["0", "1", "9223372036854775807"])
def test_absence_and_tombstone_do_not_expose_deleted_names_or_content(mcp, history_program, revision):
    assert evaluate(mcp, history_program, request(revision=revision, value="")) == ("missing", {})


def test_native_read_error_is_not_reported_as_a_missing_conversation(mcp, history_program):
    assert evaluate(mcp, history_program, request(revision="0", value="", status="error")) == ("unavailable", {})


@pytest.mark.parametrize("overrides", [
    {"session": "../conversation"}, {"limit": "0"}, {"limit": "51"}, {"limit": "01"},
    {"offset": "1"}, {"expected": "01"}, {"expected": "9223372036854775808"},
    {"revision": "9223372036854775808"}, {"status": "invented"},
    {"status": "error", "revision": "1", "value": ""},
])
def test_malformed_paging_or_observations_fail_closed(mcp, history_program, overrides):
    refused(mcp, history_program, request(**overrides), 400)


def test_wrong_session_and_absence_with_bytes_cannot_become_a_page(mcp, history_program):
    refused(mcp, history_program, request(session="another-session"), 409)
    refused(mcp, history_program, request(revision="0"), 409)


@pytest.mark.parametrize("message", [
    {"role": "system", "content": "INTERNAL CANARY"},
    {"role": "user", "content": 12},
    {"role": "assistant", "content": "untyped"},
    {"role": "assistant", "content": [{"type": "text", "text": 12}]},
    {"role": "assistant", "content": [{"type": "thinking", "thinking": "not public"}]},
    {"role": "assistant", "content": [{"type": "tool_use", "id": "call", "name": "read_file", "input": []}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call", "content": "ok", "is_error": "false"}]},
    {"role": "user", "content": [{"type": "text", "text": "wrong carrier"}]},
])
def test_internal_or_malformed_message_shapes_are_not_forwarded(mcp, history_program, message):
    refused(mcp, history_program, request([message]), 400)


def test_duplicate_selected_keys_and_trailing_bytes_are_refused(mcp, history_program):
    invalid = state(**{"5": '[{"role":"user","role":"assistant","content":"canary"}]'})
    refused(mcp, history_program, request(value=invalid), 400)
    refused(mcp, history_program, record("HP1\n", ["conversation", "", "0", "0", "20"]) + "trailing", 400)


def test_end_of_page_is_empty_but_out_of_range_is_not_silently_corrected(mcp, history_program):
    status, out = evaluate(mcp, history_program, request(expected="1", offset="1"))
    assert status == "ok" and out["messages"] == [] and out["next_offset"] is None
    assert evaluate(mcp, history_program, request(expected="1", offset="2")) == ("range", {})


def test_html_and_control_characters_remain_json_text(mcp, history_program):
    text = '<script>alert("canary")</script>\n\x00😀'
    status, out = evaluate(mcp, history_program, request([user(text)]))
    assert status == "ok" and out["messages"][0]["content"][0]["text"] == text
    # This proves transport content preservation, not browser XSS safety.


def test_fifty_large_messages_preserve_content_within_existing_guest_limits(mcp, history_program):
    messages = [user(f"message-{index}:" + "x" * 14000) for index in range(50)]
    status, out = evaluate(mcp, history_program, request(messages, limit="50"))
    assert status == "ok" and len(out["messages"]) == 50 and out["next_offset"] is None
    assert [entry["content"][0]["text"] for entry in out["messages"]] == [entry["content"] for entry in messages]


def test_large_multi_block_message_is_not_quadratically_reassembled(mcp, history_program):
    content = [{"type": "text", "text": f"block-{index}:" + "x" * 20000} for index in range(32)]
    status, out = evaluate(mcp, history_program, request([{"role": "assistant", "content": content}]))
    assert status == "ok" and out["messages"][0]["content"] == content


def test_public_byte_bound_returns_a_cursor_without_skipping_or_clipping(mcp, history_program):
    # A structural boundary fixture, not evidence that these model requests were
    # authorized or executed. Public per-call labels make this projection larger.
    def messages(width):
        return [{"role": "assistant", "content": [
            {"type": "tool_use", "id": f"call-{index}", "name": "reader", "input": {"value": "x" * width}}
            for index in range(32)]} for _ in range(50)]

    empty = json.dumps(messages(0), separators=(",", ":"))
    width = (1048000 - len(empty.encode())) // (50 * 32)
    source_messages = messages(width)
    raw = json.dumps(source_messages, separators=(",", ":"))
    assert 1040000 <= len(raw.encode()) <= 1048576
    value = state(**{"5": raw})
    status, first = evaluate(mcp, history_program, request(value=value, limit="50"))
    assert status == "ok" and 0 < first["next_offset"] < 50
    assert len(json.dumps(first["messages"], separators=(",", ":")).encode()) <= 1100000
    status, second = evaluate(mcp, history_program, request(value=value, expected="1",
                                                          offset=str(first["next_offset"]), limit="50"))
    assert status == "ok" and second["next_offset"] is None
    combined = first["messages"] + second["messages"]
    assert [item["index"] for item in combined] == list(range(50))
    for item in combined:
        assert len(item["content"]) == 32
        assert all(block["arguments"]["value"] == "x" * width for block in item["content"])
