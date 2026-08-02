# sigil-pi

The [pi](https://github.com/earendil-works/pi) agent framework, rebuilt in
[SIGIL](https://github.com/nxrobins/SIGIL) — **the agent framework where the sandbox is the
type system.** pi gets isolation from Docker/Gondolin; sigil-pi gets it from the compiler:

- every tool runs behind the **forge gate** — compiled, capability-checked, Z3-verified,
  fuel-bounded, dead in milliseconds
- tools carry **minimal capability manifests** (`net` host patterns, `kv` namespaces,
  `fs` roots) — a tool without a grant gets `-403` from the language, not from a policy file
- secrets are **taint-tracked** (`@Internal`); the compiler proves the API key can't reach
  the transcript
- there is **no bash tool and there never can be** — that's the identity, not a gap

**Status: milestones 1a–8 complete.** The deployable agent: `POST /chat {session, message}`
runs a durable, session-isolated tool-using loop — each step a sandboxed forge (authenticated
LLM call with a host-injected key that never enters a guest → inner-ring `parse_reply` → each
`tool_use` under its own minimal grant manifest, in a per-session fs sandbox), with history
persisted in kv so a restart resumes mid-conversation and **bounded** so it can't grow into the
kv cap, with a growing toolset (read/write/append/edit files, list/grep single dirs or whole
trees, fetch) each behind its own minimal grant. 179 tests + 1 honest xfail, `./ci.sh` is the gate. See the milestones below,
`docs/security-guarantee.md` for where the non-leakage guarantee stands, and `docs/style.md`
for the v14 authoring notes.

## Architecture — two footings, one gate

sigil-pi runs on **two** stacks. They forge every guest through the same gate and differ only
in **who orchestrates**. Knowing which is which matters: one is the deployable agent, the
other is a milestone artifact that is still load-bearing for the security proofs.

### The deployable agent (M7–M8) — `agent.py` over sigil-mcp

```
                    ┌────────────────── agent.py (host) ───────────────────┐
POST /chat ─────────▶ per-session lock ─▶ turn loop (≤ MAX_STEPS)          │
{session, message}  │     │  kv: session history — durable, compacted      │
                    │     ▼                                                │
                    │  forge tools/agent_turn.sigil    net + secret        │
                    │  forge tools/parse_reply.sigil   no grants — pure    │
                    │  forge tools/<tool>.sigil        manifest grants,    │
                    │     │                            per-session sandbox │
                    │     └── tool_use? ── loop back ──┘                   │
                    └──────────────────────────────────────────────────────┘
```

`serve()` is a Python `ThreadingHTTPServer`; every **step** is a separate ephemeral forge
driven through **sigil-mcp**. This is what `PI_SERVE=1 python3 agent.py` runs.

**Why the host orchestrates and sigil-serve doesn't.** A forge can't spawn sub-forges or cross
the ring, and sigil-serve routes one request to exactly one forged tool. A multi-step
tool-using loop therefore *cannot be* a sigil-serve route — something outside the sandbox has
to drive it. That's the design, not a shortcut: **the host owns every long-lived concern; the
guest owns none.** Each step it drives is still a sandboxed, capability-checked, fuel-bounded
forge under its own minimal manifest, and the api key is host-injected so it never enters a
guest at all.

### The serve-native single turn (M2) — `chat_turn.sigil` on sigil-serve

```
                    ┌────────────── sigil-serve (host) ───────────────┐
POST /chat ─────────▶ route ─▶ forge chat_turn.sigil ─▶ response      │
<session>|<message> │            kv:     history (cfg + sess grants)  │
                    │            http:   LLM call (net + secret)      │
                    └─────────────────────────────────────────────────┘
```

One turn, **no tool loop**: the entire agent turn is a single forged program, zero host
orchestration. Superseded as a runtime path — it can't dispatch tools — but deliberately kept,
because it is where the **security guarantee is proved**: `test_taint_m4` compiles the real
`chat_turn` and its adversarial fixtures against the taint checker, `test_m5_secret` attacks
the key from a tool holding `chat_turn`'s exact grants, `test_chat_serve` exercises the
full sigil-serve path, and `ci.sh`'s compile gate boots a `chat_turn` service config. It is a
proof carrier, not dead code — and not the thing to deploy.

### Not yet wired: scheduled runs

sigil-serve implements scheduling (`ScheduleEntry`: name, tool, `every_ms`, input, with durable
last-run marks), but it drives **one forged tool** — which on that stack means `chat_turn`, the
single-turn path. Nothing schedules the *agent loop*; that would need a scheduler in
`agent.py`'s host. Tracked as an open milestone rather than drawn as though it exists.

## Tools (`tools/manifest.json`)

Every tool the agent can call is a separately-forged SIGIL program with its **own minimal
grant manifest** — capabilities are per-tool, checked by the language, not a policy file. The
byte-level SIGIL is **v14-authored** via the workbench (see *Developing with v14*), except the
exploration trio (`edit_file`/`list_tree`/`grep_tree`), which is hand-authored to the same
style guide — each file's AUTHORSHIP header says which.

| Tool | Grant | What it does |
|---|---|---|
| `read_file` | `fs` (sandbox) | read a file |
| `write_file` | `fs_write` (sandbox) | create/replace a file |
| `append_file` | `fs` + `fs_write` (sandbox) | append (create if absent) |
| `edit_file` | `fs` + `fs_write` (sandbox) | replace **exactly one** occurrence (refuses ambiguity; len8-framed input, so `old`/`new` may hold any bytes) |
| `list_dir` | `fs` (sandbox) | sorted directory listing (via the `fs_list` runtime shim) |
| `list_tree` | `fs` (sandbox) | recursive sorted listing, dirs marked `name/` |
| `grep_file` | `fs` (sandbox) | lines of a file matching a substring |
| `grep_tree` | `fs` (sandbox) | search every file under a dir — `path:line: text` matches |
| `fetch` | `net` (**allowlist**) | HTTP GET a URL |

- **Sandboxing**: fs tools take paths **relative to the session sandbox**; the host resolves
  them, so `..` and absolute paths that escape are a `-403` from the compiler, and one session
  can't reach another's files.
- **`fetch` is fail-closed (SSRF-safe)**: its `net` grant is the `{NET_ALLOWLIST}` token, which
  the host expands to an **operator-configured host allowlist** (`PI_NET_ALLOWLIST=host1,host2`).
  With no allowlist, `fetch` is denied — a fresh deployment can't be steered into fetching
  internal/localhost URLs. Faithful to the minimal-capability thesis; narrow it per deployment.
- **The allowlist survives redirects.** A grant checked only on the first hop is not a grant:
  since the *model* picks the URL, one open redirect on an allowlisted host would otherwise be
  a full SSRF bypass. Redirect targets are re-validated against the grant, and a redirect on a
  request carrying caller-supplied headers (i.e. the LLM call, whose header template holds the
  injected key) is refused rather than replayed to a new host. Pinned by
  `tests/test_net_grant.py`, which includes an anti-vacuity test so the suite can't start
  reporting safety it has stopped checking.

## Status / milestones

- [x] **1a — turn0** (`tools/turn0.sigil` + `drive.py`): one agent turn (fetch reply,
      assemble JSON envelope), forge-verified byte-exact against a mock endpoint.
      Written ~95% by v14 (the GRPO-trained SIGIL model); see AUTHORSHIP in the file.
- [x] **1b — real LLM call** (`tools/agent_turn.sigil` + `chat.py`): the guest makes
      the authenticated outbound POST to the Anthropic Messages API — `x-api-key` +
      `anthropic-version` headers under a one-host `net` grant — via the new
      `http::post_hdrs` (SIGIL runtime + stdlib; `feat(runtime): http::post_hdrs`).
      Forge-verified end-to-end against a mock endpoint: headers cross the wire, the
      key never enters the output. Response `content[0].text` is extracted driver-side
      — `json` is inner-ring and an http tool is outer-ring, so the guest can't call it
      directly (R004); guest-side parsing waits on a ring bridge (see below).
- [x] **2 — serve-native pi** (`tools/chat_turn.sigil` via `make_chat_turn.py`):
      `POST /chat` with body `<session>|<message>` → one forged agent turn: kv session
      history → in-guest payload assembly (v14-authored `esc_json`) → authenticated
      `http::post_hdrs` → reply extraction (v14-authored `find_text`) → durable kv
      write → escaped reply as the response body. History is stored PRE-ESCAPED so
      replay is byte concatenation. Operator config (endpoint, headers incl. the api
      key, payload framing) lives in the kv `cfg` namespace. TDD: 26 tests green
      (hypothesis property suites for the helpers, full-stack integration + bug
      sweep); `./ci.sh` is the gate. Known M2 limits, each retired later and by
      a different milestone: the reply scanner required compact JSON with the
      first `"text"` field being the reply (**M3**'s ring bridge); sessions
      assumed a single writer, kv read-modify-write, last write wins (**M7**'s
      per-session lock); history grew unbounded, 5 MB kv cap ends a session
      (**M8**'s compaction).
- [x] **3 — tool dispatch + ring bridge** (`agent.py` + `tools/parse_reply.sigil` +
      `tools/manifest.json`): the agent loop where EVERY step is its own ephemeral
      forge with its own minimal manifest — the LLM call (net only), the reply parse
      (inner ring, no grants at all), each dispatched tool (`read_file`: fs,
      `write_file`: fs_write). The ring bridge is the two-forge design (`grant(&cap,…)`
      is capability machinery, not a cross-ring call path): `parse_reply.sigil`
      composes stdlib `json` and walks `content[]` into tagged frames — whitespace
      layouts, full `\uXXXX` decoding, `tool_use` blocks with ids — retiring every M2
      scanner limitation. A sandbox escape (`read_file /etc/hosts`) comes back
      `-403` FROM THE COMPILER and flows to the model as an `is_error` tool_result;
      the loop keeps going. 45 tests green (`./ci.sh`): property suites vs a Python
      reference codec, dispatch integration, host-hardening sweep (malformed
      tool_use input, pipe-in-path rejection, step cap), and a manifest-minimality
      guard (a tool using a capability its manifest doesn't grant fails CI).
- [x] **4 — taint-proofed secrets** (`frag_main.sigil` @Secret channel + `tests/test_taint_m4.py`):
      the api key is read ONLY through a `@Secret`-typed `kv_get` extern, so the key never
      exists as `@Internal` data anywhere in the tool. `tool_main` declares `-> i64 @Internal`,
      so any path that lets a key byte reach the output is **T001 at compile time** — the
      headline claim ("the compiler proves the api key can't reach the transcript") made
      literal. The authenticated POST is a direct `@Internal`-returning extern (the host shim
      consumes the key; the fresh response isn't Secret). Proven both ways: the real
      `chat_turn` compiles, and `fixtures/leaky_turn.sigil` (returns the key) **fails to
      forge**. Honest boundary, kept visible as a strict `xfail`: the checker is
      scalar-surface, so a byte-by-byte memory copy (`fixtures/laundering_turn.sigil`) still
      launders the secret — that flips green the day SIGIL gains memory-taint tracking.
      A guard test pins the `@Secret` discipline so it can't silently rot.
- [x] **5a — host-side key injection** (`http::post_secret` + `secret` grant): the api key
      is no longer in the guest at all. `cfg:hdrs` holds a placeholder template
      (`x-api-key: {{secret:anthropic}}`); the guest passes it to `http::post_secret`, and
      the **host** substitutes the real key (a `secret` grant) before sending — after the
      guest can no longer touch it. Non-leakage is now **structural, not analysis-dependent**:
      there are no key bytes in guest memory to read, copy, or launder. This closes the M4
      memory-laundering gap *for the key* (nothing to launder) and supersedes M4's in-guest
      `@Secret` channel for the real tools; the `@Secret` machinery lives on in the fixtures
      as language-level proofs. Proven adversarially: a tool dumping `cfg:hdrs` gets the
      placeholder; a user typing `{{secret:anthropic}}` into their message can't exfiltrate
      the key (substitution is header-scoped). Runtime shim + `SecretGrant` in SIGIL;
      `chat_turn`/`agent_turn` and the drivers migrated.
- [x] **5b — compiler rule: secret-store taints the pointer**: the *language* grew the
      capability the analysis lacked — `store8` of an `@Secret` value now taints the
      destination pointer's base local (SIGIL compiler + self-hosted taint checker, kept in
      sync by a new differential fixture), so `store8(out, secret); return out` is T001. The
      naive memory-launder that M4 documented as an xfail is now **caught**. Pointer aliasing
      (copy the pointer before the store) remains the honest next boundary — a strict xfail,
      closeable only with alias analysis. **Where the guarantee stands after M5:
      `docs/security-guarantee.md`.**
- [x] **6 — alias analysis** closes the aliasing gap: intra-procedural region-based points-to
      in the SIGIL taint checker — each `alloc` is a region, pointers carry their region,
      `store8` taints the region, every pointer in it reads the taint. So
      `let q = out; store8(out, secret); return q` is now T001 (the M5b xfail flipped green).
      Region-based, so rebinding to a fresh alloc drops the old region — no false positive on
      an alias of the old value (a differential ACCEPT fixture pins this). The frontier is now
      **interprocedural** (a pointer through a function loses its region) — the new strict
      xfail, closeable with region summaries. Guarantee write-up updated in
      `docs/security-guarantee.md`.
- [x] **7 — host-orchestrated agentic loop** (`agent.py`: `SessionStore` + `PiAgent` +
      `serve()`) — *renamed: this milestone shipped as "serve-native", which it is not; it
      forges through sigil-mcp behind a Python HTTP front, and that is the design (see the
      architecture section). M2 is the serve-native path.*
      The deployable agent. `POST /chat {session, message}` runs the **full tool-using loop**
      per session — LLM → `tool_use` → forge tool → `tool_result` → repeat → reply — with
      conversation history persisted in **kv** (`sha256(session).kv`, atomic replace) so a
      **fresh host process resumes mid-conversation**. Each session gets its own fs sandbox
      (`sandbox_root/hash(session)`); tool paths are **relative to it** (manifest `path_args`,
      resolved host-side), so the model never sees host paths, `..` can't climb out (fs grant
      denies it), and one session can't reach another's files. Turns to one session **serialize**
      (per-session lock — closes the lost-update race); different sessions run concurrently.
      The loop is host-orchestrated (a forge can't spawn sub-forges or cross the ring), true to
      "the host owns every long-lived concern; the guest owns none" — but every step is still a
      sandboxed, capability-checked forge, and the api key stays host-injected (never in a guest).
      Run it: `ANTHROPIC_API_KEY=… PI_SERVE=1 python3 agent.py` (HTTP) or plain for a REPL.
- [x] **tools — the agent toolset broadens** (`tools/manifest.json`): `fetch`, `list_dir`,
      `grep_file`, `append_file` join `read_file`/`write_file`, each a separately-forged v14
      program under its **own** minimal grant (see the Tools table above). `fetch` is
      fail-closed: its `net` grant expands from an operator allowlist, so a fresh deployment
      can't be steered into fetching internal URLs.
- [x] **8 — bounded history** (`agent.py`: `compact` + `clip_tool_result`): the last M2 limit
      retired. An unbounded transcript was re-sent in full on **every step of every turn**
      (cost quadratic within a session) and eventually walked into the **5 MB kv value cap**,
      ending the session. Two host-side, deterministic bounds — no summarizer, no extra LLM
      call, no new trust surface: `compact` drops whole **oldest turn-segments** until the
      transcript fits, and `clip_tool_result` bounds a single step (one `read_file` of a large
      file would otherwise land in kv whole, head kept and the cut **announced** so the model
      knows it holds a prefix). Cuts land **only on real-user-turn boundaries**, so a
      `tool_use` is never orphaned from its `tool_result` — the API rejects either orphan, so
      boundary discipline is the correctness property, property-tested over generated
      transcripts. Honest boundary: a single turn larger than the cap is kept **whole and
      over-cap**, because a corrupt transcript is worse than a large one. Bounds are enforced
      on the way into the payload *and* into kv (`PI_MAX_HISTORY_BYTES`,
      `PI_MAX_TOOL_RESULT_BYTES`); a guard pins the defaults safely under the kv cap.
- [x] **9 — system prompt + project context** (`agent.py`: `load_system_prompt`): the model
      finally gets told who it is. Two sources in pi's own layering — `PI_SYSTEM` (deployment
      identity) first, then an `AGENTS.md`-convention file (project instructions living with
      the deployment; `PI_SYSTEM_FILE` points elsewhere) — assembled host-side and sent as the
      Messages `system` field on **every step** of every turn. Bounded like everything that
      enters the payload (`MAX_SYSTEM_BYTES`, guard-pinned under the history cap), but bounded
      **loudly**: an over-cap prompt is a named construction error, never a clip — truncating
      instructions would change their meaning silently. Unconfigured deployments send exactly
      the payload they always sent (no empty `system` field — pinned).
- [x] **10 — the exploration trio** (`tools/edit_file.sigil`, `tools/list_tree.sigil`,
      `tools/grep_tree.sigil`): the agent can finally survey and surgically change its sandbox,
      not just read/write whole files. `edit_file` replaces **exactly one** occurrence and
      refuses ambiguity (461 not-found / 462 not-unique, file untouched on failure) — and it
      rides a new **len8 input framing** (per-arg byte-length prefixes, a manifest opt-in)
      because pipe-joining could never carry an `old` containing `|`. `list_tree`/`grep_tree`
      walk the tree **recursively in-guest** (probe-verified: `fs_list` on an entry answers
      ≥0 for a dir, −404 for a file — the listing carries no type marker), sorted DFS, under
      the same single `fs` grant as their flat siblings. All three are **hand-authored SIGIL**
      (the first here not from v14; AUTHORSHIP headers say so) and all three are
      **differential-tested against Python references** — hypothesis drives random trees,
      bodies, and patterns through the real forges and the outputs must agree byte-for-byte,
      the same guard that caught grep_file's overlap bug.
- [x] **11 — retry + usage** (`agent.py` `_llm` retry loop; `parse_reply.sigil` usage frame):
      operational resilience and metering, both shaped by existing invariants. The LLM call is
      **idempotent**, so the host retries it on transient failures only — 429/5xx (the http
      shim maps a dead host to 502; probe-verified) — with bounded backoff (`PI_LLM_RETRIES`),
      and **never** on a 4xx like -403: retrying a grant denial would blur the fail-closed
      story. Usage rides the **same ring bridge as content**: parse_reply (still the single
      zero-grant parser of the response — the host never json-parses it) emits one `g` frame
      of raw `input_tokens|output_tokens` digit slices, absent-usage emits nothing so old
      payloads stay byte-identical, and the reference codec in the property suite pins it.
      The host accumulates per turn (published race-free like `grant_log`: `last_usage`, plus
      a lifetime `usage_total`) and `POST /chat` now answers `{reply, usage}` — additive, so
      reply-only clients are untouched.

## Requirements

A SIGIL checkout with the toolchain built (`cargo build --release -p sigil-mcp`, and for
milestone 2+, `-p sigil-serve`) on the branch carrying `json` v2 + `kv` + `sigil-serve`.
Set `SIGIL_ROOT` (defaults to `../SIGIL`).

```bash
# ── THE DEPLOYABLE AGENT (M7–M8) — the tool-using loop. Needs sigil-mcp. ──
cargo build --release -p sigil-mcp        # in $SIGIL_ROOT, once
export ANTHROPIC_API_KEY=sk-ant-...
PI_SERVE=1 python3 agent.py               # POST /chat {session, message}
curl -H 'content-type: application/json' \
     -d '{"session":"s1","message":"hello"}' http://127.0.0.1:8080/chat
python3 agent.py                          # ...or omit PI_SERVE for a REPL

# Optional: PI_PORT, PI_MODEL, PI_STATE (kv + sandboxes), PI_SESSION (REPL),
# PI_NET_ALLOWLIST (hosts `fetch` may reach — EMPTY MEANS fetch IS DENIED),
# PI_MAX_HISTORY_BYTES / PI_MAX_TOOL_RESULT_BYTES (M8 transcript bounds),
# PI_MAX_STEPS (LLM round-trips one turn may spend; default 8),
# PI_SYSTEM (system prompt — deployment identity, rides every request),
# PI_SYSTEM_FILE (project-instructions file appended after PI_SYSTEM;
#   default <repo>/AGENTS.md, loaded only if present — the pi convention),
# PI_LLM_RETRIES (host-side retries of a transient-failed LLM call — 429 or
#   5xx/transport, never a grant denial; default 2, backoff 0.5s then 2s).
#
# DEPLOYMENT NOTE: POST /chat is UNAUTHENTICATED and binds 127.0.0.1. The
# guests are sandboxed; the HTTP front is not a security boundary. Keep it
# loopback, or put your own authenticating proxy in front before exposing it.

# ── the M2 serve-native single turn — no tool loop; needs sigil-serve ──
# seed kv cfg (url/hdrs/pre/post/uo/ao/cl — see tests/conftest.py CFG_KEYS;
# values are files named sha256(key).kv), write a service.json with net +
# secret + kv cfg/sess grants routing POST /chat -> chat_turn, then:
$SIGIL_ROOT/target/release/sigil-serve service.json
curl -d 'mysession|hello' http://127.0.0.1:PORT/chat

# ── the early milestone demos, kept runnable ──
cd $SIGIL_ROOT && ( cd bench/fixtures/http && python3 -m http.server 8973 --bind 127.0.0.1 & )
python3 drive.py                          # 1a, mock endpoint
SIGIL_ROOT=$SIGIL_ROOT python3 chat.py    # 1b, one real authenticated call

# the full local CI gate (toolchain pin + binary rebuild at the pin, regen
# check, compile gate, tests):
./ci.sh
```

## The toolchain pin (`SIGIL_REV`)

sigil-pi is **not self-contained**: every tool is forged by a `sigil-mcp`/`sigil-serve`
binary built from a sibling SIGIL checkout. Nothing used to record *which* toolchain, and on
2026-07-31 a rebuild of that sibling turned the whole suite red with no change to sigil-pi at
all — with no way to tell what had moved. `SIGIL_REV` pins the git **tree hashes** of SIGIL's
`crates/` and `stdlib/` (not a commit SHA — SIGIL commits to `bench/` constantly and none of
that can change the binary, so a commit pin cries wolf), and `ci.sh` checks it first. It also
rejects a dirty `crates/`/`stdlib/`, since a binary built from a dirty tree corresponds to no
revision and the pin would be a fiction.

CI (`.github/workflows/ci.yml`) is split accordingly: a **standalone** job runs everything that
needs no toolchain, and the **forge** job runs the real `./ci.sh` gate. Without a
`SIGIL_REPO_TOKEN` secret the forge job is **visibly skipped** at the job level — never a
green tick that ran nothing, and never a red X that trains everyone to ignore it. The pinned
ref is already pushed, so adding that secret is all that remains to enable the real gate; see
the comment at the top of that file.

## Developing with v14

SIGIL code here is written collaboratively with **v14** (GRPO-trained Qwen-14B, +7pp over
its SFT baseline on the heldout bench) via `bench/scripts/sigil_workbench.py` in the SIGIL
repo — describe the tool, get SIGIL, the solver oracle adjudicates. The hard-won style
guide lives in `docs/style.md`. Expect to supply exact API signatures in the task text and
hand-fix the last ~5%.
