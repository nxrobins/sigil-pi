"""Shared fixtures for the sigil-pi test suite.

Layout expectations (the M2 contract):
- tools/frag_helpers.sigil   — pure helper fns (v14-authored): esc_json, find_text
- tools/chat_turn.sigil      — GENERATED, pre-composed serve tool (see make_chat_turn.py)
- sigil-serve boots a config with kv namespaces `cfg` (read) + `sess` (read/write)
  and a net grant for the mock LLM host.

kv backing files are `sha256(key).kv` under the namespace dir (see
sigil-runtime kv_key_path), so tests seed config directly.
"""
import collections
import hashlib
import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

# A failed test is named, with its reason, at the moment it fails. pytest's own report
# comes at the end, and a run stopped at the job's time limit never reaches it
# (tests/failed_now.py; tests/test_guards.py checks every run has it loaded).
pytest_plugins = ("failed_now",)

PI_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PI_ROOT))  # `from agent import ...` in fixtures + tests

# Tests boot release bundles, and a bundle's launcher runs the first `python3`
# on PATH. ci.sh puts the gate's interpreter first; a run outside ci.sh used
# the host's, and where that is outside the supported range every bundle the
# suite booted refused to start (audit 2026-11-10, F-24). The interpreter
# running the suite goes first, for this process and every one it starts
# (tests/test_suite_interpreter_on_path.py).
_SUITE_INTERPRETER_DIR = str(Path(sys.executable).parent)
if os.environ.get("PATH", "").split(os.pathsep)[0] != _SUITE_INTERPRETER_DIR:
    os.environ["PATH"] = os.pathsep.join(
        [_SUITE_INTERPRETER_DIR, *filter(None, [os.environ.get("PATH")])])

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
    """A whole reply, as the Messages API ends one: `tool_use` when the model
    is calling a tool, `end_turn` otherwise. The host reads this field before
    it acts on a reply (audit F-10), so the mock says what the provider would."""
    calls_a_tool = any(block.get("type") == "tool_use" for block in content)
    return {"id": "m", "type": "message", "role": "assistant",
            "model": "claude-mock", "content": content,
            "stop_reason": "tool_use" if calls_a_tool else "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1}}


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


# ── the product server over real sockets (plain and TLS) ────────────────
#
# The product tests drive ProductService.dispatch() directly, which left the
# transport itself (reading a POST body, the DELETE handler, the Content-Length
# check, the TLS listener and its context) executed by no test at all (audit
# 2026-11-10, F-17). This harness starts the real serve_product() on
# 127.0.0.1 at an ephemeral port, plain or TLS, and talks to it over real
# sockets in raw request bytes, so a test can send exactly what a client
# would: a malformed request, half a body, or a connection that never speaks.

TLS_HOSTNAME = "localhost"

# A server certificate for localhost and 127.0.0.1, spelled out here rather
# than taken from the platform's openssl.cnf, so that OpenSSL and LibreSSL
# produce the same certificate and it verifies under the strict X.509 checks
# that newer Pythons enable by default.
_OPENSSL_CONFIG = """\
[req]
distinguished_name = subject
x509_extensions = leaf
prompt = no

[subject]
CN = localhost

[leaf]
subjectAltName = DNS:localhost, IP:127.0.0.1
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
"""


def make_tls_material(directory):
    """Generate a throwaway certificate and key with the openssl on PATH.

    A missing or failing openssl FAILS the calling test; it never skips it.
    A skip would let a job report the TLS listener as tested while nothing
    ran it, which is how that path went unseen (F-17)."""
    directory = Path(directory)
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.fail(
            "openssl is not on PATH. The TLS tests generate their throwaway "
            "certificate with it, and they fail rather than skip so that no job "
            "can report the TLS listener as tested without running it.",
            pytrace=False)
    directory.mkdir(parents=True, exist_ok=True)
    config = directory / "openssl.cnf"
    config.write_text(_OPENSSL_CONFIG)
    cert, key = directory / "cert.pem", directory / "key.pem"
    try:
        result = subprocess.run(
            [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-config", str(config), "-keyout", str(key), "-out", str(cert)],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        pytest.fail(f"openssl ({openssl}) did not run: {error}", pytrace=False)
    if result.returncode != 0 or not cert.is_file() or not key.is_file():
        pytest.fail(
            f"openssl ({openssl}) could not make the throwaway certificate "
            f"(exit {result.returncode}):\n{result.stderr.strip()[-2000:]}",
            pytrace=False)
    return SimpleNamespace(cert=cert, key=key, hostname=TLS_HOSTNAME)


@pytest.fixture(scope="session")
def tls_material(tmp_path_factory):
    """One throwaway certificate for the whole session (make_tls_material)."""
    return make_tls_material(tmp_path_factory.mktemp("tls"))


def http_request_bytes(method, path, *, headers=None, body=b""):
    """The exact bytes of one HTTP/1.1 request. Host and `Connection: close`
    are added unless given; Content-Length is added for a non-empty body
    unless the caller set one of its own (an invalid one, say)."""
    headers = dict(headers or {})
    present = {name.lower() for name in headers}
    if "host" not in present:
        headers = {"Host": TLS_HOSTNAME, **headers}
    if "connection" not in present:
        headers["Connection"] = "close"
    if body and "content-length" not in present:
        headers["Content-Length"] = str(len(body))
    head = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
    return f"{method} {path} HTTP/1.1\r\n{head}\r\n".encode("latin-1") + body


class HTTPReply:
    """One parsed response. `headers` is case-insensitive."""

    def __init__(self, status, reason, headers, body):
        self.status = status
        self.reason = reason
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body)

    def __repr__(self):
        return f"HTTPReply({self.status} {self.reason}, {self.body[:160]!r})"


def read_reply(sock, method="GET"):
    """Read and parse one HTTP response from a connected socket."""
    response = http.client.HTTPResponse(sock, method=method)
    try:
        response.begin()
        body = response.read()
    finally:
        response.close()
    return HTTPReply(response.status, response.reason, response.headers, body)


class ProductServerHandle:
    """A running serve_product() and a raw-socket client for it."""

    def __init__(self, server, tls_material=None):
        self.server = server
        self.host, self.port = server.server_address[:2]
        self.tls_material = tls_material
        self.client_context = (
            None if tls_material is None
            else ssl.create_default_context(cafile=str(tls_material.cert)))
        self._connections = []
        self._lock = threading.Lock()
        self._closed = False

    @property
    def tls(self):
        return self.tls_material is not None

    def connect(self, *, tls=None, timeout=5.0):
        """A connected client socket, which close() closes before it stops the
        server. It speaks TLS (verifying the throwaway certificate and its
        hostname) when the server does, unless tls=False: then it is plain
        TCP, for a client that never handshakes or talks plaintext to TLS."""
        use_tls = self.tls if tls is None else tls
        if use_tls and not self.tls:
            raise ValueError("this server was started without TLS")
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self._track(sock)
        if use_tls:
            sock = self.client_context.wrap_socket(
                sock, server_hostname=self.tls_material.hostname)
            self._track(sock)
        return sock

    def exchange(self, raw, *, method="GET", tls=None, timeout=5.0):
        """Send raw request bytes on a new connection and parse the reply."""
        sock = self.connect(tls=tls, timeout=timeout)
        try:
            sock.sendall(raw)
            return read_reply(sock, method)
        finally:
            sock.close()

    def request(self, method, path, *, headers=None, body=b"", timeout=5.0):
        """One well-formed request on a new connection (http_request_bytes)."""
        return self.exchange(
            http_request_bytes(method, path, headers=headers, body=body),
            method=method, timeout=timeout)

    def close(self, timeout=10.0):
        """Close every connection opened through this handle, then stop the
        server. Connections go first: a client that never speaks must not hold
        up shutdown, and it would whenever the server handled it on its
        serving thread, as the TLS handshake inside accept() did until S1
        (audit F-01). A server that still does not stop in time fails the
        test, not hangs it."""
        if self._closed:
            return
        self._closed = True
        with self._lock:
            connections, self._connections = self._connections, []
        for sock in connections:
            try:
                sock.close()
            except OSError:
                pass
        stopper = threading.Thread(target=self.server.shutdown, daemon=True,
                                   name=f"stop-product-server-{self.port}")
        stopper.start()
        stopper.join(timeout)
        if stopper.is_alive():
            raise AssertionError(
                f"serve_product() on port {self.port} did not stop within "
                f"{timeout}s of its client connections closing; something still "
                f"holds its serving thread")
        self.server.server_close()

    def _track(self, sock):
        with self._lock:
            self._connections.append(sock)


@pytest.fixture()
def product_server(request):
    """Start the real serve_product() on 127.0.0.1 at an ephemeral port:

        server = product_server(service)             # loopback plaintext
        server = product_server(service, tls=True)   # the throwaway certificate
        reply = server.request("POST", "/v1/chat", headers=..., body=...)

    With tls=True the server presents the session's throwaway certificate
    through tls_context(), or uses `ssl_context` when one is given (it must
    present that same certificate). A TLS start needs openssl and FAILS
    without it. Every server started here is stopped in teardown, within a
    bound (ProductServerHandle.close)."""
    from product_service import serve_product, tls_context

    handles = []

    def start(service, *, tls=False, ssl_context=None, socket_timeout_s=5):
        if ssl_context is not None and not tls:
            raise ValueError("an ssl_context needs tls=True, so the client speaks TLS")
        material = request.getfixturevalue("tls_material") if tls else None
        if tls and ssl_context is None:
            ssl_context = tls_context(str(material.cert), str(material.key))
        server = serve_product(service, host="127.0.0.1", port=0,
                               ssl_context=ssl_context,
                               socket_timeout_s=socket_timeout_s)
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


# ── the product scheduler on its real thread ────────────────────────────
#
# The product tests call ProductScheduler.tick() directly, which left the
# thread the product actually runs, ProductScheduler.start() and stop() and
# the loop between them, executed by no test at all (audit 2026-11-10, F-17).
# This harness builds a real scheduler over a real schedule store and
# session-lock directory and starts its real thread, with the two things a
# test must control injected: the wall clock the scheduler reads
# (ManualClock) and the agent its turns run on (ScriptedTurnAgent). A test
# makes an entry due by moving the clock, keeps a turn in flight by holding
# it, and waits on what the thread has done instead of sleeping. Every wait
# is bounded, and every scheduler built here is stopped and joined in
# teardown.

SCHEDULER_TENANT = "scheduler-tenant"
SCHEDULER_PRINCIPAL = "scheduler-planner"
# Only this token's digest is configured, and nothing authenticates with it:
# the scheduler finds its principal by tenant and id (AuthRegistry.policy).
_SCHEDULER_TOKEN = "scheduler-harness-token-with-enough-entropy-for-a-test-fixture"


class ManualClock:
    """The wall clock a ProductScheduler reads (its `clock=`), moved only by
    the test.

    The scheduler asks time() what is due and stamps each claim and run with
    it, and advance() is the only thing that changes it. The thread's pause
    between ticks stays real (it waits interval_s on its stop event), so the
    test decides what is due and the thread decides when it next looks."""

    def __init__(self, now=1_000_000.0):
        self._lock = threading.Lock()
        self._now = float(now)

    def time(self):
        with self._lock:
            return self._now

    def advance(self, seconds):
        if seconds < 0:
            raise ValueError("a ManualClock only moves forward")
        with self._lock:
            self._now += seconds
            return self._now


class ScriptedTurnAgent:
    """A PiAgent stand-in for scheduler tests: it records every turn it is
    asked to run and answers from a script.

    Each turn is appended to `turns` as it starts, with its session, message,
    allowed_tools, deadline_monotonic, any other keyword (`context`), the
    thread it runs on, and when it started and finished. Its outcome is the
    next script() item, a reply string or an exception (class or instance) to
    raise; with the script empty it replies `reply`. After hold(), a turn
    waits for release() before it answers, which is how a test keeps one in
    flight, and like PiAgent's it raises TimeoutError if its deadline passes
    first. No turn is held longer than hold_limit_s."""

    manifest = {"read_file": {}, "write_file": {}}
    memory = None
    _mcp = object()

    def __init__(self, reply="scheduled reply", *, usage=None, hold_limit_s=30.0):
        self.reply = reply
        self.usage = dict(usage or {"input_tokens": 2, "output_tokens": 1})
        self.hold_limit_s = hold_limit_s
        self.turns = []
        self._script = collections.deque()
        self._released = threading.Event()
        self._released.set()
        self._changed = threading.Condition()

    def script(self, *outcomes):
        """Queue the outcomes of the next turns, in order."""
        with self._changed:
            self._script.extend(outcomes)

    def hold(self):
        """Make every turn that starts from now on wait for release()."""
        self._released.clear()

    def release(self):
        """Let every held turn answer."""
        self._released.set()

    def turn_with_usage(self, session, message, *, allowed_tools=None,
                        deadline_monotonic=None, **context):
        turn = SimpleNamespace(
            session=session, message=message,
            allowed_tools=frozenset(allowed_tools or ()),
            deadline_monotonic=deadline_monotonic, context=context,
            thread=threading.current_thread(), started=time.monotonic(),
            finished=None)
        with self._changed:
            outcome = self._script.popleft() if self._script else self.reply
            self.turns.append(turn)
            self._changed.notify_all()
        try:
            self._wait_while_held(deadline_monotonic)
            if isinstance(outcome, BaseException) or (
                    isinstance(outcome, type) and issubclass(outcome, BaseException)):
                raise outcome
            return outcome, dict(self.usage)
        finally:
            with self._changed:
                turn.finished = time.monotonic()
                self._changed.notify_all()

    def _wait_while_held(self, deadline_monotonic):
        if self._released.is_set():
            return
        if deadline_monotonic is not None:
            remaining = deadline_monotonic - time.monotonic()
            if remaining < self.hold_limit_s:
                if not self._released.wait(max(0.0, remaining)):
                    raise TimeoutError("turn deadline exceeded while the turn was held")
                return
        if not self._released.wait(self.hold_limit_s):
            raise AssertionError(
                f"a scripted turn was held for {self.hold_limit_s}s and never released")

    def turn_telemetry(self):
        return {"forge_queue_wait_ms": 0.0, "tool_calls": 0, "retries": 0}

    def wait_for_turns(self, count, timeout=5.0):
        """Wait until at least `count` turns have started and return the first
        `count`. After `timeout` it fails the test, saying how many started."""
        deadline = time.monotonic() + timeout
        with self._changed:
            while len(self.turns) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"{len(self.turns)} scheduled turn(s) started within "
                        f"{timeout}s; the test waited for {count}")
                self._changed.wait(remaining)
            return list(self.turns[:count])


class ProductSchedulerHandle:
    """A real ProductScheduler, built by the product_scheduler fixture, and
    what a test needs to drive and observe its thread.

    `scheduler` is the object under test. `clock` and `agent` are what was
    injected into it; `store`, `auth`, `principal` and `locks` are real
    collaborators, with the store and the session locks on disk. The handle
    observes tick() without replacing it: a wrapper on this instance counts
    the ticks the thread completes and keeps any exception that escapes one,
    which today ends the thread (audit F-05)."""

    def __init__(self, directory, *, agent, clock, interval_s, auth=None,
                 store=None, session_locks=None, **scheduler_kwargs):
        from product_service import (
            AuthRegistry, ProductScheduler, ProductScheduleStore,
            SessionOperationLocks)

        self.clock = clock
        self.agent = agent
        self.auth = auth if auth is not None else AuthRegistry([{
            "sha256": hashlib.sha256(_SCHEDULER_TOKEN.encode()).hexdigest(),
            "principal": SCHEDULER_PRINCIPAL,
            "tenant": SCHEDULER_TENANT,
            # chat too, so the harness keeps running turns whichever way the
            # scope that scheduled turns need (audit F-28) is decided
            "scopes": ["chat", "schedules:read", "schedules:write"],
            "tools": ["read_file"],
        }])
        self.principal = next(
            (p for p in self.auth.principals if "schedules:write" in p.scopes), None)
        self.store = store if store is not None else ProductScheduleStore(
            Path(directory) / "product-schedules.json", clock=clock.time)
        self.locks = (session_locks if session_locks is not None
                      else SessionOperationLocks(Path(directory) / "locks"))
        self.scheduler = ProductScheduler(
            self.store, agent, self.auth, clock=clock, interval_s=interval_s,
            session_locks=self.locks, **scheduler_kwargs)
        self.thread = None
        self.tick_errors = []
        self._ticks_begun = 0
        self._ticks_done = 0
        self._changed = threading.Condition()
        self._closed = False
        tick = self.scheduler.tick

        def observed_tick():
            with self._changed:
                self._ticks_begun += 1
            try:
                return tick()
            except BaseException as error:
                with self._changed:
                    self.tick_errors.append(error)
                raise
            finally:
                with self._changed:
                    self._ticks_done += 1
                    self._changed.notify_all()

        self.scheduler.tick = observed_tick

    @property
    def ticks(self):
        """The ticks the thread has completed, whether they returned or raised."""
        with self._changed:
            return self._ticks_done

    def add(self, name, *, every_ms=3_600_000, session=None,
            message="scheduled message", principal=None):
        """Create a schedule as POST /v1/schedules does. It has never run, so
        it is due at once."""
        self.store.put(principal or self.principal, name=name,
                       session=session or name, message=message, every_ms=every_ms)

    def schedule(self, name, principal=None):
        """The entry as GET /v1/schedules lists it, or None."""
        tenant = (principal or self.principal).tenant_id
        return next((entry for entry in self.store.for_tenant(tenant)
                     if entry["name"] == name), None)

    def stored(self, name, principal=None):
        """The entry as the store holds it, lease fields included, or None."""
        from product_service import ProductScheduleStore

        tenant = (principal or self.principal).tenant_id
        return self.store.get(ProductScheduleStore._key(tenant, name))

    def start(self):
        """Start the real thread (ProductScheduler.start) and return it."""
        self.thread = self.scheduler.start()
        return self.thread

    def stop(self, timeout_s=None):
        """ProductScheduler.stop(), as product_main's drain calls it."""
        return self.scheduler.stop(timeout_s)

    def wait_for_ticks(self, count=1, timeout=5.0):
        """Wait until `count` ticks that begin after this call have completed,
        so each of them saw whatever the test changed just before (a clock
        advance, a new or deleted entry). After `timeout` it fails the test,
        saying whether the thread is alive and what escaped tick()."""
        deadline = time.monotonic() + timeout
        with self._changed:
            target = self._ticks_begun + count
            while self._ticks_done < target:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"waited {timeout}s for {count} more tick(s); the thread "
                        f"has completed {self._ticks_done} in all and is "
                        f"{self._thread_state()}; exceptions that escaped "
                        f"tick(): {self.tick_errors!r}")
                self._changed.wait(remaining)

    def wait_for(self, predicate, what, timeout=5.0):
        """Poll predicate() every 5 ms until it is true. After `timeout` it
        fails the test, naming `what` it waited for."""
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"not within {timeout}s: {what} (the scheduler thread is "
                    f"{self._thread_state()})")
            time.sleep(0.005)

    def _thread_state(self):
        if self.thread is None:
            return "never started"
        return "alive" if self.thread.is_alive() else "no longer alive"

    def close(self, timeout=10.0):
        """Release any held turn, ask the scheduler to stop, and join its
        thread. A thread still running `timeout` seconds later fails the test,
        showing where it is, instead of hanging the suite. The verdict is the
        thread's own: stop()'s return value is what the lifecycle tests check,
        so teardown does not rely on it. Calling it again does nothing."""
        if self._closed:
            return
        self._closed = True
        release = getattr(self.agent, "release", None)
        if callable(release):
            release()
        deadline = time.monotonic() + timeout
        stopped = self.scheduler.stop(timeout)
        thread = self.thread or getattr(self.scheduler, "_thread", None)
        if thread is None or thread.ident is None:
            if stopped:
                return
            raise AssertionError(
                "ProductScheduler.stop() reported a thread that did not stop, and "
                "the harness cannot find that thread to join it")
        thread.join(max(0.0, deadline - time.monotonic()))
        if not thread.is_alive():
            return
        frame = sys._current_frames().get(thread.ident)
        where = (" It is at:\n" + "".join(traceback.format_stack(frame)[-8:])
                 if frame is not None else "")
        raise AssertionError(
            f"ProductScheduler's thread was still running {timeout}s after "
            f"teardown asked it to stop; any held scripted turn had been released, "
            f"so something else holds it.{where}")


@pytest.fixture()
def product_scheduler(tmp_path):
    """Build a real ProductScheduler, and start its thread when the test says:

        scheduler = product_scheduler()            # ManualClock, ScriptedTurnAgent
        scheduler.add("daily", session="work")     # never run, so due at once
        scheduler.start()                          # the real thread
        [turn] = scheduler.agent.wait_for_turns(1)
        scheduler.clock.advance(3600)              # an hour on: due again

    agent= and clock= replace the scripted agent and the manual clock;
    interval_s= is the thread's pause between ticks (0.01 s here, so it looks
    often; the product's is 1 s); auth=, store= and session_locks= replace
    the harness's own; any other keyword goes to ProductScheduler (lease_s=,
    turn_deadline_s=, quota_store=, ...). Every scheduler built here is
    stopped and joined in teardown after its held turns are released, and
    one that does not stop within the bound fails the test instead of
    hanging it (ProductSchedulerHandle.close)."""
    handles = []

    def build(*, agent=None, clock=None, interval_s=0.01, **kwargs):
        handle = ProductSchedulerHandle(
            tmp_path / f"product-scheduler-{len(handles)}",
            agent=agent if agent is not None else ScriptedTurnAgent(),
            clock=clock if clock is not None else ManualClock(),
            interval_s=interval_s, **kwargs)
        handles.append(handle)
        return handle

    yield build
    failures = []
    for handle in handles:
        try:
            handle.close()
        except AssertionError as error:
            failures.append(str(error))
    if failures:
        pytest.fail("\n".join(failures), pytrace=False)
