"""The product server over real sockets, plain and TLS (audit 2026-11-10, F-17).

Every other product test drives ProductService.dispatch() directly, so until
this file the transport itself ran in no test at all: reading a POST body, the
DELETE handler, refusing a bad Content-Length, and the TLS listener with its
context. These tests pin what the server does today, over real connections
made by conftest's product_server harness, so that the transport changes
planned next (S1 to S3 of the remediation graph) are measured against them.
"""

import hashlib
import inspect
import json
import ssl
import time

import pytest
from hypothesis import example, given, settings, strategies as st

from conftest import http_request_bytes, make_tls_material, read_reply

import product_main
from product_service import (
    MAX_MESSAGE_BYTES,
    MAX_PRODUCT_REQUEST_BYTES,
    SESSION_RE,
    AuthRegistry,
    ConfigError,
    ProductDataManager,
    ProductScheduleStore,
    ProductService,
    SessionOperationLocks,
    _internal_session,
    tls_context,
)

TRANSPORTS = ("plain", "tls")

# Which test drives each request method over a socket. The guard at the end of
# this file reads the methods off the live handler class, so a handler for a
# new method fails it until a round trip for that method exists here, on both
# transports.
SOCKET_ROUND_TRIPS = {
    "GET": "test_get_round_trips_through_the_socket",
    "POST": "test_post_chat_round_trips_through_the_socket",
    "DELETE": "test_delete_session_round_trips_through_the_socket",
}

REQUEST_ID = "socket-request-0001"
USAGE = {"input_tokens": 2, "output_tokens": 1}
INVALID_LENGTH = {
    "error": {"code": "invalid_content_length",
              "message": "invalid or oversized Content-Length"},
    "request_id": REQUEST_ID,
}

CHAT_TOKEN = "socket-chat-token-with-enough-entropy-for-a-test-fixture"
OPS_TOKEN = "socket-ops-token-with-enough-entropy-for-a-test-fixture"
DATA_TOKEN = "socket-data-token-with-enough-entropy-for-a-test-fixture"
SCHEDULE_TOKEN = "socket-schedule-token-with-enough-entropy-for-a-test-fixture"


def _entry(token, principal, tenant, scopes, tools=()):
    return {
        "sha256": hashlib.sha256(token.encode()).hexdigest(),
        "principal": principal,
        "tenant": tenant,
        "scopes": scopes,
        "tools": list(tools),
    }


def _auth():
    return AuthRegistry([
        _entry(CHAT_TOKEN, "alice", "tenant-a", ["chat"], ["read_file"]),
        _entry(OPS_TOKEN, "operator", "ops", ["ops:read"]),
        _entry(DATA_TOKEN, "data-admin", "tenant-a",
               ["sessions:read", "sessions:delete"]),
        _entry(SCHEDULE_TOKEN, "planner", "tenant-a",
               ["chat", "schedules:read", "schedules:write"], ["read_file"]),
    ])


def _headers(token):
    return {"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID}


class EchoAgent:
    """A scripted agent that replies with exactly the message it was sent."""

    manifest = {"read_file": {}, "write_file": {}}
    memory = None
    _mcp = object()

    def __init__(self):
        self.calls = []

    def turn_with_usage(self, session, message, *, allowed_tools=None,
                        deadline_monotonic=None):
        self.calls.append((session, message, set(allowed_tools or ())))
        return message, dict(USAGE)

    def turn_telemetry(self):
        return {"forge_queue_wait_ms": 0.0, "tool_calls": 0, "retries": 0}


def _service(agent=None, **kwargs):
    kwargs.setdefault("log_sink", lambda record: None)
    return ProductService(agent or EchoAgent(), _auth(), **kwargs)


def _data_service(tmp_path):
    """A service whose data manager deletes real conversation, sandbox and
    audit files."""
    from agent import AuditLog, SessionStore

    agent = EchoAgent()
    agent.store = SessionStore(tmp_path / "sessions")
    agent.sandbox_root = tmp_path / "sandboxes"
    agent.audit = AuditLog(tmp_path / "audit", key=b"socket-test-audit-key")
    schedules = ProductScheduleStore(tmp_path / "schedules.json")
    locks = SessionOperationLocks()
    service = _service(agent, schedule_store=schedules, session_locks=locks,
                       data_manager=ProductDataManager(agent, schedules, locks))
    return service, agent


def _seed_session(agent, tenant, session, marker):
    internal = _internal_session(tenant, session)
    agent.store.save(internal, [{"role": "user", "content": marker}])
    sandbox = (agent.sandbox_root
               / hashlib.sha256(internal.encode()).hexdigest()[:16])
    sandbox.mkdir(parents=True)
    (sandbox / "note.txt").write_text(marker)
    agent.audit.record(internal, "test", "source", marker, "output", None, None, 10)
    return internal, sandbox


def _outcome(action):
    """Whatever `action` raised, BaseException included, so that a pytest skip
    is caught and inspected here instead of skipping the test that asks."""
    try:
        action()
    except BaseException as raised:
        return raised
    return None


@pytest.fixture(params=TRANSPORTS)
def transport(request):
    return request.param


# ── round trips, on every transport ─────────────────────────────────────


def test_get_round_trips_through_the_socket(product_server, transport):
    server = product_server(_service(), tls=transport == "tls")

    reply = server.request("GET", "/v1/health", headers=_headers(OPS_TOKEN))

    assert reply.status == 200, reply
    assert reply.json() == {"status": "ok", "request_id": REQUEST_ID}
    assert reply.headers.get_content_type() == "application/json"
    assert reply.headers["X-Request-ID"] == REQUEST_ID
    assert reply.headers["Cache-Control"] == "no-store"


def test_post_chat_round_trips_through_the_socket(product_server, transport):
    agent = EchoAgent()
    records = []
    server = product_server(_service(agent, log_sink=records.append),
                            tls=transport == "tls")
    body = json.dumps({"session": "project", "message": "hello over the wire"}).encode()

    reply = server.request("POST", "/v1/chat", headers=_headers(CHAT_TOKEN), body=body)

    assert reply.status == 200, reply
    assert reply.json() == {"request_id": REQUEST_ID, "session": "project",
                            "reply": "hello over the wire", "usage": USAGE}
    assert reply.headers.get_content_type() == "application/json"
    assert int(reply.headers["Content-Length"]) == len(reply.body)
    assert reply.headers["X-Request-ID"] == REQUEST_ID
    assert reply.headers["Cache-Control"] == "no-store"
    assert reply.headers["X-Sigil-Tool-Calls"] == "0"
    assert reply.headers["Server-Timing"].startswith("queue;dur=")
    assert agent.calls == [(_internal_session("tenant-a", "project"),
                            "hello over the wire", {"read_file"})]
    assert [(r["route"], r["status"]) for r in records
            if r.get("event") == "http_request"] == [("/v1/chat", 200)]


def test_unauthenticated_post_is_refused_over_the_socket(product_server, transport):
    agent = EchoAgent()
    server = product_server(_service(agent), tls=transport == "tls")
    body = json.dumps({"session": "project", "message": "hello"}).encode()

    reply = server.request("POST", "/v1/chat", headers={"X-Request-ID": REQUEST_ID},
                           body=body)

    assert reply.status == 401, reply
    assert reply.json()["error"]["code"] == "authentication_required"
    assert reply.json()["request_id"] == REQUEST_ID
    assert reply.headers["WWW-Authenticate"] == 'Bearer realm="sigil-pi"'
    assert agent.calls == []


def test_delete_session_round_trips_through_the_socket(product_server, transport,
                                                       tmp_path):
    service, agent = _data_service(tmp_path)
    internal, sandbox = _seed_session(agent, "tenant-a", "project", "A-private")
    other, _ = _seed_session(agent, "tenant-b", "project", "B-private")
    server = product_server(service, tls=transport == "tls")

    reply = server.request("DELETE", "/v1/sessions/project",
                           headers=_headers(DATA_TOKEN))

    assert reply.status == 200, reply
    assert reply.json() == {
        "deleted": {"conversation": True, "sandbox": True, "audit": True,
                    "schedules": 0},
        "request_id": REQUEST_ID,
    }
    assert reply.headers["X-Request-ID"] == REQUEST_ID
    assert not agent.store._path(internal).exists()
    assert not sandbox.exists()
    assert not agent.audit._path(internal).exists()
    # Another tenant's session of the same name is untouched.
    assert agent.store.load(other) == [{"role": "user", "content": "B-private"}]

    again = server.request("DELETE", "/v1/sessions/project",
                           headers=_headers(DATA_TOKEN))
    assert again.status == 404, again
    assert again.json()["error"]["code"] == "session_not_found"


def test_schedule_create_and_delete_round_trip_through_the_socket(
        product_server, transport, tmp_path):
    store = ProductScheduleStore(tmp_path / "schedules.json")
    agent = EchoAgent()
    server = product_server(_service(agent, schedule_store=store),
                            tls=transport == "tls")
    body = json.dumps({"name": "daily", "session": "project",
                       "message": "report", "every_ms": 60_000}).encode()

    created = server.request("POST", "/v1/schedules",
                             headers=_headers(SCHEDULE_TOKEN), body=body)
    assert created.status == 201, created
    assert created.json() == {"created": True, "request_id": REQUEST_ID}
    assert [(e["name"], e["session"], e["message"], e["every_ms"])
            for e in store.for_tenant("tenant-a")] == [
                ("daily", "project", "report", 60_000)]

    removed = server.request("DELETE", "/v1/schedules/daily",
                             headers=_headers(SCHEDULE_TOKEN))
    assert removed.status == 200, removed
    assert removed.json() == {"removed": True, "request_id": REQUEST_ID}
    assert store.for_tenant("tenant-a") == []
    assert agent.calls == []


# ── what the transport refuses ──────────────────────────────────────────


@pytest.mark.parametrize("declared", [
    "not-a-number", "-1", str(MAX_PRODUCT_REQUEST_BYTES + 1),
], ids=["not-a-number", "negative", "over-the-limit"])
def test_an_invalid_content_length_is_refused_before_any_body_is_read(
        product_server, transport, declared):
    agent = EchoAgent()
    # A server that waited for the declared body would hold this request for
    # its whole 30-second socket timeout; the client gives up after 5.
    server = product_server(_service(agent), tls=transport == "tls",
                            socket_timeout_s=30)
    try:
        reply = server.request(
            "POST", "/v1/chat",
            headers={**_headers(CHAT_TOKEN), "Content-Length": declared},
            timeout=5)
    except TimeoutError:
        pytest.fail(f"Content-Length {declared!r}: the server waited for a body "
                    f"instead of refusing the declared length")

    assert reply.status == 400, reply
    assert reply.headers.get_content_type() == "application/json"
    assert reply.json() == INVALID_LENGTH
    assert agent.calls == []


def test_a_body_shorter_than_its_content_length_is_refused_at_the_socket_timeout(
        product_server, transport):
    agent = EchoAgent()
    server = product_server(_service(agent), tls=transport == "tls",
                            socket_timeout_s=0.5)
    head = http_request_bytes(
        "POST", "/v1/chat", headers={**_headers(CHAT_TOKEN), "Content-Length": "64"})

    reply = server.exchange(head + b'{"session": "project"', method="POST", timeout=10)

    assert reply.status == 400, reply
    assert reply.json() == INVALID_LENGTH
    assert agent.calls == []


# ── TLS ─────────────────────────────────────────────────────────────────


def test_the_tls_listener_presents_its_certificate_with_a_tls12_floor(
        product_server, tls_material):
    server = product_server(_service(), tls=True)

    sock = server.connect()  # verifies the certificate and the hostname
    assert sock.version() in ("TLSv1.2", "TLSv1.3")
    assert ("DNS", "localhost") in sock.getpeercert()["subjectAltName"]
    sock.sendall(http_request_bytes("GET", "/v1/version", headers=_headers(OPS_TOKEN)))
    reply = read_reply(sock)
    sock.close()
    assert reply.status == 200, reply
    assert reply.json()["api_version"] == "v1"

    # A client that stops at TLS 1.2 is served, and nothing older is allowed.
    tls12 = ssl.create_default_context(cafile=str(tls_material.cert))
    tls12.maximum_version = ssl.TLSVersion.TLSv1_2
    with tls12.wrap_socket(server.connect(tls=False),
                           server_hostname=tls_material.hostname) as old_client:
        assert old_client.version() == "TLSv1.2"
    context = tls_context(str(tls_material.cert), str(tls_material.key))
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2


def test_the_tls_listener_does_not_answer_plaintext_http(product_server):
    server = product_server(_service(), tls=True)

    plain = server.connect(tls=False)
    plain.sendall(http_request_bytes("GET", "/v1/health", headers=_headers(OPS_TOKEN)))
    received = b""
    try:
        while chunk := plain.recv(4096):
            received += chunk
    except OSError:  # a reset or a timeout: either way, no HTTP answer
        pass
    plain.close()

    assert not received.startswith(b"HTTP/"), received[:80]
    # Refusing it costs nothing: the next TLS client is served.
    assert server.request("GET", "/v1/health",
                          headers=_headers(OPS_TOKEN)).status == 200


def test_the_entry_point_tls_settings_serve_verified_tls(product_server, tls_material,
                                                          monkeypatch):
    """product_main builds the listener's context from PI_TLS_CERT/PI_TLS_KEY;
    no test had ever let it build one."""
    monkeypatch.setenv("PI_TLS_CERT", str(tls_material.cert))
    monkeypatch.setenv("PI_TLS_KEY", str(tls_material.key))
    context = product_main._server_tls_context()
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2

    server = product_server(_service(), tls=True, ssl_context=context)
    reply = server.request("GET", "/v1/health", headers=_headers(OPS_TOKEN))
    assert reply.status == 200, reply


@pytest.mark.parametrize("configured", ["PI_TLS_CERT", "PI_TLS_KEY"])
def test_the_entry_point_refuses_half_a_tls_configuration(monkeypatch, tmp_path,
                                                          configured):
    monkeypatch.delenv("PI_TLS_CERT", raising=False)
    monkeypatch.delenv("PI_TLS_KEY", raising=False)
    monkeypatch.setenv(configured, str(tmp_path / "never-read.pem"))
    with pytest.raises(ConfigError, match="configured together"):
        product_main._server_tls_context()


# ── the transport is byte-exact (property) ──────────────────────────────


def _bounded_repeat(chunk, repeat):
    """`chunk` repeated up to `repeat` times, inside both size limits: the
    message's own (MAX_MESSAGE_BYTES) and the JSON body's that carries it
    (MAX_PRODUCT_REQUEST_BYTES, less room for the session and the keys)."""
    encoded = len(chunk.encode())
    escaped = len(json.dumps(chunk, ensure_ascii=False).encode()) - 2
    fits = min(MAX_MESSAGE_BYTES // encoded,
               (MAX_PRODUCT_REQUEST_BYTES - 512) // escaped)
    return chunk * max(1, min(repeat, fits))


SESSIONS = st.from_regex(SESSION_RE, fullmatch=True)
# Any text a client can send (a lone surrogate has no UTF-8 form), at sizes
# that cross the 16 KiB TLS record and the socket buffers.
MESSAGES = st.builds(
    _bounded_repeat,
    st.text(st.characters(exclude_categories=("Cs",)), min_size=1, max_size=32),
    st.integers(min_value=1, max_value=1 << 15))
# The largest body the transport accepts, made of escapes only: each "\x01"
# travels as the six bytes \u0001, inside 31 bytes of JSON for session "s".
ESCAPES_AT_THE_LIMIT = "\x01" * ((MAX_PRODUCT_REQUEST_BYTES - 31) // 6)


def test_any_chat_message_round_trips_byte_exact(product_server, transport):
    """The transport neither truncates nor re-encodes: every valid message
    reaches the agent exactly and comes back exactly in the reply."""
    agent = EchoAgent()
    server = product_server(_service(agent, requests_per_minute=1_000_000),
                            tls=transport == "tls")

    @settings(max_examples=40, deadline=None)
    @given(session=SESSIONS, message=MESSAGES)
    @example(session="s", message="x" * MAX_MESSAGE_BYTES)
    @example(session="s", message=ESCAPES_AT_THE_LIMIT)
    @example(session="s", message="é" * 20_000)
    def round_trip(session, message):
        body = json.dumps({"session": session, "message": message},
                          ensure_ascii=False).encode()
        assert len(body) <= MAX_PRODUCT_REQUEST_BYTES
        reply = server.request("POST", "/v1/chat", headers=_headers(CHAT_TOKEN),
                               body=body, timeout=30)
        assert reply.status == 200, reply
        assert reply.json() == {"request_id": REQUEST_ID, "session": session,
                                "reply": message, "usage": USAGE}
        assert agent.calls[-1] == (_internal_session("tenant-a", session),
                                   message, {"read_file"})

    round_trip()


# ── the harness itself ──────────────────────────────────────────────────


def test_tls_material_fails_rather_than_skips_without_openssl(tmp_path, monkeypatch):
    """A skipped TLS test reads as green in CI while the TLS path runs nowhere,
    which is how that path went unseen. A missing openssl must fail."""
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    outcome = _outcome(lambda: make_tls_material(tmp_path / "tls"))

    assert type(outcome) is pytest.fail.Exception, (
        f"a missing openssl must FAIL the TLS tests, never skip them; got {outcome!r}")
    assert "openssl is not on PATH" in str(outcome)


def test_tls_material_reports_a_failing_openssl_as_a_failure(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    fake = tools / "openssl"
    fake.write_text("#!/bin/sh\necho 'simulated refusal' >&2\nexit 3\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools))

    outcome = _outcome(lambda: make_tls_material(tmp_path / "tls"))

    assert type(outcome) is pytest.fail.Exception, f"got {outcome!r}"
    assert "exit 3" in str(outcome) and "simulated refusal" in str(outcome)


def test_closing_a_server_releases_a_client_that_never_speaks(product_server):
    """Teardown closes client connections before it stops the server, so a
    test that leaves a silent client behind (the F-01 scenario) cannot hang
    the suite, and the server still stops."""
    server = product_server(_service(), tls=True, socket_timeout_s=30)
    silent = server.connect(tls=False)  # connects, never sends a byte
    time.sleep(0.2)  # let the server take the connection

    started = time.monotonic()
    server.close()

    assert time.monotonic() - started < 5
    assert silent.fileno() == -1
    assert server.server.socket.fileno() == -1


def test_every_method_the_server_handles_has_a_socket_round_trip_on_both_transports(
        product_server):
    handler = product_server(_service()).server.RequestHandlerClass
    handled = {name[len("do_"):] for name in dir(handler)
               if name.startswith("do_") and callable(getattr(handler, name))}

    assert handled == set(SOCKET_ROUND_TRIPS), (
        f"serve_product handles {sorted(handled)}, but this file round-trips "
        f"{sorted(SOCKET_ROUND_TRIPS)} over a socket. Add a round trip for every "
        f"new method, on every transport, and name it in SOCKET_ROUND_TRIPS.")
    assert set(TRANSPORTS) == {"plain", "tls"}
    for method, name in SOCKET_ROUND_TRIPS.items():
        test = globals().get(name)
        assert callable(test), f"{method}: {name} is not a test in this file"
        assert "transport" in inspect.signature(test).parameters, (
            f"{name} must take the transport fixture, so it runs plain and over TLS")
