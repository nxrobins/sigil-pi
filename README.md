# sigil-pi

The [pi](https://github.com/earendil-works/pi) agent framework, rebuilt in
[SIGIL](https://github.com/nxrobins/sigil) — **the agent framework where the sandbox is the
type system.** pi gets isolation from Docker/Gondolin; sigil-pi gets it from the compiler:

- every tool runs behind the **forge gate** — compiled, capability-checked, Z3-verified,
  fuel-bounded, dead in milliseconds
- tools carry **minimal capability manifests** (`net` host patterns, `kv` namespaces,
  `fs` roots) — a tool without a grant gets `-403` from the language, not from a policy file
- secrets are **taint-tracked** (`@Internal`); the compiler proves the API key can't reach
  the transcript
- there is **no bash tool and there never can be** — that's the identity, not a gap

**Status: milestones 1a–18 complete; the authenticated, tenant-scoped `/v1` product host
(`product_service.py`) is internal alpha.** The agent: `POST /chat {session, message}`
runs a durable, session-isolated tool-using loop — each step a sandboxed forge (authenticated
LLM call with a host-injected key that never enters a guest → inner-ring `parse_reply` → each
`tool_use` under its own minimal grant manifest, in a per-session fs sandbox), with history
persisted in kv so a restart resumes mid-conversation and **bounded** so it can't grow into the
kv cap, with a toolset (read/write/append/edit files, list/grep single dirs or whole trees,
fetch, and the AXI pipeline tools `npm_info` / `gh_issues` / `gl_issues`) each behind its own
minimal grant. Around that loop: every forge lands in a signed, proof-carrying audit chain an
outsider can check without a key or a toolchain (`--verify-audit`), `{SECRET:name}` hands a
tool only the credentials it names, scheduled entries fire **ordinary** turns, bounded recall
arrives from a host-owned memory sidecar, and the HTTP front is behind a bearer token that a
non-loopback bind cannot be started without. The product host adds what one shared token
cannot: per-tenant credentials with scopes and tool policy, durable quotas, a hard turn
deadline, observability, retention, backup/restore and a deterministic release bundle — its
fail-closed release record is `docs/product-readiness.md`. 786 tests + 1 honest xfail
(research-only), `./ci.sh` is the mandatory source gate. See the milestones below,
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

### Scheduled runs (M15) — `Scheduler` in `agent.py`'s host

sigil-serve has always implemented scheduling, but it drives **one forged tool** — which on
that stack means `chat_turn`, the single-turn path. Scheduling the *agent loop* needed a
scheduler here, and now has one: durable entries (name, session, message, `every_ms`) with
last-run marks that survive a restart, driving **ordinary turns** — so every step is still a
sandboxed forge under its own manifest. The scheduler adds a trigger, not a privilege.

Two properties naive schedulers get wrong, both pinned against an injected clock rather than
sleeps: a host down for six hours fires a five-minute job **once** and resumes the cadence
(due-ness is a boolean, not a backlog), and a turn that outruns its own interval **does not
stack with itself**. Manage it with `POST /schedule`, `/schedule/list`, `/schedule/remove` —
absent entirely (404) when no scheduler is configured, since an endpoint that 500s is worse
than one that isn't there.

Both properties are **per-process**, which is why a state directory admits **one live host**:
a second `agent.py` on the same `PI_STATE` would see the same due entries and fire them too,
with each process's no-overlap set blind to the other. Startup takes an `flock` on the state
dir and a second host is refused naming the holder — flock rather than a pidfile, so a
crashed host leaves nothing stale to delete. `--verify-audit` deliberately does not take it:
auditing must work against a live host.

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
| `npm_info` | `net` (registry.npmjs.org) | npm package digest — version, license, deps (two-stage: fetch → shape) |
| `gh_issues` | `net` (api.github.com) + `secret` | open issues for a repo — count + comment counts; token host-injected, denied without one |
| `gl_issues` | `net` (gitlab.com) + `secret` | open issues for a GitLab project — same shape, second provider on the same mechanism |

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

- [x] **12 — AXI tools: pipeline dispatch + `npm_info` + `gh_issues`** — digested
      third-party API tools, written **entirely in SIGIL**. `http` is outer-ring and
      `json` is inner-ring (R004), so a tool that fetches *and* digests cannot be one
      forge; it is necessarily two. The manifest gained `shape` (a second forge whose
      input is stage 1's output and whose grants are **None always** — the
      `parse_reply` discipline, so upstream bytes are parsed by a guest that cannot
      touch fs, net or kv even if the parse goes wrong) and `bound_args` (operator
      constants prepended on the wire, so a fixed-host tool gets its base URL from the
      manifest rather than the model — and points at a mock in tests). `npm_info`
      whitelist-validates the package name and does its own `%2f` encoding;
      `gh_issues` sends `authorization: bearer {{secret:github}}` through
      `http::post_secret`, so the **token is never in the guest** (M5a, a second
      secret on the same proven path) and an unconfigured `PI_SECRET_GITHUB` is a
      clean `-403` before any request goes out. Its shaper checks GraphQL `errors`
      **before** `data`, because a 200-with-errors is the commonest real failure and
      walking `data` first would blame the parser for "no such repo". Compression is
      the point: express goes 3508 bytes → 395, left-pad 1571 → 70. Guards generalized
      to the class — every tool source is scanned for any known credential prefix, and
      any tool building an auth header must use `post_secret`, never `post_hdrs`.

- [x] **13 — the proof-carrying dispatch log** (`agent.py`: `AuditLog` + `verify_chain`):
      *"every tool ran under a minimal manifest"* is a claim about the code; this makes
      it a claim about a specific **execution**, checkable by someone who doesn't trust
      the operator. Every guest goes through one function (`_forge`), so recording
      there makes gaps **structurally impossible** — a guard pins `_forge` as the sole
      caller of `_mcp.forge`, because a second call site would silently run an
      unaudited guest. Each entry names the code (source hash), what it was permitted
      to touch (grants), and the data boundary (input/output hashes), and carries the
      hash of the entry before it: editing, deleting, or reordering any record breaks
      verification, property-tested over every field with an anti-vacuity case.
      **Secret values are redacted, names kept** — the LLM forge's grants literally
      contain the api key, so logging them raw would turn an audit feature into a
      key-disclosure bug; a runtime canary asserts the key reaches no audit file.
      Hashes not contents, so the log never becomes a second copy of the conversation.
      Check it with `python3 agent.py --verify-audit` — no key, no network, no
      toolchain, because an auditor should need none of them. Honest boundaries: the
      **final** record has nothing after it to link against (a valid prefix is
      indistinguishable from the whole, so tail truncation needs an externally-held
      head), and the chain proves consistency, not authorship — signing the head is
      the follow-up. The research primitive deliberately never truncates: a log that
      silently drops entries is worthless, and truncating one would destroy the chain.
      The authenticated product host instead reserves and settles a durable per-tenant
      audit-growth quota before accepting work, so this property cannot create unbounded
      product storage.

- [x] **14 — one secret mechanism, and a signed audit chain**: `{GITHUB_TOKEN}` was right for
      one provider and calcifies at three, so `{SECRET:name}` replaces it — reading
      `PI_SECRET_<NAME>`, with every M12 property preserved (value never in a guest,
      unconfigured means an empty grant and a `-403` before any request leaves) plus a new one:
      **a tool gets only the secrets it names**, so configuring three credentials doesn't hand
      all three to every tool. And M13's chain gained signatures (`PI_AUDIT_KEY`,
      HMAC-SHA256): a *coherent* forgery — editing a record and re-linking every downstream
      hash — passes the unsigned check and **fails** the signed one. Honest boundary: HMAC is
      symmetric, so this defends the record against someone who reaches the storage, not
      against the host at the moment of writing.
- [x] **15 — scheduled agent turns**: the open milestone above, closed. Durable entries firing
      **ordinary turns**, so the scheduler adds a trigger, not a privilege. Two properties
      naive schedulers get wrong are pinned against an injected clock: a six-hour outage fires
      a five-minute job **once** (due-ness is a boolean, not a backlog), and a turn that
      outruns its interval **doesn't stack with itself**.
- [x] **16 — `gl_issues`**: GitLab on the generalized secret mechanism, second provider, no new
      code path. Uses GraphQL rather than the REST issues endpoint for a reason worth
      recording: at the pinned toolchain the only shim that carries a host-injected secret is
      `http_post_secret` — there is **no** `http_get_secret` — so an authenticated AXI tool
      must target a POST-shaped API until the runtime grows one.
- [x] **17 — cognitive memory** (`agent.py`: `PiMemory` + the [wave-memory](
      https://github.com/nxrobins/wave-agent) sidecar): pi finally remembers what M8
      deliberately forgets. The sidecar is HOST-OWNED state like kv — a sibling subprocess
      spawned the way sigil-mcp is, never a guest — holding **no secrets and no transports**:
      when a dream cycle wants a model, it asks the host (a `model_request` callback), and the
      host answers by forging the same two proven guests every turn rides — `agent_turn`
      under net + host-injected secret, `parse_reply` under no grants — so **memory's
      completions enter the signed audit chain like any other step** (session `__memory__`).
      Each turn: bounded recall injected as a system suffix (never persisted into history;
      `PI_MEMORY_BUDGET` / `PI_MEMORY_BLOCK_BYTES`) — labeled **untrusted** in the prompt,
      because recalled text originates in past user messages and model output, and an
      unlabeled recall would launder the user channel into instruction space — and the
      conversational spine recorded on turn success. Fail-OPEN in the loop — a busy, dreaming, or dead sidecar degrades recall
      to nothing and buffers records (bounded), never failing a turn — and fail-CLOSED at
      configuration: unset means no memory at all, and a configured sidecar that refuses to
      start (e.g. `PI_MEMORY_SCOPE` flipped against an existing store) is a startup error
      carrying the sidecar's own remediation text, because every record is scope-stamped and
      reshaping a store is an explicit `migrate`, never a flag flip. Consolidation cadence
      follows M15's discipline (due-ness is a boolean; a cycle cannot stack with itself).
      The production protocol is now explicitly **v2**: startup negotiates the
      version and probes a host-served 384-dimensional embedding model before
      accepting traffic. The Rust sidecar still owns no transport—pi answers
      `embedding_request` callbacks through an operator-configured,
      credential-free loopback OpenAI-compatible endpoint. Model identity is
      stamped into the index; `reindex` safely rebuilds it after a model swap.
      `health`, bounded `inspect`, `list_sessions`, session-mode `forget`, and
      restart-safe `consolidate_all` complete the operator lifecycle. The hash
      embedder remains explicit development-only behavior.
      Tested at both layers: the client hermetically against a scripted protocol double, the
      loop wiring over real forges — including that a memoryless deployment sends exactly the
      payloads it always sent.

- [x] **18 — the HTTP perimeter** (`agent.py`: `check_auth` + `bind_is_loopback`): every
      guest in this host is sandboxed, capability-checked and audited; the front door was
      outside all of it. `POST /chat` took a caller-named session id from anyone who could
      reach the port, spent the api key, and returned that session's history — disclosed in
      a docstring rather than defended. `PI_AUTH_TOKEN` is now required on **every** route,
      compared with `hmac.compare_digest` (a token checked with `==` leaks its prefix
      through timing), and checked **before routing** so a route added later inherits it by
      construction — the argument `_forge` makes for the audit log, applied to the door. The
      rule that makes forgetting it hard: **a non-loopback bind without a token is refused at
      startup**, the same fail-closed shape as an empty allowlist denying `fetch`. `PI_BIND`
      ships in the same change on purpose — until now `host` was not operator-configurable at
      all, so adding the knob without the credential would have turned a code edit into a
      one-variable mistake. Loopback stays open: a local REPL is not exposure, and demanding
      a credential for it would only teach people to set a dummy one. Honest boundary, written
      down rather than left to be assumed: **one token is one PRINCIPAL**. This is
      authentication, not authorization — every holder can name any session id, and per-caller
      isolation needs named principals and a per-principal session key. Not a rate limit
      either. See `docs/security-guarantee.md`.

## Requirements

sigil-pi needs a **forge**: a `sigil-mcp` binary built from SIGIL, and SIGIL's `stdlib`
beside it. Both are runtime requirements, not build-time ones — the agent composes
`agent_turn` and `parse_reply` against the stdlib on its first turn and each shape tool on
first dispatch, so a binary without a stdlib is not a usable toolchain and `toolchain.py`
refuses to resolve one.

SIGIL is open source: `git clone https://github.com/nxrobins/sigil ../SIGIL` puts a checkout
where path 3 below expects it, and `SIGIL_REV` names the exact commit to check out.

**How** you have that forge is a deployment detail. `toolchain.py` resolves it from three
arrangements, first match winning, and names every path it tried when it finds none:

| | arrangement | how |
|---|---|---|
| 1 | **explicit** | `PI_FORGE_BIN` + `PI_STDLIB_DIR` (optionally `PI_SERVE_BIN`) |
| 2 | **installed release** | `PI_TOOLCHAIN_DIR`, else `~/.cache/sigil-pi/<ref>` — layout `bin/sigil-mcp`, `bin/sigil-serve`, `stdlib/` |
| 3 | **source checkout** | `SIGIL_ROOT` (default `../SIGIL`) with `cargo build --release -p sigil-mcp -p sigil-serve --features sigil-mcp/solver,sigil-serve/solver` |

Today path 3 is the one everyone uses, and it is the developer path permanently — `./ci.sh`
requires it, because it rebuilds the compiler at the pin. Paths 1 and 2 exist and work now
so that publishing SIGIL is a packaging change rather than a rewrite; nothing above
`toolchain.py` knows which one answered.

**Solver-verifying builds only.** The default `cargo build -p sigil-mcp` is a solver-*off*
compiler: Z3 never runs, and its forge gate fails closed (`R817`) unless the caller sets
`SIGIL_ALLOW_UNVERIFIED_CERT=1`. SIGIL's bench harness sets that — it benchmarks model
output and is not a security gate. sigil-pi's vendored client (`runtime_client.py`) strips
it on purpose, so every forge here is discharged by Z3 or refused; the `--features` above
are not optional, and `./ci.sh` and `scripts/build_release.py` both pass them. That needs
a Z3 with headers: `brew install z3` is found automatically; otherwise export
`Z3_SYS_Z3_HEADER=<z3.h>` and `LIBRARY_PATH=<dir with libz3>`. CI pins the official Z3
4.12.2 release, SHA256-verified, exactly as SIGIL's own solver lane does. The resulting
binary links `libz3` dynamically, so a host that runs it must provide that library.

The Python side has **no third-party runtime dependencies** — stdlib only. `pip install -e .`
gets you the host and a `pi` entry point; it does not get you a toolchain.

```bash
# ── THE DEPLOYABLE AGENT (M7–M8) — the tool-using loop. Needs sigil-mcp. ──
cargo build --release -p sigil-mcp -p sigil-serve \
    --features sigil-mcp/solver,sigil-serve/solver   # in $SIGIL_ROOT, once (path 3; needs Z3)
export ANTHROPIC_API_KEY=sk-ant-...
PI_SERVE=1 python3 agent.py               # POST /chat {session, message}
curl -H 'content-type: application/json' \
     -d '{"session":"s1","message":"hello"}' http://127.0.0.1:8080/chat
python3 agent.py                          # ...or omit PI_SERVE for a REPL

# Optional: PI_PORT, PI_MODEL, PI_STATE (kv + sandboxes — ONE live host per
#   state dir, enforced by an flock at startup; a second host is refused
#   naming the holder pid, because every no-overlap guarantee is per-process),
# PI_SESSION (REPL),
# PI_NET_ALLOWLIST (hosts `fetch` may reach — EMPTY MEANS fetch IS DENIED),
# PI_MAX_HISTORY_BYTES / PI_MAX_TOOL_RESULT_BYTES (M8 transcript bounds),
# PI_MAX_STEPS (LLM round-trips one turn may spend; default 8),
# PI_TURN_DEADLINE_S (wall-clock budget for ONE turn, checked at step
#   boundaries on the monotonic clock; 0/unset = no deadline. A caller who
#   disconnects mid-turn also stops the turn at the next boundary. Send
#   `Accept: text/event-stream` on POST /chat for step-level progress: one
#   SSE `step` event per completed tool round, `reply`/`error` terminal),
# PI_SYSTEM (system prompt — deployment identity, rides every request),
# PI_SYSTEM_FILE (project-instructions file appended after PI_SYSTEM;
#   default <repo>/AGENTS.md, loaded only if present — the pi convention),
# PI_LLM_RETRIES (host-side retries of a transient-failed LLM call — 429 or
#   5xx/transport, never a grant denial; default 2, backoff 0.5s then 2s),
# PI_SECRET_<NAME> (host-injected credentials for `{SECRET:name}` grants —
#   e.g. PI_SECRET_GITHUB for gh_issues. UNSET MEANS THE TOOL IS DENIED; the
#   value never enters a guest, which only ever names a placeholder),
# PI_AUDIT (the proof-carrying dispatch log; ON by default, PI_AUDIT=0 off),
# PI_AUDIT_KEY (HMAC key signing each audit record — held OUTSIDE the audit
#   dir, so a coherent rewrite needs the key too. Unset = unsigned: the chain
#   still catches a careless edit, not a competent forgery),
# PI_MEMORY_SIDECAR (path to the wave-memory sidecar binary — UNSET MEANS NO
#   MEMORY, like an empty allowlist means no fetch; a configured sidecar that
#   refuses to start is a loud startup error),
# PI_MEMORY_EMBEDDER (callback by default; production. `hash` is an explicit
#   development-only fallback and is never semantic retrieval),
# PI_MEMORY_EMBEDDING_URL (required for callback mode; credential-free
#   OpenAI-compatible `/v1/embeddings` endpoint on localhost/loopback only),
# PI_MEMORY_EMBEDDING_MODEL (stable model identity sent to the endpoint and
#   stamped into the recall index; default bge-small-en-v1.5),
# PI_MEMORY_EMBEDDING_TIMEOUT (local embedding request timeout in seconds;
#   default 10),
# PI_MEMORY_SCOPE (session = one store per session, preserving pi's
#   isolation invariant; shared = one store, cross-session recall — flipping
#   the flag on existing data is REFUSED with the migrate command named),
# PI_MEMORY_BUDGET (recall token budget per turn; default 512),
# PI_MEMORY_BLOCK_BYTES (byte cap on the injected block; default 8192),
# PI_MEMORY_CONSOLIDATE_EVERY (dream-cycle cadence in seconds; 0/unset =
#   never — consolidation runs only when an operator opts in),
# PI_AUTH_TOKEN (bearer credential required on every HTTP route; UNSET MEANS
#   NO AUTHENTICATION, which is why a non-loopback bind without it is refused),
# PI_BIND (address to bind, default 127.0.0.1 — anything not provably loopback
#   needs PI_AUTH_TOKEN or the host exits with the reason),
# PI_HTTP_LOG (structured JSON request log to stdout, default ON for a served
#   host; session ids appear only as sha256[:12] hashes — the kv naming rule —
#   and GET /health serves liveness + request counters + token usage).

# production memory example (the endpoint must already be serving the named
# 384-dimensional model on loopback):
export PI_MEMORY_SIDECAR=/path/to/wave-memory-sidecar
export PI_MEMORY_EMBEDDING_URL=http://127.0.0.1:8083/v1/embeddings
export PI_MEMORY_EMBEDDING_MODEL=bge-small-en-v1.5
export PI_MEMORY_SCOPE=session

# check the audit chains — no key, no network, no toolchain needed:
python3 agent.py --verify-audit
#
# DEPLOYMENT NOTE (M18): POST /chat requires `Authorization: Bearer $PI_AUTH_TOKEN`
# on EVERY route when a token is configured, and a non-loopback PI_BIND without
# one is REFUSED at startup — the same fail-closed shape as an empty net
# allowlist denying `fetch`. The loopback default stays open: a local REPL is
# not exposure. Honest boundary: one token is one PRINCIPAL, so authentication
# answers "may you talk to this host", not "which sessions are yours" — every
# holder can name any session id. See docs/security-guarantee.md.

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
# python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.lock
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

**The host declares what it is.** Since SIGIL's CSIR v9 verifier (2026-09-02), a host
operation's occurrence is Public unless the host declares a profile, and a tool that makes a
host call inside a branch on an `@Internal` value — the previous call's error code, the shape
of every tool here — is refused (`I013`) as leaking Internal control to an undeclared host.
So every forge names the `ephemeral` profile (`runtime_client.HOST_PROFILE`) and every
sigil-serve config carries `"host_profile": "ephemeral"`; a guard pins both. The bump to
the public toolchain found this the first time the gate ran against it — `SIGIL_REV` has
the story.

**Two pin modes, because there are two ways to have a toolchain.** Tree hashes can only be
checked by someone who can *clone* SIGIL — which, while SIGIL was private, is exactly why
sigil-pi was uninstallable outside it. SIGIL has been public since 2026-09-04
([nxrobins/sigil](https://github.com/nxrobins/sigil)), so anyone can check them now. An
installed toolchain has no tree to hash, so `SIGIL_REV` also
accepts `sha256_<platform>` keys naming the digest of a published binary. Which check runs is
decided by what actually resolved: `ci.sh` checks tree hashes because it rebuilds from source,
and `main()` checks the sha256 because it runs whatever was installed. No `sha256_*` key is
published yet — SIGIL's source is public but its releases carry no toolchain binaries — so an
installed toolchain is currently **unverified**, the honest state until SIGIL ships binaries.

CI (`.github/workflows/ci.yml`) is split accordingly: a **standalone** job runs everything that
needs no toolchain, and the **forge** job runs the real `./ci.sh` gate against the pinned ref,
checked out anonymously from the public `nxrobins/sigil` (it needed a repo-read secret while
SIGIL was private; it has run the full gate on every PR and push to main since 2026-08-02).
The forge job is **mandatory**: it has no job-level gate, and a pinned ref that is not
reachable in the public repo is a red failure that names itself before the checkout — never
a skipped job, because branch protection counts a skipped required check as satisfied, and a
release gate that can be satisfied by not running is not a gate. (It used to skip visibly
instead; the product-readiness record reversed that on purpose.) One lesson is recorded in
`SIGIL_REV`: GitHub repository names are case-insensitive, so when the private repo was
renamed and the public export took its old name, a checkout spelled the old way silently
pointed at the public repo — at a ref only the private one had.

Since the toolchain resolves lazily, the standalone job also **runs the tests that never
forge** — audit-chain math, compaction, scheduler timing, the memory client against its
protocol double, the product service against scripted agents. They were previously
unreachable to anyone without a SIGIL clone, not for any reason of their own but because
`conftest.py` imported it at module scope. The forge job sets `PI_REQUIRE_TOOLCHAIN=1`, under
which a missing toolchain is an **error rather than a skip**: otherwise the job that claims to
run the forge tests could report green having run none of them. `./ci.sh` additionally
enforces an independent line/branch coverage gate with a 100% requirement on the inventoried
security boundaries (see `docs/product-readiness.md`).

## Developing with v14

SIGIL code here is written collaboratively with **v14** (GRPO-trained Qwen-14B, +7pp over
its SFT baseline on the heldout bench) via `bench/scripts/sigil_workbench.py` in the SIGIL
repo — describe the tool, get SIGIL, the solver oracle adjudicates. The hard-won style
guide lives in `docs/style.md`. Expect to supply exact API signatures in the task text and
hand-fix the last ~5%.
