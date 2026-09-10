# Conversation discovery

Status: **DEVELOPMENT INTEGRATION — staged native/HTTP conformance and the
integrated local whole-source gate passed**. This advances M1/M4 of the
[MVP goal](mvp-goal.md), not the complete lifecycle, browser or external pilot.

## Request and authority

`GET /v1/sessions?limit=20&after=project-notes`

Both query parameters are optional. `limit` is a canonical decimal integer from
1 through 50, default 20. `after` is an existing-style session identifier: at most
128 ASCII bytes, beginning with an alphanumeric character, with the permitted
alphanumeric, dot, underscore and hyphen characters thereafter. Unknown/duplicate
parameters, empty values, percent escapes, signs, leading zeroes and empty query
strings are rejected.

The current credential must have `sessions:read`. SIGIL authorizes the request
before parsing query errors and selects the matched tenant's state namespace.
The host supplies an actual read-authorized native metadata page. GET body bytes
are ignored within the existing transport bound; body/query fields cannot provide
scope, grants, observations, commands or another tenant's namespace.

Discovery is tenant-shared, like [retained history](session-history.md). It does
not grant access to another principal's owner-scoped operation results or cancellation.
The final pilot sharing policy still needs M0 product-owner approval.

## Response and live pagination

An illustrative response is:

```json
{
  "sessions": [{"session": "project-notes", "state_revision": "7"}],
  "next_after": null,
  "order": "session_name",
  "consistency": "live_scan"
}
```

Only session names and exact string **per-record** revisions are public. No raw
transcript, configuration, authority, byte count, global transaction counter,
title, completion status or provider secret is included. Listing does not certify
that the transcript is valid or that a worker is running. Opening a session uses
the separately authorized history route and can still report an error.

Pages are an exclusive, binary-lexical live scan, not a collection snapshot.
Use the actual `next_after` while it is non-null; it denotes the last **scanned**
record, not the last visible session. SIGIL omits tombstones and empty payloads,
matching history's missing-session semantics. An all-tombstone page can therefore
be empty with a continuation cursor. A full final page may need one extra empty
request to establish exhaustion.

Insertion before a cursor is found by refreshing the list. A session can change
or disappear between listing and opening it. Respect history's revision/404/409
semantics; do not merge incompatible history pages or automatically resubmit work.
All responses use `Cache-Control: no-store`.

## Errors and bounds

| Condition | Result |
|---|---|
| Missing, inactive or unknown credential | `401 invalid_credential` |
| Missing `sessions:read` | `403 permission_denied` |
| Invalid query | `400 invalid_session_query` |
| Failed/corrupt native metadata observation | `503 storage_unavailable` |
| Malformed internal protocol or another host refusal | Existing fail-closed host response; never a fabricated empty success |

The native store scans at most 128 records per metadata call, using its existing
key, value and integrity bounds. SIGIL limits public requests to 50. A malformed
or corrupt entry fails the affected page rather than returning its healthy prefix.
Record revisions are not transaction receipt numbers; the native tests exercise
different values for those coordinates explicitly.

The native primitive reports key, revision, presence and UTF-8 byte size. It does
not parse conversation state or choose visibility. Its command uses the existing
action-time guard, monotonic-clock check, independent deadline and actual storage
scope. Create-only authority cannot enumerate metadata; bootstrap has no storage
handle. No write, reservation, claim or model/tool work follows from a listing.

## Explicit host profiles

| Native host profile | Entry component | Functions | Wire contract |
|---|---|---|---|
| v3 / v4 automatic | `api` | `admission`, `history` | Existing AH3/HC3, no discovery command |
| v5 / v6 automatic | `api_discovery` | `admission`, `history`, `listing` | AH4/HC4 plus actual command inventory |

The new entry is a versioned build of the same authored SIGIL API core, adding the
[SIGIL discovery route](../app/pi/listing_api.sigil) and its fixed grantless
[projection](../app/pi/listing.sigil). The build recipe has explicit counted ABI
substitutions and refuses silent rebasing of its qualified core. It does not run
Python application decisions. The old `api` compiler input is unchanged.

v6 uses the existing fixed grantless evaluators for these pure functions and keeps
fresh guest instances. v5 retains the corresponding fresh-process path. The new
command inventory is bound into the host bundle identity. Discovery needs four
entry evaluations within the unchanged eight-step ceiling; its entry and projection
fit the unchanged 64 KiB source limit. No fuel, memory, operation or request deadline
was widened.

The [static v6 test deployment](../tests/listing_support.py) illustrates assembly;
it is not a pilot deployment configuration or a Python production service. The
native executable still consumes an operator-owned configuration. Model/provider,
quotas, deployment and other M0/M8 decisions remain open.

Old host rejection is tested using the [frozen native v4 source fixture](../tests/fixtures/native-host-v4/README.md),
rebuilt at its original locks, not an imitation version checker or a silently
upgraded binary. Stdio v2 metadata support is independently versioned; v1 retains
its previous behavior. These checks do not qualify an in-flight-operation upgrade
from application v4 to v6: the bundle changes, and recovery/migration needs separate
evidence before release.

See the [acceptance record](mvp-acceptance.md) for exact source fingerprints,
executables, staged and integrated test outcomes and remaining limitations.
