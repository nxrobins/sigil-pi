#!/usr/bin/env python3
"""pi host (milestones 7–8) — the host-orchestrated agentic loop, bounded.

NOT serve-native, and deliberately so: this host is a Python HTTP front that
forges each step through sigil-mcp. A forge can't spawn sub-forges or cross
the ring, and sigil-serve routes one request to exactly one forged tool, so a
multi-step tool-using loop cannot BE a route — something outside the sandbox
has to drive it. (tools/chat_turn.sigil is the serve-native path: one turn, no
tool loop. See the README architecture section for the two footings.)

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
import re
import sys
import threading
import time
import traceback
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
MAX_REQUEST_BYTES = 1024 * 1024       # ceiling for ONE /chat request body
MAX_SYSTEM_BYTES = 32 * 1024          # ceiling for the system prompt (rides EVERY request)


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
    the model reason about a prefix as if it were the whole thing. The figure
    in the notice is EXACT — it once claimed `limit` bytes shown while the
    head held `limit - len(notice)` (at worst, zero), and a notice whose whole
    job is honesty must not be off by its own length.

    The byte budget is honoured unconditionally. At a limit too small to even
    hold the notice (operator misconfiguration) the clip goes silent rather
    than over-budget — there is no room left to be honest in.
    """
    raw = text.encode()
    if len(raw) <= limit:
        return text

    def note_for(shown: int) -> str:
        return f"\n…[clipped: {shown} of {len(raw)} bytes shown]"

    # size the cut against the worst-case notice (`shown` can't exceed the
    # limit, so no true figure has more digits), then restate the notice with
    # the byte count the head ACTUALLY has after the cut — which may be
    # smaller still, when the cut lands inside a multi-byte character and
    # errors="ignore" drops the split tail.
    keep = limit - len(note_for(limit).encode())
    if keep < 0:
        return raw[:max(0, limit)].decode(errors="ignore")
    head = raw[:keep].decode(errors="ignore")
    return head + note_for(len(head.encode()))


def _llm_error_code(err: str):
    """The HTTP-ish status inside a forge diagnostic ('tool returned error
    (429)'), or None. The http shim maps non-2xx statuses and transport
    failures (dead host -> 502) onto these codes — probe-verified."""
    m = re.search(r"tool returned error \((\d+)\)", err or "")
    return int(m.group(1)) if m else None


def _llm_error_is_transient(code) -> bool:
    """Worth retrying: rate limits and server/transport failures. NEVER a
    4xx like 403 — that is the runtime refusing a grant, and retrying it
    would blur the fail-closed story."""
    return code is not None and (code in (408, 429) or 500 <= code <= 599)


def load_system_prompt(inline, file_path: Path):
    """Assemble the deployment's system prompt from its two sources, in pi's
    own layering: PI_SYSTEM (deployment identity) first, then the project's
    AGENTS.md-convention file (instructions that live with the deployment;
    PI_SYSTEM_FILE points elsewhere). Absent/blank sources contribute
    nothing; returns None when there is no prompt at all, so unconfigured
    deployments keep sending exactly the payload they always sent."""
    parts = []
    if inline and inline.strip():
        parts.append(inline)
    if file_path is not None and file_path.exists():
        body = file_path.read_text()
        if body.strip():
            parts.append(body)
    return "\n\n".join(parts) if parts else None


def _endpoint_host(endpoint: str) -> str:
    """The bare hostname of the LLM endpoint. It becomes the `net` grant for
    every LLM-call forge, so a parse failure must be LOUD at construction —
    not an IndexError three layers in, and never a silently-empty host whose
    grant can match nothing. Case is preserved: the grant must byte-match the
    URL the guest actually requests."""
    scheme, sep, rest = endpoint.partition("://")
    netloc = rest.split("/", 1)[0].rsplit("@", 1)[-1]
    if netloc.startswith("["):                       # [v6:literal]:port
        host = netloc[1:netloc.index("]")] if "]" in netloc else ""
    else:
        host = netloc.split(":", 1)[0]
    if not sep or scheme not in ("http", "https") or not host:
        raise ValueError(
            f"endpoint must look like http(s)://host[:port]/path — got {endpoint!r}")
    return host


def _env_int(name: str, default: int) -> int:
    """An integer operator knob, or a clear named error — a typo in PI_MAX_*
    must not surface as a bare `invalid literal for int()` traceback."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"{name} must be an integer, got {raw!r}")


def _parse_allowlist(raw: str) -> list:
    """PI_NET_ALLOWLIST, comma-separated. Whitespace is operator noise, not
    part of a hostname — and because `fetch` is fail-closed, a grant of ' b'
    that can never match a URL host is indistinguishable from a deny."""
    return [h.strip() for h in raw.split(",") if h.strip()]


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
        elif tag == b"g":
            # usage frame: '<input_tokens>|<output_tokens>' raw digit slices
            try:
                in_tok, out_tok = payload.split(b"|", 1)
                counts = (int(in_tok), int(out_tok))
                if counts[0] < 0 or counts[1] < 0:
                    raise ValueError("negative token count")
                blocks.append(("usage", counts[0], counts[1]))
            except ValueError:
                # metering: a malformed or negative count is ignored, never
                # folded into the totals
                blocks.append(("other", payload.decode()))
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
                 max_tool_result_bytes=MAX_TOOL_RESULT_BYTES,
                 max_steps=MAX_STEPS, system_prompt=None, llm_retries=2):
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
        # LLM round-trips one turn may spend before the loop gives up.
        self.max_steps = max_steps
        # Deployment identity + project instructions; rides EVERY request.
        # Bounded loudly, not clipped — truncating instructions would change
        # their meaning silently, and the operator can fix a named error.
        if system_prompt is not None and len(system_prompt.encode()) > MAX_SYSTEM_BYTES:
            raise ValueError(
                f"system prompt is {len(system_prompt.encode())} bytes; the cap is "
                f"{MAX_SYSTEM_BYTES} (MAX_SYSTEM_BYTES). Trim PI_SYSTEM / the "
                f"PI_SYSTEM_FILE document — instructions this large belong in tools.")
        self.system_prompt = system_prompt
        # Bounded host-side retry of the (idempotent) LLM call. Transient
        # failures only; the sleeper is injectable so tests never wait.
        if llm_retries < 0:
            raise ValueError(
                f"llm_retries must be >= 0, got {llm_retries} (PI_LLM_RETRIES). "
                f"0 means one attempt and no retry.")
        self.llm_retries = llm_retries
        self._sleep = time.sleep
        # Usage published like grant_log: last COMPLETED turn, wholesale.
        # usage_total is process-lifetime, guarded (sessions run concurrently).
        self.last_usage = {}
        self.usage_total = {"input_tokens": 0, "output_tokens": 0}
        self._usage_lock = threading.Lock()
        # Host allowlist the `{NET_ALLOWLIST}` grant token expands to. Empty by
        # default => any net tool (e.g. `fetch`) is FAIL-CLOSED until an operator
        # opts in — no SSRF to internal/localhost from a fresh deployment.
        self.net_allowlist = list(net_allowlist or [])
        # (tool, grants) of the last COMPLETED turn — tests assert minimality.
        # Published wholesale when a turn ends, never mutated in place: turns
        # to different sessions run concurrently, and a shared mutable list
        # would interleave them into a log of no turn at all.
        self.grant_log = []
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
        self._host = _endpoint_host(self.endpoint)

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
        delay = 0.5
        attempt = 0
        # `while True` on purpose: a `for` over a range can fall off the end
        # and return None if the bound is ever degenerate. Every path out of
        # this loop is an explicit return or raise.
        while True:
            out, err = self._forge(
                self._llm_src, f"{self.endpoint}|{hdrs}|{body}",
                {"net": [self._host], "secret": [f"anthropic={self.api_key}"]})
            if not err:
                return out
            code = _llm_error_code(err)
            if attempt >= self.llm_retries or not _llm_error_is_transient(code):
                raise RuntimeError(f"llm forge failed: {err}")
            attempt += 1
            print(f"llm call failed ({code}); retry {attempt}/"
                  f"{self.llm_retries} in {delay}s", file=sys.stderr)
            self._sleep(delay)
            delay *= 4

    def _parse(self, raw_response: str):
        out, err = self._forge(self._parse_src, raw_response, None)
        if err:
            raise RuntimeError(f"parse forge failed: {err}")
        return decode_frames(out.encode())

    # ── tool dispatch (per session sandbox) ─────────────────────────────

    def _dispatch(self, name, tool_input, sandbox: Path, grant_log: list):
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
            # fs_list joins entry names with '\n', so a name containing one is
            # indistinguishable from two entries and makes list_tree/grep_tree
            # report files that do not exist. The host owns path resolution, so
            # it refuses to CREATE such a name — the only loop the model drives.
            if any(c in raw[a] for c in "\n\r\x00"):
                return (f"invalid tool argument: control character in {a!r} "
                        f"(newlines and NUL are not allowed in paths)"), True
            raw[a] = str((sandbox / raw[a]))
        args = [raw[a] for a in entry["args"]]
        if entry.get("framing") == "len8":
            # 8 decimal digits of BYTE length, then the bytes, per arg —
            # every arg may contain any bytes at all (edit_file's old/new)
            input_text = "".join(f"{len(a.encode()):08d}" + a for a in args)
        else:
            # args join on '|'; a pipe in any non-last arg would shift the split
            if any("|" in a for a in args[:-1]):
                return "invalid tool argument: '|' not allowed here", True
            input_text = "|".join(args)
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
        grant_log.append((name, grants or None))
        out, err = self._forge(source, input_text, grants or None)
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
        reply, _ = self.turn_with_usage(session_id, user_message)
        return reply

    def turn_with_usage(self, session_id: str, user_message: str):
        """The full contract: (reply, usage-dict for this turn)."""
        with self._session_lock(session_id):
            return self._turn_locked(session_id, user_message)

    def _turn_locked(self, session_id: str, user_message: str) -> str:
        messages = self.store.load(session_id)
        sandbox = self.sandbox_for(session_id)
        grant_log = []  # turn-local; published wholesale in the finally
        usage = {"input_tokens": 0, "output_tokens": 0}  # ditto
        messages.append({"role": "user", "content": user_message})
        try:
            for _ in range(self.max_steps):
                # M8: bound the transcript before it goes out. Cuts land on
                # turn boundaries, so the payload stays API-valid.
                messages = compact(messages, self.max_history_bytes)
                payload = {"model": self.model, "max_tokens": self.max_tokens,
                           "messages": messages}
                if self.system_prompt:
                    payload["system"] = self.system_prompt
                if self.manifest:
                    payload["tools"] = self.tool_specs()
                blocks = self._parse(self._llm(payload))

                assistant_content, tool_results, texts = [], [], []
                for block in blocks:
                    if block[0] == "text":
                        texts.append(block[1])
                        assistant_content.append({"type": "text", "text": block[1]})
                    elif block[0] == "usage":
                        # metering, not conversation — never enters history
                        usage["input_tokens"] += block[1]
                        usage["output_tokens"] += block[2]
                    elif block[0] == "tool_use":
                        _, tu_id, name, tool_input = block
                        assistant_content.append({"type": "tool_use", "id": tu_id,
                                                  "name": name, "input": tool_input})
                        content, is_error = self._dispatch(name, tool_input, sandbox, grant_log)
                        result = {"type": "tool_result", "tool_use_id": tu_id, "content": content}
                        if is_error:
                            result["is_error"] = True
                        tool_results.append(result)
                    # "other" blocks are dropped from the conversation

                # An assistant message with EMPTY content is rejected by the
                # API on every later turn (a non-final message must have
                # content), so persisting one bricks the session outright.
                # A content-free response is possible whenever the only frame
                # is usage — drop it rather than poison the transcript.
                if assistant_content:
                    messages.append({"role": "assistant", "content": assistant_content})
                if not tool_results:
                    return "\n".join(texts), dict(usage)  # finally persists it
                messages.append({"role": "user", "content": tool_results})
            raise RuntimeError(f"no final answer after {self.max_steps} steps")
        finally:
            # persist even a partial/looping conversation so state is never
            # lost — compacted, so the kv value is bounded on every path
            self.store.save(session_id, compact(messages, self.max_history_bytes))
            # single rebinds, so a concurrent reader sees a whole turn's worth
            self.grant_log = grant_log
            self.last_usage = dict(usage)
            with self._usage_lock:
                self.usage_total["input_tokens"] += usage["input_tokens"]
                self.usage_total["output_tokens"] += usage["output_tokens"]


# ── HTTP front ───────────────────────────────────────────────────────────


def serve(agent: PiAgent, host="127.0.0.1", port=8080):
    """Start an HTTP server exposing POST /chat {session, message} -> {reply}.
    Returns the server (call .shutdown() to stop). Requests are handled on the
    server thread; conversation durability makes concurrent sessions safe.

    UNAUTHENTICATED — the guests are sandboxed, but the HTTP front is not a
    security boundary. Keep it on loopback (the default bind), or put an
    authenticating proxy in front before exposing it anywhere."""
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/chat":
                self.send_error(404)
                return
            try:
                # Content-Length is client input too: non-numeric, negative,
                # or absurd values are a 400 — not a handler exception (a
                # dropped connection), and never an unbounded read.
                n = int(self.headers.get("Content-Length", 0))
                if not 0 <= n <= MAX_REQUEST_BYTES:
                    raise ValueError(f"Content-Length out of range: {n}")
                req = json.loads(self.rfile.read(n) or b"{}")
                session, message = req["session"], req["message"]
            except (ValueError, KeyError, TypeError):
                # JSONDecodeError is a ValueError, so this covers a malformed
                # body, a malformed length, and a non-object payload alike
                self._json(400, {"error": "expected JSON {session, message}"})
                return
            try:
                reply, usage = agent.turn_with_usage(str(session), str(message))
            except RuntimeError as e:
                # operational failures (step cap, forge errors) — the message
                # is written for the client
                self._json(500, {"error": str(e)})
                return
            except Exception:
                # anything else is a bug: log it server-side, answer with an
                # opaque 500 — never a dropped connection, never internals
                traceback.print_exc()
                self._json(500, {"error": "internal error"})
                return
            self._json(200, {"reply": reply, "usage": usage})

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
    allow = _parse_allowlist(os.environ.get("PI_NET_ALLOWLIST", ""))
    with SigilMCP.spawn(SIGIL_ROOT / "target" / "release" / "sigil-mcp") as mcp:
        mcp.initialize()
        agent = PiAgent(endpoint, api_key, store=store, sandbox_root=sandbox_root,
                        mcp=mcp, model=os.environ.get("PI_MODEL", "claude-sonnet-5"),
                        net_allowlist=allow,
                        max_history_bytes=_env_int(
                            "PI_MAX_HISTORY_BYTES", MAX_HISTORY_BYTES),
                        max_tool_result_bytes=_env_int(
                            "PI_MAX_TOOL_RESULT_BYTES", MAX_TOOL_RESULT_BYTES),
                        max_steps=_env_int("PI_MAX_STEPS", MAX_STEPS),
                        system_prompt=load_system_prompt(
                            os.environ.get("PI_SYSTEM"),
                            Path(os.environ.get("PI_SYSTEM_FILE",
                                                PI_ROOT / "AGENTS.md"))),
                        llm_retries=_env_int("PI_LLM_RETRIES", 2))
        if os.environ.get("PI_SERVE"):
            port = _env_int("PI_PORT", 8080)
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
