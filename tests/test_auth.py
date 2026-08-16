"""The HTTP perimeter (issue #16).

Every guest in this host is sandboxed, capability-checked and audited. The one
surface none of that covered was the front door: POST /chat took a caller-named
session id from anyone who could reach the port, spent the operator's api key,
and returned any session's history. These tests pin the credential, the
fail-closed bind rule that makes forgetting it hard, and the boundary the
single-token design does NOT cross.
"""
import ipaddress
import json
import re
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
from hypothesis import example, given, strategies as st

from agent import bind_is_loopback, check_auth, serve
from conftest import PI_ROOT

TOKEN = "tok-SECRET-value"


def _agent():
    return SimpleNamespace(turn_with_usage=lambda s, m: (f"said:{m}", {}),
                           turn=lambda s, m: f"said:{m}")


def _post(server, path, payload, token=None, raw_header=None):
    """Returns (status, body-dict-or-None). A 401 arrives as HTTPError, which
    is a response rather than a failure here, so it is unwrapped not raised."""
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    if raw_header is not None:
        req.add_header("Authorization", raw_header)
    elif token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body or b"{}")
        except json.JSONDecodeError:
            return e.code, None


# ── the credential ──────────────────────────────────────────────────────


def test_a_configured_token_is_required():
    server = serve(_agent(), port=0, auth_token=TOKEN)
    try:
        code, _ = _post(server, "/chat", {"session": "s", "message": "hi"})
        assert code == 401, "no credential must not reach the agent"
        code, _ = _post(server, "/chat", {"session": "s", "message": "hi"},
                        token="wrong")
        assert code == 401
        code, body = _post(server, "/chat", {"session": "s", "message": "hi"},
                           token=TOKEN)
        assert code == 200 and body["reply"] == "said:hi"
    finally:
        server.shutdown()


def test_no_token_configured_keeps_the_loopback_default_open():
    """A local REPL is not exposure. Demanding a credential for it would only
    teach people to set a dummy one, which is worse than none."""
    server = serve(_agent(), port=0)
    try:
        code, body = _post(server, "/chat", {"session": "s", "message": "hi"})
        assert code == 200 and body["reply"] == "said:hi"
    finally:
        server.shutdown()


def test_the_schedule_surface_is_behind_the_same_credential():
    """The schedule routes create durable entries that fire ordinary turns.
    An unauthenticated /schedule is a way to spend the api key on a cadence,
    and /schedule/list enumerates every entry's session and message."""
    sched = SimpleNamespace(store=SimpleNamespace(
        entries=lambda: [], put=lambda **kw: None, remove=lambda n: False))
    agent = _agent()
    agent.scheduler = sched
    server = serve(agent, port=0, auth_token=TOKEN)
    try:
        for path in ("/schedule", "/schedule/list", "/schedule/remove"):
            code, _ = _post(server, path, {})
            assert code == 401, f"{path} answered without a credential"
        code, _ = _post(server, "/schedule/list", {}, token=TOKEN)
        assert code == 200
    finally:
        server.shutdown()


def test_an_unknown_route_is_also_behind_the_credential():
    """404 vs 401 on an unknown path is a probing oracle: it tells an
    unauthenticated caller which routes exist. Auth runs BEFORE routing, so
    the answer is the same either way."""
    server = serve(_agent(), port=0, auth_token=TOKEN)
    try:
        code, _ = _post(server, "/nope", {})
        assert code == 401, "route existence leaked to an unauthenticated caller"
    finally:
        server.shutdown()


@pytest.mark.parametrize("header", [
    "", "Bearer", "Bearer ", f"Basic {TOKEN}", TOKEN,
    f"bearer {TOKEN}x", f"Bearer {TOKEN} extra",
])
def test_malformed_authorization_headers_are_rejected(header):
    assert check_auth(header, TOKEN) is False


def test_the_scheme_is_case_insensitive_but_the_token_is_not():
    assert check_auth(f"bearer {TOKEN}", TOKEN) is True
    assert check_auth(f"BEARER {TOKEN}", TOKEN) is True
    assert check_auth(f"Bearer {TOKEN.upper()}", TOKEN) is False


def test_no_configured_token_means_the_check_passes():
    """`None` is 'auth disabled', which only serve() may allow and only on a
    loopback bind. If this returned False the default deployment would 401
    itself; if the bind rule ever regressed, this is the function that would
    quietly let the network in — hence the pairing below."""
    assert check_auth(None, None) is True
    assert check_auth("anything", "") is True


# ── the fail-closed bind rule ───────────────────────────────────────────


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_loopback_spellings(host):
    assert bind_is_loopback(host) is True


@pytest.mark.parametrize("host", [
    "0.0.0.0", "", "::", "192.168.1.10", "10.0.0.5", "example.com", "garbage",
])
def test_anything_not_provably_loopback_counts_as_exposure(host):
    """0.0.0.0 and "" include loopback but are not it, and a name we would
    have to resolve is not an answer to trust here. Fail closed: the cost of
    being wrong is an unauthenticated agent on the network."""
    assert bind_is_loopback(host) is False


def test_a_public_bind_without_a_token_is_refused():
    """THE rule. Binding off loopback is the moment this stops being a local
    tool, and it is exactly when a forgotten credential stops being harmless."""
    with pytest.raises(ValueError, match="PI_AUTH_TOKEN"):
        serve(_agent(), host="0.0.0.0", port=0)


@pytest.mark.parametrize("empty", [None, ""])
def test_an_empty_token_is_no_token_even_on_a_public_bind(empty):
    """serve()'s bind check and check_auth() must agree on what 'disabled'
    means. check_auth treats ANY falsy token as disabled; a bind check that
    only catches None would let serve(host='0.0.0.0', auth_token='') bind
    publicly with auth off — the exact state the rule exists to prevent, via
    the same ''-vs-unset confusion secrets_from_env already normalises."""
    with pytest.raises(ValueError, match="PI_AUTH_TOKEN"):
        serve(_agent(), host="0.0.0.0", port=0, auth_token=empty)


def test_a_public_bind_with_a_token_is_allowed():
    server = serve(_agent(), host="0.0.0.0", port=0, auth_token=TOKEN)
    try:
        code, _ = _post(server, "/chat", {"session": "s", "message": "hi"})
        assert code == 401
        code, body = _post(server, "/chat", {"session": "s", "message": "hi"},
                           token=TOKEN)
        assert code == 200
    finally:
        server.shutdown()


# ── properties ──────────────────────────────────────────────────────────

# Printable-ASCII sans space: a space would split into scheme+token at a
# different point, which is the *malformed-header* case, tested separately.
_tokens = st.text(st.characters(min_codepoint=33, max_codepoint=126),
                  min_size=1, max_size=64)


@given(expected=_tokens, presented=_tokens)
@example(expected="secret", presented="secret2")   # prefix
@example(expected="secret2", presented="secret")   # truncation
def test_only_the_exact_token_authenticates(expected, presented):
    """The whole contract of check_auth in one property: a well-formed Bearer
    header authenticates iff the token is byte-for-byte the configured one.
    Catches whole classes at once — prefix acceptance, truncation, case
    folding of the token (only the SCHEME is case-insensitive)."""
    result = check_auth(f"Bearer {presented}", expected)
    assert result is (presented == expected)


@given(host=st.ip_addresses())
def test_bind_is_loopback_agrees_with_the_ip_stack(host):
    """For every literal IP address — v4 and v6 — the answer is exactly
    ipaddress.is_loopback: the whole 127/8 block and ::1 are loopback,
    nothing else is. The conservative fallback is only for non-literals."""
    assert bind_is_loopback(str(host)) is host.is_loopback


@given(host=st.ip_addresses(v=4))
def test_no_public_v4_address_binds_without_a_token(host):
    """The rule end-to-end, quantified: serve() either refuses (no token,
    non-loopback) or the address was loopback. No third outcome."""
    if ipaddress.ip_address(str(host)).is_loopback:
        return  # the open-by-default case, pinned in the tests above
    with pytest.raises(ValueError, match="PI_AUTH_TOKEN"):
        serve(_agent(), host=str(host), port=0)


# ── structural guards ───────────────────────────────────────────────────


def test_auth_is_the_first_thing_do_post_does():
    """Completeness by construction, not by memory. Every route is covered
    because the check runs before dispatch — so a route added later inherits
    it. The same argument _forge makes for the audit log: a second path is
    what turns a property into a promise."""
    src = (PI_ROOT / "agent.py").read_text()
    body = re.search(r"\n        def do_POST\(self\):\n(.*?)\n        def ",
                     src, re.S)
    assert body, "do_POST not found — did the handler move?"
    statements = [ln.strip() for ln in body.group(1).splitlines()
                  if ln.strip() and not ln.strip().startswith("#")]
    assert statements[0].startswith("if not check_auth("), (
        f"the auth check must be the FIRST statement in do_POST so routing "
        f"cannot precede it; found {statements[0]!r}")


def test_the_token_is_compared_in_constant_time():
    """A token checked with `==` leaks its prefix through timing, and this one
    guards an endpoint that spends the api key."""
    src = (PI_ROOT / "agent.py").read_text()
    body = re.search(r"\ndef check_auth\(.*?\n(.*?)\n\ndef ", src, re.S)
    assert body and "hmac.compare_digest(" in body.group(1), \
        "check_auth must use hmac.compare_digest, not =="
    assert "==" not in body.group(1).split("compare_digest")[0].split("scheme")[-1]


def test_the_serve_docstring_admits_the_single_principal_boundary():
    """One token is one principal: authentication, not authorization. Every
    holder can name any session id. That is a deliberate scope line, and this
    repo's habit is to write those down rather than let a reader assume the
    stronger claim — the same thing docs/security-guarantee.md does for the
    interprocedural taint gap."""
    assert "HONEST BOUNDARY" in serve.__doc__
    assert "principal" in serve.__doc__.lower()
