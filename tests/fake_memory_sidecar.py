#!/usr/bin/env python3
"""A scripted stand-in for the wave-memory sidecar, speaking its line-JSON
protocol — so PiMemory is testable hermetically (no Rust, no toolchain).

Behavior is driven by env:
  FAKE_MEMORY_MODE=ok         normal answers (default)
  FAKE_MEMORY_MODE=busy       every op answers the busy error
  FAKE_MEMORY_MODE=refuse     exit 1 immediately with a marker-style stderr
  FAKE_MEMORY_MODE=die        exit mid-protocol after the first request
  FAKE_MEMORY_MODE=storm      consolidate emits model_request callbacks forever
  FAKE_MEMORY_MODE=wrongid    replies carry a wrong id (protocol corruption)
  FAKE_MEMORY_MODE=huge       retrieve answers one enormous episodic item
  FAKE_MEMORY_LOG=<path>      append every received line (requests AND
                              callback answers) for test assertions

In ok mode: `retrieve` answers one episodic item echoing the query;
`consolidate` with model "callback" emits ONE model_request callback, waits
for its answer, then reports a committed cycle — exercising the host's full
callback path without any engine.
"""
import json
import os
import sys

MODE = os.environ.get("FAKE_MEMORY_MODE", "ok")
LOG = os.environ.get("FAKE_MEMORY_LOG")


def log(line: str) -> None:
    if LOG:
        with open(LOG, "a") as f:
            f.write(line.rstrip("\n") + "\n")


def reply(obj) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> None:
    # argv mirrors the real binary: serve --root R --mode M
    if MODE == "refuse":
        print("Error: memory root was created in 'shared' mode but the "
              "sidecar was started in 'session' mode. Run "
              "`wave-memory-sidecar migrate ...`", file=sys.stderr)
        sys.exit(1)
    handled = 0
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        log(raw)
        req = json.loads(raw)
        rid = req.get("id")
        handled += 1
        if MODE == "die" and handled > 1:
            sys.exit(1)
        if MODE == "busy" and req.get("op") != "ping":
            reply({"id": rid, "err": {"code": "busy",
                                      "message": "memory consolidation is in progress"}})
            continue
        if MODE == "wrongid" and req.get("op") != "ping":
            reply({"id": 999_999, "ok": {}})
            continue
        op = req.get("op")
        if op == "record":
            reply({"id": rid, "ok": {"cycle": handled}})
        elif op == "retrieve" and MODE == "huge":
            reply({"id": rid, "ok": {
                "procedural": [], "gaps": [], "verbatim": [], "facts": [],
                "episodic": [{"text": "x" * 200_000, "tokens": 50_000}],
                "total_tokens": 50_000, "budget_remaining": 0}})
        elif op == "retrieve":
            reply({"id": rid, "ok": {
                "procedural": [], "gaps": [], "verbatim": [], "facts": [],
                "episodic": [{"text": f"previously: {req.get('query', '')}",
                              "tokens": 8}],
                "total_tokens": 8, "budget_remaining": 100}})
        elif op == "stats":
            reply({"id": rid, "ok": {"last_cycle": handled,
                                     "consolidation_count": 0}})
        elif op == "consolidate" and MODE == "storm":
            call = 0
            while True:
                call += 1
                reply({"callback": "model_request", "call_id": call, "request": {
                    "role": "Judge", "system": "", "user": "again",
                    "max_tokens": 16}})
                line = sys.stdin.readline()
                if not line:
                    return
        elif op == "consolidate":
            if req.get("model") == "callback":
                reply({"callback": "model_request", "call_id": 1, "request": {
                    "role": "Consolidate", "system": "you consolidate",
                    "user": "summarize the episodes", "max_tokens": 256}})
                answer = json.loads(sys.stdin.readline())
                log(json.dumps(answer))
                assert answer.get("call_id") == 1, "answer must echo call_id"
            reply({"id": rid, "ok": {"dream_id": "fake-dream", "committed": True,
                                     "episodes_consolidated": 2,
                                     "patterns_created": 0, "facts_created": 0,
                                     "patterns_pruned": 0}})
        elif op == "shutdown":
            reply({"id": rid, "ok": {"stopping": True}})
            return
        else:
            reply({"id": rid, "err": {"code": "unsupported",
                                      "message": f"unknown op {op!r}"}})


if __name__ == "__main__":
    main()
