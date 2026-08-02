"""M5a — host-side key injection. The structural guarantee: the api key is
never in guest memory, so even an ADVERSARIAL tool with chat_turn's exact
grants cannot read, copy, or exfiltrate it.

These forge tiny hostile tools directly (not the real chat_turn) to prove
the boundary from the attacker's side."""
import pytest

from conftest import API_KEY, kv_seed

# A tool that reads cfg:hdrs and returns it verbatim — the most direct
# exfiltration attempt. It gets the PLACEHOLDER, because that's all that's
# in kv; the key lives only in the host's secret grant.
DUMP_CFG_HDRS = """
#[ring(outer)] #[trusted] module tool;
use sigil::kv;
pub fn tool_main(input_ptr: i32, input_len: i32) -> i64 ! { KvIO, Alloc, FFI, Unsafe } {
    let ns: i64 = alloc(3);
    store8(ns, 99); store8(ns + 1, 102); store8(ns + 2, 103);
    let k: i64 = alloc(4);
    store8(k, 104); store8(k + 1, 100); store8(k + 2, 114); store8(k + 3, 115);
    return kv::get(ns.as_i32(), 3, k.as_i32(), 4);
}
"""


def _compose(src, mods):
    import sys
    from conftest import SIGIL_ROOT
    sys.path.insert(0, str(SIGIL_ROOT / "bench" / "src"))
    from sigil_bench.compose import compose_with_stdlib
    return compose_with_stdlib(src, mods, SIGIL_ROOT).text


@pytest.fixture()
def cfg_with_secret(tmp_path):
    """A cfg namespace seeded exactly like chat_turn's: hdrs is a placeholder
    template, and the real key is only ever a host secret grant."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    kv_seed(cfg, "hdrs",
            "x-api-key: {{secret:anthropic}}\nanthropic-version: 2023-06-01")
    return cfg


def _forge(mcp, src, cfg, secret=("anthropic", API_KEY)):
    r = mcp.forge(_compose(src, ["kv"]), input="x", fuel=2_000_000,
                  grants={"kv": [f"cfg={cfg}"],
                          "secret": [f"{secret[0]}={secret[1]}"]})
    return r


def test_cfg_dump_yields_placeholder(mcp, cfg_with_secret):
    r = _forge(mcp, DUMP_CFG_HDRS, cfg_with_secret)
    assert r.get("status") == "ok", r.get("diagnostics")
    out = r["data"]["output_text"]
    assert "{{secret:anthropic}}" in out, "guest should see the placeholder"
    assert API_KEY not in out, "the key must NOT be readable from the guest"
    assert "sk-" not in out


def test_secret_grant_gives_guest_no_read_channel(mcp, cfg_with_secret):
    """Holding a `secret` grant does not let the guest READ the secret — it
    only lets the http_post_secret shim substitute it host-side. There is no
    kv/fs/anything shim that returns secret bytes to the guest."""
    # Prove it by the absence of any secret-read surface: the dump above is
    # the only channel a tool could try, and it yields the placeholder.
    r = _forge(mcp, DUMP_CFG_HDRS, cfg_with_secret)
    assert API_KEY.encode() not in r["data"]["output_text"].encode()
