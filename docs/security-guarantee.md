# Where the api-key non-leakage guarantee stands (after M5)

The headline claim is *"the compiler proves the API key can't reach the
transcript."* After milestones 4–5 that claim is **true for the api key, but
not for the reason the slogan implies** — and it's worth being precise about
what proves what, because the honest story is stronger than the slogan and the
slogan alone would overclaim.

## Two independent layers

### Layer 1 — Structural: the key is never in the guest (M5a)

For the real tools (`chat_turn`, `agent_turn`) the api key is a host-held
`secret` grant. The guest builds a header **template** with a
`{{secret:anthropic}}` placeholder and hands it to `http::post_secret`; the
**host** substitutes the real key inside the shim, after the guest can no
longer touch it.

Consequence: **there are no key bytes in guest memory.** Nothing to read,
copy, launder, or exfiltrate — independent of what the guest code does, and
independent of the taint analysis. This is the guarantee the api key actually
rests on, and it is not analysis-dependent.

What it still trusts (the irreducible surface):
- **the host** — it holds the key and does the substitution;
- **the granted endpoint** — the key is sent to exactly the `net`-granted host
  (you are, after all, giving Anthropic your key on purpose);
- **operator config** — the guarantee holds *iff* `cfg:hdrs` carries the
  placeholder, not a real key. Seeding a literal key back into cfg would put it
  in the guest. Guards: `test_no_tool_ships_a_literal_key_in_headers`, the
  discipline guards, and the operator note in `docs/style.md`. This is a
  config-trust boundary we can flag but not fully prevent from inside a tool.
- **grant scoping** — the `secret` grant is attached ONLY to the LLM-call
  forge, never to dispatched tools (M3). A `read_file`/`write_file` tool cannot
  even name the secret.

### Layer 2 — Type system: for any secret that IS in the guest (M4 + M5b)

If a tool genuinely needs a secret in guest memory (chat_turn does not), the
taint checker is the defense. Its lattice is `Public < Internal < Secret <
SecretCT`; a value that would carry a secret to a lower-classified sink is
`T001` at compile time.

- **M4** catches direct data-flow: returning the secret, assigning it into a
  returned value, arithmetic on it. (`leaky_turn.sigil` fails to forge.)
- **M5b** catches the naive memory launder: `store8(out, secret); return out`
  now raises `out`'s taint, so the return is `T001`. (`laundering_turn.sigil`
  fails to forge — it was M4's documented xfail; M5b flipped it green.)
- **Load-then-return is already caught**: `load8(secret_ptr)` lubs the pointer's
  taint into the loaded value, so returning it is `T001`. Laundering needed
  `store8` specifically because that's where value-taint fell off into memory.

## What is still open (the honest boundary)

Layer 2 is a **raised bar, not a proof**. It remains scalar-surface: it tracks
values, not memory contents. M5b closes the common launder by raising the
*store destination's base local*, but:

- **Pointer aliasing escapes.** Alias the destination before the store
  (`let q = out; store8(out, secret); return q`) and the alias keeps its old
  Public taint. `aliasing_turn.sigil` still forges — kept as a **strict
  xfail** so the gap stays visible.
- **Non-local-rooted destinations escape** — a `store8` through a
  select/ternary pointer has no single base local to raise.

Closing these needs what we deliberately did not build: **alias analysis**
(which stores a load/return might observe) or a **typed/opaque-memory model**
(no forgeable integer pointers). Both fight SIGIL's raw-byte programming model,
which the tools need. So this is closeable *through language development* — M5b
is a step of exactly that — but **not intrinsically preventable by value-level
types over a flat, pointer-addressable memory.** That was the original
question, and it stands answered: fixable by development, not by types alone.

## Bottom line

- **The api key**: provably safe, because it is structurally absent from the
  guest (Layer 1). It does **not** depend on Layer 2's completeness.
- **In-guest secrets generally**: direct flows and naive memory launders are
  caught (Layer 2); aliased-pointer launders are not. Use Layer 1 (host
  injection) whenever a secret merely needs to reach a sink, and reserve
  in-guest secrets for cases that genuinely compute over secret bytes — where
  the remaining Layer-2 gap is a known, tracked risk.

The right slogan is therefore not "the compiler proves it," but: **the key
never enters the guest, and for secrets that must, the compiler proves the
direct and naive-indirect flows and flags the rest.**
