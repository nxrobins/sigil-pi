"""Why a model reply ended, the output cap, and thinking (audit 2026-11-10, F-10).

The loop never read `stop_reason`. A reply the provider cut off at `max_tokens`
was returned as complete and its tool calls were dispatched; a refusal, which
the provider sends as HTTP 200 with empty content, became an empty successful
answer. The default output cap was 1,024 tokens on a model that thinks unless
told otherwise, with thinking and text sharing that cap. And the reasoning
behind each tool call was thrown away between tool steps, because the bridge
reduced every thinking block to its type name.

Everything here runs the REAL compiler (agent_turn and parse_reply forged by the
pinned sigil-mcp) against a scripted provider on loopback, so the frames are the
ones the guest really emits. No live provider is called.

Provider facts these tests rest on, as the provider's documentation stated
them on 2026-11-10 (platform.claude.com/docs/en: api/messages/create,
build-with-claude/handling-stop-reasons, refusals-and-fallback, thinking,
thinking-troubleshooting, preserved-thinking, thinking-tool-workflows), in
this file's own words:
- stop_reason is one of end_turn, max_tokens, stop_sequence, tool_use,
  pause_turn, refusal and model_context_window_exceeded, and is never null on
  a non-streaming response.
- A reply cut off at max_tokens can hold an incomplete tool_use block. A
  refusal is HTTP 200, with empty content or after partial output, and that
  output is to be discarded. pause_turn comes only from a server-tool loop.
  A reply that stopped at the context window is to be treated as truncated.
- Thinking counts toward max_tokens. On claude-sonnet-5, the default model, a
  request without `thinking` runs adaptive thinking. `adaptive` is accepted
  by every model from the 4.6 generation on and rejected (400) by Opus 4.5,
  Sonnet 4.5 and Haiku 4.5; `disabled` is rejected by Opus 5.5, Sonnet 5.5
  and the Fable and Mythos models; only Sonnet 5.5 accepts `between_tools`.
  The provider's own non-streaming examples with thinking send max_tokens
  16000.
- Thinking blocks must go back, complete and unmodified, while a tool call of
  their turn is being answered; passing earlier turns' thinking back is
  recommended and leaving it out is allowed. On the models that check it
  (Fable and Mythos 5.x, Opus 5.5, Sonnet 5.5, Haiku 5.5) a thinking block is
  valid only while the system prompt, the tools and every message before it
  are unchanged; removing all thinking blocks is always valid.
"""
import ast
import hashlib
import json
import re
import urllib.error
import urllib.request
import uuid

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

from conftest import API_KEY, PI_ROOT, SIGIL_ROOT, forge_ok, msg, text, tool_use

ABSENT = object()
COMPLETE = ("end_turn", "tool_use", "stop_sequence")
TRUNCATED = ("max_tokens", "model_context_window_exceeded")
THINKING_TYPES = ("thinking", "redacted_thinking")


def reply(content, stop_reason, usage=(3, 2), **extra):
    """A Messages API response with an explicit stop_reason (ABSENT omits it)."""
    doc = {"id": "msg_x", "type": "message", "role": "assistant",
           "model": "claude-mock", "content": content,
           "usage": {"input_tokens": usage[0], "output_tokens": usage[1]}, **extra}
    if stop_reason is not ABSENT:
        doc["stop_reason"] = stop_reason
    return doc


def thinking_block(signature, thought=""):
    return {"type": "thinking", "thinking": thought, "signature": signature}


def redacted_block(data):
    return {"type": "redacted_thinking", "data": data}


def tool_result(tu_id, content):
    return {"type": "tool_result", "tool_use_id": tu_id, "content": content}


def run_turn(agent, session, message):
    try:
        return "reply", agent.turn(session, message)
    except Exception as error:  # noqa: BLE001 — the outcome IS the assertion
        return "error", error


def writes(name, content="half a fi"):
    return tool_use(f"tu_{name}", "write_file", {"path": name, "content": content})


def audit_kinds(agent, session):
    return [record["kind"] for record in agent.audit.read(session)]


def thinking_in(messages):
    return [block for message in messages if isinstance(message["content"], list)
            for block in message["content"] if block.get("type") in THINKING_TYPES]


def new_agent(scripted_llm, tmp_path, mcp, **kwargs):
    from agent import PiAgent, SessionStore
    return PiAgent(scripted_llm.url, API_KEY, store=SessionStore(tmp_path / "sessions"),
                   sandbox_root=tmp_path / "sandboxes", mcp=mcp, model="claude-mock",
                   **kwargs)


# ── a reply that did not finish is never acted on ─────────────────────────


@pytest.mark.parametrize("stop_reason", TRUNCATED)
def test_a_cut_off_reply_runs_no_tool_and_ends_the_turn_explicitly(
        agent, scripted_llm, stop_reason):
    target = agent._sandbox_path / "cut.txt"
    scripted_llm.script = [
        reply([text("Writing it now."), writes("cut.txt")], stop_reason),
        msg([text("done")]),
    ]
    kind, value = run_turn(agent, "s1", "write the file")

    assert not target.exists(), (
        f"a reply cut off by the provider (stop_reason {stop_reason}) ran its "
        f"tool call: write_file created cut.txt from a possibly truncated input")
    assert len(scripted_llm.requests) == 1, (
        "the loop carried on to another model call after a cut-off reply")
    assert kind == "error", (
        f"a reply cut off by the provider (stop_reason {stop_reason}) was "
        f"returned as a complete answer: {value!r}")
    assert type(value).__name__ == "ModelOutputTruncated", repr(value)
    assert isinstance(value, RuntimeError), "the product maps RuntimeError to 502"
    assert value.code == "model_output_truncated"
    assert value.stop_reason == stop_reason
    assert audit_kinds(agent, "s1") == ["llm", "parse"], "a tool forge ran"
    # the partial reply is discarded: nothing of it reaches the transcript,
    # so no tool_use is left without its tool_result
    assert agent.store.load("s1") == [{"role": "user", "content": "write the file"}]
    # the tokens were spent all the same, so they are metered
    assert agent.turn_telemetry()["output_tokens"] == 2
    assert agent.last_usage == {"input_tokens": 3, "output_tokens": 2}


def test_the_truncation_error_names_the_cap_and_its_setting(agent, scripted_llm):
    scripted_llm.script = [reply([text("The answer is")], "max_tokens")]
    kind, value = run_turn(agent, "s1", "explain")
    assert kind == "error", f"a truncated answer was returned whole: {value!r}"
    assert str(agent.max_tokens) in str(value) and "PI_MAX_TOKENS" in str(value), (
        f"the error does not say which cap was hit or how to raise it: {value}")


def test_a_refusal_is_an_explicit_error_not_an_empty_reply(agent, scripted_llm):
    scripted_llm.script = [reply(
        [], "refusal", usage=(412, 0),
        stop_details={"type": "refusal", "category": "cyber",
                      "explanation": "explanation-canary: the provider's own words"})]
    kind, value = run_turn(agent, "s1", "help me")

    assert kind == "error", (
        f"a refusal (HTTP 200, stop_reason refusal, empty content) became a "
        f"successful reply: {value!r}")
    assert type(value).__name__ == "ModelRefused", repr(value)
    assert isinstance(value, RuntimeError)
    assert value.code == "model_refusal" and value.stop_reason == "refusal"
    assert "declined" in str(value)
    # the provider's explanation is not stable text and is not the host's to
    # repeat: the message is the host's own
    assert "canary" not in str(value) and "cyber" not in str(value)
    assert agent.store.load("s1") == [{"role": "user", "content": "help me"}]
    assert agent.last_usage == {"input_tokens": 412, "output_tokens": 0}


def test_a_refusal_mid_reply_discards_what_came_before_it(agent, scripted_llm):
    """A refusal can arrive after partial output; the provider says to treat
    that output as incomplete and discard it — including a tool call in it."""
    target = agent._sandbox_path / "partial.txt"
    scripted_llm.script = [
        reply([text("Sure, here it"), writes("partial.txt")], "refusal"),
        msg([text("done")]),
    ]
    kind, value = run_turn(agent, "s1", "go")
    assert not target.exists(), "a refused reply ran its tool call"
    assert kind == "error" and type(value).__name__ == "ModelRefused", repr(value)
    assert agent.store.load("s1") == [{"role": "user", "content": "go"}]


@pytest.mark.parametrize("stop_reason, code", [
    ("pause_turn", "model_turn_paused"),
    ("compaction_pending", "model_stop_unrecognized"),
    (None, "model_stop_unrecognized"),
    ("", "model_stop_unrecognized"),
    ("evil\u202e\nreason " * 9, "model_stop_unrecognized"),
])
def test_any_other_stop_reason_fails_closed(agent, scripted_llm, stop_reason, code):
    """pause_turn comes only from server tools, which this host declares none
    of, so it cannot resume one; a value the provider adds later is not known
    to be a whole reply. Neither is acted on."""
    target = agent._sandbox_path / "other.txt"
    scripted_llm.script = [reply([writes("other.txt")], stop_reason),
                           msg([text("done")])]
    kind, value = run_turn(agent, "s1", "go")
    assert not target.exists(), f"stop_reason {stop_reason!r} ran its tool call"
    assert kind == "error", f"stop_reason {stop_reason!r} was taken as complete"
    assert type(value).__name__ == "ModelReplyIncomplete", repr(value)
    assert value.code == code
    # the provider's value reaches an error message only as a plain identifier
    assert "\u202e" not in str(value) and "\n" not in str(value)


@pytest.mark.parametrize("stop_reason", COMPLETE + (ABSENT,))
def test_a_whole_reply_still_runs_its_tools(agent, scripted_llm, stop_reason):
    """The complete reasons run as before. A reply with no stop_reason at all
    never comes from the Messages API (it is always non-null when not
    streaming); a stub or non-conforming endpoint that omits it is handled as
    it was before this check existed, so old payloads keep working."""
    target = agent._sandbox_path / "whole.txt"
    scripted_llm.script = [reply([writes("whole.txt", "all of it")], stop_reason),
                           reply([text("done")], stop_reason)]
    assert agent.turn("s1", "write") == "done"
    assert target.read_text() == "all of it"


def test_a_reply_cut_off_after_a_finished_tool_step_keeps_that_step(agent, scripted_llm):
    """Only the reply that did not finish is discarded. The step before it ran
    and is on disk, so its tool call and result stay in the transcript, where
    the next turn can see what was done."""
    first = [thinking_block("c2lnMQ=="), writes("kept.txt", "whole")]
    scripted_llm.script = [reply(first, "tool_use"),
                           reply([text("And so the"), writes("lost.txt")], "max_tokens")]
    kind, value = run_turn(agent, "s1", "two files please")

    assert (agent._sandbox_path / "kept.txt").read_text() == "whole"
    assert not (agent._sandbox_path / "lost.txt").exists(), (
        "the cut-off second step ran its tool call")
    assert kind == "error" and type(value).__name__ == "ModelOutputTruncated", repr(value)
    saved = agent.store.load("s1")
    assert [m["role"] for m in saved] == ["user", "assistant", "user"], saved
    assert saved[1]["content"] == [first[1]]
    assert saved[2]["content"][0]["tool_use_id"] == "tu_kept.txt"
    assert agent.last_usage == {"input_tokens": 6, "output_tokens": 4}


UNICODE = st.characters(exclude_categories=("Cs",))
STOP_REASON = st.one_of(
    st.just(ABSENT), st.none(),
    st.sampled_from(COMPLETE + TRUNCATED + ("pause_turn", "refusal")),
    st.text(alphabet=UNICODE, max_size=30))


@settings(max_examples=25, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(stop_reason=STOP_REASON, with_thinking=st.booleans())
@example(stop_reason="max_tokens", with_thinking=True)
@example(stop_reason="end_turn", with_thinking=True)
@example(stop_reason="refusal", with_thinking=False)
@example(stop_reason=None, with_thinking=False)
@example(stop_reason="END_TURN", with_thinking=False)
@example(stop_reason="tool_use ", with_thinking=True)
def test_a_tool_runs_only_when_its_reply_is_whole(agent, scripted_llm,
                                                  stop_reason, with_thinking):
    """THE CLASS GUARD: whatever the provider says about why a reply ended, a
    tool call in it runs exactly when the reply is whole, the turn ends with a
    typed error otherwise, and the saved transcript is always replayable: no
    tool_use without its tool_result, and no thinking block left behind."""
    session = f"p-{uuid.uuid4().hex}"
    name = f"{uuid.uuid4().hex}.txt"
    first = ([thinking_block("c2ln")] if with_thinking else []) + [writes(name)]
    scripted_llm.script = [reply(first, stop_reason), msg([text("done")])]
    scripted_llm.requests.clear()
    kind, value = run_turn(agent, session, "go")

    whole = stop_reason is ABSENT or stop_reason in COMPLETE
    ran = (agent.sandbox_for(session) / name).exists()
    assert ran == whole, (
        f"stop_reason {'(absent)' if stop_reason is ABSENT else repr(stop_reason)}: "
        f"the tool call {'ran' if ran else 'did not run'}")
    if whole:
        assert (kind, value) == ("reply", "done")
    else:
        from agent import ModelReplyIncomplete
        assert kind == "error" and isinstance(value, ModelReplyIncomplete), repr(value)
        assert value.code.startswith("model_"), value.code
        assert len(scripted_llm.requests) == 1
    saved = agent.store.load(session)
    uses = {b["id"] for m in saved if isinstance(m["content"], list)
            for b in m["content"] if b.get("type") == "tool_use"}
    results = {b["tool_use_id"] for m in saved if isinstance(m["content"], list)
               for b in m["content"] if b.get("type") == "tool_result"}
    assert uses == results, f"orphaned tool calls in the saved transcript: {saved}"
    assert not thinking_in(saved)


def test_the_stop_check_reads_every_frame_and_carries_what_was_spent():
    """The rule itself, without a compiler: any stop frame that is not a
    whole reply fails it, and the error says what the reply cost."""
    from agent import (ModelOutputTruncated, ModelRefused, ModelReplyIncomplete,
                       require_whole_reply)
    whole = [("text", "hi"), ("usage", 5, 6), ("stop", "end_turn")]
    assert require_whole_reply(whole, 100) is None
    assert require_whole_reply([("text", "hi")], 100) is None, "no frame: as before"
    assert require_whole_reply([], 100) is None

    with pytest.raises(ModelOutputTruncated) as cut:
        require_whole_reply([("usage", 5, 6), ("usage", 1, 1), ("stop", "max_tokens")], 100)
    assert cut.value.usage == (6, 7) and "100" in str(cut.value)
    with pytest.raises(ModelRefused) as refused:
        require_whole_reply([("stop", "end_turn"), ("stop", "refusal")], 100)
    assert refused.value.usage == (0, 0)
    with pytest.raises(ModelReplyIncomplete) as unknown:
        require_whole_reply([("stop", "tool_use"), ("stop", "Tool_Use")], 100)
    assert unknown.value.stop_reason == "Tool_Use"
    assert "Tool_Use" not in str(unknown.value), "not a plain identifier: not echoed"
    with pytest.raises(ModelReplyIncomplete) as future:
        require_whole_reply([("stop", "compaction_pending")], 100)
    assert "compaction_pending" in str(future.value)
    # decode_frames only ever yields a string here; a stub's None is still
    # a reason that is not one of the whole ones, never "no reason given"
    with pytest.raises(ModelReplyIncomplete) as stub:
        require_whole_reply([("stop", None)], 100)
    assert stub.value.code == "model_stop_unrecognized"


FRAME = st.one_of(
    st.tuples(st.just("stop"), st.one_of(
        st.sampled_from(COMPLETE + TRUNCATED + ("pause_turn", "refusal")),
        st.text(alphabet=UNICODE, max_size=20))),
    st.tuples(st.just("text"), st.text(max_size=5)),
    st.tuples(st.just("usage"), st.integers(0, 10**6), st.integers(0, 10**6)),
)


@settings(max_examples=300, deadline=None)
@given(st.lists(FRAME, max_size=6), st.integers(1, 200_000))
def test_a_reply_is_whole_exactly_when_every_stop_frame_says_so(blocks, cap):
    """The rule for any frame list: whole when each stop frame carries one of
    the three whole reasons (or there is none), otherwise an error that names
    the first reason that is not, carries the tokens spent, and prints as one
    plain line whatever the provider put in the field."""
    from agent import ModelReplyIncomplete, require_whole_reply
    stops = [block[1] for block in blocks if block[0] == "stop"]
    unfinished = [reason for reason in stops if reason not in COMPLETE]
    if not unfinished:
        assert require_whole_reply(blocks, cap) is None
        return
    with pytest.raises(ModelReplyIncomplete) as raised:
        require_whole_reply(blocks, cap)
    error = raised.value
    assert error.stop_reason == unfinished[0]
    assert error.usage == (sum(b[1] for b in blocks if b[0] == "usage"),
                           sum(b[2] for b in blocks if b[0] == "usage"))
    assert error.code.startswith("model_") and isinstance(error, RuntimeError)
    assert str(error).isprintable(), f"not one plain line: {str(error)!r}"


# ── the product and the research host answer with an error, never an empty 200


def _product(agent):
    from product_service import AuthRegistry, ProductService
    token = "a4-chat-token-with-enough-entropy-for-a-test-fixture"
    auth = AuthRegistry([{"sha256": hashlib.sha256(token.encode()).hexdigest(),
                          "principal": "alice", "tenant": "tenant-a",
                          "scopes": ["chat"], "tools": ["write_file"]}])
    service = ProductService(agent, auth, log_sink=lambda record: None)
    headers = {"Authorization": f"Bearer {token}", "X-Request-ID": "a4-request-0001"}
    return service, headers


@pytest.mark.parametrize("doc", [
    reply([], "refusal", usage=(9, 0)),
    reply([text("cut off mid")], "max_tokens", usage=(9, 4)),
], ids=["refusal", "max_tokens"])
def test_the_product_answers_an_unfinished_reply_with_a_stable_error(agent, scripted_llm, doc):
    service, headers = _product(agent)
    scripted_llm.script = [doc]
    body = json.dumps({"session": "work", "message": "hello"}).encode()
    status, _, payload = service.dispatch("POST", "/v1/chat", headers, body)
    assert status != 200, (
        f"the product answered {status} {payload} to a reply that ended with "
        f"stop_reason {doc['stop_reason']}")
    assert (status, payload["error"]["code"]) == (502, "agent_failure"), payload
    assert payload["error"]["message"] == "agent turn failed", (
        "the client response carries the stable message, never the agent's text")
    assert "reply" not in payload
    # a failed turn, and its tokens are still counted against the tenant
    metrics = service.metrics.snapshot()
    assert metrics["turn_errors_total"] == 1
    assert (metrics["input_tokens_total"], metrics["output_tokens_total"]) == (
        doc["usage"]["input_tokens"], doc["usage"]["output_tokens"])


def test_the_product_still_answers_a_whole_reply(agent, scripted_llm):
    service, headers = _product(agent)
    scripted_llm.script = [reply([text("hi there")], "end_turn")]
    body = json.dumps({"session": "work", "message": "hello"}).encode()
    status, _, payload = service.dispatch("POST", "/v1/chat", headers, body)
    assert (status, payload["reply"]) == (200, "hi there"), payload
    assert service.metrics.snapshot()["turn_errors_total"] == 0


def test_the_research_host_answers_a_refusal_with_an_error(agent, scripted_llm):
    from agent import serve
    scripted_llm.script = [reply([], "refusal", usage=(9, 0))]
    server = serve(agent, port=0)
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/chat",
            data=json.dumps({"session": "s1", "message": "hi"}).encode(), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status, body = response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            status, body = error.code, json.loads(error.read())
    finally:
        server.shutdown()
        server.server_close()
    assert status == 500, f"a refusal was answered {status} {body}"
    assert "declined" in body["error"], body


def test_memory_completions_refuse_an_unfinished_reply(agent, scripted_llm):
    """Memory's completions ride the same bridge: a cut-off or refused one is
    unavailable (the dream cycle abstains), never a partial summary."""
    for doc in (reply([text("a partial gi")], "max_tokens"), reply([], "refusal")):
        scripted_llm.script = [doc]
        assert agent._memory_complete({"user": "summarize", "max_tokens": 64}) == {
            "unavailable": True}, doc["stop_reason"]
    scripted_llm.script = [reply([text("a gist")], "end_turn", usage=(7, 3))]
    assert agent._memory_complete({"user": "summarize", "max_tokens": 64}) == {
        "text": "a gist", "input_tokens": 7, "output_tokens": 3}
    # whole, but nothing except thinking: empty, as a reply with no text is
    scripted_llm.script = [reply([thinking_block("c2ln")], "end_turn")]
    assert agent._memory_complete({"user": "summarize", "max_tokens": 64}) == {"empty": True}
    # an endpoint that reports no usage: the text, and no token counts
    scripted_llm.script = [{"content": [text("a gist")], "stop_reason": "end_turn"}]
    assert agent._memory_complete({"user": "summarize", "max_tokens": 64}) == {"text": "a gist"}


# ── thinking: set on purpose, passed back unchanged within the turn ──────


def test_every_model_request_sets_thinking_on_purpose(agent, scripted_llm):
    """Omitting `thinking` leaves it to the model's default, which changed
    between generations (off on Opus 4.8, adaptive on Sonnet 5). The host
    states what it wants on every request it builds."""
    scripted_llm.script = [reply([writes("t.txt", "x")], "tool_use"),
                           reply([text("done")], "end_turn")]
    agent.turn("s1", "go")
    agent._memory_complete({"user": "summarize", "system": "", "max_tokens": 64})
    sent = [r.get("thinking") for r in scripted_llm.requests]
    assert sent == [{"type": "adaptive"}] * 3, (
        f"a request left thinking to the provider's default: {sent}")


@pytest.mark.parametrize("mode", ["adaptive", "disabled", "between_tools"])
def test_the_thinking_setting_is_sent_as_configured(scripted_llm, tmp_path, mcp, mode):
    """No single value is accepted by every model (`adaptive` is a 400 on the
    4.5 generation, `disabled` on Opus 5.5 and Sonnet 5.5, `between_tools`
    everywhere but Sonnet 5.5), so the operator names the one the configured
    model takes and the host sends exactly that."""
    host = new_agent(scripted_llm, tmp_path, mcp, thinking=mode)
    scripted_llm.script = [reply([text("ok")], "end_turn")]
    assert host.turn("s1", "hi") == "ok"
    assert scripted_llm.requests[0]["thinking"] == {"type": mode}


@pytest.mark.parametrize("mode", [None, "", "enabled", "Adaptive", "off", {"type": "adaptive"}])
def test_a_thinking_setting_the_host_does_not_have_is_refused(tmp_path, mode):
    """`enabled` needs a budget this host does not manage, and leaving the
    field out is the default this setting exists to replace."""
    from agent import PiAgent, SessionStore
    with pytest.raises(ValueError, match="PI_THINKING"):
        PiAgent("http://127.0.0.1:9/v1/messages", "k", store=SessionStore(tmp_path / "s"),
                sandbox_root=tmp_path / "b", mcp=None, thinking=mode)


def test_thinking_goes_back_unchanged_with_the_tool_call_it_led_to(agent, scripted_llm):
    first = [thinking_block("EqQBCkgIARAB", ""),
             redacted_block("ZW5jcnlwdGVk"),
             text("Reading it."),
             tool_use("tu_r", "read_file", {"path": "notes.txt"})]
    second = [thinking_block("ErUBCkYIARAB", "Now answer."), text("It is empty.")]
    (agent._sandbox_path / "notes.txt").write_text("")
    scripted_llm.script = [reply(first, "tool_use"), reply(second, "end_turn")]
    assert agent.turn("s1", "what is in notes.txt?") == "It is empty."

    replayed = scripted_llm.requests[1]["messages"]
    assert replayed[-2] == {"role": "assistant", "content": first}, (
        "the second step did not send the first step's assistant turn back "
        f"unchanged; it sent {replayed[-2]}")
    # Between turns the transcript keeps no thinking: dropping all of it is
    # always valid, and a block whose earlier messages later change (a
    # compaction cut, a new system prompt, a different tool set) would fail
    # the provider's prefix check on the models that run it.
    saved = agent.store.load("s1")
    assert not thinking_in(saved)
    assert saved[1] == {"role": "assistant", "content": first[2:]}
    assert saved[3] == {"role": "assistant", "content": second[1:]}

    scripted_llm.script = [reply([text("ok")], "end_turn")]
    agent.turn("s1", "thanks")
    later = scripted_llm.requests[-1]["messages"]
    assert later[:-1] == saved and later[-1] == {"role": "user", "content": "thanks"}


def test_a_thinking_only_reply_is_an_empty_answer_and_leaves_no_empty_message(
        agent, scripted_llm):
    """A reply that is whole but holds nothing except thinking is the empty
    answer it always was. Its assistant message must not be saved with the
    thinking removed and nothing left: the API rejects empty content on every
    later turn, which would end the session."""
    scripted_llm.script = [reply([thinking_block("c2ln")], "end_turn")]
    assert agent.turn("s1", "hi") == ""
    saved = agent.store.load("s1")
    assert saved == [{"role": "user", "content": "hi"}], saved


def _big_file_turn(scripted_llm, tmp_path, mcp, *, with_an_earlier_turn):
    """One turn whose tool result pushes the transcript past its bound after
    the model has thought: with an earlier turn in the session there is
    something to cut, without one there is not."""
    from agent import history_bytes
    host = new_agent(scripted_llm, tmp_path, mcp, max_history_bytes=3000)
    if with_an_earlier_turn:
        host.store.save("s1", [{"role": "user", "content": "an earlier question " * 60},
                               {"role": "assistant", "content": [text("an earlier answer")]}])
    (host.sandbox_for("s1") / "big.txt").write_text("row of data\n" * 300)
    first = [thinking_block("c2lnbmVk"), tool_use("tu_big", "read_file", {"path": "big.txt"})]
    scripted_llm.script = [reply(first, "tool_use"), reply([text("read it")], "end_turn")]
    assert host.turn("s1", "read big.txt") == "read it"
    step_one, step_two = (r["messages"] for r in scripted_llm.requests)
    assert history_bytes(step_two) > 3000, "anti-vacuity: the transcript is over its bound"
    return first, step_one, step_two


def test_a_cut_made_mid_turn_takes_the_thinking_it_would_invalidate(
        scripted_llm, tmp_path, mcp):
    """Cutting the oldest turns changes the messages before every thinking
    block that survives the cut, so on the models that check it each one would
    be refused (400). A cut therefore drops them all; the tool call and its
    result stay."""
    first, step_one, step_two = _big_file_turn(
        scripted_llm, tmp_path, mcp, with_an_earlier_turn=True)
    assert len(step_one) == 3, "anti-vacuity: the first step still sent the earlier turn"
    assert step_two[0] == {"role": "user", "content": "read big.txt"}, (
        "anti-vacuity: the earlier turn was cut before the second step")
    assert not thinking_in(step_two), (
        "a thinking block was sent back after the messages before it were cut")
    assert step_two[1] == {"role": "assistant", "content": first[1:]}


def test_only_a_cut_takes_the_thinking(scripted_llm, tmp_path, mcp):
    """Over its bound with nothing to cut (one turn is never split), the
    transcript goes out as it is, thinking included: dropping it on every step
    of a long turn is the defect this file is about."""
    first, _, step_two = _big_file_turn(
        scripted_llm, tmp_path, mcp, with_an_earlier_turn=False)
    assert step_two[1] == {"role": "assistant", "content": first}


def test_thinking_left_in_a_saved_transcript_is_not_replayed(agent, scripted_llm):
    """This host never saves a thinking block, but a session file written by
    another version might hold one. Its system prompt, tool set or earlier
    turns may since have changed, so it is not sent."""
    agent.store.save("s1", [
        {"role": "user", "content": "before"},
        {"role": "assistant", "content": [thinking_block("b2xk"), text("an answer")]},
    ])
    scripted_llm.script = [reply([text("ok")], "end_turn")]
    assert agent.turn("s1", "and now?") == "ok"
    sent = scripted_llm.requests[0]["messages"]
    assert not thinking_in(sent), f"a saved thinking block was replayed: {sent}"
    assert sent[1] == {"role": "assistant", "content": [text("an answer")]}


def test_strip_thinking_keeps_everything_else_as_it_was():
    from agent import strip_thinking
    keep = {"role": "assistant", "content": [text("a")]}
    only = {"role": "assistant", "content": [thinking_block("s"), redacted_block("d")]}
    mixed = {"role": "assistant", "content": [thinking_block("s"), text("b"),
                                              redacted_block("d")]}
    user = {"role": "user", "content": "q"}
    carrier = {"role": "user", "content": [tool_result("t1", "r")]}
    # not a shape this host writes; a block that is not an object is left alone
    odd = {"role": "assistant", "content": ["bare", text("c")]}
    messages = [user, keep, only, mixed, carrier, odd]
    out = strip_thinking(messages)
    assert out == [user, keep, {"role": "assistant", "content": [text("b")]}, carrier, odd]
    assert out[1] is keep and out[3] is carrier and out[4] is odd, (
        "an untouched message must not be copied")
    assert mixed["content"][0]["type"] == "thinking", "the input was mutated"
    assert strip_thinking([]) == []
    untouched = [user, keep]
    assert strip_thinking(untouched) is untouched, "nothing to strip: the same list"


TRANSCRIPT_BLOCK = st.one_of(
    st.builds(text, st.text(max_size=8)),
    st.builds(thinking_block, st.text(alphabet="abc+/=", max_size=8)),
    st.builds(redacted_block, st.text(alphabet="abc+/=", max_size=8)),
    st.builds(lambda i: tool_use(f"tu_{i}", "read_file", {"path": "a"}), st.integers(0, 9)),
    st.builds(lambda i: tool_result(f"tu_{i}", "r"), st.integers(0, 9)),
)
TRANSCRIPT = st.lists(st.one_of(
    st.builds(lambda role, content: {"role": role, "content": content},
              st.sampled_from(["user", "assistant"]), st.lists(TRANSCRIPT_BLOCK, max_size=4)),
    st.builds(lambda content: {"role": "user", "content": content}, st.text(max_size=8)),
), max_size=6)


@settings(max_examples=300, deadline=None)
@given(TRANSCRIPT)
def test_stripping_removes_all_thinking_and_nothing_else(messages):
    """What is saved, and what is sent after a cut, for any transcript: no
    thinking block, every other block in its place and order, no message
    with empty content left by the removal, and nothing to do a second time."""
    from agent import strip_thinking
    before = json.dumps(messages)
    out = strip_thinking(messages)
    assert json.dumps(messages) == before, "the input was changed"
    assert not thinking_in(out)

    def others(transcript):
        return [(m["role"], block) for m in transcript if isinstance(m["content"], list)
                for block in m["content"] if block["type"] not in THINKING_TYPES]

    assert others(out) == others(messages)
    assert ([m for m in out if isinstance(m["content"], str)]
            == [m for m in messages if isinstance(m["content"], str)])
    emptied = [m for m in out if m["content"] == []]
    assert emptied == [m for m in messages if m["content"] == []], (
        "a message was left with empty content by the removal")
    assert strip_thinking(out) is out


# ── the output cap leaves room for thinking and an answer ─────────────────


def test_the_default_output_cap_leaves_room_for_thinking_and_an_answer(agent, scripted_llm):
    """max_tokens caps thinking and text together. 1,024 was the default on a
    model that thinks unless told not to, which left little or nothing for
    the answer once the model had thought. 16,000 is what the provider's own
    examples send on a non-streaming request with thinking."""
    scripted_llm.script = [reply([text("ok")], "end_turn")]
    agent.turn("s1", "hi")
    sent = scripted_llm.requests[0]["max_tokens"]
    assert sent >= 16_000, f"the default output cap is {sent} tokens"
    from agent import DEFAULT_MAX_TOKENS
    assert sent == DEFAULT_MAX_TOKENS


def test_a_reply_as_long_as_the_default_cap_allows_crosses_the_bridge(agent, scripted_llm):
    """Room in the request is no use if the answer cannot come back: a reply
    of the size the default cap allows (about four bytes a token), with a
    long signed thinking block in front of it, must parse within the guest's
    fuel budget and reach the caller whole."""
    from agent import DEFAULT_MAX_TOKENS
    answer = ("A sentence of the answer, with \"quotes\" and a\nline break. "
              * (DEFAULT_MAX_TOKENS * 4 // 58 + 1))[:DEFAULT_MAX_TOKENS * 4]
    scripted_llm.script = [reply([thinking_block("c2ln" * 4000), text(answer)], "end_turn",
                                 usage=(1200, DEFAULT_MAX_TOKENS))]
    assert agent.turn("s1", "write it all out") == answer


@pytest.mark.parametrize("cap", [0, -1, True, 2.5, "4096", None])
def test_a_cap_that_is_not_a_positive_integer_is_refused(tmp_path, cap):
    from agent import PiAgent, SessionStore
    with pytest.raises(ValueError, match="PI_MAX_TOKENS"):
        PiAgent("http://127.0.0.1:9/v1/messages", "k", store=SessionStore(tmp_path / "s"),
                sandbox_root=tmp_path / "b", mcp=None, max_tokens=cap)


def _agent_tree():
    return ast.parse((PI_ROOT / "agent.py").read_text())


def _function(tree, name):
    found = [n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(found) == 1, f"expected one function named {name}, found {len(found)}"
    return found[0]


def test_the_research_host_reads_its_cap_and_thinking_from_the_environment():
    """The truncation error tells the operator to raise PI_MAX_TOKENS, so the
    research host must read it (it did not: every turn used the default), and
    PI_THINKING is how an operator matches thinking to the configured model."""
    main = _function(_agent_tree(), "main")
    calls = [n for n in ast.walk(main) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "PiAgent"]
    assert len(calls) == 1
    passed = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
    # ast.unparse writes string literals with single quotes
    assert passed.get("max_tokens") == "_env_int('PI_MAX_TOKENS', DEFAULT_MAX_TOKENS)", (
        "agent.main() builds PiAgent without max_tokens from PI_MAX_TOKENS")
    assert passed.get("thinking") == "os.environ.get('PI_THINKING') or DEFAULT_THINKING", (
        "agent.main() builds PiAgent without thinking from PI_THINKING")


# ── guards: one way to build a request, one way to receive a reply ───────


def _self_calls(tree, method):
    """(enclosing function name, call node) for every `self.<method>(...)`."""
    hits = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == method and ast.unparse(node.func.value) == "self"):
                hits.append((function.name, node))
    return hits


def test_the_request_builder_states_the_cap_and_thinking_and_omits_what_is_empty(tmp_path):
    from agent import PiAgent, SessionStore
    host = PiAgent("http://127.0.0.1:9/v1/messages", "k", store=SessionStore(tmp_path / "s"),
                   sandbox_root=tmp_path / "b", mcp=None, model="claude-mock",
                   thinking="between_tools")
    messages = [{"role": "user", "content": "hi"}]
    assert host._request(messages, 77) == {
        "model": "claude-mock", "max_tokens": 77,
        "thinking": {"type": "between_tools"}, "messages": messages}
    assert host._request(messages, 77, system="", tools=[]) == host._request(messages, 77)
    spec = {"name": "read_file"}
    assert host._request(messages, 78, system="be brief", tools=[spec]) == {
        "model": "claude-mock", "max_tokens": 78,
        "thinking": {"type": "between_tools"}, "messages": messages,
        "system": "be brief", "tools": [spec]}


def test_one_function_builds_every_request_and_it_sets_thinking():
    """A source guard for the class: any dict literal in agent.py that names a
    model and an output cap is a Messages API request. There is one, in
    PiAgent._request, it sets `thinking` from the agent's setting, and both
    the turn loop and memory's completions build their request there."""
    tree = _agent_tree()
    requests = [n for n in ast.walk(tree) if isinstance(n, ast.Dict)
                and {"model", "max_tokens"} <= {getattr(k, "value", None) for k in n.keys}]
    builder = _function(tree, "_request")
    assert requests and all(node in list(ast.walk(builder)) for node in requests), (
        f"a request is built outside PiAgent._request, at line(s) "
        f"{[n.lineno for n in requests]}")
    assert len(requests) == 1
    values = {getattr(k, "value", None): ast.unparse(v)
              for k, v in zip(requests[0].keys, requests[0].values)}
    assert values.get("thinking") == "{'type': self.thinking}", values.get("thinking")
    assert values["max_tokens"] == "max_tokens" and values["model"] == "self.model"
    callers = {name for name, _ in _self_calls(tree, "_request")}
    assert callers == {"_turn_locked", "_memory_complete"}, callers


def test_a_model_reply_reaches_the_host_through_one_checked_path():
    """The other half: `_reply` is the only caller of the two model guests,
    it checks why the reply ended before handing anything back, and nothing
    else decodes a frame stream. A new consumer of model output cannot skip
    the check without changing this function."""
    tree = _agent_tree()
    for guest in ("_llm", "_parse"):
        callers = [name for name, _ in _self_calls(tree, guest)]
        assert callers == ["_reply"], f"self.{guest} is called from {callers}"
    reply_calls = {name for name, _ in _self_calls(tree, "_reply")}
    assert reply_calls == {"_turn_locked", "_memory_complete"}, reply_calls

    body = _function(tree, "_reply").body
    statements = [s for s in body if not (isinstance(s, ast.Expr)
                                          and isinstance(s.value, ast.Constant))]
    assert [type(s) for s in statements] == [ast.Assign, ast.Expr, ast.Return], (
        "PiAgent._reply is: parse the reply, check why it ended, return it")
    assert ast.unparse(statements[1]).startswith("require_whole_reply(blocks,"), (
        ast.unparse(statements[1]))
    assert ast.unparse(statements[2]) == "return blocks"

    decoders = [f.name for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
                for n in ast.walk(f) if isinstance(n, ast.Call)
                and getattr(n.func, "id", None) == "decode_frames"]
    assert decoders == ["_parse"], decoders


def test_the_two_documented_whole_reasons_and_no_others_are_whole():
    """end_turn and tool_use are whole replies; stop_sequence is whole up to
    a sequence the caller asked for. Everything else the API documents is a
    reply to refuse, and the two lists together are the documented seven."""
    from agent import COMPLETE_STOP_REASONS
    assert COMPLETE_STOP_REASONS == frozenset(COMPLETE)
    documented = {"end_turn", "max_tokens", "stop_sequence", "tool_use",
                  "pause_turn", "refusal", "model_context_window_exceeded"}
    assert COMPLETE_STOP_REASONS < documented
    assert documented - COMPLETE_STOP_REASONS == set(TRUNCATED) | {"pause_turn", "refusal"}


# ── the bridge: every frame parse_reply emits is decoded, whole ──────────

BRIDGE_BLOCK = st.one_of(
    st.builds(lambda s: {"type": "text", "text": s}, st.text(alphabet=UNICODE, max_size=40)),
    st.builds(lambda i, n: {"type": "tool_use", "id": f"tu_{i}", "name": n, "input": {"k": i}},
              st.integers(0, 99), st.sampled_from(["read_file", "write_file"])),
    st.builds(lambda t, s: {"type": "thinking", "thinking": t, "signature": s},
              st.text(alphabet=UNICODE, max_size=40),
              st.text(alphabet="abcXYZ019+/=", max_size=60)),
    st.builds(lambda d: {"type": "redacted_thinking", "data": d},
              st.text(alphabet="abcXYZ019+/=", max_size=60)),
)


@settings(max_examples=30, deadline=None)
@given(content=st.lists(BRIDGE_BLOCK, max_size=4), stop_reason=STOP_REASON)
def test_the_host_decodes_every_frame_the_parser_emits(mcp, content, stop_reason):
    """parse_reply and decode_frames are two halves of one protocol. Over
    real guest output, the host must get back every block it was sent — a
    thinking block equal to the provider's, field for field — the usage, and
    the stop reason, with nothing left as an unread 'other' frame."""
    from agent import decode_frames
    from sigil_compose import compose_with_stdlib
    source = compose_with_stdlib((PI_ROOT / "tools" / "parse_reply.sigil").read_text(),
                                 ["json"], SIGIL_ROOT).text
    doc = reply(content, stop_reason, usage=(5, 4))
    out = forge_ok(mcp, source, json.dumps(doc, ensure_ascii=False))
    blocks = decode_frames(out.encode())

    expected = []
    for block in content:
        if block["type"] == "text":
            expected.append(("text", block["text"]))
        elif block["type"] == "tool_use":
            expected.append(("tool_use", block["id"], block["name"], block["input"]))
        else:
            expected.append(("thinking", block))
    expected.append(("usage", 5, 4))
    if stop_reason is not ABSENT:
        expected.append(("stop", stop_reason if isinstance(stop_reason, str) else "null"))
    assert blocks == expected


def test_a_block_type_the_bridge_does_not_know_is_dropped_and_the_reply_kept(
        agent, scripted_llm):
    """Unchanged by this fix, and pinned because the fix runs through it: a
    block that is neither text, a tool call nor thinking still crosses as its
    type name only, the host leaves it out of the conversation, and the rest
    of the reply is used."""
    unknown = {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
               "input": {"query": "x"}}
    scripted_llm.script = [reply([unknown, text("the answer")], "end_turn")]
    assert agent.turn("s1", "hi") == "the answer"
    assert agent.store.load("s1")[1] == {"role": "assistant",
                                         "content": [text("the answer")]}


def test_decode_frames_tolerates_a_thinking_frame_it_cannot_read():
    """Only a stub can send one (the guest emits balanced JSON); like a bad
    usage frame it degrades to 'other' rather than crash the turn."""
    from agent import decode_frames
    assert decode_frames(b"k" + b"00000003" + b"{x}") == [("other", "{x}")]
    assert decode_frames(b"k" + b"00000002" + b"[]") == [("other", "[]")]
    assert decode_frames(b"k" + b"00000015" + b'{"type":"text"}') == [
        ("other", '{"type":"text"}')]
    assert decode_frames(b"s" + b"00000004" + b"null") == [("stop", "null")]
    assert decode_frames(b"s" + b"00000000") == [("stop", "")]
    assert decode_frames(b"?" + b"00000015" + b"server_tool_use") == [
        ("other", "server_tool_use")]


def test_the_frame_tags_are_documented_where_the_protocol_is():
    """agent.py's header and parse_reply.sigil's header both describe the
    frame protocol; each tag the guest emits must appear in both."""
    header = (PI_ROOT / "agent.py").read_text().split('"""')[1]
    guest = (PI_ROOT / "tools" / "parse_reply.sigil").read_text().split("module tool;")[0]
    for tag in ("t", "u", "k", "?", "g", "s"):
        assert re.search(rf"'{re.escape(tag)}'", header), f"agent.py header lacks '{tag}'"
        assert re.search(rf"'{re.escape(tag)}'", guest), f"parse_reply header lacks '{tag}'"
