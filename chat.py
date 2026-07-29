#!/usr/bin/env python3
"""pi driver (milestone 1b) — one AUTHENTICATED agent turn per user message.

Each message forges tools/agent_turn.sigil once: the guest makes the outbound
POST to the Anthropic Messages API — carrying the secret `x-api-key` and
`anthropic-version` headers under a `net` grant scoped to exactly one host —
and returns the raw JSON response. The driver builds the request body and
extracts `content[0].text` (the guest can't: `json` is inner-ring, an http tool
is outer-ring — see the NOTE in agent_turn.sigil).

  export ANTHROPIC_API_KEY=sk-ant-...
  SIGIL_ROOT=../SIGIL python3 chat.py            # real API
  PI_ENDPOINT=http://127.0.0.1:8973/ python3 chat.py   # a mock, if you have one

Requires a SIGIL checkout built on the branch carrying `http::post_hdrs`:
  cargo build --release -p sigil-mcp
"""
import os, sys, json
from pathlib import Path

repo = Path(os.environ.get("SIGIL_ROOT", Path(__file__).resolve().parent.parent / "SIGIL")).resolve()
sys.path.insert(0, str(repo / "bench" / "src"))
from sigil_bench.compose import compose_with_stdlib
from sigil_bench.mcp_client import SigilMCP

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not API_KEY:
    sys.exit("set ANTHROPIC_API_KEY (the key stays a guest-side header segment; it is never echoed)")

ENDPOINT = os.environ.get("PI_ENDPOINT", "https://api.anthropic.com/v1/messages")
HOST = ENDPOINT.split("//")[1].split("/")[0].split(":")[0]
MODEL = os.environ.get("PI_MODEL", "claude-sonnet-5")
MAX_TOKENS = int(os.environ.get("PI_MAX_TOKENS", "1024"))

code = (Path(__file__).parent / "tools" / "agent_turn.sigil").read_text()
composed = compose_with_stdlib(code, ["http"], repo).text

HEADERS = "\n".join([
    f"x-api-key: {API_KEY}",
    "anthropic-version: 2023-06-01",
    "content-type: application/json",
])

print(f"pi 1b — {MODEL} @ {ENDPOINT} (grant: net={HOST}). Ctrl-D to exit.")
with SigilMCP.spawn(repo / "target" / "release" / "sigil-mcp") as m:
    m.initialize()
    while True:
        try:
            msg = input("you> ")
        except EOFError:
            break
        body = json.dumps({
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": msg}],
        })
        # FIRST TWO '|' split the segments; the body may contain '|' freely.
        r = m.forge(composed, input=f"{ENDPOINT}|{HEADERS}|{body}",
                    fuel=5_000_000, grants={"net": [HOST]})
        if r.get("status") != "ok":
            # Negative tool returns (-403 no grant, -401/-4xx status, -502 …) surface
            # here as "tool returned error (N)", alongside any compile diagnostic.
            d = (r.get("diagnostics") or [{}])[0]
            print("pi > [forge error]", d.get("code"), (d.get("message") or "")[:160])
            continue
        raw = r["data"]["output_text"]
        try:
            print("pi >", json.loads(raw)["content"][0]["text"])
        except Exception:
            print("pi > [unparseable response]", raw[:200])
