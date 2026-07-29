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

from conftest import PI_ROOT, SIGIL_ROOT, forge_err

sys.path.insert(0, str(SIGIL_ROOT / "bench" / "src"))


def _compose(src: str, mods):
    from sigil_bench.compose import compose_with_stdlib
    return compose_with_stdlib(src, mods, SIGIL_ROOT).text


def chat_turn_source() -> str:
    return (PI_ROOT / "tools" / "chat_turn.sigil").read_text()


def test_secret_marker_present_in_main():
    """M4's contract: the header/key value is typed @Secret in frag_main
    (not merely @Internal like ordinary network data)."""
    main = (PI_ROOT / "tools" / "frag_main.sigil").read_text()
    assert re.search(r"@Secret\b", main), (
        "frag_main.sigil carries no @Secret annotation — the key is not "
        "taint-distinguished from ordinary network data yet")


def test_leaky_variant_fails_taint_check(mcp):
    """A tool that copies header (key) bytes into its output must be
    REJECTED at taint-check. If this forge succeeds, the taint system is
    not guarding the secret at all."""
    leaky = (PI_ROOT / "tests" / "fixtures" / "leaky_turn.sigil")
    assert leaky.exists(), "fixtures/leaky_turn.sigil missing (M4 authors it)"
    message = forge_err(mcp, _compose(leaky.read_text(), ["http", "kv"]),
                        "s1|hi", fuel=1_000_000)
    assert re.search(r"T0\d\d|taint", message, re.I), (
        f"leak was not stopped by taint-check: {message}")
