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
- [ ] **1b — real LLM call**: blocked on outbound request **headers** (`x-api-key`,
      `anthropic-version`). In progress as a SIGIL runtime extension (`http` shim +
      stdlib wrapper) — tracked in the SIGIL repo.
- [ ] **2 — serve-native pi**: `service.json` with `POST /chat` → `agent_turn.sigil`;
      sessions via `kv` (json v2 envelopes).
- [ ] **3 — tool dispatch**: parse `tool_use` blocks; each tool a separate forged program
      with its own manifest (`read_file`: fs, `write_file`: fs_write, `search`: net…).
- [ ] **4 — taint-proofed secrets**: API key as `@Internal` input; declassification audit.

## Requirements

A SIGIL checkout with the toolchain built (`cargo build --release -p sigil-mcp`, and for
milestone 2+, `-p sigil-serve`) on the branch carrying `json` v2 + `kv` + `sigil-serve`.
Set `SIGIL_ROOT` (defaults to `../SIGIL`).

```bash
# milestone 1a demo (mock endpoint):
cd $SIGIL_ROOT && ( cd bench/fixtures/http && python3 -m http.server 8973 --bind 127.0.0.1 & )
python3 drive.py
```

## Developing with v14

SIGIL code here is written collaboratively with **v14** (GRPO-trained Qwen-14B, +7pp over
its SFT baseline on the heldout bench) via `bench/scripts/sigil_workbench.py` in the SIGIL
repo — describe the tool, get SIGIL, the solver oracle adjudicates. The hard-won style
guide lives in `docs/style.md`. Expect to supply exact API signatures in the task text and
hand-fix the last ~5%.
