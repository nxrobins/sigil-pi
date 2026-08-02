"""Dispatch tests for the broadened toolset (fetch, list_dir, grep_file,
append_file) — each a v14-authored forge with its own minimal grant.

Reuses the scripted-mock-LLM fixtures from conftest. The LLM is scripted to
emit a tool_use, the host forges the tool in the session sandbox, and we assert
the byte-exact tool_result content + grant minimality (agent.grant_log)."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from hypothesis import given, settings, strategies as st

from conftest import API_KEY, PI_ROOT, make_agent, msg, text, tool_use  # fixtures via conftest


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


def test_list_dir_on_a_file_is_error(scripted_llm, tmp_path, mcp):
    """Listing a regular file (not a directory) is a contained -404 error."""
    agent = make_agent(scripted_llm, tmp_path, mcp)
    (agent.sandbox_for("s1") / "f.txt").write_text("hi")
    scripted_llm.script = [
        msg([tool_use("t", "list_dir", {"path": "f.txt"})]),
        msg([text("not a dir")]),
    ]
    assert agent.turn("s1", "list a file") == "not a dir"
    r = _last_tool_result(scripted_llm)
    assert r["is_error"] is True and "404" in r["content"]


def test_new_fs_tools_deny_path_traversal(scripted_llm, tmp_path, mcp):
    """`..` in any new fs tool's path resolves outside the session sandbox and
    is denied by the fs grant (-403) — same guard as read_file/write_file."""
    agent = make_agent(scripted_llm, tmp_path, mcp)
    # plant a file in the sandboxes ROOT (the sandbox's parent) to try to reach
    (tmp_path / "sandboxes" / "victim.txt").write_text("secret")
    for i, (tool, inp) in enumerate([
        ("grep_file", {"path": "../victim.txt", "pattern": "x"}),
        ("list_dir", {"path": ".."}),
        ("append_file", {"path": "../victim.txt", "content": "x"}),
    ]):
        # the mock indexes its script by cumulative request count; reset it and
        # use a distinct session so each tool starts clean.
        scripted_llm.requests.clear()
        scripted_llm.script = [msg([tool_use("t", tool, inp)]), msg([text("blocked")])]
        assert agent.turn(f"trav{i}", "climb") == "blocked"
        r = _last_tool_result(scripted_llm)
        assert r["is_error"] is True, f"{tool} traversal was not denied"
    # the victim file was never modified
    assert (tmp_path / "sandboxes" / "victim.txt").read_text() == "secret"


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


def _grep(mcp, tmp_path, body: bytes, pattern: str):
    """Forge grep_file directly (no agent loop) — fast enough for a property
    suite, and it tests the TOOL rather than the dispatch around it."""
    sb = tmp_path / "gsb"
    sb.mkdir(exist_ok=True)
    f = sb / "t.txt"
    f.write_bytes(body)
    r = mcp.forge((PI_ROOT / "tools" / "grep_file.sigil").read_text(),
                  input=f"{f}|{pattern}", fuel=50_000_000, grants={"fs": [str(sb)]})
    assert r.get("status") == "ok", f"forge failed: {(r.get('diagnostics') or [{}])[0]}"
    return r["data"]["output_text"]


def _grep_reference(body: str, pattern: str) -> str:
    """What grep_file must return: the lines containing `pattern`, joined by
    '\\n', no trailing newline."""
    lines = body.split("\n")
    if body.endswith("\n"):
        lines = lines[:-1]
    return "\n".join(line for line in lines if pattern in line)


@pytest.mark.parametrize("body,pattern", [
    # THE REGRESSION: patterns whose prefix overlaps themselves. The original
    # matcher was a single streaming pass that reset its counter to at most 1
    # on a mismatch, so it could not back up into the partial match it had
    # already consumed — every one of these silently returned NO match.
    ("aaab\n", "aab"),
    ("nanano\n", "nano"),
    ("mississippi\n", "issip"),
    ("ababab\n", "abab"),
    ("aaaa\n", "aa"),
    ("baab\n", "aab"),
    ("aaab\naab\n", "aab"),
    # and the cases that always worked, kept so a "fix" can't trade one for the other
    ("xaabx\n", "aab"),
    ("alpha line\nbeta line\nalpha again\ngamma\n", "alpha"),
    ("alpha\nbeta\n", "zzz"),
    ("no newline at end", "end"),
    ("abc\n", "abcd"),          # pattern longer than the line
    ("\n\n\n", "x"),            # empty lines
    ("hello|world\n", "|"),     # pipe in the pattern (it is the last arg)
    ("émoji 😀 line\nplain\n", "😀"),
])
def test_grep_file_matches_the_reference(mcp, tmp_path, body, pattern):
    assert _grep(mcp, tmp_path, body.encode(), pattern) == _grep_reference(body, pattern)


@settings(max_examples=60, deadline=None)
@given(st.text(alphabet="ab\n", min_size=0, max_size=30),
       st.text(alphabet="ab", min_size=1, max_size=4))
def test_grep_file_property_vs_reference(mcp, tmp_path_factory, body, pattern):
    """Differential property test over a tiny alphabet — the alphabet is small
    on purpose, so overlapping prefixes occur constantly rather than by luck.
    This is the guard that keeps the matcher honest under any rewrite."""
    tmp_path = tmp_path_factory.mktemp("grep")
    assert _grep(mcp, tmp_path, body.encode(), pattern) == _grep_reference(body, pattern)


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


# ── the exploration trio: edit_file / list_tree / grep_tree ──────────────


def _forge_tool(mcp, name, input_text, grants):
    """Forge a tool source directly (no agent loop). Returns (True, output)
    or (False, first-diagnostic-message)."""
    src = (PI_ROOT / "tools" / f"{name}.sigil").read_text()
    r = mcp.forge(src, input=input_text, fuel=50_000_000, grants=grants)
    if r.get("status") == "ok":
        return True, r["data"]["output_text"]
    d = (r.get("diagnostics") or [{}])[0]
    return False, f"{d.get('code')}: {d.get('message') or ''}"


def len8(*args):
    """The len8 input framing the host builds for framing:"len8" tools:
    for each arg, 8 decimal digits of BYTE length, then the bytes. Unlike
    pipe-joining, every arg may contain absolutely any bytes."""
    return "".join(f"{len(a.encode()):08d}" + a for a in args)


def list_tree_ref(root):
    """What list_tree must print: sorted DFS, paths relative to the root,
    directories suffixed '/', children right after their directory."""
    out = []

    def walk(d, rel):
        for name in sorted(p.name for p in d.iterdir()):
            child = d / name
            r = f"{rel}/{name}" if rel else name
            if child.is_dir():
                out.append(r + "/")
                walk(child, r)
            else:
                out.append(r)

    walk(root, "")
    return "\n".join(out)


def grep_tree_ref(root, pattern):
    """What grep_tree must print: for every file in sorted DFS order, every
    line containing `pattern`, as 'relpath:lineno: line' (grep -n shaped)."""
    out = []

    def walk(d, rel):
        for name in sorted(p.name for p in d.iterdir()):
            child = d / name
            r = f"{rel}/{name}" if rel else name
            if child.is_dir():
                walk(child, r)
            else:
                body = child.read_text()
                lines = body.split("\n")
                if body.endswith("\n"):
                    lines = lines[:-1]
                for i, line in enumerate(lines, 1):
                    if pattern in line:
                        out.append(f"{r}:{i}: {line}")

    walk(root, "")
    return "\n".join(out)


def _seed(root, tree):
    """Materialize {name: str-content | dict-subtree} under root."""
    for name, val in tree.items():
        if isinstance(val, dict):
            (root / name).mkdir()
            _seed(root / name, val)
        else:
            (root / name).write_text(val)


SAMPLE_TREE = {
    "a.txt": "alpha line\nshared token\n",
    "empty_dir": {},
    "sub": {
        "b.txt": "beta\nshared token here\n",
        "deep": {"c.txt": "gamma, no trailing newline"},
    },
    "z.txt": "",
}


# ── list_tree ─────────────────────────────────────────────────────────────


def test_list_tree_matches_the_reference(mcp, tmp_path):
    root = tmp_path / "sb"
    root.mkdir()
    _seed(root, SAMPLE_TREE)
    ok, out = _forge_tool(mcp, "list_tree", str(root), {"fs": [str(root)]})
    assert ok, out
    assert out == list_tree_ref(root)
    assert out == ("a.txt\nempty_dir/\nsub/\nsub/b.txt\nsub/deep/\n"
                   "sub/deep/c.txt\nz.txt")


def test_list_tree_empty_root_is_empty_output(mcp, tmp_path):
    root = tmp_path / "sb"
    root.mkdir()
    ok, out = _forge_tool(mcp, "list_tree", str(root), {"fs": [str(root)]})
    assert ok and out == ""


def test_list_tree_on_a_file_is_error(mcp, tmp_path):
    root = tmp_path / "sb"
    root.mkdir()
    (root / "f.txt").write_text("x")
    ok, out = _forge_tool(mcp, "list_tree", str(root / "f.txt"), {"fs": [str(root)]})
    assert not ok and "404" in out


@st.composite
def fs_trees(draw, depth=2):
    """Small random trees over a tiny name alphabet — sorted-order edge
    cases (prefix names, empty dirs, empty files) occur constantly."""
    names = draw(st.lists(st.text(alphabet="abz", min_size=1, max_size=3),
                          min_size=0, max_size=4, unique=True))
    tree = {}
    for name in names:
        if depth > 0 and draw(st.booleans()):
            tree[name] = draw(fs_trees(depth=depth - 1))
        else:
            tree[name] = draw(st.text(alphabet="ab \n", max_size=20))
    return tree


@settings(max_examples=40, deadline=None)
@given(fs_trees())
def test_list_tree_property_vs_reference(mcp, tmp_path_factory, tree):
    root = tmp_path_factory.mktemp("lt")
    _seed(root, tree)
    ok, out = _forge_tool(mcp, "list_tree", str(root), {"fs": [str(root)]})
    assert ok, out
    assert out == list_tree_ref(root)


# ── grep_tree ─────────────────────────────────────────────────────────────


def test_grep_tree_matches_the_reference(mcp, tmp_path):
    root = tmp_path / "sb"
    root.mkdir()
    _seed(root, SAMPLE_TREE)
    ok, out = _forge_tool(mcp, "grep_tree", f"{root}|shared token",
                          {"fs": [str(root)]})
    assert ok, out
    assert out == grep_tree_ref(root, "shared token")
    assert out == ("a.txt:2: shared token\n"
                   "sub/b.txt:2: shared token here")


def test_grep_tree_no_match_is_empty_not_error(mcp, tmp_path):
    root = tmp_path / "sb"
    root.mkdir()
    _seed(root, SAMPLE_TREE)
    ok, out = _forge_tool(mcp, "grep_tree", f"{root}|zzz-nowhere",
                          {"fs": [str(root)]})
    assert ok and out == ""


def test_grep_tree_pattern_may_contain_pipes(mcp, tmp_path):
    """pattern is the LAST pipe-framed segment, so pipes in it are legal —
    same contract as grep_file."""
    root = tmp_path / "sb"
    root.mkdir()
    (root / "f.txt").write_text("a|b here\nplain\n")
    ok, out = _forge_tool(mcp, "grep_tree", f"{root}|a|b", {"fs": [str(root)]})
    assert ok, out
    assert out == "f.txt:1: a|b here"


@settings(max_examples=40, deadline=None)
@given(fs_trees(), st.text(alphabet="ab", min_size=1, max_size=3))
def test_grep_tree_property_vs_reference(mcp, tmp_path_factory, tree, pattern):
    """Differential over random trees × overlapping-prone patterns — the
    same guard that keeps grep_file's matcher honest, one level up."""
    root = tmp_path_factory.mktemp("gt")
    _seed(root, tree)
    ok, out = _forge_tool(mcp, "grep_tree", f"{root}|{pattern}",
                          {"fs": [str(root)]})
    assert ok, out
    assert out == grep_tree_ref(root, pattern)


# ── edit_file ─────────────────────────────────────────────────────────────


def _edit_grants(root):
    return {"fs": [str(root)], "fs_write": [str(root)]}


def test_edit_file_replaces_exactly_one_occurrence(mcp, tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("alpha beta gamma")
    ok, out = _forge_tool(mcp, "edit_file",
                          len8(str(f), "beta", "BETA"), _edit_grants(tmp_path))
    assert ok and out == "ok"
    assert f.read_text() == "alpha BETA gamma"


def test_edit_file_arbitrary_bytes_in_old_and_new(mcp, tmp_path):
    """THE reason edit_file uses len8 framing: old/new may contain pipes,
    newlines, and multi-byte characters — none of it can shift the parse."""
    f = tmp_path / "f.txt"
    f.write_text("keep\na|b\némoji 😀 line\ntail")
    ok, out = _forge_tool(
        mcp, "edit_file",
        len8(str(f), "a|b\némoji 😀 line", "x|y\nz"), _edit_grants(tmp_path))
    assert ok and out == "ok"
    assert f.read_text() == "keep\nx|y\nz\ntail"


def test_edit_file_deletion_via_empty_new(mcp, tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("abc-DROP-def")
    ok, out = _forge_tool(mcp, "edit_file",
                          len8(str(f), "-DROP-", ""), _edit_grants(tmp_path))
    assert ok and out == "ok"
    assert f.read_text() == "abcdef"


def test_edit_file_old_not_found_is_461(mcp, tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("alpha")
    ok, out = _forge_tool(mcp, "edit_file",
                          len8(str(f), "missing", "x"), _edit_grants(tmp_path))
    assert not ok and "461" in out
    assert f.read_text() == "alpha"


def test_edit_file_ambiguous_old_is_462_and_leaves_file_alone(mcp, tmp_path):
    """Exactly-one-match is the safety contract: with two candidate sites the
    tool must refuse rather than guess, and the file must be untouched."""
    f = tmp_path / "f.txt"
    f.write_text("dup X dup")
    ok, out = _forge_tool(mcp, "edit_file",
                          len8(str(f), "dup", "y"), _edit_grants(tmp_path))
    assert not ok and "462" in out
    assert f.read_text() == "dup X dup"


def test_edit_file_overlapping_old_counts_correctly(mcp, tmp_path):
    """'aa' occurs twice in 'aaa' (offsets 0 and 1) — the per-offset counter
    must see both and refuse, not slide past the overlap and edit."""
    f = tmp_path / "f.txt"
    f.write_text("aaa")
    ok, out = _forge_tool(mcp, "edit_file",
                          len8(str(f), "aa", "b"), _edit_grants(tmp_path))
    assert not ok and "462" in out
    assert f.read_text() == "aaa"


def test_edit_file_missing_file_is_404(mcp, tmp_path):
    ok, out = _forge_tool(mcp, "edit_file",
                          len8(str(tmp_path / "nope.txt"), "a", "b"),
                          _edit_grants(tmp_path))
    assert not ok and "404" in out


def test_edit_file_empty_old_is_400(mcp, tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("alpha")
    ok, out = _forge_tool(mcp, "edit_file",
                          len8(str(f), "", "x"), _edit_grants(tmp_path))
    assert not ok and "400" in out


def test_edit_file_malformed_framing_is_400(mcp, tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("alpha")
    grants = _edit_grants(tmp_path)
    for bad in ["nonsense",                          # no frames at all
                len8(str(f), "a"),                   # only two frames
                len8(str(f), "a", "b") + "junk",     # trailing garbage
                "0000000x" + "y"]:                   # non-digit in the length
        ok, out = _forge_tool(mcp, "edit_file", bad, grants)
        assert not ok and "400" in out, f"{bad!r}: {out}"


@settings(max_examples=40, deadline=None)
@given(st.text(alphabet="ab\n|é", max_size=30),
       st.text(alphabet="ab\n|é", min_size=1, max_size=6),
       st.text(alphabet="ab\n|é", max_size=6))
def test_edit_file_property_matches_python_semantics(mcp, tmp_path_factory,
                                                     body, old, new):
    """Differential: forge-edit agrees with Python's count/replace on every
    body×old×new — exactly-one → replaced, zero → 461 untouched, many → 462
    untouched. Alphabet includes pipes, newlines, and a multi-byte char."""
    root = tmp_path_factory.mktemp("ef")
    f = root / "f.txt"
    f.write_text(body)
    n = _count_overlapping(body, old)
    ok, out = _forge_tool(mcp, "edit_file", len8(str(f), old, new),
                          _edit_grants(root))
    if n == 1:
        assert ok and out == "ok"
        i = body.index(old)
        assert f.read_text() == body[:i] + new + body[i + len(old):]
    else:
        assert not ok
        assert ("461" if n == 0 else "462") in out
        assert f.read_text() == body


def _count_overlapping(body: str, old: str) -> int:
    """Occurrences at every BYTE offset (the guest counts byte-wise;
    str.count skips overlaps, so it is the wrong reference)."""
    b, o = body.encode(), old.encode()
    return sum(1 for i in range(len(b) - len(o) + 1) if b[i:i + len(o)] == o)


# ── the trio through the dispatch loop (grants + framing end-to-end) ──────


def test_edit_file_dispatch(scripted_llm, tmp_path, mcp):
    agent = make_agent(scripted_llm, tmp_path, mcp)
    sb = agent.sandbox_for("s1")
    (sb / "cfg.ini").write_text("mode=fast\nlevel=3\n")
    scripted_llm.script = [
        msg([tool_use("t", "edit_file", {"path": "cfg.ini", "old": "level=3",
                                         "new": "level=9"})]),
        msg([text("edited")]),
    ]
    assert agent.turn("s1", "bump the level") == "edited"
    assert (sb / "cfg.ini").read_text() == "mode=fast\nlevel=9\n"
    r = _last_tool_result(scripted_llm)
    assert r["content"] == "ok" and "is_error" not in r
    [(name, grants)] = agent.grant_log
    assert name == "edit_file"
    assert grants == {"fs": [str(sb)], "fs_write": [str(sb)]}


def test_list_tree_dispatch(scripted_llm, tmp_path, mcp):
    agent = make_agent(scripted_llm, tmp_path, mcp)
    sb = agent.sandbox_for("s1")
    _seed(sb, {"notes": {"a.md": "x"}, "top.txt": "y"})
    scripted_llm.script = [
        msg([tool_use("t", "list_tree", {"path": "."})]),
        msg([text("surveyed")]),
    ]
    assert agent.turn("s1", "what do I have") == "surveyed"
    r = _last_tool_result(scripted_llm)
    assert r["content"] == "notes/\nnotes/a.md\ntop.txt"
    [(name, grants)] = agent.grant_log
    assert name == "list_tree" and grants == {"fs": [str(sb)]}


def test_grep_tree_dispatch(scripted_llm, tmp_path, mcp):
    agent = make_agent(scripted_llm, tmp_path, mcp)
    sb = agent.sandbox_for("s1")
    _seed(sb, {"a.txt": "hit here\nmiss\n", "sub": {"b.txt": "another hit\n"}})
    scripted_llm.script = [
        msg([tool_use("t", "grep_tree", {"path": ".", "pattern": "hit"})]),
        msg([text("found")]),
    ]
    assert agent.turn("s1", "find hits") == "found"
    r = _last_tool_result(scripted_llm)
    assert r["content"] == "a.txt:1: hit here\nsub/b.txt:1: another hit"
    [(name, grants)] = agent.grant_log
    assert name == "grep_tree" and grants == {"fs": [str(sb)]}


def test_tree_walk_does_not_follow_symlinks_out_of_the_sandbox(mcp, tmp_path):
    """A symlink is the second way out of a sandbox (the first being `..`),
    and it is the one a recursive walker would follow by construction. The
    runtime refuses the traversal itself (-403), so the guarantee does not
    depend on the walker being careful — pinned here because the trio is the
    first code that walks INTO subdirectories at all."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "loot.txt").write_text("OUTSIDE-SECRET")
    sb = tmp_path / "sb"
    sb.mkdir()
    (sb / "normal.txt").write_text("fine")
    (sb / "escape").symlink_to(outside)

    ok, out = _forge_tool(mcp, "list_tree", str(sb), {"fs": [str(sb)]})
    assert not ok and "403" in out, f"symlink escape was walked: {out!r}"
    ok, out = _forge_tool(mcp, "grep_tree", f"{sb}|SECRET", {"fs": [str(sb)]})
    assert not ok and "403" in out, f"symlink escape was searched: {out!r}"


def test_trio_denies_path_traversal(scripted_llm, tmp_path, mcp):
    """Same sandbox contract as every fs tool: `..` resolves outside and the
    grant denies it."""
    agent = make_agent(scripted_llm, tmp_path, mcp)
    (tmp_path / "sandboxes" / "victim.txt").write_text("secret")
    for i, (tool, inp) in enumerate([
        ("edit_file", {"path": "../victim.txt", "old": "secret", "new": "x"}),
        ("list_tree", {"path": ".."}),
        ("grep_tree", {"path": "..", "pattern": "secret"}),
    ]):
        scripted_llm.requests.clear()
        scripted_llm.script = [msg([tool_use("t", tool, inp)]), msg([text("blocked")])]
        assert agent.turn(f"trio-trav{i}", "climb") == "blocked"
        r = _last_tool_result(scripted_llm)
        assert r["is_error"] is True, f"{tool} traversal was not denied"
    assert (tmp_path / "sandboxes" / "victim.txt").read_text() == "secret"
