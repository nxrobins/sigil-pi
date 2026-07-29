"""Dispatch tests for the broadened toolset (fetch, list_dir, grep_file,
append_file) — each a v14-authored forge with its own minimal grant.

Reuses the scripted-mock-LLM fixtures from test_pi_host. The LLM is scripted to
emit a tool_use, the host forges the tool in the session sandbox, and we assert
the byte-exact tool_result content + grant minimality (agent.grant_log)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import API_KEY, PI_ROOT
from test_pi_host import make_agent, msg, scripted_llm, text, tool_use  # noqa: F401


def _last_tool_result(scripted_llm):
    return scripted_llm.requests[-1]["messages"][-1]["content"][0]


# ── list_dir ──────────────────────────────────────────────────────────────


def test_list_dir(scripted_llm, tmp_path, mcp):
    agent = make_agent(scripted_llm, tmp_path, mcp)
    sb = agent.sandbox_for("s1")
    (sb / "zebra.txt").write_text("")
    (sb / "apple.txt").write_text("")
    (sb / "sub").mkdir()
    scripted_llm.script = [
        msg([tool_use("t", "list_dir", {"path": "."})]),
        msg([text("listed")]),
    ]
    assert agent.turn("s1", "what's here") == "listed"
    r = _last_tool_result(scripted_llm)
    assert r["content"] == "apple.txt\nsub\nzebra.txt"  # sorted
    assert "is_error" not in r
    [(name, grants)] = agent.grant_log
    assert name == "list_dir" and grants == {"fs": [str(sb)]}


# ── grep_file ─────────────────────────────────────────────────────────────


def test_grep_file(scripted_llm, tmp_path, mcp):
    agent = make_agent(scripted_llm, tmp_path, mcp)
    sb = agent.sandbox_for("s1")
    (sb / "log.txt").write_text("alpha line\nbeta line\nalpha again\ngamma\n")
    scripted_llm.script = [
        msg([tool_use("t", "grep_file", {"path": "log.txt", "pattern": "alpha"})]),
        msg([text("found")]),
    ]
    assert agent.turn("s1", "grep alpha") == "found"
    r = _last_tool_result(scripted_llm)
    assert r["content"] == "alpha line\nalpha again"
    assert "is_error" not in r
    [(name, grants)] = agent.grant_log
    assert name == "grep_file" and grants == {"fs": [str(sb)]}


def test_grep_file_no_match_is_empty_not_error(scripted_llm, tmp_path, mcp):
    agent = make_agent(scripted_llm, tmp_path, mcp)
    sb = agent.sandbox_for("s1")
    (sb / "log.txt").write_text("alpha\nbeta\n")
    scripted_llm.script = [
        msg([tool_use("t", "grep_file", {"path": "log.txt", "pattern": "zzz"})]),
        msg([text("none")]),
    ]
    assert agent.turn("s1", "grep zzz") == "none"
    r = _last_tool_result(scripted_llm)
    assert r["content"] == "" and "is_error" not in r


# ── append_file ───────────────────────────────────────────────────────────


def test_append_file_extends_existing(scripted_llm, tmp_path, mcp):
    agent = make_agent(scripted_llm, tmp_path, mcp)
    sb = agent.sandbox_for("s1")
    (sb / "notes.txt").write_text("first\n")
    scripted_llm.script = [
        msg([tool_use("t", "append_file", {"path": "notes.txt", "content": "second\n"})]),
        msg([text("appended")]),
    ]
    assert agent.turn("s1", "add a line") == "appended"
    r = _last_tool_result(scripted_llm)
    assert r["content"] == "ok" and "is_error" not in r
    assert (sb / "notes.txt").read_text() == "first\nsecond\n"
    [(name, grants)] = agent.grant_log
    assert name == "append_file" and grants == {"fs": [str(sb)], "fs_write": [str(sb)]}


def test_append_file_creates_new(scripted_llm, tmp_path, mcp):
    """Appending to a nonexistent file creates it (the -404-means-empty path)."""
    agent = make_agent(scripted_llm, tmp_path, mcp)
    sb = agent.sandbox_for("s1")
    scripted_llm.script = [
        msg([tool_use("t", "append_file", {"path": "fresh.txt", "content": "hello"})]),
        msg([text("created")]),
    ]
    assert agent.turn("s1", "make it") == "created"
    assert (sb / "fresh.txt").read_text() == "hello"


# ── fetch (net, deployment-allowlisted) ───────────────────────────────────


@pytest.fixture()
def http_target():
    """A tiny GET server standing in for a fetchable host."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"fetched body!"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{srv.server_address[1]}/thing"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield url
    srv.shutdown()
    srv.server_close()


def _agent_with_allowlist(scripted_llm, tmp_path, mcp, allowlist):
    from agent import PiAgent, SessionStore
    (tmp_path / "sessions").mkdir(exist_ok=True)
    (tmp_path / "sandboxes").mkdir(exist_ok=True)
    return PiAgent(scripted_llm.url, API_KEY, store=SessionStore(tmp_path / "sessions"),
                   sandbox_root=tmp_path / "sandboxes", mcp=mcp, model="claude-mock",
                   net_allowlist=allowlist)


def test_fetch_allowed_host(scripted_llm, tmp_path, mcp, http_target):
    agent = _agent_with_allowlist(scripted_llm, tmp_path, mcp, ["127.0.0.1"])
    scripted_llm.script = [
        msg([tool_use("t", "fetch", {"url": http_target})]),
        msg([text("got it")]),
    ]
    assert agent.turn("s1", "fetch it") == "got it"
    r = _last_tool_result(scripted_llm)
    assert r["content"] == "fetched body!" and "is_error" not in r
    [(name, grants)] = agent.grant_log
    assert name == "fetch" and grants == {"net": ["127.0.0.1"]}


def test_fetch_fail_closed_by_default(scripted_llm, tmp_path, mcp, http_target):
    """With no allowlist configured, the {NET_ALLOWLIST} grant is empty, so
    fetch is denied (-403) — the SSRF-safe default."""
    agent = _agent_with_allowlist(scripted_llm, tmp_path, mcp, [])
    scripted_llm.script = [
        msg([tool_use("t", "fetch", {"url": http_target})]),
        msg([text("blocked")]),
    ]
    assert agent.turn("s1", "fetch it") == "blocked"
    r = _last_tool_result(scripted_llm)
    assert r["is_error"] is True
    assert "403" in r["content"]
    [(name, grants)] = agent.grant_log
    assert name == "fetch" and grants == {"net": []}


def test_fetch_denied_host_not_in_allowlist(scripted_llm, tmp_path, mcp, http_target):
    """A host outside the allowlist is denied even though the target is up."""
    agent = _agent_with_allowlist(scripted_llm, tmp_path, mcp, ["example.com"])
    scripted_llm.script = [
        msg([tool_use("t", "fetch", {"url": http_target})]),  # 127.0.0.1, not example.com
        msg([text("denied")]),
    ]
    assert agent.turn("s1", "fetch it") == "denied"
    r = _last_tool_result(scripted_llm)
    assert r["is_error"] is True and "403" in r["content"]
