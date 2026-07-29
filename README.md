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

**Status: milestones 1a–4 complete.** One agent turn is: forge `agent_turn`/`chat_turn`
(authenticated LLM call, api key `@Secret`-proved non-leaking) → forge `parse_reply`
(inner-ring JSON walk) → forge each `tool_use` (own minimal grant manifest). 50 tests,
`./ci.sh` is the gate. See milestones below and `docs/style.md` for the v14 authoring notes.

## Architecture (v2 — on the sigil-serve platform)

```
                    ┌────────────── sigil-serve (host) ──────────────┐
POST /chat ─────────▶ route ─▶ forge agent_turn.sigil ─▶ response    │
                    │            │ kv: session state (namespace grant)│
                    │            │ http: LLM call (host-pattern grant)│
                    │            ▼                                    │
                    │        tool_use? ─▶ forge tools/<tool>.sigil    │
                    │                     (its own minimal grants)    │
 schedule ──────────▶ background agent runs (durable marks)          │
                    └─────────────────────────────────────────────────┘
```

One agent turn = one ephemeral run. The host owns every long-lived concern; the guest owns
none. Sessions live behind `kv` grants; the LLM call is an outbound `http::post` under a
`net` grant scoped to exactly one API host.

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
      sweep); `./ci.sh` is the gate. Known M2 limits: sessions assume a single
      writer (kv read-modify-write, last write wins), history grows unbounded
      (5 MB kv cap ends a session), and the reply scanner requires compact JSON
      with the first `"text"` field being the reply — all retired by M3's ring
      bridge + tool dispatch.
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
      Run it: `ANTHROPIC_API_KEY=… PI_SANDBOX=/some/dir python3 agent.py`.
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

## Requirements

A SIGIL checkout with the toolchain built (`cargo build --release -p sigil-mcp`, and for
milestone 2+, `-p sigil-serve`) on the branch carrying `json` v2 + `kv` + `sigil-serve`.
Set `SIGIL_ROOT` (defaults to `../SIGIL`).

```bash
# milestone 1a demo (mock endpoint):
cd $SIGIL_ROOT && ( cd bench/fixtures/http && python3 -m http.server 8973 --bind 127.0.0.1 & )
python3 drive.py

# milestone 1b — real authenticated LLM call (needs http::post_hdrs in the toolchain):
cargo build --release -p sigil-mcp        # in $SIGIL_ROOT, once
export ANTHROPIC_API_KEY=sk-ant-...
SIGIL_ROOT=$SIGIL_ROOT python3 chat.py

# milestone 2 — serve-native (also needs sigil-serve built):
# seed kv cfg (url/hdrs/pre/post/uo/ao/cl — see tests/conftest.py CFG_KEYS;
# values are files named sha256(key).kv), write a service.json with net +
# kv cfg/sess grants routing POST /chat -> chat_turn, then:
$SIGIL_ROOT/target/release/sigil-serve service.json
curl -d 'mysession|hello' http://127.0.0.1:PORT/chat

# the full local CI gate (regen check, compile gate, 26 tests):
./ci.sh
```

## Developing with v14

SIGIL code here is written collaboratively with **v14** (GRPO-trained Qwen-14B, +7pp over
its SFT baseline on the heldout bench) via `bench/scripts/sigil_workbench.py` in the SIGIL
repo — describe the tool, get SIGIL, the solver oracle adjudicates. The hard-won style
guide lives in `docs/style.md`. Expect to supply exact API signatures in the task text and
hand-fix the last ~5%.
