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
sys.path.insert(0, str(PI_ROOT))  # `from agent import ...` in fixtures + tests

import toolchain  # noqa: E402

# Resolved once, permissively: a machine with no SIGIL still COLLECTS the whole
# suite and runs every test that never forges. The toolchain used to be
# imported at module scope here, which meant `pytest --collect-only` needed a
# clone of a private repo — so the pure tests (scheduler timing, audit-chain
# math, compaction, the memory client against its double) were unreachable to
# anyone outside it, for no reason of their own.
_TC = toolchain.resolve(require=False)

# Kept under their historical names: six test modules import SIGIL_ROOT from
# here and pass it to compose_with_stdlib. It is the repo root compose wants,
# which in a release layout is the release dir rather than a checkout.
SIGIL_ROOT = _TC.stdlib_repo if _TC else Path(
    os.environ.get("SIGIL_ROOT", PI_ROOT.parent / "SIGIL")).resolve()
MCP_BIN = _TC.forge_bin if _TC else SIGIL_ROOT / "target" / "release" / "sigil-mcp"
SERVE_BIN = _TC.serve_bin if _TC else SIGIL_ROOT / "target" / "release" / "sigil-serve"
FIXED_EVALUATOR_BIN = PI_ROOT / "native/evaluator/target/release/sigil-fixed-evaluator"
TOOLS = PI_ROOT / "tools"
API_KEY = "sk-test-SECRET-abc123"


def needs_toolchain():
    """Skip the calling test when no toolchain is installed — UNLESS
    PI_REQUIRE_TOOLCHAIN is set, in which case its absence is a failure.

    That distinction is the whole point. A suite that silently skips its forge
    tests reports the same green as one that ran them, which is precisely the
    failure ci.yml's header describes at the job level ("a green one lies").
    The forge CI job sets the variable, so the real gate cannot degrade into a
    fast, empty, passing run without someone noticing."""
    if _TC is not None:
        return
    if toolchain.required():
        raise AssertionError(
            f"{toolchain.REQUIRE_ENV} is set but no SIGIL toolchain resolved. "
            f"This job is supposed to run the forge tests, so a skip here "
            f"would be a green check that verified nothing.")
    pytest.skip("no SIGIL toolchain (set SIGIL_ROOT or PI_FORGE_BIN)")


@pytest.fixture(scope="session")
def native_store_binary():
    """Build/test the native mechanism at its own lockfile, never a stale binary.

    The required source gate fails if Rust is unavailable. This fixture adds the
    native tests to the whole-tree pytest gate without replacing a legacy check.
    """
    import shutil
    if shutil.which("cargo") is None:
        if toolchain.required():
            pytest.fail("required native storage evidence needs cargo")
        pytest.skip("native storage component needs the pinned Rust toolchain")
    root = PI_ROOT / "native/store"
    commands = [
        ["cargo", "fmt", "--check"],
        ["cargo", "clippy", "--locked", "--all-targets", "--", "-D", "warnings"],
        ["cargo", "test", "--locked", "--quiet"],
        ["cargo", "build", "--locked", "--bin", "sigil-store"],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=root, capture_output=True, text=True,
                                timeout=240, env={**os.environ, "CARGO_BUILD_JOBS": "2"})
        assert result.returncode == 0, f"native storage gate failed: {command}\n{result.stdout}\n{result.stderr}"
    return root / "target/debug/sigil-store"


@pytest.fixture(scope="session")
def native_worker_binary():
    """Verify and build the fixed-source native execution mechanism independently."""
    import shutil
    if shutil.which("cargo") is None:
        if toolchain.required():
            pytest.fail("required native worker evidence needs cargo")
        pytest.skip("native worker component needs the pinned Rust toolchain")
    root = PI_ROOT / "native/worker"
    for command in (["cargo", "fmt", "--check"],
                    ["cargo", "clippy", "--locked", "--all-targets", "--", "-D", "warnings"],
                    ["cargo", "test", "--locked", "--quiet"],
                    ["cargo", "build", "--locked", "--bin", "sigil-worker"]):
        result = subprocess.run(command, cwd=root, capture_output=True, text=True,
                                timeout=240, env={**os.environ, "CARGO_BUILD_JOBS": "2"})
        assert result.returncode == 0, f"native worker gate failed: {command}\n{result.stdout}\n{result.stderr}"
    return root / "target/debug/sigil-worker"


@pytest.fixture(scope="session")
def native_service_binary():
    """Real native HTTP/request-binding host; no Python production dispatcher."""
    import shutil
    if shutil.which("cargo") is None:
        if toolchain.required():
            pytest.fail("required native service evidence needs cargo")
        pytest.skip("native service needs the pinned Rust toolchain")
    root = PI_ROOT / "native/service"
    for command in (["cargo", "fmt", "--check"],
                    ["cargo", "clippy", "--locked", "--all-targets", "--", "-D", "warnings"],
                    ["cargo", "test", "--locked", "--quiet"],
                    ["cargo", "build", "--locked", "--bins"]):
        result = subprocess.run(command, cwd=root, capture_output=True, text=True,
                                timeout=240, env={**os.environ, "CARGO_BUILD_JOBS": "2"})
        assert result.returncode == 0, f"native service gate failed: {command}\n{result.stdout}\n{result.stderr}"
    return root / "target/debug/sigil-application-host"


@pytest.fixture(scope="session")
def native_fixed_evaluator_binary():
    """Actual pinned compile-once runner, including full native supervisor gates."""
    import shutil
    needs_toolchain()
    if shutil.which("cargo") is None:
        if toolchain.required():
            pytest.fail("required fixed-evaluator evidence needs cargo")
        pytest.skip("fixed evaluator needs the pinned Rust toolchain")
    native_env = {**os.environ, "CARGO_BUILD_JOBS": "2"}
    if not native_env.get("Z3_SYS_Z3_HEADER"):
        if shutil.which("brew"):
            found = subprocess.run(["brew", "--prefix", "z3"], capture_output=True, text=True, timeout=30)
            prefix = Path(found.stdout.strip()) if found.returncode == 0 else None
            if prefix and (prefix / "include/z3.h").is_file():
                native_env["Z3_SYS_Z3_HEADER"] = str(prefix / "include/z3.h")
                native_env["LIBRARY_PATH"] = str(prefix / "lib") + (
                    ":" + native_env["LIBRARY_PATH"] if native_env.get("LIBRARY_PATH") else "")
        if not native_env.get("Z3_SYS_Z3_HEADER") and Path("/usr/include/z3.h").is_file():
            native_env["Z3_SYS_Z3_HEADER"] = "/usr/include/z3.h"
    if not Path(native_env.get("Z3_SYS_Z3_HEADER", "/missing-z3-header")).is_file():
        pytest.fail("fixed-evaluator source tests need Z3 headers; set Z3_SYS_Z3_HEADER and LIBRARY_PATH")
    root = PI_ROOT / "native/evaluator"
    for command in (["cargo", "fmt", "--check"],
                    ["cargo", "clippy", "--release", "--locked", "--all-targets", "--", "-D", "warnings"],
                    ["cargo", "test", "--release", "--locked", "--all-targets", "--quiet"],
                    ["cargo", "build", "--release", "--locked", "--bin", "sigil-fixed-evaluator"]):
        # This new source-build gate includes the real 4,096-call lifecycle test;
        # it changes no service, effect, operation or existing test deadline.
        result = subprocess.run(command, cwd=root, capture_output=True, text=True,
                                timeout=900, env=native_env)
        assert result.returncode == 0, f"fixed evaluator gate failed: {command}\n{result.stdout}\n{result.stderr}"
    return FIXED_EVALUATOR_BIN


@pytest.fixture(scope="session")
def native_release_service_binary(native_service_binary, native_fixed_evaluator_binary):
    """Production-shaped optimized host; retain all debug/static gates first.

    Whole-runtime SHA-256 checks stay enabled. Debug hashing of the 27 MiB pinned
    runtime is not a representative operating-envelope build.
    """
    assert native_fixed_evaluator_binary.is_file()
    root = PI_ROOT / "native/service"
    result = subprocess.run(["cargo", "build", "--release", "--locked", "--bins"],
        cwd=root, capture_output=True, text=True, timeout=240,
        env={**os.environ, "CARGO_BUILD_JOBS": "2"})
    assert result.returncode == 0, result.stdout + result.stderr
    return root / "target/release/sigil-application-host"


@pytest.fixture(scope="session")
def discovery_service_binary(native_release_service_binary, native_worker_binary, native_store_binary):
    """Current host with all baseline native/evaluator prerequisites retained."""
    assert native_worker_binary.is_file() and native_store_binary.is_file()
    return native_release_service_binary


@pytest.fixture(scope="session")
def discovery_store_binary(native_store_binary):
    return native_store_binary


@pytest.fixture(scope="session")
def legacy_http_service_binary():
    """Actual frozen v6 host: never substitute the newly upgraded executable."""
    from legacy_http_native_support import build
    build("store", "sigil-store")
    build("worker", "sigil-worker")
    return build("service", "sigil-application-host", release=True)


@pytest.fixture(scope="session")
def legacy_request_service_binary(native_fixed_evaluator_binary):
    """Actual frozen v7 host, with all pinned evaluator prerequisites retained."""
    from legacy_request_native_support import build
    assert native_fixed_evaluator_binary.is_file()
    build("store", "sigil-store")
    build("worker", "sigil-worker")
    return build("service", "sigil-application-host", release=True)


@pytest.fixture(scope="session")
def browser_runtime():
    """Missing browser evidence is a failure, not a silently skipped check."""
    import shutil
    node = shutil.which("node")
    assert node is not None, "browser checks require Node.js; see .node-version and docs/browser-interface.md"
    result = subprocess.run([node, str(PI_ROOT / "scripts/browser_runtime.mjs")],
                            cwd=PI_ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="session")
def browser_service_binary(native_release_service_binary, native_worker_binary, native_store_binary, browser_runtime):
    """Integrated host; inherited fixtures retain every native/evaluator gate."""
    assert native_worker_binary.is_file() and native_store_binary.is_file()
    assert browser_runtime["playwright"]
    return native_release_service_binary


@pytest.fixture(scope="session")
def legacy_native_store_binary():
    from legacy_native_support import build
    return build("store", "sigil-store")


@pytest.fixture(scope="session")
def legacy_native_worker_binary():
    from legacy_native_support import build
    return build("worker", "sigil-worker")


@pytest.fixture(scope="session")
def legacy_native_release_service_binary(legacy_native_store_binary, legacy_native_worker_binary):
    from legacy_native_support import build
    assert legacy_native_store_binary.is_file() and legacy_native_worker_binary.is_file()
    return build("service", "sigil-application-host", release=True)


@pytest.fixture(scope="session")
def native_claimed_worker_binary(native_service_binary):
    """The same native library's durable-claim embedding, not a Python dispatcher."""
    binary = native_service_binary.with_name("sigil-claimed-worker")
    assert binary.is_file()
    return binary


@pytest.fixture(scope="session")
def native_transaction_binary(native_service_binary):
    """Actual scoped read/produce/commit host, not a Python settlement policy."""
    binary = native_service_binary.with_name("sigil-transaction")
    assert binary.is_file()
    return binary

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
    needs_toolchain()
    assert MCP_BIN.exists(), f"build sigil-mcp first: {MCP_BIN}"
    SigilMCP, _ = toolchain.client()
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
    needs_toolchain()
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
        # Declared, as runtime_client.HOST_PROFILE declares it on every forge:
        # the v9 verifier refuses an undeclared host's tools (I013).
        "host_profile": "ephemeral",
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
