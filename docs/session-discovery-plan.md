# Conversation discovery: next implementation slice

Status: **IMPLEMENTED IN DEVELOPMENT PROFILES — integrated local source gate passed**.
This follows [retained history](session-history.md) toward M1/M4 of the
[MVP goal](mvp-goal.md). It does not replace remaining lifecycle, browser or
qualification requirements. The complete main history/API source gate passed at
18:29 UTC on 2026-09-08. The separate native/SIGIL discovery stage subsequently
passed all 94 checks and has now been integrated. [The discovery contract](session-discovery.md)
is the current implementation description. The expanded 2,241-case local source
gate passed at 20:22 UTC on 2026-09-08; Linux candidate and pilot qualification
remain open.

The implementation retains native key bounds and integrity checks and adds explicit store
stdio v2 support plus application-host v5/v6 profiles with AH4/HC4 and a bound command
inventory. Legacy application-host v3/v4 remains a separate unchanged contract.
The SIGIL entry composes to 60,620 bytes and uses four entry evaluations for discovery,
within the existing ceilings. The stage's 94-case suite includes pure entry/projection,
actual HTTP and executable-profile checks. All passed in staging; see the dated
[acceptance record](mvp-acceptance.md) for evidence and the integrated run result.

## Constraints verified before implementation

- The native store already lists bounded, scoped keys in binary lexical order.
  It validates each discovered record, includes tombstones, and returns no values
  or revisions. The automatic coordinator uses this primitive to discover work.
- The original public HTTP action interpreter supported read, call, commit and
  reply, not key enumeration. Its entry loop permits eight evaluations per request.
- Repeatedly reading and projecting a full page of transcripts would exceed that
  step limit or produce unnecessarily large intermediate payloads. Increasing the
  existing limits is not this slice's design.
- Conversation identity is already tenant plus session name. Discovery must use
  the matched tenant state namespace and current `sessions:read`, not operation
  ownership or a namespace from HTTP input.

## Proposed native mechanism

Add a bounded, read-only **record metadata page**, preserving the existing scoped
key enumeration and integrity checks. Each entry contains only native facts:
key, exact revision, whether a value is present, and its byte length. Tombstones
remain in this internal page. The host does not decide which entries are visible
conversations, parse PT1, inspect phases, choose titles, or make product policy.

Keep the existing key/count/namespace bounds and exclusive lexical cursor. No
raw record value enters the page. No claim, receipt, reservation, write or dispatch
authority is created. Create-only grants cannot enumerate metadata. Any invalid
scope, corruption or failed read must refuse rather than return a partial page.

Expose this through an application-selected command with the same native action-
time guard, deadline and actual credential-bound scope. Before adoption, bind the
new command to a distinguishable host contract and prove that an older unsupported
host cannot be admitted as compatible. The existing v3/v4 label alone is not that
proof; protocol/version handling is still an implementation decision.

## Proposed SIGIL behavior

`GET /v1/sessions?limit=20&after=project-notes` (both parameters optional).

- Authenticate current `sessions:read` before parsing query errors. Ignore GET body
  content within the existing transport bound; it cannot provide observations.
- Use the existing session-name grammar for `after`; require canonical decimal
  `limit` in 1–50, default 20. Reject unknown/duplicate fields, empty values,
  percent escapes and noncanonical numbers. No cursor is a credential.
- Request metadata from the actual matched state namespace. A fixed grantless
  SIGIL function validates the query/page and constructs the public projection.
- SIGIL omits absent or empty payloads, consistent with history's missing/tombstone
  behavior. Validate entry order, key grammar, revisions and fact types; do not
  silently repair malformed observations or skip them as if no data existed.
- Return only session names and exact string record revisions, plus an exclusive
  `next_after` cursor. Opening a selected conversation uses the existing history
  API. Listing does not certify transcript validity, a live worker or a terminal
  operation; it does not expose model configuration, raw messages or authority.

The cursor advances through the **last scanned** entry, not the last visible one.
A page containing only tombstones may therefore be empty with a non-null cursor.
Clients continue while a cursor exists. Preserve the native full-final-page rule:
one extra empty request may be needed to discover exhaustion.

Pages are a live lexical traversal, not one durable snapshot across requests.
Insertion before a cursor requires refreshing the list; deletion between listing
and opening can produce 404, and changed history can produce its normal 409. Do not
invent collection-level stability from per-record revisions or leak a global
cross-tenant revision counter. No automatic task resubmission follows these errors.

The proposed path needs at most five entry evaluations: parse via a pure function,
request native metadata, project the actual page through the pure function, then
reply. It fits the unchanged eight-step ceiling and avoids transcript-sized batches.

## Required acceptance before integration

1. Native metadata tests cover namespace/read authority, create-only denial,
   ordering/cursor boundaries, exact revisions, tombstones/empty values, malformed
   requests, corrupt records, maximum keys/pages, restart and zero state mutation.
   Existing key enumeration and get/commit behavior remains unchanged.
2. Real host conformance tests cover time/expiry checks, bootstrap refusal, unknown
   or unsupported commands, caller-forged scope/observations, fixed-function grants
   and bounded results. New host-contract compatibility is explicit and tested.
3. SIGIL query/projection tests cover every malformed field, page/byte boundary,
   all-tombstone progress, exact integer revisions, stable lexical order and no
   fabricated snapshot or execution-status claims. Pure fixtures confer no authority.
4. Actual HTTP tests create conversations through normal admission, list and reopen
   them, cross pagination boundaries, restart, exercise two tenants with identical
   names, verify same-tenant sharing and denied credentials/scopes, and assert no
   extra model/tool work or state changes from reads. Lifecycle tombstones need
   actual scoped transactions until the deletion API is implemented; label that
   controlled setup separately from public deletion evidence.
5. Run the existing history/automatic/cancellation/API regression suites and the
   complete expanded source gate. Bind evidence to the integrated source and host
   versions; no M0–M8 gate passes from this slice alone.
