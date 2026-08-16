"""Observability for the running server (issue #19).

The host has one excellent after-the-fact operator tool — `--verify-audit` —
and until now nothing for WHILE it runs: `log_message` is a no-op, there is no
GET handler at all, and "is it working?" had no answer short of sending a
request and seeing whether one came back. The audit log's own argument applies
to operations too: a claim about the code is worth less than a record of an
execution.

Three additions, deliberately small:
- `GET /health` — liveness plus counters, behind the same auth chokepoint as
  everything else (an unauthenticated health route on a public bind is a
  fingerprinting oracle, and loopback needs no token anyway);
- a structured request log, one JSON line per request to stdout, with the
  session id HASHED — the same hashes-not-contents rule the audit log follows;
- request counters, exposed on /health rather than a second endpoint.
"""
import json
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from agent import hash_session, serve

TOKEN = "tok-SECRET-value"


def _agent():
    return SimpleNamespace(turn_with_usage=lambda s, m: (f"said:{m}", {}),
                           turn=lambda s, m: f"said:{m}",
                           usage_total={"input_tokens": 3, "output_tokens": 7})


def _get(server, path, token=None):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    req = urllib.request.Request(url)
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, None


def _post(server, path, payload, token=None):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, None


# ── GET /health ─────────────────────────────────────────────────────────


def test_health_reports_liveness_and_counters():
    server = serve(_agent(), port=0)
    try:
        _post(server, "/chat", {"session": "s", "message": "hi"})
        code, body = _get(server, "/health")
        assert code == 200 and body["ok"] is True
        # This GET's own response is counted when it is SENT — after the
        # snapshot — so the floor is the chat POST alone.
        assert body["requests"] >= 1
        assert body["usage"] == {"input_tokens": 3, "output_tokens": 7}
    finally:
        server.shutdown()


def test_health_counts_client_errors_and_server_errors():
    def boom(s, m):
        raise RuntimeError("operational failure")
    server = serve(SimpleNamespace(turn_with_usage=boom, usage_total={}), port=0)
    try:
        _post(server, "/chat", {"not": "valid"})            # 400
        _post(server, "/chat", {"session": "s", "message": "m"})  # 500
        code, body = _get(server, "/health")
        assert body["client_errors"] >= 1
        assert body["server_errors"] >= 1
    finally:
        server.shutdown()


def test_health_is_behind_the_same_credential():
    """An unauthenticated health route on a public bind tells the network a
    sigil-pi host lives here and how busy it is. One chokepoint, no
    exceptions — the same completeness-by-construction argument as do_POST,
    and loopback deployments have no token so lose nothing."""
    server = serve(_agent(), port=0, auth_token=TOKEN)
    try:
        code, _ = _get(server, "/health")
        assert code == 401
        code, body = _get(server, "/health", token=TOKEN)
        assert code == 200 and body["ok"] is True
    finally:
        server.shutdown()


def test_unauthorized_requests_are_counted_but_reveal_nothing():
    server = serve(_agent(), port=0, auth_token=TOKEN)
    try:
        _get(server, "/health")                              # 401
        _post(server, "/chat", {"session": "s", "message": "m"})  # 401
        code, body = _get(server, "/health", token=TOKEN)
        assert body["unauthorized"] >= 2
    finally:
        server.shutdown()


def test_get_on_an_unknown_path_is_404_when_authenticated():
    server = serve(_agent(), port=0)
    try:
        code, _ = _get(server, "/nope")
        assert code == 404
    finally:
        server.shutdown()


# ── the request log ─────────────────────────────────────────────────────


def test_request_log_lines_are_json_with_a_hashed_session(capfd):
    server = serve(_agent(), port=0, request_log=True)
    try:
        _post(server, "/chat", {"session": "alice", "message": "hi"})
    finally:
        server.shutdown()
    lines = [json.loads(ln) for ln in capfd.readouterr().out.splitlines()
             if ln.startswith("{")]
    chat = [e for e in lines if e.get("path") == "/chat"]
    assert chat, "no request-log line for the chat request"
    entry = chat[-1]
    assert entry["status"] == 200
    assert isinstance(entry["ms"], (int, float))
    assert entry["session"] == hash_session("alice")
    assert "alice" not in json.dumps(entry), \
        "the raw session id must never appear in a log line"


def test_request_log_is_off_by_default(capfd):
    """serve() is also used by the REPL's tests and by operators who already
    have an access log in front; logging must be a choice, not ambient. main()
    turns it on for PI_SERVE (PI_HTTP_LOG=0 opts out) — pinned below."""
    server = serve(_agent(), port=0)
    try:
        _post(server, "/chat", {"session": "alice", "message": "hi"})
    finally:
        server.shutdown()
    assert not [ln for ln in capfd.readouterr().out.splitlines()
                if ln.startswith("{")]


def test_main_enables_the_request_log_for_serve_mode():
    from conftest import PI_ROOT
    src = (PI_ROOT / "agent.py").read_text()
    body = src.split("def main():", 1)[1]
    assert "PI_HTTP_LOG" in body and "request_log=" in body, (
        "main() must wire request_log from PI_HTTP_LOG (default on for a "
        "served host) — a server that runs silently is unobservable")


# ── hash_session ────────────────────────────────────────────────────────


def test_hash_session_matches_the_kv_naming_rule():
    """The same rule the kv layer uses (sha256 of the id), truncated for log
    legibility — so an operator holding a session id can grep the log with
    `python3 -c "from agent import hash_session; print(hash_session('x'))"`
    and correlate with the kv file names, but the log alone reveals nothing."""
    import hashlib
    assert hash_session("abc") == hashlib.sha256(b"abc").hexdigest()[:12]


@pytest.mark.parametrize("sid", ["", "x", "a b c", "ünicode"])
def test_hash_session_never_echoes_its_input(sid):
    h = hash_session(sid)
    assert len(h) == 12 and (sid == "" or sid not in h)
