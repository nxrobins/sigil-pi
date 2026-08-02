# SIGIL style guide (oracle-adjudicated)

## v14 workbench authoring — failure modes and the specs that fix them

- **Extern habit**: for anything cross-module, v14 defaults to `extern "C" fn`
  declarations (its http/fs/kv training pattern) — even when told a stdlib
  module exists. For inner-ring stdlib (`json`), the spec must say: "write
  `use sigil::json;` and call `json::parse_field` — do NOT declare extern
  functions". Expect to mechanically rewrite stray externs anyway.
- **Key pointers**: v14 will pass a key's first byte VALUE where a key
  POINTER is expected (`parse_field(b, blen, 116, 4)`). Spell out: allocate
  a buffer, store8 the key bytes, pass the buffer pointer + length.
- **Scope hoisting**: v14 declares unpacked ptr/len pairs inside `else`
  blocks then uses them after — T060. Spell out the sibling-statement
  pattern: `if r < 0 { return r; } else { }` then unpack immediately after.
- **Param mutation**: v14 reassigns fn params (`pos = pos + 1`) — T042.
  Either spec "copy the param into a let mut local first" or fix by
  shadowing (`let mut cur = pos;`).
- **Off-by-one on literals**: digits/divisors in specs get mangled (9-digit
  divisor for an 8-digit field). Put the exact constants in the spec AND
  verify with oracle expects that would catch the drift.
- **Prescriptive control flow wins**: name the flag variables, say when to
  set them, what the loop condition is. Vague specs produce broken
  found/position logic; the same task speced prescriptively passes.
- **Length ceiling**: ~60-line single-purpose tools one-shot reliably;
  ~120-line multi-phase walks need N=4+ sampling at T=0.7, 3600 max
  tokens (2600 truncates), and usually one mechanical repair pass.
- Inner-ring pure tools: `module tool;` + `tool_main(i64, i64) ! { Alloc }`
  — no ring attr, no FFI/Unsafe (E003 if declared).

- Net tools: `#[ring(outer)] #[trusted] module tool;` + `use sigil::http;`; `tool_main` params
  are **i32**, effects `! { NetIO, Alloc, FFI, Unsafe }`.
- `http::get/post` take **i32** (ptr, len); return packed `ptr * 2^32 + len` or negative errno.
- `http::post_hdrs(url_ptr, url_len, body_ptr, body_len, hdrs_ptr, hdrs_len)` — same i32/packing;
  `hdrs` is a newline-separated `Name: Value` blob (8 KB → -431; a line without `:` → -400). Use it
  for authenticated APIs (`x-api-key`, `anthropic-version`). Header values may hold secrets — pass
  the blob straight to the shim and never copy it into output.
- `http::post_secret(...)` — same signature, but the `hdrs` blob carries `{{secret:NAME}}`
  PLACEHOLDERS, not secret values; the host substitutes granted secrets (a `secret` grant) before
  sending, so the secret never enters the guest. Ungranted placeholder → -403, unterminated → -400.
  This is the leak-proof path (M5a): with no secret bytes in the guest, there's nothing to launder.
  Substitution is HEADER-scoped — a placeholder in the body or in user text is sent verbatim.
  Requires `net` + `secret` grants. Operator note: the guarantee holds iff the header template uses
  the placeholder — seeding a real key into `cfg:hdrs` would put it back in the guest.
- **Rings**: `http` is outer-ring (FFI) so an http tool needs `#[ring(outer)] #[trusted]`; `json` is
  inner-ring and an outer tool **cannot** call it directly (R004). One tool can't both do http and
  call `json` — split the JSON parse out (driver-side, or a separate inner-ring forge / a `grant(&cap,…)` bridge).
- The http response and everything unpacked from it is `@Internal`; returning `@Internal` from a tool
  is fine (that's the normal network-data contract) — only `@Secret`/`@SecretCT` returns are gated.
- **No implicit widening**: never mix i64/i32 operands; derive input-side indices by inference
  from the i32 params (`let mut split = input_len;`), widen counts via parallel counters.
- Compose with the stdlib before forging (`compose_with_stdlib(code, ["http"], repo)`).

## Taint gotchas (found the hard way, minimal repro each)

- **`alloc` into a `@Public` binding is T001 once the function has already
  early-returned an `@Internal` value.** Minimal repro: `let mut a: i64 =
  alloc(4);` … `let r: i64 @Internal = json::parse_field(...); if r < 0 {
  return r; } else { }` … `let mut b: i64 = alloc(7);` → T001 on `b`, while the
  identical `a` above the return is fine. `parse_reply.sigil` only avoids it by
  accident of ordering (its one `@Public` alloc precedes its first return; every
  later one is `@Internal`). **Rule: annotate every scratch/key buffer
  `@Internal`.** It costs nothing — these buffers hold or feed `@Internal` data
  anyway — and it is order-independent, so inserting an early return later can't
  retroactively break an allocation above it.
- **Derived index locals inherit the label of what they're initialized from.**
  `let pkg_start: i32 @Internal = ...; let mut v: i32 = pkg_start;` is T001;
  write `let mut v: i32 @Internal = pkg_start;`. The compiler is right and the
  fix is one annotation, but the error names neither the local nor a line.

