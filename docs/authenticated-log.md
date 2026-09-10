# Authenticated opaque-log storage — staged mechanism

Status: storage component verified; a separate [audited-transaction adapter](audited-transactions.md)
now connects actual SIGIL settlement to atomic audit publication. Complete product
audit and readiness remain unimplemented.
The native implementation is `native/store/src/authenticated_log.rs`.
The storage primitive itself introduces no guest/HTTP command or product policy.
The later opt-in trusted CLI/automatic adapter adds a native environment-key loader and
fixed SIGIL transition-audit projection, not a production deployment profile,
guest key API or legacy audit migration. The evidence below is the initial storage
foundation checkpoint; the linked adapter document records its later verification.

## Ownership and publication

`AuthenticatedLog` is a trusted native object bound to one admitted Store boot,
process, signing key, head namespace, entry namespace and explicit chain limits.
It cannot create scopes. Every operation requires existing Store authority; the
two namespaces must be different. Chain identities are 64 lowercase hexadecimal
characters. SIGIL must eventually select/authorize chain identities and event
content, redact sensitive fields, reserve audit allowance, and interpret results.

`append` takes the expected **head-record revision**, opaque UTF-8 payload and a
related Store batch. It checks the authenticated tail and uses the original Store
transaction to publish the next immutable entry, updated authenticated head and
all related mutations together. The related batch cannot touch either log namespace.
Borrowed related data is bounded before cloning; the original batch, value, scope,
capacity and CAS checks remain in force. Checks-only related batches are supported.

A returned `Publication` contains the actual whole-store commit receipt and a
separate checkpoint with `head_revision`, count, byte count and tip. The global
commit number is **not** a head CAS revision. Stale coordinates, denied writes and
capacity failure cannot publish a prefix. Commit uncertainty remains an error;
there is no retry, compensation, repair, reset, truncation or automatic rotation.
An authenticated record is not evidence that its payload describes a real effect.
The future adapter must enforce action/time guards and bind actual execution facts.

## Format and verification

Entries and heads use strict, canonical, versioned UTF-8 JSON with a body and full
HMAC-SHA256 tag. Separate record/head domains and length-delimited namespace/body
context prevent ambiguous concatenation or cross-kind/namespace reuse. The next
entry links to SHA-256 of the complete preceding signed entry. The authenticated
head binds chain identity, limits, count, encoded-entry byte total and final tip.
Unknown/duplicate fields, noncanonical representations and malformed values fail.

`verify` checks a COMPLETE requested chain in one SQLite read transaction. It
checks every tag, sequence, link and byte total against the authenticated head;
it also refuses missing/tombstoned entries and extra entries in that chain's key
range. It returns a checkpoint, not event payloads or a readiness verdict. Append
checks the tail only: appending successfully does not establish historical integrity.
Verifying one chain does not establish completeness of the entire audit inventory.

The distinct `inspect_existing` operation has the same complete bounded check for
an existing chain. `None` means no head (revision zero) and no entries in its exact
chain prefix were observed in that read snapshot; it is not verified empty history.
Tombstones and orphan records fail. Supplying an expected checkpoint forbids absence.
It never initializes, repairs or rotates a chain. Without independent inventory or
checkpoint evidence, complete erasure/pre-initialization rollback is indistinguishable
from first use. Automatic startup uses this observation before dispatch; it is not
the product readiness interpreter. The original `verify` remains strict on absence.

Limits are explicit engineering ceilings, not approved pilot allowances:

- Key buffer: 32–1,024 bytes; payload: at most 16,384 UTF-8 bytes.
- Per chain: at most 10,000 entries and 16 MiB of encoded entries, including JSON
  escaping and authentication metadata. The head is separately bounded to 4,096
  bytes and also counts against the unchanged global Store limits.
- Verification uses the existing one-second cooperative deadline and SQLite
  progress budget (8,000,000 steps, sampled at 1,000-step callback granularity),
  with bounded per-record allocations and no success after observed exhaustion.
  This is not a hard interrupt of kernel I/O or an exact total instruction counter.

The retained key buffer is private, non-serializable, non-Debug and erased on drop
with `zeroize`. This does not claim erasure of all crypto temporaries or protection
against a compromised signer. The later adapter also keeps keys outside guests;
there is no guest-facing signing or key-loading API.
The implementation uses pinned [RustCrypto HMAC](https://docs.rs/hmac/0.12.1/hmac/)
and its verification API; tests include [RFC 4231](https://www.rfc-editor.org/info/rfc4231/)
and an independently computed Python-standard-library framing/tag/tip fixture.

HMAC detects changes by an actor lacking the key, but not replacement by a complete
older authentic snapshot. Passing an independently retained latest checkpoint can
reject that rollback; keeping it independent is the caller's responsibility. The
tests deliberately demonstrate both sides of this boundary. Missing chains fail
verification, but no self-contained storage mechanism can prove that all traces of
an unknown chain were not removed. This is not an external notary or exactly-once
external-effect protocol.

## Evidence and remaining product work

Full regression run: **267 passed in 763.46 seconds**, session 59339, terminal
`33f127`, observed 2026-09-09 13:42:44 UTC. All original native/evaluator prerequisites
were retained: **120 store + 22 worker + 177 service = 319 native tests**, plus the
complete original fixed evaluator and optimized service build. The store includes
17 added test functions, one of which is the controlled child-process entrypoint.

Evidence covers actual kills before/after commit with matching old/new audit and
domain state, restart, stale/denied transaction coordinates, tenant-namespace and
chain separation, tampering with valid unkeyed storage checksums, malformed/unsigned
records, tombstones/orphans, key/limit mismatch, independently checkpointed rollback,
deadline/work-budget failure and handler cleanup, and read-only verification of
1,000 records with 1,024-byte payloads within the native verification budget.
This does not qualify maximum-volume throughput or the MVP load test.

Development caught a head-CAS/global-receipt mix-up, now covered by an interleaved
write regression. Initial compile issues were corrected. A 50-row budget fixture
did not cross SQLite's existing callback granularity; it was increased to 300 rows
without weakening the work limit, and the complete suite was rerun successfully.

Tested source aggregate, identical before/after (documentation follows):
`fd1f0a84075822054d467dbeb7f1fe16a3cec14fb25608ad849af0f408be9a10`.
Native source SHA-256:
`93090dab0a2bde713dc5df50e275c46587806d65d21f98a1b5a8ea724c7a8725`.
Optimized staged host SHA-256:
`919ca797a7fc03a57e8bc904af5ce93425e5f7ee7a88ab4dc1ea79a9791cc8d0`.

The store adds cached pinned `hmac=0.12.1` and `zeroize=1.9.0`; its lock also gains
`subtle=2.6.1` through the digest MAC feature. The service lock gains the same new
dependencies. No existing package version, worker lock, SIGIL runtime pin, original
Store limit, frozen-v8 fixture or SIGIL entry/evaluation limit changed.

The later adapter supplies a first transition-only schema and trusted key/artifact/
namespace binding. Still required: a reviewed complete product event/redaction
policy; actual model/tool/failure auditing; audit reservations and
settlement; complete chain inventory/checkpoint/freshness policy; explicit retention,
archival and legacy-format migration; runtime observations and final SIGIL readiness.
Do not omit required event fields, truncate chains, silently rotate, reduce the
supported task surface or substitute constant health values to fit these bounds.
Main remains v8, the stage still uses 218/AC1 diagnostics, and no M0–M8 gate is PASS.

The later automatic-integration checkpoint passed **433 behavioral and 324 native
tests**, including three new optional-inspection native tests. The linked adapter
document records exact run identities, the full admission/restart coverage and a
corrected test-configuration sharing failure. This does not update the historical
foundation hashes above or qualify complete inventory, freshness or retention.
