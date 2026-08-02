"""Shared fixtures for the sigil-pi test suite.

Layout expectations (the M2 contract):
- tools/frag_helpers.sigil   — pure helper fns (v14-authored): esc_json, find_text
- tools/chat_turn.sigil      — GENERATED, pre-composed serve tool (see make_chat_turn.py)
- sigil-serve boots a config with kv namespaces `cfg` (read) + `sess` (read/write)
  and a net grant for the mock LLM host.

kv backing files are `sha256(key).kv` under the namespace dir (see
sigil-runtime kv_key_path), so tests seed config directly.
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

PI_ROOT = Path(__file__).resolve().parent.parent
SIGIL_ROOT = Path(os.environ.get("SIGIL_ROOT", PI_ROOT.parent / "SIGIL")).resolve()
sys.path.insert(0, str(SIGIL_ROOT / "bench" / "src"))
sys.path.insert(0, str(PI_ROOT))  # `from agent import ...` in fixtures + tests

from sigil_bench.mcp_client import SigilMCP  # noqa: E402

MCP_BIN = SIGIL_ROOT / "target" / "release" / "sigil-mcp"
SERVE_BIN = SIGIL_ROOT / "target" / "release" / "sigil-serve"
TOOLS = PI_ROOT / "tools"
API_KEY = "sk-test-SECRET-abc123"

# ── kv seeding (mirror of kv_key_path: sha256 hex + .kv) ────────────────


def kv_seed(ns_dir: Path, key: str, value: str) -> None:
    name = hashlib.sha256(key.encode()).hexdigest() + ".kv"
    (ns_dir / name).write_bytes(value.encode())


def kv_dump(ns_dir: Path) -> bytes:
    """Concatenated bytes of every value in the namespace (for leak checks)."""
    return b"\n".join(p.read_bytes() for p in sorted(ns_dir.glob("*.kv")))


# ── escaped-slice decoding (the /chat response body contract) ───────────


def decode_escaped(body: bytes) -> str:
    """The tool returns the still-JSON-escaped text slice; wrap in quotes
    to decode. Raises if the slice is not a valid JSON string interior."""
    return json.loads('"' + body.decode() + '"')


# ── forge access (property tests) ───────────────────────────────────────


@pytest.fixture(scope="session")
def mcp():
    assert MCP_BIN.exists(), f"build sigil-mcp first: {MCP_BIN}"
    with SigilMCP.spawn(MCP_BIN) as m:
        m.initialize()
        yield m


def helpers_fragment() -> str:
    frag = TOOLS / "frag_helpers.sigil"
    assert frag.exists(), "tools/frag_helpers.sigil missing (v14 authors it — M2)"
    return frag.read_text()


def build_probe(main_body: str) -> str:
    """A pure probe tool: module header + the helper fragment + a probe main."""
    return "module tool;\n\n" + helpers_fragment() + "\n\n" + main_body


def forge_ok(mcp, source: str, input_text: str, fuel: int = 20_000_000) -> str:
    r = mcp.forge(source, input=input_text, fuel=fuel)
    assert r.get("status") == "ok", f"forge failed: {(r.get('diagnostics') or [{}])[0]}"
    return r["data"]["output_text"]


def forge_err(mcp, source: str, input_text: str, fuel: int = 20_000_000) -> str:
    """Forge expecting a tool error; returns the first diagnostic message."""
    r = mcp.forge(source, input=input_text, fuel=fuel)
    assert r.get("status") != "ok", f"expected failure, got ok: {r['data']['output_text']!r}"
    d = (r.get("diagnostics") or [{}])[0]
    return d.get("message") or ""


# ── mock LLM endpoint ───────────────────────────────────────────────────


class _MockLLM:
    """Anthropic-shaped mock. Cycles through `replies`; records every
    request's headers and parsed JSON body."""

    def __init__(self):
        self.replies = ["placeholder"]
        self.requests = []  # list of SimpleNamespace(headers=dict, body=dict, raw=bytes)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n)
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError:
                    body = None  # recorded as-is; tests assert validity
                outer.requests.append(SimpleNamespace(
                    headers={k.lower(): v for k, v in self.headers.items()},
                    body=body,
                    raw=raw,
                ))
                text = outer.replies[(len(outer.requests) - 1) % len(outer.replies)]
                payload = json.dumps({
                    "id": f"msg_{len(outer.requests)}",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-mock",
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }, separators=(",", ":"), ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._srv.server_address[1]}/v1/messages"
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


@pytest.fixture()
def mock_llm():
    m = _MockLLM()
    yield m
    m.close()


# ── the pi host under test (M3 dispatch / M7 loop) ──────────────────────
#
# ONE canonical scripted mock + agent factory. These used to live as
# near-identical copies in test_agent_dispatch.py and test_pi_host.py, with
# every other module importing fixtures cross-module — a pattern that both
# invites silent drift between the copies and trips F811 (the fixture import
# shadowed by the fixture parameter) everywhere. Fixtures belong here;
# helpers are imported explicitly (`from conftest import msg, ...`).


@pytest.fixture()
def scripted_llm():
    """An Anthropic-shaped mock endpoint whose responses are FULLY scripted
    (tests control tool_use ids and block layout). Records every request."""
    state = SimpleNamespace(script=[], requests=[], url=None)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            state.requests.append(json.loads(self.rfile.read(n)))
            doc = state.script[min(len(state.requests) - 1, len(state.script) - 1)]
            payload = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{srv.server_address[1]}/v1/messages"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield state
    srv.shutdown()
    srv.server_close()


def msg(content):
    return {"id": "m", "type": "message", "role": "assistant",
            "model": "claude-mock", "content": content,
            "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}}


def text(s):
    return {"type": "text", "text": s}


def tool_use(tu_id, name, tool_input):
    return {"type": "tool_use", "id": tu_id, "name": name, "input": tool_input}


def make_agent(scripted_llm, tmp_path, mcp, model="claude-mock"):
    """A pi host bound to a kv session dir + a sandbox root, both under tmp."""
    from agent import PiAgent, SessionStore
    kv = tmp_path / "sessions"
    sandbox_root = tmp_path / "sandboxes"
    kv.mkdir(); sandbox_root.mkdir()
    store = SessionStore(kv)
    agent = PiAgent(scripted_llm.url, API_KEY, store=store, sandbox_root=sandbox_root,
                    mcp=mcp, model=model)
    agent._kv_dir = kv
    agent._sandbox_root = sandbox_root
    return agent


@pytest.fixture()
def agent(scripted_llm, tmp_path, mcp):
    """A pi host driving a single fixed session; its sandbox is exposed as
    `_sandbox_path` for the dispatch tests."""
    a = make_agent(scripted_llm, tmp_path, mcp)
    a._session = "s1"
    a._sandbox_path = a.sandbox_for("s1")
    return a


# ── the serve stack under test ──────────────────────────────────────────

CFG_KEYS = {
    "pre": '{"model":"claude-mock","max_tokens":256,"messages":[',
    "post": "]}",
    "uo": '{"role":"user","content":"',
    "ao": '{"role":"assistant","content":"',
    "cl": '"}',
}


@pytest.fixture()
def chat(tmp_path, mock_llm):
    """Boot sigil-serve with chat_turn routed at POST /chat, kv cfg seeded
    for the mock endpoint. Yields a handle with .post(), .sess_dir, .mock."""
    assert SERVE_BIN.exists(), f"build sigil-serve first: {SERVE_BIN}"
    tool_src = TOOLS / "chat_turn.sigil"
    assert tool_src.exists(), "tools/chat_turn.sigil missing (generated — M2)"

    cfg_dir = tmp_path / "cfg"
    sess_dir = tmp_path / "sess"
    cfg_dir.mkdir()
    sess_dir.mkdir()
    kv_seed(cfg_dir, "url", mock_llm.url)
    # M5a: cfg:hdrs is a PLACEHOLDER template — the real key is a `secret`
    # grant the host injects; it never lands in kv or guest memory.
    kv_seed(cfg_dir, "hdrs", "\n".join([
        "x-api-key: {{secret:anthropic}}",
        "anthropic-version: 2023-06-01",
        "content-type: application/json",
    ]))
    for key, value in CFG_KEYS.items():
        kv_seed(cfg_dir, key, value)

    config = {
        "tools": {
            "chat_turn": {
                "source": str(tool_src),
                "fuel": 50_000_000,
                "grants": {
                    "net": ["127.0.0.1"],
                    "kv": [f"cfg={cfg_dir}", f"sess={sess_dir}"],
                    "kv_write": [f"sess={sess_dir}"],
                    "secret": [f"anthropic={API_KEY}"],
                },
            }
        },
        "http": {
            "bind": "127.0.0.1:0",
            "routes": [
                {"path": "/chat", "tool": "chat_turn", "content_type": "text/plain"}
            ],
        },
    }
    config_path = tmp_path / "service.json"
    config_path.write_text(json.dumps(config, indent=2))

    proc = subprocess.Popen(
        [str(SERVE_BIN), str(config_path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    lines = []

    def _reader():
        for line in proc.stdout:
            lines.append(line.rstrip())

    threading.Thread(target=_reader, daemon=True).start()

    addr = None
    deadline = time.time() + 30
    while time.time() < deadline:
        for line in lines:
            if "listening on " in line:
                addr = line.split("listening on ", 1)[1].strip()
                break
        if addr:
            break
        if proc.poll() is not None:
            pytest.fail("sigil-serve exited at boot:\n" + "\n".join(lines))
        time.sleep(0.05)
    assert addr, "sigil-serve never announced its address:\n" + "\n".join(lines)

    def post(path: str, body: str):
        req = urllib.request.Request(
            f"http://{addr}{path}", data=body.encode(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    handle = SimpleNamespace(post=post, sess_dir=sess_dir, cfg_dir=cfg_dir,
                             mock=mock_llm, proc=proc, log=lines)
    yield handle
    proc.kill()
    proc.wait()
