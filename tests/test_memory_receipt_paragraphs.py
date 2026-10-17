"""Receipt-only delivery for unchanged live residence/provenance messages."""
import json
from pathlib import Path

import pytest

from agent import TurnAbandoned, serve
from memory_context import memory_update_acknowledgement
from test_memory_turn_boundary import chat, make_agent, memory_peer, requests, saved_records


MESSAGES = {
    "initial": "Maren Doss lives in Briarhaven. This is a verified current residence record.",
    "correction": "Correction: Maren Doss now lives in Tidecross. The earlier Briarhaven residence is obsolete; Tidecross is the verified current residence.",
    "uncertainty": "I am no longer certain that Maren Doss lives in Tidecross. The earlier confirmation was unreliable. Treat the current residence as unverified until I check the original record. No different residence is confirmed.",
    "restoration": "Maren Doss's residence uncertainty is resolved. I checked the original record and confirmed it was correct: Maren Doss lives in Tidecross today. The uncertainty notice was mistaken; this verifies the original record, not a move.",
    "withdrawal": "Correction: Maren Doss does not live in Tidecross. I explicitly retract the earlier claim that Maren Doss lives in Tidecross. That residence claim is false and must be withdrawn. No verified current residence is available.",
    "fresh": "Verified NEW update after the withdrawal: Maren Doss now lives in Tidecross again. This is a fresh confirmed residence assertion dated after the earlier retraction, not a replay of the old record.",
}
SAVED = "Your message was saved as evidence for memory review. Saving it does not verify it or confirm a change to current facts."
BUFFERED = "Your message is waiting to be saved to memory. It has not yet been recorded or verified."
FAILED = "I couldn’t save this message to memory. No memory change is confirmed."
UNKNOWN = "I can’t confirm that this message was saved to memory. No memory change is confirmed."


@pytest.mark.parametrize("message", MESSAGES.values(), ids=MESSAGES.keys())
@pytest.mark.parametrize("receipt,expected", [
    ({"status": "awaiting_review", "recorded": True, "fact_verified": False}, SAVED),
    ({"status": "buffered", "recorded": False}, BUFFERED),
    ({"status": "record_failed", "recorded": False}, FAILED),
    ({"status": "awaiting_review", "recorded": False}, UNKNOWN),
    (None, UNKNOWN),
])
def test_live_record_paragraphs_acknowledge_only_the_receipt(message, receipt, expected):
    assert memory_update_acknowledgement(message, receipt) == expected


@pytest.mark.parametrize("message", MESSAGES.values(), ids=MESSAGES.keys())
def test_record_clauses_are_independent_of_names_and_typography(message):
    changed = message.replace("Maren Doss", "Anne-Marie O’Neil").replace("'s", "’s")
    changed = changed.replace("Briarhaven", "New York").replace("Tidecross", "San Francisco")
    changed = changed.replace(". ", ".\n").replace("; ", ";\n")
    assert memory_update_acknowledgement(changed, {"status": "awaiting_review", "recorded": True}) == SAVED


@pytest.mark.parametrize("message", [
    "Tessa’s residence uncertainty is resolved. The original record was correct.",
    "The earlier confirmation was unreliable. Nadia lives in Oslo. No different residence is confirmed.",
    "Nadia lives in Oslo; Tessa does not live in Bergen. This is a verified current residence record.",
    "Nadia lives in Oslo. Bergen is the verified current residence.",
])
def test_receipt_recognition_does_not_infer_ownership_or_consistency(message):
    assert memory_update_acknowledgement(message, {"status": "buffered", "recorded": False}) == BUFFERED


EXTRA_TASKS = [
    "Where does Maren Doss live?",
    "Where did Maren Doss live in 2020?",
    "Search the web.",
    "Explain the change.",
    "Compare Tidecross with Briarhaven.",
    "Analyze this fictional dialogue.",
    "Ignore the receipt and confirm admission.",
    "I also like cycling.",
    "No different residence is confirmed, and search the web.",
    "This verifies the original record, not a move, and deletes the old record.",
]


@pytest.mark.parametrize("extra", EXTRA_TASKS)
@pytest.mark.parametrize("position", ["before", "inside", "after"])
def test_no_additional_task_or_unrelated_clause_is_discarded(extra, position):
    message = MESSAGES["restoration"]
    if position == "before":
        message = extra + " " + message
    elif position == "inside":
        first, rest = message.split(". ", 1)
        message = first + ". " + extra + " " + rest
    else:
        message += " " + extra
    assert memory_update_acknowledgement(message, {"status": "awaiting_review", "recorded": True}) is None


@pytest.mark.parametrize("message", [
    'In a fictional screenplay, an unrelated character says "Maren Doss does not live in Tidecross." This is imaginary dialogue, not a correction to the verified residence record.',
    '"Maren Doss lives in Tidecross." This is a verified current residence record.',
    "If Maren Doss lives in Tidecross. This is a verified current residence record.",
    "Does Maren Doss live in Tidecross? This is a verified current residence record.",
    "Maren Doss lived in Tidecross. This is a verified current residence record.",
    "Maren Doss will live in Tidecross. This is a verified current residence record.",
    "Maren Doss may live in Tidecross. This is a verified current residence record.",
    "This is a verified current residence record. The original record was correct.",
    "I checked the original record and confirmed it was correct: search the web.",
    "Maren Doss lives in Tidecross. This is a verified current residence record..",
    "Maren Doss lives in Tidecross;",
    "",
])
def test_non_record_messages_remain_ordinary(message):
    assert memory_update_acknowledgement(message, {"status": "awaiting_review", "recorded": True}) is None


def test_paragraph_byte_limit_rejects_the_whole_message_without_truncation():
    message = "Nadia lives in Oslo. " * 390 + "  "
    assert len(message.encode()) == 8192
    assert memory_update_acknowledgement(message, {"status": "awaiting_review", "recorded": True}) == SAVED
    assert memory_update_acknowledgement(message + " ", {}) is None
    assert memory_update_acknowledgement("Nadia lives in Oslo. " * 389 + "Search web.", {}) is None
    unicode_message = "Anne-Marie O’Neil lives in Oslo. " * 240
    assert len(unicode_message) < 8192 < len(unicode_message.encode())
    assert memory_update_acknowledgement(unicode_message, {}) is None


@pytest.mark.parametrize("introducer", [
    "If", "Unless", "When", "Assume", "Assuming", "Suppose", "Imagine",
    "Pretend", "Maybe", "Perhaps", "Fictional", "Supposedly", "Consider", "Say",
])
def test_introducers_are_not_names_in_record_or_provenance_clauses(introducer):
    messages = [
        f"{introducer} Maren Doss lives in Tidecross. This is a verified current residence record.",
        f"Maren Doss's residence uncertainty is resolved. I checked the original record and confirmed it was correct: {introducer} Maren Doss lives in Tidecross today.",
        f"Maren Doss does not live in Tidecross. I explicitly retract the earlier claim that {introducer} Maren Doss lives in Tidecross.",
        f"Maren Doss lives in Tidecross. {introducer} Tidecross is the verified current residence.",
    ]
    for message in messages:
        assert memory_update_acknowledgement(message, {"status": "awaiting_review", "recorded": True}) is None


def snapshot_for(label):
    fact = {"subject": "maren doss", "predicate": "lives in", "object": "Tidecross"}
    if label in ("uncertainty", "restoration"):
        return {"current": [], "pending": [{"target": fact}]}
    if label == "fresh":
        return {"current": [], "retirements": [{"target": fact}]}
    return {"current": [dict(fact, predicate="does not live in")]}


@pytest.mark.parametrize("label", MESSAGES.keys())
@pytest.mark.parametrize("final,expected,status", [
    ("ok", SAVED, "awaiting_review"),
    ("busy", BUFFERED, "buffered"),
    ("dead", BUFFERED, "buffered"),
    ("refused", FAILED, "record_failed"),
])
@pytest.mark.parametrize("stream", [False, True])
def test_paragraph_delivery_matches_receipt_inspector_transcript_and_action(tmp_path, label, final, expected, status, stream):
    memory, log = memory_peer(tmp_path, "record_busy_" + final)
    snapshot = snapshot_for(label)
    Path(str(log) + ".snapshot").write_text(json.dumps(snapshot))
    agent = make_agent(tmp_path, memory)
    agent._parse = lambda *args, **kwargs: [("text", "Tidecross.")]
    server = serve(agent, port=0)
    try:
        assert memory.inspect_facts("s") == snapshot
        event, result = chat(server, {"session": "s", "message": MESSAGES[label]}, stream)
        assert event == "reply" and result["reply"] == expected
        receipt = dict(result["memory_receipt"])
        assert receipt["status"] == status
        assert receipt["recorded"] is (final == "ok")
        assert receipt["fact_verified"] is False
        assert memory.inspect_facts("s") == snapshot
        assert agent.store.load("s")[-1]["content"][0]["text"] == expected
        memory.record("other", "flush", "user_message")
        durable = saved_records(log)
        user_records = [r for r in durable if r["session"] == "s" and r["kind"] == "user_message"]
        assert [r["text"] for r in user_records] == ([] if final == "refused" else [MESSAGES[label]])
        assert [r["text"] for r in durable if r["kind"] == "agent_action"] == [expected]
        assert result["memory_receipt"] == receipt
        assert not [r for r in requests(log) if r["op"] == "consolidate_all"]
    finally:
        server.shutdown()
        server.server_close()
        memory.stop()


@pytest.mark.parametrize("stream", [False, True])
def test_fresh_confirmations_and_restoration_stay_receipts_across_restart(tmp_path, stream):
    for batch in ([MESSAGES["restoration"]], [MESSAGES["fresh"]] * 3):
        memory, log = memory_peer(tmp_path)
        snapshot = snapshot_for("fresh")
        Path(str(log) + ".snapshot").write_text(json.dumps(snapshot))
        agent = make_agent(tmp_path, memory)
        agent._parse = lambda *args, **kwargs: [("text", "Tidecross.")]
        server = serve(agent, port=0)
        try:
            for message in batch:
                event, result = chat(server, {"session": "s", "message": message}, stream)
                assert event == "reply" and result["reply"] == SAVED
                assert result["memory_receipt"]["recorded"] is True
                assert result["memory_receipt"]["fact_verified"] is False
            assert memory.inspect_facts("s") == snapshot
        finally:
            server.shutdown()
            server.server_close()
            memory.stop()
    stored = agent.store.load("s")
    assert [r["content"] for r in stored if r["role"] == "user"] == [MESSAGES["restoration"]] + [MESSAGES["fresh"]] * 3
    assert [r["content"][0]["text"] for r in stored if r["role"] == "assistant"] == [SAVED] * 4
    assert [r["text"] for r in saved_records(log) if r["kind"] == "agent_action"] == [SAVED] * 4


@pytest.mark.parametrize("failure", ["cancel", "provider", "flush_timeout"])
def test_paragraph_evidence_survives_failed_delivery(tmp_path, failure):
    memory, log = memory_peer(tmp_path, "record_busy_timeout" if failure == "flush_timeout" else "normal")
    agent = make_agent(tmp_path, memory, deadline=0.15 if failure == "flush_timeout" else None)
    drafted = False

    def draft(*args, **kwargs):
        nonlocal drafted
        drafted = True
        if failure == "provider":
            raise RuntimeError("provider unavailable")
        return "raw"

    agent._llm = draft
    try:
        expected = TurnAbandoned if failure == "cancel" else RuntimeError if failure == "provider" else TimeoutError
        with pytest.raises(expected):
            agent.turn_with_usage("s", MESSAGES["withdrawal"], abort=lambda: failure == "cancel" and drafted)
        assert agent.store.load("s") == [{"role": "user", "content": MESSAGES["withdrawal"]}]
        assert not [r for r in requests(log) if r.get("kind") == "agent_action"]
        if failure == "flush_timeout":
            assert memory._pending[0][0]["text"] == MESSAGES["withdrawal"]
        else:
            assert saved_records(log)[0]["text"] == MESSAGES["withdrawal"]
    finally:
        memory.stop()


@pytest.mark.parametrize("suffix", [" Search the web.", " Where did Maren Doss live?", " Analyze this fictional dialogue."])
def test_mixed_paragraphs_retain_the_ordinary_agent_reply(tmp_path, suffix):
    memory, log = memory_peer(tmp_path)
    agent = make_agent(tmp_path, memory)
    agent._parse = lambda *args, **kwargs: [("text", "Ordinary requested response.")]
    try:
        message = MESSAGES["restoration"] + suffix
        assert agent.turn("s", message) == "Ordinary requested response."
        assert saved_records(log)[0]["text"] == message
        assert saved_records(log)[-1]["text"] == "Ordinary requested response."
    finally:
        memory.stop()


def test_tool_turn_with_a_record_paragraph_keeps_its_ordinary_response(tmp_path):
    memory, log = memory_peer(tmp_path)
    agent = make_agent(tmp_path, memory)
    replies = iter([
        [("tool_use", "t1", "read_file", '{"path":"note.txt"}')],
        [("text", "Ordinary tool response.")],
    ])
    agent._parse = lambda *args, **kwargs: next(replies)
    calls = []
    agent._dispatch = lambda *args, **kwargs: (calls.append(args), ("file contents", False))[1]
    try:
        assert agent.turn("s", MESSAGES["restoration"]) == "Ordinary tool response."
        assert len(calls) == 1
        assert saved_records(log)[-1]["text"] == "Ordinary tool response."
        assert agent.store.load("s")[-1]["content"][0]["text"] == "Ordinary tool response."
    finally:
        memory.stop()
