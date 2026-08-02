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
    from sigil_bench.compose import compose_with_stdlib
    from conftest import SIGIL_ROOT

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


def test_runtime_state_dirs_are_gitignored():
    """Every directory the project creates at runtime must be uncommittable.
    .pi-state is the sharp one: it holds SESSION TRANSCRIPTS and per-session
    sandboxes (PI_STATE defaults to the repo root), so an unignored default
    plus one `git add -A` publishes real conversation data."""
    import subprocess
    if not (PI_ROOT / ".git").exists():
        import pytest
        pytest.skip("not a git checkout")
    for d in (".pi-state", ".venv", "__pycache__", ".pytest_cache", ".hypothesis"):
        # trailing slash: ask about the DIRECTORY. A dir-only pattern like
        # `.pi-state/` doesn't match a bare query for a path that doesn't
        # exist yet, and this guard must not depend on whether the agent has
        # ever been run in this checkout.
        r = subprocess.run(["git", "-C", str(PI_ROOT), "check-ignore", "-q", d + "/"])
        assert r.returncode == 0, \
            f"{d} is not gitignored — runtime/derived state must never be committable"


def test_forge_ci_job_is_gated_at_the_job_level():
    """When the SIGIL toolchain is unavailable to CI, the forge job must show
    as SKIPPED — visibly not-run. The original step-level gate reported a
    green 'success' in ~7s while running nothing (measured on PR #7), which
    trains everyone to read a green tick as a gate that never ran. Pin the
    job-level `if` on the gate job's output so that can't come back."""
    text = (PI_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    parts = text.split("\n  forge:", 1)
    assert len(parts) == 2, "ci.yml lost its forge job"
    header = parts[1].split("\n    steps:", 1)[0]  # forge job config, pre-steps
    assert re.search(r"needs:\s*\[?\s*gate\s*\]?", header), \
        "forge must depend on the `gate` job that probes for the toolchain"
    assert "if: needs.gate.outputs.available == 'true'" in header, (
        "forge must be gated at the JOB level (skipped, visibly) — a "
        "step-level gate reports success while running nothing")


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
