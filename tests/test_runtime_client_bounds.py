"""F-11: a compiler exchange is bounded in time and in size, end to end.

Before this, ``ProductionSigilMCP._request`` waited for the compiler's stdout
to become READABLE, with the deadline, and then called ``readline()`` with
none. A compiler that wrote part of a frame and stalled was readable at once,
so the caller blocked until the compiler moved on, the compiler was never
killed, the "hard" turn deadline was not hard, and the line had no size limit
(audit 2026-11-10, F-11; audit/repro/r10_mcp_watchdog.py). The request had the
same gap from the other side: a compiler that stopped draining its stdin held
the caller in ``write()`` for as long as it liked, and a request routinely
outgrows a pipe (the history alone may be 256 KiB). The stderr drain read
whole lines, so one endless line grew the host with it. ``runtime_client.py``
sat at 100% line and branch coverage throughout, because its doubles'
``readline()`` could not stall.

So these tests drive REAL processes over REAL pipes: a stand-in compiler for
each way an exchange can wedge, the supervisor and a whole agent turn over
one, properties over every way a response can be split into reads or cut off,
and a guard on the module's source for the class.
"""

import ast
import itertools
import json
import os
import resource
import select
import shlex
import sys
import textwrap
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given, settings, strategies as st

import runtime_client
from runtime_client import (
    ProductionSigilMCP, RuntimeClientError, RuntimeClientTimeout, SupervisedRuntime)

PI_ROOT = Path(__file__).resolve().parent.parent
MIB = 1024 * 1024
STALL_S = 60      # how long a stand-in wedges: far past every deadline here
BUDGET_S = 1.0    # the caller's deadline in these tests
GRACE_S = 3.0     # scheduling slack on a loaded machine; the bug is STALL_S
FRAME = '{"jsonrpc":"2.0","id":1,"result":{}}'

# Prepended to every stand-in. `respond` and `forged` speak the protocol the
# way sigil-mcp does: one JSON line per response, flushed.
_PRELUDE = '''\
import json, os, sys, time


def respond(request_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id,
                                 "result": result}) + "\\n")
    sys.stdout.flush()


def forged(text):
    envelope = {"status": "ok", "data": {"output_text": text}}
    return {"content": [{"type": "text", "text": json.dumps(envelope)}]}


sys.stderr.write("stand-in ready\\n")
sys.stderr.flush()
'''


def _stand_in(tmp_path, body, **constants):
    """A stand-in sigil-mcp: a real child process speaking over real pipes.

    Launched through /bin/sh rather than a shebang, because a shebang line has
    a length limit and this interpreter's path (a venv in a scratch directory)
    can exceed it."""
    script = tmp_path / "stand_in.py"
    settings_ = "".join(f"{name} = {value!r}\n"
                        for name, value in {"STALL": STALL_S, **constants}.items())
    script.write_text(_PRELUDE + settings_ + textwrap.dedent(body))
    launcher = tmp_path / "sigil-mcp"
    launcher.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))}\n")
    launcher.chmod(0o755)
    return launcher


@pytest.fixture
def spawned():
    """Spawn clients over stand-ins, and always kill them at teardown: a test
    that FAILS here does so because a caller is still blocked on one."""
    clients = []

    def started(client):
        return any(entry.endswith("stand-in ready") for entry in client.stderr_log)

    def spawn(binary, **kwargs):
        client = ProductionSigilMCP.spawn(binary, **kwargs)
        clients.append(client)
        # Wait until the child is up, so a deadline below measures the
        # exchange and not the interpreter's start on a loaded machine.
        deadline = time.monotonic() + 30
        while not started(client) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started(client), "the stand-in never started"
        return client

    yield spawn
    for client in clients:
        try:
            client._proc.kill()
            client._proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - teardown of a process that may be gone
            pass


def _bounded(call, *, within):
    """Run ``call`` on a thread and wait at most ``within`` seconds for it.

    The defect this file guards against is a caller that never returns, so a
    test must be able to FAIL on it rather than hang the suite."""
    box = {}

    def run():
        started = time.monotonic()
        try:
            box["value"] = call()
        except BaseException as error:  # noqa: BLE001 - the outcome IS the measurement
            box["error"] = error
        box["seconds"] = time.monotonic() - started

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(within)
    return SimpleNamespace(finished=not thread.is_alive(), thread=thread,
                           value=box.get("value"), error=box.get("error"),
                           seconds=box.get("seconds"))


def _killed(client):
    """The stand-in is gone: killed and reaped by the client itself."""
    return client._proc.poll() is not None


# ── time: the whole frame, not the first byte ─────────────────────────────


@pytest.mark.parametrize("caller_s,watchdog_s,budget_expired", [
    (BUDGET_S, 30.0, True),    # the caller's turn budget won the min(): routine
    (None, BUDGET_S, False),   # the base watchdog fired: the compiler's fault
], ids=["caller-budget", "base-watchdog"])
def test_a_frame_that_stalls_partway_is_killed_at_the_deadline(
        tmp_path, spawned, caller_s, watchdog_s, budget_expired):
    """THE F-11 REPRODUCTION, as a test: half a frame, then nothing.

    ``budget_expired`` must still say whose deadline ran out, because the
    supervisor counts only the watchdog's kills against runtime health."""
    client = spawned(_stand_in(tmp_path, '''
        sys.stdin.readline()
        sys.stdout.write('{"jsonrpc":"2.0","id":1,')
        sys.stdout.flush()
        time.sleep(STALL)
    '''), timeout_s=watchdog_s)
    outcome = _bounded(lambda: client.initialize(timeout_s=caller_s),
                       within=BUDGET_S + GRACE_S)
    assert outcome.finished, (
        f"the caller is still blocked {BUDGET_S + GRACE_S}s into a {BUDGET_S}s "
        f"deadline: the watchdog bounded the first byte, not the frame")
    assert isinstance(outcome.error, RuntimeClientTimeout), repr(outcome.error)
    assert outcome.error.budget_expired is budget_expired
    assert _killed(client), "the wedged compiler must be killed, not left running"
    assert client.is_healthy is False
    # The half frame did arrive: this was a stall MID-frame, not before it.
    assert bytes(client._unread) == b'{"jsonrpc":"2.0","id":1,'


@pytest.mark.parametrize("cut", [0, 1, len(FRAME) // 2, len(FRAME)],
                         ids=["before-the-first-byte", "after-one-byte", "mid-frame",
                              "all-but-the-newline"])
def test_a_frame_cut_off_anywhere_ends_at_the_deadline(tmp_path, spawned, cut):
    """Wherever the compiler stops, the caller gets its answer at the deadline.
    Before the first byte was the one case the old watchdog covered; the last
    case is complete JSON whose newline never comes, which is not a frame."""
    client = spawned(_stand_in(tmp_path, '''
        sys.stdin.readline()
        sys.stdout.write(PARTIAL)
        sys.stdout.flush()
        time.sleep(STALL)
    ''', PARTIAL=FRAME[:cut]), timeout_s=30.0)
    outcome = _bounded(lambda: client.initialize(timeout_s=BUDGET_S),
                       within=BUDGET_S + GRACE_S)
    assert outcome.finished, f"caller still blocked after the compiler sent {cut} bytes"
    assert isinstance(outcome.error, RuntimeClientTimeout), repr(outcome.error)
    assert outcome.error.budget_expired is True
    assert _killed(client)


def test_a_trickle_that_never_finishes_its_frame_is_still_bounded(tmp_path, spawned):
    """A deadline per READ would never fire here, because a byte keeps arriving.
    The deadline belongs to the whole frame."""
    client = spawned(_stand_in(tmp_path, '''
        sys.stdin.readline()
        sys.stdout.write('{"jsonrpc":"2.0","id":1,"result":{"pad":"')
        for _ in range(STALL * 20):
            sys.stdout.write("x")
            sys.stdout.flush()
            time.sleep(0.05)
    '''), timeout_s=30.0)
    outcome = _bounded(lambda: client.initialize(timeout_s=BUDGET_S),
                       within=BUDGET_S + GRACE_S)
    assert outcome.finished, "a compiler that trickles bytes held the caller indefinitely"
    assert isinstance(outcome.error, RuntimeClientTimeout), repr(outcome.error)
    assert outcome.seconds < BUDGET_S + GRACE_S
    assert _killed(client)


def test_a_compiler_that_stops_reading_its_input_cannot_hold_the_request(
        tmp_path, spawned):
    """The other half of the exchange. A request larger than a pipe holds must
    be written inside the same deadline, or a compiler that stops draining its
    stdin holds the caller in write() before any deadline is consulted."""
    client = spawned(_stand_in(tmp_path, '''
        request = json.loads(sys.stdin.readline())
        respond(request["id"], {})
        time.sleep(STALL)        # and never reads stdin again
    '''), timeout_s=30.0)
    client.initialize(timeout_s=5.0)
    outcome = _bounded(lambda: client.forge("x" * MIB, timeout_s=BUDGET_S),
                       within=BUDGET_S + GRACE_S)
    assert outcome.finished, "the caller is blocked writing a request nobody reads"
    assert isinstance(outcome.error, RuntimeClientTimeout), repr(outcome.error)
    assert outcome.error.budget_expired is True
    assert _killed(client)


# ── size: the frame is capped ─────────────────────────────────────────────


def test_an_endless_response_line_is_refused_at_the_default_cap(tmp_path, spawned):
    """No newline, ever, and more bytes than any legitimate frame. The product's
    default client refuses it on size, well before its deadline, kills the
    compiler, and never holds more than the cap plus one byte."""
    client = spawned(_stand_in(tmp_path, '''
        sys.stdin.readline()
        chunk = "x" * (1024 * 1024)
        for _ in range(80):
            sys.stdout.write(chunk)
        sys.stdout.flush()
        time.sleep(STALL)
    '''), timeout_s=30.0)
    outcome = _bounded(lambda: client.initialize(timeout_s=20.0), within=20.0 + GRACE_S)
    assert outcome.finished, "an endless response line held the caller"
    cap = runtime_client.MAX_RESPONSE_BYTES
    assert cap < 80 * MIB
    assert isinstance(outcome.error, RuntimeClientError), repr(outcome.error)
    assert not isinstance(outcome.error, RuntimeClientTimeout), \
        "refused on size, not on the clock"
    assert f"exceeds {cap} bytes" in str(outcome.error)
    assert len(client._unread) <= cap + 1
    assert _killed(client) and client.is_healthy is False


def test_the_cap_admits_a_frame_of_exactly_its_size_and_refuses_one_byte_more(
        tmp_path, spawned):
    cap = 4096
    client = spawned(_stand_in(tmp_path, '''
        def answer(extra):
            request = json.loads(sys.stdin.readline())
            head = '{"jsonrpc":"2.0","id":%d,"result":{"pad":"' % request["id"]
            tail = '"}}'
            pad = "p" * (CAP + extra - len(head) - len(tail))
            sys.stdout.write(head + pad + tail + "\\n")
            sys.stdout.flush()
        answer(0)        # exactly CAP bytes before the newline
        answer(1)        # one byte more
        time.sleep(STALL)
    ''', CAP=cap), timeout_s=30.0, max_response_bytes=cap)
    admitted = client.initialize(timeout_s=5.0)
    assert len(json.dumps({"jsonrpc": "2.0", "id": 1, "result": admitted},
                          separators=(",", ":"))) == cap
    with pytest.raises(RuntimeClientError, match=f"exceeds {cap} bytes") as caught:
        client.initialize(timeout_s=5.0)
    assert not isinstance(caught.value, RuntimeClientTimeout)
    assert _killed(client) and client.is_healthy is False


_EMIT_CONTROL_BYTES = """module tool;

pub fn tool_main(input_ptr: i64, input_len: i64) -> i64 ! { Alloc } {
    let n: i64 = %d;
    let outp: i64 = alloc(n);
    let mut idx: i64 = 0;
    while idx < n {
        store8(outp + idx, 1);
        idx = idx + 1;
    }
    return outp * 4294967296 + n;
}
"""


def test_the_cap_admits_the_longest_frame_the_pinned_compiler_can_send(mcp, monkeypatch):
    """The cap must refuse nothing legitimate. The pinned compiler's longest
    frame is its largest tool output (5 MiB) made of control bytes, each of
    which the response escapes twice into seven bytes: about 35 MiB, which
    must arrive whole. One byte more output is refused by the compiler itself,
    so 5 MiB really is its maximum. If a pin bump raises that maximum, the
    second half fails here: revisit MAX_RESPONSE_BYTES with it."""
    frames = []
    receive = mcp._receive

    def measured(deadline):
        line = receive(deadline)
        frames.append(len(line))
        return line

    monkeypatch.setattr(mcp, "_receive", measured)
    most = 5 * MIB
    largest = mcp.forge(_EMIT_CONTROL_BYTES % most, fuel=2_000_000_000)
    assert largest.get("status") == "ok", largest.get("diagnostics")
    assert largest["data"]["output_text"] == "\x01" * most
    assert 7 * most <= frames[-1] <= runtime_client.MAX_RESPONSE_BYTES, frames[-1]
    beyond = mcp.forge(_EMIT_CONTROL_BYTES % (most + 1), fuel=2_000_000_000)
    assert beyond.get("status") != "ok"
    assert "exceeds forge limit" in beyond["diagnostics"][0]["message"]


def test_a_non_positive_response_cap_is_refused(tmp_path):
    binary = tmp_path / "sigil-mcp"
    binary.write_bytes(b"placeholder")
    with pytest.raises(ValueError, match="positive"):
        ProductionSigilMCP.spawn(binary, max_response_bytes=0)


# ── end to end: the supervisor and a whole turn ───────────────────────────

_STALL_ON_FIRST_FORGE = '''
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        request = json.loads(line)
        if request["method"] != "tools/call":
            respond(request["id"], {})
            continue
        if not os.path.exists(MARKER):
            open(MARKER, "w").close()
            sys.stdout.write('{"jsonrpc":"2.0","id":%d,' % request["id"])
            sys.stdout.flush()
            time.sleep(STALL)
        respond(request["id"], forged("served"))
'''


def test_the_supervisor_replaces_a_compiler_that_stalled_mid_frame(tmp_path, spawned):
    """One tenant's stalled forge costs that tenant its budget and the compiler
    its life, and nothing else: the next forge gets a fresh generation, and a
    kill on the caller's own clock does not count against runtime health."""
    fake = _stand_in(tmp_path, _STALL_ON_FIRST_FORGE, MARKER=str(tmp_path / "stalled"))
    runtime = SupervisedRuntime(lambda: spawned(fake, timeout_s=30.0))
    with runtime:
        first = _bounded(lambda: runtime.forge("tenant A", timeout_s=BUDGET_S),
                         within=BUDGET_S + GRACE_S)
        assert first.finished, "a stalled frame held the supervisor's only compiler"
        assert isinstance(first.error, RuntimeClientTimeout), repr(first.error)
        assert first.error.budget_expired is True
        served = runtime.forge("tenant B", timeout_s=30.0)
        assert served["data"]["output_text"] == "served"
        assert runtime.generation == 2
        assert runtime.is_healthy is True and runtime.unhealthy_replacements == 0


def test_a_turn_deadline_is_hard_against_a_compiler_that_stalls_mid_frame(
        tmp_path, spawned):
    """What the turn deadline promises (PiAgent.turn_with_usage: it "kills a
    wedged compiler mid-forge, and surfaces as TimeoutError"), checked through
    the real agent, the real supervisor and a real wedged process."""
    from agent import AuditLog, PiAgent, SessionStore
    fake = _stand_in(tmp_path, _STALL_ON_FIRST_FORGE, MARKER=str(tmp_path / "stalled"))
    runtime = SupervisedRuntime(lambda: spawned(fake, timeout_s=30.0))
    (tmp_path / "sandboxes").mkdir()
    agent = PiAgent(
        "https://example.invalid/v1/messages", "sk-test-not-a-real-key",
        store=SessionStore(tmp_path / "sessions"), sandbox_root=tmp_path / "sandboxes",
        mcp=runtime, audit=AuditLog(tmp_path / "audit", key=b"k" * 32), memory=None)
    agent._llm_src = "LLM"      # composed sources are inputs to forging; assigning
    agent._parse_src = "PARSE"  # them skips the stdlib lookup, as tests elsewhere do
    turn_s = 2.0
    with runtime:
        outcome = _bounded(
            lambda: agent.turn_with_usage(
                "s", "hi", deadline_monotonic=time.monotonic() + turn_s),
            within=turn_s + GRACE_S)
        assert outcome.finished, "the hard turn deadline did not end the turn"
        assert isinstance(outcome.error, TimeoutError), repr(outcome.error)
        assert "during forge execution" in str(outcome.error)
    records = [json.loads(line)
               for path in sorted((tmp_path / "audit").glob("*.jsonl"))
               for line in path.read_text().splitlines()]
    # The killed forge is audited as what it was: the turn's deadline, not a
    # runtime fault.
    assert [(r["kind"], r["error"]) for r in records] == [("llm", "turn_deadline_exceeded")]


# ── stderr: read in bounded pieces too ────────────────────────────────────


def test_one_endless_stderr_line_is_kept_to_a_bounded_piece(tmp_path, spawned):
    """Found in the F-11 sweep: the stderr drain read whole lines, so one
    endless line from the compiler grew the host's memory with it (256 MiB of
    stderr raised peak RSS by 342 MiB). The log keeps the head of such a line,
    says it was cut, and carries on with the next."""
    client = spawned(_stand_in(tmp_path, '''
        sys.stderr.write("e" * (1024 * 1024))
        sys.stderr.write("\\nafter\\n")
        sys.stderr.flush()
        time.sleep(STALL)
    '''), timeout_s=30.0, generation=3)
    deadline = time.monotonic() + 10
    while len(client.stderr_log) < 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    log = client.stderr_log
    assert len(log) == 3, [len(entry) for entry in log]
    assert max(len(entry) for entry in log) < 64 * 1024, \
        "a stderr line is held whole, however long the compiler makes it"
    ready, cut, after = log
    assert ready == "[gen3] stand-in ready"
    assert cut == "[gen3] " + "e" * runtime_client.STDERR_LINE_CHARS + " [truncated]"
    assert after == "[gen3] after"


# ── properties over real pipes ────────────────────────────────────────────


class _Pipes:
    """A Popen-shaped object over two real pipes whose far ends the test holds.

    The near ends are text wrappers, as ``Popen(text=True)`` hands them to the
    client. The far ends play the compiler: ``responses`` is what it says and
    ``requests`` is what it was asked."""

    def __init__(self):
        in_read, in_write = os.pipe()
        out_read, out_write = os.pipe()
        self.stdin = open(in_write, "w", encoding="utf-8")
        self.stdout = open(out_read, "r", encoding="utf-8", errors="replace")
        self.requests = open(in_read, "rb", buffering=0)
        self.responses = open(out_write, "wb", buffering=0)
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self, timeout):
        return 0

    def poll(self):
        return 0 if self.killed else None

    def hang_up(self):
        """End of the compiler's output, as when the compiler exits."""
        self.responses.close()

    def close(self):
        for stream in (self.responses, self.requests, self.stdin, self.stdout):
            try:
                stream.close()
            except OSError:
                pass


def _client_over(pipes, **kwargs):
    return ProductionSigilMCP(pipes, deque(), None, **{"timeout_s": 5.0, **kwargs})


def _response(request_id, payload):
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {"v": payload}},
                      ensure_ascii=False).encode("utf-8")


def _split(stream, cuts):
    bounds = [0, *sorted(cuts), len(stream)]
    return [stream[a:b] for a, b in zip(bounds, bounds[1:])]


def _write_all(pipes, chunks):
    def write():
        try:
            for chunk in chunks:
                pipes.responses.write(chunk)
        except (OSError, ValueError):
            pass    # the client stopped reading and the test hung up

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    return writer


@settings(max_examples=60, deadline=None)
@given(payloads=st.lists(st.text(max_size=300), min_size=1, max_size=6),
       slack=st.integers(min_value=0, max_value=64), data=st.data())
def test_any_split_of_frames_into_reads_reassembles_them_exactly(payloads, slack, data):
    """However the frames are cut into reads, including several frames in one
    read and a multi-byte character split across two, each request gets
    exactly its own frame back; a frame exactly as long as the cap is
    admitted. Bytes past one frame are the start of the next, as they were
    for readline()."""
    frames = [_response(i, payload) for i, payload in enumerate(payloads, start=1)]
    stream = b"".join(frame + b"\n" for frame in frames)
    cuts = data.draw(st.sets(st.integers(min_value=1, max_value=len(stream) - 1),
                             max_size=12))
    pipes = _Pipes()
    client = _client_over(pipes, max_response_bytes=max(map(len, frames)) + slack)
    writer = _write_all(pipes, _split(stream, cuts))
    try:
        for payload in payloads:
            assert client._request("m") == {"v": payload}
        assert not pipes.killed and client.is_healthy
    finally:
        pipes.hang_up()
        writer.join(5)
        pipes.close()


@settings(max_examples=40, deadline=None)
@given(cap=st.integers(min_value=16, max_value=2048),
       over=st.integers(min_value=1, max_value=4096),
       newline=st.booleans(), data=st.data())
def test_a_frame_longer_than_the_cap_is_refused_after_at_most_one_byte_more(
        cap, over, newline, data):
    """Refused on size, whether or not its newline ever comes, without the
    client holding more than the cap plus one byte of it."""
    stream = b'{"pad":"' + b"x" * (cap + over) + b'"}' + (b"\n" if newline else b"")
    cuts = data.draw(st.sets(st.integers(min_value=1, max_value=len(stream) - 1),
                             max_size=8))
    pipes = _Pipes()
    client = _client_over(pipes, max_response_bytes=cap)
    writer = _write_all(pipes, _split(stream, cuts))
    try:
        outcome = _bounded(lambda: client._request("m"), within=5.0 + GRACE_S)
        assert outcome.finished, "an oversized frame held the caller"
        assert isinstance(outcome.error, RuntimeClientError), repr(outcome.error)
        assert not isinstance(outcome.error, RuntimeClientTimeout)
        assert f"exceeds {cap} bytes" in str(outcome.error)
        assert len(client._unread) <= cap + 1
        assert pipes.killed and not client.is_healthy
    finally:
        pipes.hang_up()
        writer.join(5)
        pipes.close()


@settings(max_examples=25, deadline=None)
@given(payload=st.text(max_size=200), data=st.data())
def test_a_frame_cut_off_at_any_byte_ends_at_the_deadline(payload, data):
    """The property behind the stand-in cases above: for any frame and any
    point where its sender stops, the exchange ends at the deadline with the
    compiler killed. The newline never comes, so even a complete JSON object
    is not a frame."""
    frame = _response(1, payload)
    cut = data.draw(st.integers(min_value=0, max_value=len(frame)))
    pipes = _Pipes()
    pipes.responses.write(frame[:cut])
    client = _client_over(pipes)
    budget = 0.05
    outcome = _bounded(lambda: client._request("m", timeout_s=budget),
                       within=budget + GRACE_S)
    try:
        assert outcome.finished, f"caller still blocked after {cut} of {len(frame)} bytes"
        assert isinstance(outcome.error, RuntimeClientTimeout), repr(outcome.error)
        assert outcome.error.budget_expired is True
        assert pipes.killed
    finally:
        pipes.hang_up()          # releases a reader stuck in a blocking read
        outcome.thread.join(5)
        pipes.close()


def test_a_deadline_that_passes_between_waits_ends_the_exchange_without_another(
        monkeypatch):
    """The deadline is re-read before every wait, so time spent between waits
    counts: here the clock moves a second per reading, the write waits the
    half second left, and the read finds the deadline gone and never waits."""
    pipes = _Pipes()
    readings = itertools.count(0.0, 1.0)
    client = _client_over(pipes, timeout_s=30.0, clock=lambda: next(readings))
    waits = []
    real_poll = select.poll

    class Recording:
        def __init__(self):
            self._poller = real_poll()

        def register(self, *args):
            self._poller.register(*args)

        def poll(self, timeout_ms):
            waits.append(timeout_ms)
            return self._poller.poll(timeout_ms)

    monkeypatch.setattr(runtime_client.select, "poll", Recording)
    try:
        with pytest.raises(RuntimeClientTimeout, match="was killed") as caught:
            client._request("initialize", timeout_s=1.5)
        assert waits == [500]
        assert caught.value.budget_expired is True
        assert pipes.killed and not client.is_healthy
    finally:
        pipes.close()


def test_a_compiler_pipe_numbered_past_fd_setsize_is_still_waited_on():
    """select.select refuses any descriptor numbered at or above FD_SETSIZE
    (1024 on Linux and macOS) with ValueError, and a busy product process gets
    there: a pool of compilers with three pipes each, client sockets, audit and
    state files. A compiler's pipes then get such numbers, and every exchange
    with it would fail before a byte moved (audit 2026-11-10, R1 sweep, routed
    to A5). The wait must not depend on the descriptor's number."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    high_read, high_write = 1500, 1501
    if soft <= high_write:
        resource.setrlimit(resource.RLIMIT_NOFILE, (high_write + 1, hard))
    pipes = _Pipes()
    read_fd, write_fd = os.pipe()
    try:
        os.dup2(read_fd, high_read)
        os.dup2(write_fd, high_write)
        client = _client_over(pipes)
        start = time.monotonic()
        assert client._wait(high_read, start + 0.2) is False
        assert time.monotonic() - start >= 0.15
        assert client._wait(high_write, time.monotonic() + 5.0, write=True) is True
        os.write(high_write, b"x")
        assert client._wait(high_read, time.monotonic() + 5.0) is True
    finally:
        for fd in (read_fd, write_fd, high_read, high_write):
            try:
                os.close(fd)
            except OSError:
                pass
        pipes.close()
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


# ── the class guard ───────────────────────────────────────────────────────


def _calls(node):
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]


def test_no_read_or_write_on_a_compiler_pipe_is_unbounded():
    """THE CLASS GUARD. Every byte between this host and a compiler moves
    through a call that cannot outlive a deadline or outgrow a cap: a
    descriptor read of a bounded size after a timed wait, a non-blocking
    descriptor write after a timed wait, or a text read with a size. A bare
    ``readline()``, ``read()``, ``readlines()``, ``communicate()``, a loop over
    a stream, a stream ``write()`` or a ``select`` or ``poll`` without a
    timeout is each a way back to F-11, so each fails here, with its line."""
    tree = ast.parse((PI_ROOT / "runtime_client.py").read_text())
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Attribute) \
                and node.iter.attr in {"stdin", "stdout", "stderr"}:
            problems.append(f"line {node.lineno}: a loop over a pipe reads whole "
                            f"lines, however long")
    for call in _calls(tree):
        owner, name = ast.unparse(call.func.value), call.func.attr
        where = f"line {call.lineno}: {owner}.{name}()"
        if name in {"readlines", "communicate"}:
            problems.append(f"{where} reads until the child is done")
        elif name in {"read", "readline"} and owner != "os" and not call.args:
            problems.append(f"{where} reads without a size")
        elif name == "write" and owner != "os":
            problems.append(f"{where} blocks while the compiler is not reading")
        elif owner == "select" and name == "select" and len(call.args) < 4:
            problems.append(f"{where} waits without a timeout")
    for function in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        # A poll object (select.poll()) waits forever when polled with no
        # timeout. Popen.poll() takes none and never blocks, so only names
        # bound to select.poll() are checked.
        pollers = {target.id for node in ast.walk(function) if isinstance(node, ast.Assign)
                   and isinstance(node.value, ast.Call)
                   and ast.unparse(node.value.func) == "select.poll"
                   for target in node.targets if isinstance(target, ast.Name)}
        problems += [f"line {c.lineno}: {ast.unparse(c.func)}() waits without a timeout"
                     for c in _calls(function) if c.func.attr == "poll"
                     and ast.unparse(c.func.value) in pollers and not c.args]
        names = {(ast.unparse(c.func.value), c.func.attr) for c in _calls(function)}
        waits = ("self", "_wait") in names or ("select", "select") in names
        if ("os", "read") in names or ("os", "write") in names:
            if not waits:
                problems.append(f"{function.name}: descriptor I/O without a timed wait")
        if ("os", "write") in names and not any(
                ast.unparse(c.func) == "os.set_blocking"
                and isinstance(c.args[-1], ast.Constant) and c.args[-1].value is False
                for c in _calls(function)):
            problems.append(f"{function.name}: os.write on a blocking descriptor")
    assert not problems, "unbounded I/O on a compiler pipe:\n  " + "\n  ".join(problems)
