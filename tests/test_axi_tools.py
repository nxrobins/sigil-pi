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

from conftest import (API_KEY, PI_ROOT, SIGIL_ROOT,  # fixtures via conftest
                      msg, text, tool_use)
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
    from sigil_compose import compose_with_stdlib
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


def test_npm_shape_json_null_reads_as_absent_not_as_the_word_null(mcp):
    """SWEEP: `"description": null` is common on real registry documents, and
    the json stdlib returns the literal characters `null` for it — so the
    digest read `x@1 — null`, telling the model the description IS the word
    'null'. A JSON null is ABSENCE; it must take the absent path."""
    ok, out = _forge_shape(mcp, "npm_shape",
                           '{"name":"x","version":"1","description":null}')
    assert ok, out
    assert out == "x@1\nlicense: unknown\ndependencies (0): none"

    ok, out = _forge_shape(mcp, "npm_shape",
                           '{"name":"x","version":"1","license":null}')
    assert ok, out
    assert "license: unknown" in out

    ok, out = _forge_shape(mcp, "npm_shape",
                           '{"name":"x","version":"1","dependencies":null}')
    assert ok, out
    assert out.endswith("dependencies (0): none")


def test_npm_shape_object_license_takes_its_type(mcp):
    """SWEEP: older packages spell the license as {"type":"MIT","url":...}.
    Emitting the raw slice put a JSON blob in a digest whose whole purpose is
    to avoid raw JSON — take `.type`, and fall back to 'unknown' if absent."""
    ok, out = _forge_shape(mcp, "npm_shape",
                           '{"name":"x","version":"1","license":{"type":"MIT","url":"u"}}')
    assert ok, out
    assert "license: MIT" in out

    ok, out = _forge_shape(mcp, "npm_shape",
                           '{"name":"x","version":"1","license":{"url":"u"}}')
    assert ok, out
    assert "license: unknown" in out


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


GH_TOKEN = "ghp_TESTTOKEN_must_never_appear_in_output"


def _gh_grants(token=GH_TOKEN):
    g = {"net": ["127.0.0.1"]}
    if token is not None:
        g["secret"] = [f"github={token}"]
    return g


def _gql(total, nodes):
    return json.dumps({"data": {"repository": {"issues": {
        "totalCount": total, "nodes": nodes}}}})


def _node(number, title, comments):
    return {"number": number, "title": title, "comments": {"totalCount": comments}}


# ── gh_fetch: host-injected token, strict repo validation ────────────────


def test_gh_fetch_sends_injected_token_and_user_agent(json_server, mcp):
    """M5a, on a second secret: the guest ships a PLACEHOLDER and the host
    substitutes. The wire must carry the real bearer token, and the guest
    source must never contain it."""
    json_server.routes["/graphql"] = (200, _gql(0, []))
    ok, out = _forge_tool(mcp, "gh_fetch",
                          f"{json_server.url}/graphql|octocat/Hello-World",
                          _gh_grants())
    assert ok, out
    [req] = json_server.requests
    assert req.method == "POST"
    assert req.headers["authorization"] == f"bearer {GH_TOKEN}"
    assert "sigil-pi" in req.headers.get("user-agent", "")
    body = json.loads(req.body)
    assert "octocat" in body["query"] and "Hello-World" in body["query"]
    assert GH_TOKEN not in (PI_ROOT / "tools" / "gh_fetch.sigil").read_text()


def test_gh_fetch_without_the_secret_grant_is_denied(json_server, mcp):
    """Fail-closed: no configured github secret means no `secret` grant, so the
    placeholder is ungranted and the runtime refuses — the request is never
    sent, rather than going out unauthenticated."""
    json_server.routes["/graphql"] = (200, _gql(0, []))
    ok, out = _forge_tool(mcp, "gh_fetch",
                          f"{json_server.url}/graphql|octocat/Hello-World",
                          _gh_grants(token=None))
    assert not ok and "403" in out
    assert json_server.requests == [], "an unauthenticated request went out"


@pytest.mark.parametrize("bad", [
    "no-slash", "a/b/c", "/name", "owner/", "own er/name", "owner/na me",
    "owner/name?x=1", 'owner/na"me', "owner/na\\me", "owner/näme", "",
])
def test_gh_fetch_rejects_malformed_repos(json_server, mcp, bad):
    """The repo goes into a GraphQL string, so it is WHITELIST-validated
    (owner/name, [A-Za-z0-9_.-], exactly one slash) rather than escaped —
    no quote or backslash can reach the query, and nothing is sent."""
    json_server.routes["/graphql"] = (200, _gql(0, []))
    ok, out = _forge_tool(mcp, "gh_fetch",
                          f"{json_server.url}/graphql|{bad}", _gh_grants())
    assert not ok and "400" in out, f"{bad!r} was accepted: {out!r}"
    assert json_server.requests == [], f"{bad!r} still reached the network"


# ── gh_shape: aggregates, empty state, and GraphQL's 200-with-errors ─────


def test_gh_shape_lists_issues_with_aggregate(mcp):
    doc = _gql(42, [_node(101, "Crash on startup", 3),
                    _node(99, "Docs typo", 0)])
    ok, out = _forge_shape(mcp, "gh_shape", doc)
    assert ok, out
    assert out == ("open issues (42), showing 2\n"
                   "#101 Crash on startup (3 comments)\n"
                   "#99 Docs typo (0 comments)")


def test_gh_shape_empty_state_is_definitive(mcp):
    ok, out = _forge_shape(mcp, "gh_shape", _gql(0, []))
    assert ok, out
    assert out == "no open issues"


def test_gh_shape_reports_graphql_errors_not_a_parse_failure(mcp):
    """GraphQL answers 200 with {"errors":[...],"data":null} for a repo that
    does not exist or is not visible to the token. Walking `data` first would
    surface that as a misleading malformed-JSON error, so `errors` is checked
    FIRST and reported as -404 (the honest 'no such repo / no access')."""
    doc = json.dumps({"data": None, "errors": [
        {"type": "NOT_FOUND", "message": "Could not resolve to a Repository"}]})
    ok, out = _forge_shape(mcp, "gh_shape", doc)
    assert not ok and "404" in out


def test_gh_shape_title_newlines_cannot_break_the_line_format(mcp):
    """SWEEP: the output is LINE-oriented (one issue per line), and a GitHub
    title is user input that may contain a newline — which split one issue
    across two lines and made the digest unparseable. Newlines and carriage
    returns in a title become spaces; the line count must equal 1 + issues."""
    doc = _gql(2, [_node(1, "crash\non startup", 0),
                   _node(2, "carriage\rreturn", 1)])
    ok, out = _forge_shape(mcp, "gh_shape", doc)
    assert ok, out
    assert out.split("\n") == ["open issues (2), showing 2",
                               "#1 crash on startup (0 comments)",
                               "#2 carriage return (1 comments)"]


def test_gh_shape_malformed_json_is_400(mcp):
    ok, out = _forge_shape(mcp, "gh_shape", '{"data": {"repository":')
    assert not ok and "400" in out


@settings(max_examples=25, deadline=None)
@given(st.lists(st.tuples(st.integers(min_value=1, max_value=99999),
                          # min_codepoint=9 so \n and \r ARE generated: the
                          # line-break flattening is a correctness property of
                          # a line-oriented format, not an incidental detail.
                          st.text(alphabet=st.characters(
                              min_codepoint=9, max_codepoint=1000,
                              exclude_characters='"\\'), min_size=0, max_size=24),
                          st.integers(min_value=0, max_value=9999)),
                min_size=0, max_size=6),
       st.integers(min_value=0, max_value=99999))
def test_gh_shape_matches_reference(mcp, nodes, total):
    """Line-for-line against a Python reference over generated issue lists —
    titles carry punctuation, unicode, control bytes and line breaks."""
    doc = _gql(total, [_node(n, t, c) for n, t, c in nodes])
    ok, out = _forge_shape(mcp, "gh_shape", doc)
    assert ok, out
    if not nodes:
        expected = "no open issues"
    else:
        head = f"open issues ({total}), showing {len(nodes)}"
        expected = "\n".join(
            [head] + [f"#{n} {t.replace(chr(10), ' ').replace(chr(13), ' ')} "
                      f"({c} comments)" for n, t, c in nodes])
    assert out == expected
    # the structural invariant the flattening exists to protect
    assert len(out.split("\n")) == (1 + len(nodes) if nodes else 1)


# ── gh_issues end-to-end ─────────────────────────────────────────────────


def _gh_manifest(base):
    return {"gh_issues": {
        "source": "tools/gh_fetch.sigil",
        "shape": "tools/gh_shape.sigil",
        "args": ["repo"], "path_args": [],
        "bound_args": [base],
        "grants": {"net": ["127.0.0.1"], "secret": ["{SECRET:github}"]},
        "spec": _spec("gh_issues", ["repo"]),
    }}


def test_gh_issues_dispatch_end_to_end(json_server, scripted_llm, tmp_path, mcp):
    from agent import PiAgent, SessionStore
    json_server.routes["/graphql"] = (200, _gql(7, [_node(5, "Bug", 2)]))
    (tmp_path / "sessions").mkdir(); (tmp_path / "sandboxes").mkdir()
    mpath = tmp_path / "manifest.json"
    mpath.write_text(json.dumps(_gh_manifest(f"{json_server.url}/graphql")))
    agent = PiAgent(scripted_llm.url, API_KEY,
                    store=SessionStore(tmp_path / "sessions"),
                    sandbox_root=tmp_path / "sandboxes", mcp=mcp,
                    model="claude-mock", manifest_path=mpath,
                    secrets={"github": GH_TOKEN})
    scripted_llm.script = [
        msg([tool_use("t", "gh_issues", {"repo": "octocat/Hello-World"})]),
        msg([text("reported")]),
    ]
    assert agent.turn("s1", "what's open?") == "reported"
    r = _last_result(scripted_llm)
    assert r["content"] == "open issues (7), showing 1\n#5 Bug (2 comments)"
    assert "is_error" not in r
    assert agent.grant_log == [
        ("gh_issues", {"net": ["127.0.0.1"], "secret": [f"github={GH_TOKEN}"]}),
        ("gh_issues.shape", None),
    ]


def test_gh_issues_without_a_configured_token_fails_closed(
        json_server, scripted_llm, tmp_path, mcp):
    """No PI_SECRET_GITHUB → the {SECRET:github} expansion is empty → the
    placeholder is ungranted → -403, surfaced to the model as an error it can
    explain. Mirrors {NET_ALLOWLIST}'s fail-closed default."""
    from agent import PiAgent, SessionStore
    json_server.routes["/graphql"] = (200, _gql(0, []))
    (tmp_path / "sessions").mkdir(); (tmp_path / "sandboxes").mkdir()
    mpath = tmp_path / "manifest.json"
    mpath.write_text(json.dumps(_gh_manifest(f"{json_server.url}/graphql")))
    agent = PiAgent(scripted_llm.url, API_KEY,
                    store=SessionStore(tmp_path / "sessions"),
                    sandbox_root=tmp_path / "sandboxes", mcp=mcp,
                    model="claude-mock", manifest_path=mpath)
    scripted_llm.script = [
        msg([tool_use("t", "gh_issues", {"repo": "octocat/Hello-World"})]),
        msg([text("no access")]),
    ]
    assert agent.turn("s1", "issues?") == "no access"
    r = _last_result(scripted_llm)
    assert r["is_error"] is True and "403" in r["content"]
    # the `secret` key survives EMPTY rather than disappearing — same shape
    # {NET_ALLOWLIST} produces for an unconfigured fetch (see
    # test_fetch_fail_closed_by_default), so an ungranted placeholder reads
    # the same way for every host-expanded grant.
    assert agent.grant_log[0] == ("gh_issues", {"net": ["127.0.0.1"], "secret": []})
    assert json_server.requests == []


GL_TOKEN = "glpat-TESTTOKEN_must_never_appear"


def _gl_grants(token=GL_TOKEN):
    g = {"net": ["127.0.0.1"]}
    if token is not None:
        g["secret"] = [f"gitlab={token}"]
    return g


def _glq(count, nodes):
    return json.dumps({"data": {"project": {"issues": {
        "count": count, "nodes": nodes}}}})


def _gl_node(iid, title, notes):
    return {"iid": str(iid), "title": title, "userNotesCount": notes}


def test_gl_fetch_sends_the_injected_token(json_server, mcp):
    """The generalized secret mechanism carrying a SECOND provider's
    credential down the same host-injection path."""
    json_server.routes["/api/graphql"] = (200, _glq(0, []))
    ok, out = _forge_tool(mcp, "gl_fetch",
                          f"{json_server.url}/api/graphql|group/proj",
                          _gl_grants())
    assert ok, out
    [req] = json_server.requests
    assert req.method == "POST"
    assert req.headers["authorization"] == f"bearer {GL_TOKEN}"
    body = json.loads(req.body)
    assert "group/proj" in body["query"]
    assert GL_TOKEN not in (PI_ROOT / "tools" / "gl_fetch.sigil").read_text()


def test_gl_fetch_without_the_secret_is_denied(json_server, mcp):
    json_server.routes["/api/graphql"] = (200, _glq(0, []))
    ok, out = _forge_tool(mcp, "gl_fetch",
                          f"{json_server.url}/api/graphql|group/proj",
                          _gl_grants(token=None))
    assert not ok and "403" in out
    assert json_server.requests == []


def test_gl_fetch_accepts_nested_groups(json_server, mcp):
    """GitLab nests, unlike GitHub — 'group/sub/proj' is a legal path and must
    not be rejected by a GitHub-shaped exactly-one-slash rule."""
    json_server.routes["/api/graphql"] = (200, _glq(0, []))
    ok, out = _forge_tool(mcp, "gl_fetch",
                          f"{json_server.url}/api/graphql|group/sub/deep/proj",
                          _gl_grants())
    assert ok, out
    assert "group/sub/deep/proj" in json.loads(json_server.requests[0].body)["query"]


@pytest.mark.parametrize("bad", [
    "noslash", "/leading", "trailing/", "a//b", "a/b?x", 'a/b"c', "a/b\\c",
    "a/bä", "",
])
def test_gl_fetch_rejects_malformed_paths(json_server, mcp, bad):
    json_server.routes["/api/graphql"] = (200, _glq(0, []))
    ok, out = _forge_tool(mcp, "gl_fetch",
                          f"{json_server.url}/api/graphql|{bad}", _gl_grants())
    assert not ok and "400" in out, f"{bad!r} was accepted: {out!r}"
    assert json_server.requests == [], f"{bad!r} reached the network"


def test_gl_shape_lists_issues(mcp):
    doc = _glq(9, [_gl_node(3, "Pipeline broken", 4),
                   _gl_node(1, "Typo", 0)])
    ok, out = _forge_shape(mcp, "gl_shape", doc)
    assert ok, out
    assert out == ("open issues (9), showing 2\n"
                   "#3 Pipeline broken (4 comments)\n"
                   "#1 Typo (0 comments)")


def test_gl_shape_empty_and_errors(mcp):
    ok, out = _forge_shape(mcp, "gl_shape", _glq(0, []))
    assert ok and out == "no open issues"
    doc = json.dumps({"data": None,
                      "errors": [{"message": "project not found"}]})
    ok, out = _forge_shape(mcp, "gl_shape", doc)
    assert not ok and "404" in out


def test_gl_shape_flattens_title_newlines(mcp):
    doc = _glq(1, [_gl_node(7, "broke\nover lines", 2)])
    ok, out = _forge_shape(mcp, "gl_shape", doc)
    assert ok, out
    assert out.split("\n") == ["open issues (1), showing 1",
                               "#7 broke over lines (2 comments)"]


@settings(max_examples=25, deadline=None)
@given(st.lists(st.tuples(st.integers(min_value=1, max_value=9999),
                          st.text(alphabet=st.characters(
                              min_codepoint=9, max_codepoint=1000,
                              exclude_characters='"\\'), min_size=0, max_size=20),
                          st.integers(min_value=0, max_value=999)),
                min_size=0, max_size=5),
       st.integers(min_value=0, max_value=9999))
def test_gl_shape_matches_reference(mcp, nodes, count):
    doc = _glq(count, [_gl_node(i, t, n) for i, t, n in nodes])
    ok, out = _forge_shape(mcp, "gl_shape", doc)
    assert ok, out
    if not nodes:
        expected = "no open issues"
    else:
        head = f"open issues ({count}), showing {len(nodes)}"
        expected = "\n".join(
            [head] + [f"#{i} {t.replace(chr(10), ' ').replace(chr(13), ' ')} "
                      f"({n} comments)" for i, t, n in nodes])
    assert out == expected


def test_gl_issues_dispatch_end_to_end(json_server, scripted_llm, tmp_path, mcp):
    from agent import AuditLog, PiAgent, SessionStore
    json_server.routes["/api/graphql"] = (200, _glq(3, [_gl_node(2, "Bug", 1)]))
    (tmp_path / "sessions").mkdir(); (tmp_path / "sandboxes").mkdir()
    mpath = tmp_path / "manifest.json"
    mpath.write_text(json.dumps({"gl_issues": {
        "source": "tools/gl_fetch.sigil", "shape": "tools/gl_shape.sigil",
        "args": ["project"], "path_args": [],
        "bound_args": [f"{json_server.url}/api/graphql"],
        "grants": {"net": ["127.0.0.1"], "secret": ["{SECRET:gitlab}"]},
        "spec": _spec("gl_issues", ["project"])}}))
    agent = PiAgent(scripted_llm.url, API_KEY,
                    store=SessionStore(tmp_path / "sessions"),
                    sandbox_root=tmp_path / "sandboxes", mcp=mcp,
                    model="claude-mock", manifest_path=mpath,
                    secrets={"gitlab": GL_TOKEN},
                    audit=AuditLog(tmp_path / "audit", enabled=False))
    scripted_llm.script = [
        msg([tool_use("t", "gl_issues", {"project": "group/proj"})]),
        msg([text("listed")]),
    ]
    assert agent.turn("s1", "issues?") == "listed"
    r = _last_result(scripted_llm)
    assert r["content"] == "open issues (3), showing 1\n#2 Bug (1 comments)"
    assert agent.grant_log == [
        ("gl_issues", {"net": ["127.0.0.1"], "secret": [f"gitlab={GL_TOKEN}"]}),
        ("gl_issues.shape", None),
    ]


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
