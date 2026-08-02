"""M8 — bounded history: the durable transcript can no longer grow without limit.

Two host-side mechanisms, both deterministic and both pure functions over the
transcript (the host owns every long-lived concern; the guest owns none):

- `compact(messages, limit)` drops whole OLDEST turn-segments until the
  serialized transcript fits `limit` bytes. Cuts land only on real-user-turn
  boundaries, so a `tool_use` is never separated from its `tool_result` — the
  Messages API rejects an orphan of either, so boundary discipline IS the
  correctness property here, not a nicety.
- `clip_tool_result(text, limit)` bounds a SINGLE step: one `read_file` of a
  large file (or a `fetch` of a large page) would otherwise land in the
  transcript, and from there in kv, whole.

The honest boundary, pinned below: if the NEWEST segment alone exceeds the
limit, it is kept over-cap rather than cut into an invalid transcript.
"""
import json
import re
import sys

from hypothesis import given, settings, strategies as st

from conftest import PI_ROOT

sys.path.insert(0, str(PI_ROOT))

from agent import (  # noqa: E402
    KV_VALUE_CAP,
    MAX_HISTORY_BYTES,
    MAX_TOOL_RESULT_BYTES,
    clip_tool_result,
    compact,
    history_bytes,
    segment_starts,
)
from conftest import make_agent, msg, text, tool_use  # noqa: E402 — fixtures via conftest

TXT = st.text(alphabet=st.characters(exclude_categories=("Cs",)), max_size=40)


@st.composite
def transcripts(draw):
    """A valid pi transcript: one or more turn-segments, each a real user
    message, zero or more tool round-trips, then a final assistant text.
    tool_use ids are unique so the pairing property is meaningful."""
    messages, n = [], 0
    for _ in range(draw(st.integers(min_value=1, max_value=5))):
        messages.append({"role": "user", "content": draw(TXT)})
        for _ in range(draw(st.integers(min_value=0, max_value=2))):
            n += 1
            tu_id = f"tu{n}"
            messages.append({"role": "assistant", "content": [
                {"type": "tool_use", "id": tu_id, "name": "read_file",
                 "input": {"path": "f.txt"}}]})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tu_id, "content": draw(TXT)}]})
        messages.append({"role": "assistant", "content": [
            {"type": "text", "text": draw(TXT)}]})
    return messages


def ids_in(messages, block_type, key):
    return {b[key] for m in messages if isinstance(m.get("content"), list)
            for b in m["content"] if b.get("type") == block_type}


# ── compact: the transcript stays API-valid ─────────────────────────────


@settings(max_examples=200, deadline=None)
@given(transcripts(), st.integers(min_value=0, max_value=4000))
def test_compact_yields_a_suffix(messages, limit):
    """Compaction only ever drops from the FRONT — it never reorders, edits,
    or drops from the middle. Anything else would rewrite history."""
    kept = compact(messages, limit)
    assert kept == messages[len(messages) - len(kept):]


@settings(max_examples=200, deadline=None)
@given(transcripts(), st.integers(min_value=0, max_value=4000))
def test_compact_never_orphans_a_tool_use_or_tool_result(messages, limit):
    """THE correctness property: every surviving tool_use keeps its
    tool_result and vice versa. An orphan of either is a 400 from the API."""
    kept = compact(messages, limit)
    assert ids_in(kept, "tool_use", "id") == ids_in(kept, "tool_result", "tool_use_id")


@settings(max_examples=200, deadline=None)
@given(transcripts(), st.integers(min_value=0, max_value=4000))
def test_compact_starts_on_a_real_user_turn(messages, limit):
    """The result must open with a real user message (string content) — not a
    bare tool_result carrier, and not an assistant message."""
    kept = compact(messages, limit)
    assert kept, "compaction must never empty the transcript"
    assert kept[0]["role"] == "user"
    assert isinstance(kept[0]["content"], str)


@settings(max_examples=200, deadline=None)
@given(transcripts(), st.integers(min_value=0, max_value=4000))
def test_compact_fits_the_limit_whenever_the_last_segment_does(messages, limit):
    """The bound is honoured whenever it CAN be — i.e. unless the newest
    segment alone is already over-cap."""
    kept = compact(messages, limit)
    last_segment = messages[segment_starts(messages)[-1]:]
    if history_bytes(last_segment) <= limit:
        assert history_bytes(kept) <= limit


@settings(max_examples=200, deadline=None)
@given(transcripts(), st.integers(min_value=0, max_value=4000))
def test_compact_is_idempotent(messages, limit):
    once = compact(messages, limit)
    assert compact(once, limit) == once


def test_compact_keeps_an_oversized_newest_segment_whole():
    """The honest boundary: a single turn bigger than the cap is kept intact
    and OVER-cap — a corrupt transcript is worse than a large one."""
    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": [{"type": "text", "text": "old reply"}]},
        {"role": "user", "content": "new"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1",
                                           "name": "read_file", "input": {"path": "f"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": "x" * 5000}]},
        {"role": "assistant", "content": [{"type": "text", "text": "new reply"}]},
    ]
    kept = compact(messages, 100)
    assert kept == messages[2:]                      # older segment dropped
    assert history_bytes(kept) > 100                 # ...and honestly over-cap
    assert ids_in(kept, "tool_use", "id") == {"t1"}  # still paired


def test_compact_under_limit_is_a_no_op():
    messages = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": [{"type": "text", "text": "yo"}]}]
    assert compact(messages, MAX_HISTORY_BYTES) == messages


def test_history_bytes_matches_what_the_store_writes(tmp_path):
    """The bound must be exact against the kv file, or the cap it protects
    (KV_VALUE_CAP) is guesswork."""
    from agent import SessionStore
    messages = [{"role": "user", "content": "héllo 😀"},
                {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}]
    store = SessionStore(tmp_path / "sessions")
    store.save("s1", messages)
    assert store._path("s1").stat().st_size == history_bytes(messages)


# ── clip_tool_result: one step can't blow the transcript ────────────────


@settings(max_examples=200, deadline=None)
@given(st.text(alphabet=st.characters(exclude_categories=("Cs",)), max_size=2000),
       st.integers(min_value=0, max_value=1500))
def test_clip_respects_the_byte_budget(body, limit):
    """Unconditional — including limits too small to hold the clip notice,
    where the function must go silent rather than go over budget."""
    out = clip_tool_result(body, limit)
    assert len(out.encode()) <= limit


@settings(max_examples=200, deadline=None)
@given(st.text(alphabet=st.characters(exclude_categories=("Cs",)), max_size=2000),
       st.integers(min_value=64, max_value=1500))
def test_clip_is_transparent_below_the_budget(body, limit):
    if len(body.encode()) <= limit:
        assert clip_tool_result(body, limit) == body


def test_clip_announces_the_cut_and_keeps_the_head():
    """A silent truncation would let the model reason about a prefix as if it
    were the whole file. The clip says so, and says how much was withheld."""
    body = "HEAD-MARKER" + "x" * 5000
    out = clip_tool_result(body, 200)
    assert out.startswith("HEAD-MARKER")
    assert "clipped" in out
    assert str(len(body.encode())) in out       # the true size is reported
    assert len(out.encode()) <= 200


@settings(max_examples=300, deadline=None)
@given(st.text(alphabet=st.characters(exclude_categories=("Cs",)), max_size=2000),
       st.integers(min_value=0, max_value=1500))
def test_clip_notice_states_exactly_what_is_shown(body, limit):
    """The notice is the model's only signal that it holds a prefix, and it
    used to claim `limit` bytes shown while the head was `limit - len(notice)`
    (and up to 3 fewer at a multi-byte cut) — a small lie in the one place
    whose whole job is honesty. Whenever the notice is present, the figure it
    states must equal the byte length of the head it follows, exactly."""
    if len(body.encode()) <= limit:
        return                              # transparent path — no notice
    out = clip_tool_result(body, limit)
    m = re.search(r"\n…\[clipped: (\d+) of (\d+) bytes shown\]$", out)
    if m is None:
        return                              # limit too small even for a notice
    head = out[:m.start()]
    assert int(m.group(1)) == len(head.encode()), \
        f"notice claims {m.group(1)} bytes shown, head is {len(head.encode())}"
    assert int(m.group(2)) == len(body.encode())


def test_clip_never_splits_a_multibyte_character():
    body = "😀" * 500                            # 4 bytes each, no ascii to land on
    out = clip_tool_result(body, 101)           # deliberately off a 4-byte boundary
    assert len(out.encode()) <= 101
    out.encode().decode()                       # round-trips => no split char


# ── wired into the loop ─────────────────────────────────────────────────


def test_long_session_stays_under_the_kv_cap(scripted_llm, tmp_path, mcp):
    """The M2 limit retired: many turns no longer grow the kv value without
    bound. Drive well past the cap and the persisted file stays under it."""
    agent = make_agent(scripted_llm, tmp_path, mcp)
    agent.max_history_bytes = 1500
    scripted_llm.script = [msg([text("reply " + "r" * 100)])]
    for i in range(12):
        agent.turn("s1", f"message {i} " + "m" * 100)
    persisted = agent.store._path("s1").stat().st_size
    assert persisted <= 1500, f"kv value grew to {persisted}"
    # and the conversation still works — the NEWEST turn survived compaction
    history = agent.store.load("s1")
    assert history[-2]["content"] == "message 11 " + "m" * 100


def test_requests_stay_bounded_and_valid_across_compaction(scripted_llm, tmp_path, mcp):
    """Every outgoing payload is under the cap AND every tool_use in it is
    paired — compaction must not hand the API an invalid transcript."""
    agent = make_agent(scripted_llm, tmp_path, mcp)
    agent.max_history_bytes = 1200
    scripted_llm.script = [
        msg([tool_use("tu1", "write_file", {"path": "n.txt", "content": "x" * 200})]),
        msg([text("done " + "d" * 100)]),
    ]
    for i in range(8):
        agent.turn("s1", f"turn {i} " + "t" * 100)
    assert len(scripted_llm.requests) > 8
    for req in scripted_llm.requests:
        sent = req["messages"]
        assert history_bytes(sent) <= 1200, "an outgoing payload broke the cap"
        assert ids_in(sent, "tool_use", "id") == ids_in(sent, "tool_result", "tool_use_id")
        assert sent[0]["role"] == "user" and isinstance(sent[0]["content"], str)


def test_the_turn_in_flight_is_never_compacted_away(scripted_llm, tmp_path, mcp):
    """Compaction runs INSIDE the loop, so it must never cut into the turn
    being served — the model would lose the question it is answering. The
    newest-segment rule is what makes compaction safe to run mid-flight."""
    agent = make_agent(scripted_llm, tmp_path, mcp)
    agent.max_history_bytes = 400          # smaller than the turn it is serving
    scripted_llm.script = [
        msg([tool_use("tu1", "write_file", {"path": "a.txt", "content": "x" * 150})]),
        msg([tool_use("tu2", "write_file", {"path": "b.txt", "content": "y" * 150})]),
        msg([text("finished")]),
    ]
    ask = "REMEMBER-THIS " + "q" * 150
    assert agent.turn("s1", ask) == "finished"
    # every step still carried the user's actual question
    for req in scripted_llm.requests:
        assert req["messages"][0]["content"] == ask


def test_oversized_tool_result_is_clipped_before_it_reaches_history(scripted_llm, tmp_path, mcp):
    """A real forged read_file over a large file: the result is clipped on the
    way into the transcript, so one step can't blow the session."""
    agent = make_agent(scripted_llm, tmp_path, mcp)
    agent.max_tool_result_bytes = 2048
    (agent.sandbox_for("s1") / "big.txt").write_text("B" * 200_000)
    scripted_llm.script = [
        msg([tool_use("tu1", "read_file", {"path": "big.txt"})]),
        msg([text("read it")]),
    ]
    assert agent.turn("s1", "read the big file") == "read it"
    result = scripted_llm.requests[-1]["messages"][-1]["content"][0]
    assert result.get("is_error") is not True
    assert len(result["content"].encode()) <= 2048
    assert result["content"].startswith("BBB")
    assert "clipped" in result["content"]
    # and the clipped form is what got persisted
    assert len(json.dumps(agent.store.load("s1")).encode()) < 200_000


# ── guard: the defaults can't silently regress ──────────────────────────


def test_defaults_are_safely_under_the_kv_value_cap():
    """A config change that pushed MAX_HISTORY_BYTES near the sigil kv value
    cap would quietly reintroduce the failure M8 exists to remove."""
    from agent import MAX_SYSTEM_BYTES
    assert MAX_HISTORY_BYTES * 4 <= KV_VALUE_CAP, "history cap too close to the kv cap"
    assert MAX_TOOL_RESULT_BYTES < MAX_HISTORY_BYTES, "one result could fill all of history"
    # the system prompt rides EVERY request alongside history — it must never
    # be the payload's dominant term
    assert MAX_SYSTEM_BYTES * 4 <= MAX_HISTORY_BYTES, "system cap too close to the history cap"


def test_agent_defaults_come_from_the_module_constants(scripted_llm, tmp_path, mcp):
    agent = make_agent(scripted_llm, tmp_path, mcp)
    assert agent.max_history_bytes == MAX_HISTORY_BYTES
    assert agent.max_tool_result_bytes == MAX_TOOL_RESULT_BYTES
