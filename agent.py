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


def _env_flag(name: str, default: bool) -> bool:
    """A boolean operator knob. An unset variable takes the default; anything
    set is read permissively, because an operator who wrote PI_AUDIT=false
    meant off and should not have to discover that only '0' counts."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def verify_audit_dir(audit_dir) -> dict:
    """Re-walk every chain in an audit directory.

    This is the half that makes the log an artifact rather than a diary: a
    record nobody can check is decoration. Reports per-file so one torn or
    tampered chain names itself instead of failing the whole run anonymously,
    and a torn file is a REPORTED problem, not an exception that aborts
    verification of the chains beside it."""
    audit_dir = Path(audit_dir)
    report = {"ok": True, "chains": 0, "records": 0, "problems": []}
    if not audit_dir.exists():
        return report
    log = AuditLog(audit_dir, enabled=False)  # reader only; creates nothing
    for path in sorted(audit_dir.glob("*.jsonl")):
        report["chains"] += 1
        try:
            records = log._read_path(path)
        except ValueError as e:
            report["ok"] = False
            report["problems"].append((path.name, str(e)))
            continue
        report["records"] += len(records)
        ok, problem = verify_chain(records)
        if not ok:
            report["ok"] = False
            report["problems"].append((path.name, problem))
    return report


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


# ── the proof-carrying dispatch log (M13) ───────────────────────────────
#
# Every guest execution goes through ONE function (`PiAgent._forge`), so
# recording there makes gaps structurally impossible: there is no way to run a
# guest without producing a record. Each entry names the code that ran (source
# hash), what it was permitted to touch (grants), and the data boundary
# (input/output hashes), and carries the hash of the entry before it.
#
# WHAT THIS BUYS: "every tool ran under a minimal manifest" is a claim about
# the code. This makes it a claim about a specific EXECUTION, checkable by
# someone who does not trust the operator — which a container cannot offer,
# because a container has no idea what ran inside it.
#
# HASHES, NOT CONTENTS. Storing raw inputs would make the log a second copy of
# every conversation and a second place for a secret to sit. Hashing preserves
# verifiability — given a claimed input you can prove it produced a given
# output — without the log becoming the liability it exists to reduce.
#
# TWO HONEST BOUNDARIES, both irreducible without external anchoring:
#   - the FINAL record has nothing after it to link against, so tampering
#     there (or truncating the tail) leaves a chain that still verifies. A
#     valid prefix is indistinguishable from the whole. Detecting it needs the
#     head hash held somewhere the writer cannot reach.
#   - the chain proves internal CONSISTENCY, not authorship: anyone with write
#     access can rewrite it coherently. Signing the head closes that, and the
#     `prev_hash` field is exactly what a signature would cover.

GENESIS_HASH = "0" * 64


def redact_grants(grants):
    """Strip secret VALUES, keep secret NAMES.

    The LLM forge's grants literally contain the api key
    (`{"secret": ["anthropic=sk-ant-..."]}`), so writing them verbatim would
    turn an audit feature into a key-disclosure bug. Which secret a forge could
    reach is precisely what an auditor needs; the value is precisely what they
    must never see. Non-secret grants (net hosts, fs roots, kv namespaces) are
    the record and pass through unchanged.

    Returns a NEW structure — the caller's dict is live state that also gets
    forged."""
    if not grants:
        return grants
    out = {}
    for kind, values in grants.items():
        if kind == "secret":
            # `name=value` -> `name=<redacted>`; a bare grant has no value half
            out[kind] = [f"{v.split('=', 1)[0]}=<redacted>" for v in values]
        else:
            out[kind] = list(values)
    return out


def entry_hash(record: dict) -> str:
    """The hash the NEXT record links to. Canonical (sorted-key, tight
    separator) JSON so the digest depends on content, never on key order or
    whitespace."""
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode()).hexdigest()


def verify_chain(records):
    """Re-walk a chain. Returns (ok, problem) — `problem` names the first
    break, because "something is wrong somewhere" is not an audit result."""
    expected = GENESIS_HASH
    for i, r in enumerate(records):
        if r.get("prev_hash") != expected:
            return False, (f"record {i} (seq {r.get('seq')}) does not link to "
                           f"its predecessor — the chain is broken here")
        if r.get("seq") != i:
            return False, f"record {i} has seq {r.get('seq')} — records missing or reordered"
        expected = entry_hash(r)
    return True, None


class AuditLog:
    """Append-only, per-session, hash-chained. One `.jsonl` per session named
    sha256(session) — the SessionStore layout, so no raw session id ever
    enters a filesystem path.

    Append rather than atomic-replace: a torn final line is DETECTABLE
    (invalid JSON) and leaves everything before it intact, which is the right
    failure mode for a log. `read` raises on a torn line rather than dropping
    it — silently discarding it would hide exactly the truncation an auditor
    is looking for.

    Deliberately UNBOUNDED, breaking the M8 pattern on purpose: a log that
    silently drops entries is worthless, and truncating one would destroy the
    chain. Growth is ~400 bytes per forge; rotation is operator policy, and
    rotating means archiving a chain segment, never deleting from the middle.
    """

    def __init__(self, audit_dir, enabled: bool = True):
        self.dir = Path(audit_dir)
        self.enabled = enabled
        if self.enabled:
            self.dir.mkdir(parents=True, exist_ok=True)
        # Turns to one session already serialize under PiAgent's per-session
        # lock, but this store must be safe for any future caller outside that
        # lock — the append and the tail-read that precedes it are one
        # critical section.
        self._lock = threading.Lock()

    def _path(self, session_id: str) -> Path:
        return self.dir / (hashlib.sha256(session_id.encode()).hexdigest() + ".jsonl")

    def read(self, session_id: str) -> list:
        return self._read_path(self._path(session_id))

    def _read_path(self, p: Path) -> list:
        if not p.exists():
            return []
        records = []
        for n, line in enumerate(p.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"{p.name}: truncated/torn record at line {n} — the "
                    f"{len(records)} record(s) before it are intact and still "
                    f"verify, but the tail was lost mid-write") from e
        return records

    # A record is a few hundred bytes; 64 KiB of tail holds the last line with
    # enormous margin, and reading a fixed window is what keeps appends O(1).
    _TAIL_WINDOW = 64 * 1024

    def _last_entry(self, path: Path):
        """The final record, WITHOUT parsing the whole file.

        Appending needs exactly two facts from the log — the last hash and the
        next sequence number — and re-reading everything to get them made each
        append O(n) and a session O(n²) (0.13ms/record at n=50, 1.02ms at
        n=800). Reading a bounded tail window makes it O(1) in log length, and
        unlike an in-memory cache it stays correct for a fresh instance
        appending to an existing log."""
        size = path.stat().st_size
        if size == 0:
            return None
        with path.open("rb") as f:
            window = min(size, self._TAIL_WINDOW)
            f.seek(size - window)
            tail = f.read(window)
        lines = [ln for ln in tail.split(b"\n") if ln.strip()]
        if not lines:
            return None
        try:
            return json.loads(lines[-1])
        except json.JSONDecodeError as e:
            # Chaining onto a torn line would bury the truncation under
            # valid-looking links — refuse instead.
            raise ValueError(
                f"{path.name}: torn/truncated final record — refusing to append "
                f"onto it. The records before it are intact; archive the file "
                f"and start a fresh chain.") from e

    def append_or_raise(self, session, kind, source, input_text, output, err,
                        grants, fuel) -> None:
        """Append one forge, surfacing any failure. `record` is the wrapper
        callers use; this is the honest one, split out so tests can observe
        failures that must not propagate into a live turn."""
        path = self._path(session)
        with self._lock:
            prior = self._last_entry(path) if path.exists() else None
            entry = {
                "seq": 0 if prior is None else prior["seq"] + 1,
                "session_sha256": hashlib.sha256(session.encode()).hexdigest(),
                "kind": kind,
                "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "input_sha256": hashlib.sha256((input_text or "").encode()).hexdigest(),
                "input_len": len((input_text or "").encode()),
                "output_sha256": (None if output is None
                                  else hashlib.sha256(output.encode()).hexdigest()),
                "output_len": None if output is None else len(output.encode()),
                "error": err,
                "grants": redact_grants(grants),
                "fuel": fuel,
                "prev_hash": GENESIS_HASH if prior is None else entry_hash(prior),
            }
            with path.open("a") as f:
                f.write(json.dumps(entry, sort_keys=True,
                                   separators=(",", ":"),
                                   ensure_ascii=False) + "\n")

    def record(self, session, kind, source, input_text, output, err,
               grants, fuel) -> None:
        """Append one forge. Never raises into the caller's path: an audit
        failure must not take down the turn it is describing — but it is loud
        on stderr, because a silently-stopped audit is worse than none."""
        if not self.enabled:
            return
        try:
            self.append_or_raise(session, kind, source, input_text, output,
                                 err, grants, fuel)
        except Exception:  # noqa: BLE001
            traceback.print_exc()


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
                 max_steps=MAX_STEPS, system_prompt=None, llm_retries=2,
                 github_token=None, audit=None):
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
        # Host-held GitHub token for the `{GITHUB_TOKEN}` secret grant. Absent
        # => the grant expands EMPTY, the guest's {{secret:github}} placeholder
        # is ungranted, and the runtime refuses with -403 before the request
        # goes out — fail-closed, exactly like an empty net allowlist. The
        # token never enters a guest either way (M5a host injection).
        self.github_token = github_token
        # M13: the proof-carrying dispatch log. Defaults to a sibling of the
        # sandbox root so a deployment gets the record without opting in — an
        # audit artifact that is opt-in is reliably absent the one time it is
        # needed. `AuditLog(..., enabled=False)` turns it off.
        self.audit = audit if audit is not None else AuditLog(
            self.sandbox_root.parent / "audit")
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

    def _forge(self, source, input_text, grants, fuel=20_000_000,
               kind="unknown", session=None):
        """THE chokepoint. Every guest execution in this host goes through
        here — llm, parse, tool, shape — so the audit record is written here
        and gaps are structurally impossible. `kind` and `session` are
        parameters rather than instance state on purpose: sessions run
        concurrently, and shared mutable context would interleave two turns
        into a record of neither (the bug grant_log already had)."""
        r = self._mcp.forge(source, input=input_text, fuel=fuel, grants=grants)
        if r.get("status") != "ok":
            d = (r.get("diagnostics") or [{}])[0]
            err = f"{d.get('code')}: {(d.get('message') or '')[:200]}"
            out = None
        else:
            out, err = r["data"]["output_text"], None
        if session is not None:
            self.audit.record(session=session, kind=kind, source=source,
                              input_text=input_text, output=out, err=err,
                              grants=grants, fuel=fuel)
        return out, err

    def _llm(self, payload: dict, session=None):
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
                {"net": [self._host], "secret": [f"anthropic={self.api_key}"]},
                kind="llm", session=session)
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

    def _parse(self, raw_response: str, session=None):
        out, err = self._forge(self._parse_src, raw_response, None,
                               kind="parse", session=session)
        if err:
            raise RuntimeError(f"parse forge failed: {err}")
        return decode_frames(out.encode())

    # ── tool dispatch (per session sandbox) ─────────────────────────────

    def _dispatch(self, name, tool_input, sandbox: Path, grant_log: list,
                  session=None):
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
        # bound_args are OPERATOR constants from the manifest (a fixed-host
        # tool's base URL), prepended on the wire ahead of the model's args —
        # the model neither sees nor chooses them. Prepending BEFORE the
        # framing block puts them under the same pipe discipline as any
        # other non-last arg.
        args = [str(b) for b in entry.get("bound_args", [])] + args
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
                elif v == "{GITHUB_TOKEN}":
                    if self.github_token:                # [] => fail-closed
                        resolved.append(f"github={self.github_token}")
                else:
                    resolved.append(v.replace("{SANDBOX}", str(sandbox)))
            grants[kind] = resolved
        grant_log.append((name, grants or None))
        out, err = self._forge(source, input_text, grants or None,
                               kind=f"tool:{name}", session=session)
        if err:
            return err, True
        # Pipeline stage 2 (M12): shape the granted stage's output in a
        # SECOND forge with no grants at all — the parse_reply discipline,
        # generalized. `http` is outer-ring and `json` is inner-ring (R004),
        # so a digested net tool is necessarily fetch→shape; keeping the
        # shaper grantless means untrusted upstream bytes are parsed by a
        # guest that cannot touch fs, net, or kv even if the parse goes wrong.
        if entry.get("shape"):
            shape_src = (PI_ROOT / entry["shape"]).read_text()
            if "use sigil::json;" in shape_src:
                shape_src = compose_with_stdlib(shape_src, ["json"], SIGIL_ROOT).text
            grant_log.append((f"{name}.shape", None))
            out, err = self._forge(shape_src, out, None,
                                   kind=f"shape:{name}", session=session)
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
                blocks = self._parse(self._llm(payload, session=session_id),
                                     session=session_id)

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
                        content, is_error = self._dispatch(
                            name, tool_input, sandbox, grant_log,
                            session=session_id)
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
    state_dir = Path(os.environ.get("PI_STATE", PI_ROOT / ".pi-state"))

    # `--verify-audit` re-walks the chains and exits non-zero on any break.
    # Deliberately BEFORE the api-key check and the toolchain spawn: verifying
    # a record is pure file reading, and an auditor should never need a key,
    # a network, or a built compiler to check it.
    if "--verify-audit" in sys.argv:
        report = verify_audit_dir(state_dir / "audit")
        for name, problem in report["problems"]:
            print(f"BROKEN {name}: {problem}", file=sys.stderr)
        print(f"{report['chains']} chain(s), {report['records']} record(s): "
              f"{'OK' if report['ok'] else 'PROBLEMS FOUND'}")
        sys.exit(0 if report["ok"] else 1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("set ANTHROPIC_API_KEY")
    endpoint = os.environ.get("PI_ENDPOINT", "https://api.anthropic.com/v1/messages")
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
                        github_token=os.environ.get("PI_GITHUB_TOKEN"),
                        audit=AuditLog(state_dir / "audit",
                                       enabled=_env_flag("PI_AUDIT", True)),
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
