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
    # argv mirrors the real binary: serve --root R --mode M --embedder E
    if MODE == "refuse":
        print("Error: memory root was created in 'shared' mode but the "
              "sidecar was started in 'session' mode. Run "
              "`wave-memory-sidecar migrate ...`", file=sys.stderr)
        sys.exit(1)
    def arg(name, default):
        try:
            return sys.argv[sys.argv.index(name) + 1]
        except (ValueError, IndexError):
            return default

    embedder = arg("--embedder", "callback")
    embedding_model = arg("--embedding-model", "bge-small-en-v1.5")
    handled = 0
    ordinary = 0
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        log(raw)
        req = json.loads(raw)
        rid = req.get("id")
        handled += 1
        op = req.get("op")
        bootstrap = op in ("hello", "ping", "probe_embedding")
        if not bootstrap:
            ordinary += 1
        if MODE == "die" and ordinary > 0:
            sys.exit(1)
        if MODE == "busy" and not bootstrap:
            reply({"id": rid, "err": {"code": "busy",
                                      "message": "memory consolidation is in progress"}})
            continue
        if MODE == "wrongid" and not bootstrap:
            reply({"id": 999_999, "ok": {}})
            continue
        if op in ("hello", "ping"):
            if req.get("protocol") not in (None, 2):
                reply({"id": rid, "err": {"code": "bad_request",
                                            "message": "unsupported protocol"}})
            else:
                reply({"id": rid, "ok": {
                    "protocol": 2, "mode": arg("--mode", "session"),
                    "embedder": {
                        "mode": embedder,
                        "model_id": (embedding_model if embedder == "callback"
                                     else "sidecar-hash-384-v1"),
                        "readiness": ("production" if embedder == "callback"
                                      else "development"),
                        "dimensions": 384},
                    "operations": ["record", "retrieve", "consolidate_all"]}})
        elif op == "probe_embedding":
            if embedder == "callback":
                reply({"callback": "embedding_request", "call_id": 7001,
                       "model": embedding_model, "texts": ["health probe"]})
                answer = json.loads(sys.stdin.readline())
                log(json.dumps(answer))
                if "error" in answer:
                    reply({"id": rid, "err": {"code": "internal",
                                                "message": answer["error"]["message"]}})
                    continue
            reply({"id": rid, "ok": {"model_id": embedding_model,
                                       "dimensions": 384, "norm": 1.0}})
        elif op == "record":
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
        elif op in ("consolidate", "consolidate_all") and MODE == "storm":
            call = 0
            while True:
                call += 1
                reply({"callback": "model_request", "call_id": call, "request": {
                    "role": "Judge", "system": "", "user": "again",
                    "max_tokens": 16}})
                line = sys.stdin.readline()
                if not line:
                    return
        elif op in ("consolidate", "consolidate_all"):
            if req.get("model") == "callback":
                reply({"callback": "model_request", "call_id": 1, "request": {
                    "role": "Consolidate", "system": "you consolidate",
                    "user": "summarize the episodes", "max_tokens": 256}})
                answer = json.loads(sys.stdin.readline())
                log(json.dumps(answer))
                assert answer.get("call_id") == 1, "answer must echo call_id"
            report = {"dream_id": "fake-dream", "committed": True,
                      "episodes_consolidated": 2, "patterns_created": 0,
                      "facts_created": 0, "patterns_pruned": 0}
            if op == "consolidate_all":
                reply({"id": rid, "ok": {"stores_consolidated": 1,
                                           "reports": [{"store": "shared",
                                                        "report": report}]}})
            else:
                reply({"id": rid, "ok": report})
        elif op == "list_sessions":
            reply({"id": rid, "ok": {"sessions": ["s1"]}})
        elif op == "inspect":
            reply({"id": rid, "ok": {"records": [{
                "cycle": 1, "text": "remembered", "kind": "user_message",
                "scope": req.get("session"), "created_at": "2026-08-01T00:00:00Z"}]}})
        elif op == "forget":
            reply({"id": rid, "ok": {"session": req.get("session"),
                                       "forgotten": True}})
        elif op == "reindex":
            reply({"id": rid, "ok": {"cycles_processed": 2,
                                       "model_id": embedding_model}})
        elif op == "shutdown":
            reply({"id": rid, "ok": {"stopping": True}})
            return
        else:
            reply({"id": rid, "err": {"code": "unsupported",
                                      "message": f"unknown op {op!r}"}})


if __name__ == "__main__":
    main()
