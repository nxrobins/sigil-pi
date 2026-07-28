# SIGIL style guide (oracle-adjudicated)

- Net tools: `#[ring(outer)] #[trusted] module tool;` + `use sigil::http;`; `tool_main` params
  are **i32**, effects `! { NetIO, Alloc, FFI, Unsafe }`.
- `http::get/post` take **i32** (ptr, len); return packed `ptr * 2^32 + len` or negative errno.
- **No implicit widening**: never mix i64/i32 operands; derive input-side indices by inference
  from the i32 params (`let mut split = input_len;`), widen counts via parallel counters.
- Compose with the stdlib before forging (`compose_with_stdlib(code, ["http"], repo)`).

