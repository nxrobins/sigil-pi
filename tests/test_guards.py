"""CI/lint guards — pin down the bug classes found during M2 so they
cannot silently return.

Guard classes:
1. stale generated artifact (chat_turn.sigil out of sync with fragments)
2. the call-expr method-receiver parse bug (`hi(x).as_i32()` — P001)
3. cross-ring json calls from the outer-ring tool (R004)
4. provenance discipline (AUTHORSHIP headers on hand-touched SIGIL)
5. raw kv error codes leaking to clients as misleading 404s
"""
import re
import sys
from pathlib import Path

PI_ROOT = Path(__file__).resolve().parent.parent
TOOLS = PI_ROOT / "tools"
sys.path.insert(0, str(PI_ROOT))


def test_generated_chat_turn_is_in_sync():
    from make_chat_turn import generate
    committed = (TOOLS / "chat_turn.sigil").read_text()
    assert committed == generate(), (
        "tools/chat_turn.sigil is stale — run python3 make_chat_turn.py")


def test_no_method_calls_on_call_expressions():
    """The grammar rejects `f(x).as_i32()` (P001); receivers must be simple
    variables. Catch it at lint time, not compile time."""
    bad = re.compile(r"\)\s*\.\s*as_i(32|64)\s*\(")
    for f in TOOLS.glob("frag_*.sigil"):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            code = line.split("//", 1)[0]  # lint code, not comments
            assert not bad.search(code), f"{f.name}:{i}: method call on call-expr"


def test_no_cross_ring_json_calls():
    """`json` stdlib is inner-ring; the outer-ring chat tool cannot call it
    (R004). The scanner helpers exist precisely to avoid this."""
    for f in TOOLS.glob("frag_*.sigil"):
        assert "json::" not in f.read_text(), f"{f.name}: cross-ring json:: call"


def test_authorship_headers_present():
    """Every non-generated SIGIL source carries provenance."""
    for f in TOOLS.glob("*.sigil"):
        if f.read_text().startswith("// GENERATED"):
            continue
        if f.name == "turn0.sigil":  # M1a, predates the header convention
            continue
        assert "AUTHORSHIP" in f.read_text(), f"{f.name}: no AUTHORSHIP header"


def test_generated_banner_present():
    text = (TOOLS / "chat_turn.sigil").read_text()
    assert text.startswith("// GENERATED"), "chat_turn.sigil lost its GENERATED banner"


def test_no_tool_ships_a_literal_key_in_headers():
    """M5a discipline: no committed SIGIL tool may build a header blob with
    a real 'x-api-key: <value>' — the key must always be a {{secret:...}}
    placeholder the host injects. Catches a regression to in-guest keys."""
    for f in TOOLS.glob("*.sigil"):
        text = f.read_text()
        if "post_secret" not in text and "x-api-key" not in text:
            continue
        # any x-api-key construction must be via the placeholder, never a
        # literal value baked into the source.
        assert not re.search(r"x-api-key:\s*sk-", text), \
            f"{f.name}: literal api key in source"


def test_m7_session_concurrency_and_isolation_invariants():
    """Pin the M7 host bug classes:
    - turns serialize per session (the lost-update race fix),
    - session ids are HASHED into both the kv filename and the sandbox dir
      (no raw session id in a filesystem path -> no traversal),
    - the loop persists state in a `finally` (durable even on error)."""
    src = (PI_ROOT / "agent.py").read_text()
    assert "_session_lock" in src and "with self._session_lock(" in src, \
        "turns must serialize per session (concurrency race guard)"

    def _body(name):
        # crude but sufficient: the function's source up to the next `def ` /
        # `class ` at the same-or-lower indent.
        m = re.search(rf"\n    def {name}\(.*?\n(.*?)\n    (?:def |class )", src, re.S)
        return m.group(1) if m else ""

    # both the kv filename and the sandbox dir must derive from a HASH of the
    # session id (no raw session id in a filesystem path -> no traversal).
    assert "sha256(session_id" in _body("_path"), \
        "SessionStore._path must hash the session id into the kv filename"
    assert "sha256(session_id" in _body("sandbox_for"), \
        "sandbox_for must hash the session id into the sandbox dir name"
    # the loop persists state in a finally (durable even on error/step-cap).
    # `(?:\s*#.*\n)*` — any amount of comment, but save must be the first
    # STATEMENT in the block; pinning the comment shape made this brittle.
    assert re.search(r"finally:\s*\n(?:\s*#.*\n)*\s*self\.store\.save", src), \
        "the loop must persist state in a finally (no silent state loss)"


def test_fs_tools_declare_path_args():
    """Any dispatched tool granted fs/fs_write must declare `path_args` so the
    host resolves its paths against the session sandbox — otherwise a tool
    could take an unsandboxed path. Pins the sandbox-resolution invariant."""
    import json
    manifest = json.loads((TOOLS / "manifest.json").read_text())
    for name, entry in manifest.items():
        grants = entry.get("grants", {})
        if "fs" in grants or "fs_write" in grants:
            assert entry.get("path_args"), \
                f"{name}: fs tool must declare path_args (host-resolved sandbox paths)"


def test_manifest_schema_and_minimality():
    """Every manifest entry is complete, its source exists, and the tool
    source uses ONLY capability families its manifest grants — a read_file
    that quietly gains `use sigil::http;` must fail here, not in review."""
    import json
    manifest = json.loads((TOOLS / "manifest.json").read_text())
    grant_to_markers = {
        "fs": ["fs_read"], "fs_write": ["fs_write"],
        "net": ["http_get", "http_post", "sigil::http"],
        "kv": ["kv_get", "sigil::kv"], "kv_write": ["kv_put", "kv_delete"],
    }
    all_markers = sorted({m for ms in grant_to_markers.values() for m in ms})
    for name, entry in manifest.items():
        for field in ["source", "args", "grants", "spec"]:
            assert field in entry, f"{name}: manifest missing `{field}`"
        assert entry["spec"]["name"] == name
        src_path = PI_ROOT / entry["source"]
        assert src_path.exists(), f"{name}: source {entry['source']} missing"
        src = src_path.read_text()
        allowed = {m for g in entry["grants"] for m in grant_to_markers[g]}
        for marker in all_markers:
            if marker in src and marker not in allowed:
                raise AssertionError(
                    f"{name}: uses `{marker}` but manifest grants only "
                    f"{sorted(entry['grants'])}")


def _code_of(path):
    """Source with `//` comments stripped — check CODE, not prose about the
    old design."""
    return "\n".join(line.split("//", 1)[0]
                     for line in (TOOLS / path).read_text().splitlines())


def test_secret_channel_discipline():
    """M5a invariant: the api key is NEVER in the guest. The LLM-call tool
    sends a header TEMPLATE (with a {{secret:NAME}} placeholder) through
    http::post_secret and the host injects the key. Catch any regression to
    an in-guest key: a kv/http path that would pull real key bytes into the
    guest, or a plain http::post_hdrs that ships whatever the guest built.

    BOTH LLM-call tools are pinned — the repo has two stacks (see the README
    architecture section) and the discipline has to hold on the one that
    actually ships, not only on the one the milestone was written against:
      frag_main.sigil  -> chat_turn, the M2 sigil-serve path (proof carrier)
      agent_turn.sigil -> forged by agent.py for EVERY LLM call (deployed)
    """
    for tool, path in [("chat_turn", "frag_main.sigil"),
                       ("agent_turn", "agent_turn.sigil")]:
        code = _code_of(path)
        assert "http::post_secret" in code, \
            f"{tool} must send headers via post_secret (host-injected secret)"
        assert "post_hdrs" not in code, \
            f"{tool} must NOT use post_hdrs — that ships guest-built headers"
        # the key isn't in the guest, so there is nothing to taint-track here
        assert "@Secret" not in code, \
            f"no @Secret in {tool} code — the key never enters the guest (M5a)"

    # chat_turn reads cfg/sess from kv, but only through the stdlib — a raw
    # kv_get extern was the M4 @Secret channel and must not come back.
    assert "kv_get" not in _code_of("frag_main.sigil"), \
        "chat_turn must read cfg via the ordinary kv stdlib, not a raw kv_get extern"
    # agent_turn holds NO kv grant at all: agent.py passes url|hdrs|body on
    # stdin, so any kv use here is a capability the manifest doesn't grant.
    assert "kv" not in _code_of("agent_turn.sigil"), \
        "agent_turn must stay kv-free — the host passes url|hdrs|body on stdin"


def test_infra_kv_errors_are_remapped_to_500():
    """Raw kv error propagation (the misleading-404 bug class): every kv::get
    /kv::put failure path in the orchestrator must remap to -500, except the
    intentional fresh-session -404 branch."""
    main = (TOOLS / "frag_main.sigil").read_text()
    # the cfg reader remaps internally; the two direct kv call sites must
    # never `return r_hist;` / `return pr;` style-propagate raw codes.
    assert not re.search(r"return\s+r_hist\s*;", main)
    assert not re.search(r"return\s+pr\s*;", main)


# ── 6. taint-label discipline in sigil-pi's OWN code ────────────────────

STDLIB_MODULES = ("json::", "kv::", "http::")


def _own_taint_errors(mcp, source: str, input_text: str, grants=None):
    """Forge `source` and return only the T001s attributable to sigil-pi.

    A T001 naming a stdlib function parameter (`json::array_len` etc.) is the
    tracked UPSTREAM issue — the stdlib declares its parameters with no taint
    annotation, so they default to @Public and cannot accept the @Internal
    network data they exist to process. Everything else is ours.
    """
    r = mcp.forge(source, input=input_text, fuel=1000, grants=grants)
    own = []
    for d in (r.get("diagnostics") or []):
        if not (d.get("code") or "").startswith("T0"):
            continue
        msg = d.get("message", "")
        if "function `" in msg:
            fn = msg.split("function `")[1].split("`")[0]
            if fn.startswith(STDLIB_MODULES):
                continue  # upstream, tracked in SIGIL_REV
        own.append(msg)
    return own


def test_our_own_code_has_no_taint_downgrades(mcp):
    """Every helper sigil-pi writes must declare the labels it actually handles.

    An unannotated parameter or binding defaults to @Public. These tools walk
    HTTP and kv payloads, which are @Internal by the network-data contract, so
    an unannotated helper is a downgrade the checker rejects — 188 T001s across
    chat_turn and parse_reply when the checker tightened on 2026-07-31.

    Annotating is free at the call sites (@Public still flows into @Internal —
    that direction is an upgrade) and it states what the code genuinely does.
    This guard keeps a new unannotated helper from silently reintroducing the
    class. It deliberately TOLERATES stdlib-parameter T001s, which are the
    upstream `json::`/`kv::`/`http::` issue recorded in SIGIL_REV; tighten this
    to allow none once that lands.
    """
    from sigil_bench.compose import compose_with_stdlib
    from conftest import SIGIL_ROOT

    chat_turn = (TOOLS / "chat_turn.sigil").read_text()
    own = _own_taint_errors(mcp, chat_turn, "s|m", {"net": ["127.0.0.1"]})
    assert not own, "chat_turn has taint errors in sigil-pi's own code:\n  " + \
        "\n  ".join(own[:5])

    parse_reply = compose_with_stdlib(
        (TOOLS / "parse_reply.sigil").read_text(), ["json"], SIGIL_ROOT).text
    own = _own_taint_errors(mcp, parse_reply, '{"content":[]}')
    assert not own, "parse_reply has taint errors in sigil-pi's own code:\n  " + \
        "\n  ".join(own[:5])


def test_parse_helpers_prelude_matches_the_tool():
    """frag_parse_helpers.sigil is the authoring prelude for parse_reply.sigil
    and duplicates its helpers. Nothing regenerates one from the other, so they
    drift silently — and a prelude that disagrees with the shipped tool teaches
    the next author the wrong signature."""
    prelude = _code_of("frag_parse_helpers.sigil")
    tool = _code_of("parse_reply.sigil")
    for fn in ("render_len8", "emit_frame", "bytes_eq"):
        a = re.search(rf"fn {fn}\(.*?\)", prelude, re.S)
        b = re.search(rf"fn {fn}\(.*?\)", tool, re.S)
        assert a and b, f"{fn} missing from prelude or tool"
        assert a.group(0) == b.group(0), (
            f"{fn} signature drifted between the prelude and the tool:\n"
            f"  frag_parse_helpers.sigil: {a.group(0)}\n"
            f"  parse_reply.sigil:        {b.group(0)}")
