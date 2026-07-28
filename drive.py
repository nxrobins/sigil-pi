#!/usr/bin/env python3
"""pi driver v0 — interactive shell around the SIGIL agent turn.

Each user message is one forge of pi/turn0.sigil against the configured endpoint.
Mock mode (default) hits the local fixture server; swap PI_ENDPOINT for a real one.

  PYTHONPATH=bench/src python pi/drive.py            # from the SIGIL repo root
"""
import os, sys
from pathlib import Path
import os
repo = Path(os.environ.get("SIGIL_ROOT", Path(__file__).resolve().parent.parent / "SIGIL")).resolve()
sys.path.insert(0, str(repo / "bench" / "src"))
from sigil_bench.compose import compose_with_stdlib
from sigil_bench.mcp_client import SigilMCP

ENDPOINT = os.environ.get("PI_ENDPOINT", "http://127.0.0.1:8973/reply.txt")
HOST = ENDPOINT.split("//")[1].split("/")[0].split(":")[0]
code = (Path(__file__).parent / "tools" / "turn0.sigil").read_text()
composed = compose_with_stdlib(code, ["http"], repo).text

print(f"pi v0 — endpoint {ENDPOINT} (grant: net={HOST}). Ctrl-D to exit.")
with SigilMCP.spawn(repo / "target" / "release" / "sigil-mcp") as m:
    m.initialize()
    while True:
        try:
            msg = input("you> ")
        except EOFError:
            break
        r = m.forge(code if False else composed, input=f"{ENDPOINT}|{msg}",
                    fuel=2_000_000, grants={"net": [HOST]})
        if r.get("status") == "ok":
            print("pi >", r["data"]["output_text"])
        else:
            d = (r.get("diagnostics") or [{}])[0]
            print("pi > [forge error]", d.get("code"), (d.get("message") or "")[:120])
