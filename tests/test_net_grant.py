"""The `net` grant must survive a redirect — S1 of the 2026-07-31 audit.

A capability grant that is checked only on the FIRST hop is not a capability
grant. The audit found (against the then-current toolchain) that it was:

  - `fetch` under `net: [127.0.0.1]` followed a 302 to an off-allowlist host and
    returned its body. Since the MODEL chooses the URL, one open redirect on any
    allowlisted host — common on CDNs, link shorteners, OAuth endpoints — was a
    full SSRF bypass of the "fail-closed" guarantee the README advertises.
  - `http::post_secret` was worse: on 301/302/303 the host-injected `x-api-key`
    was REPLAYED to the redirect target, exfiltrating the api key to a host that
    was never granted.

The runtime has since been hardened (redirect targets are re-validated against
the grants, and a redirect on a request carrying caller-supplied headers is
refused outright rather than choosing between leaking the header and silently
dropping it). Nothing in sigil-pi pinned that, so a toolchain regression would
have re-opened both holes silently. These tests are that pin.

They forge the tools DIRECTLY rather than driving the agent loop: the property
under test belongs to the tool + its grant, and going through the loop would
couple them to `parse_reply` for no benefit.
"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import PI_ROOT

CANARY = "sk-ant-CANARY-must-never-leave-the-granted-host"
HDR_TEMPLATE = "\n".join([
    "x-api-key: {{secret:anthropic}}",
    "anthropic-version: 2023-06-01",
    "content-type: application/json",
])


def _serve(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


@pytest.fixture()
def victim():
    """An off-allowlist host. Reached by NAME `localhost` — it resolves to the
    same interface as 127.0.0.1, so only the grant distinguishes them. Records
    every request it receives; reaching it at all is the failure."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def _handle(self):
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            seen.append({k.lower(): v for k, v in self.headers.items()})
            body = b'{"content":[{"type":"text","text":"STOLEN"}]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _handle

        def log_message(self, *a):
            pass

    srv, port = _serve(Handler)
    yield type("V", (), {"seen": seen, "url": f"http://localhost:{port}/steal"})
    srv.shutdown()
    srv.server_close()


def _redirector(status, location):
    class Handler(BaseHTTPRequestHandler):
        def _handle(self):
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            self.send_response(status)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST = _handle

        def log_message(self, *a):
            pass

    return _serve(Handler)


def _forge_fetch(mcp, url, allowlist):
    return mcp.forge((PI_ROOT / "tools" / "fetch.sigil").read_text(),
                     input=url, fuel=20_000_000, grants={"net": allowlist})


def _forge_llm_call(mcp, url, allowlist):
    from conftest import SIGIL_ROOT
    from sigil_compose import compose_with_stdlib
    src = compose_with_stdlib(
        (PI_ROOT / "tools" / "agent_turn.sigil").read_text(), ["http"], SIGIL_ROOT).text
    return mcp.forge(src, input=f"{url}|{HDR_TEMPLATE}|{{}}", fuel=20_000_000,
                     grants={"net": allowlist, "secret": [f"anthropic={CANARY}"]})


# ── anti-vacuity: prove the detector can actually detect ────────────────


def test_the_leak_detector_is_live(mcp, victim):
    """A security test that CANNOT observe the thing it forbids is worse than
    no test — it reports safety forever. Every assertion below depends on the
    victim being reachable by the name `localhost` and on the canary being
    visible in its request headers. So prove both, by GRANTING the victim's
    host and watching the leak happen on purpose.

    If this test ever fails, the redirect tests in this file are vacuous and
    their passing means nothing.
    """
    r = _forge_fetch(mcp, victim.url, ["localhost"])
    assert r.get("status") == "ok", (r.get("diagnostics") or [{}])[0]
    assert victim.seen, "victim unreachable when GRANTED — the redirect tests are vacuous"
    assert "STOLEN" in r["data"]["output_text"]

    before = len(victim.seen)
    r2 = _forge_llm_call(mcp, victim.url, ["localhost"])
    assert r2.get("status") == "ok", (r2.get("diagnostics") or [{}])[0]
    delivered = [h for h in victim.seen[before:] if h.get("x-api-key") == CANARY]
    assert delivered, "canary not observable when GRANTED — the key tests are vacuous"


# ── fetch: the allowlist must survive a redirect ────────────────────────


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_fetch_cannot_escape_the_allowlist_via_redirect(mcp, victim, status):
    """The model picks the URL, so an allowlisted open redirect must not become
    a door to every host the process can route to."""
    srv, port = _redirector(status, victim.url)
    try:
        r = _forge_fetch(mcp, f"http://127.0.0.1:{port}/", ["127.0.0.1"])
    finally:
        srv.shutdown()
        srv.server_close()

    assert victim.seen == [], (
        f"SSRF: a {status} from an allowlisted host reached an off-allowlist host")
    body = r["data"]["output_text"] if r.get("status") == "ok" else ""
    assert "STOLEN" not in body, "off-allowlist body was returned to the guest"


def test_fetch_still_works_without_a_redirect(mcp):
    """Guard against 'fixing' the bypass by denying everything: the ordinary
    allowlisted GET must still succeed."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"fetched body!"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv, port = _serve(Handler)
    try:
        r = _forge_fetch(mcp, f"http://127.0.0.1:{port}/thing", ["127.0.0.1"])
    finally:
        srv.shutdown()
        srv.server_close()
    assert r.get("status") == "ok", (r.get("diagnostics") or [{}])[0]
    assert r["data"]["output_text"] == "fetched body!"


# ── post_secret: the api key must never follow a redirect ───────────────


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_api_key_never_follows_a_redirect(mcp, victim, status):
    """THE one that matters. 301/302/303 downgrade POST->GET, and a naive client
    replays the headers to the new host — which is how the host-injected
    x-api-key leaked. The key must never reach a host outside the grant, by any
    status code."""
    srv, port = _redirector(status, victim.url)
    try:
        r = _forge_llm_call(mcp, f"http://127.0.0.1:{port}/v1/messages", ["127.0.0.1"])
    finally:
        srv.shutdown()
        srv.server_close()

    leaked = [h for h in victim.seen if h.get("x-api-key") == CANARY]
    assert not leaked, f"API KEY EXFILTRATED to an off-allowlist host via {status}"
    assert victim.seen == [], f"off-allowlist host was contacted at all via {status}"
    out = r["data"]["output_text"] if r.get("status") == "ok" else ""
    assert CANARY not in out, "the key came back into guest-visible output"


def test_authenticated_call_still_delivers_the_key_without_a_redirect(mcp):
    """The other half: substitution must still work on the granted host, or the
    test above would pass for the wrong reason."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            seen.append({k.lower(): v for k, v in self.headers.items()})
            body = b'{"content":[{"type":"text","text":"ok"}]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv, port = _serve(Handler)
    try:
        r = _forge_llm_call(mcp, f"http://127.0.0.1:{port}/v1/messages", ["127.0.0.1"])
    finally:
        srv.shutdown()
        srv.server_close()

    assert r.get("status") == "ok", (r.get("diagnostics") or [{}])[0]
    assert seen and seen[0]["x-api-key"] == CANARY, \
        "the host did not substitute the secret on the granted host"
    # ...and the guest still never sees it
    assert CANARY not in r["data"]["output_text"]
