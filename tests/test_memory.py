"""Cognitive memory (PiMemory + the wave-memory sidecar protocol).

Two layers, two harnesses. The PiMemory client is tested HERMETICALLY
against tests/fake_memory_sidecar.py — a scripted protocol double, so no
Rust and no toolchain. The loop wiring (recall injection, turn-boundary
records, callback completions) is tested against the REAL forges via the
mcp + scripted_llm fixtures, because "memory's completions ride the same
audited guests as turns" is a claim about actual forge traffic.

The load-bearing properties:
- memory is fail-OPEN in the loop (busy/dead degrade, never fail a turn)
  and fail-CLOSED at configuration (a refusing sidecar is a loud startup
  error naming the fix);
- recall is bounded and injected as a system suffix, never persisted;
- records buffer through a dreaming store and the buffer is bounded;
- consolidation cadence follows the M15 discipline (due-ness is a boolean,
  ticks cannot stack);
- model callbacks are answered by the agent's forge path, so the request
  reaches the LLM endpoint through agent_turn + parse_reply.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import API_KEY, msg, text

FAKE = Path(__file__).resolve().parent / "fake_memory_sidecar.py"


def make_memory(tmp_path, monkeypatch, mode="ok", scope="shared", **kw):
    from agent import PiMemory
    log = tmp_path / "fake-sidecar.log"
    monkeypatch.setenv("FAKE_MEMORY_MODE", mode)
    monkeypatch.setenv("FAKE_MEMORY_LOG", str(log))
    mem = PiMemory([sys.executable, str(FAKE)], tmp_path / "memory",
                   scope=scope, **kw)
    mem._log_path = log
    return mem


def sidecar_log(mem):
    if not mem._log_path.exists():
        return []
    return [json.loads(l) for l in mem._log_path.read_text().splitlines()]


# ── the client, hermetically ────────────────────────────────────────────


def test_recall_renders_a_bounded_labeled_block(tmp_path, monkeypatch):
    mem = make_memory(tmp_path, monkeypatch)
    block = mem.recall("s1", "what broke on friday?")
    assert "## Remembered context" in block
    assert "[episodic] previously: what broke on friday?" in block
    assert "may be incomplete or stale" in block
    mem.stop()


def test_busy_store_degrades_recall_and_buffers_records(tmp_path, monkeypatch):
    mem = make_memory(tmp_path, monkeypatch, mode="busy")
    assert mem.recall("s1", "anything") == ""
    mem.record("s1", "the deploy broke", "user_message")
    mem.record("s1", "restart fixed it", "agent_action")
    # nothing raised, both wait out the dream
    assert len(mem._pending) == 2
    mem.stop()


def test_dead_sidecar_degrades_instead_of_failing(tmp_path, monkeypatch):
    mem = make_memory(tmp_path, monkeypatch, mode="die")
    # the probe was the fake's one allowed request; everything after finds
    # it gone — and the turn-facing surface just degrades
    assert mem.recall("s1", "anything") == ""
    mem.record("s1", "into the void", "user_message")
    assert len(mem._pending) == 1
    mem.stop()


def test_refusing_sidecar_is_a_loud_startup_error(tmp_path, monkeypatch):
    from agent import PiMemory
    monkeypatch.setenv("FAKE_MEMORY_MODE", "refuse")
    with pytest.raises(RuntimeError) as e:
        PiMemory([sys.executable, str(FAKE)], tmp_path / "memory")
    assert "migrate" in str(e.value), \
        "the refusal must surface the sidecar's own remediation text"


def test_callback_consolidation_routes_through_the_completer(tmp_path, monkeypatch):
    mem = make_memory(tmp_path, monkeypatch)
    asked = []

    def complete(request):
        asked.append(request)
        return {"text": "the gist"}

    mem.complete = complete
    mem.consolidate_now()
    assert asked and asked[0]["user"] == "summarize the episodes"
    answers = [l for l in sidecar_log(mem) if "response" in l]
    assert answers and answers[0]["response"] == {"text": "the gist"}
    mem.stop()


def test_completer_failure_degrades_to_unavailable(tmp_path, monkeypatch):
    mem = make_memory(tmp_path, monkeypatch)

    def complete(request):
        raise RuntimeError("llm forge failed: 502")

    mem.complete = complete
    mem.consolidate_now()  # must not raise
    answers = [l for l in sidecar_log(mem) if "response" in l]
    assert answers and answers[0]["response"] == {"unavailable": True}
    mem.stop()


def test_tick_follows_the_m15_due_ness_discipline(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=100.0)
    mem = make_memory(tmp_path, monkeypatch, every_s=10,
                      clock=SimpleNamespace(time=lambda: clock.now))
    mem.complete = lambda request: {"text": "gist"}

    def consolidations():
        return sum(1 for l in sidecar_log(mem) if l.get("op") == "consolidate")

    mem.tick()                       # first tick: due (never run)
    assert consolidations() == 1
    clock.now = 105.0
    mem.tick()                       # not due — 5s of a 10s cadence
    assert consolidations() == 1
    clock.now = 111.0
    mem.tick()                       # due again — one firing, no backlog
    assert consolidations() == 2
    mem.stop()


# ── the loop wiring, over real forges ───────────────────────────────────


def make_agent_with_memory(scripted_llm, tmp_path, mcp, monkeypatch, **mem_kw):
    from agent import PiAgent, SessionStore
    mem = make_memory(tmp_path, monkeypatch, **mem_kw)
    kv = tmp_path / "sessions"
    sandbox_root = tmp_path / "sandboxes"
    kv.mkdir()
    sandbox_root.mkdir()
    agent = PiAgent(scripted_llm.url, API_KEY, store=SessionStore(kv),
                    sandbox_root=sandbox_root, mcp=mcp, model="claude-mock",
                    memory=mem)
    return agent, mem


def test_turn_injects_recall_and_records_the_spine(scripted_llm, tmp_path, mcp,
                                                   monkeypatch):
    agent, mem = make_agent_with_memory(scripted_llm, tmp_path, mcp, monkeypatch)
    scripted_llm.script = [msg([text("we restarted the deploy")])]
    reply = agent.turn("s1", "what did we do about the deploy?")
    assert reply == "we restarted the deploy"

    # recall rode the request as a system suffix (no PI_SYSTEM configured,
    # so the block IS the system field) …
    system = scripted_llm.requests[0].get("system", "")
    assert "## Remembered context" in system
    assert "previously: what did we do about the deploy?" in system
    # … and never entered the durable transcript
    assert "Remembered context" not in json.dumps(agent.store.load("s1"))

    # the conversational spine became experience, stamped with the session
    records = [l for l in sidecar_log(mem) if l.get("op") == "record"]
    assert [(r["session"], r["kind"], r["text"]) for r in records] == [
        ("s1", "user_message", "what did we do about the deploy?"),
        ("s1", "agent_action", "we restarted the deploy"),
    ]
    mem.stop()


def test_memory_completions_ride_the_agents_forges(scripted_llm, tmp_path, mcp,
                                                   monkeypatch):
    """The whole point of the callback design: a consolidation completion
    reaches the LLM endpoint THROUGH agent_turn (net + host-injected key) and
    parse_reply — the same guests, the same audit chokepoint — not through
    any transport of the sidecar's own."""
    agent, mem = make_agent_with_memory(scripted_llm, tmp_path, mcp, monkeypatch)
    assert mem.complete is not None, "adoption wires the forge completer"
    scripted_llm.script = [msg([text("a consolidated gist")])]

    mem.consolidate_now()

    sent = scripted_llm.requests[-1]
    assert sent["system"] == "you consolidate"
    assert sent["messages"] == [{"role": "user",
                                 "content": "summarize the episodes"}]
    assert sent["max_tokens"] == 256
    answers = [l for l in sidecar_log(mem) if "response" in l]
    assert answers[0]["response"]["text"] == "a consolidated gist"
    mem.stop()


def test_memoryless_agent_is_byte_identical(scripted_llm, tmp_path, mcp):
    """No memory configured => exactly the payloads pi always sent — the
    integration must be invisible until an operator opts in."""
    from conftest import make_agent
    agent = make_agent(scripted_llm, tmp_path, mcp)
    scripted_llm.script = [msg([text("ok")])]
    agent.turn("s1", "hello")
    assert "system" not in scripted_llm.requests[0]


# ── hardening: hostile sidecars, races, and bounds ──────────────────────


def test_callback_storm_is_bounded_and_kills_the_sidecar(tmp_path, monkeypatch):
    """Each callback fires a real completion forge, so an unbounded loop is
    a cost bomb. Past MAX_MEMORY_CALLBACKS the sidecar is broken by
    definition — kill it, degrade, carry on."""
    from agent import MAX_MEMORY_CALLBACKS
    mem = make_memory(tmp_path, monkeypatch, mode="storm")
    calls = []
    mem.complete = lambda request: (calls.append(1), {"unavailable": True})[1]
    mem.consolidate_now()  # returns rather than looping forever
    assert len(calls) <= MAX_MEMORY_CALLBACKS
    assert mem.recall("s1", "anything") == "", "storming sidecar is now dead"
    mem.stop()


def test_wrong_reply_id_is_protocol_corruption(tmp_path, monkeypatch):
    mem = make_memory(tmp_path, monkeypatch, mode="wrongid")
    assert mem.recall("s1", "anything") == ""
    mem.record("s1", "buffered instead", "user_message")
    assert len(mem._pending) == 1, "a corrupt sidecar degrades like a dead one"
    mem.stop()


def test_oversized_recall_is_clipped_with_an_honest_notice(tmp_path, monkeypatch):
    mem = make_memory(tmp_path, monkeypatch, mode="huge", block_bytes=4096)
    block = mem.recall("s1", "everything")
    assert len(block.encode()) <= 4096, "the byte budget is honoured"
    assert "clipped:" in block, "the cut is announced, never silent"
    mem.stop()


def test_pending_buffer_is_bounded_under_concurrent_records(tmp_path, monkeypatch):
    """MAX_MEMORY_PENDING holds even when every turn thread records at once
    against a permanently busy store — no crash, no unbounded growth, no
    lost accounting from interleaved read-modify-write."""
    import threading as th
    from agent import MAX_MEMORY_PENDING
    mem = make_memory(tmp_path, monkeypatch, mode="busy")
    mem.recall_lock_timeout = 0.05

    def hammer(t):
        for i in range(60):
            mem.record(f"s{t}", f"record {t}/{i}", "user_message")

    threads = [th.Thread(target=hammer, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 480 attempted > cap; all stayed pending (store is busy) so the buffer
    # sits exactly at its bound.
    assert len(mem._pending) == MAX_MEMORY_PENDING
    mem.stop()


def test_recall_timeout_preserves_turn_liveness(tmp_path, monkeypatch):
    """A dream cycle holds the protocol lock for its whole conversation;
    recall must give up quickly and answer '' rather than hang the turn."""
    import time as _time
    mem = make_memory(tmp_path, monkeypatch)
    mem.recall_lock_timeout = 0.1
    mem._lock.acquire()          # simulate an in-flight consolidation
    try:
        t0 = _time.monotonic()
        assert mem.recall("s1", "anything") == ""
        assert _time.monotonic() - t0 < 1.0
    finally:
        mem._lock.release()
    mem.stop()


# ── hardening over real forges ──────────────────────────────────────────


def test_memory_completions_land_in_the_audit_chain(scripted_llm, tmp_path, mcp,
                                                    monkeypatch):
    """The M17 headline, pinned: a consolidation completion writes llm+parse
    records into the proof-carrying chain, and the chain still verifies."""
    from agent import verify_audit_dir
    agent, mem = make_agent_with_memory(scripted_llm, tmp_path, mcp, monkeypatch)
    scripted_llm.script = [msg([text("a gist")])]
    audit_dir = agent.audit.root if hasattr(agent.audit, "root") else \
        tmp_path / "audit"
    before = verify_audit_dir(audit_dir)["records"]

    mem.consolidate_now()

    report = verify_audit_dir(audit_dir)
    assert report["ok"], f"chain must verify after memory writes: {report}"
    assert report["records"] >= before + 2, \
        "the completion adds an llm and a parse record"
    mem.stop()


def test_completion_max_tokens_is_capped(scripted_llm, tmp_path, mcp, monkeypatch):
    """The sidecar chooses max_tokens; the HOST bounds it — a broken request
    for a billion tokens must not ride a real payload."""
    agent, mem = make_agent_with_memory(scripted_llm, tmp_path, mcp, monkeypatch)
    scripted_llm.script = [msg([text("ok")])]
    response = agent._memory_complete(
        {"user": "hello", "system": "", "max_tokens": 10**9})
    assert response["text"] == "ok"
    assert scripted_llm.requests[-1]["max_tokens"] == 4096
    mem.stop()


def test_concurrent_sessions_forge_correctly_under_the_lock(scripted_llm,
                                                            tmp_path, mcp):
    """Two sessions turning at once — the _forge serialization must keep
    every response attributed to its own request (SigilMCP itself matches
    replies by nothing but arrival order)."""
    import threading as th
    from conftest import make_agent
    agent = make_agent(scripted_llm, tmp_path, mcp)
    scripted_llm.script = [msg([text("same answer for everyone")])]
    replies, errors = {}, []

    def one_turn(name):
        try:
            replies[name] = agent.turn(name, f"hello from {name}")
        except Exception as e:  # noqa: BLE001 — collected for the assertion
            errors.append(e)

    threads = [th.Thread(target=one_turn, args=(f"s{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"concurrent turns must not corrupt the mcp: {errors}"
    assert all(r == "same answer for everyone" for r in replies.values())
    assert len(replies) == 6


WAVE_SIDECAR = Path(__file__).resolve().parents[2] / "wave-agent" / "target" / \
    "debug" / "wave-memory-sidecar"


@pytest.mark.skipif(not WAVE_SIDECAR.exists(),
                    reason=f"real wave-memory sidecar not built: {WAVE_SIDECAR}")
def test_real_sidecar_honors_the_fakes_contract(tmp_path):
    """The drift guard: the hermetic suite is only as honest as the fake's
    fidelity, so the REAL binary must answer the same envelopes the fake
    does — record receipts, rendered recall, and the migrate-naming refusal
    when the scope flag flips against an existing root."""
    from agent import PiMemory
    mem = PiMemory(WAVE_SIDECAR, tmp_path / "memory", scope="session")
    mem.record("s1", "the real binary remembers", "user_message")
    assert mem._pending == [], "the real store accepted the record"
    block = mem.recall("s1", "what does the real binary do?")
    assert "## Remembered context (untrusted)" in block
    mem.stop()

    with pytest.raises(RuntimeError) as e:
        PiMemory(WAVE_SIDECAR, tmp_path / "memory", scope="shared")
    assert "migrate" in str(e.value)
