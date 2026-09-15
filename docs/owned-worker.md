# Receipt-bound owned effect execution

Status: **LOCAL MECHANISM — also embedded in the development v4 automatic service**.
Source: [owned worker](../native/service/src/claimed/owned.rs). Evidence and all
remaining product gates live in [MVP acceptance](mvp-acceptance.md).

## Contract

`OwnedWorker` is a bootstrap-created native lane holding one fixed effect bridge,
one fixed grantless SIGIL result producer, and one actual boot-bound storage scope.
The service owner retains the store. No storage handle or guest-held secret is
transferred into the execution thread. The lane admits at most one flight, including
a completed flight whose result has not been collected. Its containing registry
must also have a fixed independent host ceiling; this module alone is not a global
concurrency or tenant scheduling policy.

1. `start` calls the existing fixed SIGIL dispatch policy with bounded lookup values,
   actual scoped records, clock and held-worker facts. SIGIL chooses the input,
   authority guard and opaque application correlation.
2. The existing claim path checks the delivery slot, invokes the fixed SIGIL claim
   producer and commits its exact bound transaction. Only its actual acknowledgement
   creates the private handoff. A start acknowledgement contains that claim receipt,
   actual generation and opaque context; it does **not** acknowledge external delivery.
3. The handoff rechecks the original intent and exact phase-1 claim while the store is
   still exclusively borrowed. The effect thread then rechecks the same time guard and
   original monotonic deadline immediately before invoking the frozen one-use bridge.
   Dispatch initiation is the checked handoff, not a promise of continuous state or
   authority revalidation while an external effect runs. Live revocation requires an
   explicit supported policy and signaling path; it is not inferred from this thread.
4. The store borrow ends. Pending polls inspect the private thread handle without
   waiting for execution or invoking result policy. Actual store operations may proceed
   on the owner thread. Neither an HTTP request nor a serialized observation can
   substitute for the owned thread's completion.
5. Once the thread is finished, collection obtains its actual observation and worker.
   Result recording uses the originally held scope, rechecks the exact original intent
   and phase-1 claim bytes/revision, and requires an unused delivery slot. It passes the
   actual facts to the fixed SIGIL result producer and validates/commits the same exact
   claim-update/delivery-create contract as synchronous recording.

The dispatch deadline includes claim work and is not reset at thread start. Recording
has its own bounded interval after dispatch expiry. A checked thread clock high-water
mark is merged into the owner when collected; this does not establish a globally
serialized clock observation order across concurrently running threads.

## Cancellation, loss and recovery

`request_cancel` only signals the actual held flight. SIGIL must authorize that call
and choose any durable application transition. Signal issuance, confirmed local reaping
and remote-effect outcome are different facts. A result that already completed may
still be observed; cancellation cannot turn it into definitely-unsent.

Dropping the lane signals cancellation but does not block for or acknowledge cleanup.
The durable claim stays consumed. A panicked/lost execution owner supplies `abandoned`
facts to SIGIL: possibly run, cleanup unconfirmed, no result. No replacement bridge is
created and that lane stays unavailable. Thread-creation failure is instead a native
pre-invocation refusal; its dropped bridge is also not silently replaced. An unreaped
bridge remains poisoned under the existing worker rules.

The original scope cannot be replaced at collection. A foreign store, changed intent,
newer claim, occupied delivery slot or malformed producer output returns a recording
error with no invented delivery receipt or durable phase. A possibly committed storage
error still requires reopening/reading actual state. Completing/collecting a flight
does not reopen its claim; another preparation at that coordinate fails.

The storage borrow no longer excludes an owned live worker. The embedding SIGIL
coordinator must check its actual active-handle registry before choosing abandoned-
claim recovery. The old synchronous recovery method does not prove that an owned
flight or orphan is gone. Recovery never sends an effect, and late owned completion
cannot overwrite a recovery transition because its exact claim check fails.

## Explicit trusted conformance version

The local `sigil-claimed-worker/v4` adapter uses the same bootstrap restrictions as
v3 but replaces its blocking attempt loop with the owned lane:

- `authorize {values}` returns `started` containing the actual claim receipt,
  generation and opaque context. It does not expose a manual claim step.
- `poll {generation}` returns `completion: null` while pending, or an owned completion
  with the actual `Recorded` result, context and `owner_lost` fact. The matching active
  generation is required; repeated collection is refused.
- `cancel {ticket}` uses that actual generation for correlation and returns only
  `cancellation_requested`. This local controller is trusted; correlation is not
  authenticated product cancellation authority.
- `get {namespace,key}` remains restricted to bootstrap read authority while running.
  The create-only delivery scope is not widened into a delivery-read capability.
- Manual prepare/claim/execute/run/commit/recover requests and supplied result/receipt/
  phase/clock/scope fields are refused. Recovery scheduling belongs in the eventual
  SIGIL service coordinator, not this conformance adapter.

Versions 1–3 retain their explicit behavior. Product HTTP has a separate opt-in v4;
durable record formats and the pinned SIGIL runtime have not changed. Version 4 here
is not an automatic upgrade, public product endpoint or release candidate.

## Remaining service integration

The [automatic service](automatic-service.md) now supplies a fixed bounded effect
registry, current authority/coordinator/artifact binding, SIGIL work discovery/selection,
interpretation and settlement. Explicit API progress/cancellation and complete recovery
policy remain open.
Pure SIGIL preparation/recording still takes bounded synchronous work on the owner;
this mechanism does not qualify API latency or the required two-tenant operating load.
No browser, real-model usefulness, independent user onboarding, supported Linux
lifetime/restore, actual control-plane reuse or candidate/security gate is passed by
this local concurrency mechanism.
