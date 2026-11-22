"""A client that never speaks cannot stall the product listener (audit 2026-11-10, F-01).

serve_product() used to wrap the LISTENING socket in TLS. With Python's ssl
module that runs each client's handshake inside accept(), on the one thread
that runs serve_forever(), before any handler thread exists and before the
socket timeout is set. One client that connected and sent nothing held that
thread, so every other client waited for as long as it stayed connected, and
so did shutdown().

These tests hold stalled clients against the real server, plain and TLS,
through conftest's product_server harness, and pin three things:

- the listener: another client is served at once whatever a stalled client
  does, and shutdown() never waits for one;
- the cost of a stalled client: one connection thread for one socket timeout,
  the handshake included, then the server closes it, with no response and no
  traceback, and a failed handshake is closed the same way;
- the structure: no TLS handshake runs on the thread that accepts connections.
"""

import hashlib
import queue
import select
import socket
import ssl
import threading
import time

import pytest

from conftest import TLS_HOSTNAME, HTTPReply, http_request_bytes
from product_service import AuthRegistry, ProductService, tls_context

TRANSPORTS = ("plain", "tls")

OPS_TOKEN = "listener-ops-token-with-enough-entropy-for-a-test-fixture"
REQUEST_ID = "listener-request-0001"

# Stalled clients are held against a server whose socket timeout is
# LONG_TIMEOUT_S, while a well-behaved client must be answered within
# PROMPT_S. A listener that waits on a stalled client fails, even if it waited
# only for one timeout.
LONG_TIMEOUT_S = 30
PROMPT_S = 5.0
# Where a test waits for the server to close a stalled client, the socket
# timeout is SHORT_TIMEOUT_S and the client allows CLOSE_SLACK_S on top.
SHORT_TIMEOUT_S = 1.0
CLOSE_SLACK_S = 4.0
# A byte every DRIP_INTERVAL_S is well inside SHORT_TIMEOUT_S per read.
DRIP_INTERVAL_S = 0.1
# Time for the serving thread to take a connection that was just opened.
SETTLE_S = 0.3


class _Agent:
    """Only the attributes ProductService reads; no test here runs a turn."""

    manifest = {"read_file": {}}
    memory = None
    _mcp = object()

    def turn_with_usage(self, session, message, *, allowed_tools=None,
                        deadline_monotonic=None):
        raise AssertionError("no test in this file runs a turn")

    def turn_telemetry(self):
        return {"forge_queue_wait_ms": 0.0, "tool_calls": 0, "retries": 0}


def _service(records=None):
    auth = AuthRegistry([{
        "sha256": hashlib.sha256(OPS_TOKEN.encode()).hexdigest(),
        "principal": "operator",
        "tenant": "ops",
        "scopes": ["ops:read"],
        "tools": [],
    }])
    sink = (lambda record: None) if records is None else records.append
    return ProductService(_Agent(), auth, log_sink=sink,
                          requests_per_minute=1_000_000)


def _headers():
    return {"Authorization": f"Bearer {OPS_TOKEN}", "X-Request-ID": REQUEST_ID}


def _health(server, timeout=PROMPT_S):
    """GET /v1/health as a well-behaved client: the reply, or the error that
    stopped it, and how long it took."""
    started = time.monotonic()
    try:
        outcome = server.request("GET", "/v1/health", headers=_headers(),
                                 timeout=timeout)
    except OSError as error:  # ssl.SSLError and TimeoutError are OSErrors
        outcome = error
    return outcome, time.monotonic() - started


def _assert_served_promptly(outcome, took, context):
    assert isinstance(outcome, HTTPReply), (
        f"{context}: a well-behaved client was not served: {outcome!r} after "
        f"{took:.1f}s. The listener is waiting on a stalled client (audit F-01).")
    assert outcome.status == 200, outcome
    assert outcome.json() == {"status": "ok", "request_id": REQUEST_ID}
    assert took < PROMPT_S, f"{context}: answered only after {took:.1f}s"


def _read_until_closed(sock, *, since, limit):
    """Read from `sock` until the server closes it (an orderly close or a
    reset) or until `limit` seconds after `since`. Returns the bytes received
    and the seconds from `since` to the close, or None if it was still open."""
    received = b""
    while True:
        remaining = since + limit - time.monotonic()
        if remaining <= 0:
            return received, None
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(65536)
        except TimeoutError:
            return received, None
        except OSError:  # reset by the server: closed, abortively
            return received, time.monotonic() - since
        if not chunk:
            return received, time.monotonic() - since
        received += chunk


def _tracebacks(stderr):
    return [line for line in stderr.splitlines()
            if "Traceback (most recent call last)" in line
            or "Exception occurred during processing of request" in line]


def _http_requests(records):
    return [r for r in records if r.get("event") == "http_request"]


def _client_hello():
    """The first flight of a real TLS client, as bytes: a ClientHello."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = context.wrap_bio(incoming, outgoing, server_hostname=TLS_HOSTNAME)
    try:
        client.do_handshake()
    except ssl.SSLWantReadError:
        pass
    hello = outgoing.read()
    assert hello[:1] == b"\x16", "expected a TLS handshake record"
    return hello


def _watch_connections(server):
    """Report each connection when the server is done with it: its handler
    thread has returned, any traceback is already printed and the socket is
    closed. A connection that never reaches a handler thread is never
    reported."""
    finished = queue.Queue()
    run = server.server.process_request_thread

    def process_request_thread(request, client_address):
        try:
            run(request, client_address)
        finally:
            finished.put(client_address)

    server.server.process_request_thread = process_request_thread
    return finished


@pytest.fixture(params=TRANSPORTS)
def transport(request):
    return request.param


# ── kinds of stalled client ─────────────────────────────────────────────


def _stall_silent(server):
    return server.connect(tls=False)  # connects, never sends a byte


def _stall_partial_record_header(server):
    sock = server.connect(tls=False)
    sock.sendall(b"\x16\x03\x01")  # three bytes of a five-byte TLS record header
    return sock


def _stall_after_client_hello(server):
    sock = server.connect(tls=False)
    sock.sendall(_client_hello())  # then never answers the server's flight
    return sock


def _stall_mid_request_line(server):
    sock = server.connect(tls=False)
    sock.sendall(b"GET /v1/health HT")  # no end of line, ever
    return sock


def _stall_after_handshake(server):
    return server.connect()  # a completed TLS handshake, then nothing


# name -> (open one, the transports it applies to). Opened in this order; the
# one that completes a handshake goes first, so that it can complete even on a
# listener that the next one stalls.
STALLS = {
    "TLS handshake done, then silent": (_stall_after_handshake, {"tls"}),
    "silent": (_stall_silent, {"plain", "tls"}),
    "partial TLS record header": (_stall_partial_record_header, {"plain", "tls"}),
    "half a request line": (_stall_mid_request_line, {"plain", "tls"}),
    "ClientHello, then silent": (_stall_after_client_hello, {"tls"}),
}


def _open_stalls(server, transport):
    return {name: (open_stall(server), time.monotonic())
            for name, (open_stall, transports) in STALLS.items()
            if transport in transports}


# ── the audit's scenario ────────────────────────────────────────────────


def test_a_silent_client_does_not_hold_up_the_next_client(product_server, transport):
    server = product_server(_service(), tls=transport == "tls",
                            socket_timeout_s=LONG_TIMEOUT_S)
    silent = server.connect(tls=False)  # connects, never sends a byte
    time.sleep(SETTLE_S)

    outcome, took = _health(server)

    _assert_served_promptly(outcome, took, "one silent client connected")
    # Served alongside it, not instead of it: the silent client keeps its
    # 30-second timeout and is still connected.
    silent.setblocking(False)
    with pytest.raises(BlockingIOError):
        silent.recv(1)


def test_a_silent_client_is_closed_after_one_socket_timeout(product_server, transport,
                                                            capfd):
    records = []
    server = product_server(_service(records), tls=transport == "tls",
                            socket_timeout_s=SHORT_TIMEOUT_S)
    silent = server.connect(tls=False)  # connects, never sends a byte
    limit = SHORT_TIMEOUT_S + CLOSE_SLACK_S

    received, closed_after = _read_until_closed(silent, since=time.monotonic(),
                                                limit=limit)

    assert closed_after is not None, (
        f"a client that never spoke was still connected {limit:.0f}s after it "
        f"connected, with a {SHORT_TIMEOUT_S:.0f}s socket timeout: the server never "
        f"closed it")
    assert closed_after >= SHORT_TIMEOUT_S / 2, (
        f"closed after {closed_after:.2f}s, before its {SHORT_TIMEOUT_S:.0f}s timeout")
    assert received == b"", f"a client that never spoke received {received[:80]!r}"
    assert _tracebacks(capfd.readouterr().err) == []
    assert _http_requests(records) == []


def test_a_silent_client_does_not_hold_up_shutdown(product_server, transport):
    server = product_server(_service(), tls=transport == "tls",
                            socket_timeout_s=LONG_TIMEOUT_S)
    server.connect(tls=False)  # connects, never sends a byte; closed in teardown
    time.sleep(SETTLE_S)

    stopper = threading.Thread(target=server.server.shutdown, daemon=True,
                               name="stop-with-a-silent-client")
    stopper.start()
    stopper.join(PROMPT_S)

    assert not stopper.is_alive(), (
        f"shutdown() was still waiting after {PROMPT_S:.0f}s, held by a client "
        f"that never spoke: the serving thread is busy with it (audit F-01)")


# ── the class: no stalled client delays anyone else ─────────────────────


def test_no_stalled_client_delays_another_client(product_server, transport):
    server = product_server(_service(), tls=transport == "tls",
                            socket_timeout_s=LONG_TIMEOUT_S)
    stalled = _open_stalls(server, transport)
    time.sleep(SETTLE_S)

    for attempt in range(3):
        outcome, took = _health(server)
        _assert_served_promptly(
            outcome, took, f"attempt {attempt + 1}, with these stalled clients "
                           f"connected: {sorted(stalled)}")


def test_every_stalled_client_is_closed_within_one_socket_timeout(
        product_server, transport, capfd):
    records = []
    server = product_server(_service(records), tls=transport == "tls",
                            socket_timeout_s=SHORT_TIMEOUT_S)
    stalled = _open_stalls(server, transport)
    limit = SHORT_TIMEOUT_S + CLOSE_SLACK_S

    for name, (sock, since) in stalled.items():
        received, closed_after = _read_until_closed(sock, since=since, limit=limit)
        assert closed_after is not None, (
            f"{name}: still connected {limit:.0f}s after it connected, with a "
            f"{SHORT_TIMEOUT_S:.0f}s socket timeout")
        assert b"HTTP/" not in received, f"{name}: received {received[:120]!r}"

    assert _tracebacks(capfd.readouterr().err) == []
    assert _http_requests(records) == []


def test_a_handshake_gets_one_socket_timeout_in_total(product_server):
    """CPython bounds do_handshake() by the socket timeout as a whole, not per
    read, so a client that drips its ClientHello a byte at a time, each byte
    well inside the timeout, is still cut off after one timeout."""
    server = product_server(_service(), tls=True, socket_timeout_s=SHORT_TIMEOUT_S)
    hello = _client_hello()
    limit = SHORT_TIMEOUT_S + CLOSE_SLACK_S
    assert len(hello) * DRIP_INTERVAL_S > 3 * limit  # the drip outlasts the test
    sock = server.connect(tls=False)
    since = time.monotonic()
    closed_after = None

    for byte in hello:
        try:
            sock.sendall(bytes([byte]))
            readable, _, _ = select.select([sock], [], [], DRIP_INTERVAL_S)
            if readable and not sock.recv(65536):
                closed_after = time.monotonic() - since
        except OSError:  # reset, or a broken pipe: the server closed it
            closed_after = time.monotonic() - since
        if closed_after is not None or time.monotonic() - since > limit:
            break

    assert closed_after is not None, (
        f"a client dripping its ClientHello a byte every {DRIP_INTERVAL_S}s was "
        f"still handshaking {limit:.0f}s later, with a {SHORT_TIMEOUT_S:.0f}s "
        f"socket timeout")
    assert closed_after <= limit


# ── a failed handshake ──────────────────────────────────────────────────


def _plaintext_http(server):
    sock = server.connect(tls=False)
    sock.sendall(http_request_bytes("GET", "/v1/health", headers=_headers()))
    received, _ = _read_until_closed(sock, since=time.monotonic(), limit=PROMPT_S)
    return received


def _hang_up_mid_handshake(server):
    sock = server.connect(tls=False)
    sock.sendall(_client_hello())
    flight = sock.recv(65536)  # the server's answer: it is mid-handshake
    assert flight[:1] == b"\x16", flight[:40]
    sock.shutdown(socket.SHUT_WR)  # and the client hangs up
    rest, _ = _read_until_closed(sock, since=time.monotonic(), limit=PROMPT_S)
    return flight + rest


def _untrusted_certificate(server):
    distrustful = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # trusts no certificate
    with pytest.raises(ssl.SSLCertVerificationError):
        distrustful.wrap_socket(server.connect(tls=False),
                                server_hostname=TLS_HOSTNAME)
    return b""


FAILED_HANDSHAKES = {
    "plaintext HTTP": _plaintext_http,
    "hang-up mid-handshake": _hang_up_mid_handshake,
    "untrusted certificate": _untrusted_certificate,
}


@pytest.mark.parametrize("failure", sorted(FAILED_HANDSHAKES))
def test_a_failed_handshake_is_closed_without_a_response_or_a_traceback(
        product_server, capfd, failure):
    records = []
    server = product_server(_service(records), tls=True,
                            socket_timeout_s=LONG_TIMEOUT_S)
    finished = _watch_connections(server)

    received = FAILED_HANDSHAKES[failure](server)
    try:
        finished.get(timeout=PROMPT_S)
    except queue.Empty:
        pytest.fail(
            f"{failure}: no connection thread finished this connection within "
            f"{PROMPT_S:.0f}s, so its handshake never ran on one (audit F-01)",
            pytrace=False)

    assert b"HTTP/" not in received, f"{failure}: received {received[:120]!r}"
    assert _tracebacks(capfd.readouterr().err) == []
    assert _http_requests(records) == []
    outcome, took = _health(server)
    _assert_served_promptly(outcome, took, f"after a failed handshake ({failure})")


# ── the structure ───────────────────────────────────────────────────────


def test_no_handshake_runs_on_the_thread_that_accepts_connections(product_server,
                                                                   tls_material):
    """The class of F-01: client-paced work on the accepting thread. Record the
    thread of every accept() and every server-side do_handshake()."""
    events = []

    class RecordingSSLSocket(ssl.SSLSocket):
        def accept(self):
            events.append(("accept", threading.get_ident()))
            return super().accept()

        def do_handshake(self, *args, **kwargs):
            events.append(("handshake", threading.get_ident()))
            return super().do_handshake(*args, **kwargs)

    context = tls_context(str(tls_material.cert), str(tls_material.key))
    context.sslsocket_class = RecordingSSLSocket
    server = product_server(_service(), tls=True, ssl_context=context)

    for _ in range(3):
        outcome, took = _health(server)
        _assert_served_promptly(outcome, took, "a TLS client")

    accepting = {ident for kind, ident in events if kind == "accept"}
    handshaking = [ident for kind, ident in events if kind == "handshake"]
    assert len(accepting) == 1, events  # one serving thread accepts them all
    assert len(handshaking) == 3, events  # one handshake per connection
    assert not accepting & set(handshaking), (
        "a TLS handshake ran on the thread that accepts connections, where a "
        "client that never speaks stalls every other client (audit F-01)")
