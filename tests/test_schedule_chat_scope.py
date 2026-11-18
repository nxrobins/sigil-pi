"""A scheduled turn needs the `chat` scope (audit F-28, decision D6 (a)).

`chat` is the authority to make the model act for a principal. A scheduled
turn used to need only that its creator still held `schedules:write`, so a
credential deliberately given no `chat` scope could still drive the model and
its tools on a schedule. Now making or replacing a schedule needs `chat` as
well as `schedules:write`, and the scheduler asks the owner's current policy
before each run: an owner without either gets no turn and no model call. The
run is recorded as `policy_revoked` and counted, and the schedule stays, for
its owner to fix or delete.
"""
import ast
import hashlib
import itertools
import json
from types import SimpleNamespace

import pytest

from conftest import PI_ROOT
import product_service
from product_service import (
    KNOWN_SCOPES, AuthRegistry, ProductScheduleStore, ProductScheduler,
    ProductService, render_prometheus_metrics)

TENANT = "tenant-a"
EVERY_MS = 60_000
REQUEST_ID = "c5-request-0001"
# What a schedule's owner must hold, in the order a refusal names them.
NEEDED = ("schedules:write", "chat")
EVERY_SCOPE_SET = [frozenset(chosen) for size in range(len(KNOWN_SCOPES) + 1)
                   for chosen in itertools.combinations(sorted(KNOWN_SCOPES), size)]
# The agent's methods that have the model act: a turn.
TURN_METHODS = {"turn", "turn_with_usage"}


class ScriptedAgent:
    """Answers every turn it is asked for, and records each one."""
    manifest = {"read_file": {}}
    memory = None
    _mcp = object()

    def __init__(self):
        self.calls = []

    def turn_with_usage(self, session, message, *, allowed_tools=None,
                        deadline_monotonic=None, **grants):
        self.calls.append((session, message))
        return "answer", {"input_tokens": 2, "output_tokens": 1}

    @staticmethod
    def turn_telemetry():
        return {}


class CountingQuota:
    """A quota store that records each turn it is asked to admit."""
    resource_limits_enabled = False

    def __init__(self):
        self.turns = []

    def acquire_turn(self, tenant_id, *, session_id=None, incremental_tokens=False):
        self.turns.append(tenant_id)
        return SimpleNamespace(release=lambda **kwargs: None,
                               token_budget=lambda cap: cap)


def _token(label):
    return f"c5-{label}-token-with-enough-entropy-for-a-test-only-fixture"


def _entry(token, scopes, principal="planner"):
    return {"sha256": hashlib.sha256(token.encode()).hexdigest(),
            "principal": principal, "tenant": TENANT,
            "scopes": sorted(scopes), "tools": []}


def _headers(token):
    return {"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID}


def _post_schedule(service, token, *, name="daily", message="Prepare the report."):
    body = json.dumps({"name": name, "session": "work", "message": message,
                       "every_ms": EVERY_MS}).encode()
    status, _, payload = service.dispatch("POST", "/v1/schedules", _headers(token), body)
    return status, payload


def _denied(*missing):
    return {"error": {"code": "permission_denied",
                      "message": "credential lacks required permission: "
                                 + ", ".join(missing)},
            "request_id": REQUEST_ID}


def _clock(now):
    return SimpleNamespace(time=lambda: now[0])


def _quiet(record):
    pass


def _stored(store):
    return {entry["name"]: (entry["message"], entry["last_status"])
            for entry in store.for_tenant(TENANT)}


def _made_while_allowed(store, token, *, name="daily", principal="planner"):
    """A schedule made while its owner held what making one needs, as one
    made before this release, or before an operator edited the credential,
    was."""
    creator = AuthRegistry([_entry(token, NEEDED, principal)]).authenticate(
        f"Bearer {token}")
    store.put(creator, name=name, session="work", message="Prepare the report.",
              every_ms=EVERY_MS)


def _counted(service):
    return {status: count
            for status, count in service.operational_metrics()["scheduled_runs"].items()
            if count}


# ── making and replacing a schedule ─────────────────────────────────────


def test_making_a_schedule_needs_chat_as_well_as_schedules_write(tmp_path):
    """For every set of known scopes: POST /v1/schedules is accepted only
    with both, and otherwise answered 403 permission_denied, naming what the
    credential lacks, with nothing stored."""
    wrong = []
    for index, scopes in enumerate(EVERY_SCOPE_SET):
        token = _token(f"make-{index:02d}")
        store = ProductScheduleStore(tmp_path / f"schedules-{index:02d}.json")
        service = ProductService(ScriptedAgent(), AuthRegistry([_entry(token, scopes)]),
                                 schedule_store=store, log_sink=_quiet)
        missing = [scope for scope in NEEDED if scope not in scopes]
        expected = ((403, _denied(*missing), {}) if missing else
                    (201, {"created": True, "request_id": REQUEST_ID},
                     {"daily": ("Prepare the report.", None)}))
        got = (*_post_schedule(service, token), _stored(store))
        if got != expected:
            wrong.append(f"{sorted(scopes)}: {got[0]} {got[1]}, stored {got[2]}")
    assert not wrong, (
        f"{len(wrong)} of {len(EVERY_SCOPE_SET)} scope sets were answered wrongly. A "
        f"credential that may not chat can schedule the model to act for it (audit "
        f"F-28):\n" + "\n".join(wrong))


def test_replacing_a_schedule_needs_chat_and_leaves_it_as_it_was(tmp_path):
    token = _token("replace")
    store = ProductScheduleStore(tmp_path / "schedules.json")
    _made_while_allowed(store, token)
    service = ProductService(
        ScriptedAgent(), AuthRegistry([_entry(token, {"schedules:read", "schedules:write"})]),
        schedule_store=store, log_sink=_quiet)

    status, payload = _post_schedule(service, token, message="Delete everything.")

    assert (status, payload) == (403, _denied("chat")), (
        f"a credential without chat replaced a schedule: {status} {payload} (audit F-28)")
    assert _stored(store) == {"daily": ("Prepare the report.", None)}


# ── running a schedule, and a chat turn ─────────────────────────────────


def test_a_scheduled_turn_runs_only_while_its_owner_holds_chat_and_schedules_write(tmp_path):
    """For every set of known scopes the owner holds when the entry falls
    due: the turn runs only with both. Without them the agent is not asked,
    no turn is admitted against the tenant's quota, and the run is recorded
    as policy_revoked at the time it was due, so the entry waits its interval
    and the schedule stays."""
    wrong = []
    for index, scopes in enumerate(EVERY_SCOPE_SET):
        token = _token(f"run-{index:02d}")
        store = ProductScheduleStore(tmp_path / f"schedules-{index:02d}.json")
        _made_while_allowed(store, token)
        agent, quota = ScriptedAgent(), CountingQuota()
        ProductScheduler(store, agent, AuthRegistry([_entry(token, scopes)]),
                         clock=_clock([1000.0]), quota_store=quota,
                         log_sink=_quiet).tick()
        [(_, entry)] = store.entries()
        allowed = set(NEEDED) <= scopes
        expected = ("ok" if allowed else "policy_revoked", 1000.0,
                    int(allowed), int(allowed))
        got = (entry["last_status"], entry["last_run"], len(agent.calls), len(quota.turns))
        if got != expected:
            wrong.append(f"{sorted(scopes)}: last_status {got[0]!r}, last_run {got[1]}, "
                         f"model turns {got[2]}, admitted {got[3]}")
    assert not wrong, (
        f"{len(wrong)} of {len(EVERY_SCOPE_SET)} scope sets ran wrongly. An owner without "
        f"chat had the model act for it on a schedule (audit F-28):\n" + "\n".join(wrong))


def test_a_chat_turn_runs_only_with_chat(tmp_path):
    """The interactive path, held to the same authority: for every set of
    known scopes, POST /v1/chat asks the agent only with chat, and is
    otherwise answered 403 permission_denied naming it."""
    wrong = []
    body = json.dumps({"session": "work", "message": "hello"}).encode()
    for index, scopes in enumerate(EVERY_SCOPE_SET):
        token = _token(f"chat-{index:02d}")
        agent = ScriptedAgent()
        service = ProductService(agent, AuthRegistry([_entry(token, scopes)]),
                                 log_sink=_quiet)
        status, _, payload = service.dispatch("POST", "/v1/chat", _headers(token), body)
        if "chat" in scopes:
            right = status == 200 and len(agent.calls) == 1
        else:
            right = (status, payload, agent.calls) == (403, _denied("chat"), [])
        if not right:
            wrong.append(f"{sorted(scopes)}: {status} {payload}, model turns {len(agent.calls)}")
    assert not wrong, "\n".join(wrong)


def test_an_owner_who_loses_chat_gets_no_more_turns_and_keeps_the_schedule(tmp_path):
    """Through the API: made with chat, run; the credential is edited to drop
    chat (an auth-file change takes a restart); the next run is refused,
    recorded and counted, with no model call. The owner can still list and
    delete the schedule, but not replace it. Given chat back, it runs."""
    token = _token("owner")
    with_chat = [_entry(token, {"chat", "schedules:read", "schedules:write"})]
    without_chat = [_entry(token, {"schedules:read", "schedules:write"})]
    store = ProductScheduleStore(tmp_path / "schedules.json")
    agent, now = ScriptedAgent(), [1000.0]

    def restart(entries):
        auth = AuthRegistry(entries)
        scheduler = ProductScheduler(store, agent, auth, clock=_clock(now),
                                     log_sink=_quiet)
        return scheduler, ProductService(agent, auth, schedule_store=store,
                                         scheduler=scheduler, log_sink=_quiet)

    def listed(service):
        status, _, payload = service.dispatch("GET", "/v1/schedules", _headers(token))
        assert status == 200, payload
        return [(entry["name"], entry["last_run"], entry["last_status"])
                for entry in payload["schedules"]]

    scheduler, service = restart(with_chat)
    assert _post_schedule(service, token)[0] == 201
    scheduler.tick()
    assert listed(service) == [("daily", 1000.0, "ok")] and len(agent.calls) == 1

    scheduler, service = restart(without_chat)
    now[0] += 61
    scheduler.tick()
    assert len(agent.calls) == 1, (
        "the scheduler asked the model for a turn for an owner whose credential no "
        "longer has chat (audit F-28)")
    assert listed(service) == [("daily", 1061.0, "policy_revoked")]
    assert _counted(service) == {"policy_revoked": 1}
    assert _post_schedule(service, token, message="Something else.") == (
        403, _denied("chat"))

    scheduler, service = restart(with_chat)
    now[0] += 61
    scheduler.tick()
    assert listed(service) == [("daily", 1122.0, "ok")] and len(agent.calls) == 2

    scheduler, service = restart(without_chat)
    status, _, payload = service.dispatch("DELETE", "/v1/schedules/daily", _headers(token))
    assert (status, payload) == (200, {"removed": True, "request_id": REQUEST_ID}), (
        "the owner of a schedule it may no longer run could not delete it")
    assert listed(service) == []


# ── counting ────────────────────────────────────────────────────────────


def test_each_recorded_run_is_counted_by_status_in_both_metrics_forms(tmp_path):
    allowed, refused = _token("allowed"), _token("refused")
    store = ProductScheduleStore(tmp_path / "schedules.json")
    _made_while_allowed(store, allowed, name="kept")
    for name in ("first", "second"):
        _made_while_allowed(store, refused, name=name, principal="other")
    auth = AuthRegistry([_entry(allowed, NEEDED),
                         _entry(refused, {"schedules:read", "schedules:write"},
                                principal="other")])
    agent = ScriptedAgent()
    scheduler = ProductScheduler(store, agent, auth, clock=_clock([1000.0]),
                                 log_sink=_quiet)
    service = ProductService(agent, auth, schedule_store=store, scheduler=scheduler,
                             log_sink=_quiet)
    statuses = product_service.SCHEDULED_RUN_STATUSES
    assert service.operational_metrics()["scheduled_runs"] == dict.fromkeys(statuses, 0)

    scheduler.tick()

    assert _counted(service) == {"ok": 1, "policy_revoked": 2}
    assert len(agent.calls) == 1
    exported = render_prometheus_metrics(service.operational_metrics(), "1.0.0")
    assert "\n# TYPE sigil_pi_scheduled_runs_total counter\n" in exported
    series = sorted(line for line in exported.splitlines()
                    if line.startswith("sigil_pi_scheduled_runs_total"))
    assert series == sorted(
        f'sigil_pi_scheduled_runs_total{{status="{status}"}} '
        f'{ {"ok": 1, "policy_revoked": 2}.get(status, 0)}'
        for status in statuses)


def test_a_run_is_counted_when_it_is_recorded_and_not_before(tmp_path):
    """A run whose record could not be written keeps its claim and runs again
    once its lease has passed (at-least-once). It is counted once, when it is
    recorded, not once for each attempt."""
    token = _token("unrecorded")
    store = ProductScheduleStore(tmp_path / "schedules.json")
    _made_while_allowed(store, token)
    auth = AuthRegistry([_entry(token, {"schedules:write"})])
    now, agent = [1000.0], ScriptedAgent()
    scheduler = ProductScheduler(store, agent, auth, clock=_clock(now), lease_s=30,
                                 turn_deadline_s=10, log_sink=_quiet)
    service = ProductService(agent, auth, schedule_store=store, scheduler=scheduler,
                             log_sink=_quiet)
    real, failures = store.complete_claim, [OSError(28, "No space left on device")]

    def complete_claim(*args, **kwargs):
        if failures:
            raise failures.pop()
        return real(*args, **kwargs)

    store.complete_claim = complete_claim
    with pytest.raises(OSError):
        scheduler.tick()
    assert _counted(service) == {}
    now[0] += 31
    scheduler.tick()
    assert _counted(service) == {"policy_revoked": 1}
    assert [entry["last_status"] for entry in store.for_tenant(TENANT)] == ["policy_revoked"]
    assert agent.calls == []


def test_a_schedule_deleted_after_its_claim_runs_no_turn_and_counts_no_run(tmp_path):
    """Review R4 (2026-11-17). A schedule deleted between its claim and the
    re-read under its session's lock rightly gets no turn, but completing the
    claim then finds no row, and the run used to be counted `failed` all the
    same: a failure in both metrics forms that no run had. Only a run the
    store records is counted."""
    token = _token("deleted")
    store = ProductScheduleStore(tmp_path / "schedules.json")
    _made_while_allowed(store, token)
    auth = AuthRegistry([_entry(token, NEEDED)])
    agent = ScriptedAgent()
    scheduler = ProductScheduler(store, agent, auth, clock=_clock([1000.0]),
                                 log_sink=_quiet)
    service = ProductService(agent, auth, schedule_store=store, scheduler=scheduler,
                             log_sink=_quiet)
    claim_due, complete_claim, completed = store.claim_due, store.complete_claim, []

    def claim_then_delete(*args, **kwargs):
        entry = claim_due(*args, **kwargs)
        if entry is not None:
            assert store.remove(TENANT, "daily")
        return entry

    def observed_completion(*args, **kwargs):
        completed.append(complete_claim(*args, **kwargs))
        return completed[-1]

    store.claim_due, store.complete_claim = claim_then_delete, observed_completion
    scheduler.tick()
    assert (agent.calls, completed, store.for_tenant(TENANT)) == ([], [False], [])
    assert _counted(service) == {}


def test_a_run_whose_claim_another_scheduler_holds_now_is_not_counted(tmp_path):
    """The other completion that records nothing: the turn outlasted its lease
    and another scheduler holds the claim now. That scheduler's run is the one
    the store records, and counts; this one is not counted as well."""
    token = _token("overtaken")
    store = ProductScheduleStore(tmp_path / "schedules.json")
    _made_while_allowed(store, token)
    auth = AuthRegistry([_entry(token, NEEDED)])
    agent = ScriptedAgent()
    scheduler = ProductScheduler(store, agent, auth, clock=_clock([1000.0]),
                                 log_sink=_quiet)
    service = ProductService(agent, auth, schedule_store=store, scheduler=scheduler,
                             log_sink=_quiet)
    complete_claim = store.complete_claim
    store.complete_claim = lambda key, owner, when, status: complete_claim(
        key, "another-scheduler", when, status)
    scheduler.tick()
    assert len(agent.calls) == 1
    assert _counted(service) == {}


# ── guards ──────────────────────────────────────────────────────────────


def _innermost_functions_calling(tree, methods):
    """The innermost function around each call of a method named in
    ``methods`` (None for a call at module level)."""
    found = []

    def visit(node, function):
        for child in ast.iter_child_nodes(node):
            if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                    and child.func.attr in methods):
                found.append(function)
            visit(child, child if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function)

    visit(tree, None)
    return found


def test_every_path_that_asks_the_agent_for_a_turn_checks_the_turn_scope():
    """The class of F-28: a path that has the model act for a principal
    without asking whether that principal may. Each function of the product
    host that asks the agent for a turn must name TURN_SCOPE, directly or
    through SCHEDULED_TURN_SCOPES. There are two today, the chat route and a
    scheduled turn, so the scan cannot pass vacuously."""
    callers = {}
    for module in ("product_service.py", "product_main.py"):
        tree = ast.parse((PI_ROOT / module).read_text())
        for function in _innermost_functions_calling(tree, TURN_METHODS):
            named = set() if function is None else {
                node.id for node in ast.walk(function) if isinstance(node, ast.Name)}
            where = f"{module}:{getattr(function, 'name', '<module>')}"
            callers[where] = bool(named & {"TURN_SCOPE", "SCHEDULED_TURN_SCOPES"})
    assert len(callers) >= 2, f"the scan found only {sorted(callers)}: is it still scanning?"
    unchecked = sorted(where for where, checked in callers.items() if not checked)
    assert not unchecked, (
        f"these ask the agent for a turn without checking the turn scope: {unchecked} "
        f"(audit F-28)")
    assert product_service.TURN_SCOPE == "chat"
    assert product_service.TURN_SCOPE in product_service.SCHEDULED_TURN_SCOPES
    assert set(product_service.SCHEDULED_TURN_SCOPES) <= KNOWN_SCOPES


def test_every_status_a_scheduled_run_records_is_counted_and_documented():
    """The statuses `_run_turn` records are the stable set the metrics count
    and docs/api.md lists, so a status added later cannot go uncounted or
    undocumented."""
    tree = ast.parse((PI_ROOT / "product_service.py").read_text())
    [scheduler] = [node for node in tree.body
                   if isinstance(node, ast.ClassDef) and node.name == "ProductScheduler"]
    [run_turn] = [node for node in scheduler.body
                  if isinstance(node, ast.FunctionDef) and node.name == "_run_turn"]
    recorded = {node.value.value for node in ast.walk(run_turn)
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and any(isinstance(target, ast.Name) and target.id == "status"
                        for target in node.targets)}
    assert {"ok", "policy_revoked"} <= recorded, f"the scan found only {recorded}"
    assert recorded == set(product_service.SCHEDULED_RUN_STATUSES)
    api = (PI_ROOT / "docs" / "api.md").read_text()
    section = api.split("### `GET /v1/schedules`", 1)[1].split("\n#", 1)[0]
    undocumented = sorted(status for status in recorded if f"`{status}`" not in section)
    assert not undocumented, (
        f"docs/api.md's GET /v1/schedules section does not list {undocumented}")
