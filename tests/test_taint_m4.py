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
from pathlib import Path

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


@pytest.mark.xfail(reason="taint checker is scalar-surface: it does not track "
                          "taint through load8/store8 memory, so a byte-copy "
                          "launders the secret. Documented boundary, not a "
                          "regression — flips green if SIGIL gains memory taint.",
                   strict=True)
def test_memory_laundering_is_caught(mcp):
    """The HONEST boundary of the guarantee: a tool that copies the secret
    byte-by-byte through memory currently COMPILES. This xfail keeps the
    gap visible and will flip the day the checker closes it."""
    launder = (PI_ROOT / "tests" / "fixtures" / "laundering_turn.sigil")
    message = forge_err(mcp, _compose(launder.read_text(), ["kv"]),
                        "cfg", fuel=1_000_000)
    assert re.search(r"T001|taint|@Secret", message, re.I)
