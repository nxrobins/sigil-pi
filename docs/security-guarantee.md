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
- **M6** catches the *aliased* memory launder via intra-procedural **alias
  analysis**: each `alloc` is a region, a pointer carries its source's region,
  `store8` taints the region, and every pointer in that region reads the taint.
  So `let q = out; store8(out, secret); return q` is now `T001`
  (`aliasing_turn.sigil` fails to forge — it was M5b's documented xfail; M6
  flipped it green). Region-based, so rebinding `out` to a fresh alloc drops
  the old region — an alias of the old value stays clean, no false positive.
- **Load-then-return is already caught**: `load8(secret_ptr)` lubs the pointer's
  taint into the loaded value, so returning it is `T001`. Laundering needed
  `store8` specifically because that's where value-taint fell off into memory.

## What is still open (the honest boundary)

Layer 2 is now a **stronger bar, but still not a whole-program proof**. M6's
alias analysis is **intra-procedural**:

- **Interprocedural aliasing escapes.** Pass the destination through a function
  (`let q = identity(out); store8(out, secret); return q`) and `q` gets no
  region — the call result is untracked — so it launders. `interproc_turn.sigil`
  still forges — kept as a **strict xfail** so the gap stays visible.
- **Pointer-through-memory** (store a pointer's bytes, reload them) is a second,
  more exotic frontier — though reconstructing a full pointer from byte-granular
  `load8` is impractical in real tools.

Closing the interprocedural case needs **region summaries** (does function `F`
return a pointer into its argument's region?) — a whole-program points-to
analysis. That's the same shape of work M6 did intra-procedurally, one scope
up. So the trajectory holds: each layer of the gap is **closeable through
language development** (M5b → M6 walked two of them), but full soundness over a
flat, pointer-addressable memory is **not intrinsically preventable by
value-level types** — it's an ever-receding frontier of analysis, not a wall
the type system reaches on its own. That was the original question, and after
M6 it stands answered the same way, with the frontier pushed one scope further.

## Bottom line

- **The api key**: provably safe, because it is structurally absent from the
  guest (Layer 1). It does **not** depend on Layer 2's completeness.
- **In-guest secrets generally**: direct flows, naive memory launders (M5b),
  and *intra-procedural aliased* launders (M6) are caught; interprocedural
  aliasing is the remaining tracked gap. Use Layer 1 (host injection) whenever a
  secret merely needs to reach a sink, and reserve in-guest secrets for cases
  that genuinely compute over secret bytes — where the remaining Layer-2 gap is
  a known, tracked risk.

The right slogan is therefore not "the compiler proves it," but: **the key
never enters the guest, and for secrets that must, the compiler proves the
direct and naive-indirect flows and flags the rest.**
