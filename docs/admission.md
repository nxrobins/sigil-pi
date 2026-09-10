# Atomic SIGIL turn admission

Status: **LOCAL DEVELOPMENT IMPLEMENTATION — effects and pilot qualification incomplete**.
This connects the real [native/SIGIL API](native-service.md) to resumable turn state.
It does not complete [M2 or M3](mvp-goal.md).

## One decision, six records

After authentication and request decoding, the API reads the principal-scoped
deduplication key. A matching retained submission returns its original operation
before any new reservation is attempted. A changed payload conflicts. For a new
submission, the API reads the actual tenant-scoped conversation, its preceding
operation when present, and the reservation counter, then calls the grantless
[admission component](../app/pi/admission.sigil).

SIGIL constructs one exact native compare-and-set batch containing:

| Record | Key / role | Write precondition |
|---|---|---|
| `DQ2` | principal-encoded submission key; canonical payload and original authority | Absent, revision 0 |
| `OQ2` | random operation ID; acceptance and original context | Absent, revision 0 |
| `PT1` | tenant-scoped external conversation name; initial model phase | Actual conversation revision |
| `SI1` | operation ID plus `:1`; minimal first model input | Absent, revision 0 |
| `BH1` | `active` in that tenant's budget namespace; outstanding reservations | Actual counter revision |
| `BR1` | operation ID; exact reserved amounts and binding | Absent, revision 0 |

The native host forwards these bytes without constructing or editing product
records. The actual native store commits all six or none. Each record increments
its **own** revision; the receipt has a separate global transaction revision.
Equal record revisions are not a general transaction identity. Fresh records in
the initial fixture all happen to have revision 1.

The API returns 202 only after the native commit acknowledgement. Read/call
continuations before that commit are request-local, not durable acceptance.
Restart/retry after a lost acknowledgement finds the existing operation and does
not reserve again. A failed response can still mean unknown acceptance; no response
error authorizes blindly repeating an external effect.

The model input and initial conversation state come from the **same authored
`start_turn` implementation** used by [the turn reducer](../app/pi/turn.sigil).
Build composition extracts explicitly marked regions; it does not maintain a
second turn-start policy. The admission component also reuses the API's exact
operation/key identity codec. All composed inputs retain the original compiler
and verification bounds.

The bounded append helper copies the accumulated write list once per appended
record rather than allocating an additional full-prefix copy for its comma.
This preserves the same byte order and inclusive 2 MiB serialized-size ceiling
under the unchanged guest memory/fuel limits. Maximum-sized 262,144-byte ASCII
and UTF-8 submissions are checked for exact preservation in all four payload-
carrying records. Native-store tests also exercise maximum-sized admissions
against every stale coordinate, denied final namespace and process restart.

## Conversation and authority policy

Conversation identity remains tenant plus external session name, matching the
legacy tenant-scoped model. It is not silently changed to principal-private state.
An active, unresolved or tombstoned conversation refuses a new submission with
`409 session_busy`; it cannot be overwritten. A completed `done` conversation can
contribute retained history only after [terminal settlement](settlement.md) has
published its matching revision-2 `OQ3`. Both the prior conversation revision and
operation revision are checked by the next admission transaction. A terminal
conversation alone cannot be replaced before its operation result is saved.
An existing completed conversation without its reservation ledger is refused,
not used as justification to recreate an empty budget.

Different principals in one tenant share this conversation namespace and reservation
capacity. Operation lookup is still owner-only. **Owner-only operation lookup does
not establish transcript privacy between principals in the same tenant.** The final
M0 sharing/recovery policy must be approved before qualification. The legacy Python
API is unchanged; its synchronous `/v1/chat` behavior has not been replaced.

Before forming the initial request, SIGIL filters the fixed model tool catalog by
the authenticated credential's tool set. A model cannot add authority. The immutable
operation contains the original credential facts and configured application bundle
identity; replay does not replace them with the current credential or bundle.
This is not yet authenticated effect dispatch or a durable claim receipt.

## Reservation capacity, not complete usage accounting

`BP1` specifies a model/turn profile, maximum outstanding turns, maximum outstanding
input-token reservations, maximum outstanding output-token reservations and the
input reservation per turn. SIGIL reserves one turn, the configured input amount,
and `max_steps * max_tokens` output tokens before accepting work. The exact-boundary
case is allowed; exceeding any independent capacity returns 429 with no transaction.
Zero aggregate capacity can disable admission; per-turn input reservation must be
positive. Numeric values are canonical and bounded, not floating-point estimates.

All credentials in the same tenant must declare exactly the same profile and six
namespaces. A stored counter is bound to that exact profile and tenant. Changing
profile bytes cannot silently reset existing reservations; an explicit migration
will be needed. Credential tool/scope policy may still differ by principal.

**Only outstanding reservation capacity is implemented here.** The separate
[SIGIL settlement component](settlement.md) now releases known reservations and
retains unknown/overrun token holds, with repeat settlement prevented. The version-4
[automatic service](automatic-service.md) invokes it after SIGIL interprets a terminal
outcome. There is no general expiry sweeper, request-rate
accounting, rolling allowance window or cumulative charged-usage ledger in this path.
Restart does not clear reservations; a completed/failed effect is not free. Input metering
and actual billable dispatch must be enforced and qualified before effects are
enabled. These counters are not a tested provider-spending guarantee or replacement
for the legacy product's quotas. Fixture model names and numeric values are test
profiles, not operator-approved pilot settings.

## Draft codecs

All records use four-byte markers and eight-digit UTF-8 byte-length fields.

| Marker | Ordered fields |
|---|---|
| `CF2` | principal, tenant, epoch, not-before, expiry, scopes JSON, tools JSON, dedup namespace, operation namespace, maximum turn seconds, state namespace, intent namespace, budget namespace, reservation namespace, `BP1` |
| `BP1` | `PC1`, maximum held turns (0–1024), maximum held input (0–10^12), maximum held output (0–10^12), per-turn input reservation (1–10^9) |
| `AV1` | bootstrap registry JSON, containing the actual `CB1` credential/grant bindings |
| `AP2` | `CF2`, canonical `PS1`, operation ID, creation seconds, deadline seconds, actual state `SR1`, actual budget `SR1`, dedup key, bundle identity, current seconds, actual preceding operation `SR1` |
| `AD1` | outcome (`ok`, `busy`, `quota`, `expired`), exact commit JSON or empty, `TG1` guard or empty |
| `AS1` | held creation seconds, deadline seconds |
| `AS2` | held creation seconds, deadline seconds, actual state `SR1` |
| `AS3` | held creation seconds, deadline seconds, actual state `SR1`, actual preceding operation `SR1` |
| `BH1` | tenant, exact `BP1`, held turns, held input, held output |
| `BR1` | operation, tenant, principal, exact `BP1`, reserved input, reserved output, deadline, `reserved`, bundle identity |
| `DQ2` | principal, tenant, submission key, canonical request, operation, epoch, creation seconds, deadline seconds, original `CF2`, bundle identity |
| `OQ2` | the first nine `DQ2` fields, `accepted`, bundle identity |

`PC1`, `PT1`, `PS1` and `SI1` retain their existing meanings. `AP1` is refused because
it cannot bind the preceding terminal operation. A pure `AP2` producer
does not authenticate caller-supplied snapshots; the real API binds them through
its owned native continuation loop. HTTP JSON cannot supply an `AP2`, stage,
function, snapshot, profile, bundle or authority field.

Version-1 queued `DQ1`/`OQ1` records do not establish a reservation or initial intent.
The new path refuses them instead of upgrading their meaning or executing them.
No existing record is erased or rewritten by startup. Internal state/configuration
migration remains explicit future work; this is not a qualified upgrade path.

## Evidence and remaining work

[Admission tests](../tests/test_admission.py) compare the exact initial state and
intent with the original reducer, exercise profile/time/identity checks, and forward
SIGIL's unmodified batch into actual native storage. Stale preconditions on any of
the six coordinates and a denied final namespace prevent partial publication;
successful admission survives store-process restart.

[Real HTTP tests](../tests/test_native_api.py) exercise reservation persistence,
duplicate convergence, two tenants, shared-tenant contention and policy consistency,
profile/bootstrap refusal, secret exclusion, and rollback when native capacity is
insufficient for the six-record admission. These are local source tests, not the
candidate-bound fault/load, live-model or independent-review gates.

The separate dispatch, completion and settlement mechanisms now exercise these
boundaries with actual native reads and receipts. The version-4 automatic service
connects SIGIL-owned worker selection, delivery interpretation and terminal settlement.
Complete cancellation/recovery qualification, metering, all remaining API routes,
browser chat, the actual control-plane consumer and M0–M8 qualification remain.
