"""The agent's per-session lock table is bounded.

`PiAgent` runs the turns of one session one at a time: each turn takes the
session's lock, which it looks up in a table keyed by session id
(`PiAgent._locks`). The table only ever grew. Found by the 2026-11-10
product audit (F-22), whose reproduction drove 300 sessions through the
product API and counted 300 entries, then deleted all 300 sessions and
counted 300 again. The product's own lock table is reference-counted to
avoid exactly this, and the product still calls the agent.

An entry now lives only while a turn holds its lock or waits on it. Pinned
here, against the real `PiAgent` with the model call replaced (the lock
table is under test, so no toolchain is needed):

- sessions that have come and gone leave nothing behind, however their
  turns ended: with a reply, with an error, abandoned, interrupted, or
  having given up waiting;
- an entry in use is never dropped: the turns of one session still run one
  at a time while other sessions arrive by the thousand; a turn that gives
  up waiting takes only its own claim with it; and a turn that has asked for
  its session's lock and not yet taken it shares that lock with a turn that
  arrives in between;
- the property test walks arbitrary arrivals and endings on a few sessions
  and checks after every step that the table holds exactly the sessions in
  use and that no session has two turns in the model call;
- nothing else the agent holds in memory grows with the sessions it has
  served, so the next table like this one fails here;
- threads left to themselves, with nothing held, never run two turns of one
  session together and leave the table empty;
- by syntax: only `_session_lock` and `_session_lock_done` touch the table,
  each wholly under the table's mutex, and every `_session_lock` is followed
  at once by a `try` whose `finally` is its `_session_lock_done`, so no way
  out of a turn keeps a claim.
"""
import ast
import re
import sys
import threading
import time

import pytest
from hypothesis import given, settings, strategies as st

from conftest import PI_ROOT

from agent import PiAgent, TurnAbandoned

# How long a test waits for a turn to get somewhere. Every wait returns as
# soon as its condition holds, so this is reached only when something broke.
PATIENCE_S = 30.0


class MemoryStore:
    """Session histories in memory. The lock table is under test and these
    tests run thousands of turns; a synced file per turn would only slow
    them. Load and save are still two steps with the turn between them, so
    two turns that ran together on one session would lose one's messages
    here exactly as they would on disk."""

    def __init__(self):
        self.histories = {}

    def load(self, session_id):
        return list(self.histories.get(session_id, ()))

    def save(self, session_id, messages):
        self.histories[session_id] = list(messages)


class WatchedLock:
    """A session's lock as `turn_with_usage` was handed it. It tells the
    harness that the turn has reached it, which may stop the turn there, and
    what the turn then found, the lock taken or free. Otherwise it is the
    lock.

    A turn the test said `will_not_wait` has its wait at a taken lock cut to
    a millisecond: its deadline passes while it waits, without the test
    depending on the clock."""

    def __init__(self, lock, at_lock):
        self._lock = lock
        self._at_lock = at_lock

    def acquire(self, *args, **kwargs):
        if self._at_lock(self._lock):
            return self._lock.acquire(timeout=0.001)
        return self._lock.acquire(*args, **kwargs)

    def release(self):
        self._lock.release()


class Running:
    """One turn on a thread of its own: its reply, or the error that ended it."""

    def __init__(self):
        self.reply = None
        self.error = None
        self.finished = False
        self.thread = None


class Harness:
    """The real PiAgent, with a model call a test can hold open and session
    locks it can watch.

    A real turn spends its seconds inside the model call with its session's
    lock taken, so that is where `hold` keeps a turn. `stop_short` keeps one
    a step earlier: it has asked the table for its session's lock and has
    not tried to take it, as a thread is when it loses the processor between
    the two. `inside` says which turns are in the model call now, `found`
    what each turn met when it went to take its session's lock ("free", or
    "taken" by a turn still using it), and `together` every time two turns
    of one session were in the call at once. A turn is named by its message,
    which each test keeps unique."""

    def __init__(self, root):
        (root / "sandboxes").mkdir(exist_ok=True)
        self.store = MemoryStore()
        self.agent = PiAgent("http://127.0.0.1:9/v1/messages", "key-unused",
                             store=self.store, sandbox_root=root / "sandboxes",
                             mcp=None)
        self._state = threading.Condition()
        self._held = set()         # messages whose model call does not return yet
        self._failures = {}        # message -> what its model call raises
        self._impatient = set()    # messages whose wait at a taken lock runs out
        self._short = set()        # messages stopped short of taking their lock
        self._reached = set()      # messages that have reached their lock
        self._inside = {}          # session -> messages in the model call now
        self._on_thread = threading.local()
        self._stop_arrivals = False
        self.arrived = 0           # other sessions that have come and gone
        self.found = {}
        self.together = []
        self.order = []            # (session, message), as the model calls began
        self.threads = []
        self.agent._llm = self._model
        self.agent._parse = lambda raw, session=None: [("text", f"re: {raw}")]
        table_lookup = self.agent._session_lock
        self.agent._session_lock = lambda session_id: WatchedLock(
            table_lookup(session_id), self._at_lock)

    # ── what a test arranges ────────────────────────────────────────────

    def hold(self, *messages):
        with self._state:
            self._held.update(messages)

    def let_go(self, *messages):
        with self._state:
            self._held.difference_update(messages)
            self._state.notify_all()

    def fail(self, message, error):
        self._failures[message] = error

    def will_not_wait(self, message):
        self._impatient.add(message)

    def stop_short(self, message):
        with self._state:
            self._short.add(message)

    def go_on(self, message):
        with self._state:
            self._short.discard(message)
            self._state.notify_all()

    # ── what the agent calls ────────────────────────────────────────────

    def _model(self, payload, session=None):
        message = payload["messages"][-1]["content"]
        with self._state:
            inside = self._inside.setdefault(session, [])
            inside.append(message)
            self.order.append((session, message))
            if len(inside) > 1:
                self.together.append((session, tuple(inside)))
            self._state.notify_all()
            while message in self._held:
                self._state.wait()
        # The call takes a moment, and not under the harness's own lock: two
        # turns of one session that ran together would both be inside now.
        time.sleep(0)
        with self._state:
            inside.remove(message)
            self._state.notify_all()
        if message in self._failures:
            raise self._failures[message]
        return message

    def _at_lock(self, lock):
        """A turn is about to take its session's `lock`. True when its wait
        there is to run out."""
        message = self._on_thread.message
        with self._state:
            self._reached.add(message)
            self._state.notify_all()
            while message in self._short:
                self._state.wait()
            taken = lock.locked()
            self.found[message] = "taken" if taken else "free"
            self._state.notify_all()
        return taken and message in self._impatient

    # ── running turns ───────────────────────────────────────────────────

    def turn(self, session, message, **options):
        """One turn on the calling thread; its reply."""
        self._on_thread.message = message
        reply, _usage = self.agent.turn_with_usage(session, message, **options)
        return reply

    def _thread(self, work):
        running = Running()

        def run():
            try:
                running.reply = work()
            except BaseException as error:  # noqa: BLE001 - the test judges it
                running.error = error
            finally:
                with self._state:
                    running.finished = True
                    self._state.notify_all()

        running.thread = threading.Thread(target=run, daemon=True)
        self.threads.append(running)
        running.thread.start()
        return running

    def start(self, session, message, **options):
        """One turn on a thread of its own."""
        return self._thread(lambda: self.turn(session, message, **options))

    def start_arrivals(self):
        """Other sessions start arriving, one after another without a pause:
        each has one whole turn and is never heard from again."""
        def arrive():
            while not self._stop_arrivals:
                session = f"other-{self.arrived}"
                message = f"hello from {session}"
                assert self.turn(session, message) == f"re: {message}"
                with self._state:
                    assert self.found.pop(message) == "free"
                    self.arrived += 1
                    self._state.notify_all()

        self._arrivals = self._thread(arrive)

    def stop_arrivals(self):
        """Stop them; how many sessions came and went."""
        self._stop_arrivals = True
        self.wait_finished(self._arrivals)
        if self._arrivals.error is not None:
            raise self._arrivals.error
        return self.arrived

    # ── what a test waits for and sees ──────────────────────────────────

    def _wait(self, what, ready):
        with self._state:
            if not self._state.wait_for(ready, timeout=PATIENCE_S):
                inside = {s: m for s, m in self._inside.items() if m}
                raise AssertionError(
                    f"waited {PATIENCE_S:.0f}s for {what}. In the model call: "
                    f"{inside}; found at their locks: {str(self.found)[:400]}")

    def wait_inside(self, session, message=None):
        """Until `message`, or unnamed any turn of the session, is in the
        model call."""
        if message is None:
            self._wait(f"a turn of {session!r} to reach the model call",
                       lambda: bool(self._inside.get(session)))
        else:
            self._wait(f"{message!r} to reach the model call",
                       lambda: message in self._inside.get(session, ()))

    def wait_at_lock(self, message):
        """Until `message` has asked for its session's lock and reached it."""
        self._wait(f"{message!r} to reach its session's lock",
                   lambda: message in self._reached)

    def wait_found(self, message):
        """Until `message` has gone to take its session's lock; what it found."""
        self._wait(f"{message!r} to go to take its session's lock",
                   lambda: message in self.found)
        return self.found[message]

    def wait_arrivals(self, more):
        """Until `more` other sessions have come and gone from now; the total."""
        target = self.arrived + more
        self._wait(f"{more} more sessions to come and go",
                   lambda: self.arrived >= target or self._arrivals.finished)
        if self._arrivals.error is not None:
            raise self._arrivals.error
        return self.arrived

    def wait_finished(self, *threads):
        for running in threads:
            self._wait("a turn to end", lambda: running.finished)
            running.thread.join(PATIENCE_S)

    def inside(self, session):
        with self._state:
            return list(self._inside.get(session, ()))

    def table(self):
        """The session ids in the agent's lock table, as it stands."""
        with self.agent._locks_guard:
            return sorted(self.agent._locks)

    def close(self):
        """Let every turn go and wait for the threads, however the test ended."""
        with self._state:
            self._held.clear()
            self._short.clear()
            self._stop_arrivals = True
            self._state.notify_all()
        for running in self.threads:
            running.thread.join(5)


@pytest.fixture()
def harness(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.close()


# ── sessions that have come and gone ────────────────────────────────────

def test_sessions_that_have_come_and_gone_leave_no_entry_behind(harness):
    """The audit's measurement, taken at the agent: 300 turns on 300 sessions
    left 300 entries, for as long as the process lived."""
    for n in range(300):
        assert harness.turn(f"s{n}", "hello") == "re: hello"
    left = harness.table()
    assert left == [], (
        f"300 sessions each had one turn and none is running, yet the agent's "
        f"session-lock table holds {len(left)} entries: an entry must go when "
        f"no turn holds or waits on its lock")


def containers(owner, part=""):
    """The sizes of the dicts, lists, sets and tuples `owner` holds, and of
    those held by the parts of it that agent.py defines (its audit log, and
    its store and memory when they are the agent's own classes)."""
    sizes = {}
    for name, value in vars(owner).items():
        if isinstance(value, (dict, list, set, frozenset, tuple)):
            sizes[part + name] = len(value)
        elif type(value).__module__ == PiAgent.__module__ and not part:
            sizes.update(containers(value, f"{name}."))
    return sizes


def test_nothing_the_agent_holds_grows_with_the_sessions_it_has_served(harness):
    """THE GUARD for the next table like this one: whatever the agent keeps
    in memory is the same size after a hundred more sessions have come and
    gone. A table keyed by session that is never pruned shows here whatever
    it is called."""
    harness.turn("the first session", "hello")
    before = containers(harness.agent)
    assert "_locks" in before and len(before) > 5, before
    for n in range(100):
        harness.turn(f"s{n}", "hello")
    after = containers(harness.agent)
    grew = {name: f"{before.get(name)} -> {size}" for name, size in after.items()
            if size != before.get(name)}
    assert not grew, (
        f"100 sessions each had one turn and none is running, and what the "
        f"agent holds in memory changed size with them: {grew}")


def with_a_reply(harness):
    assert harness.turn("s", "hello") == "re: hello"


def with_a_failed_model_call(harness):
    harness.fail("hello", RuntimeError("the provider is down"))
    with pytest.raises(RuntimeError, match="the provider is down"):
        harness.turn("s", "hello")


def with_the_caller_gone(harness):
    with pytest.raises(TurnAbandoned):
        harness.turn("s", "hello", abort=lambda: True)


def with_a_tool_the_agent_does_not_have(harness):
    with pytest.raises(ValueError, match="unknown allowed tool"):
        harness.turn("s", "hello", allowed_tools=["no-such-tool"])


def with_an_interrupt(harness):
    harness.fail("hello", KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        harness.turn("s", "hello")


def with_its_deadline_already_passed(harness):
    with pytest.raises(TimeoutError, match="waiting for session"):
        harness.turn("s", "hello", deadline_monotonic=time.monotonic() - 1)


def with_a_deadline_that_is_not_a_time(harness):
    # fails on the way to the lock, after the table was asked for it
    with pytest.raises(TypeError):
        harness.turn("s", "hello", deadline_monotonic="soon")


def with_a_deadline_too_far_off_to_wait_for(harness):
    # the lock itself refuses a wait this long
    with pytest.raises(OverflowError):
        harness.turn("s", "hello", deadline_monotonic=float("inf"))


ENDINGS = [with_a_reply, with_a_failed_model_call, with_the_caller_gone,
           with_a_tool_the_agent_does_not_have, with_an_interrupt,
           with_its_deadline_already_passed, with_a_deadline_that_is_not_a_time,
           with_a_deadline_too_far_off_to_wait_for]


@pytest.mark.parametrize("ending", ENDINGS, ids=lambda ending: ending.__name__)
def test_a_turn_leaves_no_entry_behind_however_it_ends(harness, ending):
    """Every way out of a turn gives up the turn's claim on its session's
    entry, the ones that never reach the lock included."""
    ending(harness)
    left = harness.table()
    assert left == [], (
        f"a turn that ended {ending.__name__.replace('_', ' ')} left its "
        f"session in the agent's session-lock table with no turn running: {left}")


# ── an entry in use is never dropped ────────────────────────────────────

def test_one_sessions_turns_run_one_at_a_time_while_other_sessions_keep_arriving(
        harness):
    """Two threads on one session, a third later, while other sessions come
    and go without a pause. The table stays at its bound, the sessions in
    use, and the busy session's entry is never one of those let go: a turn
    that found a fresh lock there would run beside the turn holding the old
    one, and the two would overwrite each other's history."""
    harness.hold("first", "second", "third")
    harness.start_arrivals()

    def only_the_sessions_in_use_are_kept():
        arrived = harness.wait_arrivals(500)
        table = harness.table()
        assert "busy" in table, (
            f"{arrived} other sessions have come and gone while 'busy' had a "
            f"turn in flight, and the agent's session-lock table has no entry "
            f"for 'busy': it let go of an entry in use")
        others = [session for session in table if session != "busy"]
        assert len(others) <= 1, (
            f"{arrived} other sessions have come and gone while 'busy' had a "
            f"turn in flight, and the agent's session-lock table holds "
            f"{len(table)} entries: it must hold the sessions in use (here "
            f"'busy' and at most the one arriving) and no more")

    first = harness.start("busy", "first")
    harness.wait_inside("busy", "first")
    only_the_sessions_in_use_are_kept()

    second = harness.start("busy", "second")
    assert harness.wait_found("second") == "taken", (
        "the second turn found its session's lock free while the first was "
        "still in the model call: the table had let go of a lock that was held")
    only_the_sessions_in_use_are_kept()
    assert harness.inside("busy") == ["first"]

    harness.let_go("first")
    harness.wait_inside("busy", "second")
    harness.wait_finished(first)
    third = harness.start("busy", "third")
    assert harness.wait_found("third") == "taken", (
        "the third turn found its session's lock free while the second, which "
        "had waited for it, was in the model call: the table had let go of a "
        "lock that a turn was waiting on")
    only_the_sessions_in_use_are_kept()
    assert harness.inside("busy") == ["second"]

    harness.let_go("second")
    harness.wait_inside("busy", "third")
    harness.wait_finished(second)
    only_the_sessions_in_use_are_kept()
    harness.let_go("third")
    harness.wait_finished(third)
    arrived = harness.stop_arrivals()

    assert [turn.error for turn in (first, second, third)] == [None, None, None]
    assert [turn.reply for turn in (first, second, third)] == [
        "re: first", "re: second", "re: third"]
    assert harness.together == [], (
        f"two turns of one session were in the model call at once: "
        f"{harness.together}")
    assert [message for session, message in harness.order
            if session == "busy"] == ["first", "second", "third"]
    asked = [entry["content"] for entry in harness.store.histories["busy"]
             if entry["role"] == "user"]
    assert asked == ["first", "second", "third"], (
        f"the session's history lost a turn: {asked}")
    assert arrived >= 2000
    assert harness.table() == []


def test_a_turn_that_gives_up_waiting_takes_only_its_own_claim(harness):
    """A turn whose deadline passes while it waits for its session leaves
    the entry to the turn that holds the lock, and the entry goes when that
    turn and every later one are done."""
    harness.hold("first", "third")
    first = harness.start("busy", "first")
    harness.wait_inside("busy", "first")

    harness.will_not_wait("second")
    with pytest.raises(TimeoutError, match="waiting for session"):
        harness.turn("busy", "second", deadline_monotonic=time.monotonic() + 3600)
    assert harness.found["second"] == "taken"
    assert harness.table() == ["busy"], (
        "a turn that gave up waiting took the session's entry with it while "
        "another turn held the lock")

    third = harness.start("busy", "third")
    assert harness.wait_found("third") == "taken", (
        "after a turn gave up waiting, the next turn found its session's lock "
        "free while the first was still in the model call")
    assert harness.inside("busy") == ["first"]

    harness.let_go("first")
    harness.wait_inside("busy", "third")
    harness.wait_finished(first)
    assert harness.table() == ["busy"]
    harness.let_go("third")
    harness.wait_finished(third)

    assert (first.reply, third.reply) == ("re: first", "re: third")
    assert harness.together == []
    left = harness.table()
    assert left == [], (
        f"every turn of the session has ended, one of them by giving up its "
        f"wait, and the agent's session-lock table still holds {left}")


def test_a_turn_that_has_asked_for_its_lock_and_not_yet_taken_it_keeps_its_entry(
        harness):
    """Between asking the table for its session's lock and taking it, a turn
    has a claim on the entry and nothing else: the lock is free. A turn that
    arrives then is handed the same lock, so the first one waits for it when
    it goes on, and the entry stays until both are done."""
    harness.hold("early", "late")
    harness.stop_short("early")
    early = harness.start("busy", "early")
    harness.wait_at_lock("early")
    assert harness.table() == ["busy"], (
        "a turn has asked for its session's lock and the table has no entry "
        "for the session")

    late = harness.start("busy", "late")
    assert harness.wait_found("late") == "free"
    harness.wait_inside("busy", "late")
    harness.go_on("early")
    assert harness.wait_found("early") == "taken", (
        "a turn that had asked for its session's lock before another arrived "
        "found it free while the other was in the model call: the two were "
        "handed different locks")
    assert harness.inside("busy") == ["late"]

    harness.let_go("late")
    harness.wait_inside("busy", "early")
    harness.wait_finished(late)
    assert harness.table() == ["busy"]
    harness.let_go("early")
    harness.wait_finished(early)

    assert (early.reply, late.reply) == ("re: early", "re: late")
    assert harness.together == []
    left = harness.table()
    assert left == [], (
        f"both turns of the session have ended and the agent's session-lock "
        f"table still holds {left}")


def test_threads_left_to_themselves_never_run_two_turns_of_one_session_together(
        harness):
    """Nothing is held here: eight threads run turns on three sessions as
    fast as they can, with the interpreter switching between them as often
    as it will, so arrivals land wherever they land in a turn that is
    ending. Some turns fail, some will not wait long, some arrive with
    their deadline passed. No session ever has two turns in the model call,
    each session's history is exactly the turns that got its lock, in that
    order, and when the threads are done the table is empty."""
    sessions, threads, turns_each = ("a", "b", "c"), 8, 120
    harness.agent.max_history_bytes = 10 ** 9   # nothing is cut from a history
    kinds = ("replies", "replies", "fails", "will not wait long", "is too late")
    outcomes = {kind: [] for kind in kinds}

    def work(thread):
        for n in range(turns_each):
            session = sessions[(thread + n * (thread + 1)) % len(sessions)]
            kind = kinds[(thread * 7 + n) % len(kinds)]
            message = f"{thread}-{n} {kind}"
            options = {}
            if kind == "fails":
                harness.fail(message, RuntimeError("the provider is down"))
            if kind == "will not wait long":
                options["deadline_monotonic"] = time.monotonic() + 0.0002
            if kind == "is too late":
                options["deadline_monotonic"] = time.monotonic() - 1
            try:
                outcomes[kind].append(harness.turn(session, message, **options))
            except (TimeoutError, RuntimeError) as error:
                outcomes[kind].append(type(error))

    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-5)
    try:
        workers = [harness._thread(lambda thread=thread: work(thread))
                   for thread in range(threads)]
        harness.wait_finished(*workers)
    finally:
        sys.setswitchinterval(interval)

    assert [worker.error for worker in workers] == [None] * threads
    assert set(outcomes["fails"]) == {RuntimeError}
    assert set(outcomes["is too late"]) == {TimeoutError}
    assert all(outcome is TimeoutError or outcome.startswith("re: ")
               for kind in ("replies", "will not wait long")
               for outcome in outcomes[kind])
    assert sum(len(seen) for seen in outcomes.values()) == threads * turns_each
    assert harness.together == [], (
        f"two turns of one session were in the model call at once, "
        f"{len(harness.together)} time(s): {harness.together[:3]}")
    for session in sessions:
        got_the_lock = [message for owner, message in harness.order
                        if owner == session]
        saved = [entry["content"] for entry in harness.store.histories[session]
                 if entry["role"] == "user"]
        assert len(got_the_lock) > turns_each
        assert saved == got_the_lock, (
            f"session {session!r}: {len(got_the_lock)} turns got its lock and "
            f"its history holds {len(saved)} of their messages, or holds "
            f"them in another order: turns ran together and overwrote each "
            f"other")
    left = harness.table()
    assert left == [], (
        f"{threads * turns_each} turns on {len(sessions)} sessions have all "
        f"ended and the agent's session-lock table still holds {left}")


# ── the guard: exactly the sessions in use, whatever arrives and ends ───

SESSIONS = ("a", "b", "c")
ARRIVES = "a turn arrives"
WILL_NOT_WAIT = "a turn arrives that will not wait"
STOPS_SHORT = "a turn arrives and stops short of taking its lock"
GOES_ON = "a turn that stopped short goes on"
REPLY = "the turn in the model call gets its reply"
FAILS = "the turn in the model call fails"
STEPS = st.lists(
    st.tuples(st.sampled_from((ARRIVES, ARRIVES, WILL_NOT_WAIT, STOPS_SHORT,
                               GOES_ON, GOES_ON, REPLY, FAILS)),
              st.sampled_from(SESSIONS)),
    max_size=20)


class InFlight:
    """What the turns of one session are doing, as the test expects it: one
    may hold the lock, in the model call; others wait for the lock; others
    have asked for it and stopped short of taking it."""

    def __init__(self):
        self.holder = None
        self.waiting = []
        self.short = []

    def __bool__(self):
        return bool(self.holder or self.waiting or self.short)


class Walk:
    """One run of the property: the harness, what the test expects of each
    session, and the steps that move both."""

    def __init__(self, root):
        self.harness = Harness(root)
        self.sessions = {session: InFlight() for session in SESSIONS}
        self.turns = {}  # message -> Running

    def arrive(self, session, message, kind):
        harness, expected = self.harness, self.sessions[session]
        harness.hold(message)
        options = {}
        if kind == WILL_NOT_WAIT:
            harness.will_not_wait(message)
            options["deadline_monotonic"] = time.monotonic() + 3600
        if kind == STOPS_SHORT:
            harness.stop_short(message)
        self.turns[message] = harness.start(session, message, **options)
        if kind == STOPS_SHORT:
            harness.wait_at_lock(message)
            expected.short.append(message)
        else:
            self.takes_or_waits(session, message, will_not_wait=kind == WILL_NOT_WAIT)

    def go_on(self, session):
        expected = self.sessions[session]
        if expected.short:
            message = expected.short.pop(0)
            self.harness.go_on(message)
            self.takes_or_waits(session, message)

    def takes_or_waits(self, session, message, will_not_wait=False):
        """`message` goes to take its session's lock: it finds it taken
        exactly when another turn of the session holds it."""
        harness, expected = self.harness, self.sessions[session]
        found = harness.wait_found(message)
        held_by = (f"the turn {expected.holder!r} holds" if expected.holder
                   else "no turn holds")
        assert found == ("taken" if expected.holder else "free"), (
            f"{message!r} went to take the lock of {session!r}, which "
            f"{held_by}, and found it {found}")
        if not expected.holder:
            harness.wait_inside(session, message)
            expected.holder = message
        elif will_not_wait:
            gone = self.turns.pop(message)
            harness.wait_finished(gone)
            assert isinstance(gone.error, TimeoutError), gone.error
        else:
            expected.waiting.append(message)

    def end(self, session, failing=False):
        harness, expected = self.harness, self.sessions[session]
        if not expected.holder:
            return
        message, expected.holder = expected.holder, None
        if failing:
            harness.fail(message, RuntimeError("the provider is down"))
        harness.let_go(message)
        ended = self.turns.pop(message)
        harness.wait_finished(ended)
        if failing:
            assert isinstance(ended.error, RuntimeError), ended.error
        else:
            assert (ended.reply, ended.error) == (f"re: {message}", None)
        if expected.waiting:
            # one of those that waited has the lock now; see it there before
            # anything else arrives
            harness.wait_inside(session)
            inside = harness.inside(session)
            assert len(inside) == 1 and inside[0] in expected.waiting, (
                f"{message!r} ended with {expected.waiting} waiting for the "
                f"lock of {session!r}, and {inside} went into the model call")
            expected.holder = inside[0]
            expected.waiting.remove(expected.holder)

    def check(self, after):
        harness = self.harness
        in_use = sorted(session for session, expected in self.sessions.items()
                        if expected)
        table = harness.table()
        assert table == in_use, (
            f"after {after}: the sessions with a turn holding, waiting on or "
            f"asking for their lock are {in_use}, and the agent's "
            f"session-lock table holds {table}")
        for session, expected in self.sessions.items():
            inside = harness.inside(session)
            assert inside == ([expected.holder] if expected.holder else []), (
                f"after {after}: session {session!r} has {inside} in the model "
                f"call; expected {expected.holder!r}, with "
                f"{expected.waiting} waiting and {expected.short} stopped short")
        assert harness.together == [], (
            f"after {after}: two turns of one session were in the model call "
            f"at once: {harness.together}")


@settings(max_examples=100, deadline=None)
@given(steps=STEPS)
def test_the_table_holds_exactly_the_sessions_in_use_whatever_arrives_and_ends(
        tmp_path_factory, steps):
    """THE GUARD for the class: a table keyed by session that only grows, or
    one that lets go of an entry still in use. Turns arrive on three
    sessions and end in any order; some will not wait for a busy session,
    some fail, some stop between asking for their lock and taking it. After
    every step the table holds exactly the sessions that have a turn
    holding, waiting on or asking for their lock, the turn that holds each
    lock is the one turn of its session in the model call, and no two ever
    were. A turn that goes to take its session's lock finds it taken exactly
    when another turn of the session holds it."""
    walk = Walk(tmp_path_factory.mktemp("turns"))
    try:
        for number, (step, session) in enumerate(steps):
            if step in (ARRIVES, WILL_NOT_WAIT, STOPS_SHORT):
                walk.arrive(session, f"{session}{number}", step)
            elif step == GOES_ON:
                walk.go_on(session)
            else:
                walk.end(session, failing=step == FAILS)
            walk.check(f"step {number} ({step} on {session!r})")
        for session, expected in walk.sessions.items():
            while expected:
                if expected.short:
                    walk.go_on(session)
                else:
                    walk.end(session)
                walk.check(f"ending every turn of {session!r}")
        assert walk.turns == {}
    finally:
        walk.harness.close()


# ── by syntax ───────────────────────────────────────────────────────────

def _is_call_of(node, method):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == method
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "self")


def _uses_of_the_table(tree):
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "_locks"]


def test_by_syntax_every_claim_on_a_session_lock_is_given_up_on_every_way_out():
    """Every `_session_lock` is assigned and followed AT ONCE by a `try`
    whose `finally` is `_session_lock_done` for the same session and nothing
    else: no statement sits between them to raise with the claim taken, and
    the lock is taken and released inside that `try`, so the claim outlives
    the hold. The table is touched in those two functions and nowhere else,
    so nothing can add an entry without a claim or drop one that has
    claims, and each of the two is one block under the table's mutex, so a
    claim is never counted halfway."""
    tree = ast.parse((PI_ROOT / "agent.py").read_text())

    paired = 0
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            statements = getattr(node, field, None)
            if not isinstance(statements, list):
                continue
            for statement, following in zip(statements, statements[1:] + [None]):
                if not (isinstance(statement, ast.Assign)
                        and _is_call_of(statement.value, "_session_lock")):
                    continue
                where = f"agent.py:{statement.lineno}"
                assert (len(statement.targets) == 1
                        and isinstance(statement.targets[0], ast.Name)), \
                    f"{where}: the session's lock must be bound to one plain name"
                lock = statement.targets[0].id
                assert isinstance(following, ast.Try), (
                    f"{where}: `_session_lock` must be followed at once by a "
                    f"`try` that gives the claim up; without one the entry "
                    f"stays in the table for good, and a statement between "
                    f"the two can raise with the claim taken")
                assert (len(following.finalbody) == 1
                        and isinstance(following.finalbody[0], ast.Expr)
                        and _is_call_of(following.finalbody[0].value,
                                        "_session_lock_done")), (
                    f"{where}: the `finally` of the `try` that follows "
                    f"`_session_lock` must be the `_session_lock_done` call "
                    f"and nothing else")
                taken_for, given_up_for = statement.value, following.finalbody[0].value
                assert ([ast.dump(arg) for arg in taken_for.args]
                        == [ast.dump(arg) for arg in given_up_for.args]
                        and len(taken_for.args) == 1
                        and not taken_for.keywords and not given_up_for.keywords), (
                    f"{where}: the claim is not given up for the session it "
                    f"was taken for")
                used = {call.func.attr for part in following.body
                        for call in ast.walk(part)
                        if isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and isinstance(call.func.value, ast.Name)
                        and call.func.value.id == lock}
                assert {"acquire", "release"} <= used, (
                    f"{where}: the lock must be taken and released inside the "
                    f"`try`, before the claim on its entry is given up")
                paired += 1

    def calls(method):
        return sum(1 for node in ast.walk(tree) if _is_call_of(node, method))

    assert paired >= 1 and calls("_session_lock") == paired, (
        f"agent.py calls `_session_lock` {calls('_session_lock')} time(s) and "
        f"{paired} of them are paired with a `_session_lock_done`")
    assert calls("_session_lock_done") == paired, (
        f"`_session_lock_done` is called {calls('_session_lock_done')} "
        f"time(s) for {paired} claim(s): a claim given up twice drops an "
        f"entry that is still in use")

    agent = next(node for node in tree.body
                 if isinstance(node, ast.ClassDef) and node.name == "PiAgent")
    methods = {node.name: node for node in agent.body
               if isinstance(node, ast.FunctionDef)}
    owners = ["__init__", "_session_lock", "_session_lock_done"]
    touching = sorted(name for name, method in methods.items()
                      if _uses_of_the_table(method))
    assert touching == owners, (
        f"PiAgent._locks is touched by {touching}; only {owners} may, or an "
        f"entry could be added uncounted or dropped while in use")
    assert len(_uses_of_the_table(tree)) == sum(
        len(_uses_of_the_table(methods[name])) for name in owners), \
        "something outside PiAgent reaches into a `_locks` table in agent.py"
    for name in owners[1:]:
        body = methods[name].body
        if ast.get_docstring(methods[name]) is not None:
            body = body[1:]
        under_the_mutex = (
            len(body) == 1 and isinstance(body[0], ast.With)
            and len(body[0].items) == 1
            and isinstance(body[0].items[0].context_expr, ast.Attribute)
            and body[0].items[0].context_expr.attr == "_locks_guard")
        assert under_the_mutex, (
            f"PiAgent.{name} must be one `with self._locks_guard:` block and "
            f"nothing else: a claim counted or an entry removed outside the "
            f"mutex can meet another thread halfway, and an entry goes while "
            f"a turn has just claimed it")

    for path in sorted([*PI_ROOT.glob("*.py"), *(PI_ROOT / "scripts").glob("*.py")]):
        if path.name == "agent.py":
            continue
        reached = re.findall(
            r"\b_session_lock(?:_done)?\b|\bagent\._locks(?:_guard)?\b",
            path.read_text())
        assert not reached, (
            f"{path.relative_to(PI_ROOT)} reaches into the agent's session-lock "
            f"table ({sorted(set(reached))}); a turn takes its session's lock "
            f"through PiAgent.turn_with_usage")
