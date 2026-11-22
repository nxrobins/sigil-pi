"""Audit F-27: a tenant's schedule messages were charged to no quota.

A schedule's message may hold 256 KiB, and the schedule store keeps it as
JSON, where a control character takes six bytes: one tenant's 32 schedules
could put 48 MiB into the one document that every claim and every completion
reads and rewrites under the store's lock. At 20 tenants holding 8 MiB each,
the document was 160 MiB and one claim took over half a second
(``audit/repro/r8_schedule_store_scaling.py``).

What these tests hold:

- A tenant's schedule messages count against a byte quota of its own, beside
  the quota on how many schedules it may hold. A put that would take them past
  ``max_bytes_per_tenant`` is refused with 409 ``schedule_quota_exceeded``
  and leaves the store as it was; another tenant's messages do not count.
- A message counts the bytes it takes in the store: its UTF-8 bytes and the
  escapes JSON adds for a quote, a backslash or a control character, not the
  quotes around it.
- A tenant already over its quota (a store written before there was one, or a
  quota lowered since) keeps its schedules, and may replace one with a message
  no larger than the one it held; it cannot grow.
- The product takes the quota from ``PI_MAX_SCHEDULE_BYTES_PER_TENANT``, by
  default 8 MiB: 32 plain-text messages of 256 KiB, what a tenant could hold
  before.
- Every product path that builds a schedule store passes the interval floor
  and a byte quota (a source guard: the store's own floor stays the permissive
  1 ms that many tests build stores with).

Not held here: a claim or a completion still reads and rewrites the whole
document, every tenant's messages with it, so its cost still grows with the
number of tenants, now at most by the quota for each. Making it independent of
them needs another on-disk layout for schedules, which waits on decision D5.
"""

import ast
import hashlib
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from conftest import PI_ROOT
from test_product_main import _base_env, _install_happy_runtime

import product_main
import product_service
from product_service import (
    AuthRegistry,
    ConfigError,
    Principal,
    ProductError,
    ProductScheduleStore,
    ProductService,
    SCHEDULE_STORE_MARKER,
    read_schedule_rows,
)

MIB = 1024 * 1024
DEFAULT_QUOTA = 8 * MIB     # PI_MAX_SCHEDULE_BYTES_PER_TENANT when it is not set
EVERY_MS = 60_000


def _principal(tenant="tenant-a"):
    return Principal(f"planner-{tenant}", tenant, frozenset({"schedules:write"}),
                     frozenset())


def _stored(message):
    """What a message takes in the store's JSON, quotes aside."""
    return len(json.dumps(message, ensure_ascii=False).encode()) - 2


def _rows(store):
    """Every schedule the store holds, as its rows hold it, message and
    message_bytes included (schedule store schema 2)."""
    if not store.database.exists():
        return []
    with closing(sqlite3.connect(f"file:{store.database}?mode=ro", uri=True)) as db:
        return read_schedule_rows(db)[0]


def _held(store, tenant):
    return sum(_stored(entry["message"]) for entry in store.for_tenant(tenant))


def _put(store, tenant, name, message):
    store.put(_principal(tenant), name=name, session="work", message=message,
              every_ms=EVERY_MS)


def _refusal(store, tenant, name, message):
    with pytest.raises(ProductError) as error:
        _put(store, tenant, name, message)
    return error.value.status, error.value.code, error.value.message


BYTE_REFUSAL = (409, "schedule_quota_exceeded", "tenant schedule byte quota reached")


def test_a_tenant_s_schedules_hold_no_more_than_the_default_byte_quota(tmp_path):
    """The finding: the store took every message the API accepts, and kept a
    control character as six bytes. Messages of 256 KiB of U+0001 take 1.5
    MiB each; five fit in 8 MiB and a sixth is refused."""
    store = ProductScheduleStore(tmp_path / "product-schedules.json")
    message = "\x01" * product_service.MAX_MESSAGE_BYTES
    accepted = 0
    refusal = None
    for index in range(8):
        try:
            _put(store, "tenant-a", f"job{index}", message)
        except ProductError as error:
            refusal = (error.status, error.code, error.message)
            break
        accepted += 1
    held = _held(store, "tenant-a")
    assert held <= DEFAULT_QUOTA, (
        f"one tenant's schedules hold {held / MIB:.1f} MiB of the schedule store, "
        f"charged to no quota: every claim reads and rewrites them (audit F-27)")
    assert (accepted, refusal) == (5, BYTE_REFUSAL)


def test_the_default_quota_still_holds_32_plain_messages_of_the_largest_size(tmp_path):
    """What a tenant could hold before the quota, in plain text, it still can."""
    store = ProductScheduleStore(tmp_path / "product-schedules.json")
    message = "m" * product_service.MAX_MESSAGE_BYTES
    for index in range(32):
        _put(store, "tenant-a", f"job{index}", message)
    assert _held(store, "tenant-a") == DEFAULT_QUOTA
    assert _refusal(store, "tenant-a", "job0", message + "m") == (
        413, "message_too_large", "message exceeds the product limit")
    # A 33rd schedule meets the count quota first, as it did before.
    assert _refusal(store, "tenant-a", "job32", "m") == (
        409, "schedule_quota_exceeded", "tenant schedule quota reached")


def test_a_put_past_the_byte_quota_is_refused_and_changes_nothing(tmp_path):
    store = ProductScheduleStore(tmp_path / "schedules.json", max_bytes_per_tenant=100)
    _put(store, "tenant-a", "one", "a" * 60)
    _put(store, "tenant-a", "two", "b" * 40)              # exactly the quota
    before = _rows(store)
    assert _refusal(store, "tenant-a", "three", "c") == BYTE_REFUSAL
    assert _refusal(store, "tenant-a", "two", "b" * 41) == BYTE_REFUSAL
    assert _rows(store) == before
    # Another tenant's messages are not counted against this one's.
    _put(store, "tenant-b", "one", "z" * 100)
    # A replacement within the quota is accepted, and a removal frees room.
    _put(store, "tenant-a", "two", "d" * 40)
    assert store.remove("tenant-a", "one") is True
    _put(store, "tenant-a", "three", "e" * 60)
    assert {entry["name"]: entry["message"] for entry in store.for_tenant("tenant-a")} == {
        "two": "d" * 40, "three": "e" * 60}
    assert _held(store, "tenant-b") == 100


def test_a_replacement_may_grow_into_the_room_its_old_message_frees(tmp_path):
    """The message a put replaces is not counted beside the new one: a tenant
    holding 60 of its 100 bytes in one schedule may replace it with a message
    of 100 bytes, and one of 101 is refused."""
    store = ProductScheduleStore(tmp_path / "schedules.json", max_bytes_per_tenant=100)
    _put(store, "tenant-a", "job", "a" * 60)
    _put(store, "tenant-a", "job", "b" * 100)             # grows into the room it frees
    assert _held(store, "tenant-a") == 100
    assert _refusal(store, "tenant-a", "job", "c" * 101) == BYTE_REFUSAL
    assert [entry["message"] for entry in store.for_tenant("tenant-a")] == ["b" * 100]


@pytest.mark.parametrize("fits,takes,refused", [
    ('"' * 5, 10, '"' * 6),         # a quote is written as two bytes
    ("\\" * 5, 10, "\\" * 6),       # ... and a backslash
    ("\n" * 5, 10, "\n" * 6),       # a newline as two
    ("\x01", 6, "\x01\x01"),        # another control character as six
    ("é" * 5, 10, "é" * 6),         # UTF-8, which the store writes as it is
    ("x" * 10, 10, "x" * 11),
])
def test_a_message_counts_the_bytes_it_takes_in_the_store(tmp_path, fits, takes, refused):
    """Counted as schema 1's document wrote it, escapes and all: a quota of
    10 bytes takes the first message of each pair and refuses the second. The
    rows keep the message as it is, and beside it the count it was charged."""
    store = ProductScheduleStore(tmp_path / "schedules.json", max_bytes_per_tenant=10)
    assert _refusal(store, "tenant-a", "job", refused) == BYTE_REFUSAL
    _put(store, "tenant-a", "job", fits)
    [row] = _rows(store)
    assert row["message"] == fits and row["message_bytes"] == _stored(fits) == takes
    assert len(json.dumps(fits, ensure_ascii=False).encode()) == takes + 2


def test_a_tenant_over_its_quota_keeps_its_schedules_and_cannot_grow(tmp_path):
    """A store written before there was a quota, or under a larger one, loads
    as it was. Its tenant may replace a schedule with a message no larger than
    the one it held, and may not add or enlarge one until it is under."""
    path = tmp_path / "product-schedules.json"
    roomy = ProductScheduleStore(path, max_bytes_per_tenant=1000)
    for name in ("one", "two", "three"):
        _put(roomy, "tenant-a", name, name[0] * 300)
    store = ProductScheduleStore(path, max_bytes_per_tenant=500)
    assert _held(store, "tenant-a") == 900
    _put(store, "tenant-a", "one", "n" * 300)             # the same size
    _put(store, "tenant-a", "two", "w" * 250)             # smaller
    assert _refusal(store, "tenant-a", "three", "t" * 301) == BYTE_REFUSAL
    assert _refusal(store, "tenant-a", "four", "f") == BYTE_REFUSAL
    assert _held(store, "tenant-a") == 850
    assert store.remove("tenant-a", "three") is True       # 550: still over
    assert _refusal(store, "tenant-a", "four", "f") == BYTE_REFUSAL
    assert store.remove("tenant-a", "two") is True         # 300
    _put(store, "tenant-a", "five", "v" * 200)            # 500: within it
    assert _refusal(store, "tenant-a", "six", "x") == BYTE_REFUSAL
    assert _held(store, "tenant-a") == 500


def test_a_store_refuses_quotas_and_a_floor_that_are_not_positive_integers(tmp_path):
    path = tmp_path / "schedules.json"
    for bad in (0, -1, True, 1.5, "8"):
        with pytest.raises(ConfigError, match="max_schedule_bytes_per_tenant"):
            ProductScheduleStore(path, max_bytes_per_tenant=bad)
    with pytest.raises(ConfigError, match="max_schedules_per_tenant"):
        ProductScheduleStore(path, max_per_tenant=0)
    with pytest.raises(ConfigError, match="min_every_ms"):
        ProductScheduleStore(path, min_every_ms=0)
    assert not path.exists(), "a store refused at construction wrote its file"
    assert ProductScheduleStore(path, max_bytes_per_tenant=1).max_bytes_per_tenant == 1
    store = ProductScheduleStore(path)
    assert store.max_bytes_per_tenant == DEFAULT_QUOTA
    # Built, a store marks its file as schema 2, and writes no row (audit F-27).
    assert path.read_bytes() == SCHEDULE_STORE_MARKER and not store.database.exists()


@pytest.mark.parametrize("overrides,refusal", [
    ({"name": "../job"}, (400, "invalid_schedule_name")),
    ({"message": ""}, (400, "invalid_message")),
    ({"every_ms": True}, (400, "invalid_interval")),
    ({"every_ms": "soon"}, (400, "invalid_interval")),
    ({"every_ms": 0}, (400, "invalid_interval")),
    ({"every_ms": EVERY_MS - 1}, (400, "invalid_interval")),
])
def test_a_put_refused_before_the_quotas_writes_nothing(tmp_path, overrides, refusal):
    store = ProductScheduleStore(tmp_path / "schedules.json", min_every_ms=EVERY_MS,
                                 max_bytes_per_tenant=10)
    values = {"name": "job", "session": "work", "message": "x" * 10, "every_ms": EVERY_MS}
    values.update(overrides)
    with pytest.raises(ProductError) as error:
        store.put(_principal(), **values)
    assert (error.value.status, error.value.code) == refusal
    # No row, nor even the ledger it would be in; the store's marker as built.
    assert not store.database.exists()
    assert store.path.read_bytes() == SCHEDULE_STORE_MARKER


def test_the_schedule_route_answers_a_put_past_the_quota_with_a_409(tmp_path):
    token = "byte-quota-route-token-with-enough-entropy-0123456789abcdef"
    auth = AuthRegistry([{
        "sha256": hashlib.sha256(token.encode()).hexdigest(),
        "principal": "planner", "tenant": "tenant-route",
        "scopes": ["chat", "schedules:read", "schedules:write"], "tools": []}])
    store = ProductScheduleStore(tmp_path / "schedules.json", max_bytes_per_tenant=10)
    service = ProductService(None, auth, schedule_store=store, log_sink=lambda record: None)
    headers = {"Authorization": f"Bearer {token}", "X-Request-ID": "byte-quota-0001"}

    def post(name, message):
        body = json.dumps({"name": name, "session": "work", "message": message,
                           "every_ms": EVERY_MS}).encode()
        status, _, payload = service.dispatch("POST", "/v1/schedules", headers, body)
        return status, payload

    assert post("one", "x" * 10)[0] == 201
    status, payload = post("two", "y")
    assert status == 409
    assert payload == {"error": {"code": "schedule_quota_exceeded",
                                 "message": "tenant schedule byte quota reached"},
                       "request_id": "byte-quota-0001"}
    status, _, listed = service.dispatch("GET", "/v1/schedules", headers)
    assert status == 200 and [entry["name"] for entry in listed["schedules"]] == ["one"]


@pytest.mark.parametrize("configured,quota", [(None, DEFAULT_QUOTA), ("1000", 1000)])
def test_the_product_sets_the_byte_quota_from_its_setting(
        tmp_path, monkeypatch, configured, quota):
    _base_env(tmp_path, monkeypatch)
    if configured is not None:
        monkeypatch.setenv("PI_MAX_SCHEDULE_BYTES_PER_TENANT", configured)
    principal = _principal("tenant")
    seen = {}

    def during(service):
        store = service.schedule_store
        seen["quota"] = getattr(store, "max_bytes_per_tenant", None)
        # Messages of control characters, six bytes each in the store, put
        # until the tenant's next would take it past its quota.
        message = "\x01" * min(quota // 6, product_service.MAX_MESSAGE_BYTES)
        seen["refused"] = None
        for index in range(32):
            try:
                store.put(principal, name=f"job{index}", session="work",
                          message=message, every_ms=EVERY_MS)
            except ProductError as error:
                seen["refused"] = (index, error.code)
                break
        seen["held"] = sum(_stored(entry["message"]) for entry in store.for_tenant("tenant"))

    _install_happy_runtime(monkeypatch, during=during)
    product_main.run()

    assert seen["held"] <= quota, (
        f"the product's store holds {seen['held']} bytes of one tenant's schedule "
        f"messages, past {quota}: they are charged to no quota (audit F-27)")
    assert seen["quota"] == quota
    assert seen["refused"] == ((5, "schedule_quota_exceeded") if configured is None
                               else (1, "schedule_quota_exceeded"))


TENANTS = ("tenant-a", "tenant-b", "tenant-c")
MESSAGES = st.text(alphabet=['a', 'é', '"', "\\", "\n", "\x01", " "],
                   min_size=1, max_size=12)
OPERATIONS = st.lists(st.one_of(
    st.tuples(st.just("put"), st.sampled_from(TENANTS),
              st.sampled_from(["one", "two", "three"]), MESSAGES),
    st.tuples(st.just("remove"), st.sampled_from(TENANTS),
              st.sampled_from(["one", "two", "three"]), st.just(""))),
    min_size=1, max_size=14)


def test_no_sequence_of_puts_and_removals_takes_a_tenant_past_its_byte_quota():
    """The class: whatever a tenant puts and removes, its messages take no
    more than its quota in the store, each put is refused exactly when it
    would take them past it, and what is refused changes nothing."""

    @settings(max_examples=60, deadline=None)
    @given(quota=st.integers(min_value=6, max_value=30), operations=OPERATIONS)
    @example(quota=6, operations=[("put", "tenant-a", "one", "\x01"),
                                  ("put", "tenant-a", "two", "a")])
    def run(quota, operations):
        with tempfile.TemporaryDirectory() as directory:
            store = ProductScheduleStore(Path(directory) / "schedules.json",
                                         max_bytes_per_tenant=quota)
            model = {tenant: {} for tenant in TENANTS}
            for action, tenant, name, message in operations:
                mine = model[tenant]
                if action == "remove":
                    assert store.remove(tenant, name) is (mine.pop(name, None) is not None)
                    continue
                after = sum(_stored(held) for other, held in mine.items()
                            if other != name) + _stored(message)
                before = _rows(store)
                if after > quota:
                    assert _refusal(store, tenant, name, message) == BYTE_REFUSAL
                    assert _rows(store) == before
                else:
                    _put(store, tenant, name, message)
                    mine[name] = message
                for each in TENANTS:
                    assert _held(store, each) == sum(map(_stored, model[each].values()))
                    assert _held(store, each) <= quota

    run()


# ── the product's own stores ─────────────────────────────────────────────

PRODUCT_SOURCES = sorted([*PI_ROOT.glob("*.py"), *(PI_ROOT / "scripts").glob("*.py")])


def _unbounded_store_constructions(source, filename):
    """Each ProductScheduleStore(...) built in `source` without the interval
    floor or a byte quota, as "file:line"."""
    found = []
    for node in ast.walk(ast.parse(source, filename)):
        if not isinstance(node, ast.Call):
            continue
        called = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if called != "ProductScheduleStore":
            continue
        keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in node.keywords}
        if (keywords.get("min_every_ms") != "MIN_SCHEDULE_EVERY_MS"
                or "max_bytes_per_tenant" not in keywords):
            found.append(f"{filename}:{node.lineno}")
    return found


def test_every_schedule_store_the_product_builds_has_the_floor_and_a_byte_quota():
    """The store's own interval floor stays the permissive 1 ms, because many
    tests build stores with its defaults and schedules due every second
    (tests/test_failure_injection.py among them); so each store the product
    itself builds must pass the floor, and a byte quota."""
    built = [path.name for path in PRODUCT_SOURCES
             if "ProductScheduleStore(" in path.read_text()]
    assert "product_main.py" in built, built
    unbounded = [site for path in PRODUCT_SOURCES
                 for site in _unbounded_store_constructions(path.read_text(), path.name)]
    assert unbounded == []


@pytest.mark.parametrize("call,caught", [
    ("ProductScheduleStore(path)", True),
    ("ProductScheduleStore(path, min_every_ms=MIN_SCHEDULE_EVERY_MS)", True),
    ("product_service.ProductScheduleStore(path, max_bytes_per_tenant=quota)", True),
    ("ProductScheduleStore(path, min_every_ms=1, max_bytes_per_tenant=quota)", True),
    ("ProductScheduleStore(path, min_every_ms=MIN_SCHEDULE_EVERY_MS, "
     "max_bytes_per_tenant=quota)", False),
    ("OtherStore(path)", False),
])
def test_the_store_guard_finds_a_store_built_without_its_bounds(call, caught):
    assert bool(_unbounded_store_constructions(f"store = {call}\n", "probe.py")) is caught
