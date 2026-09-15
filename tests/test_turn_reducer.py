"""SIGIL owns sequencing; Python here supplies observations and checks proposals."""

import json

import pytest

from conftest import PI_ROOT
from turn_support import (READ_SPEC, configuration, event, fields, model_reply,
                          record, refused, run, source, submission, text_reply, tool_reply)


@pytest.fixture(scope="module")
def turn_source():
    return source()


def test_sigil_selects_model_tool_model_response_and_tracks_usage(mcp, turn_source):
    first = run(mcp, turn_source, event())
    assert first.action == "model" and first.sequence == "1"
    request = json.loads(first.input)
    assert request["messages"] == [{"role": "user", "content": "Read README.md"}]
    assert request["tools"] == [READ_SPEC]
    tool = run(mcp, turn_source, first.result(tool_reply(
        "read_file", usage={"input_tokens": 10, "output_tokens": 3})))
    assert (tool.action, tool.sequence, tool.tool) == ("tool", "2", "read_file")
    assert json.loads(tool.input) == {"path": "README.md"}
    second = run(mcp, turn_source, tool.result("workspace fact 😀"))
    assert (second.action, second.sequence) == ("model", "3")
    assert json.loads(second.input)["messages"][-1] == {
        "role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-0",
                                       "content": "workspace fact 😀"}]}
    final = run(mcp, turn_source, second.result(text_reply(
        "A grounded answer.", usage={"input_tokens": 8, "output_tokens": 4})))
    assert final.action == "done" and final.values[9] == "A grounded answer."
    assert final.values[10:13] == ["18", "7", "1"]


def test_text_only_response_and_completed_conversation_followup(mcp, turn_source):
    first = run(mcp, turn_source, event())
    final = run(mcp, turn_source, first.result(text_reply("First answer")))
    followup = run(mcp, turn_source, event(
        prior=final.state, operation="operation-2", payload=submission("Explain that.", key="k2")))
    assert followup.action == "model" and followup.sequence == "1"
    history = json.loads(followup.input)["messages"]
    assert len(history) == 3 and history[1]["content"][0]["text"] == "First answer"
    assert history[2] == {"role": "user", "content": "Explain that."}


def test_multiple_tools_are_serial_and_denied_tools_never_dispatch(mcp, turn_source):
    first = run(mcp, turn_source, event())
    a = run(mcp, turn_source, first.result(tool_reply("read_file", "write_file", "read_file")))
    assert a.tool == "read_file" and a.sequence == "2"
    b = run(mcp, turn_source, a.result("first"))
    assert b.tool == "read_file" and b.sequence == "3"
    assert b.values[7] == "2"
    c = run(mcp, turn_source, b.result("second", kind="tool_error"))
    assert c.action == "model" and c.sequence == "4"
    results = json.loads(c.input)["messages"][-1]["content"]
    assert [entry["tool_use_id"] for entry in results] == ["tool-0", "tool-1", "tool-2"]
    assert results[1]["content"] == "tool not authorized: write_file"
    assert results[1]["is_error"] is results[2]["is_error"] is True


def test_empty_catalog_denies_all_model_tools_without_a_tool_action(mcp, turn_source):
    first = run(mcp, turn_source, event(config=configuration(tools=[])))
    following = run(mcp, turn_source, first.result(tool_reply("read_file")))
    assert following.action == "model" and following.sequence == "2"
    assert json.loads(following.input)["messages"][-1]["content"][0]["is_error"] is True


@pytest.mark.parametrize("boundary", ["model", "tool"])
@pytest.mark.parametrize("kind,phase,error", [
    ("unknown", "uncertain", "possibly_delivered"),
    ("cancelled_unsent", "cancelled", "cancelled_unsent"),
    ("expired_unsent", "failed", "deadline_exceeded"),
    ("error", "failed", "effect_failed"),
])
def test_delivery_facts_stop_without_hidden_retry(mcp, turn_source, boundary, kind, phase, error):
    current = run(mcp, turn_source, event())
    if boundary == "tool":
        current = run(mcp, turn_source, current.result(tool_reply("read_file")))
    stopped = run(mcp, turn_source, current.result(kind=kind))
    assert stopped.action == phase and stopped.values[14] == error
    assert stopped.sequence == current.sequence
    refused(mcp, turn_source, stopped.result(text_reply()), 409)
    if kind == "unknown":
        assert stopped.values[12] == "0"


@pytest.mark.parametrize("overrides", [{"operation": "wrong-operation"}, {"sequence": 0},
                                        {"sequence": 2}, {"sequence": "01"},
                                        {"config": "replaced"}, {"deadline": 999999}])
def test_result_correlation_and_immutable_context(mcp, turn_source, overrides):
    current = run(mcp, turn_source, event())
    refused(mcp, turn_source, current.result(text_reply(), **overrides),
            409 if "operation" in overrides or overrides.get("sequence") in (0, 2) else 400)


def test_replayed_result_cannot_advance_the_newer_state(mcp, turn_source):
    first = run(mcp, turn_source, event())
    tool = run(mcp, turn_source, first.result(tool_reply("read_file")))
    stale = first.result(tool_reply("read_file"), prior=tool.state)
    refused(mcp, turn_source, stale, 409)


def test_proposal_is_deterministic_for_the_same_state_and_observation(mcp, turn_source):
    first = run(mcp, turn_source, event())
    observation = first.result(tool_reply("read_file"))
    assert run(mcp, turn_source, observation) == run(mcp, turn_source, observation)


def test_model_step_cap_preserves_tool_result_without_another_call(mcp, turn_source):
    first = run(mcp, turn_source, event(config=configuration(max_steps=1)))
    tool = run(mcp, turn_source, first.result(tool_reply("read_file")))
    final = run(mcp, turn_source, tool.result("retained result"))
    assert final.action == "failed" and final.values[14] == "step_limit"
    assert json.loads(final.values[5])[-1]["content"][0]["content"] == "retained result"


@pytest.mark.parametrize("boundary", ["start", "model", "tool"])
def test_deadline_prevents_any_next_dispatch(mcp, turn_source, boundary):
    first = run(mcp, turn_source, event(now=10000 if boundary == "start" else 100))
    if boundary == "start":
        final = first
    elif boundary == "model":
        final = run(mcp, turn_source, first.result(tool_reply("read_file"), now=10000))
    else:
        tool = run(mcp, turn_source, first.result(tool_reply("read_file")))
        final = run(mcp, turn_source, tool.result("observed result", now=10000))
    assert final.action == "failed" and final.values[14] == "deadline_exceeded"


@pytest.mark.parametrize("content,error", [
    ([], "empty_model_response"),
    ([{"type": "other"}], "empty_model_response"),
    ([{"type": "text", "text": 123}], "invalid_model_response"),
    ([{"type": "tool_use", "id": True, "name": "read_file", "input": {}}], "invalid_model_response"),
    ([{"type": "tool_use", "id": "x", "name": "read_file", "input": "{}"}], "invalid_model_response"),
    ([{"type": "tool_use", "id": "x\u001fy", "name": "read_file", "input": {}}], "invalid_model_response"),
    ([{"type": "tool_use", "id": "x", "name": "read_file", "input": {}}] * 2, "duplicate_tool_id"),
    ([{"type": "text", "text": "x"}] * 33, "invalid_model_response"),
])
def test_malformed_model_data_cannot_create_a_tool_action(mcp, turn_source, content, error):
    current = run(mcp, turn_source, event())
    stopped = run(mcp, turn_source, current.result(model_reply(content)))
    assert stopped.action == "failed" and stopped.values[14] == error
    assert stopped.values[12] == "0"


@pytest.mark.parametrize("payload", [
    '{"content":[],"content":[{"type":"text","text":"duplicate"}]}',
    '{"content":[{"type":"text","text":"a","te\\u0078t":"b"}]}',
    '{"content":"[]"}', '{"content":[]} trailing',
])
def test_duplicate_decoded_model_fields_and_bad_json_are_rejected(mcp, turn_source, payload):
    current = run(mcp, turn_source, event())
    stopped = run(mcp, turn_source, current.result(payload))
    assert stopped.action == "failed" and stopped.values[14] == "invalid_model_response"


@pytest.mark.parametrize("usage", [{"input_tokens": "1", "output_tokens": 2},
    {"input_tokens": -1, "output_tokens": 2}, {"input_tokens": 1.5, "output_tokens": 2},
    {"input_tokens": True, "output_tokens": 2},
    {"input_tokens": 1000000001, "output_tokens": 2}])
def test_invalid_usage_is_not_accounted_as_zero(mcp, turn_source, usage):
    current = run(mcp, turn_source, event())
    stopped = run(mcp, turn_source, current.result(text_reply(usage=usage)))
    assert stopped.action == "failed" and stopped.values[14] == "invalid_usage"
    assert stopped.values[12] == "0"


def test_missing_usage_is_explicitly_unknown(mcp, turn_source):
    current = run(mcp, turn_source, event())
    final = run(mcp, turn_source, current.result(text_reply()))
    assert final.action == "done" and final.values[12] == "0"


@pytest.mark.parametrize("settings", [{"max_steps": 0}, {"max_steps": 65},
    {"max_tokens": 0}, {"max_tokens": 8193}, {"history_cap": 100},
    {"result_cap": 0}, {"model": ""}, {"tools": [READ_SPEC, READ_SPEC]},
    {"tools": [{"name": True, "input_schema": {}}]}, {"tools": [{"name": "read_file"}]}])
def test_invalid_configuration_fails_before_first_model(mcp, turn_source, settings):
    refused(mcp, turn_source, event(config=configuration(**settings)), 400)


@pytest.mark.parametrize("index,value", [(0, ""), (1, "forged-phase"), (2, "-1"), (2, "0"),
    (3, "65"), (4, "bad"), (5, "{}"), (6, "{}"), (7, "9"), (8, "{}"),
    (10, "-1"), (11, "64000000001"), (12, "2"), (13, "NaN"),
    (15, ""), (16, ""), (15, ".alias"), (16, "key:alias")])
def test_corrupt_state_is_not_executed(mcp, turn_source, index, value):
    current = run(mcp, turn_source, event())
    values = current.values
    values[index] = value
    refused(mcp, turn_source, current.result(text_reply(), prior=record("PT1\n", values)), 400)


def test_tool_result_cap_stops_without_silently_clipping(mcp, turn_source):
    current = run(mcp, turn_source, event(config=configuration(result_cap=4)))
    tool = run(mcp, turn_source, current.result(tool_reply("read_file")))
    stopped = run(mcp, turn_source, tool.result("too long"))
    assert stopped.action == "failed" and stopped.values[14] == "tool_result_limit"


def test_history_cap_prevents_first_model_call(mcp, turn_source):
    stopped = run(mcp, turn_source, event(
        config=configuration(history_cap=1024), payload=submission("x" * 1024)))
    assert stopped.action == "failed" and stopped.values[14] == "history_limit"


@pytest.mark.parametrize("field", ["session", "key"])
@pytest.mark.parametrize("value", [".hidden", "_prefix", "-prefix", "a:b", "a/b", "é", "a" * 129])
def test_canonical_submission_still_requires_the_ps1_identifier_grammar(mcp, turn_source, field, value):
    refused(mcp, turn_source, event(payload=submission(**{field: value})), 400)


def test_zero_deadline_is_expired_not_missing(mcp, turn_source):
    stopped = run(mcp, turn_source, event(deadline=0, now=0))
    assert stopped.action == "failed" and stopped.values[14] == "deadline_exceeded"


def test_wire_decoder_requires_exact_marker_lengths_and_end(mcp, turn_source):
    payload = event()
    for bad in ("", payload + "x", payload[:-1], "XX1\n" + payload[4:],
                payload[:4] + "-0000001" + payload[12:]):
        refused(mcp, turn_source, bad, 400)


def test_turn_code_contains_no_effect_authority_or_python_policy():
    code = (PI_ROOT / "app/pi/turn.sigil").read_text()
    helpers = (PI_ROOT / "app/pi/turn_helpers.sigil").read_text()
    for text in (code, helpers):
        stripped = "\n".join(line.split("//", 1)[0] for line in text.splitlines())
        assert 'extern "C"' not in stripped and "#[trusted]" not in stripped
        assert "AUTHORSHIP: hand-authored" in text
    assert "// TURN_HELPERS" in code and "// RECORD_HELPERS" in helpers
    shared = (PI_ROOT / "app/shared/record_helpers.sigil").read_text()
    assert "// UTF8_VALIDATOR" in shared and 'extern "C"' not in shared
    assert fields(record("PT1\n", ["😀"] * 17), "PT1\n", 17) == ["😀"] * 17
