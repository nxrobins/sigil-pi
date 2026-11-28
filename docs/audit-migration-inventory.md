# SIGIL-native audit migration inventory

Checked: 2026-09-09. **Draft engineering inventory; not complete coverage or pilot
clearance.** Supports M0/M3 of [the MVP goal](mvp-goal.md). Main is still v8;
the authenticated-log and worker-audit mechanisms below live in the isolated
readiness stage `/private/tmp/sigil-pi-readiness-host.BiNfIvYV`. Exact verification
status belongs to [the acceptance record](mvp-acceptance.md), not this matrix.

## Preserve the reference boundary

[PiAgent._forge](../agent.py) is the reference agent's common execution boundary:
it records successful output, returned runtime errors and runtime exceptions for
session-bound model/parse/tool/shape calls. `AuditLog.append_or_raise` retains
source/input/output hashes, lengths, redacted grants, selected fuel, sequence and
chain linkage. The product supplies a host-held signing key, durable per-tenant
audit reservations/usage, and an `AuditVerificationMonitor` feeding readiness and
safe operational alerts. See [agent.py](../agent.py),
[product_service.py](../product_service.py) and [product_main.py](../product_main.py).

This is a baseline to preserve, not evidence that the new service is equivalent.
In particular, a successful state-transition audit does not audit every execution
that preceded it. Nor does one intact chain establish that all expected chains or
events exist. The old audit format is not silently converted to the new format.

## Coverage matrix

“Implemented” below means a local mechanism exists, not that its full product
acceptance criterion has passed. No row is an M3 qualification.

| Surface | Current staged evidence / gap | Evidence still required for qualification |
|---|---|---|
| Admission and public request decisions | The SIGIL HTTP entry enforces admission, but transaction/effect audit coupling is not a complete request-decision journal. | Accepted, replayed, denied, malformed, expired and resource-refused requests across every supported route; correct attribution or explicitly unauthenticated context; no raw token/body exposure. |
| Successful automatic state publications | `audited_transaction.rs` binds actual producer/formatter facts and publishes the returned batch plus authenticated append in one Store commit. | Event semantics and operation/tenant correlation for all applicable admission, cancellation, interpretation, settlement and administrative transitions; failed/uncertain commit outcomes. |
| Successful dispatch policy | `policy.rs` now captures DP1 from actual policy input/output/read-set/configuration and binds it through WP2; component checks and the full 682-case staged regression pass. | Actual browser/API and supported-tool coverage, exact current/original authority correlation, admitted configuration provenance and candidate-bound evidence. |
| Dispatch policy refusal or failure | Opt-in audit v2 retains actual `Policy::begin` evaluation facts through refusal; `OwnedWorker::start` consumes them once and uses a grantless SIGIL classifier plus native-only append. Compiled HTTP cases cover runtime error, malformed DW1 and alias-binding refusal without claim/provider invocation. v1 behavior is unchanged. | Complete route/tool coverage, actor/operation and observed-read correlation, exact outer refusal boundary, reporting failure/health policy, deadline/capacity cases and candidate-bound regression. Do not call a runtime error an authorization denial. |
| Claim and effect outcome | `effect_audit.rs` and the recorder publish prepared/observed/refused records with actual bindings and receipts; owned and synchronous paths retain actual observations. | Every supported effect and fault boundary; complete output only gets a digest; missing/truncated output and unknown remote outcome stay explicit. |
| Abandoned recovery | Native recovery omits lost EM1/WP1/WP2 facts and retains old/new generation correlation. Earlier committed claim evidence can remain available. | Restart, owner loss, late completion and changed/removed policy cases; no fabricated old artifact/authority facts and no silent retry of uncertain delivery. |
| Failed transaction producers | Opt-in audit v2 consumes native failure facts from `Transaction::prepare`; the fixed SIGIL classifier distinguishes failed evaluation from successful evaluation followed by mechanical refusal. Compiled signed-publication/restart cases preserve unchanged domain records and expose reporter failure without a receipt. | Complete attribution and read provenance, all producer/error paths, uncertain append fault evidence, bounded reporting and the full changed-host regression. Pre-evaluation input/read failure and successful no-op coverage remain separate. |
| No-op producer executions | The current transaction projector deliberately returns `none` when there is no batch. These evaluations do not grow its state-publication chain. | An explicit execution inventory and M0 decision on internal no-op/helper accounting; do not claim reference execution-audit parity from the publication-only chain or silently drop the obligation. |
| Other pure application evaluations | Entry, coordinator, request-policy, listing/history and other fixed functions have no complete common execution journal established by the two audit couplings. | Inventory all product guest execution paths and their result/error handling. Preserve equivalent product execution traceability; explicitly justify internal-helper exclusions without creating an unaudited effect path. |
| Scheduling and data lifecycle | Full schedule/export/delete/retention route migration remains open. | Authorized trigger decisions, normal turn admission for scheduled work, administrative result/uncertainty records, deletion and retention semantics covering state, reservations, delivery records and audit. |
| Capacity and exhaustion | Authenticated append enforces native chain/Store ceilings; a full chain blocks a claim, while post-effect publication failure can leave uncertainty. Read-only preflight reserves nothing. | SIGIL-owned prospective reservations, atomic consumption/settlement, per-tenant fairness, bounded failure reporting and restart reconciliation; no effect launched against fictitious reserved capacity. |
| Integrity, inventory and freshness | Configured existing chains are verified before dispatch. Clean absence is uninitialized, not verified history. Whole erasure/coherent rollback still need independent evidence. | Expected chain inventory, retained independent checkpoints, freshness policy, complete/torn/deleted/rolled-back history cases, periodic checks and actual readiness/alert delivery. |
| Retention, export and migration | New authenticated chains are private, not encrypted or automatically compatible with old JSONL audit records. | Versioned format/consumer behavior, access/redaction, bounded archival/deletion, quota credits, backup/restore and checkpoint retention; approved durations and operational ownership. |

The new lifecycle event's `dispatch_policy` member is additive. Old persisted v1
events may lack it; current manual/recovery projections use null when provenance
is unavailable. Neither absence nor null means a policy was observed to deny an
action. A reader must not synthesize current policy metadata for old history.

## Failure-observation capture points

The original read-only inspection on 2026-09-09 identified the loss sites below.
The subsequent opt-in v2 implementation now intercepts policy and transaction
failures; this historical table explains the distinction capture/publication must
preserve. It is not a frozen protocol approval or a complete execution inventory.

Follow-up implementation now adds a private native `Function::observe` result plus
facts in `native/service/src/evaluation.rs` in the stage. It retains actual fresh/
cached failure distinctions, optional result/output digests and admitted artifact
identities. Nine new native tests and repeated full 196-case service runs have
component evidence; the acceptance record retains the intervening failure.
The follow-up adds `evaluation_audit.rs` and a fixed SIGIL classifier. Only actual
policy/producer observations reach its private publication path. Other wrappers,
pre-evaluation failures and no-op executions still have no complete common journal.
The table below records the original behavior, not the current v2 interception.

| Capture point in the staged native service | Original behavior / remaining inventory | Required distinction |
|---|---|---|
| `lib.rs`: `invoke` / `Function::invoke_observed` | The fresh bridge's detailed observation is reduced to `worker`, `application` or `protocol`. Cached evaluator errors are also reduced to short codes. The selected timeout is returned only on success. | Preserve private native-origin invocation facts before reduction, including whether invocation was attempted, selected limits, actual available response/output digests and the observed failure. A runtime `status:error` is not an authorization denial. |
| `policy.rs`: `Policy::begin` | Reads and input are built before invocation; malformed DW1, alias/read-binding failure, post-evaluation deadline failure, and later `Attempt::begin` failure return without a published policy event. Even constructed DP1 is not retained in a prepared record unless the attempt succeeds. | Separate input preparation, actual evaluation, proposal validation and attempt preparation. A successful evaluation followed by mechanical refusal is not a failed evaluation or a dispatched effect. Do not fabricate a complete input/read set for an earlier preparation failure. |
| `transaction.rs`: `Transaction::prepare` | Actual successful output is parsed and bound before `Prepared.facts` exists. Producer errors, invalid TX1/batch and read-binding failures return earlier. | Preserve evaluation evidence separately from proposal validation. An observed output is not an accepted batch or a committed transition. |
| `audited_transaction.rs`: `AuditedTransaction::apply` | The formatter is reached only after `prepare` succeeds. A valid no-batch result deliberately returns no append or checkpoint. | Inventory successful no-op executions and failures explicitly; do not infer complete execution coverage from state-publication events. Keep an actual commit receipt distinct from a generated event. |

The fresh and cached paths do not currently expose identical observations.
`native/worker/src/pure.rs` returns a parsed runtime value or a fault and retains
a reusable, grantless process; the fresh bridge returns an `Observation` with
request/cleanup facts. Do not reconstruct cached request-start or reap flags from
the fresh path's schema. Absence of an observed output must remain absent, not the
digest of an invented empty output. A complete runtime-result digest, if retained,
must identify its exact representation rather than masquerading as guest output.

The implementation preserves existing v1 result/error behavior; v2 explicitly
reports unavailable/uncertain failure publication. Private, non-caller-replaceable
evidence goes to fixed SIGIL event policy. Only SIGIL interprets application refusal
or event meaning. Reporting
cannot supply a claim, effect ticket, domain batch, original result or receipt;
failure journaling must not become another dispatch path. Retain no raw bearer,
model conversation, runtime diagnostic payload or provider secret in published
events. Bind the actual admitted configuration and operation context where the
boundary knows them, without attributing unauthenticated failures to a guessed
tenant or inventing context after it has been lost.

Failure reporting needs an explicit bounded time/capacity outcome. It cannot
extend an expired action deadline or claim a durable event after formatter,
capacity or storage failure. The existing runtime/request ceilings and effect
claim gate remain; audit failure itself must not create recursive unbounded work.
Required cases include pre-invocation refusal, runtime error, unavailable/truncated
result, malformed proposal, binding refusal, expiry after evaluation, no-op,
reporter failure and restart, with unchanged no-claim/no-effect assertions.

## Production invocation-site inventory — 2026-09-09 follow-up

This is a read-only inventory of the staged `native/service/src` execution calls,
not a claim of complete application audit coverage. It distinguishes actual guest
invocations from native clock/storage observations and wrapper calls. The staged
production source is the same as the focused 224-case snapshot; the newer test
additions and broader regression do not themselves add common execution coverage.

| Actual execution boundary in the stage | Available native context / current coverage | Required next evidence |
|---|---|---|
| `lib.rs`: `Machine::run`, entry `invoke` (line 492) | Boot and each request continuation use the actual entry, framed request/facts and one original deadline. Requests have a native-generated correlation ID; matched credentials supply the actual scope, while unmatched requests have none. The wrapper drops evaluation facts. | Capture each intended product execution and final request decision with authenticated or explicitly unmatched context, without treating a transport ID as an operation or caller authority. Include failures before/after each selected action and distinguish already committed state. |
| `lib.rs`: `Step::Call` (line 567) | A fixed registered grantless helper receives only selected bytes. Its output is data; only the entry can choose a native action. Helper invocation facts are not journaled by the current audit couplings. | Enumerate the actual admitted helper registry, including admission/request-policy/history/listing/readiness functions. Retain selected alias/artifact, actual input/result and parent invocation binding; a helper result is not a command, authorization verdict or commit. |
| `automatic.rs`: coordinator boot/instruction (lines 320, 528) | Actual CL1 includes held participant facts, bundle, binding, clock, stage, observation, continuation and owned effect coordinates. The grantless coordinator's own invocation facts are dropped, even when its chosen transaction/effect later has an audit event. | Cover normal, yield/read/discovery, error and recovery decisions without auditing only state-mutating outcomes. Preserve the original error observation and actual held-flight facts; do not infer a new delivery from a coordinator instruction. |
| `policy.rs`: `Policy::begin` (line 251) | Actual policy evaluation is observed; successful DP1 binds input/output/read-set/configuration to the claim path. Opt-in v2 owned-worker failure publication retains limited mechanical failure evidence. | Complete actor/operation, actual read-set, supplied/checked clock and outer refusal-stage binding. Pre-evaluation reads/input errors, manual callers and later claim failures are not implicitly covered by the v2 callback. |
| `transaction.rs`: `Transaction::prepare` (line 218) | Actual producer evaluation is observed. Audited state publication couples a valid batch and event atomically; v2 intercepts producer/proposal failures. Plain transactions and successful no-batch results have no complete common execution journal. | Inventory every configured producer and intentional no-op, plus post-proposal formatter/storage failure. Preserve returned context as opaque until SIGIL validates attribution; a proposed batch is not a committed transition. |
| `claimed/recorded.rs`: `Recorder::propose_bound` (line 184) | WR1 comes from actual intent/claim snapshots, held generation and worker facts. Successful ER1 can feed the lifecycle projector and coupled commit. Failed evaluation, malformed ER1 or later binding refusal can exit before that publication. | Add failure coverage using the already held native bindings; distinguish claim production, observed-result recording and abandoned recovery. Reporter failure must never erase a prior send or authorize a replacement execution. |
| `audited_transaction.rs`, `effect_audit.rs`, `evaluation_audit.rs`: fixed audit projector invocations | AB1/WB1/EB1 boot checks and TA1/WA1/EA1 projections are separate grantless invocations. AR1 is a publication proposal; native append owns signatures/receipts. Projector failure does not recursively invoke another auditor. | Define the bounded internal-reporter diagnostic/health/accounting contract explicitly. These invocations cannot be exempted in a way that leaves product effects or refusals unaudited, but recursive auditing is not a completeness strategy. |
| `claimed.rs` and `claimed/owned.rs`: actual effect `Bridge::execute` | This is a separately scoped effect invocation, not a second pure `Function::observe` event. Existing lifecycle records bind real claim receipts, generations and available worker observations. | Preserve uncertain delivery and all effect/recording failures, including missing output and lost ownership. Common execution inventory must not duplicate these as invented pure evaluations. |

`Function::invoke` → `invoke_observed` → `observe` is **one** invocation with
wrappers, not three executions. `ProcessFacts::observe`, storage/read-set
observations and read-only runtime inspection do not invoke application SIGIL;
they belong to the native fact/import inventory. The eight-entry-evaluation
ceiling and fixed function registry remain unchanged.

Transport/parser/queue rejection can occur before any entry invocation. It needs
its own bounded request/operational evidence; do not fabricate an evaluated
artifact, matched principal, full input digest or application decision for that
earlier failure. Likewise, admission before state creation cannot obtain a receipt
from a state store that was deliberately never opened.

This inventory identifies the next integration surfaces; it does not approve
helper exclusions, reset existing coverage obligations, supply M0 retention or
quota values, or count any of these rows as M1/M3 qualified.

A subsequent working copy at `/private/tmp/sigil-pi-recorder-audit.Jl8kYpDT` now
adds opt-in v2 capture/publication of recorder evaluation and ER1-framing failures
at the shared `Recorder::propose_bound` boundary. All 208 native service tests and
warnings-as-errors pass; eight new actual HTTP/SIGIL cases plus the existing v2
normal turn are still running at this checkpoint. The original readiness copy
and its live 766-case regression remain unchanged. This is not yet compiled
workflow qualification or complete recorder-failure coverage: successful framing
retires the observation before the lifecycle projector, and later proposal-binding,
formatter or commit refusals are not classified as failed recorder evaluations.
The actual recorder config/scope and bounded lookup coordinates are bound, but
complete actor/operation/read/clock and outer-refusal-stage attribution remain
open. See the dated follow-up in [the acceptance record](mvp-acceptance.md).

## Invariants for the remaining implementation

- SIGIL owns event meaning, attribution policy, redaction, allowance decisions and
  recovery interpretation. Native mechanisms supply actual admitted artifacts,
  invocation observations, clocks, hashing/authentication and atomic publication.
- A public caller, tool result or model cannot supply a trusted observation,
  receipt, authorization result, signer input or private audit grant. A digest of
  caller data does not establish native provenance.
- Record only what the boundary observed. A successful runtime result is not a
  business-success guarantee; a cancellation request is not remote cancellation.
  A failed recorder does not erase an external effect or authorize its retry.
- Correlate operation, attempt/generation, policy/configuration and exact input
  without logging conversations, raw credentials, provider secrets or signing
  keys. Hashing is not encryption; access and retention remain necessary.
- Publication, capacity reservation and domain acknowledgement have distinct
  meanings. Do not advertise a read/check as reserved capacity or a generated
  event as durably published before the real commit receipt exists.
- Failure reporting itself must remain bounded and cannot recursively require an
  unbounded audit of the audit formatter. Define and test this internal mechanism
  boundary explicitly; do not use it to exempt product effects or refusals.
- Keep original source/evaluation/fuel/memory/deadline limits and all applicable
  regression expectations. Numeric pilot reservations, retention periods and
  readiness freshness must be owner-approved in M0, not inferred from fixtures.

## Next implementation boundary

Complete the changed-host regression and v2 positive/negative workflow coverage
for the new failure publisher, retaining the earlier 682 cases and all native,
fixed-evaluator and frozen-v8 prerequisites. Its focused verification status is
recorded separately; the old 682 pass does not qualify the changed source.

Then complete the common execution inventory, actual actor/operation/read binding
and prospective audit-capacity accounting. The new v2 event is deliberately only
mechanical evidence: it has no invented principal/operation, authority verdict,
claim, delivered result or commit receipt. Original lookup values are represented
only by a bounded framed hash/count. Existing v1 profiles do not silently opt in.
`audit_recording` reports unavailable publication; `audit_commit_uncertain` must
not become proof of absence or trigger automatic append replay. Native callbacks
expose these codes, but complete SIGIL readiness/alert policy remains open.

This ordering is engineering work toward the unchanged full goal, not permission
to omit HTTP decisions, remaining routes, retention, candidate qualification or
independent review. It does not authorize provider spending, deployment, runtime
pin changes or edits in the control-plane/SIGIL repositories.

## Integration checkpoint — 2026-09-09

The base changed-host regression completed with 766 passing cases, and the later
recorder-observation extension completed its 13-case focused rerun. These use
different source/artifact sets; the earlier recorder failure remains documented.
Both mechanisms and their tests are now integrated as optional v9 development
support, retaining existing v8 profiles and an actual frozen-v8 compatibility
fixture. The integrated whole-source gate is pending. See the latest
[acceptance checkpoint](mvp-acceptance.md) and
[recorder evidence](recorder-evaluation-audit.md).

This supersedes earlier stage-only and pending-focused-run statements, not the
inventory's coverage gaps or qualification requirements. Complete common-call
coverage and attribution, prospective capacity, health/alert/retention behavior,
route parity and all full MVP gates remain open.
