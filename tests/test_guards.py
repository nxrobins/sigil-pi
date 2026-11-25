"""CI/lint guards — pin down the bug classes found during M2 so they
cannot silently return.

Guard classes:
1. stale generated artifact (chat_turn.sigil out of sync with fragments)
2. the call-expr method-receiver parse bug (`hi(x).as_i32()` — P001)
3. cross-ring json calls from the outer-ring tool (R004)
4. provenance discipline (AUTHORSHIP headers on hand-touched SIGIL)
5. raw kv error codes leaking to clients as misleading 404s
"""
import ast
import inspect
import re
import sys
import textwrap
import tomllib
from pathlib import Path

PI_ROOT = Path(__file__).resolve().parent.parent
TOOLS = PI_ROOT / "tools"
sys.path.insert(0, str(PI_ROOT))


def test_generated_chat_turn_is_in_sync():
    # Genuinely needs the toolchain: regenerating chat_turn.sigil inlines the
    # pinned stdlib, so there is nothing to compare against without one.
    from conftest import needs_toolchain
    needs_toolchain()
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


# ── what a tool's source spells ─────────────────────────────────────────
#
# The tools spell nearly every string as byte stores, one
# `store8(<address>, <byte>)` per byte (102 of them in gh_fetch.sigil), so a
# credential written the way the tools write everything else never appears in
# the source text a scan of the text reads (audit 2026-11-10, F-17).


# A store of one literal byte: `store8(<address>, <byte>)`, where the address
# is a base and an optional literal offset (`a`, `a + 3`, `out + o + 3`).
_LITERAL_BYTE_STORE = re.compile(
    r"store8\(\s*(?P<address>[^,;()]+?)\s*,\s*(?P<byte>0[xX][0-9a-fA-F]+|\d+)\s*\)")
_ADDRESS = re.compile(r"(?P<base>.+?)(?:\s*\+\s*(?P<offset>\d+))?")
# The looser count the decoder is held to: every store whose byte is a literal.
_ANY_LITERAL_STORE = re.compile(r"store8\([^;]*?,\s*(?:0[xX][0-9a-fA-F]+|\d+)\s*\)\s*;")


def _byte_store_strings(text):
    """The strings a tool's byte stores write, in source order. Consecutive
    stores to one base form one string, by offset; a new base, or an offset
    the string already has, starts the next one (`out + o` is reused after
    the cursor moves)."""
    strings, base, run = [], None, {}

    def flush():
        if run:
            strings.append("".join(chr(run.get(i, 0)) for i in range(max(run) + 1)))

    for store in _LITERAL_BYTE_STORE.finditer(text):
        address = _ADDRESS.fullmatch(store.group("address").strip())
        here = " ".join(address.group("base").split())
        offset = int(address.group("offset") or 0)
        if here != base or offset in run:
            flush()
            base, run = here, {}
        run[offset] = int(store.group("byte"), 0) & 0xFF
    flush()
    return strings


def _tool_strings(text):
    """What a tool source spells: its own text, the strings its byte stores
    write, and those strings joined in the order they are written."""
    written = _byte_store_strings(text)
    return [text, *written, "".join(written)]


def test_the_byte_store_decoder_reads_what_the_tools_spell():
    """Anti-vacuity for the two scans above: the decoder reads every store
    of a literal byte in every tool, whatever its address form, and gives
    back strings the tools are known to write."""
    decoded = 0
    for f in TOOLS.glob("*.sigil"):
        text = f.read_text()
        literal = len(_ANY_LITERAL_STORE.findall(text))
        assert len(_LITERAL_BYTE_STORE.findall(text)) == literal, (
            f"{f.name}: a byte store the decoder does not read")
        decoded += literal
    assert decoded >= 700, decoded
    assert "ok" in _byte_store_strings((TOOLS / "write_file.sigil").read_text())
    assert any('{"query":"query{repository(owner:' in spelled
               for spelled in _byte_store_strings((TOOLS / "gh_fetch.sigil").read_text()))


def test_no_tool_ships_a_literal_key_in_headers():
    """M5a discipline: no committed SIGIL tool may build a header blob with
    a real 'x-api-key: <value>' — the key must always be a {{secret:...}}
    placeholder the host injects. Catches a regression to in-guest keys.
    Read in what the tool spells, byte stores decoded: the header names are
    written byte by byte."""
    for f in TOOLS.glob("*.sigil"):
        for spelled in _tool_strings(f.read_text()):
            # any x-api-key construction must be via the placeholder, never
            # a literal value baked into the source.
            assert not re.search(r"x-api-key:\s*sk-", spelled), \
                f"{f.name}: literal api key in source"


# Credential shapes that must never be committed anywhere in the tool sources,
# in ANY form — a literal in a header, a "helpful" default, a pasted example.
# Each entry is a real provider prefix, so a match is a leak, not a false alarm.
CREDENTIAL_PREFIXES = ("sk-ant-", "sk-", "ghp_", "gho_", "ghu_", "ghs_",
                       "github_pat_", "glpat-", "xoxb-", "xoxp-", "AKIA")


def _credentials_in(text):
    """Every credential prefix in what a tool source spells."""
    return sorted({prefix for spelled in _tool_strings(text)
                   for prefix in CREDENTIAL_PREFIXES if prefix in spelled})


def test_no_tool_source_contains_anything_credential_shaped():
    """Generalized from the api-key guard: the repo now injects a SECOND
    secret (the GitHub token), and the next tool will bring a third. Rather
    than add a rule per provider after the fact, scan every tool source for
    every known credential prefix. A tool needing a secret has exactly one
    legitimate way to name it — the {{secret:NAME}} placeholder — so a real
    credential in a source is always a bug. The scan reads what the tool
    spells, its byte stores decoded, not only its text."""
    for f in TOOLS.glob("*.sigil"):
        for prefix in _credentials_in(f.read_text()):
            raise AssertionError(
                f"{f.name}: contains {prefix!r} — credential-shaped. Secrets "
                f"reach a tool ONLY as a {{{{secret:NAME}}}} placeholder the "
                f"host substitutes (M5a); nothing real belongs in the source.")


def _spelled_in_bytes(name, value, *, base="k", cursor=None):
    """`value` written the way the tools write strings: one store per byte,
    at `base`, `base + 1`, ... (or `base + cursor + n`)."""
    at = base if cursor is None else f"{base} + {cursor}"
    lines = [f"    let {base}: i64 @Internal = alloc({len(value) + 1});"]
    for i, byte in enumerate(value.encode()):
        lines.append(f"    store8({at}, {byte});" if i == 0
                     else f"    store8({at} + {i}, {byte});")
    return f"// {name}\n" + "\n".join(lines) + "\n"


def test_the_credential_scan_fires_on_a_credential_spelled_in_byte_stores():
    """The fixture that proves the scan can fire. Each credential is spelled
    the way the tools spell strings, in the three address forms they use
    (`k`, `k + n` and `out + o + n`), or split across two stores; none of it
    appears in the text. The scan must report every one."""
    planted = {
        "sk-ant-": _spelled_in_bytes("api key", "x-api-key: sk-ant-api03-0123456789"),
        "ghp_": _spelled_in_bytes("token", "Authorization: Bearer ghp_0123456789",
                                  base="out", cursor="o"),
        "AKIA": (_spelled_in_bytes("first half", "AK", base="a")
                 + _spelled_in_bytes("second half", "IAEXAMPLE0123", base="b")),
        "glpat-": "// a literal, which the text scan always caught: glpat-0123\n",
    }
    for prefix, source in planted.items():
        if prefix != "glpat-":
            assert prefix not in source, "the fixture must not spell it in text"
        assert prefix in _credentials_in(source), (
            f"the scan did not fire on {prefix!r} spelled as byte stores:\n{source}")
    assert re.search(r"x-api-key:\s*sk-", "\n".join(_tool_strings(planted["sk-ant-"])))
    assert _credentials_in("store8(k, 115); store8(k + 1, 107); store8(k + 2, 105);") == []


def test_every_authenticated_tool_uses_the_host_injection_path():
    """The M5a discipline as a CLASS rule rather than a per-tool one: any
    committed tool that builds an Authorization/x-api-key header must go
    through post_secret with a {{secret:...}} placeholder, and must NOT use
    post_hdrs — which ships whatever the guest assembled, putting the
    credential back in guest memory. Pins every future authenticated tool,
    not just the two that exist."""
    for f in TOOLS.glob("*.sigil"):
        problems = _host_injection_problems(f.name, f.read_text())
        assert not problems, problems


_AUTH_HEADER = re.compile(r"(authorization|x-api-key)", re.I)


def _host_injection_problems(name, text):
    """What the M5a class rule above finds wrong with one tool source."""
    # Skip GENERATED files: chat_turn.sigil inlines the whole stdlib http
    # module at generation time, so it CONTAINS the `http_post_hdrs`
    # declaration without ever calling it. Its authored source is
    # frag_main.sigil, which this glob checks directly — the same split
    # test_secret_channel_discipline already relies on.
    if text.startswith("// GENERATED"):
        return []
    code = "\n".join(line.split("//", 1)[0] for line in text.splitlines())
    # header names are built byte-by-byte, so look at what the byte stores
    # spell as well as the text and its PROSE header, where every tool so far
    # documents the headers it sends.
    if not any(_AUTH_HEADER.search(spelled) for spelled in _tool_strings(text)):
        return []
    if "http_get" in code and "post" not in code:
        return []  # a pure GET tool merely mentioning auth in a comment
    problems = []
    if "post_secret" not in code:
        problems.append(
            f"{name}: builds an auth header but does not use post_secret — "
            f"the host must inject the credential, not the guest")
    if "{{secret:" not in text:
        problems.append(
            f"{name}: uses post_secret but names no {{{{secret:NAME}}}} "
            f"placeholder — there is nothing for the host to substitute")
    if "post_hdrs" in code:
        problems.append(
            f"{name}: uses post_hdrs — that ships guest-built headers, "
            f"putting the credential in guest memory (M5a regression)")
    return problems


def test_the_host_injection_rule_sees_a_header_built_in_byte_stores():
    """The rule above decided whether a tool builds an auth header from the
    tool's text, trusting every tool to name its headers in prose. A tool
    that spells `Authorization: Bearer ` in byte stores, says nothing about
    it, and sends it with post_hdrs, was skipped (found in the sweep for
    audit 2026-11-10, F-17)."""
    tool = (
        "#[ring(outer)] #[trusted] module tool;\n"
        + _spelled_in_bytes("headers", "Authorization: Bearer ", base="h")
        + "    let raw: i64 @Internal = http_post_hdrs(url, url_len, h, 22, b, b_len);\n")
    assert not _AUTH_HEADER.search(tool), "the fixture must not name the header in text"
    problems = "\n".join(_host_injection_problems("byte_built.sigil", tool))
    assert "does not use post_secret" in problems, problems
    assert "uses post_hdrs" in problems, problems


def test_m7_session_concurrency_and_isolation_invariants():
    """Pin the M7 host bug classes:
    - turns serialize per session (the lost-update race fix),
    - session ids are HASHED into the kv filename, the audit filename and
      the sandbox dir (no raw session id in a filesystem path -> no
      traversal),
    - the loop persists state in a `finally` (durable even on error).
    Functions are read by qualified name (`_method_source`)."""
    src = (PI_ROOT / "agent.py").read_text()

    turn_with_usage = _method_source(src, "PiAgent.turn_with_usage")
    assert all(marker in turn_with_usage for marker in (
            "self._session_lock(session_id)", "lock.acquire(", "lock.release()")), \
        "turns must acquire/release their per-session lock (concurrency race guard)"

    # the kv filename, the audit filename and the sandbox dir must all derive
    # from a HASH of the session id (no raw session id in a filesystem path
    # -> no traversal). Each is read from the function its message names.
    assert not _unhashed_session_paths(src), _unhashed_session_paths(src)
    # the loop persists state in a finally (durable even on error/step-cap).
    # `(?:\s*#.*\n)*` — any amount of comment, but save must be the first
    # STATEMENT in the block; pinning the comment shape made this brittle.
    assert re.search(r"finally:\s*\n(?:\s*#.*\n)*\s*self\.store\.save", src), \
        "the loop must persist state in a finally (no silent state loss)"


def _method_source(src, qualified):
    """The source of the function `qualified` names in `src` (`Class.method`
    or a module-level `function`), found by that name and nothing else. A
    lookup by the bare name returned the first function of that name in the
    file, so a guard naming SessionStore._path read AuditLog._path."""
    *owners, name = qualified.split(".")
    scope = ast.parse(src).body
    for owner in owners:
        found = [node for node in scope
                 if isinstance(node, ast.ClassDef) and node.name == owner]
        assert len(found) == 1, f"{qualified}: {len(found)} classes named {owner}"
        scope = found[0].body
    found = [node for node in scope
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name == name]
    assert len(found) == 1, f"{qualified}: {len(found)} definitions"
    return textwrap.dedent(ast.get_source_segment(src, found[0], padded=True))


# Each filesystem path agent.py builds from a session id: the function that
# builds it, by qualified name, and what the guard says when it does not hash.
HASHED_SESSION_PATHS = {
    "SessionStore._path": "SessionStore._path must hash the session id into the kv filename",
    "AuditLog._path": "AuditLog._path must hash the session id into the audit filename",
    "PiAgent.sandbox_for": "sandbox_for must hash the session id into the sandbox dir name",
}


def _unhashed_session_paths(src):
    return [message for qualified, message in HASHED_SESSION_PATHS.items()
            if "sha256(session_id" not in _method_source(src, qualified)]


def test_the_session_path_guard_reads_the_function_it_names():
    """The guard's message named SessionStore._path, and it read the first
    `def _path` in agent.py, which is AuditLog._path (audit 2026-11-10,
    F-17). A copy of agent.py whose SessionStore._path uses the raw session
    id, with AuditLog._path, defined first, still hashing, must fail it."""
    src = (PI_ROOT / "agent.py").read_text()
    hashed = 'self.dir / (hashlib.sha256(session_id.encode()).hexdigest() + ".kv")'
    assert src.count(hashed) == 1, "SessionStore._path changed; update this fixture"
    raw = src.replace(hashed, 'self.dir / (session_id + ".kv")')
    assert _unhashed_session_paths(src) == []
    assert _unhashed_session_paths(raw) == [HASHED_SESSION_PATHS["SessionStore._path"]]
    import agent
    for qualified in HASHED_SESSION_PATHS:
        owner, name = qualified.split(".")
        live = textwrap.dedent(inspect.getsource(getattr(getattr(agent, owner), name)))
        assert _method_source(src, qualified).strip() == live.strip(), qualified


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


def test_manifest_spec_coheres_with_the_dispatch_contract():
    """The `spec` block is what the MODEL sees; `args`/`path_args` are what the
    host dispatch actually does. They live inches apart in manifest.json and
    nothing forced them to agree — read_file/write_file shipped specs saying
    'absolute path inside the sandbox' while dispatch resolves paths RELATIVE
    to it, teaching the model to earn -403s. Pin the whole contract:
    every arg is a spec property and required, path_args are args, and every
    path_arg's description says relative-to-the-sandbox (never 'absolute')."""
    import json
    manifest = json.loads((TOOLS / "manifest.json").read_text())
    for name, entry in manifest.items():
        schema = entry["spec"]["input_schema"]
        assert set(schema["properties"]) == set(entry["args"]), \
            f"{name}: spec properties disagree with dispatch args"
        assert set(schema.get("required", [])) == set(entry["args"]), \
            f"{name}: every dispatch arg must be spec-required (dispatch " \
            f"errors on any missing arg, so an optional spec arg is a lie)"
        assert set(entry.get("path_args", [])) <= set(entry["args"]), \
            f"{name}: path_args must be a subset of args"
        assert entry.get("framing") in (None, "len8"), \
            f"{name}: unknown framing {entry.get('framing')!r} — dispatch " \
            f"would silently fall back to pipe-joining and shift every arg"
        for a in entry.get("path_args", []):
            desc = schema["properties"][a].get("description", "")
            assert "relative to the sandbox" in desc, (
                f"{name}.{a}: path args are host-resolved relative to the "
                f"session sandbox; the spec must say so — got {desc!r}")
            assert "absolute" not in desc, (
                f"{name}.{a}: spec says 'absolute' but dispatch resolves "
                f"relative to the sandbox — got {desc!r}")
        # M12 pipeline fields. The shape stage is forged with NO grants —
        # that is the point of the pattern — so an outer-ring or FFI shaper
        # is a config error that would only surface as a runtime trap.
        if "shape" in entry:
            shape_path = PI_ROOT / entry["shape"]
            assert shape_path.exists(), f"{name}: shape {entry['shape']} missing"
            shape_src = shape_path.read_text()
            assert "#[ring(outer)]" not in shape_src, \
                f"{name}: shape stage must be inner-ring (it forges grantless)"
            assert 'extern "C"' not in shape_src, \
                f"{name}: shape stage must not declare externs (no FFI grantless)"
        for b in entry.get("bound_args", []):
            assert isinstance(b, str) and b, \
                f"{name}: bound_args must be non-empty strings, got {b!r}"
            assert "|" not in b, \
                f"{name}: bound_arg {b!r} contains '|' — it would shift the " \
                f"pipe-framed split for every arg after it"


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
        # the secret grant is the host-injection path: a tool may name a
        # {{secret:...}} placeholder only if it goes through post_secret,
        # and only if its manifest actually grants `secret`.
        "secret": ["post_secret"],
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
        # the shape stage holds NO grants, so NO capability marker may appear
        # in it at all — a shaper that grew an http_get must fail here.
        if "shape" in entry:
            shape_src = (PI_ROOT / entry["shape"]).read_text()
            for marker in all_markers:
                assert marker not in shape_src, (
                    f"{name}: shape stage uses `{marker}` but shape forges "
                    f"hold no grants whatsoever")


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


def _taint_errors(mcp, source: str, input_text: str, grants=None):
    """Forge `source` and return its T0xx diagnostics as (ours, stdlib).

    Both must be empty. The split is not a tolerance — it is triage, so a
    failure says WHERE to look: `ours` means a helper in tools/ is missing a
    label, `stdlib` means the toolchain regressed underneath us and SIGIL_REV
    needs attention. Collapsing them into one list would make an upstream
    regression read as our bug.
    """
    r = mcp.forge(source, input=input_text, fuel=1000, grants=grants)
    ours, stdlib = [], []
    for d in (r.get("diagnostics") or []):
        if not (d.get("code") or "").startswith("T0"):
            continue
        msg = d.get("message", "")
        fn = msg.split("function `")[1].split("`")[0] if "function `" in msg else ""
        (stdlib if fn.startswith(STDLIB_MODULES) else ours).append(msg)
    return ours, stdlib


def test_our_own_code_has_no_taint_downgrades(mcp):
    """Every helper sigil-pi writes must declare the labels it actually handles.

    An unannotated parameter or binding defaults to @Public. These tools walk
    HTTP and kv payloads, which are @Internal by the network-data contract, so
    an unannotated helper is a downgrade the checker rejects — 188 T001s across
    chat_turn and parse_reply when the checker tightened on 2026-07-31.

    Annotating is free at the call sites (@Public still flows into @Internal —
    that direction is an upgrade) and it states what the code genuinely does.
    This guard keeps a new unannotated helper from silently reintroducing the
    class.

    ZERO tolerance, both halves. It once tolerated stdlib-parameter T001s while
    the upstream `json::`/`kv::`/`http::` gap was open; that landed on
    2026-07-31 as @Flow taint polymorphism, so tolerating them now would mean
    an upstream regression could reappear and the guard would still pass.
    """
    from conftest import SIGIL_ROOT
    from sigil_compose import compose_with_stdlib

    cases = [
        ("chat_turn", (TOOLS / "chat_turn.sigil").read_text(),
         "s|m", {"net": ["127.0.0.1"]}),
        ("parse_reply", compose_with_stdlib(
            (TOOLS / "parse_reply.sigil").read_text(), ["json"], SIGIL_ROOT).text,
         '{"content":[]}', None),
    ]
    for label, source, input_text, grants in cases:
        ours, stdlib = _taint_errors(mcp, source, input_text, grants)
        assert not ours, (
            f"{label} has taint errors in sigil-pi's OWN code — a helper in "
            f"tools/ is missing a label:\n  " + "\n  ".join(ours[:5]))
        assert not stdlib, (
            f"{label} hits taint errors in the STDLIB — the toolchain regressed "
            f"underneath us; check SIGIL_REV against the pinned trees:\n  "
            + "\n  ".join(stdlib[:5]))


def _sigil_rev_cfg():
    cfg = {}
    for line in (PI_ROOT / "SIGIL_REV").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def test_sigil_rev_ref_is_a_full_length_sha():
    """`ref` is what CI hands to actions/checkout, and that action only treats
    a value as a COMMIT when it is the full 40 hex chars. Anything shorter is
    taken as a branch/tag name: it fetches `refs/heads/<ref>*`, matches
    nothing, retries three times, and fails with `The process '/usr/bin/git'
    failed with exit code 1` — an error naming neither the ref nor the cause.

    The abbreviated `eb9f1715` sat here through five PRs, invisible because
    the forge job had never actually run. Only the first real run found it.
    Pin the format so the next pin bump cannot reintroduce a failure whose
    error message explains nothing."""
    ref = _sigil_rev_cfg().get("ref", "")
    assert ref, "SIGIL_REV has no `ref` key — CI needs one to fetch a candidate"
    assert re.fullmatch(r"[0-9a-f]{40}", ref), (
        f"SIGIL_REV `ref` must be a FULL 40-char commit sha — got {ref!r} "
        f"({len(ref)} chars). actions/checkout reads anything shorter as a "
        f"branch/tag name and fails opaquely.")


def test_runtime_state_dirs_are_gitignored():
    """Every directory the project creates at runtime must be uncommittable.
    .pi-state is the sharp one: it holds SESSION TRANSCRIPTS and per-session
    sandboxes (PI_STATE defaults to the repo root), so an unignored default
    plus one `git add -A` publishes real conversation data."""
    import subprocess
    if not (PI_ROOT / ".git").exists():
        import pytest
        pytest.skip("not a git checkout")
    # .pi-state covers the audit log too (state_dir/audit) — the chain names
    # every tool a session ran, which is operational detail, not source.
    for d in (".pi-state", ".venv", "__pycache__", ".pytest_cache", ".hypothesis"):
        # trailing slash: ask about the DIRECTORY. A dir-only pattern like
        # `.pi-state/` doesn't match a bare query for a path that doesn't
        # exist yet, and this guard must not depend on whether the agent has
        # ever been run in this checkout.
        r = subprocess.run(["git", "-C", str(PI_ROOT), "check-ignore", "-q", d + "/"])
        assert r.returncode == 0, \
            f"{d} is not gitignored — runtime/derived state must never be committable"


def test_every_forge_and_serve_config_declares_the_ephemeral_host():
    """Since SIGIL's CSIR v9 verifier a host operation's occurrence is Public
    unless the host declares a profile, and a tool that makes a host call
    inside a branch on an @Internal value — the previous call's error code,
    the shape of every tool here — is refused (I013) as leaking Internal
    control to an undeclared host. The toolchain bump to the public tree found
    this the first time the gate ran: every forge red, no .sigil changed.

    The declaration lives in exactly two kinds of place — the vendored
    client's forge call and each sigil-serve config — and a new serve-config
    builder that forgets it would fail on its first tool. So the builders are
    enumerated here, and adding one is a deliberate edit to this list."""
    client = (PI_ROOT / "runtime_client.py").read_text()
    assert 'HOST_PROFILE = "ephemeral"' in client, "the profile name lost its one definition"
    assert '"host_profile": HOST_PROFILE' in client, \
        "ProductionSigilMCP.forge must declare the host on every call"
    builders = ["ci.sh", "tests/conftest.py"]
    for name in builders:
        assert '"host_profile": "ephemeral"' in (PI_ROOT / name).read_text(), \
            f"{name} builds a sigil-serve config without declaring the host"
    candidates = [PI_ROOT / "ci.sh", PI_ROOT / "product-ci.sh",
                  *PI_ROOT.glob("*.py"), *(PI_ROOT / "tests").glob("*.py"),
                  *(PI_ROOT / "scripts").glob("*.py")]
    with_routes = sorted(
        path.relative_to(PI_ROOT).as_posix() for path in candidates
        if path.is_file() and path.name != "test_guards.py"
        and '"rou' 'tes"' in path.read_text(errors="ignore"))
    assert with_routes == builders, (
        f"sigil-serve config builders are {with_routes}; each must declare the "
        f"host profile and be listed here deliberately")


def test_ci_provisions_the_lean_toolchain_before_building_the_compiler():
    """The public SIGIL compiler statically links a Lean-built kernel:
    sigil-formal-bridge's build.rs runs `lake` and panics without it, naming
    nothing useful. The first forge run against the public tree (run
    34015201659) failed exactly there, after every step that had a name had
    passed. So both workflows must install the pinned Lean toolchain — read
    from SIGIL's own pin in the checkout, never copied — BEFORE the gate
    builds, and ci.sh must name a missing lake the way it names a missing
    cargo."""
    for workflow, gate_step in (("ci.yml", "- name: ./ci.sh"),
                                ("release.yml", "- name: Run the mandatory product gate")):
        text = (PI_ROOT / ".github" / "workflows" / workflow).read_text()
        assert "SIGIL/proofs/lean/lean-toolchain" in text, \
            f"{workflow} must read the Lean pin from the SIGIL checkout, not carry a copy"
        assert "elan toolchain install" in text, f"{workflow} must install that toolchain"
        assert gate_step in text, f"{workflow} lost its gate step"
        assert text.index("elan toolchain install") < text.index(gate_step), \
            f"{workflow} would build the compiler before Lean is installed"
    # release.yml builds the compiler in two jobs (audit F-16): the gate, through
    # ci.sh, and the build, through build_release.py. Each installs Lean first.
    _, jobs = _release_jobs()
    for job, builds in (("gate", "./ci.sh"), ("build", "scripts/build_release.py")):
        text = jobs.get(job, "")
        assert "SIGIL/proofs/lean/lean-toolchain" in text and "elan toolchain install" in text, \
            f"release.yml's {job} job must install the Lean toolchain SIGIL pins"
        assert builds in text and text.index("elan toolchain install") < text.index(builds), \
            f"release.yml's {job} job would build the compiler before Lean is installed"
    assert "command -v lake" in (PI_ROOT / "ci.sh").read_text(), \
        "ci.sh must name a missing lake up front rather than let build.rs panic"


def test_forge_ci_job_is_mandatory():
    """A product release gate may not turn toolchain unavailability into a
    skipped job. Whatever can go wrong fetching SIGIL must be a red, named
    failure, never a path around the full forge suite.

    SIGIL is public (github.com/nxrobins/sigil, since 2026-09-04), so the
    forge job needs no credential — and must not grow one back: a token would
    mean a private input crept into the release gate. It must also spell the
    repo the way GitHub canonicalises it, because repository names are
    case-insensitive: the capitalised old spelling silently resolved to the
    public repo the day the private one was renamed, at a ref only the
    private one had (found 2026-09-06). Comments may tell that story; the
    directives may not spell it that way."""
    for workflow in ("ci.yml", "release.yml"):
        text = (PI_ROOT / ".github" / "workflows" / workflow).read_text()
        directives = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert "repository: nxrobins/sigil" in directives, \
            f"{workflow} must fetch SIGIL from the public repository"
        assert "nxrobins/SIGIL" not in directives, \
            f"{workflow} spells the repository the ambiguous way (names are case-insensitive)"
        assert "SIGIL_REPO_TOKEN" not in directives, \
            f"{workflow} reintroduces a private-repository credential into the gate"
    text = (PI_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    parts = text.split("\n  forge:", 1)
    assert len(parts) == 2, "ci.yml lost its forge job"
    header = parts[1].split("\n    steps:", 1)[0]  # forge job config, pre-steps
    assert "if:" not in header and "needs:" not in header, (
        "the full forge gate must be unconditional — an unavailable toolchain "
        "must fail CI, not skip a required product check")
    assert "api.github.com/repos/nxrobins/sigil/commits/" in parts[1], \
        "forge must prove the pinned ref is public before checking it out"


def test_python_package_version_matches_release_version():
    metadata = tomllib.loads((PI_ROOT / "pyproject.toml").read_text())
    assert metadata["project"]["version"] == (PI_ROOT / "VERSION").read_text().strip(), (
        "Python package metadata and the release VERSION must identify the same release")


def _release_jobs():
    """release.yml as the text before `jobs:` and each job's own text, in order,
    without `#` comment lines, so prose cannot satisfy a pin."""
    text = (PI_ROOT / ".github" / "workflows" / "release.yml").read_text()
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    head, found, body = code.partition("\njobs:\n")
    assert found, "release.yml lost its jobs"
    parts = re.split(r"(?m)^  ([A-Za-z0-9_-]+):[ \t]*$", body)
    return head, dict(zip(parts[1::2], parts[2::2]))


def test_release_workflow_gates_and_attests_the_exact_bundle():
    """A tag may not publish a source-only or untested release. It must run
    the mandatory gate, bundle the pinned runtime, verify the checksum, and
    sign both provenance and the SBOM assertion.

    Each of those runs in a job of its own (audit 2026-11-10, F-16). One job
    used to hold the write and signing permissions while it installed the
    development packages, ran the tests, built and signed, so anything the
    tests ran could change what was then signed, and nothing checked that the
    tag was on main. Now the gate checks the tag and runs the tests with a
    token that can only read; the build packages a compiler it builds itself
    from a fresh checkout at the pin; and only the publish job, which checks
    out nothing, can write or sign. tests/test_release_workflow.py runs the
    steps that read the tag."""
    head, jobs = _release_jobs()
    assert list(jobs) == ["gate", "build", "publish"], (
        f"release.yml's jobs are {list(jobs)}: the tests, the build and the signing "
        f"must each run in a job of their own")
    gate, build, publish = jobs["gate"], jobs["build"], jobs["publish"]
    assert re.search(r"(?m)^permissions: \{\}$", head), \
        "release.yml must grant nothing by default; each job names its own permissions"
    for permission in ("contents: write", "id-token: write", "attestations: write"):
        assert permission in publish, f"release workflow lost required {permission} permission"
        assert permission not in gate + build, f"only the publish job may hold {permission}"
    for job in (gate, build):
        assert re.search(r"\n    permissions:\n      contents: read\n    \S", job), \
            "the gate and the build may only read"
    assert "\n    needs: gate\n" in build and "\n    needs: [gate, build]\n" in publish, \
        "the build runs only after the gate, and the publish job only after both"
    assert "./ci.sh" in gate, "a release tag must pass the mandatory source gate"
    assert "./ci.sh" not in build + publish
    assert "fetch-depth: 0" in gate and "refs/remotes/origin/main" in gate \
        and "git merge-base --is-ancestor" in gate, "a release tag must be on main"
    assert gate.index("git merge-base --is-ancestor") < gate.index("./ci.sh"), \
        "the gate refuses a tag that is not on main before it tests anything"
    assert "scripts/build_release.py" in build and "--no-build" not in build, \
        "release must bundle a runtime the build job builds itself from pinned source"
    assert "ref: ${{ steps.pin.outputs.ref }}" in build, \
        "the build job must check out SIGIL at the pin"
    for taken in ("rust-cache", "actions/cache", "pip install", "download-artifact"):
        assert taken not in build, f"the build job must start from a fresh checkout: {taken}"
    assert "sha256sum --check --strict" in build and "sha256sum --check --strict" in publish, \
        "release must verify its outer checksum before publication"
    assert "actions/checkout" not in publish, "the job that signs checks out nothing"
    assert publish.count("actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6") == 2, \
        "release requires separate signed provenance and SBOM attestations"
    assert "sbom-path:" in publish and "subject-checksums:" in publish
    assert 'gh release create "$GITHUB_REF_NAME"' in publish, \
        "attested assets must be published on the immutable version tag"
    command = publish.split('gh release create "$GITHUB_REF_NAME"', 1)[1]
    assert "--prerelease" in command and "--latest=false" in command, \
        "the candidate workflow must not publish an alpha as the latest GA release"
    workflows = sorted((PI_ROOT / ".github" / "workflows").glob("*.yml"))
    assert len(workflows) >= 3, "a workflow file went missing"
    for workflow_path in workflows:
        workflow_text = workflow_path.read_text()
        for action in re.findall(r"uses:\s*([^\s#]+)", workflow_text):
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", action), (
                f"{workflow_path.name}: action input is mutable rather than "
                f"commit-pinned: {action}")
    product_gate = PI_ROOT / "product-ci.sh"
    assert product_gate.stat().st_mode & 0o111, "product-ci.sh must be executable"
    product_text = product_gate.read_text()
    assert "./ci.sh" in product_text and "check_readiness_evidence.py" in product_text


def test_ci_rebuilds_the_forge_binaries_at_the_pin():
    """Issue #6: the pin check proves the SOURCE tree matches SIGIL_REV, but a
    leftover binary built from an older tree passes that check and forges with
    different behavior. ci.sh must therefore REBUILD (cargo is incremental — a
    no-op costs ~0.1s when nothing moved) so the binaries are causally built
    from the pinned tree before anything forges through them."""
    src = (PI_ROOT / "ci.sh").read_text()
    build = re.search(r"cargo build --release[^\n]*", src)
    assert build, "ci.sh no longer rebuilds the forge binaries at the pin (issue #6)"
    for pkg in ("-p sigil-mcp", "-p sigil-serve"):
        assert pkg in build.group(0), f"ci.sh rebuild must cover {pkg}"
    # the rebuild must come AFTER the pin check, so what gets built is the
    # tree the pin just proved.
    assert src.index("SIGIL_REV") < src.index("cargo build"), \
        "ci.sh must verify the pin before rebuilding"


def test_readme_test_count_is_current():
    """The README status line claims an exact test count, and this repo's
    credibility rests on its docs being exact — the claim sat at 98 while the
    suite had grown past 130. Collect and compare, so growing the suite
    without touching the README fails here, with the right number in hand.
    The claim reads 'N tests + M honest xfail' where N+M is the collection."""
    import subprocess
    readme = (PI_ROOT / "README.md").read_text()
    m = re.search(r"(\d+) tests \+ (\d+) honest xfail", readme)
    assert m, "README lost its 'N tests + M honest xfail' status claim"
    claimed = int(m.group(1)) + int(m.group(2))
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        capture_output=True, text=True, cwd=PI_ROOT)
    assert out.returncode == 0, f"collection failed:\n{out.stdout}\n{out.stderr}"
    collected = sum(int(n) for n in re.findall(r"^tests/\S+: (\d+)$", out.stdout, re.M))
    assert collected > 0, f"could not parse collection output:\n{out.stdout}"
    assert claimed == collected, (
        f"README claims {m.group(1)} tests + {m.group(2)} xfail = {claimed}, "
        f"but the suite collects {collected} — update the README status line")
    # decorator occurrences only (@-anchored), so this guard's own source —
    # which necessarily names the marker — doesn't count itself
    xfails = sum(len(re.findall(r"^\s*@pytest\.mark\.xfail", p.read_text(), re.M))
                 for p in (PI_ROOT / "tests").glob("test_*.py"))
    assert int(m.group(2)) == xfails, (
        f"README claims {m.group(2)} honest xfail, tests mark {xfails}")


def test_product_contract_states_retry_idempotency_and_state_schema_rules():
    """Release docs must not leave callers guessing whether a lost response
    is safe to replay or operators guessing whether unknown state is mutable."""
    api = (PI_ROOT / "docs" / "api.md").read_text()
    for rule in (
            "does not accept or interpret an `Idempotency-Key`",
            "not replay-safe after an ambiguous transport outcome",
            "A client disconnect does not cancel accepted work",
            "only for transient `429`/5xx failures"):
        assert rule in api, f"API contract lost required rule: {rule}"
    state = (PI_ROOT / "docs" / "state-compatibility.md").read_text()
    for rule in (
            "There is no implicit research-to-v1 migration",
            "state schema unchanged",
            "state migration required",
            "An in-place schema change without an explicit migration"):
        assert rule in state, f"state compatibility contract lost required rule: {rule}"


def test_readme_tools_table_matches_the_manifest():
    """The README table is how a reader learns the toolset, and the manifest
    is what the agent actually offers. Nothing tied them together, so adding
    a tool could leave the table quietly wrong. Names and grants must agree."""
    import json
    manifest = json.loads((TOOLS / "manifest.json").read_text())
    readme = (PI_ROOT / "README.md").read_text()
    section = readme.split("## Tools", 1)[1].split("\n## ", 1)[0]
    rows = dict(re.findall(r"^\| `(\w+)` \| ([^|]+?) \|", section, re.M))
    assert set(rows) == set(manifest), (
        f"README tools table disagrees with the manifest: "
        f"only in README {sorted(set(rows) - set(manifest))}, "
        f"only in manifest {sorted(set(manifest) - set(rows))}")
    for name, entry in manifest.items():
        for grant in entry["grants"]:
            assert grant in rows[name], \
                f"README row for {name} does not mention its `{grant}` grant"


def test_every_tool_source_declares_its_authorship():
    """Provenance is a claim this repo makes in public (v14-authored SIGIL).
    The trio is hand-authored, which is fine — but every tool must SAY which,
    so the README's authorship claim can't quietly become false."""
    import json
    manifest = json.loads((TOOLS / "manifest.json").read_text())
    for name, entry in manifest.items():
        head = (PI_ROOT / entry["source"]).read_text().split("module ", 1)[0]
        assert "AUTHORSHIP:" in head, f"{name}: no AUTHORSHIP header"
        assert re.search(r"AUTHORSHIP:.*?(v14|hand-authored)", head, re.S), \
            f"{name}: AUTHORSHIP must say v14 or hand-authored"


def test_manifest_grant_values_are_lists():
    """Every grant family must map to a LIST of strings. A bare string passes
    every other check and then gets iterated character by character downstream
    — which in the audit redactor turned a credential into one `<char>=` entry
    per byte, preserving the secret in order. Pin the shape at the source."""
    import json
    manifest = json.loads((TOOLS / "manifest.json").read_text())
    for name, entry in manifest.items():
        for kind, values in entry.get("grants", {}).items():
            assert isinstance(values, list), (
                f"{name}: grant {kind!r} is {type(values).__name__}, not a "
                f"list — a bare string is iterated per-character downstream")
            for v in values:
                assert isinstance(v, str) and v, \
                    f"{name}: grant {kind!r} holds a non-string value {v!r}"


def test_secret_grants_use_the_general_form():
    """One mechanism for every provider. A provider-specific token
    (`{GITHUB_TOKEN}`, `{SLACK_TOKEN}`, ...) means a new env var, a new
    constructor parameter and a new branch in the grant resolver per API —
    the calcification `{SECRET:name}` exists to prevent. Every secret grant
    in the manifest must use the general form."""
    import json
    manifest = json.loads((TOOLS / "manifest.json").read_text())
    for name, entry in manifest.items():
        for v in entry.get("grants", {}).get("secret", []):
            assert re.fullmatch(r"\{SECRET:[a-z0-9_]+\}", v), (
                f"{name}: secret grant {v!r} is not the general "
                f"{{SECRET:name}} form")
    # and no provider-specific expansion may live in the resolver
    src = (PI_ROOT / "agent.py").read_text()
    assert not re.search(r"\{[A-Z][A-Z0-9_]*_TOKEN\}", src), \
        "a provider-specific secret token reappeared in the host"


def test_forge_is_the_only_path_to_a_guest():
    """M13's completeness rests entirely on `_forge` being the SOLE caller of
    `self._mcp.forge` — that is what makes 'every guest execution is recorded'
    structural rather than a promise to remember. A second call site would
    silently run an unaudited guest, which is exactly the gap the log exists
    to close. Pin the chokepoint."""
    src = (PI_ROOT / "agent.py").read_text()
    call_sites = re.findall(r"self\._mcp\.forge\(", src)
    assert len(call_sites) == 1, (
        f"{len(call_sites)} call sites reach the runtime directly — every "
        f"guest execution must go through _forge, or it runs unaudited")
    body = re.search(r"\n    def _forge\(.*?\n(.*?)\n    def ", src, re.S)
    assert body and "self._mcp.forge(" in body.group(1), \
        "the single _mcp.forge call must be the one inside _forge"


def test_audit_records_never_carry_a_secret_value():
    """Source-level companion to the runtime canary: the record builder must
    pass grants through redact_grants. A future field that logged raw grants
    would leak the api key to disk on every LLM call."""
    src = (PI_ROOT / "agent.py").read_text()
    body = re.search(r"\n    def append_or_raise\(.*?\n(.*?)\n    def ", src, re.S)
    assert body, "append_or_raise not found — did the audit writer move?"
    assert '"grants": redact_grants(grants)' in body.group(1), \
        "audit entries must store REDACTED grants — raw grants contain the key"


def test_no_retry_or_bounded_loop_can_fall_through():
    """The PI_LLM_RETRIES=-1 bug class: a `for` over a computed range can end
    without returning, so the function falls off and returns None — a config
    typo becoming a silent no-op. Bounded retry loops must be `while True`
    with explicit return/raise on every path."""
    src = (PI_ROOT / "agent.py").read_text()
    body = re.search(r"\n    def _llm\(.*?\n(.*?)\n    def ", src, re.S).group(1)
    assert "while True:" in body, \
        "_llm's retry loop must be `while True` (no fall-through path)"
    assert "for attempt in range" not in body, \
        "a `for ... in range(n)` retry loop returns None when n <= 0"


def test_operator_int_knobs_are_range_checked():
    """Every numeric knob an operator can typo must be validated where it is
    accepted, not discovered as strange behavior downstream."""
    from agent import PiAgent, SessionStore
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        common = dict(store=SessionStore(base / "s"), sandbox_root=base / "b",
                      mcp=None)
        for kwargs, knob in [({"llm_retries": -1}, "PI_LLM_RETRIES"),
                             ({"system_prompt": "x" * (32 * 1024 + 1)},
                              "MAX_SYSTEM_BYTES")]:
            with __import__("pytest").raises(ValueError, match=knob):
                PiAgent("http://127.0.0.1:9/v1/messages", "k", **common, **kwargs)


def test_lint_gate_matches_between_local_and_ci():
    """ci.sh and the standalone CI job must run the SAME lint invocation.
    The rules are pyflakes-level only (F: dead/shadowed imports, undefined
    names; E9: syntax errors) — real-bug classes, zero style opinions. If the
    two gates drift, a local green can fail on GitHub or the reverse, and
    the weaker gate quietly becomes the real one."""
    invocation = "ruff check --select F,E9 ."
    assert invocation in (PI_ROOT / "ci.sh").read_text(), \
        "ci.sh lost the lint step"
    assert invocation in (PI_ROOT / ".github" / "workflows" / "ci.yml").read_text(), \
        "the standalone CI job lost the lint step"


def test_ci_requires_complete_coverage_for_identified_security_boundaries():
    """The global 85% gate cannot hide a missed branch in an explicitly
    critical authentication, isolation, quota, lifecycle, or forge boundary;
    nor, since the audit of 2026-11-10 (F-17) found none of them in it, in the
    server, the dispatch path, the readiness snapshot, the scheduler, or the
    agent's tool dispatch, grant redaction and audit chain, or in the
    validation of release evidence. Every
    ProductScheduler and ProductScheduleStore function is named, so a scheduler
    change cannot leave one short. docs/product-readiness.md names the same
    entries (tests/test_coverage_gate_guards.py holds it to ci.sh)."""
    source = (PI_ROOT / "ci.sh").read_text()
    selected = set(re.findall(r"--critical\s+([^\s\\]+)", source))
    required = {
        "runtime_client.py",
        "sigil_compose.py",
        "product_service.py:AuthRegistry._active",
        "product_service.py:AuthRegistry.authenticate",
        "product_service.py:AuthRegistry.policy",
        "product_service.py:DurableQuotaStore.acquire_turn",
        "product_service.py:_DurableTurnLease.__init__",
        "product_service.py:_DurableTurnLease.token_budget",
        "product_service.py:_DurableTurnLease.release",
        "product_service.py:_TurnTokenBudget.__init__",
        "product_service.py:_TurnTokenBudget.reserve",
        "product_service.py:_TokenAttempt.__init__",
        "product_service.py:_TokenAttempt.settle",
        "product_service.py:DurableQuotaStore.reserve_model_tokens",
        "product_service.py:DurableQuotaStore.reconcile_model_tokens",
        "product_service.py:DurableQuotaStore.settle_turn",
        "product_service.py:DurableQuotaStore.registered_session_inactive",
        "product_service.py:DurableQuotaStore.remove_registered_session",
        "product_service.py:ProductScheduleStore.internal_active",
        "product_service.py:ProductDataManager.verify_quota_registry",
        "product_service.py:ProductDataManager.export",
        "product_service.py:ProductDataManager.delete_internal_files",
        "product_service.py:ProductDataManager.delete",
        "product_service.py:ProductRetentionMonitor.tick",
        "product_service.py:_internal_session",
        "product_service.py:ProductService._allowed_tools",
        "product_service.py:ProductService.is_ready",
        "product_service.py:ProductService._require",
        "product_service.py:ProductService._validate_session",
        "product_service.py:ProductService._validate_message",
        "product_service.py:ProductService._chat_request",
        "product_service.py:validate_transport",
        "product_service.py:serve_product",
        "product_service.py:tls_context",
        "product_service.py:ProductHTTPServer.__init__",
        "product_service.py:ProductHTTPServer.answering",
        "product_service.py:ProductHTTPServer.server_close",
        "product_service.py:ProductHTTPServer.get_request",
        "product_service.py:ProductHTTPServer.shutdown_request",
        "product_service.py:_readable",
        "product_service.py:_RequestReader.readline",
        "product_service.py:_ConnectionInput.__init__",
        "product_service.py:_ConnectionInput.readable",
        "product_service.py:_ConnectionInput.readinto",
        "product_service.py:_declared_length",
        "product_service.py:drain_service",
        "product_service.py:ServiceMetrics.transport_error",
        "product_service.py:ProductService.transport_error",
        "product_service.py:_log_event",
        "product_main.py:_server_tls_context",
        "product_service.py:ProductService.dispatch",
        "product_service.py:ProductService.authenticate",
        "product_service.py:_request_id",
        "product_service.py:DurableQuotaStore.admit_request",
        "product_service.py:FixedWindowRateLimiter.admit",
        "product_service.py:ProductService._json_object",
        "product_service.py:ProductService._in_flight",
        "product_service.py:InFlightWork.enter",
        "product_service.py:InFlightWork.leave",
        "product_service.py:ProductService._turn_enter",
        "product_service.py:ProductService._turn_usage",
        "product_service.py:TenantSecrets.from_directory",
        "product_service.py:TenantSecrets.missing",
        "product_service.py:TenantSecrets.for_principal",
        "product_service.py:_check_private",
        "product_service.py:_read_secret_value",
        "product_service.py:principal_tool_grants",
        "product_service.py:ProductService._tool_grants",
        "agent.py:PiAgent._tool_grants",
        "product_service.py:ProductService.readiness_snapshot",
        "product_service.py:ProductService.operational_metrics",
        "product_service.py:render_prometheus_metrics",
        "product_service.py:AuditVerificationMonitor.check",
        "product_service.py:AuditVerificationMonitor.start",
        "product_service.py:AuditVerificationMonitor.stop",
        "product_service.py:AuditVerificationMonitor.snapshot",
        "product_service.py:AuditVerificationMonitor.is_healthy",
        "product_service.py:AuditVerificationMonitor._check_did_not_complete",
        "product_service.py:AuditVerificationMonitor.write_failed",
        "product_service.py:ProductRetentionMonitor.start",
        "product_service.py:ProductRetentionMonitor.stop",
        "product_service.py:ProductRetentionMonitor.snapshot",
        "product_service.py:ProductRetentionMonitor.is_healthy",
        "product_service.py:ProductRetentionMonitor._sweep_did_not_complete",
        "product_service.py:_running_thread",
        "product_service.py:_ended_on_its_own",
        "product_service.py:_audit_write_failure",
        "product_service.py:_report_audit_write_failure",
        "product_service.py:ProductScheduler.__init__",
        "product_service.py:ProductScheduler._print_log",
        "product_service.py:ProductScheduler._due",
        "product_service.py:ProductScheduler._due_since",
        "product_service.py:ProductScheduler._halted",
        "product_service.py:ProductScheduler.tick",
        "product_service.py:ProductScheduler._keep_looking",
        "product_service.py:ProductScheduler._look_again",
        "product_service.py:ProductScheduler._look",
        "product_service.py:ProductScheduler._start_thread",
        "product_service.py:ProductScheduler._finish",
        "product_service.py:ProductScheduler._run_turn",
        "product_service.py:ProductScheduler._progressed",
        "product_service.py:ProductScheduler._tick_completed",
        "product_service.py:ProductScheduler._tick_failed",
        "product_service.py:ProductScheduler.is_healthy",
        "product_service.py:ProductScheduler.snapshot",
        "product_service.py:ProductScheduler.start",
        "product_service.py:ProductScheduler.stop",
        "product_service.py:ProductScheduleStore.__init__",
        "product_service.py:ProductScheduleStore._locked",
        "product_service.py:ProductScheduleStore._tenant_hash",
        "product_service.py:ProductScheduleStore._key",
        "product_service.py:ProductScheduleStore._load",
        "product_service.py:ProductScheduleStore._save",
        "product_service.py:ProductScheduleStore.remove_scratch",
        "product_service.py:ProductScheduleStore.is_healthy",
        "product_service.py:ProductScheduleStore.put",
        "product_service.py:ProductScheduleStore.for_tenant",
        "product_service.py:ProductScheduleStore.remove",
        "product_service.py:ProductScheduleStore.entries",
        "product_service.py:ProductScheduleStore.get",
        "product_service.py:ProductScheduleStore.remove_session",
        "product_service.py:ProductScheduleStore.remove_internal",
        "product_service.py:ProductScheduleStore.remove_inactive",
        "product_service.py:ProductScheduleStore.mark_run",
        "product_service.py:ProductScheduleStore.claim_due",
        "product_service.py:ProductScheduleStore.complete_claim",
        "product_service.py:ProductScheduleStore.release_claim",
        "product_service.py:SessionOperationLocks.hold",
        "agent.py:PiAgent._dispatch",
        "agent.py:PiAgent.sandbox_for",
        "agent.py:PiAgent.tool_specs",
        "agent.py:PiAgent._session_lock",
        "agent.py:PiAgent._session_lock_done",
        "agent.py:redact_grants",
        "agent.py:entry_hash",
        "agent.py:sign_entry",
        "agent.py:verify_chain",
        "agent.py:verify_audit_dir",
        "agent.py:_audit_failure_replaced_by",
        "agent.py:AuditLog.__init__",
        "agent.py:AuditLog._path",
        "agent.py:AuditLog.read",
        "agent.py:AuditLog._read_path",
        "agent.py:AuditLog._last_entry",
        "agent.py:AuditLog.append_or_raise",
        "agent.py:AuditLog.record",
        "scripts/check_readiness_evidence.py:check",
        "scripts/check_readiness_evidence.py:verify_attestation",
        "scripts/check_readiness_evidence.py:attestation_command",
        "scripts/check_readiness_evidence.py:_verified_parts",
        "scripts/check_readiness_evidence.py:_validate_turn",
        "scripts/check_readiness_evidence.py:_validate_online_backup",
        "scripts/check_readiness_evidence.py:_number",
        "scripts/check_readiness_evidence.py:_arguments",

    }
    assert selected == required, (
        "ci.sh critical-coverage inventory drifted: "
        f"missing={sorted(required - selected)}, extra={sorted(selected - required)}")


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


def test_gate_and_release_build_a_solver_verifying_compiler():
    """THE bug class found 2026-08-22. `cargo build -p sigil-mcp` is a solver-OFF
    compiler whose forge gate fails closed (R817) unless
    SIGIL_ALLOW_UNVERIFIED_CERT=1 — an override SIGIL's bench harness sets and
    this host's vendored client strips. The suite forged through the bench
    client for months, so the product client had never forged against the
    binary ci.sh built. Pin that every place a compiler is built for this
    host builds it WITH the solver, that CI pins a Z3 to build it against,
    and that nothing here ever sets the override back."""
    ci = (PI_ROOT / "ci.sh").read_text()
    code = "\n".join(ln for ln in ci.splitlines() if not ln.strip().startswith("#"))
    build = re.search(r"cargo build --release[^\n]*", code)
    assert build, "ci.sh no longer rebuilds the forge binaries"
    for feature in ("sigil-mcp/solver", "sigil-serve/solver"):
        assert feature in build.group(0), (
            f"ci.sh builds a solver-OFF compiler (missing --features {feature}); the "
            f"vendored client strips SIGIL_ALLOW_UNVERIFIED_CERT, so every forge would "
            f"fail closed with R817")
    assert "Z3_SYS_Z3_HEADER" in code, "ci.sh lost its Z3 discovery for z3-sys"
    release = (PI_ROOT / "scripts" / "build_release.py").read_text()
    assert '"sigil-mcp/solver"' in release, \
        "build_release.py would ship a solver-off compiler the product cannot forge with"
    for workflow in ("ci.yml", "release.yml"):
        text = (PI_ROOT / ".github" / "workflows" / workflow).read_text()
        assert "Z3_SYS_Z3_HEADER" in text and "sha256sum -c" in text, (
            f"{workflow} must install a SHA256-pinned Z3 and point z3-sys at it, or the "
            f"solver-verifying build cannot link")
    _, jobs = _release_jobs()
    for job in ("gate", "build"):  # release.yml builds the compiler in both
        text = jobs.get(job, "")
        assert "Z3_SYS_Z3_HEADER" in text and "sha256sum -c" in text, (
            f"release.yml's {job} job builds the compiler and must install the pinned Z3")
    # The override must never be SET on the host side — in Python
    # (os.environ[...] =, env={...: "1"}, setenv), in the shell (VAR=1,
    # export VAR=1) or in a workflow (VAR: "1"). Prose that names it to
    # explain why it is stripped is fine, so comments and docstrings are
    # removed before matching. runtime_client.py may name it only to pop it;
    # the tests that prove the pop set it deliberately and are exempt.
    sets_override = re.compile(r"""SIGIL_ALLOW_UNVERIFIED_CERT["']?\s*\]?\s*[:=]""")
    for name in ("agent.py", "toolchain.py", "product_main.py", "make_chat_turn.py",
                 "runtime_client.py", "tests/conftest.py", "ci.sh",
                 ".github/workflows/ci.yml", ".github/workflows/release.yml"):
        hit = sets_override.search(_host_code(name))
        assert not hit, (
            f"{name} sets SIGIL_ALLOW_UNVERIFIED_CERT ({hit.group(0)!r}) — the "
            f"benchmark escape hatch must not be reachable from the host")
    client = (PI_ROOT / "runtime_client.py").read_text()
    assert 'child_env.pop("SIGIL_ALLOW_UNVERIFIED_CERT"' in client, \
        "runtime_client.py must strip the override from the compiler's environment"


def _host_code(name):
    """A file's source with `#` comment lines and (for Python) docstrings
    removed, so a guard can match what the code DOES rather than what its
    prose explains."""
    text = (PI_ROOT / name).read_text()
    lines = text.splitlines()
    if name.endswith(".py"):
        import ast
        for node in ast.walk(ast.parse(text)):
            body = getattr(node, "body", None)
            if (isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                  ast.AsyncFunctionDef))
                    and body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                for i in range(body[0].lineno - 1, body[0].end_lineno):
                    lines[i] = ""
    return "\n".join(ln for ln in lines if not ln.strip().startswith("#"))


def test_tests_compose_through_the_vendored_composer():
    """Order dependence, found 2026-08-22: six test modules imported SIGIL's
    bench composer, and the import only worked because test_taint_m4.py
    pushed <SIGIL_ROOT>/bench/src onto sys.path at module scope during
    collection — run test_guards.py on its own and the import failed. The
    vendored sigil_compose is pinned byte-identical to the bench composer by
    test_sigil_compose.py, the ONE module allowed to import the bench (it is
    the comparison), so every other test composes through the vendored one
    and nothing touches sys.path."""
    # Both needles are assembled at runtime so this guard's own source cannot
    # trip either of them.
    word = "ben" + "ch"
    needle = "sigil_" + word                          # the bench harness package
    on_path = re.compile("[\"']" + word + "[\"']")   # a bench dir spliced onto sys.path
    exempt = {
        "test_sigil_compose.py",   # the equivalence pin: it must import the bench
        "test_toolchain.py",       # upstream's guard names both strings to forbid them
    }
    for path in sorted((PI_ROOT / "tests").glob("*.py")):
        if path.name in exempt:
            continue
        text = path.read_text()
        assert needle not in text, (
            f"tests/{path.name} imports SIGIL's bench harness; compose through "
            f"sigil_compose (pinned identical) — a bench import only ever worked by "
            f"the sys.path side effect of another module's collection")
        assert not on_path.search(text), \
            f"tests/{path.name} puts a SIGIL bench directory on sys.path"


def test_the_readiness_gate_never_rebuilds_the_candidate():
    """THE GUARD for the circularity measured 2026-08-23.

    product-ci.sh used to rebuild the candidate from the working tree at gate
    time (`build_release.py --no-build`), so the digest every evidence file
    binds to moved whenever the readiness process recorded a result. It moved
    for ordinary reasons too: README.md is in the payload and
    test_readme_test_count_is_current forces it to change with every test added.

    A published candidate is verified, never recomputed. If `build_release` ever
    reappears in the gate, this fails.
    """
    gate = (PI_ROOT / "product-ci.sh").read_text()
    code = "\n".join(ln for ln in gate.splitlines() if not ln.strip().startswith("#"))
    assert "--no-build" not in code, (
        "product-ci.sh rebuilds the candidate; the gate must VERIFY a published "
        "archive, or recording evidence keeps invalidating it")
    assert "--verify" in code and "candidate.json" in code, (
        "product-ci.sh must resolve the frozen candidate from docs/evidence/candidate.json")


def test_attestation_inputs_are_literal_paths_not_globs():
    """THE BUG CLASS, found in pre-publication review 2026-08-23.

    `actions/attest`'s own action.yml documents ONLY `subject-path` as
    accepting a glob: "May contain a glob pattern or list of paths".
    `subject-checksums` is "Path to checksums file" and `sbom-path` is "Path to
    the JSON-formatted SBOM file". Passing `sigil-pi-*.tar.gz.sha256` to those
    meant the SBOM step could not find its file, and — worse — the provenance
    step would resolve ZERO subjects, signing nothing while reporting success.

    The previous guard asserted only that the KEYS were present, which is why
    CI stayed green over an unresolvable path.
    """
    text = (PI_ROOT / ".github" / "workflows" / "release.yml").read_text()
    for key in ("subject-checksums:", "sbom-path:"):
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith(key):
                continue
            value = stripped[len(key):].strip()
            assert "*" not in value, (
                f"{key} takes a literal path, not a glob ({value!r}); resolve the "
                f"filename in a step and pass it through $GITHUB_OUTPUT")
            assert value.startswith("${{"), (
                f"{key} should reference a resolved step output, got {value!r}")
    assert "gh attestation verify" in text, (
        "an attestation that bound to nothing must be caught before publishing, "
        "not discovered by whoever tries to verify the release later")
    # Attestation is skipped only for the one reason GitHub imposes — it refuses
    # to persist attestations for user-owned private repositories — and a
    # release that skipped it must SAY so rather than look identical to one
    # that did not.
    assert text.count("if: ${{ !github.event.repository.private }}") == 3, (
        "both attest steps and the verify step must share one visibility "
        "condition, so a public repo can never silently skip signing")
    assert "Published WITHOUT attestations" in text, (
        "an unattested release must announce itself; product readiness area 1 "
        "cannot be satisfied by one")


def test_every_bundled_entry_point_enforces_the_python_floor():
    """docs/support-matrix.md ships INSIDE the bundle saying Python <3.12 is
    unsupported and that "unsupported selections must fail startup where the
    process can detect them" — and the SBOM stamps python.requires >=3.12.
    Nothing enforced it, so the bundle would start and serve real turns on an
    older interpreter while carrying the document that forbids it."""
    for name in ("product_main.py", "state_tool.py", "scripts/release_drill.py"):
        source = (PI_ROOT / name).read_text()
        assert "MINIMUM_PYTHON" in source and "sys.version_info" in source, (
            f"{name} is a bundled entry point and must refuse an unsupported "
            f"interpreter before it does any work")


def test_recovery_drill_workflow_drills_the_frozen_candidate_and_nothing_else():
    """The qualifying distinct-version drill runs in CI because the published
    artifacts are linux-x86_64 and their embedded runtime cannot exec on a
    development Mac. What makes the run EVIDENCE rather than an exercise is
    binding: the workflow must verify the downloaded candidate against
    docs/evidence/candidate.json before drilling it, drill the recorded
    rollback_from as the old release, produce the backup with the fixture
    (which commits a real turn), and install the pinned libz3 into the
    loader's path — the drill's probe deliberately strips LD_LIBRARY_PATH,
    exactly like a production host."""
    path = PI_ROOT / ".github" / "workflows" / "recovery-drill.yml"
    assert path.is_file(), "the recovery drill workflow is missing"
    text = path.read_text()
    for needle, why in (
            ("workflow_dispatch", "the drill is run deliberately, not on every push"),
            ("scripts/drill_fixture.py", "the backup must come from a real committed turn"),
            ("scripts/release_drill.py", "the drill itself"),
            ("--verify", "the candidate must be verified before it is drilled"),
            ("candidate.json", "the frozen record is the binding"),
            ("rollback_from", "the old release must be the recorded rollback target"),
            ("ldconfig", "libz3 must be resolvable with a stripped environment"),
            ("Z3_SHA256", "the runtime library is pinned, not whatever apt has"),
    ):
        assert needle in text, f"recovery-drill.yml lost {needle!r}: {why}"


def test_failure_drill_workflow_binds_the_candidate_and_can_really_fill_a_disk():
    """The six-category matrix is only evidence about THE candidate, and only
    if the injections are real. Two of those properties live in the workflow
    rather than the drill: it must verify the downloaded archive against
    docs/evidence/candidate.json before injecting anything, and it must run on
    a host that can mount a tmpfs — the drill refuses to fake a full disk, so
    without that privilege the run burns forty minutes to report its own
    ineligibility. The report is stamped with this run's immutable URL, which
    is what makes the raw evidence name the run that produced it."""
    path = PI_ROOT / ".github" / "workflows" / "failure-drill.yml"
    assert path.is_file(), "the failure drill workflow is missing"
    text = path.read_text()
    for needle, why in (
            ("workflow_dispatch", "the drill is run deliberately, not on every push"),
            ("scripts/failure_drill.py", "the drill itself"),
            ("--verify", "the candidate must be verified before it is drilled"),
            ("candidate.json", "the frozen record is the binding"),
            ("mount -t tmpfs", "a full disk must be a real filesystem filling up"),
            ("--evidence-url", "the report must name the run that produced it"),
            ("github.run_id", "the evidence URL must be THIS run, not a literal"),
            ("ldconfig", "libz3 must be resolvable with a stripped environment"),
            ("Z3_SHA256", "the runtime library is pinned, not whatever apt has"),
    ):
        assert needle in text, f"failure-drill.yml lost {needle!r}: {why}"
    assert "mktemp -d" in text, "mount preflight must use a fresh disposable directory"
    assert "tmpfs /mnt" not in text, "never mount over the runner's generic /mnt"
    run_blocks = "\n".join(text.split("run: |")[1:])
    assert '"${{ inputs.tag }}"' not in run_blocks
    assert '"${{ inputs.bounded_filesystem_mb }}"' not in run_blocks


def test_failure_drill_is_not_bundled_into_the_release():
    """Bundling the drill would move the candidate digest every evidence
    artifact binds to, and it is not an operator tool: it breaks a bundle from
    OUTSIDE. The recovery drill is bundled because an operator runs it during
    a real recovery; this one is release-gate machinery."""
    from scripts.build_release import APP_FILES
    assert "scripts/release_drill.py" in APP_FILES, "the recovery drill is an operator tool"
    assert "scripts/failure_drill.py" not in APP_FILES, (
        "the failure drill must not be packaged: it would change the candidate "
        "digest that docs/evidence/candidate.json freezes")
    assert "scripts/failure_drill_rename.c" not in APP_FILES


def test_no_shipped_module_waits_with_select_select():
    """select.select raises ValueError for any descriptor numbered at or above
    FD_SETSIZE (1024), and a busy host gets there: a pool of compilers with three
    pipes each, client sockets, audit and state files. Every wait on a descriptor
    uses poll, which has no such limit, as product_service._readable does
    (audit 2026-11-10, R1 sweep, routed to A5). A ValueError is not an OSError,
    so the callers' error handling would not even see it as a broken connection."""
    offenders = []
    for path in sorted(PI_ROOT.glob("*.py")) + sorted((PI_ROOT / "scripts").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if (isinstance(node, ast.Attribute) and node.attr == "select"
                    and isinstance(node.value, ast.Name) and node.value.id == "select"):
                offenders.append(f"{path.relative_to(PI_ROOT)}:{node.lineno}")
    assert not offenders, "select.select cannot wait on descriptor 1024 or above: " + ", ".join(offenders)


def test_blocking_lane_fixtures_cannot_race_their_own_deadline():
    """THE BUG CLASS, found by the full gate on 2026-09-10 and again on 2026-09-12.

    Lane tests drive real subprocesses. Two kinds of wall-clock bound govern them:
    the fixture's own request deadline, and the wait that watches it. Both were set
    to four or five seconds while the rest of this suite allows 15-30s for
    subprocess work, so both lost their race on a loaded host while passing alone
    with a wide margin.

    `Bridge` checks cancellation BEFORE the deadline, so dropping a lane is observed
    as `Cancelled` — but only while the lane thread is still running. A request
    deadline that elapses during fixture setup finishes the thread with `Deadline`
    first, and the test sees `Deadline` where it asserted `Cancelled`. Separately, a
    notifier that waits only five seconds for a worker to reach invocation gives up
    mid-startup and cancels anyway. Because the `readiness_host_binary` fixture
    asserts a zero `cargo test` exit, either failure also takes out every Python
    test in the file that depends on it — 50 the first time, 17 the second.

    The margin is the fix. Weakening an assertion to accept either fault would hide
    a real cancellation regression, so the exact assertions are pinned too.
    """
    claimed = PI_ROOT / "native" / "service" / "src" / "claimed"
    shared = (claimed / "tests.rs").read_text()

    match = re.search(r"const HELD_TIMEOUT_MS: u64 = ([0-9_]+);", shared)
    assert match, (
        "the lane fixtures must name their request budget in one shared constant, "
        "so the margin is reviewable rather than buried in per-test literals")
    held_ms = int(match.group(1).replace("_", ""))
    assert held_ms >= 30_000, (
        f"HELD_TIMEOUT_MS is {held_ms}ms; a fixture that blocks until a test "
        f"cancels it needs a deadline far beyond real setup time on a loaded "
        f"host. 30_000 is the largest value every admission path accepts "
        f"(pure::invoke clamps to 30_000).")

    wait = re.search(r"const HELD_WAIT: Duration = Duration::from_secs\((\d+)\);", shared)
    assert wait, "the waits that watch lane fixtures must share one named budget"
    assert int(wait.group(1)) >= 30, (
        f"HELD_WAIT is {wait.group(1)}s; process_tests already allows 15s and four "
        f"other suites allow 30s for subprocess work. A shorter wait expires while "
        f"the worker is still starting and reports it as the worker's fault.")

    # The lane-fixture files arrive in different changes, so check the ones that
    # are here. Each carries the exact cancellation assertion its margin protects.
    pinned = {
        "owned/tests.rs": "assert_eq!(observation.fault, Some(Fault::Cancelled));",
        "audit_tests.rs": "assert_eq!(observed.fault, Some(Fault::Cancelled));",
    }
    present = [name for name in pinned if (claimed / name).is_file()]
    assert present, (
        "this guard ships with the lane fixtures; if none of "
        f"{sorted(pinned)} exists it is guarding nothing and must be removed "
        "rather than left passing vacuously")

    for name in present:
        source = (claimed / name).read_text()
        assert "Instant::now() + Duration::from_secs(" not in source, (
            f"{name} builds a wall-clock wait from a bare literal; lane waits must "
            f"use HELD_WAIT so one edit moves them all. A deliberately short "
            f"deadline under test should use from_millis and say why.")
        assert "= HELD_TIMEOUT_MS;" in source, (
            f"{name} must apply the held budget to the REQUEST timeout. The "
            f"deadline is Instant::now() + timeout_ms taken at prepare time; "
            f"config.max_timeout_ms is only its ceiling, so raising the ceiling "
            f"alone leaves the five-second race exactly as it was.")
        assert pinned[name] in source, (
            f"{name} must still assert Cancelled exactly; accepting whatever fault "
            f"a slow host produced first would make a real cancellation "
            f"regression invisible")


def test_only_the_store_observer_reads_a_native_store_database():
    """THE BUG CLASS, seen on CI on 2026-11-23 and reproduced by holding a reader's lock for 50ms.

    native/store keeps one SQLite connection with a zero busy timeout and treats a
    failed commit as fatal: the store poisons itself and refuses work until it is
    reopened. Any ordinary SQLite connection, read-only included, holds a SHARED
    lock while a statement runs, and that is enough to fail the owner's COMMIT.

    Nine places in the tests read `records/records.sqlite` through `?mode=ro`, one
    of them every 10ms while waiting for a turn to reach a boundary. When a poll met
    a commit the service stopped where it was, and 100 seconds later the test
    reported that the service "did not commit": it blamed what it had just broken.
    It passed alone and on a fast machine, and failed on a slow CI runner.

    tests/store_observer.py reads without locking and keeps a read only when it has
    proved the file held still. So a test names the store's database nowhere else,
    except where it means to interfere, and every such line is listed here.
    """
    deliberate = {
        # A path that must NOT exist. Nothing is opened.
        "test_native_store.py": ['assert not (root / "records.sqlite").exists()'],
        # Corrupts one digest, to prove discovery reports it and repairs nothing.
        "test_listing_api.py": ['with closing(sqlite3.connect(root / "records/records.sqlite")) as db:'],
        # Takes the write lock on purpose: readiness must report the store busy.
        "test_readiness_storage_host.py": ['with closing(sqlite3.connect(root / "records/records.sqlite")) as db:'],
    }
    own = {"store_observer.py", "test_store_observer.py", "test_guards.py"}
    tests = PI_ROOT / "tests"
    observed = 0
    for path in sorted(tests.iterdir()):
        if not path.is_file() or path.suffix not in {".py", ".mjs"} or path.name in own:
            continue
        source = path.read_text()
        observed += "from store_observer import observe" in source
        named = [line.strip() for line in source.splitlines() if "records.sqlite" in line]
        assert named == deliberate.get(path.name, []), (
            f"tests/{path.name} names the native store's database itself:\n  "
            + "\n  ".join(named) + "\n"
            "A connection opened on that file can take a lock, and one lock at the "
            "wrong moment stops the service for good. Read it with "
            "store_observer.observe(root, lambda db: ...). If the test means to "
            "interfere with the store, list the exact line here and say why.")
    assert observed, (
        "no test reads the store through store_observer any more; if that is "
        "intended this guard and the observer are guarding nothing and should go")


def test_this_run_says_which_test_failed_at_the_moment_it_fails(request):
    """THE BUG CLASS, seen on CI twice on 2026-11-23.

    pytest names a failed test in its closing report. The gate's job is killed
    when it reaches its time limit, and a run that is killed has no closing
    report. Run 45172865023 was cancelled at 180 minutes after 4,824 results,
    four of them F; run 45237576754 at 90 minutes after 3,384, two of them F.
    Neither log names a test. All six were named afterwards, by counting each
    F's position against the collection order of its commit. What they said is
    in neither log.

    tests/failed_now.py writes a line for each failure as it happens. Its own
    tests run it on a scratch file and cannot tell whether this suite loads it,
    so this asks the run it is part of. However the suite was started, and
    whatever was added to the command, the plugin must be loaded and writing to
    this run's own terminal reporter.
    """
    import failed_now
    plugins = request.config.pluginmanager
    announcer = plugins.get_plugin(failed_now.ANNOUNCER)
    assert announcer is not None, (
        "tests/failed_now.py is not loaded in this run: a failure is one F until the "
        "run ends, and nothing at all if the run is stopped first. tests/conftest.py "
        "loads it with pytest_plugins, and `-p no:failed_now` on a command unloads it.")
    assert announcer.terminal is plugins.get_plugin("terminalreporter"), (
        "tests/failed_now.py is loaded but is not writing to this run's terminal reporter")
