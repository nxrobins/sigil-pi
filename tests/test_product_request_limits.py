"""What a client without a credential can make the product server hold
(audit 2026-11-10, F-08).

do_POST read a declared body of up to 1 MiB before dispatch() authenticated
the request; the listener started a thread for every connection, with no cap;
and the only clock was the socket timeout, which starts again at every read.
200 connections that sent headers and no credential held 200 threads, and a
client sending a byte every 2 seconds stayed connected for 13 seconds against
a 3-second timeout. These tests pin the bounds that replace that, on both
transports:

- a request is authenticated from its headers before any of its body is read,
  and a refused request is answered and closed at once;
- the listener handles at most `max_connections` connections at a time
  (MAX_CONNECTIONS by default). A connection beyond the cap waits, unanswered,
  in the listen queue until another ends, and is then served as usual;
- a request, its TLS handshake, head and body, must arrive within
  `request_deadline_s` of the server taking its connection, however it is
  paced. The deadline bounds receiving the request, not the turn;
- the header lines of a request total at most MAX_REQUEST_HEAD_BYTES, where
  the standard library's limits (100 lines of 64 KiB) let a client without a
  credential make the server hold 6.4 MiB of headers per connection;
- a body is framed by one Content-Length of digits (RFC 9112 section 6.3), and
  a body that ends before its declared length is not processed.
"""

import hashlib
import http.client
import json
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler
from types import SimpleNamespace

import pytest
from hypothesis import example, given, settings, strategies as st

from conftest import ProductServerHandle

import product_service
from product_service import (
    MAX_PRODUCT_REQUEST_BYTES,
    AuthRegistry,
    ConfigError,
    ProductService,
    serve_product,
    tls_context,
)

TRANSPORTS = ("plain", "tls")
REQUEST_ID = "limits-request-0001"
CHAT_TOKEN = "limits-chat-token-with-enough-entropy-for-a-test-fixture"
OPS_TOKEN = "limits-ops-token-with-enough-entropy-for-a-test-fixture"
UNKNOWN_TOKEN = "limits-unknown-token-with-enough-entropy-for-a-test-fixture"
USAGE = {"input_tokens": 2, "output_tokens": 1}
# The default the operations guide states for the cap.
DEFAULT_MAX_CONNECTIONS = 128
BODY = json.dumps({"session": "project", "message": "hello"}).encode()
INVALID_LENGTH = {
    "error": {"code": "invalid_content_length",
              "message": "invalid or oversized Content-Length"},
    "request_id": REQUEST_ID,
}


class EchoAgent:
    """A scripted agent that replies with the message it was sent, after
    `delay_s`."""

    manifest = {"read_file": {}, "write_file": {}}
    memory = None
    _mcp = object()

    def __init__(self, delay_s=0.0):
        self.calls = []
        self.delay_s = delay_s

    def turn_with_usage(self, session, message, *, allowed_tools=None,
                        deadline_monotonic=None):
        self.calls.append((session, message))
        time.sleep(self.delay_s)
        return message, dict(USAGE)

    def turn_telemetry(self):
        return {"forge_queue_wait_ms": 0.0, "tool_calls": 0, "retries": 0}


def _entry(token, principal, tenant, scopes):
    return {"sha256": hashlib.sha256(token.encode()).hexdigest(),
            "principal": principal, "tenant": tenant, "scopes": scopes, "tools": []}


def _service(agent=None, records=None, auth=None):
    return ProductService(
        agent or EchoAgent(),
        auth or AuthRegistry([_entry(CHAT_TOKEN, "alice", "tenant-a", ["chat"]),
                              _entry(OPS_TOKEN, "operator", "ops", ["ops:read"])]),
        log_sink=records.append if records is not None else (lambda record: None))


@pytest.fixture(params=TRANSPORTS)
def transport(request):
    return request.param


@pytest.fixture()
def server_with(request):
    """serve_product() with the limits a test names, otherwise started as
    conftest's product_server starts it, and stopped within a bound in
    teardown (ProductServerHandle.close)."""
    handles = []

    def start(service, *, tls=False, socket_timeout_s=5, **limits):
        material = request.getfixturevalue("tls_material") if tls else None
        context = tls_context(str(material.cert), str(material.key)) if tls else None
        server = serve_product(service, host="127.0.0.1", port=0, ssl_context=context,
                               socket_timeout_s=socket_timeout_s, **limits)
        handle = ProductServerHandle(server, material)
        handles.append(handle)
        return handle

    yield start
    failures = []
    for handle in handles:
        try:
            handle.close()
        except AssertionError as error:
            failures.append(str(error))
    if failures:
        pytest.fail("\n".join(failures), pytrace=False)


def _head(method="POST", path="/v1/chat", *, token=None, length=None, extra=()):
    """The bytes of a request head, without a body."""
    lines = [f"{method} {path} HTTP/1.1", "Host: sigil-pi.test",
             f"X-Request-ID: {REQUEST_ID}"]
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    if length is not None:
        lines.append(f"Content-Length: {length}")
    lines.extend(extra)
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def _read_all(sock, within):
    """Everything the server sends until it ends the connection, waiting at
    most `within` seconds in all: (bytes, whether the server ended it)."""
    end = time.monotonic() + within
    data = b""
    while (left := end - time.monotonic()) > 0:
        sock.settimeout(left)
        try:
            chunk = sock.recv(65536)
        except TimeoutError:
            break
        except OSError:          # reset: ended
            return data, True
        if not chunk:
            return data, True
        data += chunk
    return data, False


def _status(data):
    match = re.match(rb"HTTP/1\.[01] (\d{3}) ", data)
    return int(match.group(1)) if match else None


def _parse(data):
    """(status, lower-cased headers, body) of one response."""
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return _status(data), headers, body


def _exchange(server, raw, within=5.0, tls=None):
    sock = server.connect(tls=tls)
    sock.sendall(raw)
    data, _ = _read_all(sock, within)
    sock.close()
    return data


def _watch(server):
    """How many connections the server is handling: every connection passes
    through process_request, which starts its handler thread, and then
    shutdown_request, which ends it. Wrapping the two on this server instance
    observes them without changing what they do."""
    state = {"now": 0, "most": 0}
    lock = threading.Lock()
    process, shutdown = server.server.process_request, server.server.shutdown_request

    def counted_process(request, client_address):
        with lock:
            state["now"] += 1
            state["most"] = max(state["most"], state["now"])
        return process(request, client_address)

    def counted_shutdown(request):
        try:
            return shutdown(request)
        finally:
            with lock:
                state["now"] -= 1

    server.server.process_request = counted_process
    server.server.shutdown_request = counted_shutdown
    return state


def _wait_until(predicate, within, what):
    end = time.monotonic() + within
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate(), f"{what} (waited {within}s)"


def _dribble(sock, data, every_s, give_up_s):
    """Send `data` one byte at a time, `every_s` apart, reading meanwhile,
    until the server ends the connection or `give_up_s` passes:
    (seconds until the server ended it, or None; the bytes it sent)."""
    started = time.monotonic()
    received = b""
    for index in range(len(data)):
        try:
            sock.sendall(data[index:index + 1])
        except OSError:
            return time.monotonic() - started, received
        pause_until = time.monotonic() + every_s
        while (left := pause_until - time.monotonic()) > 0:
            sock.settimeout(left)
            try:
                chunk = sock.recv(65536)
            except TimeoutError:
                break
            except OSError:
                return time.monotonic() - started, received
            if not chunk:
                return time.monotonic() - started, received
            received += chunk
        if time.monotonic() - started > give_up_s:
            return None, received
    rest, ended = _read_all(sock, max(0.0, give_up_s - (time.monotonic() - started)))
    return (time.monotonic() - started if ended else None), received + rest


# ── authentication before the body ─────────────────────────────────────


@pytest.mark.parametrize("credential", ["missing", "unknown"])
def test_a_request_without_a_valid_credential_is_refused_before_its_body_is_read(
        server_with, transport, credential):
    agent, records = EchoAgent(), []
    server = server_with(_service(agent, records), tls=transport == "tls",
                         socket_timeout_s=30)
    token = None if credential == "missing" else UNKNOWN_TOKEN
    code = "authentication_required" if credential == "missing" else "invalid_credential"
    sock = server.connect()
    started = time.monotonic()
    sock.sendall(_head(token=token, length=MAX_PRODUCT_REQUEST_BYTES))  # and no body

    data, ended = _read_all(sock, within=5)

    assert data, (
        f"no answer within 5s to a request whose credential is {credential} and which "
        f"declared a {MAX_PRODUCT_REQUEST_BYTES}-byte body it never sent: the server was "
        f"waiting for the body of a request it would refuse (audit F-08)")
    status, headers, body = _parse(data)
    assert status == 401, data[:200]
    assert json.loads(body)["error"]["code"] == code
    assert json.loads(body)["request_id"] == REQUEST_ID
    assert headers["www-authenticate"] == 'Bearer realm="sigil-pi"'
    assert headers["x-request-id"] == REQUEST_ID
    assert ended and time.monotonic() - started < 3, (
        "the server answered the refused request but kept its connection open")
    assert agent.calls == []
    assert [(r["event"], r["status"], r["failure_classification"], r["principal"])
            for r in records] == [("http_request", 401, code, None)], (
        "a refusal from the headers must be logged and counted as dispatch() does")


def test_clients_refused_from_their_headers_do_not_hold_the_server(server_with, transport):
    """The reproduction's part (a): connections that send headers with no
    credential, declare the largest body and send none."""
    server = server_with(_service(), tls=transport == "tls", socket_timeout_s=30)
    watched = _watch(server)
    clients = []
    for _ in range(40):
        sock = server.connect()
        sock.sendall(_head(length=MAX_PRODUCT_REQUEST_BYTES))
        clients.append(sock)

    end = time.monotonic() + 5
    outcomes = [_read_all(sock, max(0.01, end - time.monotonic())) for sock in clients]

    unanswered = sum(1 for data, _ in outcomes if _status(data) != 401)
    still_open = sum(1 for _, ended in outcomes if not ended)
    assert (unanswered, still_open) == (0, 0), (
        f"of 40 connections that sent headers and no credential, {unanswered} had no "
        f"401 and {still_open} were still open 5s later: each held a handler thread "
        f"while the server waited for its body (audit F-08)")
    _wait_until(lambda: watched["now"] == 0, 2,
                f"{watched['now']} refused connections still held handler threads")


def test_a_refused_client_still_sending_its_body_reads_the_refusal(server_with, transport):
    """The server answers before the body arrives. It must not then close the
    connection with the body unread, which resets it under a client that is
    still sending, before that client has read the answer: it drains the
    body, without keeping it, and the client reads its 401."""
    server = server_with(_service(), tls=transport == "tls", socket_timeout_s=30)
    body = json.dumps({"session": "project", "message": "x" * (900 * 1024)}).encode()
    assert len(body) <= MAX_PRODUCT_REQUEST_BYTES
    sock = server.connect()
    try:
        sock.sendall(_head(token=UNKNOWN_TOKEN, length=len(body)) + body)
    except OSError as error:
        pytest.fail(f"the server ended the connection while the refused client was "
                    f"still sending its body: {error!r}")
    data, _ = _read_all(sock, within=5)
    assert _status(data) == 401, data[:200]


def test_a_refused_client_cannot_keep_the_server_reading(server_with):
    """What a refused client sends after its answer is dropped only up to a
    limit: past it the server closes the connection, however fast the data
    comes, instead of reading it until the request deadline."""
    server = server_with(_service(), socket_timeout_s=30)
    watched = _watch(server)
    sock = server.connect()
    sock.sendall(_head(length=MAX_PRODUCT_REQUEST_BYTES))
    chunk, sent, ceiling = b"x" * 65536, 0, 64 * 1024 * 1024
    sock.settimeout(5)
    try:
        while sent < ceiling:
            sock.sendall(chunk)
            sent += len(chunk)
    except OSError:
        pass

    assert sent < ceiling, (
        f"the server read {ceiling} bytes from a client it had refused")
    _wait_until(lambda: watched["now"] == 0, 5,
                "the refused connection was still being read")


def test_a_request_is_authenticated_once(server_with):
    """The transport authenticates from the headers and dispatch() uses that
    outcome. Authenticating again could disagree at a credential's
    not-before or expiry instant, after the body had been read."""
    calls = []
    auth = AuthRegistry([_entry(CHAT_TOKEN, "alice", "tenant-a", ["chat"])])
    original = auth.authenticate

    def counted(authorization):
        calls.append(authorization)
        return original(authorization)

    auth.authenticate = counted
    server = server_with(_service(auth=auth))
    for token in (CHAT_TOKEN, UNKNOWN_TOKEN):
        calls.clear()
        data = _exchange(server, _head(token=token, length=len(BODY)) + BODY)
        assert _status(data) == (200 if token == CHAT_TOKEN else 401), data[:200]
        assert len(calls) == 1, f"authenticated {len(calls)} times for one request"


def test_no_method_reads_a_body_before_authenticating(server_with, transport):
    """The guard for the class: every request method the handler has, on each
    kind of route, answers a request with no credential whose declared body
    never comes. A method that read the body first would wait for it."""
    server = server_with(_service(), tls=transport == "tls", socket_timeout_s=30)
    handler = server.server.RequestHandlerClass
    methods = sorted(name[len("do_"):] for name in dir(handler)
                     if name.startswith("do_") and callable(getattr(handler, name)))
    assert "POST" in methods, "found no body-carrying method: the guard would pass vacuously"
    waited = []
    for method in methods:
        for path in ("/v1/chat", "/v1/schedules", "/v1/schedules/nightly",
                     "/v1/sessions/project", "/v1/health"):
            data = _exchange(server, _head(method=method, path=path,
                                           length=MAX_PRODUCT_REQUEST_BYTES), within=2)
            if _status(data) != 401:
                waited.append(f"{method} {path}: {data[:40]!r}")
    assert not waited, (
        f"with no credential and a declared body that never came, these got no 401 "
        f"within 2s: {waited} (audit F-08)")


# ── framing: Content-Length ─────────────────────────────────────────────


@pytest.mark.parametrize("form,accepted", [
    ("{n}", True), (" {n}\t", True), ("00{n}", True),
    ("+{n}", False), ("{head}_{tail}", False), ("{n}.0", False), ("0x{hex}", False),
    ("-{n}", False), ("{n}, {n}", False), ("{n} {n}", False), ("", False),
    pytest.param("1" * 30, False, id="30-digits"),
    pytest.param("9" * 5000, False, id="5000-digits"),
])
def test_a_content_length_is_one_run_of_digits(server_with, form, accepted):
    """RFC 9112 section 6.3: Content-Length is 1*DIGIT, and an invalid one is
    a framing error (400, then close). int() also accepted "+38" and "3_8"."""
    agent = EchoAgent()
    server = server_with(_service(agent))
    n = len(BODY)
    value = form.format(n=n, head=str(n)[:1], tail=str(n)[1:], hex=f"{n:x}")
    head = _head(token=CHAT_TOKEN).replace(
        b"\r\n\r\n", f"\r\nContent-Length: {value}\r\n\r\n".encode("latin-1"))

    data = _exchange(server, head + BODY)

    if accepted:
        assert _status(data) == 200, data[:200]
        assert [message for _, message in agent.calls] == ["hello"]
    else:
        assert _status(data) == 400 and json.loads(_parse(data)[2]) == INVALID_LENGTH, (
            f"Content-Length {value!r} was taken as a length: {data[:120]!r}")
        assert agent.calls == []


@pytest.mark.parametrize("lengths,accepted", [
    (("{n}", "{n}"), True), (("{n}", "1"), False), (("1", "{n}"), False),
])
def test_conflicting_content_lengths_are_a_framing_error(server_with, lengths, accepted):
    agent = EchoAgent()
    server = server_with(_service(agent))
    n = len(BODY)
    extra = [f"Content-Length: {value.format(n=n)}" for value in lengths]

    data = _exchange(server, _head(token=CHAT_TOKEN, extra=extra) + BODY)

    if accepted:
        assert _status(data) == 200, data[:200]
    else:
        assert _status(data) == 400 and json.loads(_parse(data)[2]) == INVALID_LENGTH, (
            f"Content-Length {lengths} resolved to one of them: {data[:120]!r}")
        assert agent.calls == []


def test_a_body_that_ends_before_its_declared_length_is_not_processed(server_with):
    """RFC 9112 section 8: a message whose body ends before its Content-Length
    is incomplete. The server used to run the turn on the bytes it had."""
    agent = EchoAgent()
    server = server_with(_service(agent))
    sock = server.connect()
    sock.sendall(_head(token=CHAT_TOKEN, length=len(BODY) + 20) + BODY)
    sock.shutdown(socket.SHUT_WR)  # the client's body ends here

    data, _ = _read_all(sock, within=5)

    assert _status(data) == 400 and json.loads(_parse(data)[2]) == INVALID_LENGTH, (
        f"a body 20 bytes short of its declared length was answered {data[:60]!r}")
    assert agent.calls == []


def test_a_post_without_a_content_length_has_no_body(server_with):
    """No Content-Length declares no body, as before: the request is still
    authenticated first, and then answered by dispatch()."""
    agent = EchoAgent()
    server = server_with(_service(agent))

    assert _status(_exchange(server, _head())) == 401
    data = _exchange(server, _head(token=CHAT_TOKEN))

    assert _status(data) == 400, data[:200]
    assert json.loads(_parse(data)[2])["error"]["code"] == "invalid_request"
    assert agent.calls == []


_FIELD_VALUE = st.text(alphabet="0123456789+-_., \tx", max_size=9)


@settings(max_examples=300, deadline=None)
@given(values=st.lists(_FIELD_VALUE, max_size=3))
@example(values=["+38"])
@example(values=["3_8"])
@example(values=[" 38\t"])
@example(values=["38", "38"])
@example(values=["38", "1"])
@example(values=["0" * 30 + "38"])
@example(values=["1" * 30])
@example(values=[])
def test_the_declared_length_is_exactly_what_rfc_9112_allows(values):
    """The property behind the two tests above, through the standard
    library's header parser as the server reads it: a length is taken only
    from one value of ASCII digits, repeated identically if at all, with
    optional surrounding spaces or tabs; none declares no body. Past 18
    significant digits, any length over every limit stands for it."""
    raw = "".join(f"Content-Length: {value}\r\n" for value in values) + "\r\n"
    headers = http.client.parse_headers(_Lines(raw.encode("latin-1")))
    parsed = [value.strip(" \t") for value in headers.get_all("Content-Length") or ()]
    expected = (0 if not parsed
                else min(int(parsed[0]), 10 ** 18) if len(set(parsed)) == 1
                and re.fullmatch(r"[0-9]+", parsed[0]) else None)

    assert product_service._declared_length(headers) == expected


class _Lines:
    """A readline() source over bytes, as the server's input is."""

    def __init__(self, data):
        self._lines = data.splitlines(keepends=True)

    def readline(self, limit=-1):
        return self._lines.pop(0) if self._lines else b""


# ── the request head ───────────────────────────────────────────────────


def test_a_request_head_over_the_limit_is_refused_without_a_credential(
        server_with, transport):
    """The standard library allows 100 header lines of 64 KiB: 16 connections
    sending such a head and no credential grew the server by 7.8 MiB each.
    Header lines over MAX_REQUEST_HEAD_BYTES in total end the request with
    431 before it is dispatched."""
    records = []
    server = server_with(_service(EchoAgent(), records), tls=transport == "tls",
                         socket_timeout_s=30)
    fill = "x" * (40 * 1024)
    raw = _head(method="GET", path="/v1/health",
                extra=[f"X-Fill-{index}: {fill}" for index in range(2)])

    data = _exchange(server, raw)

    assert _status(data) == 431, (
        f"an {len(raw) // 1024} KiB head was read in full: {data[:60]!r}")
    assert records == [], "the request reached dispatch"


def test_a_request_head_within_the_limit_is_served(server_with, transport):
    server = server_with(_service(), tls=transport == "tls")
    fill = "x" * (30 * 1024)
    raw = _head(method="GET", path="/v1/health", token=OPS_TOKEN,
                extra=[f"X-Fill-{index}: {fill}" for index in range(2)])
    assert len(raw) < 63 * 1024

    assert _status(_exchange(server, raw)) == 200


# ── the request deadline ───────────────────────────────────────────────


def test_a_client_dribbling_its_head_is_ended_at_the_request_deadline(
        server_with, transport):
    """The reproduction's part (b): a byte every 0.2 s keeps a 2 s socket
    timeout from ever firing. The deadline ends the connection, unanswered."""
    server = server_with(_service(), tls=transport == "tls", socket_timeout_s=2,
                         request_deadline_s=1.5)
    sock = server.connect()
    head = b"GET /v1/health HTTP/1.1\r\nHost: sigil-pi.test\r\nX-Pad: " + b"a" * 200

    ended_after, received = _dribble(sock, head, every_s=0.2, give_up_s=6)

    assert ended_after is not None, (
        "a client sending a byte every 0.2s was still connected after 6s, against a "
        "1.5s request deadline and a 2s socket timeout (audit F-08)")
    assert 1.2 <= ended_after <= 3.5, f"ended after {ended_after:.2f}s"
    assert received == b"", f"an unfinished head was answered: {received[:60]!r}"


def test_a_body_dribbled_past_the_request_deadline_is_refused(server_with, transport):
    agent = EchoAgent()
    server = server_with(_service(agent), tls=transport == "tls", socket_timeout_s=2,
                         request_deadline_s=1.5)
    sock = server.connect()
    sock.sendall(_head(token=CHAT_TOKEN, length=len(BODY)))

    ended_after, received = _dribble(sock, BODY, every_s=0.2, give_up_s=6)

    assert ended_after is not None, (
        "a body arriving a byte every 0.2s was still being read after 6s, against a "
        "1.5s request deadline (audit F-08)")
    assert ended_after <= 3.5, f"ended after {ended_after:.2f}s"
    assert _status(received) == 400, received[:200]
    assert json.loads(_parse(received)[2]) == INVALID_LENGTH
    assert agent.calls == []


def test_a_silent_tls_client_is_ended_at_the_request_deadline(server_with):
    """The handshake is part of receiving the request: with a deadline shorter
    than the socket timeout, the deadline bounds it."""
    server = server_with(_service(), tls=True, socket_timeout_s=10, request_deadline_s=1)
    sock = server.connect(tls=False)  # connects and never sends a ClientHello
    started = time.monotonic()

    data, ended = _read_all(sock, within=5)

    assert ended and data == b"", "the silent client was not ended within 5s"
    assert time.monotonic() - started <= 2.5


def test_the_request_deadline_does_not_bound_the_turn(server_with, transport):
    """The deadline is for receiving the request. A turn runs under its own
    deadline, and its answer is written under the socket timeout."""
    agent = EchoAgent(delay_s=1.5)
    server = server_with(_service(agent), tls=transport == "tls", request_deadline_s=1)

    data = _exchange(server, _head(token=CHAT_TOKEN, length=len(BODY)) + BODY, within=10)

    assert _status(data) == 200, data[:200]
    assert json.loads(_parse(data)[2])["reply"] == "hello"


# ── the connection cap ─────────────────────────────────────────────────


def test_the_default_listener_handles_at_most_its_cap_of_connections(server_with):
    """The reproduction's measure, with silent clients that hold their
    connections for the socket timeout."""
    server = server_with(_service(), socket_timeout_s=30)
    watched = _watch(server)
    clients = [server.connect() for _ in range(DEFAULT_MAX_CONNECTIONS + 16)]
    _wait_until(lambda: watched["now"] >= min(len(clients), DEFAULT_MAX_CONNECTIONS), 5,
                "the server did not take the connections")
    time.sleep(0.5)  # time for more than the cap to be taken, were there no cap

    assert watched["most"] <= DEFAULT_MAX_CONNECTIONS, (
        f"{len(clients)} connections were handled at once, each on its own thread; the "
        f"cap is {DEFAULT_MAX_CONNECTIONS} (audit F-08)")
    assert product_service.MAX_CONNECTIONS == DEFAULT_MAX_CONNECTIONS


def test_a_connection_beyond_the_cap_waits_and_is_then_served(server_with, transport):
    server = server_with(_service(), tls=transport == "tls", socket_timeout_s=30,
                         max_connections=2)
    watched = _watch(server)
    holders = [server.connect(tls=False) for _ in range(2)]
    _wait_until(lambda: watched["now"] == 2, 5, "the two holders were not taken")
    answer = {}

    def client():
        answer["data"] = _exchange(server, _head(method="GET", path="/v1/health",
                                                 token=OPS_TOKEN), within=10)
        answer["at"] = time.monotonic()

    waiting = threading.Thread(target=client, daemon=True)
    waiting.start()
    waiting.join(1.0)
    assert "data" not in answer, (
        f"a third connection was answered while two held a cap of two: "
        f"{answer['data'][:60]!r}")
    assert watched["most"] == 2

    holders[0].close()
    freed = time.monotonic()
    waiting.join(5)
    assert _status(answer.get("data", b"")) == 200, answer
    assert answer["at"] - freed < 2, "the waiting connection was not served once a slot freed"


def test_shutdown_is_not_held_up_at_the_cap(server_with):
    """drain_service() stops the listener within its one budget: a listener
    waiting for a slot must still stop at once."""
    server = server_with(_service(), socket_timeout_s=30, max_connections=1)
    watched = _watch(server)
    server.connect()  # holds the only slot
    _wait_until(lambda: watched["now"] == 1, 5, "the holder was not taken")
    waiting = server.connect()
    waiting.sendall(_head(method="GET", path="/v1/health", token=OPS_TOKEN))
    time.sleep(0.3)

    stopper = threading.Thread(target=server.server.shutdown, daemon=True)
    started = time.monotonic()
    stopper.start()
    stopper.join(5)

    assert not stopper.is_alive(), "shutdown() was still waiting after 5s at the cap"
    assert time.monotonic() - started < 1.5


def test_every_way_a_connection_ends_frees_its_slot(server_with, transport, monkeypatch):
    """Eight connections, each ending a different way, through a cap of two;
    afterwards the cap still admits exactly two, and no slot was released
    twice."""
    failures = []
    monkeypatch.setattr(threading, "excepthook", lambda args: failures.append(args))
    tls = transport == "tls"
    server = server_with(_service(), tls=tls, socket_timeout_s=1, max_connections=2,
                         request_deadline_s=1)
    watched = _watch(server)

    assert _status(_exchange(server, _head(method="GET", path="/v1/health",
                                           token=OPS_TOKEN))) == 200
    assert _status(_exchange(server, _head(length=10))) == 401
    assert _status(_exchange(server, _head(token=CHAT_TOKEN, length="+3"))) == 400
    assert _status(_exchange(server, _head(
        method="GET", path="/v1/health", extra=[f"X-Fill: {'x' * 70000}"]))) == 431
    server.connect().close()                                  # a client that leaves
    assert _exchange(server, b"GET /v1/health HT", within=3) == b""   # the deadline
    if tls:
        assert _exchange(server, _head(method="GET", path="/v1/health"),
                         tls=False, within=3) == b""          # a failed handshake
    else:
        assert _status(_exchange(server, b"BREW /pot HTTP/1.1\r\n\r\n")) == 501
    _wait_until(lambda: watched["now"] == 0, 5, "a connection never ended")

    holders = [server.connect(tls=False) for _ in range(2)]
    _wait_until(lambda: watched["now"] == 2, 3, "the cap no longer admits two")
    third = server.connect(tls=False)
    time.sleep(0.5)
    assert watched["now"] == 2, "the cap admitted a third connection"
    for sock in (*holders, third):
        sock.close()
    assert failures == [], f"a connection's thread failed: {failures[0].exc_value!r}"


@pytest.mark.parametrize("limits", [
    {"max_connections": 0}, {"max_connections": -1}, {"max_connections": 1.5},
    {"max_connections": True}, {"request_deadline_s": 0}, {"request_deadline_s": -1},
])
def test_the_limits_must_be_positive(limits):
    with pytest.raises(ConfigError):
        serve_product(_service(), host="127.0.0.1", port=0, **limits)


def test_a_slot_is_given_back_when_the_connection_it_waited_for_has_gone():
    """With every slot taken, the server waits for one when a connection is
    waiting. If that connection has gone by the time a slot frees (a client
    that gave up), the slot is given back, and accept() is not left holding
    the serving thread until another connection comes."""
    server = product_service.ProductHTTPServer(
        ("127.0.0.1", 0), BaseHTTPRequestHandler, max_connections=1)
    clients = []
    try:
        clients.append(socket.create_connection(server.server_address))
        taken, _ = server.get_request()
        threading.Timer(0.1, server.shutdown_request, args=(taken,)).start()
        started = time.monotonic()
        with pytest.raises(OSError, match="no connection is waiting"):
            server.get_request()
        assert time.monotonic() - started < 0.45

        clients.append(socket.create_connection(server.server_address))
        request, _ = server.get_request()   # the slot was given back
        server.shutdown_request(request)
    finally:
        for client in clients:
            client.close()
        server.server_close()


def test_a_failed_accept_gives_its_slot_back():
    server = product_service.ProductHTTPServer(
        ("127.0.0.1", 0), BaseHTTPRequestHandler, max_connections=1)
    listener = server.socket

    def refuse():
        raise OSError("too many open files")

    try:
        server.socket = SimpleNamespace(accept=refuse)
        with pytest.raises(OSError, match="too many open files"):
            server.get_request()
        server.socket = listener
        client = socket.create_connection(server.server_address)
        request, _ = server.get_request()   # the one slot is free again
        server.shutdown_request(request)
        client.close()
    finally:
        server.socket = listener
        server.server_close()
