"""Paths of the 100% inventory that no test took (audit 2026-11-10, F-17).

ci.sh now requires 100% line and branch coverage of the server, the dispatch
path, the readiness snapshot, the scheduler and the agent's dispatch,
redaction and chain verification. The full suite already ran most of that
code; these tests take the rest, each for the behaviour it decides:

- dispatch() answered a request for a route it does not serve, and a schedule
  body without exactly its four fields, by code no test ran (S3's checker);
- two handlers held only because every error the tests raised was an
  OSError: _log_event swallowing a sink's failure, and the scheduler's look
  failing a tick on a refused claim (C1's checker kept both narrowings alive).
  A log sink fails with ValueError once stdout is closed, and a schedule store
  with whatever its own code raises;
- a body that is JSON but not an object, a metered scheduler with no data
  manager, a scheduler with no time for a turn, a claim that can be neither
  run nor given back, a directory where the schedule store should be, an
  entry another scheduler holds the lease of, and an audit chain whose file
  is empty, holds blank lines or ends in a torn record.
"""
import errno
import hashlib
import json

import pytest

import product_service
from agent import GENESIS_HASH, AuditLog, AuditWriteError, verify_chain
from product_service import (
    AuthRegistry,
    ConfigError,
    DurableQuotaStore,
    ProductScheduler,
    ProductScheduleStore,
    ProductService,
    _internal_session,
    _log_event,
)

REQUEST_ID = "inventory-request-0001"


def _token(tenant):
    return f"{tenant}-inventory-test-token-with-enough-entropy-0123456789"


def _auth(*tenants, scopes=("chat", "schedules:read", "schedules:write", "ops:read")):
    return AuthRegistry([{
        "sha256": hashlib.sha256(_token(tenant).encode()).hexdigest(),
        "principal": f"planner-{tenant}",
        "tenant": tenant,
        "scopes": list(scopes),
        "tools": ["read_file"],
    } for tenant in tenants])


def _headers(tenant=None):
    headers = {"X-Request-ID": REQUEST_ID}
    if tenant is not None:
        headers["Authorization"] = f"Bearer {_token(tenant)}"
    return headers


class _Agent:
    """Never asked for a turn by these requests."""
    manifest = {"read_file": {}}
    memory = None
    _mcp = object()

    def turn_with_usage(self, *args, **kwargs):
        raise AssertionError("no request here starts a turn")


def _service(tmp_path, tenant, logged):
    store = ProductScheduleStore(tmp_path / "schedules.json",
                                 min_every_ms=product_service.MIN_SCHEDULE_EVERY_MS)
    service = ProductService(_Agent(), _auth(tenant), schedule_store=store,
                             log_sink=logged.append)
    return service, store


def _counted(service, method, route):
    return [(entry["status"], entry["count"])
            for entry in service.metrics.snapshot()["requests"]
            if entry["method"] == method and entry["route"] == route]


@pytest.mark.parametrize("method, path", [
    ("GET", "/v1/no-such-route"),
    ("GET", "/"),
    ("PUT", "/v1/chat"),
    ("GET", "/v1/chat"),
    ("DELETE", "/v1/health"),
    ("POST", "/v1/ready"),
    ("PATCH", "/v1/schedules"),
    ("GET", "/v1/schedules/daily"),
])
def test_a_route_the_product_does_not_serve_is_answered_404_not_found(tmp_path, method, path):
    """A credentialed request for a route, or a method on a route, the product
    does not serve gets the error contract's 404 not_found, counted and logged
    as such, with no exception text and nothing done."""
    tenant = "tenant-routes"
    logged = []
    service, store = _service(tmp_path, tenant, logged)

    status, headers, payload = service.dispatch(method, path, _headers(tenant), b"{}")

    assert (status, payload) == (404, {
        "error": {"code": "not_found", "message": "route not found"},
        "request_id": REQUEST_ID,
    })
    assert headers == {"Cache-Control": "no-store", "X-Request-ID": REQUEST_ID}
    assert _counted(service, method, "unknown") == [(404, 1)]
    assert service.metrics.snapshot()["error_classes"] == {"not_found": 1}
    [record] = logged
    assert {key: record[key] for key in ("event", "request_id", "method", "route",
                                         "status", "failure_classification")} == {
        "event": "http_request", "request_id": REQUEST_ID, "method": method,
        "route": "unknown", "status": 404, "failure_classification": "not_found"}
    assert store.for_tenant(tenant) == []


@pytest.mark.parametrize("method, path", [("GET", "/v1/no-such-route"), ("PUT", "/v1/chat")])
def test_a_route_is_not_looked_up_for_a_request_without_a_credential(tmp_path, method, path):
    """Authentication comes before routing: a client without a credential
    learns nothing about which routes exist."""
    logged = []
    service, _ = _service(tmp_path, "tenant-routes", logged)

    status, headers, payload = service.dispatch(method, path, _headers(), b"")

    assert status == 401 and payload["error"]["code"] != "not_found", payload
    assert headers["WWW-Authenticate"] == 'Bearer realm="sigil-pi"'
    assert [record["route"] for record in logged] == ["unknown"]


@pytest.mark.parametrize("body", [
    {},
    {"name": "daily", "session": "work", "message": "m"},
    {"name": "daily", "session": "work", "every_ms": 60_000},
    {"name": "daily", "session": "work", "message": "m", "every_ms": 60_000,
     "tenant": "tenant-other"},
    {"name": "daily", "session": "work", "message": "m", "every_ms": 60_000,
     "last_run": 0},
], ids=["empty", "no-every_ms", "no-message", "a-tenant", "a-last_run"])
def test_a_schedule_body_without_exactly_its_four_fields_is_refused(tmp_path, body):
    """A schedule is created from exactly name, session, message and every_ms.
    A field more (a tenant, a last run) or less is refused with 400
    invalid_request, before the store is asked, so nothing is stored."""
    tenant = "tenant-schedules"
    logged = []
    service, store = _service(tmp_path, tenant, logged)

    status, _, payload = service.dispatch(
        "POST", "/v1/schedules", _headers(tenant), json.dumps(body).encode())

    assert (status, payload) == (400, {
        "error": {"code": "invalid_request",
                  "message": "expected exactly name, session, message, and every_ms"},
        "request_id": REQUEST_ID,
    })
    assert _counted(service, "POST", "/v1/schedules") == [(400, 1)]
    assert store.entries() == []
    complete = {"name": "daily", "session": "work", "message": "m", "every_ms": 60_000}
    assert service.dispatch("POST", "/v1/schedules", _headers(tenant),
                            json.dumps(complete).encode())[0] == 201


class _SinkError(Exception):
    """Not an OSError: what a sink's own code, or a closed stream, raises."""


@pytest.mark.parametrize("error", [
    ValueError("I/O operation on closed file."),
    OSError(errno.ENOSPC, "No space left on device"),
    BrokenPipeError(errno.EPIPE, "Broken pipe"),
    UnicodeEncodeError("ascii", "☃", 0, 1, "ordinal not in range(128)"),
    _SinkError("the sink's own failure"),
], ids=lambda error: type(error).__name__)
def test_an_event_the_sink_cannot_take_is_dropped_whatever_the_sink_raises(error):
    """print() to a closed stdout raises ValueError, not OSError. A sink that
    fails in any way loses the line, and nothing else: _log_event returns."""
    calls = []

    def sink(record):
        calls.append(record)
        raise error

    assert _log_event(sink, {"event": "probe"}) is None
    assert calls == [{"event": "probe"}]


def test_a_request_is_answered_when_its_log_line_raises_a_value_error(tmp_path):
    """The answer, the count and the work of a request stand when its log
    line cannot be written because stdout was closed."""
    tenant = "tenant-closed-stdout"
    store = ProductScheduleStore(tmp_path / "schedules.json")

    def closed_stdout(record):
        raise ValueError("I/O operation on closed file.")

    service = ProductService(_Agent(), _auth(tenant), schedule_store=store,
                             log_sink=closed_stdout)
    body = {"name": "daily", "session": "work", "message": "m", "every_ms": 60_000}
    status, _, payload = service.dispatch("POST", "/v1/schedules", _headers(tenant),
                                          json.dumps(body).encode())

    assert (status, payload) == (201, {"created": True, "request_id": REQUEST_ID})
    assert _counted(service, "POST", "/v1/schedules") == [(201, 1)]
    assert [entry["name"] for entry in store.for_tenant(tenant)] == ["daily"]


class _StoreFailure(Exception):
    """A store failure that is not an OSError: a bug, a corrupt value."""


@pytest.mark.parametrize("refusal", [
    RuntimeError("schedule store is locked by another owner"),
    ValueError("lease_until is not a number"),
    KeyError("every_ms"),
    _StoreFailure("the store's own failure"),
    OSError(errno.ENOSPC, "No space left on device"),
], ids=lambda error: type(error).__name__)
def test_a_claim_the_store_refuses_fails_the_tick_whatever_it_raises(product_scheduler, refusal):
    """A claim the store refuses, with any error, is not run, is given no
    lease, is tried once in the tick, and fails the tick with that error once
    the other tenant's due entry has run; the scheduler goes on ticking."""
    refused_tenant, other_tenant = sorted(
        ("tenant-refused", "tenant-served"),
        key=lambda name: hashlib.sha256(name.encode()).hexdigest())
    auth = _auth(refused_tenant, other_tenant, scopes=("chat", "schedules:write"))
    scheduler = product_scheduler(auth=auth)
    refused = auth.authenticate(f"Bearer {_token(refused_tenant)}")
    served = auth.authenticate(f"Bearer {_token(other_tenant)}")
    scheduler.add("refused", principal=refused)
    scheduler.add("served", principal=served)
    refused_key = ProductScheduleStore._key(refused_tenant, "refused")
    claim = scheduler.store.claim_due
    attempts = []

    def claim_due(key, *args):
        if key == refused_key:
            attempts.append(key)
            raise refusal
        return claim(key, *args)

    scheduler.store.claim_due = claim_due

    with pytest.raises(type(refusal)) as raised:
        scheduler.scheduler.tick()

    assert raised.value is refusal
    assert attempts == [refused_key]
    assert [turn.session for turn in scheduler.agent.turns] == [
        _internal_session(other_tenant, "served")]
    assert scheduler.schedule("served", served)["last_status"] == "ok"
    stored = scheduler.stored("refused", refused)
    assert "lease_owner" not in stored and stored.get("last_run") is None
    scheduler.store.claim_due = claim
    scheduler.scheduler.tick()
    assert [turn.session for turn in scheduler.agent.turns][-1] == _internal_session(
        refused_tenant, "refused")


@pytest.mark.parametrize("body", [b"[]", b'"daily"', b"60000", b"null", b"true"])
def test_a_body_that_is_json_but_not_an_object_is_refused(tmp_path, body):
    """Every route that takes a body takes a JSON object; any other JSON value
    is 400 invalid_request, and nothing is stored."""
    tenant = "tenant-bodies"
    service, store = _service(tmp_path, tenant, [])

    status, _, payload = service.dispatch("POST", "/v1/schedules", _headers(tenant), body)

    assert (status, payload) == (400, {
        "error": {"code": "invalid_request", "message": "request body must be a JSON object"},
        "request_id": REQUEST_ID,
    })
    assert store.entries() == []


def test_a_metered_scheduler_without_the_data_manager_is_a_configuration_error(tmp_path):
    """A scheduled turn is charged storage and audit bytes as a chat turn is,
    and the data manager measures them. A scheduler given a metered quota
    store and no data manager would charge nothing; product startup refuses
    it instead. An unmetered quota store needs no data manager."""
    store = ProductScheduleStore(tmp_path / "schedules.json")
    auth = _auth("tenant-metered")
    metered = DurableQuotaStore(
        tmp_path / "metered.sqlite3", tokens_per_day=1_000_000,
        token_reservation_per_turn=10_000,
        storage_bytes_per_tenant=1 << 20, storage_reservation_per_turn=1 << 10,
        audit_bytes_per_tenant=1 << 20, audit_reservation_per_turn=1 << 10)

    with pytest.raises(ConfigError,
                       match="scheduled resource quota enforcement requires the product data manager"):
        ProductScheduler(store, _Agent(), auth, quota_store=metered)

    unmetered = DurableQuotaStore(tmp_path / "unmetered.sqlite3")
    assert ProductScheduler(store, _Agent(), auth, quota_store=unmetered).quota_store is unmetered


@pytest.mark.parametrize("setting, message", [
    ({"turn_deadline_s": 0}, "schedule turn_deadline_s must be positive"),
    ({"turn_deadline_s": -1}, "schedule turn_deadline_s must be positive"),
    ({"lease_s": 0}, "schedule lease_s must be positive"),
])
def test_a_scheduler_with_no_time_for_a_turn_or_its_lease_is_a_configuration_error(
        tmp_path, setting, message):
    """A turn needs a deadline after it starts and a claim a lease that ends
    after it is made. With either at zero or below, every scheduled turn would
    be out of time before it began, so the scheduler is refused at startup."""
    store = ProductScheduleStore(tmp_path / "schedules.json")
    with pytest.raises(ConfigError, match=message):
        ProductScheduler(store, _Agent(), _auth("tenant-settings"), **setting)


def test_a_claim_that_can_be_neither_run_nor_given_back_waits_out_its_lease(product_scheduler):
    """No thread can be started for a claimed entry's turn, and giving its
    claim back fails too (a read-only store). The tick fails with the first
    error, still runs the rest of what was due, and the entry keeps its
    lease: it is not run twice at once, and once the lease ends it runs."""
    tenants = sorted(("tenant-one", "tenant-two", "tenant-three"),
                     key=lambda name: hashlib.sha256(name.encode()).hexdigest())
    auth = _auth(*tenants, scopes=("chat", "schedules:write"))
    scheduler = product_scheduler(auth=auth)
    principals = {tenant: auth.authenticate(f"Bearer {_token(tenant)}") for tenant in tenants}
    for tenant in tenants:
        scheduler.add(tenant, principal=principals[tenant])
    pool = scheduler.scheduler
    start_thread = pool._start_thread
    refused_threads = []

    def first_turn_thread_fails(name, target, *args):
        if name == "pi-scheduled-turn" and not refused_threads:
            refused_threads.append(args[0].key)
            raise RuntimeError("can't start new thread")
        return start_thread(name, target, *args)

    def read_only_release(key, owner):
        raise OSError(errno.EROFS, "Read-only file system")

    pool._start_thread = first_turn_thread_fails
    release_claim = scheduler.store.release_claim
    scheduler.store.release_claim = read_only_release

    with pytest.raises(RuntimeError, match="can't start new thread"):
        pool.tick()

    [refused_key] = refused_threads
    [refused] = [tenant for tenant in tenants
                 if ProductScheduleStore._key(tenant, tenant) == refused_key]
    ran = sorted(turn.session for turn in scheduler.agent.turns)
    assert ran == sorted(_internal_session(tenant, tenant) for tenant in tenants
                         if tenant != refused), "the tick stopped serving after the failure"
    held = scheduler.stored(refused, principals[refused])
    assert held["lease_owner"] == pool.owner and held.get("last_run") is None
    assert pool._running == set() and pool._pool == set()

    pool._start_thread = start_thread
    scheduler.store.release_claim = release_claim
    pool.tick()
    assert len(scheduler.agent.turns) == 2, "the leased entry ran before its lease ended"
    scheduler.clock.advance(pool.lease_s + 1)
    pool.tick()
    assert scheduler.agent.turns[-1].session == _internal_session(refused, refused)
    assert scheduler.schedule(refused, principals[refused])["last_status"] == "ok"


def test_a_directory_in_place_of_the_schedule_store_is_corrupt_not_empty(tmp_path):
    """A directory where the store's file should be is not an empty store:
    reading it fails, and the store reports itself unhealthy, so readiness
    and the scheduler fail closed instead of finding nothing due."""
    path = tmp_path / "schedules.json"
    path.mkdir()
    store = ProductScheduleStore(path)

    for read in (store.entries, lambda: store.get("any-key"),
                 lambda: store.for_tenant("tenant-any")):
        with pytest.raises(RuntimeError, match="product schedule store is corrupt"):
            read()
    assert store.is_healthy() is False


def test_an_entry_another_scheduler_holds_is_not_claimed_until_its_lease_ends(tmp_path):
    """Product workers share one store, and one entry never runs twice at
    once: while one worker's lease on a due entry runs, another worker's
    claim gets nothing; once it has run out (that worker died mid-turn), the
    other worker may claim the entry."""
    tenant = "tenant-lease"
    store = ProductScheduleStore(tmp_path / "schedules.json")
    principal = _auth(tenant).authenticate(f"Bearer {_token(tenant)}")
    store.put(principal, name="daily", session="work", message="m", every_ms=3_600_000)
    key = ProductScheduleStore._key(tenant, "daily")
    now = 1_000_000.0

    first = store.claim_due(key, now, "worker-a", 300)
    assert (first["lease_owner"], first["lease_until"]) == ("worker-a", now + 300)
    for later in (0.0, 10.0, 299.9):
        assert store.claim_due(key, now + later, "worker-b", 300) is None
    assert store.get(key)["lease_owner"] == "worker-a"

    second = store.claim_due(key, now + 300.5, "worker-b", 300)
    assert (second["lease_owner"], second["lease_until"]) == ("worker-b", now + 600.5)


def _forge(log, session, n):
    log.append_or_raise(session, "tool", f"source {n}", f"input {n}", f"output {n}", None,
                        {"kv": ["sess=/private/state"]}, 1000 + n)


def test_blank_lines_in_an_audit_chain_are_read_past_and_never_break_it(tmp_path):
    """A blank line in a session's chain is skipped by the reader, so the
    chain still verifies; a file that holds nothing but blank lines has no
    last record, and the next append starts the chain at its genesis."""
    key = b"inventory-audit-key"
    log = AuditLog(tmp_path / "audit", key=key)
    _forge(log, "spaced", 0)
    path = log._path("spaced")
    path.write_text(path.read_text() + "\n   \n")
    _forge(log, "spaced", 1)
    path.write_text("\n" + path.read_text())
    records = log.read("spaced")
    assert [record["seq"] for record in records] == [0, 1]
    assert verify_chain(records, key) == (True, None)

    blank = log._path("blank")
    blank.write_text("\n\n  \n")
    _forge(log, "blank", 0)
    [record] = log.read("blank")
    assert (record["seq"], record["prev_hash"]) == (0, GENESIS_HASH)
    assert verify_chain([record], key) == (True, None)


def test_an_empty_audit_file_has_no_last_record_and_its_chain_starts_at_genesis(tmp_path):
    """A session's audit file can exist with nothing in it (created, then the
    process stopped before its first record was written). It has no last
    record, so the next append is the chain's first, linked to the genesis
    hash, and the chain verifies."""
    key = b"inventory-audit-key"
    log = AuditLog(tmp_path / "audit", key=key)
    empty = log._path("empty")
    empty.touch()

    _forge(log, "empty", 0)
    _forge(log, "empty", 1)

    records = log.read("empty")
    assert [(record["seq"], record["prev_hash"] == GENESIS_HASH) for record in records] == [
        (0, True), (1, False)]
    assert verify_chain(records, key) == (True, None)


def test_an_audit_chain_whose_last_record_is_torn_is_never_appended_to(tmp_path):
    """A record cut off mid-write leaves a final line that is not JSON.
    Chaining a new record onto it would hide the loss under valid links, so
    the append is refused, the file is left as it is, and a strict log fails
    the forge it was recording."""
    key = b"inventory-audit-key"
    log = AuditLog(tmp_path / "audit", key=key)
    for n in range(2):
        _forge(log, "torn", n)
    path = log._path("torn")
    torn = path.read_bytes()[:-20]
    path.write_bytes(torn)

    with pytest.raises(ValueError, match="torn/truncated final record"):
        _forge(log, "torn", 2)
    assert path.read_bytes() == torn

    strict = AuditLog(tmp_path / "audit", key=key, strict=True)
    with pytest.raises(AuditWriteError) as failed:
        strict.record("torn", "tool", "source", "input", "output", None, {}, 1)
    assert isinstance(failed.value.__cause__, ValueError)
    assert path.read_bytes() == torn
    with pytest.raises(ValueError, match="truncated/torn record at line 2"):
        log.read("torn")
