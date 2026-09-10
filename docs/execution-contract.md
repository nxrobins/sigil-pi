# Durable execution contract: SIGIL application/executor boundary

Contract name: `sigil-action/v1`. Status: **DRAFT FOR CONFORMANCE REVIEW**.
Date: 2026-09-07. Implements the design work for M0 of [the MVP goal](mvp-goal.md),
not a claim that a runtime or either application already conforms.

The [2026-09-09 integration review](shared-runtime-integration-review.md) records
current control-plane/AIN observations, compatibility gaps and a proposed real
second-consumer test. It does not freeze this draft or qualify either application.

## Responsibilities and compatibility

SIGIL applications own authorization, allowance, sequencing, retry and recovery
decisions. A shared SIGIL executor owns delivery bookkeeping; native primitives
enforce granted mechanisms and independent ceilings. Neither the native host nor
an application-facing Python service interprets conversation/company policy.

Keep the existing `/v1` contract while migrating its implementation. Introduce an
additive asynchronous operation interface for browser/direct clients; do not change
`POST /v1/chat` from a synchronous 200 response into an asynchronous 202 response.
The [migration inventory](../config/api-migration.json) records all existing routes
and proposed additions. The legacy research `/chat` remains outside the product.

The operation protocol is independent of HTTP, Python, Wasmtime and AIN. Persist
semantic IDs and causal dependencies, never process IDs, pointers, or graph-node
addresses. Transport request IDs correlate calls; they are not operation identity,
deduplication authority, or proof that an effect was delivered.

## Record vocabulary

Records are versioned, bounded and strictly decoded. Unknown required versions,
duplicate fields, invalid encodings, non-finite/overflowing numeric values and
out-of-bound lengths fail closed. The final canonical encoding and numeric caps
must be fixed with the real SIGIL codec before this draft can be frozen.

The new operation request body now has an executable
[PS1 submission codec](submission-codec.md). It canonicalizes the three client
fields in SIGIL; it does not supply the authenticated authority context or replace
the durable record schemas in this contract. M0 freeze remains pending.

The [local native/SIGIL API](native-service.md) now reuses that decoder in real HTTP
authorization, durable operation admission and acceptance lookup. Its version-3
entry application calls a fixed grantless SIGIL function to construct one
[six-record transaction](admission.md): deduplication, operation, initial conversation
state, first model intent, outstanding reservation counter and per-operation reservation.
Original matched facts and configured code-bundle identity are bound into the records.
This connects authenticated admission to state/intent publication. A separate
[native claim gate](claimed-worker.md) now enforces receipt-bound, create-once worker
execution in the integration path. A [SIGIL dispatch policy](dispatch-policy.md)
now checks native-bound authority, actual records and held-worker facts before that
gate. A [native-bound SIGIL completion path](worker-completion.md) owns actual worker
observations and claim/delivery commits, including conservative abandoned-claim recovery.
A [native-bound SIGIL settlement path](settlement.md) now publishes terminal operation
results and reservation dispositions atomically; the real API reads those retained
results and refuses follow-up replacement before publication. The opt-in
[automatic v4 service](automatic-service.md) now connects native-bound dispatch,
SIGIL result interpretation and settlement without a fixture step driver.
[Never-claimed finalization](preclaim.md) adds atomic expiry/allowance publication
without fabricated delivery. [Public cancellation](cancellation.md) now records a
request, rechecks it at actual dispatch and preserves observed/uncertain outcomes.
Complete accounting, changed/removed authority, the remaining cancellation/fault
matrix and all product/candidate qualification remain open.
The host-command protocol carries a SIGIL-selected `TG1` time window,
checked natively after worker execution and immediately before storage/read/reply
initiation. Guarded bootstrap precedes native state opening. This closes the earlier
pre-worker-only check for those boundaries, not dispatch-time authority or completion
before expiry. Function results are data, not automatically executed host commands.
A later failed acknowledgement still cannot undo a committed decision;
the [native-service contract](native-service.md#time-bound-action-initiation) states
the remaining clock, scheduling and in-progress-storage limitations explicitly.

The [first conversation reducer](turn-reducer.md) also has executable internal
state/event/proposal codecs. SIGIL chooses model/tool sequencing in isolated tests,
including truthful uncertainty after worker cancellation. Those fixtures do not
provide the authenticated admission, durable commit or dispatch protocol below;
their operation/sequence fields must not be mistaken for trusted attempt records.

| Record | Required meaning |
|---|---|
| Application revision | Tenant-scoped aggregate ID, monotonic revision and schema, plus application-owned state; all expected prior revisions are checked before publication |
| Operation | Application namespace, authenticated tenant/principal identity, unguessable stable operation ID, client submission key, immutable payload digest, application/artifact/contract versions, creation time, dependency IDs and status |
| Authority binding | Authenticated issuer/context, principal and tenant binding, immutable policy snapshot/epoch, allowed operation/tool and exact resource bounds, credential validity and operation deadline, reserved resources, grant ceiling and delegated authority identity |
| Intent | Operation ID, unique attempt ID, exact tool/artifact identity, immutable input bytes/digest, authority binding/digest, deadline and required prior observations; contains no raw provider key |
| Dispatch record | Exact intent digest, attempt ID, executor generation/fencing identity, durable dispatch-start marker and observed start time |
| Delivery record | Same intent/attempt binding, delivery observation, bounded status/body/error facts, usage facts or explicitly unknown usage, observed timestamps and result digest |
| Application interpretation | Consumed delivery identity/digest, next aggregate revision, result/recovery decision and any next intent; no executor-authored business verdict |

An identifier or digest authenticates nothing on its own. Application-derived
authority must be issued through a trusted boundary and bound to its immutable
record. A worker receives a host-resolved opaque grant set, not arbitrary paths,
tenant strings or secret names accepted from a client/model. Results are admitted
only from the owning executor with the exact current intent/attempt correlation.

## Durable transition protocol

1. **Admission and preparation.** SIGIL validates identity, requested action, current
   policy and quota facts. A generic storage transaction or proven SIGIL journal
   protocol commits the application after-image, reservations, deduplication entry
   and immutable intent as one recoverable decision. No worker may see a dispatchable
   intent before that decision is committed. Admission failure causes no effect.
2. **Dispatch.** Resolve the exact admitted artifact and operation-scoped grants;
   re-evaluate expiry, policy epoch and remaining reservations under the frozen
   policy. Durably claim the intent and record `dispatch_started` BEFORE invoking
   any effect primitive. Recheck deadline immediately before the effect. A successful
   claim grants at most one attempt; polling the same record must not send again.
3. **Observation.** The effect primitive reports what it observed, not whether an
   application task succeeded. The SIGIL executor persists the correlated delivery
   record. It cannot write the application's domain aggregate.
4. **Interpretation.** The SIGIL application consumes that exact delivery once and
   commits its next state, settled or uncertain reservations, result and optional
   next intent. Marking a delivery consumed and advancing the application state are
   one recoverable decision. A client response is sent only after its promised
   durable state exists.

Storage acknowledgement must specify atomic visibility AND durability. Atomic
rename alone is not a promise of survival across power loss. The MVP requires a
documented local-POSIX implementation with explicit file/directory synchronization
or an equivalently tested transactional primitive. Inject process crashes at each
boundary; distinguish those results from physical power-loss evidence.

The repo-local [native opaque-record store](native-store.md) is now an executable
implementation candidate for this primitive: scoped atomic compare-and-set batches,
explicit durable acknowledgement and versioned tombstones. Real SIGIL turn tests
exercise it across process restarts. It is not part of the pinned SIGIL runtime,
not yet an admitted production dispatcher and not frozen as the shared backend.
It does not retroactively make the existing `kv_put` durable. A subsequent
[SIGIL application transaction producer](turn-transactions.md) emits state/intent
commits and correlated delivery read preconditions in the real integration tests.
Cross-project review, authenticated context/attempt binding, and the production
executor remain required. The later [shared executor transaction component](executor-transactions.md)
also emits atomic claim/delivery commits; draft `SI1`/`SD1`/`DR1` correlation is not
full intent admission. Pi's `PX2` adapter rejects the superseded `PX1`/`SO1` pair.

The repo-local [native worker bridge](native-worker.md) now supplies fixed-source,
fixed-grant, one-use execution tickets and bounded worker transport in those tests.
The fixture now uses a [native claim gate](claimed-worker.md) that binds actual intent
reads, the prepared generation and its own durable claim receipt before allowing
execution. Retained claims/tombstones refuse preparation at that coordinate after
reopening. The mechanism does not yet authenticate the full operation's authority,
artifact/input/grant selection, result routing or old-worker termination by itself.
The newer policy-bound path supplies the first three checks in the controlled
integration. The later completion path binds actual results and recovery facts there,
and in the newer automatic v4 API path, but does not establish old-worker termination. These mechanisms
are not fulfillment of this admission contract or universal semantic deduplication.

The first topology has one admitted application process, with enforced serialization
of each mutable aggregate and a single authorized dispatcher for an intent. Reject a
second writer/dispatcher before serving; a configuration convention is insufficient.
Do not allow an old executor to keep sending after its replacement acquires authority.
Multi-process distribution requires real compare-and-set/fencing, not lease timing
alone. Per-domain and intent/delivery roots must be disjoint and alias-checked.

## Delivery facts and recovery

| Durable situation | Permitted recovery | Forbidden inference |
|---|---|---|
| No committed acceptance | Report not accepted; a fresh submission may be evaluated | The request was accepted because a transport request ID exists |
| Committed intent, no dispatch claim | Re-evaluate current eligibility and dispatch once, or commit not-dispatched cancellation/expiry | Skip current authority checks because old policy allowed it |
| Dispatch started, no durable observation | Record `possibly_delivered`; require explicit reconciliation or application-approved safe retry strategy | Worker death, timeout or cancellation proves the remote action did not happen |
| Primitive proves no external dispatch | Record `not_dispatched` with bounded mechanical reason | Every HTTP failure or non-2xx status means no remote effect |
| Response observed and recorded | Consume the exact observation once; SIGIL interprets it | HTTP 200 means the user's task succeeded |
| Delivery recorded, interpretation not committed | Re-run deterministic interpretation with the same observation and prior revision | Re-send the effect to recover a missing application response |
| Interpretation committed, acknowledgement lost | Return the retained operation state/result for a matching submission key | Start a new turn because the browser refreshed |

`possibly_delivered` is a real state. Without a provider's explicit idempotency or
query/reconciliation guarantee, do not automatically retry an ambiguous effect.
Even read-only model calls can consume money or yield different responses. New
attempts need distinct identities and retain the earlier uncertainty; they cannot
rewrite history into `not_dispatched`. Provider-specific retry policy lives in SIGIL.

For a tool call accepted by the model, correlate the provider's tool-use ID with
the application operation/attempt; do not use that model-supplied ID as authority.
Persist the tool result before constructing the next model request. A turn which
returns several tools must specify dependencies/order explicitly and reserve their
authority separately. The first slice may execute them serially.

## Submission identity, retention and deletion

The new asynchronous submission accepts a client-generated key bound to
`(application, tenant, principal, key)` and the canonical request payload. Same key
and payload returns the same operation; a changed payload is a conflict. Check
current authorization before returning any retained result, including on a retry.
Concurrent duplicates must converge to one committed acceptance.

Retain deduplication for the full operation lifetime plus a documented bounded
post-terminal interval. The interval, quotas and key limits are M0 profile decisions
still to freeze. Storage exhaustion rejects new acceptance before effects. Within
the advertised interval, an unavailable record must not be interpreted as permission
to re-execute. Deletion uses a content-free tombstone for the advertised interval
or expires the submission namespace with an explicit contract; it must not silently
turn a replay into new work. Expired keys return an explicit expired/conflict result,
or require a demonstrably new submission namespace, not indefinite hidden dedup.

Transcript export/delete and retention must cover operation, intent, delivery,
reservation and audit surfaces. Deletion of active work first stops new dispatch
and resolves local worker ownership; possibly delivered remote effects remain a
disclosed limitation. Backups must preserve tombstones and uncertainty, and restored
workers must not redispatch pre-backup intents. Restoring an older snapshot requires
reconciliation against the post-backup effect interval before effects resume.

## Authority, time and resource lifecycle

- No browser/model-supplied tenant or policy field selects a namespace or grant.
  SIGIL interprets authenticated credential facts from a narrow host mechanism.
  Provider keys remain in native secret injection, never in application memory.
- Initial compatibility target: bounded credential validity and explicit restart
  rotation, not a new live-revocation service. Record the policy epoch in acceptance.
  Restart invalidates old executable grants. Resume an old intent only after current
  SIGIL policy re-authorizes the same or narrower authority under a new bound attempt
  decision; never mutate the old intent. Changed authority cannot be broadened by retry.
- The deadline is an absolute cap recorded durably, with monotonic elapsed-time
  enforcement for a running attempt. A restart must not reset its budget. Clock
  rollback/uncertainty fails closed for dispatch until a trusted time basis is restored.
- Expiry or acknowledged revocation prevents new dispatch. Native cancellation stops
  local execution and waits for termination before claiming it stopped; it cannot undo
  an already-sent external effect. An in-flight unknown result retains its reservation
  until SIGIL applies an explicit bounded reconciliation/accounting rule. A crashed
  worker must not make possibly consumed model tokens disappear from the budget.
- User allowances are SIGIL policy over metering/reservation facts. Host fuel, memory,
  concurrency, I/O and wall-clock ceilings apply independently, including to faulty
  SIGIL code. Fuel exhaustion is failure, never successful completion.

## Additive API proposal

These routes are the migration target. The local service implements durable
six-record admission, accepted/terminal result lookup and durable cancellation
requests. Its opt-in v4 coordinator
executes the first model/file/model turn and invokes [settlement](settlement.md);
the v3 admission-only reference still requires separately invoked mechanisms.
Neither supplies the complete lifecycle below.
Existing `/v1/chat` behavior remains unchanged and
does not acquire deduplication semantics by association.

- `POST /v1/operations` (`chat`): exact `{session, message, submission_key}` body;
  returns 202 with operation identity and status location, or the retained identity
  for a matching replay. Payload/key conflict returns a stable 409.
- `GET /v1/operations/{operation}` (`sessions:read`): tenant-authorized state,
  progress, bounded results/usage, uncertainty and next allowed user actions.
- `POST /v1/operations/{operation}/cancel` (`chat`): records cancellation intent;
  the current implementation returns request/control status only. Full answers and
  usage require `sessions:read` through GET, even when cancellation finds an already
  terminal operation. Confirmed local-execution/delivery presentation remains a
  target, not a current guarantee. Acceptance is not proof that the worker has
  stopped. Repeated cancellation is idempotent.
- `GET /v1/sessions` (`sessions:read`): bounded paginated conversation discovery.
- `GET /v1/sessions/{session}/messages` (`sessions:read`): bounded paginated history.

Freeze schemas, pagination, key retention and exact status/error semantics with the
SIGIL decoder/encoder. Same-tenant principal visibility must follow an explicit policy;
tenant membership is not automatically permission to cancel another principal's work.

## Pinned-runtime capability check and implementation gaps

Inspected public SIGIL source at sigil-pi's exact pin
`8277a1d92d599df89e6b4391fc70fd0fa534d696` on 2026-09-07. This is a source assessment,
not an executed conformance result. The newer local SIGIL and the control plane's
patched toolchain cannot silently substitute for this pin.

| Mechanism | Observed at the pin | Work required for this contract |
|---|---|---|
| HTTP envelopes and parameter/wildcard routes | Present in `sigil-serve` (some exact-route comments are stale) | Strict application decoding, credential-fact boundary and safe response/status behavior; review native framing before product exposure (duplicate content lengths, transfer encoding, header decoding and proxy agreement) |
| Source compilation at startup; fresh tool instances | Present | Inventory-wide admitted-module retention; current Wasmtime cache holds one entry |
| Scoped I/O and host-injected provider secrets | Present | Authenticated per-operation grant resolution and dispatch-time lifecycle binding; configured tool grants are static |
| Certificate gate and host profile | Present; configured certificates are optional in general server config | Mandatory whole-application admission with exact inventory/config/authority reconciliation for this product |
| KV put | Fixed sibling temporary file, then rename | Enforced serialization/unique staging, crash-durable acknowledgement, atomic aggregate/journal protocol and replay tests; no assumption of multi-key transactions |
| HTTP effect observation | Existing body/error shims and secret injection | A bounded one-attempt primitive with truthful status/body/delivery facts; preserve unknown outcomes conservatively until supplied |
| Process cancellation | Existing pi supervisor kills/replaces its compiler; no MCP cancel method | Generic supervised worker lifecycle bound to durable dispatch and authority; retain no-hidden-replay behavior |

Exact inspected sources:
[server configuration](https://github.com/nxrobins/sigil/blob/8277a1d92d599df89e6b4391fc70fd0fa534d696/crates/sigil-serve/src/config.rs),
[HTTP transport](https://github.com/nxrobins/sigil/blob/8277a1d92d599df89e6b4391fc70fd0fa534d696/crates/sigil-serve/src/http.rs),
[tool host](https://github.com/nxrobins/sigil/blob/8277a1d92d599df89e6b4391fc70fd0fa534d696/crates/sigil-serve/src/host.rs),
[ephemeral runtime](https://github.com/nxrobins/sigil/blob/8277a1d92d599df89e6b4391fc70fd0fa534d696/crates/sigil-runtime/src/ephemeral.rs).

Use the control plane's separated intent/delivery SIGIL executor and journal experience
as implementation input, but reconcile its patches and single-process assumptions
explicitly. Do not copy its two tenant slots as the shared protocol's semantic limit.

## Conformance and review before freeze

The first executable component is the pure
[`delivery_state.sigil`](../app/shared/delivery_state.sigil) transition kernel.
Its tests exercise all 42 phase/event combinations and ten malformed encodings
through the real pinned forge, plus a no-effect-authority source guard. The owning
executor must authenticate and correlate events before calling it, and must commit
its result durably. This component alone does neither of those things.

An undispatched intent can be cancelled or expired as definitely unsent. Loss,
cancellation or expiry after a dispatch claim yields `possibly_delivered` unless
an authenticated observation is already durably recorded. Terminal delivery facts
are immutable: a later observation belongs in a separate reconciliation record,
not a transition that reopens the original attempt. The kernel's two-digit test ABI
is deliberately not the proposed HTTP or durable-record encoding.

Required positive/negative traces: duplicate submissions; payload collisions; forged
tenant/authority; expired/changed policy; stale result/attempt; result before dispatch;
response lost before and after persistence; cancelled-before-dispatch; cancelled-after-
dispatch; full disk at each commit; duplicate workers; crash during state+intent commit;
restore with effects newer than backup; quota/retention exhaustion; late results after
deletion. Real runtime tests must retain raw observations and durable records.

Review is pending with the control-plane and AIN work. In particular: generic storage
primitives and acknowledgment; grant issuance/lifecycle; authority identity versus AIN
origin identity; causal dependencies and permissible observation order; artifact/host
binding; and what remains outside each proof. No AIN implementation/proof milestone is
a predecessor to implementing this contract on the supported Wasmtime path.

M0 remains PARTIAL until that review, codecs, numeric profile, migration fixtures and
product-owner decisions are frozen. No existing GA gate or released API is changed by
this draft. Follow progress in [the MVP acceptance record](mvp-acceptance.md).
