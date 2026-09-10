# Native durable-claim execution gate

Status: **EXECUTABLE LOCAL MECHANISM; NOT THE PRODUCT DISPATCHER**.
Sources: [gate](../native/service/src/claimed.rs),
[native tests](../native/service/src/claimed/tests.rs), and
[trusted stdio embedding](../native/service/src/bin/sigil-claimed-worker.rs).
No SIGIL runtime pin, HTTP host version or stored-record schema changed.

## What this closes

The original worker bridge could execute a prepared ticket without a durable
claim. The durable-turn fixture ordered commit before execute, but that ordering
was not enforced by the mechanism. The new `Attempt` exclusively borrows the same
actual native store, bootstrap scope, fixed worker and clock high-water mark from
preparation until retirement. There is no caller-supplied receipt parameter.

1. Read the actual intent and claim slot through that scope. The intent must have
   positive revision and nonempty bytes; the claim slot must be revision zero.
2. Prepare the exact worker input, fuel and deadline without starting execution.
   The fixed bridge holds source/grants and returns its random ticket/generation.
3. SIGIL produces the claim transaction using those actual intent facts and generation.
4. The gate checks the transaction binding, commits it itself, and arms the attempt
   only after receiving the store's actual successful acknowledgement.
5. Recheck the time bounds and retained intent/claim, then consume the worker ticket
   once. Return the existing bridge's actual execution/reaping/result observation.

The accepted claim has exactly one read check for the bound intent's namespace,
key and revision, and exactly one create-at-zero write to the selected distinct
claim namespace at the same key. Its `SD1` record must bind that same intent and
the actual prepared generation, with phase `1`. This is mechanical validation of
the shared execution contract: SIGIL still constructs the bytes and chooses the
claim/observation/cancellation/recovery transition. No conversation, tenant allowance,
model/tool selection or delivery-transition table was added to native code.

A receipt's global transaction revision is not a record revision. A newly created
claim has record revision 1 even if the global receipt is 2 or larger. Before
execution, the gate requires the original intent record and exact committed claim
bytes at that record revision. Its exclusive store borrow prevents normal embedding
code from inserting another mutation between these steps; rechecking also fails
closed on detected storage replacement/corruption.

## Refusal and recovery

- A hint, forged receipt, claim committed elsewhere, changed generation, wrong
  coordinates/revision, terminal phase, missing check, extra write/check or malformed
  record cannot arm an attempt. Scope and store limits independently apply.
- A failed or uncertain claim commit retires the attempt without starting a worker.
  A successful commit followed by lost acknowledgement leaves a retained claim; it
  does not become permission to prepare the effect again.
- Any existing claim slot, including a versioned tombstone, prevents preparation
  at that coordinate across reopening. SIGIL may record an observed or uncertain
  result, but the gate does not reopen a claimed attempt or automatically retry it.
- Dropping a prepared/claimed attempt cancels its unconsumed in-memory ticket before
  releasing the borrowed worker. A wrong ticket is refused; a valid execute before
  claim retires the attempt. Repeated execution cannot start another worker.
- `TG1` is checked at preparation, immediately before claim commit and before worker
  initiation, against fresh wall time and an independent monotonic ceiling. Clock
  regression fails closed within the process. The guard remains SIGIL-selected;
  the mechanism does not parse credentials or choose expiry/revocation policy.
- Errors from the gate occur before invoking the worker for that attempt. Once the
  worker is invoked, its actual observation distinguishes possible execution from
  definitely-unsent and reports whether its direct child was reaped. Lost observations
  cannot be reconstructed as definitely-unsent merely because the new process
  cannot find the old ticket.

This is create-once execution fencing for one retained claim coordinate and supported
exclusive store owner, not universal exactly-once delivery. It does not prove that
an old worker or its descendants died when the controller crashed, prevent semantic
duplication under another newly authorized intent, or make restoring an older backup
safe. The existing [worker lifecycle limits](native-worker.md#time-cancellation-and-resource-bounds)
and pending Linux qualification remain. Checking time before worker initiation does
not prove that a remote service receives/completes a request before expiry.

## Embedding and current evidence

`sigil-claimed-worker/v1` is a trusted, bounded stdio embedding/conformance surface,
not a guest capability or public API. Its bootstrap fixes one existing private state
root, native scope and effect worker. It exposes native scoped get/commit between
attempts and prepare/claim/execute/cancel during an attempt. Unknown fields and
duplicate JSON keys are refused. A request cannot supply a snapshot, receipt, new
artifact or widened grants. The adapter has only pre-execution cancellation; the
Rust API retains the bridge's concurrent cancellation flag. API credentials do not
authorize access to this separate local embedding.

The durable-turn tests now use this gate for every model and file effect, forwarding
the shared SIGIL executor's exact commit bytes. A second starting path accepts the
first intent through the real authenticated HTTP API. The test then supplies the
fixed effect registry and invokes the same SIGIL/native components across process
restarts. It explicitly checks that the operation remains `accepted` and its budget
remains `reserved`: automatic product dispatch, terminal operation state and settlement
are not connected and are not claimed by this integration evidence.

Native tests exercise live receipt ownership, snapshot/claim changes, guarded
initiation, cancellation, stale tickets, scope refusal, tombstones and reopen refusal.
Real solver-verified integration tests additionally exercise exact SIGIL-produced
claims, actual file/model effects, forbidden request overrides and replacement-worker
refusal after a committed claim. Process kills/reopening are not physical power-loss
evidence. Counts and qualifying run results are recorded in the
[MVP evidence log](mvp-acceptance.md).

The newer [policy-bound path](dispatch-policy.md) supplies actual reads and held
worker metadata to SIGIL before preparing this same gate. Its version-2 stdio
configuration requires a fixed grantless policy and refuses direct preparation.
The original version-1 conformance path remains explicit, not an automatic upgrade.

The [version-3 completion path](worker-completion.md) now binds actual native observations
to a fixed SIGIL claim/result producer, owns both commits and refuses manual result
injection. Its separate native-bound abandoned-claim recovery records uncertainty
without replay or a claim that the old worker stopped. Versions 1/2 remain explicit.

The newer [v4 automatic service](automatic-service.md) now connects current authority,
SIGIL dispatch, application result interpretation and terminal settlement. Still
required are complete restart/cancellation policy, full artifact admission and the
remaining M0–M8 gates. Earlier conformance evidence above retains its original scope.

The newer [owned worker and v4 conformance adapter](owned-worker.md) retain this actual
claim path but release the storage borrow during effect execution. Pending reads and
cancellation no longer require the effect to return in that embedding. The HTTP
service now has a SIGIL coordinator but still needs explicit application cancellation policy.
