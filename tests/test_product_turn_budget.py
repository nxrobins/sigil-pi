"""What a turn may spend on the product host, and how much of its day a tenant can use.

Audit 2026-11-10, two findings that meet in ``product_main.py``.

F-21, "Only 62% of the daily token quota is usable at the defaults".
Admission refuses a turn when the tokens a tenant has used, the reservations
of its turns in flight and one more reservation exceed ``PI_TOKENS_PER_DAY``.
The defaults were 10,000,000 tokens a day and 4,000,000 reserved for every
turn, so a tenant was refused once it had used about 6,000,000, for turns
that spend less than 1,000,000 at the default bounds.

F-10, the product's half. The agent's default output cap became 16,000 tokens
and it sets ``thinking`` on every request, but the product host kept its own
default of 1,024 and read no thinking setting: a default deployment still
sent ``max_tokens`` 1,024, and one whose model rejects ``adaptive`` thinking
failed every turn with no way to turn it off.

These tests start ``product_main.run()`` the way an operator starts it, with
every setting left to its default unless the test names it, and keep the REAL
``PiAgent`` that startup builds. Only what a test must not start is a double:
the compiler process, the listener and the background loops. The provider is
a script that answers the requests the agent builds; nothing leaves the
process.

- A tenant can spend at least 90% of its daily limit before it is refused.
- The default reservation is sized to the turn the default bounds allow, in
  which every step sends a transcript as large as ``PI_MAX_HISTORY_BYTES``
  permits and receives a reply as long as ``PI_MAX_TOKENS`` permits: it
  covers that turn, so the turn cannot take a tenant past its limit, and it
  is not several times larger.
- Admission keeps its rule (used + reserved + one more reservation must fit
  in the limit), so a day's total exceeds the limit only by what the turns in
  flight spent above their reservations.
- A request that never reached the agent is charged nothing.
- A default deployment caps a reply at the agent's default, ``PI_THINKING``
  reaches every request and the effective settings, and the product passes
  every setting the agent takes or says why not.
"""

import ast
import copy
import hashlib
import inspect
import json
import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given, settings, strategies as st

from conftest import PI_ROOT

import product_main
from agent import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_THINKING,
    THINKING_MODES,
    PiAgent,
    SessionStore,
    compact,
    history_bytes,
)
from product_service import (
    ConfigError,
    DurableQuotaStore,
    ProductError,
    SessionOperationLocks,
    _internal_session,
)

TENANT = "tenant-a"
CHAT_TOKEN = "p3-chat-token-with-test-only-entropy-0123456789abcdef"
OPS_TOKEN = "p3-operations-token-with-test-only-entropy-0123456789"
# What the compiler double returns for a tool: more than one tool result may
# keep, so every tool step adds a full, clipped result to the transcript.
TOOL_OUTPUT = "0123456789abcdef" * 4096


def _tokens(byte_count):
    """Bytes of request as tokens, at the provider's figure for the tokenizer
    the default model uses: "1M tokens is roughly 555k words or 2.5M Unicode
    characters on the current tokenizer" (models overview, read 2026-11-10).
    A byte is at most one character, so text of that density is not
    undercounted."""
    return -(-byte_count * 2 // 5)


# ── starting the product ─────────────────────────────────────────────────


def _prepare(tmp_path, monkeypatch, env=()):
    """What an operator provides, and doubles for what a test must not
    start. Only the variables in ``env`` are set beyond the four a product
    cannot start without. Returns what the doubles saw."""
    seen = SimpleNamespace(compilers=0, during=lambda service: None, result=None)
    for name in [name for name in os.environ if name.startswith("PI_")]:
        monkeypatch.delenv(name)        # "default" means the operator set nothing
    now = int(time.time())
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": [
        {"sha256": hashlib.sha256(token.encode()).hexdigest(),
         "principal": principal, "tenant": tenant, "scopes": scopes, "tools": tools,
         "not_before_unix": now - 60, "expires_unix": now + 86400}
        for token, principal, tenant, scopes, tools in (
            (CHAT_TOKEN, "user", TENANT, ["chat"], ["*"]),
            (OPS_TOKEN, "operator", "operations", ["ops:read"], []))]}))
    monkeypatch.setenv(
        "ANTHROPIC_API_KEY", "provider-key-for-a-test-that-never-calls-the-provider")
    monkeypatch.setenv("PI_AUDIT_KEY", "audit-key-for-the-turn-budget-tests-0123456789")
    monkeypatch.setenv("PI_AUTH_FILE", str(auth))
    monkeypatch.setenv("PI_STATE", str(tmp_path / "state"))
    for name, value in dict(env).items():
        monkeypatch.setenv(name, value)

    class Compiler:
        is_healthy = True
        _proc = SimpleNamespace(pid=os.getpid(), kill=lambda: None)

        def initialize(self, timeout_s=None):
            return {}

        def close(self):
            pass

        def forge(self, source, *, input="", fuel=0, grants=None, timeout_s=None):
            return {"status": "ok", "data": {"output_text": TOOL_OUTPUT}}

    def spawn(path, timeout_s=90.0, generation=0):
        seen.compilers += 1
        return Compiler()

    class Stop:
        """The operator's signal, arriving as soon as the service is up."""

        def wait(self, timeout=None):
            return True

        def set(self):
            pass

    class Idle:
        """The scheduler and the retention sweep, with nothing to do."""

        def __init__(self, *args, **kwargs):
            pass

        def tick(self):
            return True

        def start(self):
            pass

        def stop(self, timeout_s):
            return True

        @staticmethod
        def is_healthy():
            return True

        @staticmethod
        def snapshot():
            return {"healthy": True}

    class Listener:
        def shutdown(self):
            pass

        def server_close(self):
            pass

    def serve(service, **kwargs):
        seen.result = seen.during(service)
        return Listener()

    monkeypatch.setattr(
        product_main.toolchain, "resolve",
        lambda require=True: SimpleNamespace(
            forge_bin=Path("/nonexistent/sigil-mcp"), origin="test double"))
    monkeypatch.setattr(product_main.toolchain, "verify_binary", lambda path, rev=None: None)
    monkeypatch.setattr(product_main, "threading", SimpleNamespace(Event=Stop))
    monkeypatch.setattr(product_main, "ProductionSigilMCP", SimpleNamespace(spawn=spawn))
    monkeypatch.setattr(product_main, "ProductScheduler", Idle)
    monkeypatch.setattr(product_main, "ProductRetentionMonitor", Idle)
    monkeypatch.setattr(product_main, "serve_product", serve)
    return seen


def _boot(tmp_path, monkeypatch, during, env=()):
    """Run the product until it serves, call ``during(service)`` where the
    listener would take requests, then let it drain and stop."""
    seen = _prepare(tmp_path, monkeypatch, env)
    seen.during = during
    product_main.run()
    return seen.result


class Provider:
    """The model, scripted. It takes the place of the agent's two guests, so
    it sees each request exactly as the agent built it, and ``answer(request,
    step)`` returns the frames a reply would decode to."""

    def __init__(self, agent, answer):
        self.requests = []
        self._answer = answer
        agent._llm = self._llm
        agent._parse = self._parse

    def _llm(self, payload, session=None):
        request = copy.deepcopy(payload)    # the agent goes on appending to its list
        self.requests.append(request)
        return request

    def _parse(self, request, session=None):
        return self._answer(request, len(self.requests))


def _short_answer(request, step):
    return [("text", "hello"), ("usage", 3, 2), ("stop", "end_turn")]


def _chat(service, message, session="work"):
    return service.dispatch(
        "POST", "/v1/chat", {"Authorization": f"Bearer {CHAT_TOKEN}"},
        json.dumps({"session": session, "message": message}).encode())


def _effective_settings(service):
    """As an operator reads them: ``GET /v1/metrics``, through JSON."""
    status, _, body = service.dispatch(
        "GET", "/v1/metrics", {"Authorization": f"Bearer {OPS_TOKEN}"})
    assert status == 200, body
    return json.loads(json.dumps(body))["metrics"]["effective_settings"]


# ── F-21: the token reservation and the daily limit ──────────────────────


def test_a_tenant_can_spend_nine_tenths_of_its_daily_tokens_at_the_defaults(
        tmp_path, monkeypatch):
    """The audit's reproduction (audit/repro/r6_token_budget.py) through the
    product: turns of 250,000 tokens, "a generous real turn", until admission
    refuses."""
    turn = 250_000

    def during(service):
        Provider(service.agent, lambda request, step: [
            ("text", "done"), ("usage", turn - 1000, 1000), ("stop", "end_turn")])
        quota = service.quota_store
        spent = 0
        while True:
            status, headers, body = _chat(service, "next")
            if status != 200:
                return (quota.tokens_per_day, spent, status, headers, body,
                        quota.tenant_usage(TENANT)["tokens_today"])
            spent += turn

    limit, spent, status, headers, body, metered = _boot(tmp_path, monkeypatch, during)
    assert (status, body["error"]["code"]) == (429, "token_quota_exceeded"), body
    assert int(headers["Retry-After"]) > 0
    assert metered == spent
    assert spent >= 0.9 * limit, (
        f"a tenant spending {turn:,} tokens a turn was refused after {spent:,} of its "
        f"{limit:,} daily tokens ({spent / limit:.0%}): less than 90% of the advertised "
        f"quota is usable at the defaults (audit F-21)")
    assert spent <= limit


def test_a_turn_is_still_admitted_with_nine_tenths_of_the_daily_limit_used(
        tmp_path, monkeypatch):
    """What "usable" means exactly: admission is open while a tenant's used
    tokens and one reservation fit in the limit, so the reservation is the
    part of a day that cannot be relied on. At the defaults that part is at
    most a tenth."""

    def during(service):
        quota = service.quota_store
        Provider(service.agent, _short_answer)
        quota.acquire_turn(TENANT).release(
            usage={"input_tokens": quota.tokens_per_day * 9 // 10, "output_tokens": 0})
        status, _, body = _chat(service, "hi")
        return quota.tokens_per_day, quota.token_reservation_per_turn, status, body

    limit, reservation, status, body = _boot(tmp_path, monkeypatch, during)
    assert status == 200, (
        f"with {limit * 9 // 10:,} of its {limit:,} daily tokens used a tenant is "
        f"refused ({status} {body['error']['code']}): the {reservation:,} reserved for "
        f"each turn leaves {1 - reservation / limit:.0%} of the limit usable (audit F-21)")


def _long_conversation(limit):
    """A session with a long past: short turns, more of them than a
    transcript may hold, cut by the agent's own rule to the most it keeps."""
    line = "The quick brown fox jumps over the lazy dog. " * 10
    turns = []
    for number in range(limit // 900):
        turns += [
            {"role": "user", "content": f"{number}: {line}"},
            {"role": "assistant",
             "content": [{"type": "text", "text": f"{number}: {line}"}]}]
    assert history_bytes(turns) > limit, "not long enough to need a cut"
    return compact(turns, limit)


@pytest.mark.parametrize("answers", [True, False],
                         ids=["answers-on-its-last-step", "never-answers"])
def test_the_default_reservation_is_sized_to_the_turn_the_default_bounds_allow(
        tmp_path, monkeypatch, answers):
    """One turn as large as the default bounds let it be, through the real
    loop: the session already holds a full transcript, every step sends as
    much of it as ``PI_MAX_HISTORY_BYTES`` keeps, with the tool definitions,
    and every reply is as long as ``PI_MAX_TOKENS`` allows and calls a tool.
    The turn either answers on the last of its ``PI_MAX_STEPS`` or never
    does, and the loop ends it; a failed turn is charged all the same. The
    scripted provider charges each request at the documented token density
    (``_tokens``). The tenant starts where admission is about to close."""

    def during(service):
        agent, quota = service.agent, service.quota_store
        limit, reservation = quota.tokens_per_day, quota.token_reservation_per_turn
        agent.store.save(_internal_session(TENANT, "work"),
                         _long_conversation(agent.max_history_bytes))
        quota.acquire_turn(TENANT).release(
            usage={"input_tokens": limit - reservation, "output_tokens": 0})

        def full_step(request, step):
            usage = ("usage",
                     _tokens(len(json.dumps(request, ensure_ascii=False).encode())),
                     request["max_tokens"])
            if answers and step == agent.max_steps:
                return [("text", "done"), usage, ("stop", "end_turn")]
            return [("tool_use", f"call-{step}", "read_file", {"path": "notes.txt"}),
                    usage, ("stop", "tool_use")]

        provider = Provider(agent, full_step)
        status, _, body = _chat(service, "go")
        return SimpleNamespace(
            limit=limit, reservation=reservation, status=status, body=body,
            steps=agent.max_steps, cap=agent.max_tokens,
            history_limit=agent.max_history_bytes,
            transcripts=[history_bytes(request["messages"])
                         for request in provider.requests],
            tokens_today=quota.tenant_usage(TENANT)["tokens_today"],
            next_turn=_chat(service, "one more"))

    turn = _boot(tmp_path, monkeypatch, during)
    # The turn was the one described: every step the bounds allow, each with
    # a full transcript and a full reply.
    assert len(turn.transcripts) == turn.steps
    assert all(0.99 * turn.history_limit <= size <= turn.history_limit
               for size in turn.transcripts), turn.transcripts
    spent = turn.tokens_today - (turn.limit - turn.reservation)
    if answers:
        assert turn.status == 200, turn.body
        assert turn.body["usage"] == {"input_tokens": spent - turn.steps * turn.cap,
                                      "output_tokens": turn.steps * turn.cap}
    else:
        assert (turn.status, turn.body["error"]["code"]) == (502, "agent_failure")
    assert spent > turn.steps * turn.cap, "the turn's requests were not charged"

    assert spent <= turn.reservation, (
        f"a turn at the default bounds spent {spent:,} tokens, more than the "
        f"{turn.reservation:,} reserved for it: the reservation does not cover a real turn")
    assert turn.reservation < 2 * spent, (
        f"the default reservation ({turn.reservation:,}) is "
        f"{turn.reservation / spent:.1f} times what a turn at the default bounds can "
        f"spend ({spent:,}): several times larger than it needs to be (audit F-21)")
    # Covered by its reservation, the turn did not take the tenant past its
    # limit, and admission is closed behind it.
    assert turn.tokens_today <= turn.limit
    status, _, body = turn.next_turn
    assert (status, body["error"]["code"]) == (429, "token_quota_exceeded"), body


@settings(max_examples=60, deadline=None)
@given(limit=st.integers(min_value=1, max_value=60), data=st.data())
def test_admission_never_overshoots_by_more_than_the_turns_in_flight(limit, data):
    """The rule that makes a reservation mean something, for any limit,
    reservation, concurrency and order of turns: a turn is admitted exactly
    when used + reserved + its own reservation fits in the limit. So after
    every admission the tenant's commitments fit, and from then on they
    exceed the limit only by what the turns in flight at that admission spend
    above their reservations: nothing, when a reservation covers a turn."""
    reservation = data.draw(st.integers(min_value=1, max_value=limit), label="reservation")
    concurrency = data.draw(st.integers(min_value=1, max_value=3), label="concurrency")
    operations = data.draw(st.lists(st.one_of(
        st.just(("admit",)),
        st.tuples(st.just("settle"), st.integers(0, 7),
                  st.integers(0, 2 * reservation + 1))), max_size=24), label="operations")
    with tempfile.TemporaryDirectory(prefix="quota-property-") as directory:
        store = DurableQuotaStore(
            Path(directory) / "quota.sqlite3", max_concurrent_turns=concurrency,
            tokens_per_day=limit, token_reservation_per_turn=reservation,
            storage_bytes_per_tenant=1 << 30, storage_reservation_per_turn=1,
            audit_bytes_per_tenant=1 << 30, audit_reservation_per_turn=1,
            clock=lambda: 1000.0)
        used, in_flight, above_reservation = 0, [], 0
        for operation in operations:
            if operation[0] == "admit":
                fits = used + (len(in_flight) + 1) * reservation <= limit
                try:
                    lease = store.acquire_turn(TENANT)
                except ProductError as refused:
                    busy = len(in_flight) >= concurrency
                    assert busy or not fits, "refused a turn that fits in the limit"
                    assert (refused.status, refused.code) == (
                        429, "tenant_busy" if busy else "token_quota_exceeded")
                else:
                    assert len(in_flight) < concurrency and fits, (
                        f"admitted a turn that does not fit: {used} used, "
                        f"{len(in_flight)} in flight, {reservation} reserved each, "
                        f"limit {limit}")
                    in_flight.append(lease)
                    above_reservation = 0
            elif in_flight:
                _, which, spent = operation
                in_flight.pop(which % len(in_flight)).release(
                    usage={"input_tokens": spent // 2, "output_tokens": spent - spent // 2})
                used += spent
                above_reservation += max(0, spent - reservation)
            usage = store.tenant_usage(TENANT)
            assert (usage["tokens_today"], usage["reserved_tokens"]) == (
                used, len(in_flight) * reservation)
            assert used + len(in_flight) * reservation <= limit + above_reservation


def test_a_request_that_never_reached_the_agent_is_charged_nothing(tmp_path, monkeypatch):
    """A chat request whose deadline passes while it waits for its session
    (another worker holds it here) never reaches the agent. The agent counts
    a turn's tokens and tool calls per thread and starts over when a turn
    begins, so on a thread that served a turn before, the count is still that
    turn's. The product read it as this request's: the tenant was charged
    the earlier turn's tokens a second time."""

    def during(service):
        quota = service.quota_store
        Provider(service.agent, lambda request, step: (
            [("tool_use", "call-1", "read_file", {"path": "notes.txt"}),
             ("usage", 700, 300), ("stop", "tool_use")] if step == 1 else
            [("text", "done"), ("usage", 800, 200), ("stop", "end_turn")]))
        first = _chat(service, "hi", session="one")
        after_first = (quota.tenant_usage(TENANT)["tokens_today"],
                       service.metrics.snapshot())
        service.turn_deadline_s = 0.05
        another_worker = SessionOperationLocks(service.session_locks.lock_dir)
        with another_worker.hold(_internal_session(TENANT, "two")):
            second = _chat(service, "hi", session="two")
        return (first, after_first, second, quota.tenant_usage(TENANT),
                service.metrics.snapshot())

    first, (charged, counted), second, usage, recounted = _boot(
        tmp_path, monkeypatch, during)
    status, headers, body = first
    assert (status, body["usage"], headers["X-Sigil-Tool-Calls"], charged) == (
        200, {"input_tokens": 1500, "output_tokens": 500}, "1", 2000)
    status, headers, body = second
    assert (status, body["error"]["code"]) == (504, "turn_deadline_exceeded"), body
    assert (usage["tokens_today"], usage["reserved_tokens"]) == (charged, 0), (
        f"a request that never reached the agent was charged "
        f"{usage['tokens_today'] - charged:,} tokens: the turn before it, again")
    assert (headers["X-Sigil-Tool-Calls"], headers["X-Sigil-Retries"]) == ("0", "0")
    for total in ("input_tokens_total", "output_tokens_total", "tool_calls_total"):
        assert recounted[total] == counted[total], total
    assert (recounted["turns_total"], recounted["turn_errors_total"]) == (2, 1)


# ── F-10, the product's half: the output cap and thinking ────────────────


def test_a_default_deployment_caps_a_reply_at_the_agents_default(tmp_path, monkeypatch):
    def during(service):
        provider = Provider(service.agent, _short_answer)
        status, _, body = _chat(service, "hi")
        assert status == 200, body
        return (service.agent.max_tokens, provider.requests[0]["max_tokens"],
                _effective_settings(service)["max_tokens"])

    held, sent, reported = _boot(tmp_path, monkeypatch, during)
    assert (held, sent, reported) == (DEFAULT_MAX_TOKENS,) * 3, (
        f"a product started with default settings sends max_tokens {sent} (its agent "
        f"holds {held}, its metrics report {reported}); the agent's default is "
        f"{DEFAULT_MAX_TOKENS} (audit F-10)")


def test_a_configured_cap_still_replaces_the_default(tmp_path, monkeypatch):
    def during(service):
        provider = Provider(service.agent, _short_answer)
        status, _, body = _chat(service, "hi")
        assert status == 200, body
        return provider.requests[0]["max_tokens"], _effective_settings(service)["max_tokens"]

    sent, reported = _boot(tmp_path, monkeypatch, during, env={"PI_MAX_TOKENS": "4097"})
    assert (sent, reported) == (4097, 4097)


@pytest.mark.parametrize("configured", [None, ""] + list(THINKING_MODES))
def test_the_thinking_setting_reaches_every_request_and_the_effective_settings(
        tmp_path, monkeypatch, configured):
    """``PI_THINKING`` is how an operator matches thinking to the configured
    model: ``adaptive`` is refused by some models and ``disabled`` by others.
    Unset or empty, it is the agent's default, as on the research host."""
    expected = configured or DEFAULT_THINKING

    def during(service):
        provider = Provider(service.agent, lambda request, step: (
            [("tool_use", "call-1", "read_file", {"path": "notes.txt"}),
             ("usage", 3, 2), ("stop", "tool_use")] if step == 1 else
            _short_answer(request, step)))
        status, _, body = _chat(service, "hi")
        assert status == 200, body
        return ([request.get("thinking") for request in provider.requests],
                _effective_settings(service).get("thinking"))

    env = {} if configured is None else {"PI_THINKING": configured}
    sent, reported = _boot(tmp_path, monkeypatch, during, env=env)
    assert sent == [{"type": expected}] * 2, (
        f"PI_THINKING={configured!r} reached the provider as {sent}: a deployment "
        f"cannot set thinking to what its model accepts (audit F-10)")
    assert reported == expected, (
        f"the effective settings report thinking as {reported!r}, not {expected!r}")


@pytest.mark.parametrize("value", ["enabled", "Adaptive", "off", "adaptive ", "none"])
def test_a_thinking_mode_the_agent_does_not_have_fails_startup_before_any_state(
        tmp_path, monkeypatch, value):
    seen = _prepare(tmp_path, monkeypatch, env={"PI_THINKING": value})
    with pytest.raises(ConfigError, match="PI_THINKING"):
        product_main.run()
    assert seen.compilers == 0, "a compiler was started for a configuration that is refused"
    assert not (tmp_path / "state").exists()


def _agent_arguments_product_main_passes():
    tree = ast.parse((PI_ROOT / "product_main.py").read_text())
    [call] = [node for node in ast.walk(tree)
              if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "PiAgent"]
    names = list(inspect.signature(PiAgent.__init__).parameters)[1:]
    return set(names[:len(call.args)]) | {keyword.arg for keyword in call.keywords}


# The settings PiAgent takes that the product leaves alone, and why.
NOT_PASSED = {
    "manifest_path": "the product serves the tools in the bundled tools/manifest.json",
    "turn_deadline_s": "the product gives each turn a hard deadline of its own "
                       "(deadline_monotonic, from PI_TURN_DEADLINE_SECONDS)",
}
# What the product passes that is meant to differ from the agent's default.
DIFFERS_ON_PURPOSE = {
    "audit": "the product signs its audit chain with PI_AUDIT_KEY",
}


def test_the_product_passes_every_setting_the_agent_takes_or_says_why_not():
    """The class guard for F-10's product half. ``thinking`` became an agent
    setting and the product, which builds its agent with keywords, went on
    without it: no deployment could set it. A setting the agent gains fails
    here until product_main passes it or this file says why it does not."""
    taken = set(inspect.signature(PiAgent.__init__).parameters) - {"self"}
    passed = _agent_arguments_product_main_passes()
    assert {"endpoint", "api_key", "max_steps"} <= passed, "the scan found nothing"
    assert not passed - taken and not set(NOT_PASSED) - taken
    assert not passed & set(NOT_PASSED), "passed and excused at once"
    missing = taken - passed - set(NOT_PASSED)
    assert not missing, (
        f"PiAgent takes {sorted(missing)}, which product_main neither passes nor "
        f"leaves out for a stated reason: a product deployment cannot set it")


def test_a_default_deployment_gives_its_agent_the_agents_own_defaults(
        tmp_path, monkeypatch):
    """The other half of the class: the product had a default of its own for
    ``max_tokens`` (1,024) after the agent's changed. Whatever the product
    passes that the agent has a default for must, with nothing configured,
    be that default."""
    agent = _boot(tmp_path, monkeypatch, lambda service: service.agent)
    reference = PiAgent(
        agent.endpoint, "unused", store=SessionStore(tmp_path / "reference-sessions"),
        sandbox_root=tmp_path / "reference-sandboxes", mcp=None)
    defaulted = {name for name, parameter
                 in inspect.signature(PiAgent.__init__).parameters.items()
                 if parameter.default is not inspect.Parameter.empty}
    compared = {name for name in _agent_arguments_product_main_passes() & defaulted
                if hasattr(reference, name) and name not in DIFFERS_ON_PURPOSE}
    assert {"model", "max_tokens", "max_steps", "max_history_bytes",
            "max_tool_result_bytes", "llm_retries"} <= compared, compared
    different = {name: (getattr(agent, name), getattr(reference, name))
                 for name in sorted(compared)
                 if getattr(agent, name) != getattr(reference, name)}
    assert not different, (
        f"with default settings the product's agent differs from a default agent in "
        f"{different} (product, agent): the product has a default of its own")
