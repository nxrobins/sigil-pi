# Shared SIGIL executor transactions

Status: **EXECUTABLE BOOKKEEPING COMPONENT; NOT AN ADMITTED DISPATCHER**,
2026-09-08 UTC. Sources: [executor](../app/shared/executor_transaction.sigil),
[delivery kernel](../app/shared/delivery_state.sigil),
[storage encoding](../app/shared/store_protocol.sigil), and
[record helpers](../app/shared/record_helpers.sigil).

This component constructs the executor's atomic claim and delivery transactions.
It reuses the existing delivery-state kernel rather than implementing another
transition table in Python or native code. It knows no model, tool, conversation,
company or assignment policy. A nonempty opaque intent and its scoped storage
coordinate/revision are its application-facing inputs.

The [durable pi integration](../tests/test_durable_turn.py) now forwards SIGIL's
application AND executor commit bytes unchanged. The test driver still supplies
trusted-fact substitutes, controls native processes, and selects a fixed registry.
Actual effects now use the [native worker bridge](native-worker.md): prepare a
fixed-source/input/grant ticket, commit SIGIL's claim with its generation, execute
once, then commit SIGIL's correlated delivery. The [native claim gate](claimed-worker.md)
now enforces actual intent/generation/receipt binding and create-once execution at
the retained claim coordinate. One fixture starts with a real HTTP-admitted intent,
but registry/authority selection, result routing and settlement are not automatic
product dispatch or completion of M1/M2/M6.

The newer [native-bound completion component](worker-completion.md) uses this exact
producer for actual worker observations and abandoned-claim recovery. It derives
EX1 in SIGIL from native-supplied facts, not controller-authored outcomes, and its
native binder owns both actual commits. This closes the result-provenance gap in
the local mechanism, not automatic application result routing or API completion.

## Draft wire records

Each marker includes LF; fields use the existing eight-digit UTF-8 length frames.
All records reject wrong versions, malformed lengths and trailing bytes. All numeric
fields are canonical nonnegative decimal text; storage revisions retain the native
signed-64-bit range without floating-point conversion.

| Record | Fields in order |
|---|---|
| `EX1` + LF | intent namespace, dispatch namespace, delivery namespace, record key, intent revision, intent bytes, dispatch revision, dispatch record, event, observing generation, outcome, observed payload |
| `SD1` + LF | intent namespace, key, intent revision, claiming generation, delivery phase |
| `DR1` + LF | the five `SD1` fields, outcome, observed payload |
| `ER1` + LF | resulting phase, continuation hint, exact native commit JSON (empty for no-op) |

Namespaces must be pairwise distinct and match the native identifier grammar.
The key is opaque to this shared component; pi uses its operation/sequence key.
Generations are bounded opaque identifiers, not process IDs, lease timestamps,
credentials or proof that a worker is dead. The durable fixture now uses the native
bridge's random generation for live effects; the native claim gate requires that
actual generation before committing phase 1. Component tests still supply generation
strings directly. Full authenticated operation/authority binding and fencing worker
lifetimes across controller restart remain required.

An existing intent has positive revision and 1–2,097,152 bytes of opaque data.
No dispatch record means revision zero and empty record bytes. A present dispatch
record requires positive revision, phase 1–6, and exact intent namespace/key/revision
correlation. Persisting a prepared phase-zero record is not supported. Missing
positive-revision state and old single-digit phase records fail closed.

The whole input is capped at 4 MiB, observation payloads at 1 MiB, and constructed
requests/output at 2 MiB. Native memory, fuel and storage ceilings remain independent
and may refuse an earlier combination. These are component ceilings, not the pilot
profile or guaranteed capacity for every individually valid field combination.

## Events, observations and ownership

Events retain the kernel's numbering: 0 claim, 1 observed response, 2 proven not
dispatched, 3 owner lost, 4 acknowledged local cancellation, 5 expiry/stop fact.
The host must establish the mechanical facts before SIGIL consumes them. An API
cancel request is not evidence of completed local cancellation, and an expired
credential alone is not proof that an already running worker stopped.

Only event 1 carries `outcome` (`returned` or `failed`) and a bounded observed
payload. These distinguish an observed result from an observed error; neither is
a business-success verdict or proof of rollback. A trap/transport failure with an
unknown external outcome must follow the uncertainty path, not be relabeled a
completed response. The draft does not yet provide every native single-attempt
status/body/usage fact required by the full execution contract.

For a live claim, response, not-dispatched, cancellation and expiry facts must name
the claiming generation. Owner-loss recovery may come from a replacement generation,
but the original claiming generation remains in the stored delivery. A different
generation string does not authenticate loss or fence the old worker by itself.

| Prior situation | Result |
|---|---|
| Prepared + claim | Commit phase 1, then request dispatch admission |
| Prepared + owner lost | No commit and no dispatch hint; eligibility must be re-evaluated |
| Prepared + cancellation/expiry | Commit definitely-unsent terminal phase 5/6 and delivery |
| Claimed + observed response | Commit phase 2 plus exact outcome/payload |
| Claimed + proven not dispatched | Commit phase 3 plus empty outcome/payload |
| Claimed + owner loss/cancellation/expiry | Commit phase 4, possibly delivered; never request another send |
| Terminal + any valid event | Return the retained phase, no commit and no dispatch hint |

A late result cannot overwrite a terminal uncertainty record. Future reconciliation
needs a separate explicit record/protocol; it cannot reopen the original attempt.
The executor cannot read delivery contents through its create-only delivery handle.
If a supposedly terminal attempt lacks a delivery, the application must fail closed;
this component does not fabricate a replacement or redispatch to repair it.

## Atomicity and hints are not authority

Every mutating request includes a read precondition for the exact intent revision,
a compare-and-set dispatch write, and—on a terminal transition—a create-only delivery
write in the SAME batch. An existing delivery or stale dispatch/intent revision
rejects the entire transaction, preserving the prior state. An `ER1` no-op emits
no native request at all.

`dispatch_after_commit` means the caller must first receive the durable claim
acknowledgement and then pass fresh artifact/authority/deadline admission. It is
not a capability or one-use execution token. The native claim gate now owns the
actual commit acknowledgement and enforces intent/generation/time binding in the
durable fixture; the hint alone cannot arm it. `record_after_commit` likewise cannot
be reported as durable before the commit acknowledgement. A lost acknowledgement
requires reading retained records, not repeating an effect. The gate refuses repeat
preparation at the retained coordinate; full authority binding and old-worker lifetime
fencing remain required before product dispatch.

`SD1`/`DR1` bind scoped coordinates and revisions structurally; they do NOT contain
the full authenticated immutable payload/authority/attempt binding required by
`sigil-action/v1`. Neither a supplied snapshot nor its revision authenticates its
bytes. The future admitted host must bind the actual read results, event provenance,
output routing, and exact execution to the same operation authority.

## Pi interpretation and compatibility

Pi's [application transaction adapter](turn-transactions.md) now accepts `PX2` with
the actual `DR1` delivery. It checks intent namespace/key and its create-only intent's
revision 1. The pi adapter maps generic phases into conversation events: uncertainty
stops the turn, definitely-unsent cancellation/expiry stops appropriately, and an
observed failure becomes a model failure or a correlated tool error according to
pi's own saved phase. No pi decision moved into this shared executor or native store.

The old draft `PX1`/`SO1` pair is rejected. No released product used these records;
no silent state upgrade, migration of the current product store, or runtime-pin
change is performed. A future schema freeze must version the admitted record set.

[Executor tests](../tests/test_executor_transaction.py) exercise all 42 kernel
phase/event pairs, correlation/ownership/grammar/resource refusals, exact revisions,
real scoped native commits across restarts, stale replay, immutable terminal delivery,
read-precondition rollback, delivery-collision rollback and native scope enforcement.
The generic opaque intent used in component tests is not M6's real second consumer.
