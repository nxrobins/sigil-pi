"""Network and model-provider failures, injected where the product meets them.

The failure-injection matrix bound `network_failures` and
`unavailable_model_providers` to tests that inject nothing into the product:
the research host's retry probe, whose `_forge` is scripted; a scripted agent
that raises; and the load generator's own error classifier (audit
2026-11-10, F-17). The tests here make each failure happen outside the
product, at the model provider's end of the network, and watch the product
answer over its own socket. A chat request goes through serve_product to
ProductService; the real agent forges the model call on the real compiler;
the guest's HTTP call meets a provider that drops the connection, is not
there, or is overloaded. What the client is told, what the product counts,
and whether it serves again once the provider is back are all checked.

The agent's backoff sleeps are recorded instead of slept: the schedule is
pinned by the research host's retry tests, and here it only has to happen.
"""

import hashlib
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import API_KEY, msg, text
from product_service import AuthRegistry, ProductService, _internal_session

TOKEN = "provider-failure-chat-token-with-enough-entropy-for-a-test"
TENANT = "tenant-a"
REQUEST_ID = "provider-failure-0001"
ANSWER = "the provider answered"
# What the agent's model-call failure reads like inside the product; none of
# it may reach the client.
DIAGNOSTICS = ("R803", "trapped", "tool returned error", "llm forge failed",
               "529", "overloaded", "127.0.0.1", API_KEY)


class Provider:
    """A model provider at the far end of the network. Each request it
    receives takes the next scripted behaviour, and the last one repeats:
    "drop" reads the request and closes the connection with no reply,
    "overloaded" answers 529 with the provider's error body, and "answer"
    answers with a message."""

    def __init__(self, *behaviours):
        self.behaviours = list(behaviours)
        self.requests = 0
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                with outer._lock:
                    outer.requests += 1
                    step = outer.behaviours[
                        min(outer.requests, len(outer.behaviours)) - 1]
                if step == "drop":
                    self.close_connection = True
                    return
                if step == "overloaded":
                    status, document = 529, {
                        "type": "error",
                        "error": {"type": "overloaded_error", "message": "Overloaded"}}
                else:
                    status, document = 200, msg([text(ANSWER)])
                payload = json.dumps(document).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/v1/messages"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


def _absent_provider_url():
    """The URL of a provider that is not there: a loopback port that was free
    a moment ago and that nothing listens on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1/messages"


@pytest.fixture()
def providers():
    started = []

    def start(*behaviours):
        provider = Provider(*behaviours)
        started.append(provider)
        return provider

    yield start
    for provider in started:
        provider.close()


def _service(tmp_path, mcp, endpoint):
    """A product over a real agent that forges on the real compiler, with
    the agent's retry budget at its product default (two retries)."""
    from agent import PiAgent, SessionStore

    agent = PiAgent(endpoint, API_KEY, store=SessionStore(tmp_path / "sessions"),
                    sandbox_root=tmp_path / "sandboxes", mcp=mcp, llm_retries=2)
    sleeps = []
    agent._sleep = sleeps.append
    auth = AuthRegistry([{
        "sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
        "principal": "chat-user", "tenant": TENANT, "scopes": ["chat"], "tools": [],
    }])
    service = ProductService(agent, auth, log_sink=lambda record: None)
    return service, agent, sleeps


def _chat(server, message="hello"):
    return server.request(
        "POST", "/v1/chat",
        headers={"Authorization": f"Bearer {TOKEN}", "X-Request-ID": REQUEST_ID},
        body=json.dumps({"session": "work", "message": message}).encode(),
        timeout=120)


def test_a_dropped_provider_connection_is_retried_and_the_turn_is_served(
        product_server, mcp, providers, tmp_path):
    provider = providers("drop", "answer")
    service, agent, sleeps = _service(tmp_path, mcp, provider.url)
    server = product_server(service)

    reply = _chat(server)

    assert reply.status == 200, reply
    assert reply.json()["reply"] == ANSWER
    assert reply.headers["X-Sigil-Retries"] == "1"
    assert provider.requests == 2, "the dropped call was not made again"
    assert sleeps == [0.5]
    metrics = service.metrics.snapshot()
    assert (metrics["retries_total"], metrics["turn_errors_total"],
            metrics["active_turns"]) == (1, 0, 0)
    stored = agent.store.load(_internal_session(TENANT, "work"))
    assert [m["role"] for m in stored] == ["user", "assistant"]


@pytest.mark.parametrize("failure", ["dropping", "absent", "overloaded"])
def test_an_unavailable_provider_fails_the_turn_with_the_stable_error_and_recovers(
        product_server, mcp, providers, tmp_path, failure):
    """`dropping`: every connection to the provider is closed with no reply.
    `absent`: nothing listens where the provider should be. `overloaded`:
    the provider answers every call with 529. After three attempts (the call
    and its two retries) the turn fails with the contract's stable error, and
    nothing of the failure's own text reaches the client. When the provider
    is back, the same product serves the next turn."""
    if failure == "absent":
        provider, endpoint = None, _absent_provider_url()
    else:
        provider = providers("drop" if failure == "dropping" else "overloaded")
        endpoint = provider.url
    service, agent, sleeps = _service(tmp_path, mcp, endpoint)
    server = product_server(service)

    reply = _chat(server)

    assert reply.status == 502, reply
    assert reply.json() == {
        "error": {"code": "agent_failure", "message": "agent turn failed"},
        "request_id": REQUEST_ID}
    rendered = reply.body.decode()
    for diagnostic in DIAGNOSTICS:
        assert diagnostic not in rendered, f"{diagnostic!r} reached the client"
    assert reply.headers["X-Sigil-Retries"] == "2"
    assert sleeps == [0.5, 2.0]
    if provider is not None:
        assert provider.requests == 3
    metrics = service.metrics.snapshot()
    assert (metrics["retries_total"], metrics["turn_errors_total"],
            metrics["active_turns"]) == (2, 1, 0)

    # The provider comes back; the product serves again, with nothing left
    # held by the failed turn.
    if provider is None:
        provider = providers("answer")
        agent.endpoint = provider.url
    else:
        provider.behaviours = ["answer"]
    reply = _chat(server, "again")
    assert reply.status == 200, reply
    assert reply.json()["reply"] == ANSWER
    assert service.metrics.snapshot()["active_turns"] == 0


def test_every_test_here_goes_through_the_product_socket_to_the_real_compiler():
    """What makes this module evidence for the failure matrix: each test
    sends its request through serve_product (`product_server`) to an agent
    forging on the real compiler (`mcp`). tests/test_failure_injection.py
    relies on it for the categories it requires to be injected here."""
    tests = [value for name, value in globals().items()
             if name.startswith("test_") and callable(value)
             and value is not test_every_test_here_goes_through_the_product_socket_to_the_real_compiler]
    assert len(tests) >= 2
    for test in tests:
        parameters = set(test.__code__.co_varnames[:test.__code__.co_argcount])
        assert {"product_server", "mcp"} <= parameters, test.__name__
