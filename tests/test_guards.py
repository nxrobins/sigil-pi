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


def test_infra_kv_errors_are_remapped_to_500():
    """Raw kv error propagation (the misleading-404 bug class): every kv::get
    /kv::put failure path in the orchestrator must remap to -500, except the
    intentional fresh-session -404 branch."""
    main = (TOOLS / "frag_main.sigil").read_text()
    # the cfg reader remaps internally; the two direct kv call sites must
    # never `return r_hist;` / `return pr;` style-propagate raw codes.
    assert not re.search(r"return\s+r_hist\s*;", main)
    assert not re.search(r"return\s+pr\s*;", main)
