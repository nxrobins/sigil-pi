# Native-bound SIGIL completion and abandoned-claim recovery

Status: **local execution mechanism, not an automatic product service or MVP clearance**.
The [goal](mvp-goal.md) and [acceptance record](mvp-acceptance.md) remain authoritative.

The fixed, grantless [SIGIL completion component](../app/shared/worker_completion.sigil)
classifies native worker observations and builds the existing `EX1` request. Composition
reuses the exact [executor transaction producer](executor-transactions.md) and delivery
kernel; there is no second transition table in Rust or Python. The
[native recorder](../native/service/src/claimed/recorded.rs) owns actual storage reads,
worker observations, execution generations and commit receipts. It forwards SIGIL's
exact proposed transaction only after checking its mechanical binding.

## Ownership and ordering

The version-3 trusted stdio embedding requires the existing fixed SIGIL dispatch
policy and a fixed completion worker. Both are grantless. The executor has read-only
intent access, read/write access to one fixed claim namespace, and create-only access
to one distinct fixed delivery namespace. All other executor namespaces must be
read-only. Policy reads have their own fixed read-only scope; the completion worker
cannot access storage or effects directly. Full operator-template/package admission
is still required; installing arbitrary policy code is not made safe by this protocol.

`authorize` uses the [native-bound dispatch policy](dispatch-policy.md) and creates a
held `Attempt`. `run` accepts only that attempt's actual ticket, and then:

1. Checks that the actual delivery coordinate was never used and that the held scope
   permits creation. A retained value or tombstone refuses before any claim or effect.
2. Supplies actual intent/claim snapshots and the prepared worker generation to SIGIL.
   Commits its exact claim through the receipt-bound gate before invoking the effect.
3. Executes the frozen one-use worker with fresh initiation-time checks, or records an
   actual native gate refusal before worker invocation. No caller-supplied result enters.
4. Supplies the actual observation and fresh retained snapshots to the fixed SIGIL
   completion worker. Validates and commits its exact claim-update/delivery-create batch.
5. Returns a phase only when the actual terminal commit is acknowledged. Failure after
   execution retains the observation plus `recording_error`, without a delivery receipt
   or an invented durable phase. Storage errors can mean an uncertain commit; reopening
   and reading actual records is required, not resending the effect.

Native validation requires exactly one read check for the bound intent revision, one
claim update at record revision 1 and one delivery creation at revision 0. `SD1` and
`DR1` must bind the same actual intent, original worker generation and SIGIL-selected
terminal phase. No application-domain write or changed output coordinate is accepted.
The store's scope, integrity checks, resource bounds and atomic CAS remain independent.

The effect's original deadline still includes preparation/claim work; it is not reset
to compensate for compilation. Result recording gets its own bounded interval, at most
the fixed pure worker's 30-second ceiling: expired initiation authority cannot erase an
observation of work already started. This is not new authority to send another effect.

## Draft records and interpretation

Four-byte markers include LF. Every field is prefixed with eight decimal digits of
UTF-8 byte length. Wrong versions, trailing bytes and contradictory facts are refused.

| Record | Ordered fields |
|---|---|
| `WR1` (10) | intent namespace; claim namespace; delivery namespace; key; actual intent revision; actual intent bytes; actual claim revision; actual SD1 bytes; native worker/recovery generation; WF1 |
| `WF1` (9) | kind; generation; request-may-have-run (`0`/`1`); worker-reaped (`0`/`1`); fault; runtime result status; output kind; actual output UTF-8 byte length; bounded output text |
| `ER1` (3) | SIGIL phase; continuation hint; exact native commit JSON |

Output kind is `missing`, `string`, `other` or `oversized`. Strings up to 1 MiB retain
their exact bytes and length. Larger strings retain their real length and an explicit
oversized tag, never a truncated successful body. Diagnostics/unrelated metadata are
not substituted for output. The bridge's independent 16 MiB transport limit remains.

| Native facts | SIGIL classification |
|---|---|
| Prepared, unused claim, confirmed no invocation | Claim phase 1; no effect until actual commit |
| Native pre-invocation refusal or observed confirmed no-send with reaped worker | Phase 3, definitely unsent |
| May-have-run, reaped worker, no fault, runtime status `ok`, complete string output | Phase 2, observed `returned` with exact output |
| Runtime error/trap, missing/non-string/oversized output, or unconfirmed cleanup | Phase 4, possibly delivered, no invented result |
| Abandoned live claim, no authoritative result/cleanup observation | Phase 4, possibly delivered, original claim generation retained |

A runtime status of `ok` is not an HTTP status or a business-success verdict. Even a
returned empty string is an observation that the application must interpret. Conversely,
a runtime error is not proof that a remote service did nothing. The conservative
may-have-run signal is the first forge-request byte written to the child, not proof of
HTTP delivery. These records do not yet supply every status/usage/timestamp/digest fact
required by the complete draft execution contract.

## Restart without silently repeating effects

Between attempts, trusted version-3 `recover` accepts an intent namespace and key only.
The claim/delivery namespaces come from bootstrap, not the request. It reads the actual
intent and live phase-1 claim, checks their correlation and actual unused delivery slot,
and creates a fresh native recovery-observer generation. It supplies `abandoned` facts:
possible execution, **unconfirmed** cleanup, `owner_unavailable`, no result and no output.
The original worker identity, not this new observer identity, remains in `SD1`/`DR1`.

The exclusive store borrow prevents recovery while a synchronous `Attempt` is held.
It does not exclude the newer [owned in-flight worker](owned-worker.md); its embedding
coordinator must exclude actual active handles before choosing abandoned-claim recovery.
The inner v3 stdio loop also refuses recovery. This proves neither that an orphaned worker is dead
nor that a remote request has stopped. No effect worker is prepared or invoked by
recovery. A terminal claim, missing/malformed/mismatched claim, used delivery coordinate,
out-of-scope lookup or failed commit cannot reopen the effect. Late responses cannot
replace terminal uncertainty; explicit reconciliation would need a separate protocol.

The result includes the actual delivery receipt, committed phase, original claim
generation and new recovery generation. It does not manufacture a worker-reaped
observation or a receipt for the earlier claim. This is a trusted local mechanism,
**not** an authenticated user-facing recovery endpoint or automatic recovery scheduling.

Versions 1 and 2 retain their explicitly manual conformance behavior. Version 3 refuses
manual claim/execute and arbitrary commit requests; unknown fields and duplicate keys
are refused. Its `cancel` request only retires a pending attempt before `run`. Concurrent
cancellation remains available through the Rust API's actual bridge flag, not through
the synchronous stdio loop. HTTP API semantics/version and durable SD1/DR1 schemas did
not change. No source cap, certificate check or SIGIL runtime pin was relaxed.

The explicit [v4 conformance adapter](owned-worker.md) adds owned start/poll and
generation-correlated cancellation while preserving actual observation/receipt
ownership. It does not add public HTTP cancellation or automatic recovery scheduling.

## Evidence and remaining boundary

Tests use the solver-verifying pinned runtime, real native storage and local controlled
providers. They exercise full model/file/model turns, forged-result refusals, scope and
delivery collisions, independent recording time bounds, and controller termination
before and after result commitment. The abandoned delivery is consumed by the real pi
SIGIL transaction adapter, which stops the conversation as uncertain without a retry.
See the acceptance record for exact run status and source identities.

Older conformance tests retain a trusted step fixture. The new
[v4 automatic service](automatic-service.md) performs registry selection, delivery
interpretation and [settlement](settlement.md) through SIGIL without that fixture
driver. Its source evidence is separate from the component tests described above.
Public cancellation and complete recovery policy remain open, as do public
API/browser parity, full quotas/audit/retention, qualified
Linux worker lifetime/restore behavior, real-provider usefulness, actual control-plane
reuse and all other M0–M8 criteria remain open. Process termination is not physical
power-loss evidence. The pinned HTTP shim's two-second timeout remains unchanged.
