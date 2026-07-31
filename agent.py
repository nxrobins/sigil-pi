#!/usr/bin/env python3
"""pi host (milestones 7–8) — the serve-native agentic loop, bounded.

`POST /chat {session, message}` runs the full tool-using loop, keyed by
session, with conversation history persisted durably in kv so a fresh host
process resumes mid-conversation. History is BOUNDED (M8): the transcript is
compacted on turn boundaries and single tool results are clipped, so neither
the outgoing payload nor the kv value grows without limit. Every STEP is its
own ephemeral forge with its own minimal grant manifest — the host is trusted
orchestration, the guests are sandboxed:

    llm call   tools/agent_turn.sigil   net + secret       (outer ring)
    parse      tools/parse_reply.sigil  no grants — pure    (inner ring)
    dispatch   tools/<name>.sigil       manifest grants     (per tool, per session)

The ring bridge is the two-forge design: the outer tool never parses JSON, the
inner parser never touches the network. The api key is host-injected via
http::post_secret (never in the guest). Each session gets its own fs sandbox;
tool paths are RELATIVE to it (the host resolves them), so the model never sees
host paths and one session cannot reach another's files.

Frames from parse_reply: tag ('t'/'u'/'?') + 8-digit length + payload;
'u' payload = id \\x1f name \\x1f raw-input-JSON.
"""
import hashlib
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PI_ROOT = Path(__file__).resolve().parent
SIGIL_ROOT = Path(os.environ.get("SIGIL_ROOT", PI_ROOT.parent / "SIGIL")).resolve()
sys.path.insert(0, str(SIGIL_ROOT / "bench" / "src"))

from sigil_bench.compose import compose_with_stdlib  # noqa: E402
from sigil_bench.mcp_client import SigilMCP  # noqa: E402

MAX_STEPS = 8

# ── bounding the transcript (M8) ────────────────────────────────────────
#
# Left unbounded, a session's history grows on every turn: it is re-sent in
# full on every STEP of every turn (cost grows quadratically within a
# session), and it eventually walks into the sigil kv value cap and ends the
# session outright — the limit M2 documented and nothing since retired.
#
# Both bounds below are host-side, deterministic, and pure functions over the
# transcript: no summarizer, no extra LLM call, no new trust surface. The host
# owns every long-lived concern; the guest owns none.

KV_VALUE_CAP = 5 * 1024 * 1024        # sigil-runtime kv value limit
MAX_HISTORY_BYTES = 256 * 1024        # ceiling for a persisted/sent transcript
MAX_TOOL_RESULT_BYTES = 16 * 1024     # ceiling for ONE tool result


def history_bytes(messages) -> int:
    """Serialized size of a transcript — byte-identical to what
    SessionStore.save writes, so the bound is exact against KV_VALUE_CAP."""
    return len(json.dumps(messages, ensure_ascii=False).encode())


def segment_starts(messages) -> list:
    """Indices of real user turns (string content) — the ONLY safe cut points.
    A tool_result carrier is also role=user, but cutting there would orphan it
    from its tool_use, and the Messages API rejects an orphan of either."""
    return [i for i, m in enumerate(messages)
            if m.get("role") == "user" and isinstance(m.get("content"), str)]


def compact(messages, limit: int = MAX_HISTORY_BYTES):
    """Drop whole oldest turn-segments until the transcript fits `limit`.

    Returns a suffix of `messages` that always opens on a real user turn, so
    every surviving tool_use keeps its tool_result. The NEWEST segment is
    never dropped: a single turn larger than `limit` is kept whole and
    over-cap, because a corrupt transcript is worse than a large one.
    Quadratic in message count by design — n counts turns, not tokens.
    """
    if not messages or history_bytes(messages) <= limit:
        return messages
    starts = segment_starts(messages)
    if not starts:
        return messages
    for cut in starts[1:]:
        if history_bytes(messages[cut:]) <= limit:
            return messages[cut:]
    return messages[starts[-1]:]


def clip_tool_result(text: str, limit: int = MAX_TOOL_RESULT_BYTES) -> str:
    """Bound ONE tool result — a `read_file` of a large file or a `fetch` of a
    large page would otherwise enter the transcript, and kv, whole.

    Keeps the head and states what was withheld: a silent truncation would let
    the model reason about a prefix as if it were the whole thing.

    The byte budget is honoured unconditionally. At a limit too small to even
    hold the notice (operator misconfiguration) the clip goes silent rather
    than over-budget — there is no room left to be honest in.
    """
    raw = text.encode()
    if len(raw) <= limit:
        return text
    note = f"\n…[clipped: {limit} of {len(raw)} bytes shown]"
    keep = limit - len(note.encode())
    # errors="ignore" drops a trailing multi-byte char the cut would split
    if keep < 0:
        return raw[:max(0, limit)].decode(errors="ignore")
    return raw[:keep].decode(errors="ignore") + note


def decode_frames(data: bytes):
    """Frame stream -> [("text", str) | ("tool_use", id, name, input_dict) |
    ("other", str)] — the host-side half of the bridge protocol."""
    blocks, i = [], 0
    while i < len(data):
        tag = data[i:i + 1]
        n = int(data[i + 1:i + 9])
        payload = data[i + 9:i + 9 + n]
        assert len(payload) == n, "truncated frame"
        i += 9 + n
        if tag == b"t":
            blocks.append(("text", payload.decode()))
        elif tag == b"u":
            tu_id, name, raw_input = payload.split(b"\x1f", 2)
            try:
                tool_input = json.loads(raw_input)
            except json.JSONDecodeError:
                tool_input = None  # dispatch surfaces this as is_error
            blocks.append(("tool_use", tu_id.decode(), name.decode(), tool_input))
        else:
            blocks.append(("other", payload.decode()))
    return blocks


class SessionStore:
    """Durable conversation history, kv-backed. One file per session named
    sha256(session).kv (the sigil kv on-disk layout), holding the JSON message
    list. The host owns this state; a restart resumes from it."""

    def __init__(self, kv_dir):
        self.dir = Path(kv_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self.dir / (hashlib.sha256(session_id.encode()).hexdigest() + ".kv")

    def load(self, session_id: str) -> list:
        p = self._path(session_id)
        if not p.exists():
            return []
        return json.loads(p.read_bytes() or b"[]")

    def save(self, session_id: str, messages: list) -> None:
        # atomic replace so a crash mid-write never leaves a torn session
        tmp = self._path(session_id).with_suffix(".kv.tmp")
        tmp.write_bytes(json.dumps(messages, ensure_ascii=False).encode())
        tmp.replace(self._path(session_id))


class PiAgent:
    def __init__(self, endpoint, api_key, store, sandbox_root, manifest_path=None,
                 model="claude-sonnet-5", max_tokens=1024, mcp=None, net_allowlist=None,
                 max_history_bytes=MAX_HISTORY_BYTES,
                 max_tool_result_bytes=MAX_TOOL_RESULT_BYTES):
        self.endpoint = endpoint
        self.api_key = api_key
        self.store = store
        self.sandbox_root = Path(sandbox_root)
        self.model = model
        self.max_tokens = max_tokens
        # M8: transcript bounds. Both are enforced host-side on the way into
        # the payload AND on the way into kv, so neither the request nor the
        # persisted session can grow without limit.
        self.max_history_bytes = max_history_bytes
        self.max_tool_result_bytes = max_tool_result_bytes
        # Host allowlist the `{NET_ALLOWLIST}` grant token expands to. Empty by
        # default => any net tool (e.g. `fetch`) is FAIL-CLOSED until an operator
        # opts in — no SSRF to internal/localhost from a fresh deployment.
        self.net_allowlist = list(net_allowlist or [])
        self.grant_log = []  # (tool, grants) for the LAST turn — tests assert minimality
        # Per-session lock: turns to the SAME session serialize (the kv
        # read-modify-write is not atomic), while different sessions run
        # concurrently under the ThreadingHTTPServer. Closes the lost-update
        # race two concurrent POST /chat to one session would otherwise hit.
        self._locks = {}
        self._locks_guard = threading.Lock()
        manifest_path = manifest_path or PI_ROOT / "tools" / "manifest.json"
        self.manifest = json.loads(Path(manifest_path).read_text())
        self._mcp = mcp
        self._llm_src = compose_with_stdlib(
            (PI_ROOT / "tools" / "agent_turn.sigil").read_text(), ["http"], SIGIL_ROOT).text
        self._parse_src = compose_with_stdlib(
            (PI_ROOT / "tools" / "parse_reply.sigil").read_text(), ["json"], SIGIL_ROOT).text
        self._host = self.endpoint.split("//")[1].split("/")[0].split(":")[0]

    # ── per-session sandbox ─────────────────────────────────────────────

    def sandbox_for(self, session_id: str) -> Path:
        sb = self.sandbox_root / hashlib.sha256(session_id.encode()).hexdigest()[:16]
        sb.mkdir(parents=True, exist_ok=True)
        return sb

    # ── forge plumbing ──────────────────────────────────────────────────

    def _forge(self, source, input_text, grants, fuel=20_000_000):
        r = self._mcp.forge(source, input=input_text, fuel=fuel, grants=grants)
        if r.get("status") != "ok":
            d = (r.get("diagnostics") or [{}])[0]
            return None, f"{d.get('code')}: {(d.get('message') or '')[:200]}"
        return r["data"]["output_text"], None

    def _llm(self, payload: dict):
        # M5a: the guest gets a PLACEHOLDER, never the key. The host substitutes
        # it inside http::post_secret from the `secret` grant.
        hdrs = "\n".join([
            "x-api-key: {{secret:anthropic}}",
            "anthropic-version: 2023-06-01",
            "content-type: application/json",
        ])
        body = json.dumps(payload, ensure_ascii=False)
        out, err = self._forge(
            self._llm_src, f"{self.endpoint}|{hdrs}|{body}",
            {"net": [self._host], "secret": [f"anthropic={self.api_key}"]})
        if err:
            raise RuntimeError(f"llm forge failed: {err}")
        return out

    def _parse(self, raw_response: str):
        out, err = self._forge(self._parse_src, raw_response, None)
        if err:
            raise RuntimeError(f"parse forge failed: {err}")
        return decode_frames(out.encode())

    # ── tool dispatch (per session sandbox) ─────────────────────────────

    def _dispatch(self, name, tool_input, sandbox: Path):
        entry = self.manifest.get(name)
        if entry is None:
            return f"unknown tool: {name}", True
        if not isinstance(tool_input, dict):
            return "malformed tool input (not a JSON object)", True
        try:
            raw = {a: str(tool_input[a]) for a in entry["args"]}
        except KeyError as e:
            return f"missing tool argument: {e}", True
        # path args are RELATIVE to the session sandbox; the host resolves them
        # to an absolute path. fs_read/fs_write then grant-check it against the
        # sandbox, so `..` or an absolute path escaping the sandbox is a -403.
        for a in entry.get("path_args", []):
            raw[a] = str((sandbox / raw[a]))
        args = [raw[a] for a in entry["args"]]
        # args join on '|'; a pipe in any non-last arg would shift the split
        if any("|" in a for a in args[:-1]):
            return "invalid tool argument: '|' not allowed here", True
        source = (PI_ROOT / entry["source"]).read_text()
        grants = {}
        for kind, values in entry.get("grants", {}).items():
            resolved = []
            for v in values:
                if v == "{NET_ALLOWLIST}":
                    resolved.extend(self.net_allowlist)  # [] => fail-closed
                else:
                    resolved.append(v.replace("{SANDBOX}", str(sandbox)))
            grants[kind] = resolved
        self.grant_log.append((name, grants or None))
        out, err = self._forge(source, "|".join(args), grants or None)
        if err:
            return err, True
        # M8: one oversized result can't blow the transcript (err is already
        # bounded — _forge truncates the diagnostic to 200 chars).
        return clip_tool_result(out, self.max_tool_result_bytes), False

    def tool_specs(self):
        return [e["spec"] for e in self.manifest.values()]

    # ── the loop, per session ───────────────────────────────────────────

    def _session_lock(self, session_id: str):
        with self._locks_guard:
            return self._locks.setdefault(session_id, threading.Lock())

    def turn(self, session_id: str, user_message: str) -> str:
        with self._session_lock(session_id):
            return self._turn_locked(session_id, user_message)

    def _turn_locked(self, session_id: str, user_message: str) -> str:
        messages = self.store.load(session_id)
        sandbox = self.sandbox_for(session_id)
        self.grant_log = []
        messages.append({"role": "user", "content": user_message})
        try:
            for _ in range(MAX_STEPS):
                # M8: bound the transcript before it goes out. Cuts land on
                # turn boundaries, so the payload stays API-valid.
                messages = compact(messages, self.max_history_bytes)
                payload = {"model": self.model, "max_tokens": self.max_tokens,
                           "messages": messages}
                if self.manifest:
                    payload["tools"] = self.tool_specs()
                blocks = self._parse(self._llm(payload))

                assistant_content, tool_results, texts = [], [], []
                for block in blocks:
                    if block[0] == "text":
                        texts.append(block[1])
                        assistant_content.append({"type": "text", "text": block[1]})
                    elif block[0] == "tool_use":
                        _, tu_id, name, tool_input = block
                        assistant_content.append({"type": "tool_use", "id": tu_id,
                                                  "name": name, "input": tool_input})
                        content, is_error = self._dispatch(name, tool_input, sandbox)
                        result = {"type": "tool_result", "tool_use_id": tu_id, "content": content}
                        if is_error:
                            result["is_error"] = True
                        tool_results.append(result)
                    # "other" blocks are dropped from the conversation

                messages.append({"role": "assistant", "content": assistant_content})
                if not tool_results:
                    return "\n".join(texts)   # the finally below persists it
                messages.append({"role": "user", "content": tool_results})
            raise RuntimeError(f"no final answer after {MAX_STEPS} steps")
        finally:
            # persist even a partial/looping conversation so state is never
            # lost — compacted, so the kv value is bounded on every path
            self.store.save(session_id, compact(messages, self.max_history_bytes))


# ── HTTP front ───────────────────────────────────────────────────────────


def serve(agent: PiAgent, host="127.0.0.1", port=8080):
    """Start an HTTP server exposing POST /chat {session, message} -> {reply}.
    Returns the server (call .shutdown() to stop). Requests are handled on the
    server thread; conversation durability makes concurrent sessions safe."""
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/chat":
                self.send_error(404)
                return
            n = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
                session, message = req["session"], req["message"]
            except (json.JSONDecodeError, KeyError, TypeError):
                self._json(400, {"error": "expected JSON {session, message}"})
                return
            try:
                reply = agent.turn(str(session), str(message))
            except RuntimeError as e:
                self._json(500, {"error": str(e)})
                return
            self._json(200, {"reply": reply})

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    import threading
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("set ANTHROPIC_API_KEY")
    endpoint = os.environ.get("PI_ENDPOINT", "https://api.anthropic.com/v1/messages")
    state_dir = Path(os.environ.get("PI_STATE", PI_ROOT / ".pi-state"))
    store = SessionStore(state_dir / "sessions")
    sandbox_root = state_dir / "sandboxes"
    sandbox_root.mkdir(parents=True, exist_ok=True)
    # comma-separated hosts the `fetch` tool may reach (empty => fetch denied).
    allow = [h for h in os.environ.get("PI_NET_ALLOWLIST", "").split(",") if h]
    with SigilMCP.spawn(SIGIL_ROOT / "target" / "release" / "sigil-mcp") as mcp:
        mcp.initialize()
        agent = PiAgent(endpoint, api_key, store=store, sandbox_root=sandbox_root,
                        mcp=mcp, model=os.environ.get("PI_MODEL", "claude-sonnet-5"),
                        net_allowlist=allow,
                        max_history_bytes=int(os.environ.get(
                            "PI_MAX_HISTORY_BYTES", MAX_HISTORY_BYTES)),
                        max_tool_result_bytes=int(os.environ.get(
                            "PI_MAX_TOOL_RESULT_BYTES", MAX_TOOL_RESULT_BYTES)))
        if os.environ.get("PI_SERVE"):
            port = int(os.environ.get("PI_PORT", "8080"))
            server = serve(agent, port=port)
            print(f"pi m7 — serving POST /chat on 127.0.0.1:{port}; state {state_dir}. Ctrl-C exits.")
            try:
                import time
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                server.shutdown()
            return
        # interactive REPL against a single durable session
        session = os.environ.get("PI_SESSION", "repl")
        print(f"pi m7 — {agent.model} @ {endpoint}; session {session!r}, state {state_dir}. Ctrl-D exits.")
        while True:
            try:
                msg = input("you> ")
            except EOFError:
                break
            try:
                print("pi >", agent.turn(session, msg))
            except RuntimeError as e:
                print("pi > [error]", e)


if __name__ == "__main__":
    main()
