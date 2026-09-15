# Finalizing work that was never claimed

Status: **LOCAL DEVELOPMENT IMPLEMENTATION — focused source/HTTP checks passed**.
The [acceptance record](mvp-acceptance.md) distinguishes completed checks from
remaining product/candidate gates. The original `UF1` behavior below is retained as
a conformance reference; the automatic service now selects the cancellation-aware
`UF2` extension described below. Neither is live revocation.

The [SIGIL component](../app/pi/preclaim.sigil) lets the automatic service publish
a truthful terminal result when the current action has never been claimed and can
no longer proceed under the unchanged application policy. It uses the same allowance
helper as [dispatch](dispatch-policy.md) and the exact [settlement](settlement.md)
implementation, rather than adding native usage or conversation policy.

## Eligibility and refusal

The native transaction template supplies current credential facts, the actual bundle,
native time and seven scoped snapshots. SIGIL requires both claim and delivery to be
absent: revision zero AND empty value. A retained tombstone or any positive revision
cannot be reclassified as never dispatched. Operation, reservation and intent must be
at revision one; the current conversation must be active with a positive, incrementable
revision. All operation/session/action identities and the exact intended payload must
match the state. The original settlement authority/profile/accounting invariants also
apply, including on an eligible no-op.

The application checks these conditions in order:

1. The turn deadline has been reached: `deadline_exceeded`.
2. Current credential validity excludes the current time: `credential_inactive`.
3. For a model action, prior usage is unknown: `usage_unknown`.
4. For a model action, input allowance is exhausted or the next maximum output
   cannot fit its reservation: `quota_exhausted`.

Otherwise it returns an eligible no-op. This is **not an execution grant**: dispatch
still checks authority, exact worker binding, grants, intent and time before claiming
and again enforces the dispatch time guard at handoff. Expiry between eligibility
and start leads to reevaluation, not permission to send late.

## One atomic publication

SIGIL constructs a proposed failed conversation at the next revision and invokes
the existing settlement producer entirely in memory. It does not commit a temporary
terminal state. It replaces settlement's proposed-future-state precondition with a
write against the actual current conversation revision and adds actual intent,
absent-claim and absent-delivery checks. One transaction therefore has:

- Three checks: current intent revision, claim absence and delivery absence.
- Four writes: terminal operation `OQ3`, reservation `BR2`, capacity `BH1`, and
  terminal conversation `PT1`. The operation names that new conversation revision.

The generic native binder independently requires all seven actual reads exactly once
across checks/writes, with exact coordinates and revisions. Native scope still denies
claim/delivery writes. A stale check aborts the entire batch. No claim or delivery
record is invented, and no external effect occurs in this transaction.

All previously observed usage is preserved. Never sending the current action does
not mean earlier usage was zero or known. Existing settlement releases the active-turn
slot; unknown/overrun token holds remain reserved. Known usage releases capacity, not
provider charges. Repeating a terminal operation cannot release capacity again: the
coordinator skips its settled revision, and this component refuses reopening it.

## Internal contract and registry version

`UF1` has 15 length-prefixed fields, in order: current `CF2`, actual bundle, operation
lookup, session lookup, intent key, claim namespace, delivery namespace, native seconds,
then actual operation/reservation/state/intent/budget/claim/delivery `SR1` snapshots.
Public requests cannot supply these facts or the resulting commit.

The existing `TX1` contains two opaque context strings and the proposed commit JSON.
For eligibility its context is `["eligible", ""]`, with empty proposal and actual
native `receipt: null`. For finalization its context is `["failed", reason]` and
the native result includes the actual positive commit revision. SIGIL's coordinator
requires the matching native transaction stage and consistent context/receipt before
starting or rescanning. A receipt is not effect authority.

The earlier `LB2` introduced the fifth, fixed preclaim alias. The current automatic
service requires `LB3`, exactly two effect and three transaction aliases, and the
`UF2`/`DF2` cancellation-aware components. `LB1`/`LB2` are rejected, not silently
upgraded. HTTP configuration remains development v4, but changed sources and registry
change the full bundle. There is no implicit migration of old accepted operations
or old deployment configurations.

## Current cancellation-aware extension

[UF2](../app/pi/preclaim_cancellable.sigil) adds the actual cancellation lookup key
and `SR1` snapshot to the fifteen `UF1` fields. Both versions compose the exact
same `finalize_unclaimed` implementation and settlement producer. A valid bound
request chooses `cancelled` before expiry/validity/allowance reasons. The actual
cancellation revision becomes a fourth check, so the terminal transaction covers
all eight actual reads with four checks and four writes. Existing claim/delivery
absence and authority/accounting requirements are unchanged. An eligible no-op
still grants nothing: `DF2` rechecks cancellation at the actual dispatch boundary.
See [public cancellation](cancellation.md) for request, race and scope semantics.

## Explicit limitations

Only validity-endpoint rotation is compatible with existing settlement. Changes to
principal, tenant, epoch, scopes, tools, namespaces, profile or bundle still refuse;
removed principals need an explicit retired-owner recovery protocol. Other mechanism
or artifact failures can still leave accepted work requiring operator attention.
Claimed work uses the existing recorded-observation/uncertain recovery path, not UF1/UF2.
Full cancellation-boundary qualification, intermediate progress, retained-hold reconciliation, full quota
parity, complete fault/load evidence and all M0–M8 qualification remain open.
