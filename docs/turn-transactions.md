# SIGIL-produced atomic turn transactions

Status: **EXECUTABLE COMPONENT; NOT PRODUCTION ADMISSION**, 2026-09-08 UTC.
Sources: [pi transaction producer](../app/pi/turn_transaction.sigil),
[shared storage encoding](../app/shared/store_protocol.sigil), and the existing
[turn reducer](../app/pi/turn.sigil). This advances M1/M2 without qualifying them.

## What changed

The durable integration driver no longer decides which application writes or
read preconditions belong in a commit. One composed SIGIL invocation runs the
existing `reduce_turn`, validates observation correlation, constructs the minimal
next intent, and emits the exact `sigil-store/v1` JSON request. The test driver
forwards those bytes unchanged to the real native store. Python does not implement
an alternative model/tool sequence or rebuild the application transaction.

The shared file owns storage coordinates, exact revision encoding, JSON request
construction and operation/sequence key framing. The pi file owns pi-specific
observation vocabulary and transaction membership. Native storage still interprets
only opaque records, namespace scopes, revision preconditions and atomic batches.
The later [shared SIGIL executor](executor-transactions.md) also constructs claim
and delivery commits; the driver no longer chooses any of those atomic writes.

## Draft snapshot and record shapes

These internal codecs use the same exact eight-digit UTF-8 byte-length frames as
[the reducer](turn-reducer.md). They are not HTTP request bodies and must never
accept namespace, state, time, configuration or delivery facts from an untrusted
client as authoritative. A valid frame does not confer permission.

| Marker including LF | Fields in order |
|---|---|
| `PX2` + LF | state namespace, intent namespace, delivery namespace, session storage key, expected state revision, `PE1` event, expected delivery revision, `DR1` delivery |
| `SI1` + LF | operation, sequence, pi action (`model` or `tool`), tool name (empty for model), required effect input |
| `DR1` + LF | intent namespace, intent key, intent revision, claiming generation, delivery phase, outcome, observed payload |

`SI1` is a minimal pi proposal record, **not the complete admitted intent** required
by `sigil-action/v1`. `PX2` consumes the shared executor's `DR1` record, replacing
the earlier draft `PX1`/`SO1` pair, which now fails closed. This is not an automatic
migration of existing state. These files are not in a released product package; an eventual durable schema
freeze/migration must include their meaning and authenticated outer bindings.

Namespace names are 1–128 ASCII alphanumeric or `._:-` bytes and must be pairwise
distinct. Storage keys use that alphabet with a 256-byte ceiling; the pi adapter
also requires the key to equal the reducer's validated session identifier. Intent
and delivery keys are `operation:sequence`, with a bounded canonical integer
sequence; the operation/sequence pair is not an authenticated attempt identity.

Storage revisions are canonical decimal integers from zero through
9,223,372,036,854,775,807. They are parsed with checked integer accumulation and
emitted as the original decimal bytes, never converted through JSON floating point.
The native store may still refuse a commit at its revision/resource ceiling.

## Commit rules

- Empty prior state requires revision zero; present state requires a positive
  revision. A tombstone is not empty revision-zero state and cannot be resurrected
  through this interface. A completed prior state can begin a new operation under
  the reducer's existing follow-up rules, preserving the conversation.
- A start has delivery revision zero and no delivery bytes. Other events require
  a positive delivery revision and an exact `DR1` record. Its intent namespace/key
  and immutable pi intent revision 1 must match. Pi interprets the phase/outcome;
  the derived kind and exact payload must equal the event. The reducer independently checks
  operation/sequence against prior state and rejects transitions from terminal state.
- Only an observed response (phase 2) may carry `returned`/`failed` and payload.
  Pi maps an observed failure to `tool_error` in a tool phase and `error` in a model
  phase. Phases 3–6 carry empty outcome/payload and map to `error`, `unknown`,
  `cancelled_unsent`, and `expired_unsent`. Unknown versions, phases or outcomes,
  extra frames, malformed lengths or trailing bytes fail closed. Provenance and
  the underlying facts still require an admitted executor.
- Every interpretation includes a native read precondition for the exact delivery
  coordinate/revision. Every commit compares the prior application revision and
  writes its after-image. A model/tool proposal adds exactly one create-only minimal
  intent at revision zero, in the same transaction. A terminal decision adds none.
- A conflicting application revision, changed delivery revision, or existing next
  intent rejects the whole batch in native storage. No state-only prefix is committed.
  Atomic application advancement is the consumption marker for this slice; an old
  interpretation cannot advance state a second time.

The input ceiling is 4 MiB and the emitted request ceiling is 2 MiB. A preflight
counts JSON quoting expansion before expensive request construction; final bounded
concatenation includes the remaining envelope overhead. The unchanged native guest
memory/fuel ceilings can refuse an invocation earlier. No partial request is returned
as success, and neither producing a complete request nor receiving an error commits
anything. A host trap is distinct from the producer's explicit `-413` size refusal.
These are component ceilings, not the frozen pilot capacity profile.

## Evidence and remaining work

[Transaction tests](../tests/test_turn_transaction.py) exercise real pinned SIGIL
compilation/execution, exact output bytes, observation/state correlation, immutable
terminal behavior, malformed records, numeric precision above 2^53, namespace
separation, quoting/control characters and independent memory refusal. Native tests
send the SIGIL output unchanged and demonstrate collision/read-precondition rollback,
two disjoint scopes with identical semantic IDs, and retained follow-up history after
restart. The [durable effect suite](../tests/test_durable_turn.py) uses the producer
across real isolated model/file/model effects and interruption boundaries.
Its effects now use the [native worker bridge](native-worker.md), whose generated
ticket and observation are correlated through SIGIL's claim/delivery records.
This does not change the transaction producer's trust assumptions below.

These tests still supply authenticated-context substitutes: namespace scopes,
read snapshots, registry, clock, operation IDs and observations. A caller can create
a structurally consistent false snapshot; this pure component cannot authenticate it.
The scope-denial test proves only the native handle's independent ceiling, not an
authenticated two-tenant service. Matching a revision does not authenticate a supplied
record's bytes, and a plain operation/sequence pair is not an artifact/payload binding.

Before the real API can use this path: implement SIGIL authorization and immutable
operation/authority/attempt records; bind storage snapshots and output routing to
admitted artifacts and scopes; complete authenticated executor admission;
enforce native worker ownership, dispatch-time checks and hard cancellation; preserve
deduplication, reservations, audit, expiry/revocation and route parity. No current
product endpoint is switched, no runtime pin is changed, and no M0–M8 gate is PASS.
