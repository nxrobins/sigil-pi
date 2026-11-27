# Operation submission codec PS1

Status: **IMPLEMENTED COMPONENT / DRAFT APPLICATION CONTRACT**, 2026-09-07.
Source: [SIGIL decoder](../app/pi/submission.sigil), with the shared
[UTF-8 validator](../app/shared/utf8.sigil).
Evidence: [executable tests](../tests/test_submission.py).
This is preparation for `POST /v1/operations`, not a deployed API route.
It does not modify the synchronous `/v1/chat` contract or qualify M0/M1/M2.

## Request body

The UTF-8 JSON object has exactly three string fields. Field order is irrelevant;
keys are compared after JSON escape decoding. Duplicate decoded keys and unknown
fields are errors, including caller-supplied tenant, principal, grants or deadline.

| Field | Proposed v1 constraint |
|---|---|
| `session` | 1–128 ASCII bytes; first character is a letter/digit, remaining characters may also be `.`, `_` or `-`; matches the existing product session rule |
| `message` | Non-empty string, at most 262,144 decoded UTF-8 bytes; whitespace and escaped control characters are preserved, not trimmed |
| `submission_key` | 1–128 ASCII bytes with the same character rule as `session`; client-generated uniqueness within the authenticated submission namespace, not a credential or server operation ID |

The entire encoded body, including whitespace and escapes, is at most 1,048,576
bytes, matching the existing product request ceiling. A decoded message fitting its
own limit can still exceed the body limit when JSON-escaped. Limits apply before
any effect or durable acceptance, and do not constitute a reservation by themselves.
The added submission-key rule still requires M0 profile/compatibility approval.

Raw UTF-8 must be well formed: continuation bytes, overlong sequences, encoded
surrogates and values above U+10FFFF are rejected. JSON escapes use the pinned
SIGIL codec, including proper surrogate pairing. Raw string control bytes, trailing
garbage, malformed escapes, trailing commas and type coercion are not accepted.
The decoder only accepts this flat string schema; it is not a general JSON parser.

## Canonical component output

The successful result contains these bytes in this exact order:

1. Four ASCII bytes `PS1` followed by LF (`50 53 31 0a` in hexadecimal).
2. Eight zero-padded decimal digits of the decoded session's UTF-8 byte length,
   followed by that session's bytes.
3. The same length/value frame for the message.
4. The same length/value frame for the submission key.

There are no separators or terminators beyond those specified. Lengths count
bytes, not characters; the fixed framing permits messages containing arbitrary
valid Unicode, LF, NUL, apparent headers or apparent JSON structure without field
confusion. The marker is a codec version, not an application or authority identity.
The output is bounded by 262,428 bytes under the field limits above.

Equivalent JSON escaping, whitespace and field ordering produce identical output.
No Unicode normalization is performed: composed and decomposed scalar sequences
remain different payloads. Consumers must decode lengths strictly and require exact
end-of-buffer; arbitrary PS1-looking caller data is not proof of running this decoder.

This encoding is not yet the durable operation record. Operation identity/payload
binding must additionally include the authenticated application, tenant/principal,
schema/artifact/contract and authority context defined in
[the execution contract](execution-contract.md). The host must not accept a caller's
precomputed frame/digest as authorization. Repeated canonical payloads are only
deduplicated once the durable acceptance protocol actually exists.

## Application errors

These are negative SIGIL return codes, not HTTP statuses. The authenticated SIGIL
API handler must map them to the appropriate versioned response envelope. Directly
routing the decoder with sigil-serve's default error mapping would yield HTTP 500
for these codes and is **not** the intended product API.

| Return code | Application error | Intended HTTP status |
|---|---|---|
| -40001 | `invalid_json` | 400 |
| -40002 | `invalid_request` | 400 |
| -40003 | `invalid_session` | 400 |
| -40004 | `invalid_message` | 400 |
| -40005 | `invalid_submission_key` | 400 |
| -40006 | `duplicate_field` | 400 |
| -41301 | `request_too_large` | 413 |
| -41302 | `message_too_large` | 413 |

When several errors are present, the first bounded validation failure wins; this
does not promise a diagnostic for every malformed field. No input bytes are put
into error messages. The test harness checks actual R803 tool-return diagnostics,
so a compiler rejection or fuel exhaustion cannot pass as a validation result.

## Evidence and remaining integration

Use [the fixed application build recipe](../scripts/compose_application.py) with
component `submission` and the pinned stdlib. The authored source contains a
`UTF8_VALIDATOR` marker; composing only the JSON library is no longer sufficient.
The recipe accounts for both authored files and the final compiler input in its
hashes. Hashes describe build inputs, not approved execution authority.

108 tests pass through the pinned solver-verifying forge, including a 60-example
Unicode/property test, all six field orderings, escaped duplicate keys, all field
types, size boundaries and fourteen malformed raw UTF-8 sequences reconstructed in
guest memory by a test-only adapter. One of the 108 is a source-boundary guard.
Tests supply no I/O grants. This is component evidence, not browser/HTTP, credential,
grant, durable-submission, deduplication or candidate evidence.

The input/body decoder must run on the authenticated SIGIL API path and feed the
same durable acceptance decision for browser and direct clients. The current
Python production service remains unchanged. Freeze this codec and the rest of M0
with the runtime/control-plane review before candidate qualification; version any
incompatible encoding change rather than reinterpreting already stored bytes.
