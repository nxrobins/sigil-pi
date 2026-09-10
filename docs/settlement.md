# Durable terminal results and reservation settlement

Status: **LOCAL DEVELOPMENT IMPLEMENTATION — used by the opt-in automatic v4 service**.
The [SIGIL settlement component](../app/pi/settlement.sigil) publishes a terminal
operation result and settles its outstanding reservation in one native transaction.
The real [HTTP API](native-service.md) can read that result after restart. Older
conformance fixtures retain a trusted step driver; the new [automatic service](automatic-service.md)
invokes interpretation and settlement through SIGIL without that driver.
Neither path completes M1, M2 or M3 of [the MVP goal](mvp-goal.md).

## One atomic terminal publication

Settlement receives fixed authority/bundle bindings, operation/session lookup values,
actual native time and actual operation, conversation, reservation and budget reads.
SIGIL correlates the original admission, exact profile and tool-filtered configuration,
tenant/principal, session/submission, operation identity, deadline and bundle. A first
settlement requires `OQ2` and `BR1` at record revision 1 and a terminal `PT1` state:
`done`, `failed`, `cancelled` or `uncertain`. Missing, tombstoned, mismatched, active or
mixed settled/unsettled records cannot produce a terminal transaction.

One exact batch checks the terminal conversation revision and writes:

- `OQ3`: the original operation context plus its terminal phase, typed result and
  the revision of the terminal conversation snapshot.
- `BR2`: the original reservation plus reported usage, whether usage is known,
  accounting disposition and publication time.
- `BH1`: the corresponding change to outstanding reserved capacity.

Every write uses its actual prior record revision. Any stale precondition or denied
write aborts the whole batch. The conversation is not rewritten. The native receipt
acknowledges the actual commit; a proposed batch or a failed response is not a receipt.
An acknowledgement lost after commitment is recovered by inspecting retained records,
not by treating the operation as uncommitted.

The [admission component](admission.md) now requires a valid retained `OQ3` at revision
2 before continuing a `done` conversation. It binds the old operation, tenant, phase
and exact terminal conversation revision and includes an operation read precondition
in the next admission batch. This prevents replacing the old conversation before its
result has been saved. Failed, cancelled and uncertain conversations still refuse
follow-ups; their explicit recovery/product policy remains to be implemented.

## Capacity is not billing

SIGIL always releases one active-turn slot on first settlement. Token capacity follows
one of three explicit dispositions:

| Disposition | Observation | Capacity action |
|---|---|---|
| `reported` | Known input/output totals, neither above the original reservation | Release the entire original input/output capacity reservation |
| `unknown` | Usage is not fully known | Keep the entire original token reservation held |
| `overrun` | Known totals exceed either original reservation | Keep the entire original token reservation held |

The ledger must contain at least the original reservation and must not exceed its
configured capacity caps. An uncertain conversation cannot claim known usage. The
reported totals and disposition remain in both terminal records. A repeated settlement
validates that retained pair and returns no new commit or receipt, even after a later
turn replaces the old conversation. It cannot release the next turn's reservation.

`BH1` measures outstanding capacity, **not cumulative charged usage or money spent**.
Releasing a known reservation does not refund a provider charge. Keeping an overrun's
original reservation does not cover spending above that amount. Prospective input
metering, provider reconciliation, rolling/rate allowances, expiry sweeping and an
explicit resolution process for retained unknown/overrun holds remain missing.
These mechanisms are not a qualified provider-spending guarantee.

Accounting may be recorded after the original credential/operation expires: this
performs no external effect and grants no new dispatch authority. The installed
authority must match the original facts byte-for-byte except validity endpoints,
allowing equivalent validity rotation. Epoch, principal, tools, scopes, profile,
namespaces and bundle changes require explicit handling rather than silently adopting
new policy for an old operation. Publication time must not precede admission; repeat
settlement must not precede the retained publication time. A durable trusted clock
high-water and live revocation are not established by this rule.

## Typed records and public lookup

Markers use the existing eight-digit UTF-8 byte-length framing. `SF1` has nine fields:
fixed `CF2`, fixed bundle, operation lookup, session lookup, host seconds, then actual
operation/state/reservation/budget `SR1` records. It is internal, not an HTTP request.

`OQ3` has the eleven `OQ2` fields with terminal phase at field 9, followed by `OR1`
and the exact terminal `PT1` revision. `BR2` has the nine `BR1` fields with disposition
at field 7, followed by input total, output total, usage-known and publication seconds.
`OR1` has reply, input total, output total, usage-known (`0`/`1`), error, sequence,
publication seconds and disposition. The shared
[operation codec](../app/pi/operation_records.sigil) validates canonical bounded
fields and renders public JSON; stored arbitrary JSON is not trusted as a response.

`GET /v1/operations/{id}` retains the existing tenant-and-owner check and reports
accepted `OQ2` or a revision-2 terminal `OQ3`. A terminal response contains `operation`,
`status`, `reply`, `usage` (`input_tokens`, `output_tokens`, `known`), `error`,
`sequence`, `completed_at` and `accounting`. `completed_at` is terminal publication
time, not a provider's effect-completion timestamp. Errors are strings (empty on a
normal completion); `known` is a boolean. Other principals receive 404 before outcome
validation. An authorized malformed retained outcome fails closed. Old results remain
readable after follow-up admission. Submission replay still returns its original
accepted identity; polling is the way to obtain terminal status.

## Native-bound transaction mechanism

The generic [transaction host](../native/service/src/transaction.rs) supplies actual
scoped reads and clocks to an owner-installed pure SIGIL function. Its fixed template
accepts bounded lookup values, not caller-authored snapshots, authority, clocks,
outcomes, commits or receipts. Native code does not interpret pi records, terminal
phases, allowance policy or accounting dispositions.

The function returns `TX1`: two opaque context strings and exact commit JSON, empty
only for an application-selected no-op. For a nonempty proposal the native host requires
every check/write to name an actual read at its exact revision, every read to be covered
exactly once, and at least one write. Native scope and storage ceilings independently
enforce the commit. The exclusive store handle spans read/evaluate/commit; it does not
authenticate application policy or establish multi-owner support.

The function has fixed source/runtime hashes, independent fuel/time bounds, and no
filesystem, network or secret grants. Native time-regression and monotonic checks run
before evaluation and before commit initiation. A started storage commit is not undone
by expiry or a lost acknowledgement. `receipt: null` means this invocation did not issue
a commit; the opaque context is not an execution capability or fresh commit evidence.

The trusted stdio binary `sigil-transaction` accepts one version-1 owner configuration
and only `apply` requests with lookup values. It opens existing state, never initializes
missing/corrupt state, and is **not a public authenticated API**. Bootstrap is at most
64 KiB; templates have 1–32 inputs and at most 16 caller values, each at most 512 bytes.
Fixed literals are at most 16 KiB and total framed input at most 4 MiB. Read inputs
require actual read grants; effect-worker facts are not valid inputs here. Production
registry selection and dispatch authorization remain the service's integration work.

## Evidence and remaining work

Build composition shares the exact operation codec among API, admission and
settlement. For these three compiler inputs it also removes leading indentation
outside literals/block comments, while preserving newlines, tokens, literal bytes
and original-source hashes. This is layout compaction, not a relaxed 64 KiB source
cap or verification bypass. The existing dispatch and turn-transaction compiler
inputs are unchanged. Composition tests check both emitted code and preservation
of strings, multiline literals and comments.

The [component tests](../tests/test_settlement.py),
[contract tests](../tests/test_settlement_contract.py) and
[native integration tests](../tests/test_native_settlement.py) cover terminal states,
unknown/overrun holds, record correlation, stale atomic batches, malformed outcomes,
forged requests/proposals, native scope refusal, restart and lost acknowledgement.
The recorded modes of [the durable dispatch test](../tests/test_native_dispatch.py)
run the actual model/file/model path through native-bound settlement and real HTTP
terminal lookup using a controlled local provider. See [the acceptance record](mvp-acceptance.md)
for completed runs; controlled providers do not qualify real-model usefulness.

All remaining API routes, an automatic SIGIL-driven service loop, browser chat,
full quota/audit/retention behavior, actual control-plane reuse, supported Linux
fault/load/restore evidence and pilot clearance remain required. `AP2`, `OQ3` and
`BR2` are new development contracts, not a qualified state upgrade/rollback path.
No old state is erased at startup, and no runtime pin or proof gate was relaxed.
