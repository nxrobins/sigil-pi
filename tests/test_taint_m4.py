"""M4 red tests — taint-proofed secrets.

The headline claim: the compiler PROVES the api key cannot reach the
transcript. M4 makes the key arrive as @Secret data and requires:

1. the real chat tool still compiles (the key flows only into the
   http::post_hdrs header argument — a permitted @Secret sink), and
2. a deliberately-leaky variant that copies key bytes into its output
   FAILS taint-check — the negative proof that the checker is actually
   guarding the boundary, not waving everything through.

These start red: cfg:hdrs is currently read via kv::get, which returns
@Internal — not @Secret — so the leaky variant compiles today. Going
green requires a @Secret-carrying channel for the key (M4's design work:
e.g. a kv_get_secret shim or a @Secret cast at the cfg boundary) plus
taint annotations in chat_turn.
"""
import re
import sys

import pytest

pytestmark = pytest.mark.m4  # red by design until M4

from conftest import PI_ROOT, SIGIL_ROOT, forge_err

sys.path.insert(0, str(SIGIL_ROOT / "bench" / "src"))


def _compose(src: str, mods):
    from sigil_bench.compose import compose_with_stdlib
    return compose_with_stdlib(src, mods, SIGIL_ROOT).text


def chat_turn_source() -> str:
    return (PI_ROOT / "tools" / "chat_turn.sigil").read_text()


def test_key_is_not_in_the_guest_at_all():
    """M5a supersedes M4's in-guest @Secret channel with a STRONGER,
    structural guarantee: chat_turn holds only a {{secret:...}} placeholder
    template and sends it via http::post_secret; the host injects the key.
    The cfg the guest can read holds the placeholder, never the key."""
    code = "\n".join(
        line.split("//", 1)[0]
        for line in (PI_ROOT / "tools" / "frag_main.sigil").read_text().splitlines())
    assert "http::post_secret" in code
    assert "@Secret" not in code, "the key never enters the guest — nothing to taint here"


def test_leaky_variant_fails_taint_check(mcp):
    """A tool that returns the @Secret header (key) value must be REJECTED
    at taint-check. If this forge succeeds, the taint system is not
    guarding the secret at all."""
    leaky = (PI_ROOT / "tests" / "fixtures" / "leaky_turn.sigil")
    assert leaky.exists(), "fixtures/leaky_turn.sigil missing"
    message = forge_err(mcp, _compose(leaky.read_text(), ["kv"]),
                        "cfg", fuel=1_000_000)
    assert re.search(r"T001|taint|@Secret", message, re.I), (
        f"leak was not stopped by taint-check: {message}")


def test_real_chat_turn_still_compiles(mcp):
    """The @Secret annotations must not break the legitimate flow: the key
    reaches only the http_post_hdrs header arg, never the output/kv/body."""
    src = (PI_ROOT / "tools" / "chat_turn.sigil").read_text()
    r = mcp.forge(src, input="s|m", fuel=1000, grants={"net": ["127.0.0.1"]})
    # forge fails at RUN (no kv grant on this bare probe), NOT at compile:
    # any T0xx taint/type diagnostic would mean the annotations broke it.
    codes = [d.get("code") for d in (r.get("diagnostics") or [])]
    assert not any(c and c.startswith("T0") for c in codes), \
        f"chat_turn no longer compiles cleanly: {codes}"


def test_memory_laundering_is_caught(mcp):
    """M5b closed the naive memory-launder: `store8(out, secret)` now raises
    `out`'s taint, so returning it is T001. (Was a strict xfail before M5b;
    flipped green when the compiler grew the store8-taint rule.)"""
    launder = (PI_ROOT / "tests" / "fixtures" / "laundering_turn.sigil")
    message = forge_err(mcp, _compose(launder.read_text(), ["kv"]),
                        "cfg", fuel=1_000_000)
    assert re.search(r"T001|taint|@Secret", message, re.I), \
        f"memory launder was not caught: {message}"


def test_pointer_aliasing_launder_is_caught(mcp):
    """M6 closed the aliasing gap with region-based points-to: `let q = out;
    store8(out, secret); return q` now taints `out`'s REGION, and every local
    in that region (incl. the alias `q`) surfaces the secret -> T001. (Was a
    strict xfail after M5b; flipped green when the compiler grew the region
    analysis.)"""
    alias = (PI_ROOT / "tests" / "fixtures" / "aliasing_turn.sigil")
    message = forge_err(mcp, _compose(alias.read_text(), ["kv"]),
                        "cfg", fuel=1_000_000)
    assert re.search(r"T001|taint|@Secret", message, re.I), \
        f"aliasing launder was not caught: {message}"


@pytest.mark.xfail(reason="M6's region analysis is INTRA-procedural: a pointer "
                          "passed through a function loses its region (the call "
                          "result is untracked), so aliasing via a helper still "
                          "launders. Closing it needs interprocedural region "
                          "summaries — a whole-program analysis deliberately not "
                          "built. Honest boundary; flips green if SIGIL grows it.",
                   strict=True)
def test_interprocedural_aliasing_launder_is_caught(mcp):
    """The boundary AFTER M6: alias the destination THROUGH a function
    (`let q = identity(out)`). The call result carries no region, so `q`
    escapes. This xfail keeps the remaining gap visible."""
    interp = (PI_ROOT / "tests" / "fixtures" / "interproc_turn.sigil")
    message = forge_err(mcp, _compose(interp.read_text(), ["kv"]),
                        "cfg", fuel=1_000_000)
    assert re.search(r"T001|taint|@Secret", message, re.I)
