"""Every answer the product listener writes follows the error contract, and
the ways a connection fails are counted, not printed (audit 2026-11-10,
F-25, with the transport items T1, S1, S2 and C1 recorded for it).

docs/api.md, "Common response rules": a JSON response has
`Content-Type: application/json`, `Cache-Control: no-store` and an
`X-Request-ID`, and an error has one shape, {"error": {"code", "message"},
"request_id"}. Before this:

- every answer, a 401 to a client with no credential included, carried
  `Server: BaseHTTP/0.6 Python/3.12.13`;
- the answers the standard library writes itself, to a request the server
  cannot read or route (400, 414, 431, 501, 505), were HTML pages whose
  reason phrase could repeat the request ("501 Unsupported method ('PUT')"),
  and those it writes before it has read a version, and every answer to a
  request line without one, had no status line or headers at all;
- the transport's own 400 invalid_content_length was not logged, not
  counted, and had no X-Request-ID or Cache-Control, and a chunked body was
  read as an empty one;
- a client that went away before its answer was written put a traceback on
  stderr, plain and TLS, as did a log line that could not be written, which
  also cost the client its answer;
- a failed handshake, a request head that timed out and one cut off by a
  reset left no trace at all;
- a drain could report complete with the answer of a finished turn still
  unwritten, and the process then exited without it.

What a client sends never reaches an answer's status line or body, or a
log record about a connection that ended without a request.
"""

import ast
import errno
import hashlib
import http.client
import http.server
import inspect
import io
import json
import queue
import re
import socket
import socketserver
import ssl
import struct
import threading
import time
from http import HTTPStatus

import pytest
from hypothesis import given, settings, strategies as st

import product_service
from conftest import http_request_bytes
from product_service import (
    MAX_REQUEST_HEAD_BYTES,
    REQUEST_ID_RE,
    AuthRegistry,
    ProductService,
    drain_service,
)

TRANSPORTS = ("plain", "tls")
SERVER = "sigil-pi"
REQUEST_ID = "contract-request-0001"
CHAT_TOKEN = "contract-chat-token-with-enough-entropy-for-a-test-fixture"
OPS_TOKEN = "contract-ops-token-with-enough-entropy-for-a-test-fixture"
USAGE = {"input_tokens": 2, "output_tokens": 1}
CHAT_BODY = json.dumps({"session": "project", "message": "hello"}).encode()
# Text that appears only in what a test's client sends.
MARKER = "zq-client-text-7f3a"
METHOD_MARKER = "ZQCLIENTTEXT"
PROMPT_S = 5.0


class Agent:
    """Replies "answer". While held, each turn waits for release()."""

    manifest = {"read_file": {}, "write_file": {}}
    memory = None
    _mcp = object()

    def __init__(self):
        self.calls = []
        self.entered = threading.Event()
        self._go = threading.Event()
        self._go.set()

    def hold(self):
        self._go.clear()

    def release(self):
        self._go.set()

    def turn_with_usage(self, session, message, *, allowed_tools=None,
                        deadline_monotonic=None):
        self.calls.append(session)
        self.entered.set()
        assert self._go.wait(15), "the test never released the turn"
        return "answer", dict(USAGE)

    def turn_telemetry(self):
        return {"forge_queue_wait_ms": 0.0, "tool_calls": 0, "retries": 0}


def _entry(token, principal, tenant, scopes):
    return {"sha256": hashlib.sha256(token.encode()).hexdigest(),
            "principal": principal, "tenant": tenant, "scopes": scopes, "tools": []}


def _service(agent=None, records=None, log_sink=None):
    if log_sink is None:
        log_sink = records.append if records is not None else (lambda record: None)
    return ProductService(
        agent or Agent(),
        AuthRegistry([_entry(CHAT_TOKEN, "alice", "tenant-a", ["chat"]),
                      _entry(OPS_TOKEN, "operator", "ops", ["ops:read"])]),
        log_sink=log_sink)


@pytest.fixture(params=TRANSPORTS)
def transport(request):
    return request.param


def _bearer(token, **extra):
    return {"Authorization": f"Bearer {token}", **extra}


def _chat_request(**extra):
    return http_request_bytes("POST", "/v1/chat", headers=_bearer(CHAT_TOKEN, **extra),
                              body=CHAT_BODY)


def _exchange(server, raw, *, tls=None, within=PROMPT_S):
    """Send `raw` on a new connection and return every byte of the answer,
    up to the server closing the connection."""
    sock = server.connect(tls=tls, timeout=within)
    try:
        sock.sendall(raw)
        data = b""
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(65536)
            except (TimeoutError, ConnectionError, ssl.SSLError):
                break
            if not chunk:
                break
            data += chunk
        return data
    finally:
        sock.close()


def _parse(data):
    """(status line, headers with lower-case names, body) of one answer. An
    answer without a status line fails the test, with what it began with."""
    assert data.startswith(b"HTTP/1."), (
        f"the answer has no status line or headers: it began {data[:60]!r}")
    head, _, body = data.partition(b"\r\n\r\n")
    status_line, *lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return status_line, headers, body


def _assert_product_error(data, status, code=None, *, request_id=None, head=False):
    """The answer is `status` in the product's error shape, with the
    contract's headers, and repeats nothing the client sent."""
    status_line, headers, body = _parse(data)
    assert status_line == f"HTTP/1.0 {status} {HTTPStatus(status).phrase}", status_line
    assert headers.get("content-type") == "application/json", data[:300]
    assert headers.get("cache-control") == "no-store", data[:300]
    assert REQUEST_ID_RE.fullmatch(headers.get("x-request-id", "")), data[:300]
    for text in (MARKER, METHOD_MARKER):
        assert text not in data.decode("latin-1"), f"the answer repeats the request: {data[:300]!r}"
    if head:
        assert body == b"", f"an answer to HEAD carried a body: {body[:80]!r}"
        assert int(headers["content-length"]) > 0
        assert headers.get("server") == SERVER, headers.get("server")
        return None
    assert int(headers["content-length"]) == len(body), data[:300]
    payload = json.loads(body)
    assert set(payload) == {"error", "request_id"}, payload
    assert set(payload["error"]) == {"code", "message"}, payload
    assert re.fullmatch(r"[a-z][a-z0-9_]*", payload["error"]["code"]), payload
    assert payload["error"]["message"] and isinstance(payload["error"]["message"], str)
    assert payload["request_id"] == headers["x-request-id"], (payload, headers)
    if code is not None:
        assert payload["error"]["code"] == code, payload
    if request_id is not None:
        assert payload["request_id"] == request_id, payload
    assert headers.get("server") == SERVER, headers.get("server")
    return payload


def _watch(server):
    """A queue that receives one item each time the server has finished a
    connection: its handler thread has returned, so anything it was going to
    log, count or print has been."""
    finished = queue.Queue()
    run = server.server.process_request_thread

    def watched(request, client_address):
        try:
            run(request, client_address)
        finally:
            finished.put(True)

    server.server.process_request_thread = watched
    return finished


def _finished(watched, count=1, within=10.0):
    for _ in range(count):
        try:
            watched.get(timeout=within)
        except queue.Empty:
            pytest.fail(f"the server did not finish a connection within {within}s",
                        pytrace=False)


def _reset(sock):
    """Close `sock` with a TCP reset, as a client that goes away does."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()


def _tracebacks(stderr):
    return [line for line in stderr.splitlines()
            if "Traceback (most recent call last)" in line
            or "Exception occurred during processing of request" in line]


def _metrics(service):
    snapshot = service.metrics.snapshot()
    return ([(r["method"], r["route"], r["status"], r["count"]) for r in snapshot["requests"]],
            snapshot["error_classes"])


# ── the Server header ───────────────────────────────────────────────────


def test_no_answer_names_the_interpreter_or_a_version(product_server, transport):
    """Dispatched answers, the transport's own and the standard library's:
    each names the product and nothing more."""
    server = product_server(_service(), tls=transport == "tls")
    answers = {
        "200": http_request_bytes("GET", "/v1/health", headers=_bearer(OPS_TOKEN)),
        "401": http_request_bytes("GET", "/v1/health"),
        "framing 400": http_request_bytes(
            "POST", "/v1/chat", headers=_bearer(CHAT_TOKEN, **{"Content-Length": "+3"})),
        "501": b"PUT /v1/chat HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n",
    }
    for name, raw in answers.items():
        _, headers, _ = _parse(_exchange(server, raw))
        assert headers.get("server") == SERVER, (
            f"{name}: Server: {headers.get('server')!r} (audit F-25: the interpreter and "
            f"its version were in every answer)")


# ── what the standard library answers ───────────────────────────────────

_LONG_PATH = "/" + "a" * 70000
REFUSALS = {
    "an unknown method": (
        f"PUT /v1/chat HTTP/1.1\r\nHost: x\r\nX-Request-ID: {REQUEST_ID}\r\n"
        f"X-Note: {MARKER}\r\nContent-Length: 0\r\n\r\n",
        501, "method_not_supported", REQUEST_ID),
    "a made-up method": (f"{METHOD_MARKER} /v1/chat HTTP/1.1\r\n\r\n",
                         501, "method_not_supported", None),
    "a request line too long": (f"GET {_LONG_PATH}{MARKER} HTTP/1.1\r\n\r\n",
                                414, "request_line_too_long", None),
    "a head over the product's limit": (
        "GET /v1/health HTTP/1.1\r\n" + "".join(
            f"X-Fill-{index}: {MARKER}{'x' * (MAX_REQUEST_HEAD_BYTES // 2)}\r\n"
            for index in range(2)) + "\r\n",
        431, "request_head_too_large", None),
    "more header lines than the standard library takes": (
        "GET /v1/health HTTP/1.1\r\n" + "".join(
            f"X-Line-{index}: {MARKER}\r\n" for index in range(101)) + "\r\n",
        431, "request_head_too_large", None),
    "a version that is not HTTP": (f"GET /{MARKER} HTTX/1.1\r\n\r\n",
                                   400, "malformed_request", None),
    "a request line of four words": (f"GET /{MARKER} extra HTTP/1.1\r\n\r\n",
                                     400, "malformed_request", None),
    "HTTP/2.0": (f"GET /{MARKER} HTTP/2.0\r\n\r\n", 505, "http_version_not_supported", None),
    "a request line without a version, for a method other than GET": (
        f"BREW /{MARKER}\r\n\r\n", 400, "malformed_request", None),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_a_request_the_server_cannot_read_or_route_gets_a_product_error(
        product_server, case):
    raw, status, code, request_id = REFUSALS[case]
    server = product_server(_service())

    data = _exchange(server, raw.encode("latin-1"))

    _assert_product_error(data, status, code, request_id=request_id)


def test_whatever_status_the_standard_library_refuses_with_the_answer_is_a_product_error(
        product_server, monkeypatch):
    """The class: BaseHTTPRequestHandler.send_error, called with any error
    status and with text from the request as its message and explanation,
    as the standard library calls it, answers in the product's shape with
    the standard reason phrase and none of that text."""
    server = product_server(_service())
    handler = server.server.RequestHandlerClass
    statuses = [status for status in HTTPStatus if 400 <= status < 600]
    for status in statuses:
        monkeypatch.setattr(handler, "do_GET", lambda self, status=status: self.send_error(
            status, f"refused {self.path!r}", f"because of {self.path!r}"))
        data = _exchange(server, f"GET /{MARKER} HTTP/1.1\r\n\r\n".encode())
        _assert_product_error(data, status)


def test_a_refused_head_request_gets_the_headers_of_its_answer_and_no_body(product_server):
    server = product_server(_service())

    data = _exchange(server, b"HEAD /v1/health HTTP/1.1\r\nHost: x\r\n\r\n")

    _assert_product_error(data, 501, head=True)


def test_a_request_line_without_a_version_is_answered_with_a_status_line_and_headers(
        product_server):
    """The standard library takes "GET /path" as HTTP/0.9 and answers it with
    a body alone: no status, no Content-Type, no X-Request-ID."""
    server = product_server(_service())

    data = _exchange(server, f"GET /v1/health\r\nAuthorization: Bearer {OPS_TOKEN}\r\n\r\n"
                     .encode())

    status_line, headers, body = _parse(data)
    assert status_line == "HTTP/1.0 200 OK", status_line
    assert headers["content-type"] == "application/json"
    assert headers["cache-control"] == "no-store"
    assert json.loads(body)["request_id"] == headers["x-request-id"]



def _python_3_14_parse_request(original):
    """http.server's parse_request as Python 3.14 has it for a request line
    without a version: HTTP/0.9, whose headers are "not expected at all", so
    they are left unread. Python 3.12 reads them."""
    def parse_request(self):
        requestline = str(self.raw_requestline, "iso-8859-1").rstrip("\r\n")
        words = requestline.split()
        if len(words) == 2 and words[0] == "GET":
            self.requestline = requestline
            self.request_version = self.default_request_version
            self.close_connection = True
            self.command, self.path = words
            if self.path.startswith("//"):
                self.path = "/" + self.path.lstrip("/")
            self.headers = {}
            return True
        return original(self)
    return parse_request


def test_a_request_line_without_a_version_is_read_alike_on_every_interpreter(
        product_server, monkeypatch):
    """Python 3.14's http.server reads a request line without a version as
    HTTP/0.9 and leaves its headers unread, where 3.12 reads them, so the same
    bytes were answered 200 on 3.12 and 401 on 3.14 (found by the remediation's
    final run on 3.14). The gate runs 3.12 only, so this puts 3.14's reading
    under the product on any interpreter: the product must read such a line
    as HTTP/1.0, headers included, before the standard library chooses."""
    monkeypatch.setattr(
        http.server.BaseHTTPRequestHandler, "parse_request",
        _python_3_14_parse_request(http.server.BaseHTTPRequestHandler.parse_request))
    server = product_server(_service())

    data = _exchange(server, f"GET /v1/health\r\nAuthorization: Bearer {OPS_TOKEN}\r\n\r\n"
                     .encode())

    status_line, headers, body = _parse(data)
    assert status_line == "HTTP/1.0 200 OK", status_line
    assert json.loads(body)["request_id"] == headers["x-request-id"]

# ── the transport's own refusals ────────────────────────────────────────


def test_a_framing_refusal_is_answered_logged_and_counted_as_dispatch_answers_an_error(
        product_server, transport):
    """T1's sweep (4): the transport's 400 invalid_content_length bypassed
    dispatch, so it wrote no request log line, was in no request metric and
    had neither X-Request-ID nor Cache-Control."""
    records = []
    service = _service(records=records)
    server = product_server(service, tls=transport == "tls")
    watched = _watch(server)

    data = _exchange(server, http_request_bytes(
        "POST", "/v1/chat",
        headers=_bearer(CHAT_TOKEN, **{"Content-Length": "+3", "X-Request-ID": REQUEST_ID})))
    _finished(watched)

    payload = _assert_product_error(data, 400, "invalid_content_length", request_id=REQUEST_ID)
    assert payload["error"]["message"] == "invalid or oversized Content-Length"
    assert [(r["event"], r["route"], r["status"], r["failure_classification"],
             r["principal"], r["request_id"]) for r in records] == [
        ("http_request", "unknown", 400, "invalid_content_length", None, REQUEST_ID)]
    requests, classes = _metrics(service)
    assert requests == [("POST", "unknown", 400, 1)]
    assert classes == {"invalid_content_length": 1}
    assert ('sigil_pi_http_requests_total{method="POST",route="unknown",status="400"} 1'
            in product_service.render_prometheus_metrics(
                service.operational_metrics(), "test"))


TRANSFER_CODINGS = {
    "chunked, without a Content-Length": {"Transfer-Encoding": "chunked"},
    "chunked, with a Content-Length": {"Transfer-Encoding": "chunked",
                                       "Content-Length": str(len(CHAT_BODY))},
    "a coding the server does not know": {"Transfer-Encoding": "gzip, chunked"},
}


@pytest.mark.parametrize("case", sorted(TRANSFER_CODINGS))
def test_a_request_with_a_transfer_coding_is_a_framing_error(product_server, case):
    """T1's sweep (3). The server reads a body by its Content-Length only. A
    chunked body was read as an empty one and answered 400 invalid_request,
    and a Transfer-Encoding beside a Content-Length, which RFC 9112 section
    6.3 says overrides it and ought to be handled as an error, was ignored."""
    agent = Agent()
    server = product_server(_service(agent))
    chunked = b"%x\r\n%s\r\n0\r\n\r\n" % (len(CHAT_BODY), CHAT_BODY)

    data = _exchange(server, http_request_bytes(
        "POST", "/v1/chat", headers=_bearer(CHAT_TOKEN, **TRANSFER_CODINGS[case]))
        + chunked)

    _assert_product_error(data, 400, "invalid_content_length")
    assert agent.calls == []


@settings(max_examples=200, deadline=None)
@given(codings=st.lists(st.text(alphabet="chunkedgzipdflat, \t", max_size=12),
                        min_size=1, max_size=2),
       lengths=st.lists(st.text(alphabet="0123456789", min_size=1, max_size=4), max_size=2))
def test_a_transfer_coding_is_never_taken_as_a_length(codings, lengths):
    """The property behind the test above, through the standard library's
    header parser as the server reads it: whatever a Transfer-Encoding says,
    and whatever Content-Length comes with it, no length is declared."""
    raw = ("".join(f"Transfer-Encoding: {coding}\r\n" for coding in codings)
           + "".join(f"Content-Length: {length}\r\n" for length in lengths) + "\r\n")
    headers = http.client.parse_headers(io.BytesIO(raw.encode("latin-1")))

    assert product_service._declared_length(headers) is None


def test_only_write_puts_an_answer_on_the_connection():
    """The class behind the transport's answers: each goes through
    Handler._write, whose callers give it the contract's headers and which
    counts a write that fails instead of letting socketserver print it. The
    transport's 400 and the standard library's send_error each wrote their
    own answer, outside both."""
    source = inspect.getsource(product_service.serve_product)
    handler = next(node for node in ast.walk(ast.parse(source))
                   if isinstance(node, ast.ClassDef) and node.name == "Handler")
    wire = {"wfile", "send_header", "end_headers", "flush_headers", "send_response",
            "send_response_only", "sendall", "send", "write"}
    methods = [node for node in handler.body if isinstance(node, ast.FunctionDef)]
    for method in methods:
        if method.name in ("_write", "send_response"):
            continue
        touched = sorted({node.attr for node in ast.walk(method)
                          if isinstance(node, ast.Attribute) and node.attr in wire})
        assert not touched, (
            f"Handler.{method.name} writes to the connection itself ({touched}), not "
            f"through _write")
    assert {"send_error", "version_string", "_write"} <= {method.name for method in methods}


# ── connections that fail ───────────────────────────────────────────────


def test_a_client_that_leaves_before_its_answer_gets_a_content_free_event_not_a_traceback(
        product_server, transport, capfd):
    """The turn ran and was logged; its answer could not be written. That is
    a second record, with the request's id and nothing from the request, and
    a count, and nothing on stderr."""
    agent, records = Agent(), []
    service = _service(agent, records)
    server = product_server(service, tls=transport == "tls")
    watched = _watch(server)
    agent.hold()
    sock = server.connect()
    sock.sendall(_chat_request(**{"X-Request-ID": REQUEST_ID}))
    assert agent.entered.wait(PROMPT_S), "the turn never started"

    _reset(sock)
    agent.release()
    _finished(watched)

    assert _tracebacks(capfd.readouterr().err) == []
    assert [(r["event"], r["status"]) for r in records][:1] == [("http_request", 200)]
    assert records[1:] == [{"event": "http_response_failed", "request_id": REQUEST_ID,
                            "status": 200, "failure_classification": "client_disconnected"}]
    assert _metrics(service) == ([("POST", "/v1/chat", 200, 1)],
                                 {"client_disconnected": 1})


ANSWERS = {
    "a dispatched answer": (lambda: http_request_bytes(
        "GET", "/v1/health", headers=_bearer(OPS_TOKEN)), True),
    "a dispatched refusal": (lambda: http_request_bytes("GET", "/v1/health"), True),
    "the transport's refusal": (lambda: http_request_bytes(
        "POST", "/v1/chat", headers=_bearer(CHAT_TOKEN, **{"Content-Length": "x"})), True),
    "the standard library's refusal": (
        lambda: b"PUT /v1/chat HTTP/1.1\r\nContent-Length: 0\r\n\r\n", False),
    "a refused HEAD": (lambda: b"HEAD /v1/chat HTTP/1.1\r\n\r\n", False),
}
WRITE_FAILURES = {
    "broken pipe": lambda: BrokenPipeError(errno.EPIPE, "Broken pipe"),
    "reset": lambda: ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"),
    "TLS EOF": lambda: ssl.SSLEOFError(8, "EOF occurred in violation of protocol"),
    "write timeout": lambda: TimeoutError("timed out"),
}


@pytest.mark.parametrize("failure", sorted(WRITE_FAILURES))
@pytest.mark.parametrize("answer", sorted(ANSWERS))
def test_no_answer_that_cannot_be_written_prints_a_traceback(
        product_server, monkeypatch, capfd, answer, failure):
    """The class: whatever the answer and however writing it fails, the
    failure is counted, a logged request gets its content-free record, and
    nothing reaches stderr."""
    records = []
    service = _service(records=records)
    server = product_server(service)
    watched = _watch(server)
    raw, logged = ANSWERS[answer]

    def fail(self, data):
        raise WRITE_FAILURES[failure]()

    monkeypatch.setattr(socketserver._SocketWriter, "write", fail)
    sock = server.connect()
    sock.sendall(raw())
    _finished(watched)
    sock.close()

    assert _tracebacks(capfd.readouterr().err) == []
    assert _metrics(service)[1].get("client_disconnected") == 1
    failed = [r for r in records if r["event"] == "http_response_failed"]
    if logged:
        assert [r["event"] for r in records] == ["http_request", "http_response_failed"]
        assert failed == [{"event": "http_response_failed",
                           "request_id": records[0]["request_id"],
                           "status": records[0]["status"],
                           "failure_classification": "client_disconnected"}]
    else:
        assert records == []


def _silent(server):
    return server.connect(tls=False)


def _plaintext_to_tls(server):
    sock = server.connect(tls=False)
    sock.sendall(b"GET /v1/health HTTP/1.1\r\n\r\n")
    return sock


def _partial_head(server):
    sock = server.connect()
    sock.sendall(f"GET /v1/health?{MARKER} HT".encode())
    return sock


def _signal_bytes_read(monkeypatch):
    """An event the server sets once it has read some of a request's bytes.

    A client that resets its connection may make the server's kernel drop
    the bytes the server has not read yet; the server then sees a
    connection that closed without a byte, which is not a failure and is
    not counted. Over TLS the reset can overtake the record holding the
    partial head, so a test that resets straight after sending counted
    nothing in about one run in twenty. Waiting for this event first makes
    the reset the one that cuts off a head the server already holds."""
    read = threading.Event()
    readinto = product_service._ConnectionInput.readinto

    def observed(self, buffer):
        count = readinto(self, buffer)
        if count:
            read.set()
        return count

    monkeypatch.setattr(product_service._ConnectionInput, "readinto", observed)
    return read


def _partial_head_then_reset(server):
    sock = _partial_head(server)
    assert server.bytes_read.wait(10), "the server never read the partial head"
    _reset(sock)
    return None


ENDINGS = {
    # name: (tls, open the connection, the class it is counted as)
    "plain: a head cut off by the socket timeout": (False, _partial_head, "request_timeout"),
    "plain: a client that never speaks": (False, _silent, "request_timeout"),
    "plain: a head cut off by a reset": (False, _partial_head_then_reset, "request_aborted"),
    "tls: a handshake that fails": (True, _plaintext_to_tls, "tls_handshake_failed"),
    "tls: a handshake that times out": (True, _silent, "request_timeout"),
    "tls: a head cut off by the socket timeout": (True, _partial_head, "request_timeout"),
    "tls: a head cut off by a reset": (True, _partial_head_then_reset, "request_aborted"),
}


@pytest.mark.parametrize("ending", sorted(ENDINGS))
def test_a_connection_that_ends_before_its_request_is_counted_by_how_it_ended(
        product_server, capfd, monkeypatch, ending):
    """S1's sweep (3) and S2's (5): a failed or timed-out handshake and a head
    that never arrived left no record and no count, so an operator could not
    see a flood of them. Each is now counted by class, in
    sigil_pi_http_errors_total; none writes a log line, since there is no
    request to log and nothing about the connection but how it ended."""
    tls, open_connection, failure_class = ENDINGS[ending]
    records = []
    service = _service(records=records)
    server = product_server(service, tls=tls, socket_timeout_s=1)
    server.bytes_read = _signal_bytes_read(monkeypatch)
    watched = _watch(server)

    sock = open_connection(server)
    _finished(watched)
    if sock is not None:
        sock.close()

    assert _metrics(service) == ([], {failure_class: 1})
    assert records == []
    assert _tracebacks(capfd.readouterr().err) == []


INCOMPLETE_HEADS = {
    "a request line without its line end": "GET /v1/health HTTP/1.1",
    "header lines without the blank line that ends them":
        f"GET /v1/health HTTP/1.1\r\nAuthorization: Bearer {OPS_TOKEN}\r\n",
}


@pytest.mark.parametrize("cut", sorted(INCOMPLETE_HEADS))
def test_a_head_the_client_ended_before_it_was_complete_is_not_processed(
        product_server, transport, capfd, cut):
    """S2 made a body that ends before its Content-Length a request that is
    not processed (RFC 9112 section 8). A head that ended before the line end
    or blank line that completes it, the client having closed its side, or
    reset a TLS connection, was processed as complete: dispatched, answered
    and logged. It is now what a head cut off by a reset is on a plain
    connection: request_aborted, with no answer."""
    records = []
    service = _service(records=records)
    server = product_server(service, tls=transport == "tls")
    watched = _watch(server)
    sock = server.connect()

    sock.sendall(INCOMPLETE_HEADS[cut].encode())
    sock.shutdown(socket.SHUT_WR)
    _finished(watched)
    received = b"" if transport == "tls" else sock.recv(65536)
    sock.close()

    assert records == [], f"the incomplete head was processed: {records}"
    assert _metrics(service) == ([], {"request_aborted": 1})
    assert received == b"", received[:120]
    assert _tracebacks(capfd.readouterr().err) == []


def test_a_connection_closed_without_a_byte_is_not_a_failure(product_server):
    """A TCP health check connects and closes: nothing to count."""
    service = _service()
    server = product_server(service)
    watched = _watch(server)

    server.connect().close()
    _finished(watched)

    assert _metrics(service) == ([], {})


def test_a_request_the_server_cannot_read_or_route_is_counted_by_class_not_as_a_request(
        product_server):
    """The request series, which the error-rate alert divides, holds the
    requests the service routed. A 501 is a 5xx a client chooses to cause:
    counted there, any client could page the operator with made-up methods."""
    records = []
    service = _service(records=records)
    server = product_server(service)
    watched = _watch(server)
    for _ in range(3):
        _exchange(server, b"BREW /pot HTTP/1.1\r\n\r\n")
    _exchange(server, REFUSALS["a head over the product's limit"][0].encode())
    _finished(watched, 4)

    assert _metrics(service) == ([], {"method_not_supported": 3, "request_head_too_large": 1})
    assert records == []
    text = product_service.render_prometheus_metrics(service.operational_metrics(), "test")
    assert 'sigil_pi_http_errors_total{class="method_not_supported"} 3' in text
    assert "sigil_pi_http_requests_total{" not in text


# ── the request log ─────────────────────────────────────────────────────

SINK_FAILURES = {
    "a full disk": lambda: OSError(errno.ENOSPC, "No space left on device"),
    "a closed pipe": lambda: BrokenPipeError(errno.EPIPE, "Broken pipe"),
    "a closed stdout": lambda: ValueError("I/O operation on closed file."),
}


@pytest.mark.parametrize("failure", sorted(SINK_FAILURES))
def test_a_request_is_answered_when_its_log_line_cannot_be_written(
        product_server, capfd, failure):
    """C1's sweep (7). The log sink was called in a finally outside any
    handler: a sink that raised dropped the connection of every request,
    GET /v1/ready included, with a traceback. A request whose work is done
    is answered: its effects, its charge and its audit record stand whether
    or not its log line is written, the metrics have counted it already, and
    a client denied the answer would retry work that was done."""
    def sink(record):
        raise SINK_FAILURES[failure]()

    agent = Agent()
    service = _service(agent, log_sink=sink)
    server = product_server(service)

    health = server.request("GET", "/v1/health", headers=_bearer(OPS_TOKEN))
    chat = server.request("POST", "/v1/chat", headers=_bearer(CHAT_TOKEN), body=CHAT_BODY)

    assert (health.status, health.json()["status"]) == (200, "ok")
    assert (chat.status, chat.json()["reply"]) == (200, "answer")
    assert len(agent.calls) == 1
    assert _metrics(service)[0] == [("GET", "/v1/health", 200, 1),
                                    ("POST", "/v1/chat", 200, 1)]
    assert _tracebacks(capfd.readouterr().err) == []


def test_dispatch_returns_its_answer_when_the_log_sink_raises():
    def sink(record):
        raise OSError(errno.ENOSPC, "No space left on device")

    service = _service(log_sink=sink)

    status, headers, payload = service.dispatch(
        "GET", "/v1/health", {"Authorization": f"Bearer {OPS_TOKEN}"})

    assert (status, payload["status"]) == (200, "ok")
    assert headers["X-Request-ID"] == payload["request_id"]


# ── the drain ───────────────────────────────────────────────────────────


class StalledChatLog:
    """A log sink that holds a chat request's log line until released, as a
    stalled stdout would: the turn has finished, its answer is still to be
    written."""

    def __init__(self):
        self.records = []
        self.entered = threading.Event()
        self._go = threading.Event()

    def __call__(self, record):
        if record.get("route") == "/v1/chat":
            self.entered.set()
            self._go.wait(15)
        self.records.append(record)

    def release(self):
        self._go.set()


def _chat_in_flight(product_server, log):
    service = _service(log_sink=log)
    server = product_server(service)
    replies = []
    client = threading.Thread(target=lambda: replies.append(server.request(
        "POST", "/v1/chat", headers=_bearer(CHAT_TOKEN), body=CHAT_BODY, timeout=20)),
        daemon=True)
    client.start()
    assert log.entered.wait(PROMPT_S), "the chat request never reached its log line"
    return service, server, client, replies


def test_a_drain_waits_within_its_budget_for_the_answer_of_a_finished_turn(product_server):
    """C2's note: the drain counted the turn, not its answer. With the turn
    finished and its answer still to be written, the drain reported complete,
    and the process exits once it has, ending the handler threads, which
    are daemons, with the answer unwritten."""
    log = StalledChatLog()
    service, server, client, replies = _chat_in_flight(product_server, log)
    outcome = []
    drain = threading.Thread(target=lambda: outcome.append(
        drain_service(service, server.server, (), 10)), daemon=True)

    drain.start()
    drain.join(1.0)
    still_draining = drain.is_alive()
    log.release()
    drain.join(10)
    client.join(10)

    assert still_draining, (
        f"the drain finished ({outcome}) while a finished turn's answer was still to be "
        f"written")
    assert outcome == [True]
    assert [reply.status for reply in replies] == [200]


def test_an_answer_still_unwritten_at_the_deadline_does_not_hold_or_fail_the_drain(
        product_server):
    """The answers are waited for within the one budget, not vouched for: a
    client reading slowly leaves the state as drained as it is."""
    log = StalledChatLog()
    service, server, client, replies = _chat_in_flight(product_server, log)

    started = time.monotonic()
    drained = drain_service(service, server.server, (), 1)
    took = time.monotonic() - started
    log.release()
    client.join(10)

    assert drained is True
    assert took < 3, f"the drain took {took:.1f}s on a 1s budget"
    assert [reply.status for reply in replies] == [200]
