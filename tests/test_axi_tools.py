"""M12 AXI tools — npm_info and gh_issues, each a fetch→shape pipeline.

Direct-forge tests drive each stage alone (the mock server sees exactly what
the guest sends; fixture JSONs drive the shapers), then dispatch integration
proves the pipeline end-to-end with grant minimality. Property tests pin the
one piece of hand-rolled parsing — npm_shape's flat-object key walker (the
stdlib json API has no key enumeration) — against a Python reference.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from hypothesis import given, settings, strategies as st

from conftest import PI_ROOT, SIGIL_ROOT, msg, text, tool_use  # fixtures via conftest
from test_pipeline import pipeline_agent, _last_result, _spec
from test_tools import _forge_tool


# ── a canned-response HTTP server (npm GET routes / gh POST graphql) ─────


@pytest.fixture()
def json_server():
    """Routes dict: path -> (status:int, body:str). Records every request as
    SimpleNamespace(method, path, headers, body)."""
    state = SimpleNamespace(routes={}, requests=[], url=None)

    class Handler(BaseHTTPRequestHandler):
        def _serve(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(n) if n else b""
            state.requests.append(SimpleNamespace(
                method=self.command, path=self.path,
                headers={k.lower(): v for k, v in self.headers.items()},
                body=body))
            status, payload = state.routes.get(
                self.path, (404, '{"error":"Not found"}'))
            raw = payload.encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = _serve
        do_POST = _serve

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield state
    srv.shutdown()
    srv.server_close()


def _forge_shape(mcp, name, input_text, fuel=50_000_000):
    """Forge a shape stage the way _dispatch does: composed with stdlib json
    iff the source uses it, grantless."""
    from sigil_bench.compose import compose_with_stdlib
    src = (PI_ROOT / "tools" / f"{name}.sigil").read_text()
    if "use sigil::json;" in src:
        src = compose_with_stdlib(src, ["json"], SIGIL_ROOT).text
    r = mcp.forge(src, input=input_text, fuel=fuel)
    if r.get("status") == "ok":
        return True, r["data"]["output_text"]
    d = (r.get("diagnostics") or [{}])[0]
    return False, f"{d.get('code')}: {d.get('message') or ''}"


NET = lambda server: {"net": ["127.0.0.1"]}  # noqa: E731


# ── npm_fetch: URL building, validation, encoding ────────────────────────


def test_npm_fetch_gets_package_latest(json_server, mcp):
    json_server.routes["/left-pad/latest"] = (200, '{"name":"left-pad"}')
    ok, out = _forge_tool(mcp, "npm_fetch",
                          f"{json_server.url}|left-pad", NET(json_server))
    assert ok, out
    assert out == '{"name":"left-pad"}'
    [req] = json_server.requests
    assert (req.method, req.path) == ("GET", "/left-pad/latest")


def test_npm_fetch_percent_encodes_scoped_names(json_server, mcp):
    json_server.routes["/@types%2fnode/latest"] = (200, '{"name":"@types/node"}')
    ok, out = _forge_tool(mcp, "npm_fetch",
                          f"{json_server.url}|@types/node", NET(json_server))
    assert ok, out
    assert json_server.requests[-1].path == "/@types%2fnode/latest"


def test_npm_fetch_unknown_package_propagates_404(json_server, mcp):
    ok, out = _forge_tool(mcp, "npm_fetch",
                          f"{json_server.url}|no-such-pkg-xyz", NET(json_server))
    assert not ok and "404" in out


@pytest.mark.parametrize("bad", [
    "",                    # empty
    "left pad",            # space
    "left\tpad",           # control
    "pkg?download=1",      # query smuggling
    "pkg#frag",            # fragment
    "pkg%2e%2e",           # pre-encoded bytes — we do the encoding, not the model
    "week|end",            # a second pipe would shift nothing, but names can't have one
    "päckage",             # non-ASCII — npm names are ASCII
])
def test_npm_fetch_rejects_invalid_package_names(json_server, mcp, bad):
    """Whitelist validation (-400): letters, digits, ``- _ . @ /`` only —
    and NO request is made for a rejected name."""
    ok, out = _forge_tool(mcp, "npm_fetch",
                          f"{json_server.url}|{bad}", NET(json_server))
    assert not ok and "400" in out, f"{bad!r} was accepted: {out!r}"
    assert json_server.requests == [], f"{bad!r} still reached the network"


def test_npm_fetch_without_separator_is_400(json_server, mcp):
    ok, out = _forge_tool(mcp, "npm_fetch", "no-pipe-here", NET(json_server))
    assert not ok and "400" in out


# ── npm_shape: AXI-minimal digest of a /latest doc ───────────────────────


FULL_DOC = json.dumps({
    "name": "left-pad", "version": "1.3.0",
    "description": "String left pad",
    "license": "WTFPL",
    "dependencies": {"loose-envify": "^1.1.0", "scheduler": "~0.23"},
    "devDependencies": {"jest": "^29"},          # ignored
    "dist": {"tarball": "https://x/y.tgz"},      # ignored
})


def test_npm_shape_full_document(mcp):
    ok, out = _forge_shape(mcp, "npm_shape", FULL_DOC)
    assert ok, out
    assert out == ("left-pad@1.3.0 — String left pad\n"
                   "license: WTFPL\n"
                   "dependencies (2): loose-envify, scheduler")


def test_npm_shape_definitive_empty_states(mcp):
    """AXI principle 5: absent description drops its clause, absent license
    reads 'unknown', absent/empty dependencies read '(0): none' — never a
    blank or a guess."""
    ok, out = _forge_shape(mcp, "npm_shape",
                           '{"name":"tiny","version":"2.0.1"}')
    assert ok, out
    assert out == "tiny@2.0.1\nlicense: unknown\ndependencies (0): none"

    ok, out = _forge_shape(mcp, "npm_shape",
                           '{"name":"t","version":"1","dependencies":{}}')
    assert ok, out
    assert out.endswith("dependencies (0): none")


def test_npm_shape_alias_versions_with_colons(mcp):
    """`npm:alias@^1` version strings contain colons and at-signs — the
    value skipper must not mistake them for structure."""
    doc = json.dumps({"name": "x", "version": "1.0.0",
                      "dependencies": {"a": "npm:@scope/real@^1.2", "b": "^2"}})
    ok, out = _forge_shape(mcp, "npm_shape", doc)
    assert ok, out
    assert out.endswith("dependencies (2): a, b")


def test_npm_shape_missing_name_or_version_errors(mcp):
    ok, out = _forge_shape(mcp, "npm_shape", '{"version":"1.0.0"}')
    assert not ok and "404" in out
    ok, out = _forge_shape(mcp, "npm_shape", '{"name":"x"}')
    assert not ok and "404" in out


def test_npm_shape_malformed_json_is_400(mcp):
    ok, out = _forge_shape(mcp, "npm_shape", '{"name": "x", "version": ')
    assert not ok and "400" in out


DEP_NAME = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=1000,
                           exclude_characters='"\\'),
    min_size=1, max_size=12)
DEP_VERSION = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=1000),
    min_size=0, max_size=16)


@settings(max_examples=25, deadline=None)
@given(st.dictionaries(DEP_NAME, DEP_VERSION, min_size=0, max_size=8))
def test_npm_shape_dep_walker_matches_reference(mcp, deps):
    """The one hand-rolled parser (stdlib json has no key enumeration) vs a
    Python reference, over generated objects whose VALUES carry hostile
    structure — braces, brackets, quotes-via-escape, colons, commas — which
    json.dumps escapes and the walker must skip string-aware."""
    doc = json.dumps({"name": "p", "version": "1", "dependencies": deps},
                     ensure_ascii=False)
    ok, out = _forge_shape(mcp, "npm_shape", doc)
    assert ok, out
    if deps:
        expected = f"dependencies ({len(deps)}): {', '.join(deps.keys())}"
    else:
        expected = "dependencies (0): none"
    # split("\n"), NOT splitlines(): the tool's separator is exactly \n, but
    # str.splitlines() also breaks on U+0085/U+2028/U+2029 — which are legal
    # bytes INSIDE a dependency name, so splitlines() truncated the last line
    # mid-name and reported a tool bug that did not exist. (Found by this
    # property test generating a U+0085 key.)
    assert out.split("\n")[-1] == expected


# ── npm_info end-to-end: the pipeline through dispatch ───────────────────


def _npm_manifest(base):
    return {"npm_info": {
        "source": "tools/npm_fetch.sigil",
        "shape": "tools/npm_shape.sigil",
        "args": ["package"], "path_args": [],
        "bound_args": [base],
        "grants": {"net": ["127.0.0.1"]},
        "spec": _spec("npm_info", ["package"]),
    }}


def test_npm_info_dispatch_end_to_end(json_server, scripted_llm, tmp_path, mcp):
    json_server.routes["/left-pad/latest"] = (200, FULL_DOC)
    agent = pipeline_agent(scripted_llm, tmp_path, mcp,
                           _npm_manifest(json_server.url))
    scripted_llm.script = [
        msg([tool_use("t", "npm_info", {"package": "left-pad"})]),
        msg([text("summarized")]),
    ]
    assert agent.turn("s1", "what is left-pad?") == "summarized"
    r = _last_result(scripted_llm)
    assert r["content"].startswith("left-pad@1.3.0 — String left pad")
    assert "is_error" not in r
    assert agent.grant_log == [
        ("npm_info", {"net": ["127.0.0.1"]}),
        ("npm_info.shape", None),
    ]
