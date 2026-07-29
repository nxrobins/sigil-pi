# SIGIL style guide (oracle-adjudicated)

- Net tools: `#[ring(outer)] #[trusted] module tool;` + `use sigil::http;`; `tool_main` params
  are **i32**, effects `! { NetIO, Alloc, FFI, Unsafe }`.
- `http::get/post` take **i32** (ptr, len); return packed `ptr * 2^32 + len` or negative errno.
- `http::post_hdrs(url_ptr, url_len, body_ptr, body_len, hdrs_ptr, hdrs_len)` — same i32/packing;
  `hdrs` is a newline-separated `Name: Value` blob (8 KB → -431; a line without `:` → -400). Use it
  for authenticated APIs (`x-api-key`, `anthropic-version`). Header values may hold secrets — pass
  the blob straight to the shim and never copy it into output.
- **Rings**: `http` is outer-ring (FFI) so an http tool needs `#[ring(outer)] #[trusted]`; `json` is
  inner-ring and an outer tool **cannot** call it directly (R004). One tool can't both do http and
  call `json` — split the JSON parse out (driver-side, or a separate inner-ring forge / a `grant(&cap,…)` bridge).
- The http response and everything unpacked from it is `@Internal`; returning `@Internal` from a tool
  is fine (that's the normal network-data contract) — only `@Secret`/`@SecretCT` returns are gated.
- **No implicit widening**: never mix i64/i32 operands; derive input-side indices by inference
  from the i32 params (`let mut split = input_len;`), widen counts via parallel counters.
- Compose with the stdlib before forging (`compose_with_stdlib(code, ["http"], repo)`).

