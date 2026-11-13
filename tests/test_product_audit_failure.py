"""A product turn fails closed when its audit record cannot be written.

Audit 2026-11-10, F-04, the product half. A1 gave agent.py a strict audit
log: `AuditLog(strict=True)` fails the forge whose record cannot be appended
with `AuditWriteError`, after its guest has run, and withholds the result.
The product host did not turn it on. The reproduction
(audit/repro/r4_r5_real_agent.py, part R5a) made a session's chain
unwritable and sent one more turn: HTTP 200 with the reply, two guests run,
zero records, two tracebacks on stderr, no metric and no event. With the log
strict but the product unchanged, the same turn answered `502
agent_failure`, which blames the model provider, a scheduled turn was stored
as "failed", and still nothing was counted or raised.

Pinned here, over the REAL PiAgent with the log the product builds, a
compiler double that answers every guest, and the real ProductService,
ProductScheduler and AuditVerificationMonitor:

- `product_main.run()`, booted as an operator starts it, builds its audit
  log strict and hands its audit monitor to the scheduler as well as to the
  service: a chat turn and a scheduled turn whose records cannot be written
  both fail closed;
- a chat turn answers the stable `500 audit_write_failed` and never a reply,
  whatever else failed while the turn unwound; a scheduled run is recorded
  `audit_write_failed`;
- either way the audit monitor counts it (`write_failures_total`), readiness
  fails through `audit_verification` and stays failed until the process is
  replaced, a content-free critical `audit_write` event is logged, and the
  policy's `audit_write_failed` alert fires, while the tampering alert stays
  quiet;
- the tenant is charged what the agent counted before the record failed;
- by syntax and by property: the product builds its log strict by a literal,
  every product path that runs a turn classifies an audit failure before
  anything else, and the classification finds one under any number of later
  errors.
"""
import ast
import errno
import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given, settings, strategies as st

from conftest import PI_ROOT

import product_main
import product_service
from agent import AuditLog, AuditWriteError, PiAgent, SessionStore, verify_audit_dir
from product_service import (
    AuditVerificationMonitor, AuthRegistry, DurableQuotaStore, ProductDataManager,
    ProductError, ProductScheduler, ProductScheduleStore, ProductService,
    SessionOperationLocks, _internal_session)

KEY = b"product-audit-failure-test-key-0123456789"
CHAT_TOKEN = "chat-token-for-the-product-audit-failure-tests-0123"
OPS_TOKEN = "ops-token-for-the-product-audit-failure-tests-01234"
TENANT = "tenant-a"
AUDIT_WRITE_FAILED = {"code": "audit_write_failed",
                      "message": "turn audit record could not be written"}
# The probe tool is answered by the compiler double and never compiled.
TOOL_SRC = "// probe tool: answered by the compiler double, never compiled\n"
USAGE = (100, 10)       # what every parse guest reports: input, output tokens


def frame(tag, payload):
    """One bridge frame, as parse_reply emits it (see agent.decode_frames)."""
    return f"{tag}{len(payload.encode()):08d}{payload}"


class Compiler:
    """The compiler, for the real PiAgent and as product_main spawns it.

    It answers the llm guest with an opaque body, each parse guest with the
    next queued reply (a probe tool call when `tool_calls` is set) or else
    the text "hi", always with a usage frame of USAGE, and the probe tool with
    its output. `ran` lists every guest that executed, by kind, so a test can
    count the guests that ran against the records that were written."""

    is_healthy = True
    _proc = SimpleNamespace(pid=os.getpid(), kill=lambda: None)

    def __init__(self, tool_calls=0):
        self.ran = []
        self.tool_calls = tool_calls
        self._lock = threading.Lock()

    def initialize(self, timeout_s=None):
        return {}

    def close(self):
        pass

    def forge(self, source, *, input="", fuel=0, grants=None, timeout_s=None):
        kind = {"LLM": "llm", "PARSE": "parse", TOOL_SRC: "tool"}[source]
        with self._lock:
            self.ran.append(kind)
            call = kind == "parse" and self.tool_calls > 0
            if call:
                self.tool_calls -= 1
            number = len(self.ran)
        if kind == "parse":
            # Content frames first, then usage: parse_reply's order.
            out = (frame("u", f"tu{number}\x1fprobe\x1f" + '{"arg":"x"}') if call
                   else frame("t", "hi"))
            out += frame("g", "%d|%d" % USAGE)
        else:
            out = {"llm": "{}", "tool": "probed"}[kind]
        return {"status": "ok", "data": {"output_text": out}}


def entry(token, principal, tenant, scopes, tools=()):
    return {"sha256": hashlib.sha256(token.encode()).hexdigest(),
            "principal": principal, "tenant": tenant,
            "scopes": list(scopes), "tools": list(tools)}


def strict_agent(root, compiler):
    """The real PiAgent with the audit log the product builds, a one-tool
    manifest, and its two composed guest sources assigned, so that no
    standard library and no toolchain is needed."""
    root = Path(root)
    for name in ("sessions", "sandboxes", "audit", "probe-tools"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "probe-tools" / "probe.sigil").write_text(TOOL_SRC)
    (root / "manifest.json").write_text(json.dumps({"probe": {
        # absolute: dispatch joins it onto PI_ROOT, which keeps it
        "source": str(root / "probe-tools" / "probe.sigil"),
        "args": ["arg"], "path_args": [], "grants": {},
        "spec": {"name": "probe", "description": "probe",
                 "input_schema": {"type": "object",
                                  "properties": {"arg": {"type": "string"}},
                                  "required": ["arg"]}},
    }}))
    host = PiAgent("https://example.invalid/v1/messages", "sk-test-not-a-real-key",
                   store=SessionStore(root / "sessions"),
                   sandbox_root=root / "sandboxes",
                   manifest_path=root / "manifest.json", mcp=compiler,
                   audit=AuditLog(root / "audit", key=KEY, strict=True),
                   memory=None)
    host._llm_src, host._parse_src = "LLM", "PARSE"
    return host


class Product:
    """The product's components over one state directory, wired as
    product_main wires them: one audit monitor, read by readiness and the
    metrics, and told by the service (and the scheduler, when a test builds
    one with `scheduler()`) about every record that could not be written."""

    def __init__(self, root, compiler=None):
        root = Path(root)
        self.root = root
        self.compiler = compiler or Compiler()
        self.agent = strict_agent(root, self.compiler)
        self.auth = AuthRegistry([
            entry(CHAT_TOKEN, "alice", TENANT,
                  ["chat", "schedules:read", "schedules:write"], ["probe"]),
            entry(OPS_TOKEN, "operator", "operations", ["ops:read"]),
        ])
        self.locks = SessionOperationLocks(root / "locks")
        self.quota = DurableQuotaStore(
            root / "product-quotas.sqlite3", requests_per_minute=10_000,
            tokens_per_day=10_000_000, token_reservation_per_turn=1_000,
            storage_bytes_per_tenant=1 << 30, storage_reservation_per_turn=1 << 20,
            audit_bytes_per_tenant=1 << 28, audit_reservation_per_turn=1 << 16)
        self.schedules = ProductScheduleStore(root / "product-schedules.json")
        self.data = ProductDataManager(
            self.agent, self.schedules, self.locks, quota_store=self.quota)
        self.events = []
        self.monitor = AuditVerificationMonitor(
            lambda: verify_audit_dir(root / "audit", key=KEY),
            log_sink=self.events.append)
        assert self.monitor.check() is True
        self.requests = []
        self.service = ProductService(
            self.agent, self.auth, schedule_store=self.schedules,
            session_locks=self.locks, data_manager=self.data,
            quota_store=self.quota, audit_monitor=self.monitor,
            log_sink=self.requests.append)

    def scheduler(self):
        return ProductScheduler(
            self.schedules, self.agent, self.auth, session_locks=self.locks,
            quota_store=self.quota, data_manager=self.data,
            audit_monitor=self.monitor, log_sink=self.events.append)

    def chat(self, session, message="hello"):
        status, _, payload = self.service.dispatch(
            "POST", "/v1/chat", {"Authorization": f"Bearer {CHAT_TOKEN}"},
            json.dumps({"session": session, "message": message}).encode())
        return status, payload

    def get(self, path):
        status, _, payload = self.service.dispatch(
            "GET", path, {"Authorization": f"Bearer {OPS_TOKEN}"})
        return status, payload

    def metrics(self):
        status, payload = self.get("/v1/metrics")
        assert status == 200, payload
        return payload["metrics"]

    def chain(self, session):
        return self.agent.audit._path(_internal_session(TENANT, session))

    def write_events(self):
        return [event for event in self.events if event.get("event") == "audit_write"]


def full_disk(monkeypatch, chain=None):
    """Audit appends fail as on a full disk, before any byte is written.
    Every other write, and every read, works."""
    del chain
    real_open = Path.open

    def open_(path, mode="r", *args, **kwargs):
        if "a" in mode and path.suffix == ".jsonl":
            raise OSError(errno.ENOSPC, "No space left on device (injected)")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_)


def torn_tail(monkeypatch, chain):
    """An earlier append was cut short: the chain ends in half a record, and
    the log refuses to chain onto it."""
    del monkeypatch
    with chain.open("ab") as stream:
        stream.write(b'{"seq":7,"kind":"ll')


def not_a_file(monkeypatch, chain):
    """A directory is where the chain was."""
    del monkeypatch
    chain.unlink()
    chain.mkdir()


FAULTS = {"full_disk": full_disk, "torn_tail": torn_tail, "not_a_file": not_a_file}


def state_of(path):
    if path.is_dir():
        return ("directory", sorted(child.name for child in path.iterdir()))
    return ("file", path.read_bytes())


def fail_append_number(log, n):
    """The n-th append from now on (0-based) fails as a full disk does,
    before any byte is written. Every other append is real."""
    real = log.append_or_raise
    calls = iter(range(1 << 30))

    def append(*args, **kwargs):
        if next(calls) == n:
            raise OSError(errno.ENOSPC, "No space left on device (injected)")
        return real(*args, **kwargs)

    log.append_or_raise = append


def policy_alert(alert_id):
    policy = json.loads((PI_ROOT / "config" / "alert-policy.json").read_text())
    [alert] = [alert for alert in policy["alerts"] if alert["id"] == alert_id]
    return alert


def fires(alert_id, metrics):
    """Evaluate a policy ``equals`` condition against a metrics document."""
    condition = policy_alert(alert_id)["condition"]
    assert condition["operator"] == "equals"
    value = metrics
    for part in condition["path"].split("."):
        value = value[part]
    return value == condition["value"]


def assert_content_free(product, *secrets):
    logged = json.dumps([product.events, product.requests])
    for secret in (*secrets, TENANT, "Errno", "injected", "No space",
                   "Traceback", str(product.root)):
        assert secret not in logged, f"{secret!r} reached a log line"


# ── the chat path ────────────────────────────────────────────────────────


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_a_chat_turn_whose_record_cannot_be_written_fails_closed(
        tmp_path, monkeypatch, capsys, fault):
    """R5a, reversed. The turn's first guest (the model call) runs, its
    record cannot be appended, and the turn ends there: no reply and no
    success, a stable error, the monitor counting it, readiness failed, one
    critical event, and nothing on stderr."""
    product = Product(tmp_path)
    assert product.chat("work")[0] == 200
    chain = product.chain("work")
    FAULTS[fault](monkeypatch, chain)
    before, ran = state_of(chain), len(product.compiler.ran)

    status, payload = product.chat("work", "the turn nothing can record")

    assert (status, payload.get("error")) == (500, AUDIT_WRITE_FAILED), (
        f"a turn whose audit record could not be written answered {status} "
        f"{payload}: it must fail with the stable audit_write_failed error "
        f"(audit F-04)")
    assert set(payload) == {"error", "request_id"}
    assert product.compiler.ran[ran:] == ["llm"], "a guest ran after the failed record"
    assert state_of(chain) == before, "the failed turn changed the chain"

    ready_status, ready = product.get("/v1/ready")
    assert ready_status == 503 and ready["status"] == "not_ready"
    assert ready["dependencies"] == {
        "runtime": True, "quota_store": True, "schedule_store": True,
        "state_storage": True, "audit_verification": False}
    metrics = product.metrics()
    assert metrics["audit_verification"]["write_failures_total"] == 1
    assert metrics["audit_verification"]["writes_healthy"] is False
    assert metrics["dependency_components"]["audit_verification"] is False
    assert metrics["error_classes"] == {"audit_write_failed": 1}
    assert metrics["turn_errors_total"] == 1
    assert product.write_events() == [{
        "event": "audit_write", "severity": "critical", "status": "failed",
        "turn": "chat"}]
    [request] = [line for line in product.requests
                 if line["route"] == "/v1/chat" and line["status"] != 200]
    assert (request["status"], request["failure_classification"]) == (
        500, "audit_write_failed")
    assert "Traceback" not in capsys.readouterr().err
    assert_content_free(product, "work", "the turn nothing can record", chain.name)


def test_the_prometheus_form_carries_the_count_and_the_failed_component(
        tmp_path, monkeypatch):
    product = Product(tmp_path)
    full_disk(monkeypatch)
    assert product.chat("work")[0] == 500
    status, _, payload = product.service.dispatch(
        "GET", "/v1/metrics/prometheus", {"Authorization": f"Bearer {OPS_TOKEN}"})
    assert status == 200
    lines = set(payload.body.splitlines())
    assert {"sigil_pi_audit_verification_write_failures_total 1",
            "sigil_pi_audit_verification_writes_healthy 0",
            'sigil_pi_dependency_component_ready{dependency="audit_verification"} 0',
            'sigil_pi_http_errors_total{class="audit_write_failed"} 1'} <= lines


def test_an_audit_failure_is_not_replaced_by_the_settlement_that_follows_it(
        tmp_path, monkeypatch):
    """A1's note for this node: the chat handler settles the turn in a
    `finally`, reading the session's footprint, and an error raised there
    while the AuditWriteError propagated replaced it. The tenant then got
    `500 internal_error` and nothing was counted. An I/O error on the volume
    can fail both."""
    product = Product(tmp_path)
    assert product.chat("work")[0] == 200
    full_disk(monkeypatch)

    def footprint(internal_session):
        raise OSError(errno.EIO, "Input/output error (injected)")

    monkeypatch.setattr(product.data, "footprint", footprint)
    status, payload = product.chat("work")
    assert (status, payload.get("error")) == (500, AUDIT_WRITE_FAILED), payload
    assert product.monitor.snapshot()["write_failures_total"] == 1
    assert product.service.is_ready() is False


def test_an_audit_failure_keeps_its_answer_when_the_quota_store_fails_too(
        tmp_path, monkeypatch):
    """Found in this node's sweep. A full disk fails SQLite as well ("database
    or disk is full"), so the settlement under the session lock raises, and
    the request's last `finally` tries the same release again, which raises
    again and replaced the stable error on its way out: the tenant got `500
    internal_error`. The failure is still answered as what it is, and
    counted once."""
    product = Product(tmp_path)
    assert product.chat("work")[0] == 200
    full_disk(monkeypatch)

    def settle_turn(*args, **kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(product.quota, "settle_turn", settle_turn)
    status, payload = product.chat("work")
    assert (status, payload.get("error")) == (500, AUDIT_WRITE_FAILED), payload
    assert product.monitor.snapshot()["write_failures_total"] == 1
    assert len(product.write_events()) == 1
    assert product.metrics()["error_classes"] == {"audit_write_failed": 1}


def test_an_error_with_no_audit_failure_behind_it_is_still_internal(
        tmp_path, monkeypatch):
    """The other side of the test above: the same failing settlement with
    every record written is an internal error, and nothing is counted."""
    product = Product(tmp_path)

    def settle_turn(*args, **kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(product.quota, "settle_turn", settle_turn)
    status, payload = product.chat("work")
    assert (status, payload["error"]["code"]) == (500, "internal_error"), payload
    assert product.monitor.snapshot()["write_failures_total"] == 0
    assert product.service.is_ready() is True


@pytest.mark.parametrize("tool_calls, failing_append, charged", [
    pytest.param(0, 0, 0, id="the-first-model-call-record"),
    pytest.param(2, 5, sum(USAGE), id="the-second-step-tool-record"),
])
def test_a_turn_that_fails_on_its_record_is_charged_what_the_agent_counted(
        tmp_path, tool_calls, failing_append, charged):
    """P3's rule holds on this way out: the agent was asked, so the tenant is
    charged what the agent counted, and the reservation is released. That is
    every step completed before the step whose record failed. A model call
    whose own record fails (append 0) never reaches its parse guest, so its
    tokens are never counted (A1's note). A step's usage frame comes after
    its tool calls and is counted after they run, so when the second step's
    tool record fails (append 5), only the first step is charged. The
    session's storage and audit bytes are settled at their real size."""
    product = Product(tmp_path, Compiler(tool_calls=tool_calls))
    fail_append_number(product.agent.audit, failing_append)
    status, payload = product.chat("work")
    assert (status, payload.get("error")) == (500, AUDIT_WRITE_FAILED), payload
    usage = product.quota.tenant_usage(TENANT)
    assert usage["tokens_today"] == charged
    assert usage["reserved_tokens"] == 0
    chain = product.chain("work")
    assert usage["audit_bytes"] == (chain.stat().st_size if chain.exists() else 0)
    assert product.metrics()["input_tokens_total"] == (USAGE[0] if charged else 0)


def test_readiness_stays_failed_until_the_process_is_replaced(tmp_path, monkeypatch):
    """The disk has room again and turns are recorded, and a verification
    passes, because the chain a failed append leaves is a valid prefix. None
    of that shows that every execution since the failure was recorded, so
    the monitor stays unhealthy. A replacement starts healthy: product
    startup verifies every chain before it serves."""
    product = Product(tmp_path)
    assert product.chat("work")[0] == 200
    with monkeypatch.context() as disk:
        full_disk(disk)
        assert product.chat("work")[0] == 500
    assert product.chat("work")[0] == 200
    assert product.monitor.check() is True
    snapshot = product.monitor.snapshot()
    assert snapshot["healthy"] is True
    assert (snapshot["writes_healthy"], snapshot["write_failures_total"]) == (False, 1)
    assert product.service.readiness_snapshot()["dependencies"]["audit_verification"] is False
    assert product.get("/v1/ready")[0] == 503

    replacement = AuditVerificationMonitor(
        lambda: verify_audit_dir(tmp_path / "audit", key=KEY), log_sink=lambda line: None)
    assert replacement.check() is True and replacement.is_healthy() is True
    assert replacement.snapshot()["write_failures_total"] == 0


def test_the_alert_fires_on_a_write_failure_and_the_tampering_alert_does_not(
        tmp_path, monkeypatch):
    """The policy's own conditions, read from the metrics the product
    serves. A full disk is not tampering: the chains still verify, so the
    security alert for a broken chain stays quiet, and the write failure has
    its own alert and runbook."""
    product = Product(tmp_path)
    assert product.chat("work")[0] == 200
    quiet = product.metrics()
    assert not fires("audit_write_failed", quiet)
    assert not fires("service_not_ready", quiet)

    full_disk(monkeypatch)
    assert product.chat("work")[0] == 500
    loud = product.metrics()
    assert fires("audit_write_failed", loud)
    assert fires("service_not_ready", loud)
    assert not fires("audit_verification_failed", loud)
    alert = policy_alert("audit_write_failed")
    assert (alert["severity"], alert["for_seconds"]) == ("critical", 0)
    assert alert["runbook"] == "docs/runbooks.md#audit-write-failure"


def test_each_failed_record_is_counted_and_logged(tmp_path, monkeypatch):
    product = Product(tmp_path)
    full_disk(monkeypatch)
    for session in ("one", "two", "three"):
        assert product.chat(session)[0] == 500
    assert product.monitor.snapshot()["write_failures_total"] == 3
    assert len(product.write_events()) == 3


def test_a_service_without_an_audit_monitor_still_fails_the_turn_closed(
        tmp_path, monkeypatch):
    """A library user may build the service with no audit monitor. Its turn
    still fails with the stable error, which the metrics count by class;
    there is no audit component to fail and no monitor to tell."""
    product = Product(tmp_path)
    service = ProductService(product.agent, product.auth, log_sink=lambda line: None)
    full_disk(monkeypatch)
    status, _, payload = service.dispatch(
        "POST", "/v1/chat", {"Authorization": f"Bearer {CHAT_TOKEN}"},
        json.dumps({"session": "work", "message": "hello"}).encode())
    assert (status, payload.get("error")) == (500, AUDIT_WRITE_FAILED), payload
    assert service.metrics.snapshot()["error_classes"] == {"audit_write_failed": 1}
    assert service.readiness_snapshot()["dependencies"] == {"runtime": True}


def test_a_monitor_whose_log_sink_fails_still_counts_and_fails_readiness():
    """The event goes to stdout, which a full disk can fail too. The count
    and the verdict are kept first, and the sink's error does not replace
    the turn's stable error."""
    def broken_sink(record):
        raise OSError(errno.ENOSPC, "No space left on device")

    monitor = AuditVerificationMonitor(
        lambda: {"ok": True, "chains": 0, "records": 0}, log_sink=broken_sink)
    assert monitor.check() is True
    monitor.write_failed(turn="chat")
    assert monitor.snapshot()["write_failures_total"] == 1
    assert monitor.is_healthy() is False


# ── the scheduled path ───────────────────────────────────────────────────


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_a_scheduled_turn_whose_record_cannot_be_written_fails_closed(
        tmp_path, monkeypatch, fault):
    """The same failure on a schedule. The run is recorded with the same
    stable error, the monitor counts it, readiness fails, and the event
    names the scheduled path."""
    product = Product(tmp_path)
    assert product.chat("work")[0] == 200
    scheduler = product.scheduler()
    principal = product.auth.authenticate(f"Bearer {CHAT_TOKEN}")
    product.schedules.put(principal, name="nightly", session="work",
                          message="summarize the night", every_ms=3_600_000)
    FAULTS[fault](monkeypatch, product.chain("work"))
    ran = len(product.compiler.ran)

    scheduler.tick()

    [entry_] = product.schedules.for_tenant(TENANT)
    assert entry_["last_status"] == "audit_write_failed", (
        f"a scheduled turn whose audit record could not be written was "
        f"recorded {entry_['last_status']!r} (audit F-04)")
    assert product.compiler.ran[ran:] == ["llm"]
    assert product.monitor.snapshot()["write_failures_total"] == 1
    assert product.service.readiness_snapshot()["dependencies"]["audit_verification"] is False
    assert product.write_events() == [{
        "event": "audit_write", "severity": "critical", "status": "failed",
        "turn": "scheduled"}]
    assert_content_free(product, "work", "summarize the night", "nightly")


def test_a_scheduled_audit_failure_is_not_replaced_by_its_settlement(
        tmp_path, monkeypatch):
    product = Product(tmp_path)
    scheduler = product.scheduler()
    principal = product.auth.authenticate(f"Bearer {CHAT_TOKEN}")
    product.schedules.put(principal, name="nightly", session="work",
                          message="summarize the night", every_ms=3_600_000)
    full_disk(monkeypatch)

    def footprint(internal_session):
        raise OSError(errno.EIO, "Input/output error (injected)")

    monkeypatch.setattr(product.data, "footprint", footprint)
    scheduler.tick()
    [entry_] = product.schedules.for_tenant(TENANT)
    assert entry_["last_status"] == "audit_write_failed"
    assert product.monitor.snapshot()["write_failures_total"] == 1


# ── the product host, booted as an operator starts it ───────────────────


def boot(tmp_path, monkeypatch, during):
    """``product_main.run()`` with its own agent, log, monitors, stores,
    scheduler and service, over the compiler double; only the listener, the
    toolchain lookup and the stop signal are doubles, and the agent's two
    composed guest sources are assigned. ``during(booted)`` runs where run()
    waits for its signal, with everything started; the product then drains
    and stops, and an assertion that failed in ``during`` is raised after."""
    now = int(time.time())
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": [
        {**entry(OPS_TOKEN, "operator", "operations", ["ops:read"]),
         "not_before_unix": now - 60, "expires_unix": now + 86400},
        {**entry(CHAT_TOKEN, "alice", TENANT,
                 ["chat", "schedules:read", "schedules:write"]),
         "not_before_unix": now - 60, "expires_unix": now + 86400},
    ]}))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "provider-key-that-is-never-used")
    monkeypatch.setenv("PI_AUDIT_KEY", "product-audit-failure-boot-key-0123456789")
    monkeypatch.setenv("PI_AUTH_FILE", str(auth))
    monkeypatch.setenv("PI_STATE", str(tmp_path / "state"))
    booted = SimpleNamespace(service=None, compiler=Compiler(), result=None, error=None)

    class Agent(PiAgent):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._llm_src, self._parse_src = "LLM", "PARSE"

    class Scheduler(ProductScheduler):
        """The product's own scheduler, looking every 10 ms instead of every
        second, so that the test does not wait on its cadence."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, interval_s=0.01, **kwargs)

    class Signal:
        def set(self):
            pass

        def wait(self, timeout=None):
            try:
                booted.result = during(booted)
            except BaseException as error:  # noqa: BLE001  re-raised after the drain
                booted.error = error
            return True

    def serve(service, **kwargs):
        booted.service = service
        return SimpleNamespace(shutdown=lambda: None, server_close=lambda: None)

    monkeypatch.setattr(product_main, "threading", SimpleNamespace(Event=Signal))
    monkeypatch.setattr(
        product_main, "ProductionSigilMCP",
        SimpleNamespace(spawn=lambda path, timeout_s=90.0, generation=0: booted.compiler))
    monkeypatch.setattr(
        product_main.toolchain, "resolve",
        lambda require=True: SimpleNamespace(forge_bin=Path("/nonexistent/sigil-mcp"),
                                             origin="test double"))
    monkeypatch.setattr(product_main.toolchain, "verify_binary", lambda path, rev=None: None)
    monkeypatch.setattr(product_main, "PiAgent", Agent)
    monkeypatch.setattr(product_main, "ProductScheduler", Scheduler)
    monkeypatch.setattr(product_main, "serve_product", serve)
    monkeypatch.setattr(product_main, "load_system_prompt", lambda *args: None)
    product_main.run()
    if booted.error is not None:
        raise booted.error
    return booted


def _dispatch(service, method, path, token, body=None):
    raw = b"" if body is None else json.dumps(body).encode()
    status, _, payload = service.dispatch(
        method, path, {"Authorization": f"Bearer {token}"}, raw)
    return status, payload


def test_the_product_host_fails_chat_and_scheduled_turns_closed(
        tmp_path, monkeypatch, capsys):
    """End to end through product_main.run(). Before, its log was not
    strict: the chat turn answered 200 with its reply and no record."""

    def during(booted):
        service = booted.service
        assert service.agent.audit.strict is True, (
            "product_main.run() built a non-strict audit log (audit F-04)")
        assert _dispatch(service, "POST", "/v1/chat", CHAT_TOKEN,
                         {"session": "work", "message": "hello"})[0] == 200
        with monkeypatch.context() as disk:
            full_disk(disk)
            status, payload = _dispatch(service, "POST", "/v1/chat", CHAT_TOKEN,
                                        {"session": "work", "message": "hello"})
            assert (status, payload.get("error")) == (500, AUDIT_WRITE_FAILED), payload
            assert _dispatch(service, "POST", "/v1/schedules", CHAT_TOKEN, {
                "name": "nightly", "session": "night", "message": "hello",
                "every_ms": 3_600_000})[0] == 201
            deadline = time.monotonic() + 10
            while True:
                [listed] = _dispatch(service, "GET", "/v1/schedules",
                                     CHAT_TOKEN)[1]["schedules"]
                if listed["last_status"] is not None:
                    break
                assert time.monotonic() < deadline, "the scheduled turn never ran"
                time.sleep(0.01)
        assert listed["last_status"] == "audit_write_failed"
        ready_status, ready = _dispatch(service, "GET", "/v1/ready", OPS_TOKEN)
        metrics = _dispatch(service, "GET", "/v1/metrics", OPS_TOKEN)[1]["metrics"]
        return ready_status, ready, metrics

    booted = boot(tmp_path, monkeypatch, during)
    ready_status, ready, metrics = booted.result
    assert ready_status == 503 and ready["dependencies"]["audit_verification"] is False
    assert {name for name, ok in ready["dependencies"].items() if not ok} == {
        "audit_verification"}
    assert metrics["audit_verification"]["write_failures_total"] == 2
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()
              if line.startswith("{")]
    assert [event["turn"] for event in events if event.get("event") == "audit_write"] == [
        "chat", "scheduled"]


# ── guards ───────────────────────────────────────────────────────────────


def _callee(call):
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def test_the_product_host_builds_every_audit_log_strict_by_a_literal():
    """Strict mode is not an operator choice for the product: every AuditLog
    product_main.py builds says `strict=True` itself, so no setting, default
    or forwarded keyword can turn it off."""
    tree = ast.parse((PI_ROOT / "product_main.py").read_text())
    logs = [node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _callee(node) == "AuditLog"]
    assert logs, "product_main.py builds no AuditLog: did the product's log move?"
    for call in logs:
        strict = [keyword for keyword in call.keywords if keyword.arg == "strict"]
        assert (len(strict) == 1 and isinstance(strict[0].value, ast.Constant)
                and strict[0].value.value is True
                and not any(keyword.arg is None for keyword in call.keywords)), (
            f"product_main.py:{call.lineno} builds an AuditLog that is not strict "
            f"by a literal True (audit F-04)")


def _turn_calls_and_their_handlers(source):
    """Each call to ``turn_with_usage`` in a module, with the first handler
    of the innermost ``try`` around it that has handlers (None if none)."""
    tree = ast.parse(source)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _callee(node) == "turn_with_usage"):
            continue
        handler, child, parent = None, node, parents.get(node)
        while parent is not None:
            if (isinstance(parent, ast.Try) and parent.handlers
                    and any(child is statement for statement in parent.body)):
                handler = parent.handlers[0]
                break
            child, parent = parent, parents.get(parent)
        found.append((node.lineno, handler))
    return found


def test_every_product_path_that_runs_a_turn_classifies_audit_failures_first():
    """A turn's failure is mapped to what the tenant or the schedule sees in
    one handler, and an audit record that could not be written outranks
    everything else in it: a deadline, a model failure, a quota refusal, or
    the settlement that failed on the same disk. So the handler around every
    agent turn the product runs catches Exception, and its first statement
    asks `_audit_write_failure`. A new path that runs a turn without that
    handler fails here."""
    calls = _turn_calls_and_their_handlers(
        (PI_ROOT / "product_service.py").read_text())
    assert len(calls) >= 2, calls
    for lineno, handler in calls:
        assert handler is not None, (
            f"product_service.py:{lineno} runs a turn outside any handler")
        first = handler.body[0]
        assert (isinstance(handler.type, ast.Name) and handler.type.id == "Exception"
                and handler.name is not None
                and isinstance(first, ast.If) and isinstance(first.test, ast.Call)
                and _callee(first.test) == "_audit_write_failure"
                and [ast.dump(arg) for arg in first.test.args]
                == [ast.dump(ast.Name(id=handler.name, ctx=ast.Load()))]), (
            f"product_service.py:{lineno} runs a turn whose failure is not "
            f"first checked for an audit record that could not be written "
            f"(audit F-04)")
    assert ("turn_with_usage" not in (PI_ROOT / "product_main.py").read_text()), (
        "product_main.py runs a turn itself")


ERRORS = (
    RuntimeError, TimeoutError, OSError, ValueError, sqlite3.OperationalError,
    lambda message: ProductError(500, "internal_error", message),
    AuditWriteError,
)


def _escaped(kinds, links):
    """Raise ERRORS[kinds[0]], then each next one while handling the one
    before, as a `finally` or a handler does while a turn unwinds: plainly,
    `from` the one before, or `from None`. Return what escapes."""
    def make(index):
        return ERRORS[kinds[index]](f"error {index}")

    def nested(index):
        if index == 0:
            raise make(0)
        try:
            nested(index - 1)
        except BaseException as below:
            if links[index] == "from":
                raise make(index) from below
            if links[index] == "from None":
                raise make(index) from None
            raise make(index)

    try:
        nested(len(kinds) - 1)
    except BaseException as error:  # noqa: BLE001
        return error
    raise AssertionError("nothing escaped")


@settings(max_examples=300, deadline=None)
@given(st.lists(st.integers(0, len(ERRORS) - 1), min_size=1, max_size=6).flatmap(
    lambda kinds: st.tuples(
        st.just(kinds),
        st.lists(st.sampled_from(("plain", "from", "from None")),
                 min_size=len(kinds), max_size=len(kinds)))))
def test_an_audit_failure_is_found_under_any_errors_raised_after_it(drawn):
    kinds, links = drawn
    expected = len(ERRORS) - 1 in kinds
    assert product_service._audit_write_failure(_escaped(kinds, links)) is expected


def test_the_classification_ends_on_a_cycle():
    first, second = RuntimeError("a"), OSError("b")
    first.__context__, second.__context__ = second, first
    assert product_service._audit_write_failure(first) is False
    second.__cause__ = AuditWriteError("c")
    assert product_service._audit_write_failure(first) is True
