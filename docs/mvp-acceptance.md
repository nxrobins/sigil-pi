# SIGIL-native MVP acceptance record

Updated: 2026-09-10 UTC. **NOT READY; no qualifying MVP candidate exists.**
Authority: [MVP goal](mvp-goal.md). This record is separate from GA qualification.

| Gate | Status | Current evidence / missing work | Decision owner |
|---|---|---|---|
| M0 Contract | PARTIAL | [Draft execution contract](execution-contract.md), [route inventory](../config/api-migration.json) and [initial ownership inventory](sigil-migration-inventory.md); exact pinned runtime inspected. Codecs, cross-project review, pilot profile and owner approval remain open. | Engineering; product owner approval pending |
| M1 SIGIL product | PARTIAL | The [automatic native/SIGIL service](automatic-service.md) drives a real HTTP model/file/model turn and follow-up without a Python step driver. SIGIL owns admission, selection, interpretation, cancellation, settlement, [retained-history retrieval](session-history.md) and [discovery in v5/v6 profiles](session-discovery.md). The development browser and [v7 HTTP metadata profile](http-exchange.md) are integrated; the v8 request-admission profile is also integrated. Its expanded 2,702-case source gate passed on unchanged inputs; this is local macOS source evidence, not candidate qualification. All eleven legacy route migrations and complete recovery/cancellation qualification remain; legacy Python behavior is unchanged and full parity is not established. | Engineering |
| M2 Durable execution | PARTIAL | Authenticated admission, native-bound dispatch, [completion/recovery](worker-completion.md) and [settlement](settlement.md) use actual reads and receipts. The automatic coordinator connects these mechanisms. Never-claimed expiry/allowance finalization and [public cancellation](cancellation.md) have focused local evidence, with exact qualification below. All interruption boundaries and changed/removed authority remain open. | Engineering |
| M3 Migrated security | PARTIAL | The automatic registry binds current authority, scoped grants, artifacts and native observations; callers cannot supply results, snapshots, commits or receipts. Unknown/overrun token holds are retained. Complete accounting/prospective metering, full tool/route boundaries, audit/retention, artifact admission and candidate qualification remain. | Engineering; independent reviewer pending |
| M4 Interfaces/usefulness | PARTIAL | Operation submission/lookup/cancel, [retained-history paging](session-history.md) and [conversation discovery](session-discovery.md) are connected to the SIGIL API. [20 draft task cases](../acceptance/README.md) and a controlled workspace exist. The opt-in [development browser](browser-interface.md), its original 21 HTTP/browser cases and v7 retargeted cases passed in the 2,687-case source gate. Fourteen further v8 browser cases passed in staging, were integrated and passed the expanded 2,702-case source gate. Earlier staged screenshots were inspected, with limitations recorded below. Broader browser boundaries, full lifecycle/API parity, real-model qualification and independent onboarding remain. | Product/engineering; participants pending |
| M5 Recovery/load | MISSING | v0.4.0 evidence is historical, with a known unreleased full-disk fix. No MVP candidate or migrated-operation fault/load evidence. | Engineering/operations |
| M6 Shared reuse | MISSING | Control-plane design inspected; its existing two-slot executor is not conformance to this draft. Integration review and actual second-consumer run remain. | Respective repository owners |
| M7 Candidate | MISSING | No SIGIL-native product package, clean-host install or final protected source/contract/browser evidence. | Engineering/release owner |
| M8 Pilot entry | EXTERNAL | Deployment/model/spending/retention choices, named owners, independent review/scans and sign-offs remain. | Product, operations, independent security |

No row is PASS. Source tests cannot qualify candidate/runtime/browser or operational
criteria, and candidate-bound evidence must be re-established after migration.

Latest development integration: optional v9 readiness/storage and authenticated
transaction/effect/evaluation audit mechanisms are now in the repository, with
unchanged existing v8 configuration behavior and frozen-v8 compatibility fixtures.
The base stage passed 766 cases; the later recorder extension passed 13 focused
cases on different binaries. Its earlier failing run remains in the evidence.
The expanded 3,635-case whole-source run is terminal FAILED: its sole reported
test failure was the stale README collection count, and CI stopped before the
coverage command. A preserved-data diagnostic separately passed the unchanged
coverage thresholds (91.23% line / 85.98% branch), not the full CI gate.
[SIGIL health/version and truthful browser usage display](service-info-migration.md)
are now integrated; their 100 JavaScript tests passed. The [native deployment
builder](native-deployment.md) is also integrated after 57 staged checks passed,
including actual copied-host/browser/API turns and restart. Main collects 3,778
cases and lint passes. The README count is corrected; its guard and all original
gates are unchanged. The new integrated full-source run is being prepared.
The last completed full
source pass remains the preceding v8 integration, not these newer bytes.
All nine gates above remain unqualified; this is not full legacy parity or a pilot.

## Historical implementation checkpoints

The sections below preserve chronological evidence and the limitations at each
checkpoint. Use the gate table above and the latest dated section at the end for
current status; earlier "next" steps and test counts are not current readiness claims.

The preceding goal-definition turn was progress: it created the charter and preserved
the separate GA gate. This first implementation turn established an explicit additive
API design and inspected the pinned runtime rather than adopting the newer sibling.
`tests/test_api_migration.py` adds 36 passing, toolchain-free oracle checks: exact
11-route/method/scope inventory, each route's missing/unknown/insufficient credential
refusal before agent work, synchronous chat response/tenant/tool semantics, and refusal
of new operation fields on legacy chat. These are not complete route parity or runtime
evidence.

The first shared SIGIL component is `app/shared/delivery_state.sigil`: a pure
delivery-state transition with no host imports or I/O authority. Its 53 checks cover
all 42 phase/event pairs, ten malformed input cases, and an authority/provenance
source guard. Cancellation or lost ownership after dispatch preserves uncertainty;
terminal observations cannot reopen an attempt. Authentication, correlation,
granted dispatch and durable commitment are explicitly outside this component.

The 36 oracle checks and 53 component checks passed together on 2026-09-07:
89 passed, no skips, using a fresh solver-enabled build of public SIGIL
`8277a1d92d599df89e6b4391fc70fd0fa534d696` in an isolated temporary checkout.
The product client removes the unverified-certificate override, and
`PI_REQUIRE_TOOLCHAIN=1` made missing toolchain evidence a failure. This local run
used macOS arm64, Python 3.14.6 and Homebrew Z3 4.16.0, not the required Linux
candidate topology or CI's Z3 4.12.2 dependency profile. The suite now collects
925 cases (924 plus one declared research-only xfail). No candidate is qualified.

The unchanged full `./ci.sh` source gate then passed against that clean pinned
checkout: generated-artifact/compile checks, lint, the full test suite and independent
coverage thresholds. Measured coverage was **91.19% line / 85.84% branch**, with the
existing critical boundaries meeting their 100% requirements. The Linux-only
`LD_PRELOAD` rename-observation case was skipped on macOS; the declared strict
research-only xfail remained. This is local source evidence, not protected Linux CI,
candidate qualification or an independent review. The initial sandboxed attempt
failed because local mock-server socket binding was denied; the passing rerun used
local-server permission without changing tests or thresholds.

The immediate runtime dependencies are concrete: crash-durable storage/serialization,
mandatory application admission, authenticated per-operation grant binding, truthful
one-attempt effect observations, and supervised cancellation. Existing HTTP envelopes,
route patterns and fresh isolated instances are already present at the pin.

## Follow-up: executable submission contract

The preceding implementation turn was progress: the exact-pin build and full local
source gate verified the first shared component. The next turn added the SIGIL
[operation-submission decoder](submission-codec.md), with 108 passing tests against
that same solver-enabled pin. It rejects ambiguous/duplicate keys, type coercion,
invalid Unicode and oversize requests, and emits canonical length-framed bytes.
No authority, identity issuance or durability is inferred from successful decoding.
The expanded 1,033-case `./ci.sh` source gate then passed on the same macOS arm64
environment: **91.19% line / 85.77% branch** coverage, all critical-boundary gates
passed, with the same one Linux-only skip and one declared strict research xfail.
This supersedes the 925-case source result for the expanded suite, not for candidate
or Linux qualification. Legacy synchronous chat is unchanged. No hosted CI or
release was run; the changes remain local and uncommitted.

The [usefulness fixtures](../acceptance/README.md) now contain 20 predefined draft
cases, including multi-file answers, factual absence, three same-conversation
follow-ups, a reopen case and a hostile-file instruction case. Fixture source paths,
case IDs/count and the CSV arithmetic were independently checked. No real or mock
model was run for this benchmark; model/profile selection, owner approval, fixture
digests, per-interface results and reviewer records remain required before M4 can
be qualified. The numerical 18/20-per-interface threshold is unchanged.

Permission has been requested to coordinate design-only feedback with the existing
SIGIL and control-plane tasks. No review request or cross-repository edit has been
dispatched without that permission. Runtime framing and primitive admission must
also be reviewed; the existing native HTTP envelope alone is not a qualified product
transport. This does not stop unaffected SIGIL application/component development.

Next: freeze authority/commit/delivery record codecs and conformance cases, reconcile
the generic primitive requirements with the owning runtime/control-plane work, and implement the first
SIGIL-owned durable turn. Preserve the Python route oracle during that work. Do not
block unaffected implementation on final model selection or external review, and do
not mark M0 complete while its contract/profile/review decisions remain open.

## Follow-up: SIGIL-owned conversation sequencing

The [turn reducer](turn-reducer.md) now chooses the model/tool/result/response
sequence, serial multi-tool execution, denied-tool results, completed-conversation
follow-ups, step/deadline/size stops and explicit unknown usage. Its strict internal
codecs carry byte-framed state and observations; they do not authenticate those
inputs or establish durable acceptance. The existing Python product remains the
reference implementation and its routes are unchanged.

The new execution fixture runs actual solver-verified SIGIL artifacts against a
local scripted provider and real temporary workspace. Provider work receives only
network/secret grants; file work receives only a workspace filesystem grant;
state interpretation and argument preparation receive no I/O grants. Parent,
absolute-path and symlink escape attempts are denied by the real filesystem
boundary. A provider observes one request before the worker is killed/reaped;
a fresh runtime interprets the saved proposal as possibly delivered without replay.
The saved proposal is a fixture snapshot, not crash-durable operation storage.

The initial combined run passed 208 component/integration/regression checks
(71 reducer, 13 execution, 16 build composition and 108 submission). Subsequent
checks tighten consumption of PS1 identifiers, invalid sequences and zero-deadline
handling, bringing those four suites to 226 cases (89 reducer checks).

The complete **1,151-case source gate passed** after those changes against the
same isolated solver-enabled public pin on macOS arm64/Python 3.14.6/Z3 4.16.0.
Coverage was **91.19% line / 85.77% branch**, with all critical-boundary requirements
passed. The one Linux-only rename-observation skip and one declared strict research
xfail remain. The initial full run's only failure was the README's stale test count;
that documentation was corrected, its guard passed independently, and the complete
unchanged `./ci.sh` gate was then rerun successfully. No test or threshold was relaxed.
This is local source evidence, not protected Linux CI, a real-provider run,
independent security review, durable operation qualification or candidate evidence.

The build recipe preserves authored-input, stdlib and final compiler-input hashes
while lexically removing line comments to fit the existing 64 KiB forge source
limit. Certificate verification, source limits and coverage gates were not weakened.
These are test/build inputs, not an admitted production artifact set.

Next: authenticated authority/attempt codecs and durable commitment/dispatch using
qualified native mechanisms, then real service integration. Still required:
compaction/clipping and route/tool parity, quota and scheduling policy, the browser,
cross-project conformance, real-model/usefulness evidence and candidate qualification.
External review is not blocking this component work. No cross-repository edits,
runtime-pin changes, real-provider spending, hosted release or pilot exposure occurred.

## Follow-up: native atomic storage and durable integration

The preceding turn was progress: executable SIGIL sequencing passed the complete
local source gate. This turn added a repo-local [native storage mechanism](native-store.md)
without modifying the SIGIL compiler/runtime pin or sibling repositories. It uses
opaque UTF-8 records, scoped read/read-write/create-only handles, atomic revision-
checked batches, integrity checks, explicit initialization and versioned tombstones.
It contains no pi/control-plane domain decisions. Rust/dependency versions are
pinned separately; the component is not yet included in a product release package.

Native checks cover all-or-nothing state/intent publication, stale preconditions,
scope isolation, identical keys in different namespaces, boot-bound handles,
duplicate owners, unsafe filesystem targets, corrupted/relocated records and
resource refusal. Real child processes are killed before commit and after commit
before reply. SQLite page-ceiling exhaustion is exercised, not a full-volume ENOSPC
or physical power-loss test. The 27 native test entries include the child-process
entrypoint used by the two crash tests; it is scaffolding, not an additional proof.

Twenty-one executable stdio checks and four durable SIGIL-turn cases were added.
The combined focused run, including the README-count guard, passed 26 cases. The
required whole-tree source gate builds the native component from its lockfile and
runs Rust formatting, Clippy and native tests through the session fixture. No
legacy test, certificate check or coverage threshold was removed or relaxed.

A concurrent test run found an owner-lock lifetime race: a child between fork and
exec could temporarily retain a copied descriptor after the parent closed its store,
causing an unexpected busy refusal on immediate reopen. Store destruction now closes
SQLite before explicitly releasing the lock in its owning process; inherited
non-owner handles cannot release it or use the store. Direct regression cases and
20 consecutive runs of the final 27-entry native suite passed. This is bounded
mechanism stress evidence, not the candidate's 30-minute product load qualification.
The final minimal-intent protocol/integration/count-guard rerun passed all 26 cases.

The final complete local source gate passed with 1,176 collected pytest cases,
91.19% line coverage and 85.84% branch coverage; every critical coverage gate
passed. There were no unexpected failures: one Linux-only rename-observation
case was skipped on macOS, and the strict research-only interprocedural-aliasing
case remained an expected failure. This run includes the final owner-lock/PID
guards and minimal-intent integration. It used the pinned SIGIL source on local
macOS arm64 with Python 3.14.6 and Z3 4.16.0, and is not a substitute for protected
Linux CI, immutable candidate qualification or independent review.

The durable sequence uses real isolated SIGIL provider/file artifacts and separately
scoped native application/executor connections. State and the next minimal effect
intent commit together; the intent carries only the effect's required payload,
not a copy of the application's state. Delivery records commit before interpretation.
Storage is killed/reopened between those boundaries. A lost final acknowledgement
leaves a retrievable final answer, and a stale interpretation cannot overwrite it.
The SIGIL delivery kernel distinguishes unclaimed work from a durable dispatch
claim. Both a claim with no known request and a request actually observed by the
provider recover as possibly delivered when the owning worker/observation is lost;
neither is silently repeated.

This is still not the M2 product path. The fixture directly supplies actor scopes,
attempt identities, transaction construction, observations and dispatch. The next
step is to move that shared protocol producer into SIGIL and bind it through
authenticated artifact/authority admission and native worker lifecycle enforcement.
Then connect the actual API; retain quota, scheduling, compaction/clipping, history,
export/delete, audit and operational parity requirements. M0 review, M6 cross-project
integration and all final qualification/sign-off requirements remain open.

## Follow-up: SIGIL-produced application commits

The preceding storage/integration turn was progress: the complete local source gate
passed with real atomic storage and process-kill evidence. This turn moved application
transaction construction out of the test driver into the [SIGIL transaction producer](turn-transactions.md).
It invokes the existing reducer, checks exact operation/sequence/kind/payload
correlation against a framed delivery observation, and emits native commit JSON.
The durable integration tests forward those bytes unchanged rather than selecting
application writes or delivery preconditions in Python.

Shared SIGIL storage encoding remains separate from pi-specific observation and
transaction policy. State plus the next minimal effect intent are one commit;
terminal interpretations emit no next intent. Store revision comparisons and
create-only intent writes retain their independent native enforcement. New tests
prove all-or-nothing refusal for stale delivery revisions and existing next intents,
exact large integer encoding, opaque Unicode/control-byte preservation, two disjoint
native scopes with identical semantic IDs, and retained follow-up history after
restart. An old reply transaction cannot overwrite that newer conversation state.

The 53 new transaction cases, 89 reducer regressions, 17 build/composition checks,
four durable effect cases and README-count guard passed together: **164 passed in
106.64 seconds**, no skips. The native fixture also rebuilt its locked executable
and ran formatting, Clippy and the 27-entry native suite. A control-character-heavy
input exposed the unchanged guest memory ceiling before output construction; the
test distinguishes that host refusal from explicit SIGIL size refusal and confirms
a subsequent fresh invocation succeeds. A bounded quoting preflight now refuses
another oversized transaction before costly output copies. No partial commit request
is exposed in either negative case, and no existing ceiling was raised.

The final complete `./ci.sh` source gate then passed with **1,230 collected cases**
and no unexpected failures. The existing Linux-only rename-observation case was
skipped on macOS, and the strict research-only interprocedural-aliasing xfail
remained. Every critical coverage requirement passed; measured coverage was
**91.19% line / 85.84% branch** over the gate's existing Python module scope, not a
measurement of SIGIL or Rust coverage. Those components have executable assertions,
not a newly claimed coverage percentage. This run rebuilt the solver-verifying
SIGIL binaries at the unchanged clean pin, retained every gate and used the same
local macOS arm64/Python 3.14.6/Z3 4.16.0 environment. It is not protected Linux CI,
candidate-bound qualification, a real-model run or independent security review.

The composed transaction source is 64,656 bytes, within the unchanged 65,536-byte
forge ceiling, with compiler-input SHA-256
`9ef149affc08c446b7c09052e457d620e8de7c92eafd8b1e4cba5f12619264b4`.
The fixed composer records all five authored input hashes plus the pinned stdlib
hash. This source identity is not authenticated artifact admission or release provenance.

Remaining immediately: authenticated immutable operation/authority/attempt records,
trusted snapshot/output binding, SIGIL executor claim/delivery transaction production,
and admitted native worker ownership/dispatch/cancellation. The test still supplies
those bindings and effects' observed facts; `SI1`/`SO1` are explicitly draft pi
correlation records, not the full shared contract. Production routing, policy parity,
deduplication/reservations, browser, real second consumer and all candidate/pilot
qualification remain open. No sibling repository, runtime pin, production endpoint,
external deployment or real-provider spending was changed by this turn.

## Follow-up: shared SIGIL executor commits

The preceding turn was progress: SIGIL-owned application commits and the complete
1,230-case local source gate passed. The next turn added the [shared executor
transaction producer](executor-transactions.md), using the existing delivery kernel
and shared pure record/storage helpers. The real durable model/file/response tests
now forward SIGIL's application, claim AND delivery commit bytes unchanged; their
driver no longer constructs any atomic batch. Python still supplies the fixture's
registry, snapshots, actor/generation facts, clock and physical effect dispatch.

The executor compares the intent's namespace/key/revision, retains the claiming
generation, requires matching live-generation facts, and atomically commits claim
or terminal dispatch/delivery records. Possibly-delivered records never produce a
new dispatch hint. Terminal events are no-ops, so late results cannot overwrite
uncertainty or cause another send. Stale revision and existing-delivery conflicts
roll back every write in the native transaction. Neither a generation string nor
`dispatch_after_commit` authenticates an actor or fences its worker; the native
admission/lifecycle mechanism remains required before actual production dispatch.

The generic `SD1`/`DR1` records replace fixture-only phase digits/unframed facts.
Pi interprets those shared facts in SIGIL: an observed failure stops a model call
or becomes a correlated tool error according to the saved pi phase. The `PX2`
application envelope consumes `DR1` directly; the earlier draft `PX1`/`SO1` pair
is rejected rather than silently reinterpreted. No released product state or
runtime pin was migrated. Shared code contains no pi/control-plane domain branch.

The focused run passed **297 cases in 132.91 seconds**, no skips: 77 executor,
56 application-transaction, 89 reducer, 53 kernel, 18 composition and four real
durable-effect cases. Its native fixture also rebuilt the locked store and passed
formatting, Clippy and the existing 27-entry native suite. Coverage includes all
42 kernel phase/event combinations, generation/record correlation, native scope
refusal, exact large revisions, restart, immutable late delivery and atomic rollback.
These are component/native-scope tests, not authenticated service or M6 evidence.

Compiler-input identities at the unchanged pin:

| Component | Bytes / authored inputs | SHA-256 |
|---|---|---|
| Pi application transaction | 65,293 / 6 | `2636f32d87ade18a8dbfa1fe49165663c0b0b223b414f36517b7cbd0d1d666b9` |
| Shared executor transaction | 42,679 / 5 | `2967da0d04d0a86d44ff37d2c9705df0cad79a9acd376d7bfa017ec14b5b6faf` |

Both fit the existing 65,536-byte compiler-input ceiling; no verification override
or limit increase was introduced. Source hashes are not authenticated admission.

The final full `./ci.sh` gate passed with **1,311 collected cases**, no unexpected
failures, the existing one Linux-only rename-observation skip and one strict
research-only interprocedural-aliasing xfail. All critical coverage requirements
passed: **91.19% line / 85.84% branch** over the gate's existing Python scope, not
SIGIL/Rust coverage percentages. This run rebuilt the solver-verifying binaries
from the unchanged clean SIGIL pin and retained every legacy gate on local macOS
arm64/Python 3.14.6/Z3 4.16.0. It does not establish protected Linux CI, authenticated
product behavior, a packaged candidate, real-model usefulness or independent review.

Next: bind this durable path to authenticated immutable operation/authority/attempt
records and real native snapshot/output routing, artifact admission, one-use dispatch
and worker fencing/cancellation. Then connect the actual SIGIL API with the required
quota/deduplication/audit and compatibility policy. Existing Python product routes
remain unchanged. M0 review/profile decisions, M6's real consumer, browser/usefulness,
candidate qualification and independent pilot clearance remain outstanding.

## Follow-up: native one-use worker execution

The preceding shared-executor turn was progress: SIGIL produced all application,
claim and delivery commits and the complete 1,311-case local source gate passed.
This turn added a repo-local [native worker bridge](native-worker.md), without
changing the SIGIL pin or sibling repositories. It freezes source/input/grants,
issues one-use in-memory tickets, launches fresh runtime processes, bounds their
pipe transport, and reports generation-correlated execution/cleanup facts. It has
no product authorization, conversation, schedule or retry policy.

The durable model/file/response fixture now uses this bridge for actual effects,
and its native generation appears in SIGIL-produced claim and delivery records.
The claim commits before the fixture consumes the execution ticket. Storage still
restarts between intent, delivery and interpretation commits. Reusing the ticket
is refused. A provider observes exactly one request before a local timeout; recovery
retains possible delivery and does not replay. A subsequent preparation can run in
a fresh healthy worker. Stopped local execution is never treated as remote rollback.

The focused executable run passed **19 cases in 44.82 seconds**: 15 new native-worker
cases and four migrated durable-turn cases. After adding an oversized-stdout check,
the final Rust mechanism suite passed **13 tests** with formatting and Clippy clean.
Those native checks include replay/wrong-ticket handling, cancellation before and
during execution, partial frames, non-reading request pipes, stdout/stderr limits,
protocol confusion, changed runtime identity and the cleared child environment.
The real SIGIL checks cover fixed input/source, provider-secret injection and
recovery after a deadline. Initial harness timing assumptions were corrected using
request-received markers; the real deadline test uses 1.5 seconds, below the pinned
runtime's own two-second HTTP limit, and asserts the provider actually received it.

The final complete local `./ci.sh` source gate then passed with **1,326 collected
cases**, no unexpected failures, the existing one Linux-only rename-observation
skip and one strict research-only interprocedural-aliasing xfail. All critical
coverage requirements passed: **91.19% line / 85.77% branch**, measured over the
existing Python scope, not Rust/SIGIL coverage. This gate includes the final
13-test native worker suite, native storage suite and all legacy source checks.
It rebuilt the solver-verifying binaries from the unchanged clean SIGIL pin on
macOS arm64/Python 3.14.6/Z3 4.16.0. It is not protected Linux CI, an immutable
product candidate, real-model usefulness evidence or independent review.

This remains component/integration evidence, not M2 or M3 qualification. Hash-then-
path execution assumes trusted local package paths, not race-free authenticated
artifact admission. The bridge does not validate a durable claim receipt or bind
an authenticated operation/authority context; a new controller could prepare the
same semantic operation without the missing durable admission/fencing layer.
`worker_reaped` confirms the direct child only. The Linux parent-death hook has not
been exercised by this macOS run, and macOS has no equivalent controller-death
mechanism here. In-flight cancellation is exposed by the Rust API, not yet by the
serial stdio adapter. These limitations are explicit in the component contract.

Next: admitted immutable operation/authority/attempt bindings, actual snapshot and
output routing, claim-receipt enforcement and restart-safe worker ownership. Then
connect the actual SIGIL API and preserve quota, scheduling, audit and route parity.
M0's review/profile choices, the browser, M6's real second consumer, qualified Linux
packaging and M8 pilot clearance remain open. Independent review does not block
unaffected local development. All M0–M8 rows remain unqualified.

## Follow-up: real native HTTP with SIGIL-owned admission

The preceding worker turn was progress: the complete 1,326-case local source gate
passed, including one-use native effect execution in the durable fixture. This turn
added a [native application host and SIGIL API handler](native-service.md). Actual
HTTP operation submission and acceptance lookup now run without a Python product
dispatcher. Native code supplies bounded transport, digest matching, scoped storage
and continuation mechanics; SIGIL owns bootstrap policy checks, credential validity,
route permission, canonical submission, replay/conflict policy, record construction
and response semantics. The existing Python product API remains unchanged.

This closes fixture-supplied request/context/snapshot routing for the new admission
path. HTTP clients cannot supply the matched facts, store handle, generated identity,
stage, observation or continuation. SIGIL validates the exact configured scope list
before state is opened. One atomic native commit publishes the queued-operation and
deduplication entries, including exact canonical payload and original authority facts.
Repeated matching submissions, including concurrent calls and process restart,
return the retained identity; payload collisions return 409. Other tenants and other
same-tenant principals cannot retrieve that operation. Replays do not rewrite or
widen the retained original authority. Credential time and scope decisions run in SIGIL.

The first executable run exposed an initialization-contract mismatch: the native
store correctly requires an existing private root. The application host now creates
exactly one fresh 0700 directory only for explicit `init`, syncs its parent, then
lets the store independently validate it. Existing/missing/corrupt state is not
silently repaired or reinitialized. No permission check was weakened.

The initial focused suite passed **38 cases in 271.46 seconds** through the actual
native HTTP service, real solver-verified SIGIL and native storage. Three additional
policy cases then checked equivalent credential rotation, retained tombstones/failed
commits and deadline clamping to expiry. Those and the final composition suite passed
together: **22 cases in 2.28 seconds**. The final native host's three helper tests,
formatting and Clippy passed. The expanded full suite collects **1,368 cases**, and
the README collection guard passed. Final whole-tree source-gate evidence is recorded
below; these focused results do not substitute for it.

The first whole-tree run failed at the existing native-worker session fixture:
its protocol-error test reported a deadline instead of the intended protocol refusal,
propagating 19 setup errors to dependent tests. An independent
repeat also observed a partial-response fixture expiring before any forge bytes were
sent. The deliberately flooding/short-deadline process fixtures now run serially;
the explicit concurrent-cancellation test remains concurrent. No production code,
deadline, assertion or coverage threshold was relaxed. Five consecutive runs of
all 13 native worker tests then passed, with formatting/Clippy clean. The failed run
does not qualify the expanded suite.

The final complete `./ci.sh` rerun passed with **1,368 collected cases**, no unexpected
failures, the existing one Linux-only rename-observation skip and one strict research-
only interprocedural-aliasing xfail. It includes all 41 new API cases, the actual
durable-turn/worker integrations, the final native host/worker/store suites and all
legacy checks. Every critical coverage requirement passed: **91.19% line / 85.84%
branch** over the existing Python module scope, not a Rust/SIGIL coverage percentage.
This was the unchanged solver-verifying SIGIL pin on local macOS arm64/Python 3.14.6/
Z3 4.16.0. It is not protected Linux CI, complete product parity, an immutable
candidate, independent review or live-model qualification.

The final API compiler input is **58,868 bytes**, within the unchanged 65,536-byte
ceiling, with five authored inputs and SHA-256
`4441020242e4e8f11f08521a1cbf81b2a10804ab7c566eac7196875645281fea`.
Composition reuses the existing submission decoder rather than creating a second
JSON/request policy. The SIGIL runtime pin and verification gate remain unchanged.
The new Rust host/dependencies are locally locked, not yet packaged or qualified.

This is queued-operation admission, **not authenticated end-to-end turn execution**.
The records are not yet connected to the existing reducer/executor path, reservations,
conversation state or initial-intent publication. Other route handlers return 501
in the new prototype, while the legacy service is untouched. The host binds only
loopback and has no external-bind option. No browser, live-model benchmark, tenant
quota/retention/audit parity, admitted effect lifecycle, real control-plane consumer,
Linux candidate or independent pilot clearance is established. All M0–M8 gates
remain unqualified; M3 now has partial new-boundary evidence rather than only legacy
tests. No sibling repository, runtime pin, real-provider spending or pilot exposure
was changed by this turn.

Next: join the authenticated immutable operation context to SIGIL reservation/state/
intent production in one recoverable decision, then enforce a durable claim receipt
and restart-safe scoped effect dispatch. Finish the complete API/browser and operator
policy surface against the unchanged goal, not a smaller queue-only product.

## Version-2 native action-time boundary (2026-09-08)

Before expanding admission into reservations/state/intents, the pre-worker-only
time check needed a native action boundary. SIGIL now chooses a `TG1` window in
each `HC2` command. Authenticated reads/replies use credential validity; admission
commits use the tighter operation/credential deadline. Bootstrap approval also
has a window based only on credentials currently active in the supplied snapshot.
The native host checks fresh wall time and its own monotonic ceiling after worker
execution/command decoding and immediately before initiating the action. It does
not parse credential facts, choose tenant allowances or interpret operation state.

The host configuration, startup identifier and request/command protocol are version
2; old `AH1`, `HC1`, missing I/O/bootstrap guards and configuration version 1 are
refused. This is an explicit draft internal-contract change, not a silent change
to the legacy Python API. Existing `CF1`, `DQ1` and `OQ1` stored record bytes are
unchanged; no data was erased or migrated. Development state can be opened with
the matching new source/configuration, but an upgrade/rollback is not yet qualified.

The native host now has **15 Rust tests**: the existing three framing/digest checks
and twelve action-boundary tests. They use real scoped storage plus deterministic
clock observations to cover exact bounds, invalid encoding, absent/widened scopes,
atomic compare-and-set results, rollback, monotonic overruns, protected read/reply
refusal, and a committed decision remaining retrievable after acknowledgement expiry
and store reopening. Formatting and Clippy pass without relaxed checks.

The focused API/composition run passed **79 cases in 338.01 seconds**: all 60 API
cases and 19 composition checks. New cases include real HTTP/native execution of
owner-installed, solver-verified conformance probes that return expired, future,
missing, malformed or old-version commands. A valid guarded command actually writes;
refused commands do not. This probe is not a caller-selectable product application.
Startup rejects an invalid/expired guard before creating the state directory. SIGIL
policy checks also verify the tighter turn guard, guarded authenticated replies,
old-envelope refusal, and the distinction between active and future credentials.

One preliminary invocation stopped because loopback listening was sandbox-restricted;
the real HTTP suite was rerun with the required local-network permission. The first
probe revision was rejected by the compiler for passing public helper arguments
inside internal control flow. The test probe now computes that helper result before
branching; no taint override, declassification or production policy relaxation was
introduced. Neither preliminary run is qualifying evidence.

The compiler input is **60,421 bytes**, under the unchanged 65,536-byte ceiling,
with five authored inputs and SHA-256
`eee4ca0cf44130d1b35bd373652843e4428068db3c549f5ea066238278ccda11`.
The SIGIL/toolchain pin, solver verification and coverage gates remain unchanged.
The README collection guard passes for **1,387 collected cases**. The final complete
`./ci.sh` run passed on that expanded suite, including all 60 API cases, the version-2
startup-identifier assertion, the native host/worker/store tests and all legacy
checks. There were no unexpected failures: the existing Linux-only rename-observation
skip and strict research-only interprocedural-aliasing xfail remain. All critical
coverage requirements passed, with **91.19% line / 85.84% branch** coverage over the
existing Python module scope, not Rust or SIGIL coverage. This was local macOS arm64,
Python 3.14.6 and Z3 4.16.0 at the unchanged solver-verifying SIGIL pin. It is not
protected Linux CI, live-model evidence, an immutable candidate or independent review;
the preceding 1,368-case checkpoint is retained as historical evidence only.

The [time-bound initiation contract](native-service.md#time-bound-action-initiation)
does not promise that a started filesystem operation or HTTP response finishes
before expiry. It does not roll back committed state, fence effect dispatch,
persist a trusted clock across restart or implement live revocation. A later
503/timeout is still unknown acceptance, not evidence that nothing committed.
Queued-operation execution, atomic reservation/state/intent admission and all
M0–M8 qualifications remain open. No external provider, deployment, sibling
repository, runtime pin or independent-review scope was changed.

## Authenticated six-record admission (2026-09-08)

The real API now connects authenticated admission to reservations, conversation
state and the first model intent. SIGIL checks deduplication first, reads actual
tenant state/counter snapshots, calls a fixed grantless admission component and
forwards its exact six-record native transaction. It returns acceptance only after
the actual acknowledgement. The batch contains `DQ2`, `OQ2`, `PT1`, `SI1`, `BH1` and
`BR1`; no Python or native code constructs the domain records.

The admission component reuses the exact extracted `start_turn` implementation
from the existing reducer and the API's exact operation/key codec. Existing reducer
and transaction tests remain. Each store record increments its own revision; the
global receipt revision is separate. Atomicity is established by one native batch
and failure/restart tests, not by assuming all record revisions are transaction IDs.

Native host version 3 supports bounded, fixed grantless function calls. The entry
application retains the original request, matched facts, actual observations,
continuation, configured bundle identity and function inventory. Callee output is
data, not an executable host command. Bootstrap uses the same mechanism with no
storage handle and validates the nested model/reservation profiles before state
opening. All entry/function network, filesystem and secret grants are refused.
The existing eight-step and independent time ceilings are retained. The bundle
fingerprint identifies configured source/runtime hashes and ceilings; it is not
complete signed artifact admission or native-host executable provenance.

Real API checks cover durable tenant-scoped reservation capacity, replay without
another reservation, same-tenant conversation contention, policy consistency,
refused function code/grants/aliases and function output that resembles a native
command but cannot execute one. A native five-record ceiling rejects the six-record
public admission and rolls back its attempted writes. Tests also reject stale
preconditions on each coordinate and a denied final namespace, and reopen committed
state after a killed store process. The raw bearer header is not copied into guest
envelopes or retained records, and model tool catalogs are filtered by SIGIL.

An initial **185-case** admission/reducer/transaction run passed in **124.38 seconds**.
The real HTTP admission/replay/restart smoke passed in **40.08 seconds**. The focused
capacity/concurrency/function-boundary run passed **18 cases in 134.86 seconds**;
the admission/composition and final capacity/bootstrap checks passed **66 cases in
49.17 seconds**. The native service now has **17 Rust tests**; formatting and Clippy
are enforced by its fixture. The expanded suite contains **1,455 cases**, including
44 admission, 83 API and 20 composition cases, and the README count guard passes.
The final complete `./ci.sh` source/coverage gate passed on all **1,455 collected
cases**, with no unexpected failures. The existing Linux-only rename-observation
skip and strict research-only interprocedural-aliasing xfail remain. All critical
coverage requirements passed, with **91.19% line / 85.77% branch** coverage over the
existing Python module scope, not Rust or SIGIL coverage. This was local macOS arm64,
Python 3.14.6 and Z3 4.16.0 at the unchanged solver-verifying SIGIL pin. It is not
protected Linux CI, live-model evidence, an immutable candidate or independent review.
Earlier source-gate results remain historical evidence only.

Current compiler inputs remain below the unchanged 65,536-byte ceiling:

| Component | Bytes / authored inputs | SHA-256 |
|---|---|---|
| API | 63,044 / 5 | `6aeadd558fb03fb49ec97623fd2682cc27dcfccd4d19632808699327147f683d` |
| Admission | 61,196 / 7 | `f80d6f16ee74fee7d9833792781498b5a4ae6bd2e3a6a4278ddedb8ee7283e10` |
| Turn | 57,978 / 4 | `37768210752c29f07ef22933360e8f95a6a82a37dc54ef626cb4751793d0a85c` |
| Turn transaction | 65,454 / 6 | `cbf8344912b254c5b9966b8f6a6db48be58422c29758b3ce734404c66c9da078` |

This is **reservation capacity and durable admission, not executed assistant work**.
There is no admitted model/tool dispatcher, durable claim receipt/fencing, result
integration, settlement, charged-usage/rate-window parity or reservation-expiry
worker yet. Existing queued records and old host protocols are refused, not silently
converted into admitted work or erased. Migration and principal/conversation-sharing
policy remain explicit M0 decisions. The legacy service is unchanged.

The [admission contract](admission.md) records these semantics and limitations.
Next is the admitted effect lifecycle through the same durable intent/result
mechanisms, with accounting and truthful recovery. Browser/API completion, the actual
control-plane consumer, Linux/candidate qualification and independent pilot clearance
remain required. No M0–M8 row is PASS, and no provider spending, deployment, sibling
repository change or independent review was started by this work.

## Receipt-bound native worker initiation (2026-09-08)

The preceding goal turn was progress: it recorded the successful six-record
admission source gate. This follow-up closes a concrete execution gap: the durable
fixture previously ordered claim commit before worker execution, but the bare
worker bridge did not enforce receipt ownership.

The new [native claim gate](claimed-worker.md) exclusively holds the actual store,
bootstrap scope, fixed bridge and clock state for one attempt. It reads the actual
intent and requires an unused claim coordinate. SIGIL supplies the claim transaction;
the gate accepts only its exact intent read check and one create-at-zero `SD1` write,
bound to the actual intent/revision, native prepared generation and claim phase.
It arms execution only after its own successful durable commit. A supplied receipt
or `dispatch_after_commit` hint cannot arm it. Execution rechecks records/time and
consumes the fixed worker once. Retained claims and tombstones prevent preparation
at that coordinate after reopening; dropping an attempt retires its pending ticket.
No application authorization, conversation choice, quota or recovery transition was
implemented in native code.

The trusted `sigil-claimed-worker/v1` stdio embedding uses the same library as the
HTTP host. It is not a public API, a credential-authorized effect dispatcher or a
new guest capability. Entry/function grants in the HTTP service remain empty.
The HTTP host version, stored records, compiler ceilings, verification requirement
and SIGIL runtime pin are unchanged. The common native commit decoder was extracted
for reuse; it retains strict JSON and bounded request handling.

All existing durable-turn fixtures now use the gate for actual model/file effects
and forward SIGIL's claim/delivery bytes unchanged. A new starting path accepts a
real authenticated HTTP request before the fixture continues the same stored intent
through model -> file -> model -> retained response across process restarts. The
fixture still selects artifacts/grants and routes observations; it explicitly verifies
that the API operation remains `accepted` and the reservation remains `reserved`.
That evidence does not substitute for automatic product dispatch and settlement.
The time-unit table for `PE1` was corrected from milliseconds to Unix seconds,
matching the existing `AH3`/`AP1` admission path; no stored timestamps were converted.

The focused integration run passed **25 cases in 80.28 seconds**: 20 new native
claim-boundary cases and five durable-turn cases. The native service fixture enforces
formatting, Clippy and **29 Rust tests**, including receipt ownership, wrong generation,
expanded/missing preconditions, storage-scope refusal, changed records, cancellation,
fresh time checks, clock regression, retirement and retained/tombstoned-slot reopening.
The initial test-only lint failure was corrected without relaxing the lint gate.
The first HTTP-started fixture attempted to read an intent through a create-only
application scope and was correctly denied; its inspection now uses a separate
explicit read scope, without widening the application grant. These preliminary
failed runs are not qualifying evidence.

The final complete `./ci.sh` source gate passed on **1,476 collected cases**, including
the HTTP fixture's retained credential/operation time window and explicit duplicate-JSON
refusals at the trusted adapter. There were no unexpected failures: the existing
Linux-only rename-observation skip and strict research-only interprocedural-aliasing
xfail remain. All critical coverage requirements passed, with **91.19% line / 85.84%
branch** coverage over the existing Python module scope, not Rust or SIGIL coverage.
This was local macOS arm64, Python 3.14.6 and Z3 4.16.0 at the unchanged solver-verifying
SIGIL pin. It is not protected Linux CI, a real-provider qualification, an immutable
candidate or independent security review. Earlier source-gate results remain historical.
Current new gate source SHA-256:
`3e439dbf172151284e6ea9f0ac007777da4a0db4ce63db25f2d00d8c77d9e770`;
trusted embedding SHA-256:
`7654c29bdbb31463e724039ec2ae95b5be7f54350fb0094de7441bb10d886640`.
These identify local source files, not signed executable provenance or a candidate.

Next is SIGIL-owned admitted dispatch and actual result routing/settlement through
this mechanism. Create-once fencing at a retained coordinate is not universal
exactly-once delivery, old-worker death, safe rollback of old backups or full immutable
operation/authority/artifact/input/grant admission. Linux owner-death/cancellation,
all route/browser behavior, actual control-plane reuse, candidate qualification,
external review and every M0–M8 gate remain open. No external provider spending,
deployment, sibling-repository edit, task message or review request was initiated.

## Native-bound SIGIL dispatch policy (2026-09-08)

The preceding claim-gate milestone passed its full local source gate. This follow-up
adds [SIGIL dispatch policy](dispatch-policy.md) over the original/current authority,
operation, reservation, conversation, intent, held counter and exact effect binding.
It reuses the existing authority/state codecs, admission tool filtering, model-body
builder and file argument adapter. This is application policy in SIGIL, not a native
model/tool or tenant-policy implementation.

The new native binder reads the actual scoped records, supplies its clock and the
held worker's non-secret hash/grant facts, and invokes a fixed grantless policy.
It retains exclusive store ownership through preparation, claim and execution.
An accepted shared `DW1` envelope must name the fixed worker and an actual read
intent/revision. Pi's `DX1` correlation is opaque to the native host. The version-2
trusted stdio path requires this policy and refuses direct preparation; version 1
remains the explicitly separate claim-gate conformance surface. No public API or
stored-record version was changed. The HTTP application's bundle still covers its
entry/admission configuration, not this separately pinned dispatch/effect/host set.

The initial policy/admission/composition run passed 167 cases. A later 142-case run
passed the complete 106-case pure policy suite and the first 36 native integration
cases. The final focused integration run passed **72 cases in 277.93 seconds**:
47 native dispatch cases, 20 claim-gate regressions and five earlier durable-turn
cases. The new cases include a full model -> file -> response path starting from
real authenticated HTTP admission, with native-bound policy checking every effect
and durable result/interpretation boundaries exercised across process restarts.
They also include operator-installed verified policy probes that challenge the
native envelope independently of pi's implementation.

Adversarial checks refuse caller-supplied facts/snapshots/clock/worker/guard overrides,
direct preparation, mismatched lookups, changed authority/records, widened grants,
wrong hashes/endpoints/tenants, effectful policy configuration, non-read policy
scopes and stale/unguarded/foreign worker envelopes. Metadata projection tests
exclude secret values and environment-variable names, and refuse dead/poisoned
bridge ownership. Native worker tests increased to 14; the existing service suite
remains 29. Formatting and Clippy with warnings denied passed for the changed native
code, and the Python lint gate passed.

During development the compiler refused the reserved local identifier `effect`;
it was renamed without changing the compiler. Inspection also found that model
body reconstruction needed the **filtered admitted** catalog rather than the
operator's full catalog. That was corrected and four positive cases now cover
empty, non-catalog, wildcard and explicitly allowed tool sets. No permission,
certificate, source-size, test-count or coverage threshold was relaxed.

The complete **1,630-case `./ci.sh` source gate passed**, including the unchanged
toolchain-pin/rebuild, generated-artifact/compile, lint and independent coverage
requirements. There were no unexpected failures: the one Linux-only rename-observation
skip and one strict research-only interprocedural-aliasing xfail remain. Coverage
was **91.19% line / 85.84% branch** over the existing Python module scope, not Rust
or SIGIL coverage. This was local macOS arm64/Python 3.14.6/Z3 4.16.0, not protected
Linux CI, a real-provider run, an immutable candidate or independent review. The
prior 1,476-case result is historical. Current composed inputs at the unchanged
solver-verifying public pin:

| Component | Bytes | Compiler-input SHA-256 |
|---|---:|---|
| API | 63,068 | `4a708003442c24cd6f01f0deaa9c51c44c0fbfb9aea44187fbab6a75b0255a76` |
| Admission | 61,435 | `96199f8358a9e764222ef087b3adbdd6b5b706f126fad7f6f955edb587f31761` |
| Dispatch | 65,365 | `eb7c5c0907818529c1ce5acfa7f1b137c02fb8e8463e6dec9345f3c8c9f412ae` |
| Turn | 57,994 | `bb545388304b0a05d5fd24c212e06d54439ce7bf0c82c2af069c5ce27fae041f` |
| Turn transaction | 65,474 | `be064ce2bde749c3b16244555b02ba135103b57a1922bd64de460a624dd0775a` |
| File request | 43,154 | `16cf4b7a088023e517249c698b94fddda648a6c80bc3111fc51914007dfcad24` |

Native binder source SHA-256:
`eb1e7fc7431b29529ddb8b484df662ccdf196c37c07c24d9a6e282d4a41bbb11`;
trusted embedding:
`22ac15be603b7cafe48b6823f8605cd14f19bdf75f25322cf7639667b670a00d`;
worker bridge:
`4d1f49ad302000c182a69bcbf745ac0d0ada6e793b9d7e010f055260dfeab5d4`.
These identify local source, not signed executable provenance or a release candidate.

The tests deliberately retain the unresolved product boundary: a trusted fixture
still selects operator-installed bindings, records results and invokes interpretation.
The HTTP operation remains `accepted`, and its reservation remains `reserved` after
the retained conversation reaches `done`. Reported-usage checks do not establish
prospective input metering, billing or settlement. Current authority is static
bootstrap/restart policy, not live revocation. The two-second pinned HTTP effect
timeout and Linux-specific worker/restore qualification remain outstanding.

Next: actual result provenance/routing and SIGIL terminal operation/reservation
settlement, followed by an automatic admitted service dispatcher with SIGIL-owned
scheduling/recovery. All nine goal gates remain open. No source evidence here
constitutes real-model usefulness, protected Linux CI, actual control-plane reuse,
candidate admission, independent review or pilot clearance. No sibling edits,
runtime-pin change, real-provider spending, deployment or task messages occurred.

## Native-bound SIGIL completion and abandoned-claim recovery — 2026-09-08 UTC

This goal turn is **progress**, not completion. [Completion/recovery](worker-completion.md)
now uses native-owned observations and actual retained records, not controller-authored
events/outcomes. A fixed grantless SIGIL component constructs EX1 and reuses the exact
shared executor and delivery kernel. The native recorder owns claim and result commits,
checks exact intent/claim/delivery coordinates and generations, and returns a durable
phase only with a confirmed terminal receipt. No pi business rule moved into Rust.

The trusted stdio embedding's new version 3 requires both the dispatch policy and
completion worker. It permits only read-only domain/intent access, the fixed claim's
read/write scope and the fixed delivery's create-only scope. It refuses manual
claim/execute, arbitrary commits and caller-supplied results. Versions 1/2 remain
explicit manual conformance paths; the HTTP service version and stored SD1/DR1 schemas
are unchanged. The native-only metadata probe checks actual create authority and
never-used delivery slots without exposing their contents or widening general reads.

SIGIL classifies complete returned output as an observation, confirmed no-send as
definitely unsent, and runtime errors/incomplete output/unconfirmed cleanup as possibly
delivered. A new native-bound recovery path reads a retained live claim when no local
Attempt is held, supplies conservative abandonment facts, and commits uncertainty
without preparing an effect. It preserves the original worker identity, never asserts
that an orphan or remote effect stopped, and cannot overwrite a terminal delivery.
Result persistence has a separately bounded interval; expired dispatch authority does
not erase an already observed result or authorize another send.

Focused evidence:

- **92 completion/recovery tests passed**: 51 pure SIGIL cases and 41 real native
  integration cases. These cover canonical/contradictory facts, exact output bytes,
  forbidden request overrides, scope/coordinate/generation refusals, delivery values
  and tombstones, real failed provider observations, and native-bound recovery.
- The three malformed installed-recorder probes were then extended to challenge
  recovery itself and rerun: **3 passed**. Wrong generation, wrong namespace and
  malformed output cannot produce a recovery acknowledgement or mutate the retained
  claim. Reopening with the correct fixed producer records uncertainty, not a replay.
- The controller was killed after a local provider observed its request but before
  result commitment. Reopening recorded phase 4 with the original generation and no
  resend; the actual pi SIGIL transaction consumed that delivery and stopped uncertain.
- The controller was killed after terminal commitment but before the client read its
  acknowledgement. Reopening retained the exact phase-2 response, without replacement
  by recovery or repeated execution. Neither experiment establishes physical power-loss
  durability, universal exactly-once delivery or orphan-process termination.
- Earlier in this turn, **71 integration tests passed** covering the original 22
  recording cases plus all 49 dispatch cases, including authenticated HTTP admission
  followed by a complete model/file/model turn with native-owned result recording.
  The 77 shared-executor regressions also passed alongside the initial 39 completion
  cases. These runs precede the final abandoned-recovery extension.
- Native store **29**, service **35**, and prior unchanged worker **14** unit tests
  pass. Store tests cover the metadata-only probe's authority, tombstones, nonmutation,
  foreign ownership and corruption. Service tests include actual cancelled execution,
  output projection and result persistence after initiation authority expires. Controlled
  fake-native protocol responders in those unit tests are not compiler evidence.

Development corrections did not relax product gates: an `@Internal` helper boundary
was made explicit after a compiler taint refusal; a provider-error test stopped using
a tiny deadline that could expire during claim compilation before any request was
sent. The revised test requires actual provider arrival and the pinned runtime's real
error observation. The runtime's two-second HTTP timeout and original execution
deadlines were not changed.

Final source qualification: **the complete 1,725-case `./ci.sh` source gate passed**,
observed terminal success by 2026-09-08 09:07 UTC. This includes the unchanged pin/rebuild,
generated-artifact/compile, lint and independent coverage gates, plus all new and existing
tests. There were no unexpected failures: one Linux-only rename-observation skip and one
strict research-only interprocedural-aliasing xfail remain. Coverage is **91.19% line /
85.84% branch** over the existing Python module scope, not Rust or SIGIL coverage.
Formatting, Clippy with warnings denied and the Python F/E9 lint gate also passed.
The prior 1,630-case result is historical. This is local macOS arm64/Python 3.14.6/Z3
4.16.0 source evidence, not protected Linux CI, a candidate, a live-model run or
independent review. No source was changed during the qualifying run; the result and
storage-checksum documentation were updated afterward.

Current compiler inputs at the unchanged solver-verifying pin
`8277a1d92d599df89e6b4391fc70fd0fa534d696`:

| Component | Bytes | Compiler-input SHA-256 |
|---|---:|---|
| Shared executor | 42,837 | `f6bfc58cde3c01163021dd1371a6a184f0bd24b197c4a7efc31d5721238bbbe2` |
| Worker completion | 46,484 | `92e4e3aca9581e5e486e40a35ef3f1dfadfb5e94822ffbb93afaaf12ed093f7b` |

API, admission, dispatch, turn, turn-transaction and file-request compiler inputs match
the preceding table. The 64 KiB source cap is unchanged; turn-transaction still has only
62 bytes of headroom. Source SHA-256 identities for the changed native mechanisms:

- Claim gate: `586e14c19bc4e1c0957548015ee0dd33a3236ca6117a4af2178a6a7c763b12cd`.
- Recorder: `c0e3e40da9e0fbbd6a35ca4a05dcadf4fb183e0d24081918d64519006b7e9027`.
- Trusted embedding: `d698ad99d2ad0480a35056497e8fb8dc0843f2cd655b8c67156c15915935a349`.
- Store: `5f7ce13f32109b49574eec9fda666c0acf51538e443ae46f3efe7e8f8ff643db`.

These identify local source, not an admitted immutable executable. The trusted fixture
still selects fixed bindings and invokes application interpretation. Actual HTTP OQ2
remains `accepted` and BR1 remains `reserved` even after a fixture-completed conversation;
tests continue to assert this unresolved boundary. Next: SIGIL terminal operation and
reservation settlement, then automatic service dispatch/interpretation/recovery with
current authority. Full route/tool parity, quotas/audit/retention, browser usefulness,
real control-plane reuse, Linux fault/load/restore qualification and pilot clearance
remain mandatory. All nine gates remain open. No sibling edits, pin change, real-provider
spending, deployment or task messages occurred.

## Terminal operation publication and native-bound settlement — 2026-09-08

This is further local implementation progress, not a gate promotion. The preceding
1,725-case full source result is now historical after application/native source changes.
This milestone collected **1,837 cases**. Its full unchanged `./ci.sh` source gate
passed, with terminal success observed at **2026-09-08 10:35 UTC**, before the later
discovery integration below. There were no unexpected failures: one Linux-only
rename-observation skip and one strict research-only xfail remain. Coverage passed
at **91.19% line / 85.84% branch** over the existing Python module scope, not Rust or
SIGIL coverage. This is local macOS arm64 source evidence, not a candidate or protected
supported-topology CI.

The new [settlement contract](settlement.md) has one SIGIL-owned decision over actual
native reads and time. It atomically publishes `OQ3`/typed `OR1`, updates `BR1` to `BR2`
and settles `BH1` capacity with an exact terminal-state read precondition. All three
writes commit or none do. Native code treats product context as opaque and independently
requires every proposed coordinate/revision to match an actual read; every read remains
a precondition. The trusted stdio caller cannot provide snapshots, outcomes, authority,
clocks, writes or receipts. The installed pure function has no effect or secret grants.

Known in-reservation usage releases outstanding capacity, not provider charges. Unknown
or overrun usage releases only the active-turn slot and retains the full original token
reservation. Repeated settlement checks the retained operation/reservation pair and emits
no commit or fresh receipt. It remains a no-op after a later conversation turn, preventing
double release. This is not prospective metering, cumulative billing or a spending guarantee.

The HTTP API now renders a strict terminal result after owner/tenant checks. `AP2` admission
requires the matching revision-2 terminal operation before a `done` conversation can be
replaced, checking both previous conversation and operation revisions. Terminal results
remain independently readable after follow-up admission. Failed/cancelled/uncertain
conversations still require explicit recovery policy; no implicit fresh turn is allowed.

Completed local evidence so far:

- Native formatting and Clippy with warnings denied passed; **37 Rust service tests**
  passed (including two new generic transaction-validator tests). Existing independent
  native store/worker suites retain their own gates. No lint or runtime bound was relaxed.
- An initial **114 focused cases passed** for settlement, native integration, admission
  and composition. A later ledger-cap consistency check superseded that exact source
  snapshot; it was included in the following completed run.
- **87 extended cases passed in 361.89 seconds**: 55 contract cases, 28 actual native
  settlement cases and all four durable-dispatch admission/recording modes. The recorded
  modes execute controlled model/file/model effects, actual claim/result commits, SIGIL
  state interpretation, native-bound terminal settlement and real HTTP result lookup.
  The run covers stale CAS rollback on all four settlement coordinates, malformed and
  mismatched records, fixed producer/scope refusals, restart and lost acknowledgement.
- Eight additional negative follow-up cases were then added. The 220-case regression
  stopped after **79 passes and one setup error**: an existing native expiry test's
  three-second wall-clock window elapsed before dispatch. It did not reach the intended
  post-dispatch recording assertion. This failure is retained, not called a passing run.
- The result-persistence closure was extracted into an internal-only continuation of
  `run_recorded`, with unchanged checks and no new public result API. The test now obtains
  a real claim/worker observation, then explicitly expires both dispatch boundaries
  before calling that same continuation. It no longer races fixture setup against a
  sleep. **All 37 native service tests passed in 1.90 seconds**, together with formatting
  and Clippy. Python F/E9 lint and `git diff --check` passed. Product timeouts and the
  separate bounded recording interval were not increased or disabled.
- The complete **1,837-case source gate passed** (started about 09:57 UTC, terminal
  success at 10:35 UTC). This includes the unchanged pin/rebuild, generated-artifact
  compile, lint, full tests and independent coverage gates. It supersedes the failed
  220-case selection without erasing that failure. Product source remained unchanged
  during this run; documentation and the isolated discovery prototype changed separately.

Current compiler inputs at the same solver-verifying pin
`8277a1d92d599df89e6b4391fc70fd0fa534d696`:

| Component | Bytes | Compiler-input SHA-256 |
|---|---:|---|
| API | 49,126 | `4891fce8825d2ffd66b9a7c0c15a4c7774d2b7de036681a0811b9418df552066` |
| Admission | 47,312 | `1b0c73e570313b5b907af70a1ecebf328847fea859159fa9338de85551852e59` |
| Settlement | 47,890 | `fb0c2d4256c08e4124e4776e1e97c96e154ea3305a9d20782c3b64412cd975be` |

The shared strict operation codec is used by all three. Build-time indentation
compaction outside literals/block comments preserves tokens/newlines/literal bytes and
original authored-source hashes. The 64 KiB limit and verification gates are unchanged;
dispatch is still 65,365 bytes and turn-transaction 65,474 bytes (only 62 bytes headroom).
The native transaction mechanism source SHA-256 is
`1ed9b77065ad646d5c11602c1717f85822fe6c45ae493a6427ae08b630e833c5`;
its trusted stdio embedding is
`3cb2a4becf735e9c95edf6563a65e01b88fe213c3893c4c16331ad52af069461`.
After the internal recording-continuation extraction, recorder source is
`998fd73e52538f7bce11e0a5bd27d4f9b0951ff240f92ff3ed2f6c62b3709be8`.
These identify local source, not qualified executable provenance or a candidate.

The fixture still selects fixed effect bindings and invokes application interpretation
and settlement. The running HTTP service does not automatically drive this sequence.
Next is automatic SIGIL-owned service orchestration/result routing/recovery over these
mechanisms, followed by the remaining routes/tools, quotas/audit/retention and browser.
Real control-plane reuse, live-model/user evidence, protected supported-topology CI,
candidate packaging, fault/load/restore qualification and independent pilot clearance
remain mandatory. This is macOS arm64 source evidence with controlled providers, not
Linux/candidate/security-review qualification. No sibling changes, runtime-pin changes,
real-provider spending, deployment, commit/push or task messages occurred in this slice.

## Automatic-service preparation during the frozen source run

The next [service-loop implementation plan](service-loop-plan.md) is now tied to
concrete source gaps and end-to-end checks. The current synchronous `Attempt` holds
the store through effect execution; embedding it unchanged in the HTTP owner loop
would delay polling/cancellation and other tenants. The next effect mechanism must
retain an actual receipt-bound, non-serializable in-flight handle while allowing the
storage/application owner to process requests. SIGIL still chooses eligibility,
worker selection, recovery, cancellation policy and settlement.

A bounded namespace-key discovery prototype was implemented in an isolated temporary
source copy, **not yet integrated into the repository under verification**. It passed
38 native checks (36 library, 2 stdio) plus 14 actual-process checks. Pages require
actual read authority, return at most 128 keys, preserve tombstones, validate record
integrity and use explicit lexical cursors. Restart/rescan, same keys in disjoint
namespaces, malformed requests, create-only scope refusal and bounded key/page sizes
are exercised. Discovery is not a claim, a stable cross-page snapshot or dispatch
authority. Integrate and reverify it after the existing full source run terminates;
these isolated results cannot qualify repository/runtime conformance.

The 1,837-case repository run passed with product source unchanged. Only documentation
and the isolated prototype changed during its execution. All nine
MVP gates remain open; no public deployment, live-model spending or sibling edit was
performed.

## Scoped discovery integrated after baseline verification — 2026-09-08

After the 1,837-case baseline passed, the isolated discovery changes were integrated
using an exact base-hash check. The native store now exposes the bounded `keys`
primitive described in [its contract](native-store.md#bounded-key-discovery).
No on-disk schema, previous get/commit behavior, grants or SIGIL/runtime pin changed.
It remains a storage mechanism, not automatic work selection or a public API route.

The tree at this checkpoint collected **1,851 Python cases**. The 1,837-case full result above
is the preceding baseline, **not a full qualification of this changed source**.
Post-integration evidence: **35 repository storage/process cases passed in 5.86s**,
including all 14 discovery cases and the 21 existing stdio cases. Their native gate
also ran formatting, Clippy, 36 storage-library and 2 stdio tests. The dependent
native service's **37 tests passed**, with formatting/Clippy, and the worker's
**14 tests passed**. Python F/E9 lint and patch whitespace checks passed. The final
real-HTTP terminal-result/follow-up continuity check **passed in 62.24 seconds**
(whole-test elapsed time, not a service-latency qualification). Terminal results
remain readable after follow-up admission with the updated store.

Current native source identities:

- Store: `19776433cbef97055cb0bcd4155586abb0458e8bd9730a14b881a6e79562a23d`.
- Trusted store stdio adapter: `161d15506b15e0ab0627a4d9619b504cb5c8cbbba025c1317095ec2f2d098bb4`.

The next implementation boundary remains a receipt-bound in-flight execution handle
that lets the application/storage owner answer requests during an effect, then a
SIGIL coordinator using discovery, actual result interpretation and settlement.
See [the service-loop plan](service-loop-plan.md) for the required end-to-end evidence.
No M0–M8 gate is promoted by this mechanism or its local tests.

## Receipt-bound owned effect lane — 2026-09-08

The [owned worker mechanism](owned-worker.md) now retains the actual claim receipt,
exact intent/claim binding, original scope and a private execution-thread result
channel while releasing the store borrow during the effect. It reuses the fixed
SIGIL dispatch/claim/result producers; native code adds no model/tool-selection,
tenant authorization, budget or recovery transition policy. Pending polling does
not wait for the effect or run the result producer. Collection checks exact retained
bindings again before actual SIGIL-directed result commitment.

Cancellation signals the actual owned worker and does not claim remote rollback.
Controller loss is supplied as unconfirmed execution/cleanup, not definitely-unsent.
A lost controller or failed thread creation does not manufacture a replacement bridge.
The lane has one in-flight slot; the future service registry must also be bounded.

The existing synchronous path remains available. An explicit trusted local
`sigil-claimed-worker/v4` conformance adapter exposes native-owned start/poll,
generation-correlated cancellation and scoped reads while refusing supplied results,
receipts, claim/commit/execute bypasses and caller-chosen recovery. Product HTTP
configuration remains v3; stored schemas, compiler caps and the SIGIL pin are unchanged.

Verification completed: **54 focused integration/regression cases passed in 368.47
seconds** (13 new real-SIGIL owned-worker cases plus all 41 existing recording/recovery
cases), with no skips or expected failures in that selection. Its native setup gate
passed formatting, Clippy with warnings denied, all **46 native service library tests**
(9 new ownership/concurrency cases plus the preceding 37), and binary builds. The
initial native-only run also passed all 46 in 9.16 seconds. The new tests include a
held local provider, an actual in-flight cancellation,
post-completion/restart claim fencing, and forged-result/bypass refusal. Native process
fixtures additionally exercise same-store writes during execution, changed intent/claim,
delivery collision, foreign-store collection, Drop cleanup and injected controller loss.
Native protocol fixtures are not substituted for compiler verification.
Python F/E9 lint, final Rust formatting and patch whitespace checks passed. The first
post-adapter Clippy attempt found a missing `Poll` match arm in the old protocol loop;
it was corrected to an explicit protocol refusal before the successful combined run.
Product source stayed unchanged during that run; only documentation changed.

This tree collects **1,864 Python cases across 65 files**. No full 1,864-case source
gate has run; the 1,837-case full result is still the preceding baseline. A complete
source gate remains required after the coherent service integration, with candidate/
protected Linux/operational qualification separate. No M0–M8 row becomes PASS.

Local source identities at this implementation checkpoint:

- Claim gate: `ea5f3c274d782a2a316b7359a0d4681a05ab187bc6da994dd36c3ab2df49a13d`.
- Owned lane: `231e46c501b48a219490c94c953c73e0d7324abc4c0613f925243b3d1bc7fee2`.
- Shared native recorder: `67fd6d1ab59544677be3ec75803013b764d4514fa054fb095a231fdddb2f5df3`.
- Trusted claimed-worker adapter: `69708c3591c1df0b980f80a39b442ffdffe9198ebc989ecfeed44729d6a0bf51`.

The remaining integration is the actual SIGIL coordinator/registry, bounded discovery
exposure, current-authority/bundle binding, interpretation, settlement and explicit
API progress/cancellation/recovery behavior. The HTTP service does not yet drive a
whole turn automatically. No external model call, deployment, sibling edit, runtime
pin change, commit/push or task message was performed in this slice.

## Automatic HTTP turn, scoped registry and restart recovery — 2026-09-08

The opt-in [HTTP v4 service](automatic-service.md) now drives an accepted turn
through model → permitted file → model → terminal publication, then completes a
same-conversation follow-up. The integration test supplies only fixed deployment
configuration, a controlled provider and HTTP requests. No fixture chooses a worker,
interprets a result, advances application state or requests settlement between
admission and completion. SIGIL owns those decisions in the running service.

The fixed registry binds the current credential facts, actual bundle, artifacts and
delegated scopes. All bound policy/transaction templates are validated before
storage initialization. Automatic effect templates must use the actual credential,
bundle and worker-facts placeholders, not static authority literals. The SIGIL
`turn_completion` component derives its reducer event from actual native snapshots,
reuses the existing reducer, and produces an exact revision-bound state/intent commit.

Local post-integration evidence:

- **20 automatic-service tests passed in 384.12s**: model/file/model plus follow-up;
  two tenants with identical session/submission names and disjoint file canaries;
  cross-owner lookup refusal and duplicate submission; owner kill after actual
  provider arrival, automatic uncertainty publication without resend, and another
  restart retaining that result; 17 invalid registry/template/authority/grant cases
  refused before application storage is created. Tenant turns are sequential:
  this is isolation evidence, not the concurrent load gate.
- **77 pure/component/composition cases passed in 26.32s**: 24 new coordinator
  checks, all 22 native-bound delivery interpreter checks and 31 build-recipe checks.
- The automatic test fixture retained formatting, warnings-denied Clippy, all
  **46 native service library tests** and debug builds before building the optimized
  host. The worker's **20 native tests passed in 4.98s**, including six new bounded
  grantless-cache cases, with warnings-denied Clippy. Formatting, Python F/E9 lint
  and patch whitespace checks passed.

The v4 API entry/admission/coordinator reuse a separate fixed, grantless process
for at most 4,096 evaluations; effect workers retain fresh one-use processes.
Complete runtime hashes, solver verification and original deadlines remain enabled.
The pinned runtime source shows immutable Wasmtime module caching with fresh guest
stores/instances on every evaluation. No new runtime pin, compiler-free deployment,
AIN backend or independently measured memory-isolation qualification is claimed.

Debug-host automatic tests initially timed out before final publication; retained
state showed all three effects recorded. Reusing the grantless process alone did
not remove that cost. The optimized native build passed the unchanged positive test
in 119.52s (whole-test time including follow-up, not turn latency); debug/static
gates are still mandatory. During expanded qualification, the first tenant fixture
omitted model usage, and actual dispatch correctly refused the second model call.
Its positive fixture was corrected to supply usage; no quota/deadline or product
assertion was relaxed. A short earlier run was interrupted to correct two new test
expectations to the existing contract (duplicate POST remains 202; unknown accounting
is named `unknown`). These initial attempts do not qualify the changed source.

Remaining concrete product gap: pre-claim expiry, unknown usage, exhausted allowance
or changed/removed authority currently fail closed but can leave an operation
accepted. SIGIL still needs a truthful terminal/accounting transition for those
cases, distinct from abandoned-claim recovery. Public cancellation/progress, all
other interruption boundaries, full API/browser behavior and the rest of M0–M8
remain open. An uncertain restart is not proof that an orphaned remote effect stopped.

This tree collects **1,932 Python cases across 68 files**. The preceding 1,837-case
full source result does not qualify this changed tree; a new full source gate is
required. Focused results above are local macOS arm64/Python 3.14.6/Z3 4.16 source
evidence at the unchanged solver-verifying SIGIL pin, not protected Linux CI,
real-model usefulness, a packaged candidate, shared control-plane conformance or
independent security clearance. No M0–M8 row becomes PASS.

Key local source identities:

- Coordinator: `5de09df8be24bd391372696a4652ee12e1eba7a447040ddc7276b5976922e7af`.
- Delivery interpreter: `8d49261536a0329a3d2a746488fc53636ef2bf761f38ea2a5a52fcf3925956cf`.
- Automatic native registry/interpreter: `4f676a62ec4e2b9719a9d536713dc065b9cbea5760a4a6570be8d25c9fdfcef1`.
- Bound dispatch templates: `ec6f2b0369d82625ae80d7d66c5ff1c37c1f0b1c912ed50cdd0d337922ad8316`.
- Native transaction mechanism: `482be1b75d17ce1b090e2945201fda3264defcd2c96c42b4d55e9f14295937f0`.
- Fixed grantless evaluator: `67957105feefccc38914b5d378fc28b425682458d5f546f7f6d54fe4a3eaf31c`.

No external model call, deployment, sibling edit, runtime pin change, commit/push or
task message was performed. External reviews remain independent of local development.

The unchanged full `./ci.sh` gate ran from **2026-09-08 12:10 UTC** to completion
observed at **12:58 UTC** against the isolated pinned checkout. **CI PASS**, exit 0,
with all 1,932 collected cases accounted for: one Linux-only `LD_PRELOAD` observation
skip on macOS and one strict, documented interprocedural-taint research xfail.
The independent coverage gate passed: **91.19% line / 85.84% branch**. Pin/rebuild,
generated/compile checks and lint passed. A non-failing unclosed SQLite connection
ResourceWarning was reported; it is not hidden or treated as a test failure.

The complete tracked-plus-unignored worktree fingerprint remained
`4bed72616cbe399014993b998bdcd0c0f97729b33add075327ab47c5e5094b15`
through the terminal result. This qualifies that frozen local source checkpoint,
not subsequent pre-claim integration, a Linux candidate, or any M0–M8 gate.

## Never-claimed terminal publication — 2026-09-08

After the frozen 1,932-case baseline completed, [UF1 finalization](preclaim.md)
was integrated into the actual automatic service. SIGIL now finalizes eligible
never-claimed failure cases instead of leaving them indefinitely accepted:
deadline reached, current credential inactive, unknown preceding model usage, or
exhausted next-model allowance. The shared allowance rule is identical for dispatch
and finalization; the exact existing settlement producer is composed into UF1.

The one terminal transaction checks the actual intent and absent claim/delivery,
then writes operation, reservation, capacity and conversation atomically. Prior
observed usage is preserved; unknown/overrun holds stay reserved. No claim/delivery
is invented and possibly sent work cannot be reclassified as unsent. The native
registry change adds only claim READ delegation to transactions. LB2 explicitly
requires the new fixed alias; preceding LB1 bindings are rejected and the changed
bundle is not silently applied to old accepted operations.

Integrated local source evidence at this checkpoint:

- **227 component/contract/composition cases passed in 139.74s**: 48 UF1 cases,
  all 106 unchanged dispatch contract cases, 40 coordinator cases and 33 composition
  checks. This includes exact model-allowance boundaries, pending-tool payload
  matching, preserving earlier known/unknown usage, current-owner/policy mismatch
  refusals, receipt/context consistency and the unchanged 65,536-byte source cap.
- **36 native/HTTP cases passed in 616.82s**: eight actual native read/atomic-commit
  cases, three automatic preclaim HTTP cases, the three existing whole-service
  scenarios and 22 bootstrap/template checks. The original model/file/model/follow-up,
  two-tenant canaries and after-send uncertain restart checks remain unchanged.
  Four new invalid configurations prove that read-write and create-only access to
  reserved claim/delivery namespaces are rejected before storage initialization.
- The native service's **46 library tests passed in 5.90s**, with warnings-denied
  Clippy and formatting checks passing. Native fixture builds also retained the
  mandatory store/service debug/static/test gates before the optimized HTTP host.
  Python F/E9 lint and patch whitespace checks passed.

The API cases use only HTTP admission/lookup/restart and a controlled local provider;
the fixture never steps the application or supplies claims/results/settlement.
Expiry across restart leaves both claim and delivery absent and sends no request.
Unknown/exhausted usage completes the preceding model and permitted file, then
publishes failure without claiming or sending the next model action. Duplicates and
another restart retain results and capacity accounting without a second release.

The first integrated native/HTTP attempt passed eight native cases, then failed in
the test's read-only SQLite inspection helper because stored bytes were not decoded
to UTF-8 before using the test codec. That helper was corrected; no application rule,
grant, deadline or test outcome was relaxed. The 36-case result above used that fix.
Afterward, the three API cases were tightened to assert exact usage totals and close
the test's database connections explicitly; that final rerun **passed all three
cases in 159.40s**, with no skips or xfails. Its test-file SHA-256 is
`009625f673b8c093e47dc9e30421dd9347242db8033cf02e4e89c29a84bf12c0`.
Application/native sources were unchanged during that refinement and match the
identities below. The combined unique focused Python coverage is 263 cases, not
266: rerunning those three cases is not counted as additional coverage.

Authored source identities:

- UF1: `b088f5b7aa629233aa89ddcf92243787c721a371e6e43b02592ed38047d2889b`.
- Shared allowance: `0fc289a584b68bd29827fbc7a5266ec760bc4a4a0d1a93c9a2e3e98c4102e711`.
- Dispatch: `197cfcf666bc643e78c512a1a34ec45322fdd5670ba53e7e31dbf05579401f52`.
- Coordinator: `54cbb337843a9ef219d52c1c64c79ad55e740e056f474d6e35206489450922f8`.
- Native automatic interpreter: `5507eb49be9a16fba4255299c80fe423e8a8776e81fd22596fb0a2d5b9817d23`.
- Composition recipe: `3cbf878bf0c4277b1de5ccd1703d49c79cefb4e91d791d4c0353f03417ddbb57`.

Composed UF1 is 53,919 bytes, SHA-256
`63a9be656985376c167dd2e409738990efc036e84f882bae1b62617f70008388`;
dispatch is 48,451 bytes, `b9c4c115939bd9208c562a41b4e7b7965032cb8be6e91b5ef43679cb2a6a2c05`;
coordinator is 45,551 bytes, `8dfcee87d260ae15cfc2a61b23bec1460e46f0eb4030fc3106f5b78e5423b91a`.
All authored input hashes remain recorded by composition. The existing lexical
indentation compaction was extended to dispatch so the shared helper fits the same
source ceiling. Solver verification, proof requirements, fuel and deadlines were
not changed. The earlier isolated 185-case staging result was not treated as native
service integration evidence.

This tree collects **2,014 Python cases across 71 files**. No full 2,014-case source
gate has run; the preceding 1,932-case full result qualifies only its frozen baseline.
These are local macOS arm64/Python 3.14.6/Z3 4.16 checks at the unchanged verified
SIGIL pin, not protected Linux CI, real-model usefulness or candidate evidence.
No M0–M8 gate becomes PASS.

Next lifecycle slice: [public cancellation](cancellation-plan.md) and progress.
Cancellation must be rechecked at actual dispatch, covering the interval after an
eligible preclaim no-op, and must preserve observed/uncertain effects. Changed/removed
authority, all other route/browser behavior, full accounting/audit/retention, fault/load,
actual control-plane reuse and releasable-candidate/pilot clearance remain open.
No external provider call, deployment, sibling edit, runtime pin change, commit/push
or other-task message was performed. Independent review does not block local work.

## Public cancellation, actual dispatch check and result-read scope — 2026-09-08

The actual SIGIL HTTP API now implements [public cancellation](cancellation.md).
It checks active `chat` scope and the accepting principal AND tenant, reads the
actual operation/cancellation slot, and commits one create-once `CR1` request with
the original operation revision as a check. It preserves OQ2/reservation/intent
records. Matching duplicates and lost acknowledgement retain the same request;
pending GET reports it under the existing `sessions:read` permission.

The automatic service selects `LB3`, cancellation-aware `DF2` dispatch and `UF2`
finalization. DF2 reuses the exact DF1 policy and adds an actual cancellation read
under native exclusive read/claim ownership. A request accepted between an earlier
eligible no-op and actual start therefore prevents dispatch. UF2 uses the same
finalization/settlement core, adding the actual cancellation check: four checks and
four writes cover all eight reads. It preserves prior usage and refuses any retained
claim/delivery, rather than reclassifying possibly sent work as unsent.

While an effect is held, SIGIL reads cancellation between polls, selects the actual
native generation, requests the existing native signal and polls actual completion.
Signal acknowledgement is not a stopped-worker or remote-rollback fact. After-send
cancellation can remain uncertain; an observed final answer may win and remain done.
No native source, guest grants, proof/fuel/deadline requirement or runtime pin changed.

Qualification is in progress on frozen application/native/existing test sources:

- **277 component/contract/composition cases passed in 179.71s** after the final
  scope correction: 42 cancellation, 48 original UF1, 46 coordinator, 35 composition
  and all 106 original dispatch cases. Terminal session 38832, exit 0.
- The 34-case native/automatic run (session 27049) **failed after 10 passes in
  786.68s**, exit 1, at the existing two-tenant automatic scenario's unchanged
  115-second publication wait. Saved state retained the initial observed model and
  file deliveries and the next model intent (sequence 3), with no third claim or
  terminal publication. No mechanism refusal was reported before timeout. Other
  focused runs were concurrent; contention is a hypothesis, not an established
  explanation. This is not a passing suite or isolation qualification. No timeout,
  turn deadline or expected result has been relaxed. The unchanged isolated recheck
  (session 61977) also **failed in 140.17s**, with the same next-model intent retained
  but no third claim or terminal publication. Contention alone is not sufficient.
- **154 API/settlement/native-preclaim cases passed in 1,042.16s** on the same frozen
  source, session 94223, exit 0: all 83 native API, 63 settlement-contract and eight
  native-preclaim cases. This result does not erase the separate automatic timeout.
- Two additional observed-model/tool-boundary tests are staged separately under
  `/private/tmp/sigil-pi-cancel-boundary.WXeDSwdU`, keeping the main test source frozen.
  Their read-only observer chooses cancellation timing from actual committed state;
  it cannot advance the application or write claims/results. Both staged cases
  **passed in 91.14s**, session 48436, exit 0: cancel after observed model/requested
  tool and after observed file/before next model. Exact earlier usage (7 input/3
  output) was preserved, no later claim/delivery/provider request occurred, and
  restart plus duplicate submission retained result/capacity without double release.
  The identical file was subsequently added as
  `tests/test_automatic_cancellation_boundaries.py`, SHA-256
  `bee34ca04dcc60779fba30a7ba06346220cf9e045fa093bdb97407a71c9ac650`.
  Its integrated rerun and the full expanded source gate remain pending.

During this slice, a new regression reproduced a real permission inconsistency:
terminal POST/cancel returned the answer despite requiring only `chat`, whereas GET
requires `sessions:read`. The initial regression failed with a private-result canary.
The fix returns only operation/status/cancellation-status/status-location metadata;
full reply/usage/error content stays behind GET. The final automatic test also reopens
the actual terminal service with a chat-only credential and verifies GET 403 and
control-only cancellation. An earlier regression run was explicitly interrupted
before this source edit (55 passing cases, exit 2); it is not a passing qualification.

Earlier attempts are preserved as diagnostics, not final evidence: 276 component
cases and three automatic cancellation cases passed before the scope fix. The native
gap test first expected a `worker` error instead of the actual `application` refusal,
then tried a storage read while its native preparation was held. The adapter correctly
refused that command. The corrected test explicitly discards that pending one-use
permit before reading, proves no claim, commits HTTP cancellation, and checks actual
DF2 start refusal before the original deadline with no claim or provider call. Its
controlled native conformance boundary is not described as an automatic production turn.

Frozen authored identities for the final regressions:

- API cancellation: `85805a491006946a132bb8f19fbb5a84fd7958a6fdb69cba607518e1cafe31e0`.
- Cancellation record: `96c72fda7c506bf3a93a284be4b2934a80a233f455176e82e70a8d806571defb`.
- DF2 wrapper: `23314fc91b0ba02dd5c6d54d765b0a01da72b0c9e5cc0262fdea25a23a0c17de`.
- Shared finalizer: `7cc7565ac30b23d50509678cbf93fa0adec88ec22ce6358da7d33393d35bfa80`.
- UF2 wrapper: `e3f608559d6cb97738799fdc070166f97bed3948592528b87544be15802cc809`.
- Coordinator: `16935f5f627f00aa4e6abd97ac68ca95856560c77da5217f353511e770e43676`.
- Composition: `f52d53d57984c981af4178947b6119f9831ec1ce61566c28ceae4023ecbaeecf`.
- Native automatic interpreter, unchanged: `5507eb49be9a16fba4255299c80fe423e8a8776e81fd22596fb0a2d5b9817d23`.

Compiler inputs remain below the unchanged 65,536-byte ceiling: API 55,370 bytes,
coordinator 47,529, original DF1 48,451, original UF1 54,323, DF2 50,366 and UF2 55,854.
Their individual authored input hashes are retained by composition. DF2 compiler
input is `3322d05f12cc5d7025b535376b09e9ab6af85fe0e725fc600a234520711a7524`;
UF2 is `9844ebdaa8660e181edfa2d3cdced601dbdb89b71c77d435c98ade675bd3d9ab`;
API is `38e586f40c8e8984f0a0186527f79b54c555092f1cea6ee484eafbacd02796eb`.

The main tree collects **2,072 Python cases across 75 files** after adding those two
boundary regressions (the prior frozen selection collected 2,070/74). No full 2,072-case
source gate has run; the preceding 1,932-case CI pass belongs only to that earlier
frozen checkpoint. These local macOS arm64/Python 3.14.6/Z3 4.16 mock-provider checks
are not Linux lifecycle/load, real-model usefulness, candidate packaging, actual
control-plane reuse or independent review. No M0–M8 gate becomes PASS. The remaining
fault/cancellation matrix, changed/removed policy recovery, full accounting, audit,
retention, API/browser parity and all candidate/pilot requirements remain open.

A passive diagnostic wrapper around the unchanged two-tenant test also failed
(session 48194, 134.89s). It measured pending GETs at roughly 2.1 seconds each,
first provider arrival around 46 seconds, committed tool state around 68.84 seconds
and next-model state around 114.63 seconds. It only timed actual HTTP calls and
read committed SQLite state; it did not write state or choose application transitions.
The pinned MCP source confirms that each call repeats SIGIL compilation/verification,
despite reuse of a process and immutable Wasmtime code. The new cancellation reads
make the current serialized polling/coordination path exceed the existing test bound.
The [fixed grantless evaluator prototype](fixed-evaluator.md) is being tested as a
mechanism-level improvement. It is not selected by the service, and no performance,
security or candidate qualification is inferred from its source alone.

## Fixed grantless evaluation and unchanged automatic behavior — 2026-09-08

The [fixed evaluator](fixed-evaluator.md) now compiles/solver-verifies one exact
source per supervised process and executes every current input in a fresh pinned
runtime instance. It has no effect grants, product-policy branch, external compiled-
artifact input, result cache or verification override. The existing parent still
binds source/executable hashes, clears the environment, supervises deadlines and
recycles at 4,096 evaluations. Existing SIGIL API/coordinator/effect sources are
unchanged from the cancellation checkpoint.

Completed local evidence before default configuration adoption:

- **13 native evaluator unit tests passed in 0.02s**: exact source binding, fresh
  memory and changed input, fresh fuel, actual file-tool denial under empty grants,
  output/source/input bounds, strict framing/fields, caller-asserted proof refusal,
  initialization and call limit. Initial fixture failures used the wrong pointer/
  length packing and a straight-line program to test loop-fuel exhaustion; only
  those fixtures were corrected, with no evaluator rule relaxed.
- Formatting and warnings-denied release/all-target Clippy passed; optimized build
  passed. Compiler/runtime sources use the existing exact SIGIL pin. The initial
  broad dependency resolution was stopped before testing, and the final lock was
  seeded from the upstream lock. Its only registry name/version/checksum difference
  is serde_json 1.0.150 (upstream 1.0.149), matching the existing native bridge.
- **Four actual parent/child tests passed in 259.57s**, alongside the 13 unit tests:
  current input rather than cached answers, real timeout/reaping and a new process,
  executable-change invalidation, and the full 4,096 evaluations plus successful
  evaluation one in the replacement process. The replacement test was then made
  portable by replacing the executable path atomically instead of writing a running
  inode; its final rerun is part of the mandatory integrated gate below.
- **Three staged service scenarios passed in 111.34s**. They called the unchanged
  original test functions and only substituted the fixed entry/admission/coordinator
  executable/hash at bootstrap. Both tenants completed the original isolation/canary
  contract (58.85s whole test call); model/file/model plus follow-up passed (28.18s),
  as did after-send restart uncertainty without resend (16.97s). These timings are
  not M5's load profile or a real-provider capacity claim. No operation deadline,
  provider script, assertion, effect runtime or privilege was changed.

The default automatic development configuration now selects that fixed executable
for the three grantless roles only. Policy, recorder, transaction and actual effect
workers retain their previous runtime. New pin/bootstrap contract tests guard the
selection, and the release-service fixture requires formatting, warnings-denied
Clippy, every native evaluator unit/bridge test and a locked binary build. All other
native store/worker/service gates remain in place.

The post-adoption **38-case integrated run passed in 721.62s**, session 46870,
terminal `46ef0a`, exit 0, from 2026-09-08 15:06 to 15:18 UTC. It covers two new
pin/bootstrap cases, three native cancellation cases, three automatic cancellation
cases, the two integrated handoff-boundary cases, three automatic preclaim cases
and all 25 automatic-service cases. The mandatory evaluator setup also passed
formatting, warnings-denied release/all-target Clippy, all 13 unit and four actual
parent/child tests (including the portable executable-replacement test and full
4,096-call recycle), and the locked release build; setup took 268.96s. The unchanged
two-tenant case took 61.83s and the model/file/model-plus-follow-up case 31.10s.
No assertion, operation deadline or effect authority was changed. These are local
functional timings, not a qualified load envelope. Application/native/test source
and evaluator executable hashes remained unchanged throughout the integrated run.

The selected evaluator executable at the staged checkpoint was 28,173,872 bytes,
SHA-256 `97fdf79c1c5b57039788c38a809e708f7e037a3ab3a961c5c0fb73f069e081dd`.
The integrated source-subset fingerprint is
`059fa54a92bae693a3e2554751a562177118370b5c6c9f5245ee8fd2e38e88a9`:
tracked plus unignored `.py/.sigil/.rs/.toml/.lock/.json/.sh` paths, sorted in the C
locale, per-file SHA-256 lines then SHA-256 of those lines. This excludes documentation
and separate temporary staging files; it is not a candidate or executable provenance record.

The main tree collects **2,074 Python cases across 76 files**. The next verification
is the unchanged complete `./ci.sh` source gate, with the exact isolated SIGIL source
pin, mandatory toolchain evidence and no binary override. No full expanded source
gate has completed yet. The preceding 1,932-case full pass still belongs only to its
earlier checkpoint; the original cancellation-era automatic timeout is retained
above and is not erased by this backend work. Supported Linux, full fault/load,
complete APIs/browser, changed-authority/accounting/retention, real-model usefulness,
actual control-plane reuse, packaging and independent review/sign-offs remain open.
No M0–M8 row becomes PASS; no external model call, deployment, sibling source edit,
SIGIL revision change, commit/push or other-task message was performed.

## Full expanded source run and documentation correction — 2026-09-08

The complete `./ci.sh` run used the frozen 2,074-case main tree, exact isolated
SIGIL source pin, mandatory toolchain evidence and no binary override. Session
22275 started at 15:19 UTC and finished by 16:14 UTC, terminal `cdd194`, **exit 1**.
The source-pin/clean-source check, solver-enabled rebuild, generated artifact/actual
compilation check and F/E9 lint passed. Pytest reached 100%; its only unexpected
failure was `test_readme_test_count_is_current`. The README had the correct 2,074
total in prose but had dropped the exact `N tests + M honest xfail` format required
by the unchanged guard. The platform-specific macOS skip and declared strict
research-only expected failure remained. An unclosed SQLite connection warning
was reported during the npm dependency-walker case; it was not a failing assertion.

The all-authored-files fingerprint at both start and completion was
`533567281231b8d615fd6ff2eb87c3fc6bc3f258251b72a05ac199c5af968065`:
tracked plus unignored paths, C-locale sort, per-file SHA-256 lines and SHA-256 of
that manifest. Unlike the earlier source subset, this also includes documentation.
No main source, test or document was edited during this run.

Because pytest failed, `ci.sh` correctly stopped before its final coverage command.
The unchanged independent coverage command was then run diagnostically on that
run's generated coverage data: **91.19% line / 85.77% branch**, with the same 85%
thresholds and all required critical-boundary checks passing (terminal `2c9eef`).
That diagnostic result does not turn the failed `./ci.sh` into a pass.

After the run ended, the README's required `2073 tests + 1 honest xfail` wording
was restored. The original guard passed in **0.53s**, terminal `283f7d`, including
its actual collection/count and expected-failure checks. No guard, test assertion,
runtime limit or deadline was weakened. The ownership inventory was also brought
up to date: automatic execution/settlement are connected, while the other routes,
full accounting, complete recovery and browser remain incomplete. A subsequent
complete expanded source-gate pass is still required.

While the main run was frozen, a separate temporary stage prepared a SIGIL retained-
conversation history reader and actual API route using existing scoped-read and
grantless-function mechanisms. It contains 49 checks: 33 projection cases, five API
cases and eleven unchanged original automatic/cancellation/recovery cases using only
the staged bootstrap. The complete stage subsequently **passed all 49 cases in
635.64s**, session 66991, terminal `71763f`, exit 0, by 16:28 UTC. This includes
compiler-backed large-page/content cases and actual HTTP tenant/sharing/scope,
pagination/follow-up/restart behavior. The original two-tenant scenario took 61.73s;
the required native setup took 267.26s and reran the full evaluator lifecycle gate.
An additional actual HTTP case for the existing maximum admitted user-message size
is being qualified separately before integration. These temporary files are not in
the main 2,074-case collection and the route is **not integrated or available**.
No native mechanism/grant change, provider call or runtime pin change was introduced.

The model/provider and total spending limit for eventual real-model qualification
have now been requested from the product owner. That unanswered choice does not
authorize spending or block local development. Cross-project review permission and
all other M0/M6/M8 decisions remain pending; no M0–M8 gate becomes PASS.

## Maximum-message admission regression — 2026-09-08

The additional staged HTTP byte-path case failed before history retrieval: a valid
262,144-byte ASCII submission returned `503 host_refused` instead of 202. Session
20588 ended with **1 failed in 262.93s**, terminal `36a971`; mandatory native setup
passed in 258.32s. Read-only inspection of its actual SQLite store found global
revision 0 and no records. The refused request committed no acknowledged work.

Pure localization (session 4569, terminal `6af7d0`, **1 failed / 2 passed in 4.75s**)
reproduced `guest allocation exceeds forge memory limit` in the unchanged admission
producer. Both the original API and the staged history API forwarded the exact
maximum-sized payload through their normal admission continuation. This was an
existing shared-producer allocation bug, not evidence that history itself needed
a wider runtime limit.

A permanent regression in `tests/test_admission.py` reproduced the same failure
before implementation changed (terminal `84ca90`). The admission-local `add_write`
helper now copies the accumulated prefix once while appending its comma and next
record, instead of allocating another full-prefix copy for the comma. The same
2 MiB serialized-size bound, six-record contents, atomic transaction, permissions,
runtime pin, guest memory/fuel limits and time guards remain in force. Shared
storage helpers and native code were not changed.

The focused suite **passed 93 cases in 32.23s**, session 43081, terminal `0344fa`:
58 admission cases plus 35 application-composition cases. New coverage checks
maximum-sized ASCII and UTF-8 payload preservation, the append helper below/at/above
its output ceiling, every stale-coordinate rollback at maximum size, a denied final
namespace and maximum-sized durable commit/restart. The actual native store fixture
also runs its required format, lint, unit-test and build gate. README count validation
passed separately in 0.48s (`907be0`); changed-file F/E9 lint and `git diff --check`
passed. Main collection is now **2,087 cases**, not a new complete-source CI pass.

The source-subset fingerprint for this fix is
`98a22434a0540c21df915d5a12a94a353c7fb991c654e4e7499d8e6a8c061c44`
(tracked plus unignored Python/SIGIL/Rust/TOML/lock/JSON/shell files, C-locale sorted
per-file SHA-256 lines hashed again). A fresh 50-case staged run, session 13814,
is exercising the failed maximum-message HTTP case first, followed by the original
49 history/compatibility cases against the fixed producer. Its result is pending;
the history route remains unintegrated and no M0–M8 gate is PASS.

## Retained-history integration — 2026-09-08

The fresh 50-case staged run **passed in 620.12s**, session 13814, terminal `1486c4`,
exit 0, by 16:57 UTC. This includes the previously failing maximum-message actual
HTTP admission/history/restart scenario, all 33 projection cases, five history API
cases and eleven unchanged automatic/cancellation/recovery contracts. Required
native setup passed in 251.25s, including the full evaluator lifecycle checks. The
unchanged two-tenant case took 61.30s. The admission-fix source-subset fingerprint
still matched `98a22434a0540c21df915d5a12a94a353c7fb991c654e4e7499d8e6a8c061c44`
at completion; no application/test source changed during that run.

After that terminal result, `app/pi/history.sigil` and `history_api.sigil` were
integrated through the ordinary application composer, with corresponding v3/v4
test deployment configuration and permanent projection/HTTP tests. SIGIL now
requires exactly the fixed `admission` and `history` function registrations. The
v4 history function uses the same fixed grantless evaluator as entry/admission/
coordinator code. No native code, capability, fuel/memory limit, deadline, runtime
pin or effect behavior changed. The Python additions are build/configuration/test
code, not a production request policy or conversation driver.

The integrated compiler inputs are **byte-identical** to the passing stage
(diagnostic terminal `84d1c8`): API SHA-256
`95a7dbc7986cb1e51fc6613eef7e972096553807d7368ab1f72aff57813ec373`
at 59,034 bytes with nine authored inputs; history SHA-256
`cc83f07829c26dc92a7579e81be2ad7f76eb970cc78d89ebc89680b80350cfc1`
at 40,712 bytes with seven authored inputs. Both retain the 65,536-byte source
ceiling and mandatory verification. These hashes establish source identity, not
signed artifact admission or approval to rebind older accepted operations.

The integrated projection/composition/admission selection **passed 127 cases in
47.52s**, session 16112, terminal `ff2420`. Changed-file F/E9 checks passed. Main
collection is now **2,139 cases** after adding a UTF-8 actual HTTP maximum-message
case and exercising invalid/missing registrations for each function independently.
The exact README collection/count guard passed in 0.54s, terminal `e9c8ab`.

A broader **136-case integrated HTTP/API/automatic/fixed-evaluator run** started
at 17:01 UTC, session 52143. It starts with the ASCII and UTF-8 maximum-message
HTTP cases, then the remaining history scenarios, original automatic/cancellation/
preclaim/boundary scenarios, the full native API suite and evaluator contract tests.
Its result is pending. Its source-subset fingerprint is
`4d1280ef30bff10d075a0f230e85e78c98e9b037e931875043b2f61f782ba3f6`
using the same rule above. Application/native/test sources are held unchanged
while that run executes; documentation updates do not alter those inputs.

The [history contract](session-history.md) specifies tenant-shared retained context,
exact string revisions, bounded non-clipping pagination and truthful tool/phase
labels. It is not full transcript retention, discovery, export/delete, intermediate
progress, browser rendering or a user-approved final sharing policy. The bundle
changes; changed/removed-bundle and authority recovery remain open. A complete
expanded `./ci.sh` pass and all remaining M0–M8 evidence are still required. No
readiness gate is PASS and no deployment or provider spending was authorized here.

### In-progress verification and isolated next slice

During session 52143 the integrated ASCII/UTF-8 maximum-message HTTP cases and
all selected history, automatic-turn, cancellation, preclaim and observed-boundary
modules passed their assertions. The remaining native API checks and final contract
checks are still running; no aggregate pass is recorded before terminal completion.
The source fingerprint rechecked at 17:17 UTC still exactly matched
`4d1280ef30bff10d075a0f230e85e78c98e9b037e931875043b2f61f782ba3f6`.

Read-only inspection established the next discovery constraint: the native store
already exposes scoped keys to the automatic coordinator, but the public action
interpreter lacks enumeration and its request loop permits eight evaluations.
[The discovery plan](session-discovery-plan.md) proposes bounded native metadata
facts with SIGIL-owned visibility/paging, not fetching every transcript or raising
that ceiling. Host-contract compatibility and the actual native/HTTP implementation
remain explicitly unimplemented decisions and work.

A separate temporary prototype at
`/private/tmp/sigil-pi-discovery-prototype.RcDKQuqf` contains a pure SIGIL query/page
projection and 51 collected checks over proposed metadata. Its composition-only
check passed in 0.06s (`6047f4`), and Python F/E9 lint passed; SIGIL execution has
not yet been verified. The temporary stage changes no main application/native/test
input and is excluded from the 2,139-case collection. Pure proposed observations
confer no authority or actual discovery evidence. Next: finish the live regression,
execute the staged pure checks, and run the full expanded main source gate before
adopting another main-tree mechanism change.

## Integrated history/API regression completed — 2026-09-08

Session 52143 reached a successful terminal result, `8c2e74`, exit 0:
**136 passed in 1,623.85s (27:03)**, by 17:29 UTC. This covers all seven integrated
history API cases (including actual HTTP ASCII and UTF-8 maximum-size admission,
history retrieval and restart), the selected automatic/cancellation/preclaim/
observed-boundary modules, the complete expanded native API suite and both fixed-
evaluator contract cases. The mandatory native setup passed in 261.97s. The original
two-tenant automatic case passed in 57.04s; the native reservation/isolation/replay
case took 58.37s. No assertion, limit, deadline or native implementation changed
during the run. The post-run source-subset fingerprint still exactly matched
`4d1280ef30bff10d075a0f230e85e78c98e9b037e931875043b2f61f782ba3f6`.

This supersedes the pending status of that focused run, not the prior failed full
source gate. The unchanged complete `./ci.sh` must still run against the 2,139-case
tree, with mandatory pinned toolchain, regression coverage and native requirements.
No M0–M8 gate is PASS. Discovery's pure prototype is being checked separately after
this timing-sensitive run; it remains outside the main tree and cannot establish
native metadata support, authenticated discovery or public API availability.

The separate listing prototype subsequently **passed all 51 pure checks in 16.34s**,
session 75011, terminal `5e8111`, exit 0, by 17:30 UTC. It used the same exact pinned
SIGIL runtime and unchanged 300-million fuel/guest memory limits, with mandatory
verification. The checks validate the proposed query/page projection only; native
metadata support, host-contract compatibility and actual HTTP discovery remain
unimplemented. The main collection is unchanged at 2,139 cases.

Next is the complete unchanged source gate against this main history/API tree,
with no executable override and the clean isolated SIGIL source pin. Main authored
files will remain frozen for that run; temporary discovery work is outside it.
The full gate's live handle and start fingerprint are recorded in the task's tool
evidence, and its terminal result must be inspected before a full-source pass claim.

## Complete history/API source gate passed — 2026-09-08

The full main `./ci.sh` run, session **50118**, completed **CI PASS**, exit 0,
terminal **`a76db1`**, observed at **18:29:44 UTC**. It started at 17:31 UTC
against the 2,139-collected-case main tree. The exact clean isolated SIGIL source
pin, solver-verifying rebuild, generated-artifact synchronization/actual compile,
whole-tree F/E9 lint, complete pytest tree and independent line/branch coverage
gate all passed. Coverage was **91.19% line / 85.84% branch**, including the required
critical-path checks. The expected macOS platform skip and research-only xfail
remain explicit. A SQLite `ResourceWarning` was reported and was not a gate failure.

All authored main inputs were frozen for the entire run. The aggregate SHA-256 at
both start and after successful completion was:

`78eb57e2609e4cf5326833b740dda7158815a711a65e91c9a578e3b791deb1d2`

The post-run fingerprint was independently read at 18:30 UTC, terminal `86d4fe`,
using `git ls-files --cached --others --exclude-standard -z`, bytewise sorted paths,
per-file SHA-256 and SHA-256 over those resulting records. The source subset remains
`4d1280ef30bff10d075a0f230e85e78c98e9b037e931875043b2f61f782ba3f6`.
The native application-host release executable was
`855081b57b60dfa0fb0dfeb5cf594c8a8d954acd71ffbb4a359c31f724eb93fc`;
the fixed evaluator was
`97fdf79c1c5b57039788c38a809e708f7e037a3ab3a961c5c0fb73f069e081dd`.

Invocation used no executable override, `PI_REQUIRE_TOOLCHAIN=1`, offline Cargo,
two build jobs, and the isolated exact pin at
`/private/tmp/sigil-pi-pinned-8277a1d9.9NXtMw`:
`8277a1d92d599df89e6b4391fc70fd0fa534d696`, crates tree
`5ffb00735509f9f871ddd11e3664db265b320ac0`, stdlib tree
`3690c875f8bb86a2cafae35486c187429bd9c5fc`.

This supersedes the pending full-source status above for this history/API checkpoint.
It does not erase the older README-format failure or qualify the later staged
discovery sources. These documentation updates happened only after the terminal
result and fingerprint check. No limit, deadline, grant, assertion, source test,
runtime pin or product implementation changed during the run.

This is local macOS source evidence, not protected release CI, a Linux candidate,
live-provider success, browser usability, control-plane reuse or independent review.
All M0–M8 rows remain unqualified. Source CI no longer holds the next mechanism's
execution tests; model/spending/profile choices and the other external decisions
remain pending without preventing ordinary local development.

## Discovery native/API stage entered execution verification — 2026-09-08

While the full source gate was frozen, the separate temporary discovery directory
gained a scoped native metadata mechanism, versioned stdio/HTTP host support and
the SIGIL public-route connection. No main application/native/test source changed.
The [discovery plan](session-discovery-plan.md) records the mechanism, authority and
live-scan semantics. Host v5/v6 explicitly uses AH4/HC4 and binds its actual command
inventory; legacy v3/v4 uses its original contract. This does not qualify an upgrade
of in-flight operations across those profiles: bundle identity changes must still
be handled explicitly before a release.

The staged entry is **60,620 bytes**, SHA-256
`ec1d1bf060609fc0a166d49325e76f3f63b5e45070a24faa5a2d72474150d34b`,
with eleven input hashes. The unchanged projection is **32,750 bytes**, SHA-256
`9107a84e5fad6a497730af2b0bfb3327b49e126b29e65983f06452625f629ee6`,
with five input hashes. Both fit the unchanged 64 KiB source bound. The selected
sequence uses four entry evaluations within the original eight-step ceiling.

After full main CI passed, session **62260** started the staged **94-case** suite at
18:30 UTC, with mandatory baseline native/evaluator gates and staged native gates
retained. Its staged source fingerprint was
`1d6a4db04aace17bc2c86fccc02d5a479052b6b6b75df17deeaa92f73f2c87ff`
(`c0b3f6`): bytewise sorted `.py`, `.rs`, `.sigil`, `.toml` and `Cargo.lock` files,
per-file SHA-256 and SHA-256 over the resulting records. Stage sources and main
code/test inputs remain unchanged while it runs; only main documentation updates
are permitted during this stage run.

At 18:32 UTC, **all 28 pure entry cases had completed successfully** and the suite
had entered the HTTP module's native prerequisites (`bf9bb6`). Those cases verify
SIGIL sequencing, current authority, inventory/ABI requirements and refusal behavior
using synthetic envelopes. They do not establish authenticated HTTP/native behavior.
The remaining real HTTP, executable-profile and projection tests and aggregate result
are still pending. No new native/API pass, integrated discovery route or M0–M8 pass
is claimed before their actual results.

### First staged run stopped on an incorrect revision expectation

Session 62260 became terminal with **28 passed / 1 setup error in 302.58s**, exit 1,
terminal `31b12c`, observed at 18:38 UTC. The pure SIGIL entry module passed. The
HTTP module did not execute: its mandatory native-store gate rejected a newly
authored test assertion that incorrectly equated a record revision with the
store-wide transaction receipt. Baseline native/evaluator prerequisites and staged
store formatting/Clippy had completed before that unit-test failure. No aggregate
native/API success is inferred from this stopped run.

Inspection of the actual store commit code confirmed the distinction: each written
record uses its prior CAS revision plus one; the receipt separately advances the
store-wide transaction counter. The maximum-page test now explicitly requires
record revision **2**, global receipt **4**, and revision **1** for the other newly
created records. The staged HTTP tombstone fixture uses an actual record read for
its CAS coordinate instead of borrowing a transaction receipt. These changes correct
test expectations and strengthen the distinction; no native implementation, limit,
deadline, grant or refusal assertion changed to make the failure disappear.

The subsequent complete staged store test run passed: **42 library tests and four
executable tests**, 2.00s and 0.12s respectively, session 79536, terminal `518982`,
exit 0 at 18:40 UTC. Staged store formatting and Python F/E9 lint also passed. The
staged service's own Clippy/native checks and another complete 94-case run remain
required before integrated discovery or authenticated HTTP behavior can be claimed.
Main application/native/test inputs still match the already-passed source checkpoint.

The staged host then passed its own formatting and Clippy checks, followed by
**all 52 native unit tests in 6.55s**, session 16557, terminal `19702b`, exit 0 at
18:42 UTC. These include the new metadata command's scope, codec, clock/deadline,
bootstrap and legacy/new-ABI cases. The HTTP/public-route integration remains
unverified until the complete staged suite is rerun successfully. All mandatory
baseline native/evaluator prerequisites remain in that rerun; none is skipped.

The second complete **94-case** staged run started at 18:43 UTC, session **76707**.
Its authored stage source fingerprint (excluding native build output) is
`5c8846d34dec321ba52b6714f216f99ba85da8e950d8b3f7a96ba3c1f9255962`
(`8172b9`). The main code/test fingerprint remains
`4d1280ef30bff10d075a0f230e85e78c98e9b037e931875043b2f61f782ba3f6`.
Its aggregate result is pending; no new implementation changes or deadline/limit
adjustments are included in this rerun.

## Discovery conformance passed and integrated — 2026-09-08

The second staged suite completed **94 passed in 567.14s (9:27)**, session 76707,
terminal `a927f6`, exit 0, observed at 18:54 UTC. All baseline native/evaluator and
staged native prerequisites passed (319.74s setup). The actual model/tool/list/
reopen/two-tenant/restart flow passed in 107.61s, HTTP corruption handling in 31.07s,
maximum-page/tombstone handling in 24.13s and actual older-executable compatibility
in 21.59s. These are deterministic local-provider tests, not live-provider task scores.

The stage's authored source fingerprint at start and after success matched
`5c8846d34dec321ba52b6714f216f99ba85da8e950d8b3f7a96ba3c1f9255962`
(`489db9`, native build output excluded). Main code/test inputs still matched
`4d1280ef30bff10d075a0f230e85e78c98e9b037e931875043b2f61f782ba3f6`
(`7eb7de`) before integration. The tested new host executable was
`3e6e26c4422208c16c046975eb9850fa8a673948425388b592a8e94df5c459d6`;
the tested new store executable was
`242b48bc02ee594b08bdbbac97096576b8084256bf357035040e174d30e6d614`.
The old host/store executables were respectively
`855081b57b60dfa0fb0dfeb5cf594c8a8d954acd71ffbb4a359c31f724eb93fc` and
`aea049bda2aec8df049fb34b97186d55ec908bba139096c77f8e1bc2b44889e3`.

The six changed native files and new SIGIL listing components were then copied into
main. The production build recipe now provides `api_discovery` and `listing`, while
retaining the original `api` component's exact compiler input for v3/v4 compatibility.
It emits the **same tested SIGIL compiler inputs**: 60,620-byte entry SHA-256
`ec1d1bf060609fc0a166d49325e76f3f63b5e45070a24faa5a2d72474150d34b`
and 32,750-byte projection SHA-256
`9107a84e5fad6a497730af2b0bfb3327b49e126b29e65983f06452625f629ee6`
(`cc1478`). No Python production request or turn orchestration was added.

To keep the older-executable tests reproducible after main rebuilds its native
host, a **test-only 34-file source fixture** preserves the exact preceding native
store/worker/service sources. All three source directories were compared byte for
byte against the passing baseline before main changed (`c4986f`, `b1c67e`, `76de8b`).
Its manifest and drift checks bind aggregate
`7a6c2838f514b67a34fef88e99674c0e123527365585a38b8d4bb8406b79b0d5`.
Tests rebuild actual older binaries from those locks and retain native gates; no
old binary is guessed from a path, emulated by a stub, or shipped in the product.

Integrated pure/recipe/fixture checks passed: **123 passed in 43.48s**, session
80792, terminal `378356`, exit 0, observed at 19:09 UTC. This includes all 28 entry
and 51 projection cases, build-input/byte-identity checks, explicit rebase refusal
and frozen-fixture integrity checks. The whole main tree now collects **2,241 cases**
(`bde3fe`, 0.31s). F/E9 lint passed (`05f391`). README keeps the existing guarded
format, `2240 tests + 1 honest xfail (research-only)`.

The [discovery contract](session-discovery.md) records the additive public route,
tenant sharing, exact revisions, live-scan cursor semantics, non-mutating behavior,
explicit host profiles and truthful error handling. The eleven legacy API routes,
complete lifecycle, browser, usage/audit/retention, recovery/upgrade, Linux candidate,
real-model/onboarding, control-plane reuse and independent clearance remain required.
All M0–M8 rows remain unqualified. An integrated whole-source run is still required;
the earlier 2,139-case CI pass does not qualify this later implementation by association.

## Integrated discovery whole-source gate passed — 2026-09-08

The complete expanded local source gate completed successfully at **20:22:03 UTC**:
session 36164, terminal `5b9e83`, exit 0, **CI PASS**, with **2,241 collected cases**.
Coverage was **91.19% line / 85.84% branch**; the independent critical-boundary
requirements passed. The expected Linux-only macOS skip and declared strict
research xfail remain. A Hypothesis-helper SQLite ResourceWarning was non-fatal.
This supersedes the integrated-source-pending statements at earlier checkpoints,
not any missing product or candidate evidence.

The run started at 19:18:34 UTC using the unchanged `./ci.sh`: exact-pin
solver-enabled rebuild, generated-artifact and real compilation checks, F/E9 lint,
the whole test tree, and independent coverage gates. Required native store,
worker, service, fixed-evaluator and frozen older-host fixture checks were retained.
It used the isolated SIGIL pin `8277a1d92d599df89e6b4391fc70fd0fa534d696`, offline
Cargo, two build jobs, no executable override and `PI_REQUIRE_TOOLCHAIN=1` on
macOS arm64, Python 3.14.6 and Z3 4.16.0. No test, threshold, limit, deadline, grant
or runtime pin was weakened to obtain this result.

The bytewise-sorted authored-file SHA-256 aggregate matched before and after the
entire run: `c3ed2d68e9a2b167f2c93829c983cc23390c740964f389e2b60db0e75bb9760e`.
The code/test/config subset was
`06857886436566530e84d22cb351abc805a02121a25a0c6f3dd3442e070596a2`.
The terminal fingerprint check (`e23d98`) also passed `git diff --check`; the subset
was independently rechecked unchanged before these documentation-only updates.

Tested executable SHA-256 values at that checkpoint:

| Executable | SHA-256 |
|---|---|
| Main release application host | `1a286960dfb2c1b1d336c3d3f127a911d04f4997c313491dcdbcd8e16653c774` |
| Main debug store | `354ec9775e976dd60f33b175baee34edf59d35d792fc24857ae4dd4b06bb77ec` |
| Main debug worker | `d0721da57e7f058ec7e8fae1f76eced6ec69fa0f52490e7aa7fd0255fc2685a6` |
| Main release fixed evaluator | `97fdf79c1c5b57039788c38a809e708f7e037a3ab3a961c5c0fb73f069e081dd` |
| Frozen v4 fixture release application host | `3657cf8490695171f29c93b489a23bc53c0356ad1c247ea1db2a95f544eae2de` |
| Frozen v4 fixture debug store | `cd4db6c2ec3d518085c605badb5109223fc184ed25763c429c0fb3d64c006dc8` |

This is integrated **local source evidence**, not protected Linux CI, an immutable
release candidate, browser verification, live-model usefulness, second-consumer
conformance, onboarding or independent security clearance. The browser transport
and client remain separately staged; their prepared tests do not count as executed
browser evidence. No M0–M8 gate is PASS, and the active goal remains unachieved.

## Staged browser transport entered execution verification — 2026-09-08

The separate browser stage at `/private/tmp/sigil-pi-browser-stage.YXdntFy8`
passed native formatting and strict all-target Clippy (`7ac43b`, exit 0), then
all **64 native service tests**: 52 existing library tests in 6.05s plus 12 new
public-asset tests in 0.01s (session 1933, terminal `234813`, exit 0). An earlier
Clippy handle was unavailable; its outcome was not inferred. These are fresh
terminal results, not assumed success from that missing handle.

The added mechanism loads bounded, operator-declared public bytes before opening
application state and serves only exact `/` or `/_assets/` mounts. It checks actual
file hashes, preserves existing API/runtime limits and adds opt-in same-origin
transport checks. SIGIL still owns API authorization, conversation and agent
decisions. The manifest establishes byte identity, not publisher trust or pilot
admission. No browser source or native transport change has been integrated into
main at this checkpoint.

The staged **13-case HTTP/browser suite** is now running in session 67353 with
mandatory baseline native/evaluator and frozen older-host prerequisites retained.
Its authored-file fingerprint before execution is
`bbe9403fe5785907671cb6ca30ee483e30b6f35eb650b9078afefe734de039da` (`65c7b8`).
Stage sources and main code/test inputs remain frozen for the run. Local scripted
provider results will not count toward the 18/20 real-model criterion. The prepared
browser cases and screenshots must execute and be inspected before browser success
can be claimed; all M0–M8 gates remain unqualified.

### First browser run exposed a native fetch receiver defect

Session 67353 completed **11 passed / 1 failed in 452.39s**, terminal `6b3dc0`,
exit 1. All 11 HTTP/manifest/real older-host compatibility cases passed, including
the real native/SIGIL model/tool/follow-up/two-tenant/reopen path against a local
scripted provider. The browser's initial rendering check passed; its connection
step failed before any provider request. The final lost-ack browser case did not
run because the suite stopped at this failure. No aggregate browser pass is claimed.

The actual connection/failure screenshots were inspected. The stage's authored
fingerprint still matched `bbe9403fe5785907671cb6ca30ee483e30b6f35eb650b9078afefe734de039da`
after failure (`e43ca4`), and main code/test inputs remained unchanged (`bd1cf1`).
An isolated Chromium check reproduced a browser-specific exception: invoking
`fetch` as a PiApi field supplied the wrong receiver and threw "Illegal invocation";
an unbound invocation succeeded on the same data URL (`0f4d91`). The new receiver
regression failed against the old client (`e5e582`).

The staged client now invokes the retained fetch function without that receiver.
All **63 lightweight tests passed** (`0c3f3a`, 74.95ms), including the regression.
These are transport/minimal-DOM checks, not a substitute for rerunning the complete
actual browser path. Failure diagnostics now retain browser errors and non-sensitive
status text. No native/SIGIL source, resource limit, deadline, grant or existing
assertion changed to obtain this result. The fix remains staged and unqualified.

The complete 13-case suite was restarted only after that terminal failure and
specific client fix, now in session **2790**. This is browser retry cycle 1, not a
restart following an observation timeout. The new staged authored fingerprint is
`bab21da57a3278d0ab557fb611c3e95de71c1a173663aeb84df921fbd0f59b07`
(`a095da`). All native/evaluator/older-host prerequisites and existing assertions
remain required. Stage sources and main code/test inputs are frozen until the
terminal result; browser-flow success and integration remain pending.

### Initial staged browser conformance passed

Retry session 2790 completed **13 passed in 464.86s**, terminal `e2220d`, exit 0,
observed at **20:51:04 UTC** on 2026-09-08. All mandatory native/evaluator and
real older-host prerequisites passed. The stage's before/after authored SHA-256
aggregate matched `bab21da57a3278d0ab557fb611c3e95de71c1a173663aeb84df921fbd0f59b07`
(`20f154`); main code/test inputs remained
`06857886436566530e84d22cb351abc805a02121a25a0c6f3dd3442e070596a2` (`11ad2f`).
The tested staged release host digest was
`1e190553b4a20e3e5cfe68f8b129e2deb6b652e619fe0880cfdf559ca643b922`.

The real browser completed a model/tool/response turn, follow-up, direct API turn
visible through chat, delayed-history/view switching, tenant switching/reopening,
keyboard navigation and refresh without replay. Its normal case made three browser
submissions plus one direct API submission and observed five local-provider calls.
The lost-acknowledgement case dropped the response after actual native acceptance,
then explicitly retried identical bytes and identity: two browser submissions,
one submission key, two provider calls and one model/tool turn. Both flows reported
zero page errors and foreign requests. The intentional transport abort in the
lost-ack case was the only permitted browser console error for that case.

Actual desktop, 390px mobile and lost-ack screenshots were visually inspected.
The mobile image contains loaded retained history with a transient status check,
not a terminal-status screenshot. These checks establish the initial local browser
path, not independent usability, all fault states or live-model usefulness.

The browser remains staged. Four additional actual-browser boundary cases are
prepared separately: unknown/expired credentials, denied submission and refresh
after an observed external request followed by cancellation/restart. Syntax/lint
checks passed; those cases have not yet executed. Full browser boundary coverage,
main integration/CI, API lifecycle parity and all M0–M8 gates remain unqualified.

The combined **17-case** suite (four new boundary cases plus the preceding 13)
collected successfully (`ffd22b`, 0.29s) and is running in session **38270**. The
client/native stage is unchanged except for its post-pass documentation; its
current authored fingerprint is
`5b089f4a0aeb537ee94a11b69fad7ef71402a73dabd95249f7e91c2703afee41` (`220023`).
The separate boundary stage's authored `.py`, `.mjs` and README aggregate is
`6528c8d6149efcf5d89ceb46fda851835897f54ac1868f1d7f8f13e35263c62c` (`31a84e`);
Python bytecode/cache files are excluded. All mandatory prerequisites and original
assertions remain in this run. The cancellation case coordinates an actual provider
observation through private test-process pipes, not manufactured application state.
Its withheld response is a fault injection against the unchanged worker deadline.
No new boundary-case pass is claimed before a terminal result.

### Boundary fixture corrected without weakening bootstrap policy

Session 38270 stopped with **1 passed / 1 failed in 277.97s**, terminal `f26582`,
exit 1, observed at 20:59:59 UTC. The actual unknown-credential browser case passed
with no committed state change or provider work. The expired-credential case failed
during service startup, before opening a browser: its registry contained no active
credential. SIGIL correctly returned an application refusal. The remaining 15 cases
did not execute. Both stage authored fingerprints matched after this terminal
result (`621a6c`, `5da03c`); main implementation remains unchanged.

Inspection of the actual SIGIL `boot` policy confirmed that at least one registered
credential must currently be active. The existing native/listing fixtures already
distinguish bootstrap admission from rejecting an individual expired credential.
The new browser fixture now retains a disjoint active control tenant but sends only
the expired credential from the browser. No application/native/browser source,
permission, expiry rule, runtime deadline or assertion was weakened. Full rerun
remains required; this does not qualify expiry during an active operation.

### Staged browser boundaries passed; development integration completed

The corrected combined run, session 90241, completed **17 passed in 503.01s**,
terminal `7ddf04`, exit 0, observed at **21:12:39 UTC on 2026-09-08**. All native,
fixed-evaluator and real frozen older-host prerequisites remained required. Both
stages' authored fingerprints matched before/after the run:

- Browser/native stage: `5b089f4a0aeb537ee94a11b69fad7ef71402a73dabd95249f7e91c2703afee41`.
- Boundary stage (`.py`, `.mjs`, README; excluding bytecode/cache):
  `58c21c35c9f51aa1dbc3a191412c87e7961b7e75b93adf64147b07600ad53000`.
- Tested staged release host:
  `1e190553b4a20e3e5cfe68f8b129e2deb6b652e619fe0880cfdf559ca643b922`.
- Main pre-browser code/test/config fingerprint remained
  `06857886436566530e84d22cb351abc805a02121a25a0c6f3dd3442e070596a2`
  immediately before integration. Its earlier 2,241-case source pass is historical
  evidence for those inputs, not for the subsequently expanded repository.

Unknown and expired callers caused no provider work or committed state change.
The readonly caller's single submission was denied and did not execute; the browser
retained an explicit retry/clear state. The observed-request case refreshed and
reconnected without submitting again, requested cancellation while work was active,
and retained a possibly-delivered outcome and unknown usage across service reopen.
It observed one provider request, and the other tenant could not read the operation.
The preceding normal and lost-acknowledgement cases also passed in this same run.

Actual refusal and uncertain-outcome screenshots were inspected under the local
`pytest-334` evidence directory. The uncertain screen correctly distinguished a
committed history snapshot from a still-refreshing terminal status. These local
fixture screenshots do not establish independent onboarding or real-model quality.
No application limit, policy, grant or assertion was loosened to obtain this pass.

The tested browser, opt-in native asset transport and 17 HTTP/browser cases were
then copied into this repo, initially checked byte-for-byte against their staged
sources. Test adapters now resolve the repo's manifest builder/public files and
use the existing mandatory native service/worker/store fixtures. The frozen v4
fixture and its old CLI remain unchanged. This is scoped development integration,
not a product release or completion of M4.

Exact Node/Playwright development dependencies, lockfile, runtime checks and
mandatory workflow setup were added. The source gate still retains its previous
regression and coverage requirements. Seven new browser-tooling checks passed
in **0.52s** (`593591`), including execution of all 63 JavaScript unit checks,
syntax validation, exact dependency/CI guards and missing/wrong-runtime refusals.
No real provider was called and no CI/release job or external deployment was started.

The suite now collects **2,265 cases**. **The expanded integrated full-source gate
has not yet run.** Native formatting, Python lint, targeted tooling and collection
checks do not replace that run. The broader M4 boundary matrix, all eleven legacy
route migrations and every remaining M0–M8 criterion are unchanged and unqualified.
See [development browser setup and limitations](browser-interface.md).

### Integrated browser source gate passed on unchanged inputs

Session **48322** completed with **CI PASS**, terminal `deaf8e`, exit 0,
observed at **22:39:12 UTC on 2026-09-08**. The expanded suite collected
**2,265 cases**. The exact-pin solver-enabled rebuild, generated-artifact/real
compile check, whole-repo F/E9 lint, full test suite and independent coverage
gate all passed. Coverage was **91.19% line / 85.84% branch**; the existing
critical-boundary requirements also passed without a threshold change.

The complete authored-file aggregate matched before and after execution:
`267192d9ed39b90c32102fe3443e30f2dc69fc1f61f94926c2849099fc01ffc7`
(`808f69`, `8e8670`). These documentation updates follow that terminal result;
no implementation or test inputs were edited during the run. Its locally built
release host digest is
`391222cc03ec44d3e822090c10f9d99bce3df5d8e86bf83c1d796a6b8fb9e7ca`.
The native store, worker, fixed evaluator and real frozen v4 host digests remained,
respectively:

- `354ec9775e976dd60f33b175baee34edf59d35d792fc24857ae4dd4b06bb77ec`
- `d0721da57e7f058ec7e8fae1f76eced6ec69fa0f52490e7aa7fd0255fc2685a6`
- `97fdf79c1c5b57039788c38a809e708f7e037a3ab3a961c5c0fb73f069e081dd`
- `3657cf8490695171f29c93b489a23bc53c0356ad1c247ea1db2a95f544eae2de`

This run used macOS arm64, the same clean SIGIL
`8277a1d92d599df89e6b4391fc70fd0fa534d696` checkout, solver-enabled builds,
Node 26.4.0 and the available Playwright 1.62.1 package. It retained all native
worker/store/service/fixed-evaluator and frozen older-host prerequisites, the
63 JavaScript checks, all 17 integrated HTTP/browser cases and previous regressions.
The Linux-only `LD_PRELOAD` rename observation was skipped on macOS, and the one
declared strict research-only xfail remained. A nonfatal Hypothesis SQLite
ResourceWarning was emitted; it did not fail a test or coverage check.

Six integrated screenshots were inspected from the `pytest-337` local evidence:
desktop model/tool/response, mobile reopened history, permission denial, expired
credential refusal, lost acknowledgement and uncertain cancellation. The mobile
image shows loaded committed history with a transient operation-status check;
the uncertain screen retains the prior committed history while its refresh is
pending. Neither image is presented as a terminal-history snapshot. Error text,
retry/cancellation distinctions and literal untrusted markup were visible. The
neutral styling of the disconnected credential-refusal notice remains a usability
polish item, not an authentication bypass or an independent-user acceptance pass.

Separately, a clean temporary npm installation of the exact locked development
packages passed (`f686fa`, three packages in 427ms), with lifecycle scripts
disabled and isolated empty user/global npm configurations. The first attempt
had failed before installation because npm rejected using `/dev/null` for both
configuration roles; separate empty files corrected that test setup. The copied
runtime checker then resolved the new Playwright/core 1.62.1 installation without
the bundled-package override (`26e7cf`), and all four copied input hashes remained
unchanged. Its Chromium path still uses this Mac's existing browser cache: this
does not prove a clean Linux install or browser binary provenance.

Four additional actual-browser cases are prepared outside the repo: mid-turn
quota exhaustion, unknown usage, an actual service outage/reopen, and a held
tenant-A response after reconnecting as tenant B. Combined with the integrated
17 cases they collect **21 tests** (`df7731`), with the original fixture module
and all prerequisites retained. Those four new cases have not yet executed.
Their actual run and screenshots remain required before integration or a pass
claim. The source gate above does not include those external staged files.

No hosted CI, release, external provider call or deployment was performed. This
is integrated local source evidence, not M1 parity, complete M4, M7 packaging or
M8 clearance. All M0–M8 statuses remain below PASS.

### Shared-runtime compatibility rechecked read-only

The neighboring control-plane worktree still selects `tenancy-runtime-v19dj` in
its application profile; the earlier PCOMP v7 runtime/UI split is historical,
not the current selection. The selected service configuration hashes to
`5a4fa924b52239c517bbf322d6692e0e22efdad5a9e67e81cc6ff1299e185b4e`.
Its actual v19dj composed application is 1,276,296 bytes with digest
`4e7f274adf0c8201bcf7f18331227e4b280e4df49964b267927e326ef1692d76`;
the separate adapter is 53,729 bytes with digest
`700ee978d3e5c71bf21744dd75fb60b31a0205c300e34f95c082ad8ff66a7006`.

The control-plane lock still declares SIGIL
`dc1b40b1d104139f9172a4766af901e16685ef47` plus eleven patches, unlike pi's
current pin. Its adapter uses KV and single-attempt HTTP imports and a two-slot
serialized delivery protocol, not pi's current atomic-record/action contract.
Its full application cannot be admitted unchanged under pi's 64-KiB worker
source ceiling. This is a concrete integration constraint, not a reason to widen
that ceiling or claim a drop-in backend. Reuse must demonstrate a real workflow
over reviewed, versioned common mechanisms while each application retains policy.

This was source/configuration inspection only. No control-plane files or pins
were changed, no workflow or verifier was run there, and no other task was sent
work. It does not replace owner coordination, cross-project conformance or M6.

### Expanded browser accounting, outage and connection checks passed

Session **36384** completed **21 passed in 603.41s**, terminal `11d0fc`,
exit 0, observed at **22:54:44 UTC on 2026-09-08**. This combined the four
external staged cases with all seventeen existing integrated HTTP/browser cases.
The mandatory native service/store/worker, fixed evaluator and real frozen
older-host prerequisites remained included. There were no skips, xfails or
browser retry cycles in this scoped run.

Both authored aggregates matched before/after execution:

- Main: `4d3999f7497085520b53ea2c083eb767ea4e761943de3d62a3ea18ca0c146441`
  (after-check `9be320`).
- External Python/JavaScript/README stage:
  `a216370915cc91249601342fa4d480c3bcf360ef9d211cfd3c76bf76967b7e05`
  (after-check `e6ab17`).
- Tested release host:
  `391222cc03ec44d3e822090c10f9d99bce3df5d8e86bf83c1d796a6b8fb9e7ca`
  (after-check `81e5d0`); pinned SIGIL crates/stdlib remained clean.

The run used macOS arm64, Node 26.4.0 and the independently installed exact-lock
Playwright/core 1.62.1 packages under the scoped clean-install directory. This is
the first actual browser run against that package copy. The existing local Chromium
cache was reused. It is not clean supported-Linux-host installation or browser
binary provenance evidence.

The new cases independently verify:

- Unknown provider usage and reported quota exhaustion after one model request and
  an approved file action yield the exact failed operation/accounting outcome.
  Browser refresh/reconnection and host reopen retain that outcome; actual
  claim/delivery rows remain exactly the model and file attempts. Unknown usage
  retains the token hold, while the reported-within-hold outcome releases it.
  No further model request occurs, and the other tenant cannot read the operation.
- A completed turn survives a real service process stop/reopen at the same origin.
  The browser observes a genuine connection-refused read, keeps the previous
  committed answer with an outage notice, then reads the same result/history
  after restart. Exactly one submission, one provider request and one retained
  claim/delivery pair prohibit replay; no cancellation request is sent.
- Actual authorized A history bytes are held while the browser disconnects and
  reconnects as B, using the same external conversation name. Releasing the held
  response leaves only B's actual result/history. Two ordinary API turns supplied
  the committed state; the test fabricated no application state or response.
  Disconnect is allowed to abort its old read. This is browser connection
  isolation, not server revocation or credential-rotation qualification.

Six actual screenshots from `pytest-338` were inspected: both accounting stops,
unknown-usage reopen, actual outage, restored service and old-response release.
The initial accounting screenshots contain terminal failure plus prior revision-1
history while refresh is pending; the reopened unknown-usage screenshot contains
revision-4 failed history with model request and tool result. Outage/restoration
show the same committed answer with truthful connection feedback. The connection
image shows B's canary and no A content. These images are fixture evidence, not
independent onboarding or real-model scores.

The six tested files were added to the main `tests/` directory after the terminal
result and fingerprint checks, with only the accounting module's introductory
docstring adjusted for integration. The mandatory module-syntax guard now also
names the three new browser drivers. README collection accounting is updated
to **2,268 tests plus one declared research xfail (2,269 total)**. No production
application, native host, fixture prerequisites, limits, grants or policy changed.
Current-source tooling/collection and the integrated browser rerun must be recorded
separately; the earlier 2,265-case full-source pass excludes these four additions.

Admission-time quota refusal, prospective/cumulative metering, expiry during work,
server credential rotation, history conflicts/tombstones, preclaim cancellation
and the full fault matrix remain open. So do all eleven legacy route migrations,
real-model/usefulness and onboarding qualification, M6's real shared consumer,
packaging, supported-Linux evidence and independent pilot clearance. All M0–M8
statuses remain below PASS; no real provider, hosted CI, release or deployment ran.

After integration, whole-repo F/E9 lint and whitespace checks passed, followed by
**8 tooling/count guards in 1.27s** (`0d8ace`), including the 63 JavaScript unit
checks and syntax checks for every browser module. Ordinary main collection is
**2,269 tests** (`baf800`, 0.46s); the normal integrated browser selection collects
**21** (`629503`, 0.09s) without external fixture-loading flags. This establishes
tooling/collection only. The actual integrated browser rerun and eventual expanded
full-source qualification remain required, with no test prerequisites bypassed.

### Expanded 2,269-case integrated source gate passed (2026-09-09 UTC)

The prior running gate, session **48313**, reached terminal **CI PASS**, exit 0,
`d7d29b`, observed at **00:07:32 UTC**. It began at 22:58:47 UTC on 2026-09-08.
The ordinary whole-source selection was 2,269 cases, including all 21 integrated
HTTP/browser cases. Pinned toolchain rebuild, generated source/compile check,
whole-tree lint, mandatory native/evaluator/older-host and browser prerequisites,
full tests and critical-boundary coverage checks remained required.

Authored inputs matched before and after execution:
`4640794b3331ec2eacb675a7ac5dbf752b3e8fbf1e7076dc7110e55f22d761e2`
(final check `4f6aa1`, before these documentation updates). Line coverage was
**3,880/4,255 (91.19%)**; branch coverage was **1,231/1,434 (85.84%)**.
The run retained the strict research-only xfail. The Linux-only LD_PRELOAD
rename-observation fixture does not execute on this macOS host and remains
required on Linux. One non-fatal unclosed-SQLite ResourceWarning was reported;
it is not presented as a warning-free run.

The command used the exact clean pinned SIGIL checkout
`/private/tmp/sigil-pi-pinned-8277a1d9.9NXtMw`, no forge/toolchain/stdlib overrides,
`PI_REQUIRE_TOOLCHAIN=1`, offline Cargo and two build jobs. The fresh exact-lock
Playwright installation was selected through
`PI_PLAYWRIGHT_DIR=/private/tmp/sigil-pi-browser-clean-install.LDlIoWgu/node_modules/playwright`.
macOS arm64 / Node 26.4.0 and the existing Chromium cache are local source-test
conditions, not clean supported-Linux or independently admitted browser evidence.
Pinned compiler/stdlib remained clean after execution (`de2c8d`).

The tested native release host remained
`391222cc03ec44d3e822090c10f9d99bce3df5d8e86bf83c1d796a6b8fb9e7ca`;
the fixed evaluator remained
`97fdf79c1c5b57039788c38a809e708f7e037a3ab3a961c5c0fb73f069e081dd`.
Store/worker and frozen-v4 binary digests also matched their preceding run.
No runtime pin, grant, deadline, source/fuel/memory ceiling or required check changed.

Seven actual screenshots from `pytest-340` were inspected: model/tool/final
response with markup rendered as text; mobile reopened history containing the
same API follow-up; unknown-usage and quota-exhausted terminal failures; actual
service outage and restoration; and tenant-B content after releasing A's stale
response. The mobile capture still has a pending status refresh while showing
the retained revision-8 history; the accounting captures show terminal failure
while prior revision-1 history refreshes. They are not falsely described as fully
idle snapshots. The outage/restoration images retain the same committed answer,
and the released-response image contains B's canary, not A's.

During the gate, HTTP metadata work was prepared only in an external stage.
It introduces a proposed explicit v7/AH5/HC5 profile: native captures fixed
request-header facts and emits bounded, explicitly admitted response metadata;
SIGIL owns request-ID interpretation and application response choices. It changes
no main production route. That stage has 154 collected checks, of which 37
reference/build checks have passed. Its actual SIGIL/native/HTTP/browser tests
remain unexecuted and cannot inherit this source-gate pass. All eleven legacy
route migrations, body/log correlation parity and every remaining M0–M8
criterion are still required. No readiness gate is promoted to PASS.

### HTTP-profile compiled component checks (2026-09-09 UTC)

The external v7 draft remains unintegrated. Its first actual run passed all 52
standalone request-ID cases, then exposed T001 in the composed wrapper. A first
correction was insufficient: a second run again passed those 52 cases but exposed
the mismatch at the preceding wrapper call. Both runs stopped before native or
browser verification and are recorded as implementation failures, not safety passes.

The compiler showed that metadata validation establishes an Internal control
context. The isolated composition now strengthens both internal API callees'
pointer/length parameter labels to Internal, matching their existing result label;
it introduces no declassification, proof bypass, capability or limit change. The
actual 29-case wrapper selection subsequently passed in 30.76s (`4ad9f4`, session
60455, observed 00:17:51 UTC). Those checks compare the new compiled wrapper with
the unchanged v6 SIGIL API: exact bodies/statuses, non-reply commands, time guards,
bootstrap and precise traps for invalid facts/inventories. The corrected entry
is 63,651 bytes, below the original 65,536-byte ceiling.

This focused run lacks a separately captured pre-run aggregate and is not claimed
as candidate-bound qualification. The complete 154-case staged run, including
all native/evaluator/older-host prerequisites and the existing 21 HTTP/browser
cases retargeted without assertion changes, still must pass. No main production
route changed; full legacy API parity and M0–M8 remain open.

### HTTP profile staged verification and main integration (2026-09-09 UTC)

The next staged run (session 91321) passed 120 checks, then failed a new HTTP
fixture that incorrectly expected 64 correlation hints plus four fixed headers
to fit the existing 64-total-header limit. Both source aggregates matched after
that terminal failure (`16ef3a`, 120 passed / 1 failed in 395.36s). The test was
corrected to accept 60 hints and require exact 431 refusals at 61, 64 and 65 hints.
The separate RF1 parser still tests its own 64-hint bound. No runtime limit or
product behavior was changed to satisfy the test.

The complete corrected **154-case staged run passed in 751.21s**, session 64829,
terminal `e38115`, exit 0. It began at 00:35:54 UTC and its terminal pass was
observed before the 00:49:49 UTC fingerprint check. It includes 81 compiled SIGIL,
37 reference/build, 15 actual HTTP/native-service and all 21 original HTTP/browser
cases retargeted to explicit v7. Original native store/worker/service/fixed-evaluator
and older-host prerequisites, plus the staged full native fmt/Clippy/test/release
build, remained mandatory. No concurrent timing-heavy run or browser retry occurred.

The before/after main fingerprint was
`f5616da33a815879ae1ca92ba3acc87da14932d883e743c409ffc6dae3efad96`;
the staged authored fingerprint was
`61d02b06863ffbe9c98292445e61b5f8d95d9a7ebf6c03b491ebc7cda815a02e`.
Both matched after termination (`45b9ee`, `fecbb8`). The staged release host was
`96035ad2f9b8a739a110b84077bb0f545460fd4d5d2aebeb83e95cbb94979da2`;
the actually older main v6 host was
`391222cc03ec44d3e822090c10f9d99bce3df5d8e86bf83c1d796a6b8fb9e7ca`.
This is local macOS arm64, pinned SIGIL and local mock-provider evidence, not
Linux/candidate qualification, real-model usefulness or pilot clearance.

Eight actual screenshots from the run's `pytest-346` temporary directory were
inspected: initial controls, both accounting stops, model/tool/final transcript,
mobile reopen, stale-tenant-response rejection, and outage/restoration. The mobile
capture contains revision-8 history including the direct-API answer while status
reload is still pending. Accounting captures show terminal failure alongside older
revision-1 history awaiting refresh. These transient screenshots do not establish
convergence of every screen state or the broader M4 matrix.

After termination and fingerprint comparison, the tested SIGIL sources, build
recipe, native HTTP mechanism and all 154 cases were integrated into normal repo
locations. Test deployment adapters now use the main native fixtures, which retain
the original mandatory native/evaluator gates. Old v6 browser tests remain in the
ordinary selection; the new profile repeats their unchanged functions and drivers.
No test imports a temporary staging path.

Before changing main native sources, all 36 v6 store/worker/service source and
lockfiles were copied byte-for-byte into a separately identified compatibility
fixture. Aggregate:
`afb5134fe14255f55c80df446697de2e3e5d664ec6c7c0170a8e930f50ff3e2f`.
Inventory/content checks passed (`72a0f8`). The integrated fixture validates its
manifest before rebuilding; it retains full native gates and refuses source,
lockfile, missing-file, extra-source and symlink drift. This preserves actual
older-executable comparison instead of substituting the newly upgraded host.
It is test data, not a second production runtime; the frozen v4 fixture is unchanged.

The relocated application remains exactly **63,651 compiler-input bytes**, digest
`4261e3bcb64f0b7b6f80f5e0226336cc6ff5d3c020457b4af4724bd8f327e289`.
The original API/discovery digests are unchanged. The first integrated static
selection passed all 43 reference/build/fixture cases (`93d43b`); full-tree Python
lint and native formatting passed. Collection now has **2,429 cases** (`a3d4b2`),
including six new immutable-fixture checks. The full integrated source gate must
still pass; a staged pass is not inherited by the relocated integration.

[HTTP metadata scope](http-exchange.md) remains narrow: request-header correlation
only. Body/log correlation parity, authentication-error distinctions, request-rate
admission, all eleven legacy routes and all remaining M0–M8 criteria stay open.
No readiness row is promoted to PASS, and no spending, deployment, cross-repository
edit or independent-review request was made by this work.

### Expanded HTTP integration source gate passed (2026-09-09 UTC)

The unchanged **2,429-case whole-source selection passed** in session 37117,
terminal `a9bc5c`, exit 0, observed **02:17:31 UTC**. The run began at 00:59:52 UTC.
Pin/rebuild, generated artifact/compile checks, lint, full native/evaluator and
older-host prerequisites, the whole test tree and independent coverage gate all
remained required. Line coverage was **91.19%**, branch coverage **85.84%**.
This supersedes the integrated-source-pending statement at the preceding HTTP
checkpoint, not any missing candidate or product-compatibility evidence.

The authored-source aggregate matched before/after:
`90c3d1574dc4902ce74f109d2987f2e50ce86211d57ad68e35977a1fab4b1c2a`
(`4b0b94` before, `c28df8` after terminal). No main input changed during the run,
no second timing-heavy job ran alongside it, and it was not restarted because of
an observation timeout. Documentation was updated only after terminal success
and the fingerprint comparison.

The run retained its platform skip and declared strict research xfail. Source
inspection identifies the Linux-only LD_PRELOAD rename-observer case and the
existing interprocedural region-analysis research limitation; neither qualifies
a missing Linux or migrated-product boundary. A nonfatal `ResourceWarning` for an
unclosed SQLite connection was reported during
`test_axi_tools.py::test_npm_shape_dep_walker_matches_reference`; this is not a
warning-free result. The terminal output reports the coverage gate and CI PASS,
not 2,429 ordinary test passes.

This checkpoint remains local macOS arm64, pinned SIGIL, mocked local providers
and source-level evidence. It does not establish protected Linux CI, an immutable
package, full API migration, real-model usefulness, independent onboarding,
control-plane reuse, independent review or external-pilot approval. All M0–M8
rows remain unqualified.

While this run was frozen, request-admission work stayed in an external stage.
It contains a bounded native read-set draft, a SIGIL observation codec and SIGIL
request-window accounting. The combined lightweight preflight passed 48 reference/
build checks (`08c44f`); 94 SIGIL execution cases and 16 new native tests remain
unexecuted. No new host command, credential/time-fact ABI, API request limit or
legacy route was integrated. Those drafts cannot inherit this source-gate pass.

### Request-accounting components passed in isolation (2026-09-09 UTC)

After the main gate terminated and its input hash matched, a separate frozen
stage completed all original native store/worker/service formatting, Clippy,
tests and builds. Session 59679 ended with PASS (`c75fb0`, exit 0, observed
02:22:42 UTC): **169 native tests passed**, including sixteen new read-set tests,
with no ignored/filtered test and the original two-job/240-second command limits.
The optimized host build also completed. No heavy job overlapped the main gate.

The complete staged selection then passed **142 cases in 34.36s**, session 3214,
terminal `10d41f`, exit 0, observed 02:26:16 UTC. It includes the prior 48 reference/
build cases, 47 compiled SIGIL read-set cases, and 47 SIGIL request-accounting
cases. Two of the latter apply actual SIGIL proposals to a real native store,
checking restart/tenant separation and stale-update rejection; original native
fixture gates remain required. This supersedes only the staged component-pending
statement above, not any HTTP migration or MVP acceptance requirement.

The stage's before/after digest was
`dfc51cbb5270e76e52cdee63592bea113455ffc0bb3c18c0e57b9847acceec9b`;
main, after its documentation-only source-result update, remained
`e94654ba4cd2c27659db658e44b78827cf71926a32b2f642aae7d41e2eec84d6`
(`26094e`, `070b4c`). The new native mechanism performs an exact scoped read set;
SIGIL validates expected coordinates and produces conditional request-window
updates. Numeric limits are explicit test inputs, not a chosen pilot allowance.
A draft refusal on backward-window movement is explicitly different from the
legacy oracle and still needs M0 review; no trusted-clock claim is made.

The code remains isolated: no new command/profile, native credential/time-fact
observation or HTTP request-rate path has been integrated. Binding caller identity,
committing accounting before route errors, preserving the eight-step follow-up,
readiness fallback, body/log correlation and all eleven legacy routes remain open.
This is local component evidence, not a candidate, independent review or M0–M8 pass.

### Versioned request facts and grouped reads passed in isolation (2026-09-09 UTC)

The same external request-admission stage now implements an explicit v8 native
profile, AH6/HC6 facts and a bounded `read_many` command. It remains outside the
main implementation at `/private/tmp/sigil-pi-request-admission.tbXy5mXp`.
SIGIL still owns credential validity, permissions, allowances and all route/domain
decisions. The new host supplies only an exact-prefix presentation fact, a
subsecond flag from the same sample as its existing whole-second clock, and actual
scoped transactional reads. Tokens do not enter guest input. The existing source,
fuel, deadline, memory and eight-entry-step ceilings are not widened.

Complete staged native checks passed **179 tests** (54 store, 20 worker, 105
service), including ten new version/fact/action tests while retaining the prior
sixteen read-set cases. Formatting, Clippy all-targets with warnings denied and
debug/optimized builds passed in session 1262 (`9bc2f0`, exit 0, observed
02:41:08 UTC). Before/after stage fingerprint matched:
`9fa1cc98015cd639fdc2e195a427d13037413eb526a3a7990e0eb85a0824904d`.
The v7 bundle shape and original protocol assertions remain covered; this is not
the full old-profile upgrade qualification required before integration.

The expanded complete selection passed **148 cases in 346.82 seconds**, session
53626, terminal `ccf120`, exit 0, observed 02:58:59 UTC. All 142 previous cases and
all original native/fixed-evaluator prerequisites remain required. Six added cases
include real HTTP-to-compiled-SIGIL fact/read checks, exact response/guard refusals,
real v7 rejection of v8 before creating state, a build check, and two unchanged v7
boundary tests retargeted to the new executable. The owner-installed test artifact
is 64,182 bytes; it is explicitly not the product API and must not be deployed.
The HTTP cases leave domain state unchanged and make no provider requests.

An earlier attempt stopped with 94 passes and a loopback bind denied by the
sandbox (`47e46c`); it is not counted as a pass. The successful run used explicit
local-server permission and reran the unchanged suite, without concurrent heavy
jobs or bypassed prerequisites. Before/after source hashes match (`a78d32`,
`979a1d`): stage
`88fb666090b533a5f1ea64eb1e12975208885bea00db30b6e1d41dd5fed86408`,
main `3574eb80682b8674225c46fb96fcf5f3b9e10b261277c2e40b51a892824de874`.
Staged optimized-host SHA-256:
`9ed6d28096e4af1dc471088273a1d3fab2f7ef404a7b6bdd71993990d6633a9d`.
This result note was written only after terminal success and input comparison.

Actual SIGIL request-rate admission is still not connected to the product API.
The real accounting commit must precede route errors/domain work; full follow-ups
must still fit eight steps. Numeric/clock policy, grouped-read error precedence,
header normalization, emergency readiness, body/log correlation, browser quota
handling, the complete route matrix and old-host fixtures remain required. No
main integration, external deployment, model spending or cross-project action
was performed. This checkpoint is local macOS mechanism evidence, not a candidate
or a PASS for any M0–M8 gate.

The next isolated component, `app/pi/request_policy.sigil`, then passed **44
focused SIGIL tests in 13.35 seconds** (`6e0c33`, session 98422, exit 0). It combines
request-window accounting and the unchanged submission decoder in one pure call,
returning either a prepared body or a deferred 400/413 alongside the accounting
proposal. Route/permission handling is not replaced by a submission error for
other routes or missing chat scope. Nonadmission produces no body plan or write.
The compiled input is 37,066 bytes, SHA-256
`0209e73f45b4911ef400474094c6768389810d9260feab569408bb3dd4fd7c9f`.
This is subsequent focused evidence, not part of the preceding 148-case pass.
The real HTTP entry still must bind these inputs, validate the actual accounting
commit receipt before using the plan, and prove the complete eight-step follow-up.
No actual request-rate enforcement, route migration or readiness gate is claimed.

### Actual request-rate admission and lifecycle passed in isolation (2026-09-09 UTC)

The v8 request-admission stage at
`/private/tmp/sigil-pi-request-admission.tbXy5mXp` now connects the real SIGIL
`request_policy` to its SIGIL HTTP entry. Matched, active credentials first read
their tenant's request window. SIGIL computes an admission proposal; the entry
requires an actual successful native commit receipt before releasing a prepared
submission, a deferred body error, or the domain route. Missing/invalid credentials
do not read or write accounting. Exhaustion produces 429 with bounded Retry-After
and no additional accounting or effect. The host does not decide allowances or
route policy, and the request limit is an explicit artifact-bound build input,
not a default or approved pilot allowance.

SIGIL now requests deduplication, session and active-budget observations as one
bounded scoped read set, validates its complete coordinates and framing, and
interprets any previous-operation reference itself. The actual follow-up path
still fits eight entry evaluations. No source, memory, fuel, deadline, store,
grant, command-size or evaluator-retirement ceiling was increased.

The entry is 63,349 bytes at fixture limit 2, SHA-256
`1d32fdafea96a37900b9302b6ba4fdbecb56019dc2bd7c98bee6dd82e9591824`,
from twenty hash-bound inputs. The maximum signed-64-bit limit also fits at
63,367 bytes. The build-only layout transformation removes permitted whitespace,
not compiler tokens or quoted contents. Tests compare the complete before/after
token streams and literal values using the exact pinned compiler's real lexer,
for both limit values, and include a changed-literal negative control. The
70,119-byte layout provenance is test data, not an admitted executable or source-
limit exception. Compilation/verification of the final input remains mandatory.

The complete 244-case staged selection passed in 461.30 seconds (`a92d25`, session
47899, exit 0, observed 05:20:59 UTC), retaining all previous component, native,
fixed-evaluator and unchanged-v7 boundary prerequisites. Before/after inputs
matched: stage
`fb849ea41dc6f6c814d58378bfc908cae595eee2bf4113d46429b9f1e0033f2a`,
main `fc6ef815155bdccc368efae7a775b027b935e64369efd8504f60ba22bae2264c`
(`ac1388`, `18cd83`). This first whole-stage result includes the preceding
44-policy and 45-entry/layout focused cases. A separate lint check found eight
intentional imported tests missing explicit export declarations; the declarations
were added without deleting imports, tests or assertions.

Two further real-HTTP cases and stronger retained-lifecycle assertions then passed
in the complete **246-case selection, 517.85 seconds**, terminal `a1a51b`, session
49785, exit 0, observed 05:33:33 UTC. The full original native/compiler and staged
formatting, Clippy, native-test and build prerequisites remained required. Python
F/E9 lint also passed before and after this run. There were no concurrent heavy
jobs; both runs used loopback-only app/mock-provider servers, with no external
provider, runtime-pin change or deployment.

The real API evidence covers accounting before unknown-route/body/scope errors,
exhaustion without mutation, persisted counts across restart and same-tenant
credential rotation, separate tenant quotas, model/file/model response and
follow-up, retained operation results/history/discovery, replay/conflict, and
terminal cancellation. All eleven legacy routes plus the five additive route
surfaces reject missing scope before parsing malformed bodies, committing only
the request charge; this is refusal coverage, not successful legacy-route parity.
A hanging local provider observes one actual request before cancellation. The
SIGIL application records unknown usage and an uncertain result, preserves it
across restart, denies another tenant access, and does not resend the request.
Read/replay/terminal-control checks require exact accounting changes while keeping all
non-accounting rows and effect counts unchanged. Older v7 whole-state assertions
remain unchanged in their original tests.

The final run's before/after stage hash matched
`41df556be1858b055b11c97f32ca15fd2fe6fc64f15d1fb15cee26d62a33fb24`;
main retained the hash above until this evidence-only documentation update
(`ffef3d`, `df2c76`). The optimized staged host remains SHA-256
`9ed6d28096e4af1dc471088273a1d3fab2f7ef404a7b6bdd71993990d6633a9d`.
This is local macOS arm64 development evidence, not protected Linux CI,
immutable packaging, a browser-v8 run, a real-model benchmark, independent review,
control-plane reuse or candidate qualification. No M0–M8 row becomes PASS.

Next: preserve the actual current v7 sources as an immutable compatibility fixture
and prove old-profile reopening/rejection before integrating the additive v8 path.
Full grouped-read failure precedence, header normalization, emergency readiness,
request-ID body/log parity, all eleven successful legacy routes, browser quota/
expiry behavior, other fault boundaries and the full MVP gates remain open.
Backward-clock behavior remains an explicit draft compatibility difference needing
M0 review; model, numeric pilot limits and cross-project approvals remain unchosen.

### Frozen-v7 compatibility and v8 development integration (2026-09-09 UTC)

The preceding turn was progress: 244 then 246 staged checks passed and their exact
inputs were recorded. The current turn first revalidated main and stage hashes,
then froze the actual forty-file v7 native source without changing product code.
The snapshot aggregate is
`1d8a89ed9c5eaa3457c80a7a82e2c26e934d4088fd441538005ce83626538178`.
All copied bytes matched the original files; seven snapshot/drift checks and lint
passed (`fd1fc4`). The new fixture retains original formatting, Clippy, native
test/build and pinned evaluator prerequisites; it is not a version-check stub.

The expanded isolated selection passed **248 tests in 668.38 seconds**, session
65008, terminal `a59b7e`, exit 0, observed 05:54:08 UTC. The frozen v7 executable
rejects a real v8 configuration before state creation and when asked to open a
populated store. An unchanged-v7-profile round trip creates a real tool turn on
the old host, reads/replays and creates a follow-up on the new host, then reopens
both results/history/discovery on the old host. Request correlation, tenant
boundaries, exact read/replay state and effect counts are preserved. This is old-
profile host compatibility, not an in-flight v7-to-v8 application-bundle migration.

Before/after source hashes matched (`758b5d`, `93218d`): stage
`ccb5560be3429a9b91936a0494f0713d8c76f46c624bd7afb5c124237b71e6ea`,
main `1f50a8d9f1b122f7c41930eb4be0b008d573a51542e4f97c3d3d6b4223503d73`.
The frozen v7 optimized executable is SHA-256
`7d8acc679db6246220bac9e878c98616b53724d96cb6baf5a7a565ec3310f619`.
Only one heavy job ran at a time, using loopback app/mock-provider permission.
No external provider, deployment, runtime-pin change or cross-project action ran.

After terminal success and input comparison, forty-one reviewed application,
native, build and test files were integrated and byte-checked (`955ce5`). The
entry/policy/layout recipes were moved out of test helpers into `scripts/`;
thin fixture wrappers call those actual recipes. Ten integrated composition and
snapshot checks passed and repository-wide F/E9 lint passed (`d30e20`). Both
compiler-input digests and sizes match the staged programs; the old v7 compiler
input remains unchanged. Collection is **2,687 cases** (`006635`), including the
existing research xfail; collection is not execution or a full-source pass.

The expanded integrated `./ci.sh` still must pass. This update records development
integration, not protected Linux CI, packaging, browser-v8 qualification, live-model
usefulness, control-plane reuse, independent review or any M0–M8 PASS. The remaining
boundaries in the preceding checkpoint and the full goal continue to apply.

### Integrated request-admission source gate and browser expansion (2026-09-09 UTC)

The complete **2,687-case integrated `./ci.sh` passed**, session 62114, terminal
evidence `ff81ed`, observed 07:27:54 UTC. Toolchain pin/rebuild, generated compile,
lint, all required source/native/browser checks and coverage gates passed. Coverage
was **91.23% line / 85.91% branch**. The existing Linux-only skip and strict research
xfail remain; a nonfatal unclosed-SQLite ResourceWarning was emitted. Before/after
main aggregate was `15bb556eda9efd2a579b2ca9583063a4bc4659c7647e273c6f6eb7ea9b260258`
(`3c2aa4`). This supersedes the preceding 2,687-case source-pending statement, not
Linux, protected CI, packaging or any full MVP gate.

The separate browser stage at `/private/tmp/sigil-pi-request-browser.OM7sPk9v`
first passed 95 client/controller checks (`c912a3`). Its initial full run then
finished **2 failed, 13 passed in 678.17 seconds**, session 16144, `d527c9`.
The accepted-operation quota and real-expiry driver asserted response counts while
post-navigation reads were still pending. Traces showed five requests/three completed
responses and four requests/three responses respectively; saved screenshots showed
correctly rendered content with read controls still disabled. The failure archive
is `/private/tmp/sigil-pi-browser-failure-evidence.wiGCxw8z`.

After that run terminated, the driver was changed to wait for actual controller
read completion instead of a previously fired navigation `networkidle` event.
All exact response counts, accounting/state/effect assertions, 75-second real
credential expiry, 35-second UI observation bound and host limits were retained.
No application/controller policy, native mechanism, grant or deadline changed.

The complete rerun passed **15 tests in 696.09 seconds**, session 71475, terminal
`b99192`, 07:53:59 UTC: fourteen real-browser cases plus the fixture-sharing check.
This includes all four new quota/expiry scenarios and ten original-driver v8
lifecycle/boundary cases, retaining native, worker, store, fixed-evaluator and
locked Chromium prerequisites. Quota-limited status reads retain the accepted ID
without inventing completion, while independent read-only store observations prove
the actual model/file/model turn completes with exactly three claim/delivery pairs
and two local-provider calls. Expiry clears a real retained conversation; the
expired request does not charge accounting and a second tenant cannot see prior
content. No automatic request is introduced by Retry-After guidance.

Before/after browser aggregate was
`af8b1ee84f993df42b7149f2a345e1ce445dc88646267147e67e2290cd56a2c0`
(`d401d2`, `4fd2ab`); main retained the 15bb556e aggregate above (`6b968a`, `3d69ad`).
Actual optimized native-host SHA-256:
`d49c8e5b6f198d1d2d296eff08f5b2c2125526e3487d3bf7c763da93d89d62b9`.
Quota-accepted, retained, expired-cleared and second-tenant screenshots were visually
inspected. Copies and terminal output are retained at
`/private/tmp/sigil-pi-browser-pass-evidence.67UeUovl`. Other archived images are not
by themselves visual-review evidence. Browser page errors and foreign requests were
zero; expected HTTP 401/429 resource errors are not described as absent.

Eight browser/client/test files were then integrated and compared byte for byte
with the passing stage (`83d081`). Redundant conftest fixture re-exports were removed
from `test_request_host.py`, retaining all test functions and root prerequisites.
The actual setup-only plan over automatic-service, request-host and both new browser
modules selected 56 cases and registered each native/browser prerequisite once
(`e51b2a`); it did not execute or bypass those prerequisites. Integrated client/tooling
checks passed (`ac8f1a`) and repository-wide F/E9 lint passed (`5a8531`).

The expanded **2,702-case full-source gate still must run**. All 21 preceding HTTP/
browser cases remain required alongside the fourteen added v8 browser cases. This
is local macOS arm64, pinned SIGIL, Node 26.4.0, locked Playwright 1.62.1 and scripted
local-provider evidence, not real-model usefulness, Linux/fault/load/candidate
qualification, shared control-plane execution, independent review or pilot clearance.

A separate, still-unintegrated readiness projection at
`/private/tmp/sigil-pi-readiness-policy.85uU1fZF` passed **62 checks in 60.61 seconds**
(`ef064f`), including 50 compiled-SIGIL tests against the existing ProductService
reference. Its 25,500-byte compiler input has SHA-256
`8ee36c471b96ba9d0ea13047776b11320b942554e15c8b607ef87dcccdc3575b`; stage aggregate
`48d083e6f0a44c23add152f25d72a240ab0b65ce1886881b1ac43cc5e34df872` and main inputs
matched before/after (`e70652`, `410a26`, `071e94`). This is pure readiness decision/
projection evidence only. The actual route, authenticated emergency accounting,
real dependency probes and their host/entry binding are not implemented by that
draft and cannot inherit a readiness-endpoint pass. All eleven legacy successful
routes and every outstanding M0–M8 requirement remain in scope.

## Follow-up: expanded browser integration source gate passed

On 2026-09-09, the same full `./ci.sh` process (session 37072, started 08:03:21 UTC)
completed with exit 0 and **CI PASS**, terminal `2ef97c`, observed 09:32:54 UTC.
All **2,702 collected cases** completed, including the browser expansion and all
existing native/compiler prerequisites; the declared research-only xfail and the
Linux-only skip on macOS remain explicit exceptions. Coverage gates passed at
**91.23% line / 85.98% branch**. A nonfatal SQLite ResourceWarning was reported.

The before/after aggregate was identical:
`e917d3486cc2b3f33e0eeaf70113bff4d05075830f4510253d696bbead569085`
(`a61fcd`, `f48769`). The runtime pin, source/entry/fuel/deadline ceilings and host
grants were not changed to obtain the pass. Documentation updates after the run
record its result; they are not part of that exact frozen aggregate.

This supersedes the source-gate-pending statement immediately above. It is local
macOS arm64 source, native and scripted-provider browser evidence, not protected
Linux CI or candidate qualification. All M0–M8 gates remain unqualified, including
the eleven legacy route migrations, real dependency/readiness behavior, final
accounting/audit/retention, the remaining recovery/fault/load matrix, live-model
usefulness, independent onboarding, actual control-plane reuse and pilot clearance.

## Follow-up: readiness native and SIGIL components verified in staging

After the full-source gate ended, the isolated readiness stage at
`/private/tmp/sigil-pi-readiness-host.BiNfIvYV` passed all original native fmt,
locked all-target Clippy (warnings denied), tests and binary-build gates:
**87 store tests** (`7fd5a4`), **20 worker tests** (`8b97f7`) and **137 service tests**
(`af72e1`). These 244 native cases include 65 new temporary-cell, native-failure-
origin, result-codec and monotonic-clock checks. They use actual scoped Store
operations and include real missing/replaced paths, unsafe layout, SQLite write
failure, corrupt data, capacity refusal and reopening. Synthetic private-field
faults are explicitly labeled and are not actual fork/crash evidence.

The following **228-check suite passed in 108.85 seconds**, session 30786, terminal
`ad9bef`: 36 build/reference checks, 142 compiled SIGIL emergency-policy cases,
and 50 compiled SIGIL readiness-projection cases. The unchanged solver-enabled
runtime pin and 300,000,000 fuel ceiling were retained. Shared SIGIL guards can
be used before temporary reads and rechecked by the policy; the shared projection
is callable from an entry. Neither is yet wired into a production HTTP route.

The native store records whether a failure arose before storage access or from
the actual storage operation. SIGIL, not Rust, decides whether that observation
permits seeking separately bounded emergency admission. Temporary CAS receipts
remain distinct from durable commits. The clock and cells are process-bound
mechanisms, not expiry/allowance policy or a persisted operation identity.

Executed compiler inputs: emergency **35,293 bytes**, SHA-256
`176b7742c7f7e157f188d253e1438c353f550525661b921f8b5ded71b7e1a698`;
projection **25,658 bytes**, SHA-256
`73d7e2436d826aeab1d2ed95a4a37f8ee4376ce761393767f81c518ba6639d78`.
Both use stdlib hash `b5f40e2eba41b6734f8db071`. The stage source aggregate was
identical before/after the native and SIGIL runs (`d5bbe9`, `ff70bb`, `c755cc`):
`bd7ef6da718eb699829e49f67fde9001877a9fb3a5edaa5af44d331996f53dcd`.
The stage README was updated after that frozen checkpoint. F/E9 lint passed.

This does not establish an enabled host contract, authenticated HTTP fallback,
actual readiness dependency probes or a complete migrated legacy route. Policy
fixtures do not substitute for real bound observations/receipts. Integration
must retain the eight-entry-evaluation and 65,536-byte source ceilings on both
read-failure and commit-failure paths, preserve old-v8 compatibility, and cover
correlation, failure precedence, expiry, accounting uncertainty and recovery.
No native production profile or runtime pin changed; all eleven legacy route
migrations and every M0–M8 gate remain open. No pilot candidate is qualified.

## Follow-up: readiness compatibility baseline and bounded entry preparation

The readiness stage now includes the real prior v8 native host as a frozen
46-file fixture, not a version-check stub. Its exact manifest aggregate is
`b2ddb658d807c3bf0b826baf235e73004200633edb6c196d3ba985177b56d639`.
Eleven inventory/drift checks passed, followed by every original store, worker
and service fmt/Clippy/test/build gate (`aa2f80`, session 26981). New-profile
rejection, state reopening and in-flight migration remain separate unproved work.

To fit the new endpoint without increasing source or entry-evaluation limits, a
draft build-only recipe removes five exact unreferenced helper definitions.
The first version passed **56 checks in 285.52 seconds** (`2ea08d`), including the
full original compiler/evaluator prerequisite, independent pinned-parser spans,
pinned-lexer equality of all remaining tokens/literals, and all 19 existing
compiled entry-transition cases. The enabled v8 recipe and expectations are
unchanged. This was component evidence, not new readiness behavior.

The latest draft also compacts word/operator whitespace. Combined source size is
**55,994 bytes** for limit 2 and **56,012 bytes** for the maximum signed-64-bit limit,
within the existing 65,536-byte ceiling. It retains original-source provenance,
exact omitted-definition hashes and build-script hashes. **The complete changed
77-case compiler-backed run passed in 281.53 seconds** (`5ba025`, session 20618),
including six independent parser/lexer cases and all 19 compiled entry-transition
cases. Original compiler/evaluator gates ran again. The stage aggregate matched
before/after (`5e6178`, `1e67f8`):
`322ca34bb087004adbb14c56ffceaa4498218e3d2c4475e033a2607f5c09e413`.
The earlier pass was not transferred to the changed compiler input. Actual HTTP
binding, dependency probes, receipts, correlation and failure/recovery behavior
remain unimplemented in this draft. No production host profile, runtime pin,
cross-project state, deployment or readiness gate changed.

## Follow-up: explicit v9 native mechanism binding in staging

The isolated readiness stage now binds its tested mechanisms into the native
Engine and action dispatcher under an explicit draft v9 profile. It requires
automatic execution, HTTP metadata and non-null process configuration. Declared
temporary namespaces require existing configured write authority and must fit
the unchanged independent ceilings; every dispatched operation still requires
the matched credential's real scope. Configuration and lifetime/limit contracts
are bundle-bound. No application fallback or allowance rule is implemented in Rust.

AH7/HC7 has 18 fields, with an actual process-local MC1 clock observation at index
17 (empty during bootstrap). One clock/cell instance is created after SIGIL
bootstrap and Store admission and retained by the Engine. Guarded commands
`read_observed`, `commit_observed`, `temporary_read` and `temporary_write` emit
distinct DR2/DC2/VR1/VC1 observations. Existing profiles retain their old formats
and reject these commands; guard/capability/protocol failures do not become outages.

All original native fmt, locked all-target Clippy, tests and binary builds passed:
**87 store + 20 worker + 150 service = 257 tests** (`ae6d60`, session 55000).
Thirteen new tests cover configuration/manifest boundaries and actual dispatcher
operations, missing paths, unsafe layout, scope denial, stale writes, expiry,
deadlines and temporary clock/state lifetime. Unit clocks and reconstructed
lifetimes are explicitly not actual HTTP expiry, process-crash or restart proof.
Before/after native-gate aggregate (`8fa5ad`, `3a4b68`):
`f4a30dd55ee94254f298f4fc2ed7c1523c196ba8deb3c2b2836ce754c939e77d`.
The frozen 46-file old-v8 fixture remains manifest-valid; it was not refreshed.

This code remains outside main. A real v9 SIGIL bootstrap/entry, readiness probes,
normal/emergency HTTP behavior and old/new compatibility/recovery tests still
need integration. Source limits, eight entry evaluations and runtime pin remain
unchanged. Every M0–M8 gate and all eleven legacy route migrations remain open.

## Follow-up: real scoped storage inspection through the staged v9 host

The readiness stage now implements `inspect_storage` under its explicit v9
contract. It takes at most eight namespaces, requires their actual held read
authority before touching storage, and inspects one read transaction. It checks
the admitted private files, SQLite identity/schema/quick-check, frozen capacity,
and bounded record encoding/digests inside the requested namespaces. Shared
physical-store checks do not return other tenants' record contents. No writes,
new scopes, reservations or durable receipts are created by inspection.

`SI1` contains only status, a finite error label and origin. Actual scope/input
failures remain `precheck`; storage failures remain `storage`; deadline and
SQLite-work exhaustion are `execution`, not fabricated corruption. The independent
one-second and approximately eight-million SQLite progress-step budgets cannot
extend a caller deadline. They are cooperative limits, not a promise to interrupt
kernel filesystem calls. A late or partially completed inspection cannot succeed.
Success is not future write capacity, audit verification, retention completion,
application readiness, or an admission receipt. SIGIL must interpret these facts.
The pinned rusqlite hooks feature is enabled; all three dependency locks and the
SIGIL pin remain unchanged.

The first full conformance run passed native prerequisites but failed all three
HTTP cases at bootstrap (`dede84`, 1 passed / 3 failed in 304.59 seconds). Direct
diagnosis against the pinned compiler identified T042: the new test-only SIGIL
artifact reassigned an immutable local. That declaration and a separate incorrect
test restart command were corrected; no production limit or test requirement was
relaxed. The complete rerun passed **4 tests in 283.19 seconds**, session 98837,
terminal `f8d99a`, observed 2026-09-09 11:03:55 UTC.

The run retained all original store/worker/service fmt, locked all-target Clippy,
test and binary-build prerequisites (**103 + 20 + 155 = 278 native tests**), the
full original pinned fixed-evaluator gate, and the optimized service build.
Sixteen new store tests and five new service tests cover actual outages, locks,
corruption, scoped digest checks, no mutation, bounded queries, real SQLite
deadline/work interruption and subsequent usable storage. Existing action tests
also exercise the new command's guard, capability and old-profile refusals.

The three actual HTTP cases use an owner-installed, explicitly test-only SIGIL
artifact, preserve full bootstrap/profile validation, and pass observations from
the real dispatcher to SIGIL. They verify forged body observations have no effect,
cross-scope refusal, real path-loss/restoration, a real exclusive SQLite lock,
actual schema damage/restoration, unchanged durable rows and native MC1 continuity.
An actual process restart gets a new clock domain and reopens unchanged storage.
No model request was made. This does not qualify emergency-cell restart semantics,
the product's authorization policy, a readiness route, or old/new host migration.

The executed probe is 59,551 bytes, SHA-256
`8688ce2db9fc5a72711ea8f54466690cefa8e65b044a904393a1362c8f612f6f`,
with 19 bound source/recipe inputs and the unchanged stdlib fingerprint.
Before/after source aggregate (`fc8943`, `adfc29`):
`a46d848f5e1ae391cf6c5a08d4ef22bcef06f0e5a3aa11825593d63725bed266`.
F/E9 lint passed (`331dff`), and all eleven frozen-v8 inventory checks passed
(`999bcd`); the old fixture was not refreshed. These are local macOS arm64 results,
not protected Linux CI, a package, independent security review or pilot evidence.

Implementation remains outside main in the same isolated readiness stage. Next is
the production SIGIL v9 entry: real dependency observations, successful normal or
emergency admission receipts, exact readiness behavior and compatibility, within
the unchanged source, fuel, memory, grant and eight-evaluation ceilings. Audit,
retention, schedule and runtime health must not be inferred from this inspection.
No M0–M8 gate is PASS, and all eleven legacy route migrations remain in scope.

## Follow-up: combined storage/runtime inspection through actual SIGIL HTTP

The isolated readiness stage adds a guarded v9-only `inspect_execution` action.
It performs the existing scoped storage inspection first. A native precheck
failure prevents any runtime inspection; a storage-origin failure still permits
an independent runtime observation under the already checked scope. `EI1` carries
separate `SI1` storage and `RI1` runtime frames, not a native readiness verdict.
This fits both observations into one entry step without increasing the eight-step
ceiling or moving emergency allowance/authorization policy into Rust.

The runtime registry derives bindings from the actual admitted entry, functions,
coordinators, effects, dispatch policies, recorders and transaction producers.
Each inspection revalidates distinct pinned executable files and current available
owner prechecks. In-flight or uncollected effect ownership is reported separately;
inspection does not join, cancel, poll for delivery, retry, spawn or consume calls.
A retired but usable pure owner is not silently classified as failed. Lost owners,
unconfirmed cleanup and wrong-process ownership cannot produce successful owner
observations. In-flight owners are not falsely claimed to have passed idle checks.

The new runtime portion is independently capped at one second, 64 distinct files,
256 MiB per file and 512 MiB total, with a 16 KiB streaming buffer. Identical admitted
path/hash bindings are coalesced only inside one inspection; there is no cross-request
result cache. Reads check time between chunks and cannot succeed late. These remain
cooperative checks, not kernel-I/O interruption or future spawn/effect guarantees.
No filename, source, grant, secret or raw diagnostic enters `RI1`. Existing profile
inventories and the SIGIL/dependency pins are unchanged.

The complete run passed **6 tests in 298.77 seconds**, session 73933, terminal
`5eb30e`, observed 2026-09-09 11:30:20 UTC. It retained all original native
fmt/Clippy/test/build prerequisites (**103 store + 22 worker + 168 service = 293
native tests**), the complete original fixed-evaluator gate and optimized service
build. Fifteen additional native cases cover registry traversal/bounds, changed or
missing artifacts, real modes/symlink/FIFO refusal, owner observations, scoped action
binding and inspection during an actual native effect flight without interference.
macOS stripped the requested set-ID bits in one initial fixture; real mode tests
now assert the actual installed modes, and privileged-bit refusal is separately
tested against the numeric predicate. No privileged-file execution is claimed.

All prior storage/clock HTTP cases were rerun. Two added actual SIGIL/native HTTP
cases establish independent storage/runtime failures, skipped runtime inspection
after scope denial, and actual damage/restoration of a private effect-runtime copy.
The shared pinned runtime was untouched, durable rows were unchanged, and the
local provider received zero requests. The owner-installed artifact is still a
conformance fixture, not a production API or authorization policy.

Executed probe: 59,955 bytes, SHA-256
`1df8e53211505c82ef6532c6afc4213911094716fbc924d4ea0ecd5002b78212`.
The source aggregate matched before/after (`8a57b7`, `c752c9`):
`c43cda96a41bba7c4e56a8ac9d5238cccdf23bea950b7d07b2883d7fb9431c14`.
F/E9 lint and the eleven unchanged frozen-v8 inventory checks passed (`ac9f52`).

Main remains the existing v8 application. Production v9 SIGIL entry wiring,
normal/emergency admission receipts, draining and all configured dependency checks,
exact readiness behavior, old/new compatibility and recovery remain unfinished.
Storage/runtime observations do not establish audit verification, retention or
schedule correctness. No gate, pilot profile, external authority or release claim
changed; the full M0–M8 goal remains active and unqualified.

## Follow-up: draft SIGIL admission connected to actual native accounting

The isolated readiness stage now contains a production-entry admission fragment
and exact build recipe. It retains the original SIGIL bootstrap, request/body
policy and domain path, but uses v9 `DR2`/`DC2` observations for request accounting.
The shared emergency guard runs before any temporary read. The fixed SIGIL policy
receives the retained actual failure, temporary observation and native monotonic
clock; only a positive `VC1` receipt permits readiness inspection. Precheck failures,
malformed observations, failed CAS and tombstones cannot enable an accounting reset.
All scope, expiry, native ceilings and the eight-entry-evaluation limit remain.

Normal durable admission still precedes ordinary domain work. For readiness,
`RN1` now carries normal/emergency admission to an actual `inspect_execution` action.
**The final dependency/lifecycle response remains unfinished.** The draft is not
enabled in main and is not a substitute always-not-ready endpoint. Actual draining,
audit, retention and schedule observations, their SIGIL interpretation, full route
parity and old/new migration remain required. Native HTTP request-ID collection
was also corrected to include the already advertised v9 HTTP profile.

The complete staged run passed **92 tests in 394.60 seconds**, session 29154,
terminal `9fd56d`, observed 2026-09-09 12:00:22 UTC. It includes 72 entry/recipe
cases, two new actual HTTP cases plus their recipe check, six existing storage/
runtime conformance cases, and eleven unchanged frozen-v8 inventory cases. All
original native fmt/Clippy/test/build prerequisites (**293 native cases**) and the
complete original fixed-evaluator gate passed, followed by an optimized host build.
F/E9 lint passed, and staged source hashes matched before/after (`a0139a`, `6e8b8f`):
`f6b98c209b8264073d556c8ba1567c7c09879608935761415ff168b77e8befd3`.

The HTTP artifact runs the entire draft admission path but replaces only the
unfinished terminal inspection continuation with an explicitly test-only **218 /
AC1** diagnostic. It is not a production readiness response. Tests establish real
normal accounting, ignored forged body receipts, request-ID handling, actual
filesystem outage, independent emergency limits for two tenants, a third tenant's
scope refusal, exhaustion, and recovery without resetting usage or creating durable
temporary rows. The local provider received zero requests. The eight-evaluation
durable-commit-failure path is compiled transition-fixture evidence; native HTTP
commit-failure injection and final readiness parity are still outstanding.

An initial DR2-to-SR1 adapter mistake escaped 69 shallow transition cases and was
found during contract review. It was fixed before this full run. Three new cases
consume the actual translated output with the real compiled policy, including
tombstone refusal; the longest-path test now chains that actual output too. The
earlier count does not qualify the integrated adapter.

Draft entry: 62,942 bytes, 25 bound inputs, SHA-256
`17b055ba6a32c0539178aa281951f38c0403c5e4c64e56f3c864b22b95501c0f`.
The largest request-limit literal produces 62,960 bytes. Actual HTTP conformance
artifact: 64,158 bytes, 27 inputs,
`2bc5f56a0bf56bf11fcc6fc2482ccb8db8fec510f89562f0eadad6176c49f95f`.
These preserve the original 65,536-byte source limit and pinned stdlib. This is
local macOS stage evidence, not Linux CI, a releasable package, real-model scoring,
control-plane reuse, independent review or pilot readiness. Main remains v8 and
no M0–M8 gate is PASS. The next work is the actual dependency/lifecycle path and
final SIGIL readiness response, not a reduced acceptance definition.

## Follow-up: real operator-drain facts without native application policy

The isolated readiness stage adds an explicitly configured SIGUSR1 latch. Native
code reports a one-way, process-owned operator-request fact; SIGIL owns the new-work
decision. It installs no handler without the new lifecycle opt-in, rejects existing
signal ownership conflicts and reports changed/wrong-process ownership as errors.
There is no reset API, automatic exit, implicit worker cancellation, quiescence
claim or external-effect rollback. **This signal is not enabled in main v8; do not
send it to a non-opted-in host, where it may terminate the process.**

Opted-in v9 configurations bind `PF1 = MC1 + LF1` in existing field 17, with explicit
process-facts/v2 and HTTP-exchange/v4 metadata. Non-opted-in v9 still returns its
original MC1; older profiles are unchanged. The draft SIGIL entry validates the
container and extracts the original clock input for emergency accounting. Unknown
or failed lifecycle observations are never interpreted as healthy/running.

The SIGIL checkpoint runs after authentication, scope/body validation, confirmed
request accounting and deduplication lookup, before new operation admission.
Drain requests produce `503 service_draining`; unavailable observations produce
`503 lifecycle_unavailable`. Existing acceptance replay, changed-payload conflicts,
lookup and cancellation are preserved. The observation is not atomic with a later
effect, does not revoke earlier admission and does not prove a completed drain.

Verification: **117 tests passed in 459.83 seconds**, session 5268, terminal
`6aef6a`, observed 2026-09-09 12:32:39 UTC. Included are 95 compiled-entry/recipe
cases, five admission-conformance cases (four actual HTTP), six original storage/
runtime conformance cases and eleven frozen-v8 inventory checks. Full original
native gates passed **302 tests** (103 store, 22 worker, 177 service), including
nine new lifecycle/configuration cases. The complete original fixed-evaluator
gate and optimized service build also passed. F/E9 lint passed; staged sources
matched before/after (`f94971`, `f15508`):
`de5dd686b603a1aab89973bd81e34cf4bff5bc9e0c94360cc165582c19b20a94`.

Actual signals were sent only to private live test children. HTTP evidence covers
caller-body non-control, process-wide refusal of two tenants' new work, accounting
preservation, and a new latch/clock lifetime on restart. During an actually sent
local model request, acceptance replay and payload conflicts remained correct,
new work was refused, and explicit cancellation produced a retained uncertain
outcome with unknown usage/accounting. Restart preserved that result; only one
provider request was observed, and another tenant could neither inspect nor
cancel it. This is a local test provider, not a live-model usefulness score.

The final readiness continuation remains deliberately unfinished: HTTP conformance
uses explicitly test-only 218/AC1 diagnostics, not a product readiness response.
The fixture now retains the original complete public entry/header wrapper and
intercepts only that terminal continuation. Shared guarded-command code and the
original wall-clock test coordination keep the source/step/deadline bounds intact.

Draft entry at limit 2: 64,405 bytes, 26 inputs,
`023a9366b7645ca9b732d9eea10bdca22d643c41f21de0bbd288a1cbfd945382`.
The largest limit literal produces 64,423 bytes. Test HTTP artifacts remain below
65,536 bytes; their explicit-limit hashes are recorded in the stage README. No
runtime pin, Cargo lock, native ceiling, frozen-v8 fixture, pilot profile or release
gate changed. Main is still v8. Final readiness, audit/retention/schedule checks,
complete drain/recovery semantics, legacy parity, Linux qualification and every
remaining M0–M8 requirement stay open; no gate is PASS.

## Follow-up: complete SIGIL bootstrap checks moved without widening limits

The isolated readiness stage now pairs the draft entry with an extended version
of its existing fixed, grantless SIGIL admission function. The new AV2 request
validates the complete authority/grant registry, nested model/reservation profiles,
same-tenant namespace/profile agreement, same-principal rotation policy, and all
cross-tenant namespace exclusions. Exact original `configured`/`same_tools`
definitions and shared authority helpers retain source/fingerprint provenance.
Native code gains no application policy and no new registered function is needed.

The entry retains per-row fact validation and its current-active-credential time
window. Both the function call and final 204 carry TG1; native code freshly checks
the final guard before creating/opening application state. Some unsafe registry
relationships now fail inside the fixed pure function rather than the first entry
evaluation, but still before state creation/open and external effects. AV2 requires
the distinct `registry_validated` result. Old profile-only admission code and
`profiles_validated` replies cannot satisfy the new entry. AV1/AP2 behavior and the
main v8 recipes remain unchanged.

Verification: **267 tests passed in 625.86 seconds**, session 24699, terminal
`77abe1`, observed 2026-09-09 13:03:33 UTC. Coverage includes real compiled entry/
function chaining, all 36 cross-tenant namespace pairs, authority/grant/profile and
rotation negatives, active-expiry boundaries, the complete original AV1/AP2 suite
against the new function, existing lifecycle/admission cases, 17 new native startup/
reopen cases, previous HTTP and storage/runtime checks, and eleven frozen-v8
inventory checks. All original native prerequisites passed again: **302 tests**
(103 store, 22 worker, 177 service), complete fixed-evaluator verification and the
optimized service build. F/E9 lint passed. Tested stage aggregate was unchanged:
`7e3dc27a8811b66db134e596f6d306624553d21352349ebbbd76b2f6c6691ae7`.

The preliminary run had 227 passes and one incorrect test-inventory count assertion
(19 original test functions, not 20). All original cases were present; the count
was corrected and the complete run above passed. No behavioral expectation, native
prerequisite or execution ceiling was loosened.

Actual native startup refused old code/results, unsafe grants, tenant conflicts,
rotation changes, bad profile/scope/tools, duplicate credential digests, guest
network authority and missing/expired final guards before the records directory
was created. No provider request occurred. Refused reopen preserved existing
durable accounting rows; this does not qualify a full version/state/in-flight
migration. Previous real drain/cancellation/outage scenarios also still passed.

The draft entry recovered 1,551 bytes: 62,854 bytes at request limit 2, and 62,872
at the largest signed-64-bit literal, within the original 65,536-byte bound. The
new admission artifact is 48,844 bytes. Their SHA-256 identities are respectively
`a4c8888d7bbbdf4c6fc47ed657ab77517fa90e4a1f17ffef3da0e0c58f77cd36`,
`2a62ae372915000a257bcd13b55e7e25dbaa75a5e4fbc7c378dc9594c08e034b`,
and `2d22f07b492fa9a87107b7bb71b7538265c47383bc740af0950d1fbc862ba277`.
The stage README records full conformance identities. Five fixed functions, eight
entry evaluations, fuel/memory/deadline bounds, runtime pins, native code, Cargo
locks and frozen-v8 fixtures are unchanged in this step.

Final readiness and actual audit/retention/schedule checks still need implementation;
the HTTP fixture still emits test-only 218/AC1 diagnostics. Required dependencies
cannot be omitted or replaced with constant outcomes to fit source space. Main is
still v8. No gate is PASS, and the entire M0–M8 goal remains active.

## Follow-up: authenticated-log storage with atomic related-state publication

The isolated readiness stage adds a native **generic storage/secret mechanism**,
not native application audit policy. An authenticated opaque entry, updated chain
head and related domain mutations use the same existing Store transaction. HMAC
keys remain in a private native object; no guest/HTTP/CLI key binding, environment
loader, product configuration, event policy or legacy audit replacement is enabled.
SIGIL still must own event selection/redaction, allowance/retention policy and the
interpretation of actual execution observations. Signed bytes alone do not prove
that an execution happened.

The format authenticates chain identity, namespace context, content, sequence,
links and configured bounds. Full-chain verification uses one read snapshot with
bounded memory and the existing cooperative deadline/SQLite progress mechanism.
Missing/tombstoned/orphaned records fail; complete historical verification is
separate from append's authenticated-tail check. An independently retained latest
checkpoint can detect coherent rollback; no self-contained HMAC log is claimed to
detect replacement by every possible older authentic snapshot or a compromised signer.

Verification: **267 tests passed in 763.46 seconds**, session 59339, terminal
`33f127`, observed 2026-09-09 13:42:44 UTC. All original native prerequisites passed:
**120 store + 22 worker + 177 service = 319 tests**, complete fixed-evaluator gates
and the optimized service build. New native tests cover real process kills before/
after commit, restart, stale/denied related writes, cross-chain/namespace reuse,
tampering with repaired storage checksums, malformed/unsigned data, key/limit
mismatch, rollback/checkpoint limits, deadline/work exhaustion and a read-only
1,000-record verification. Existing HTTP accounting/drain/cancellation/outage and
all frozen-v8 inventory cases passed. This is not the required product load test.

Development corrected a distinction between the global commit receipt and the
head's own CAS revision; an interleaved-write regression now pins it. The initial
SQLite budget test used too little work to reach the existing callback granularity;
its workload was increased from 50 to 300 records, without widening the budget.
The complete run above followed those fixes.

Tested stage source aggregate matched before/after:
`fd1f0a84075822054d467dbeb7f1fe16a3cec14fb25608ad849af0f408be9a10`.
Optimized staged host:
`919ca797a7fc03a57e8bc904af5ce93425e5f7ee7a88ab4dc1ea79a9791cc8d0`.
The stage's `docs/authenticated-log.md` records exact API, format and test limits.
Store/service locks add cached pinned HMAC/zeroization dependencies and the digest
MAC feature; existing package versions, worker lock, SIGIL runtime pin, original
Store/entry ceilings and frozen-v8 fixture remain unchanged. Main code is untouched.

Still open: SIGIL audit schema/redaction, scoped key/artifact/namespace admission,
actual audited execution and complete inventory, reservations/settlement, retention,
archival/legacy migration, freshness/checkpoint policy, and final readiness using
actual dependencies. No constant healthy/unhealthy substitute or omitted dependency
qualifies the endpoint. Main remains v8; the staged HTTP response is still diagnostic
218/AC1; no M0–M8 gate is PASS and the full goal remains active.

## Follow-up: actual SIGIL settlement and atomic authenticated audit publication

The isolated readiness stage now connects the actual fixed SIGIL settlement
producer to a fixed, grantless SIGIL transaction-audit projection. Native code
provides actual source/runtime identities, scoped storage grants, clock samples,
selected budgets and input/output/batch hashes and sizes. SIGIL defines and
validates the event schema. Raw conversations, snapshots, returned text and
native signing-key bytes are not supplied to that projection.

The opt-in trusted `sigil-transaction/v2` embedding publishes the domain changes,
audit entry and authenticated head in **one original Store commit**. It cannot
fall back to an unaudited commit when the projection, permissions, runtime or
capacity fail. Original transaction requests cannot supply facts, hashes, batches,
receipts or audit targets. Explicit verification reports chain integrity, not
independently checked freshness. The original v1 path and automatic-service
configuration remain supported; this is not yet HTTP/automatic audit integration.

Verification: **378 behavioral tests passed**, session 80939, terminal `4f9a8a`,
exit 0, observed **2026-09-09 14:47:03 UTC**. This includes 75 new audit-policy and
native integration checks, all 28 unchanged native settlement tests, all 8 unchanged
native preclaim tests, and all previous 267 bootstrap/readiness/lifecycle/storage/
frozen-v8 checks. The quiet configuration omitted the usual count/duration summary;
post-run collection confirmed 342 staged + 28 settlement + 8 preclaim cases, with
only passing progress markers in the completed run. No duration is invented.

Every original prerequisite passed: formatting, warnings-as-errors checks,
**120 Store + 22 worker + 179 service = 321 native tests**, complete fixed-evaluator
verification and optimized host builds. The two new native tests require exact
request fields. Native test enumeration confirmed the counts. F/E9 lint passed.

Evidence includes independent HMAC verification over actual persisted entries,
reconstruction of actual producer input/output/batch hashes, real settlement and
restart, a process kill after acknowledgement became available but before the
client consumed it, no duplicate audit/settlement on retry, original read-only
permissions, orphan/tombstoned logs, incorrect keys, actual runtime-file mutation,
refused malformed/empty/skip proposals, full 62-namespace metadata preservation,
and an installed seven-record Store refusing six existing plus two audit records
without partial publication. Generic Store before/after-commit kill tests remain
included; the new CLI test does not claim a pre-commit kill it did not perform.

Preliminary runs found a warnings-as-errors enum-layout issue, fixed without
disabling that check, and an incorrect new-test assumption about source-file
mutation. The worker executes immutable verified bytes retained at admission;
changing their original file is not a code-update/revocation channel. New tests
now prove the retained source digest/behavior and separately reject a changed
runtime executable. No existing fixture or execution limit was loosened. The full
passing run above followed both corrections.

The stage source aggregate matched before/after:
`379449800aaff7b288dfb99e39d332487bbf96cafc65080b97f7957b560a117a`.
Composed SIGIL audit policy: 28,562 bytes,
`37f2cdce146b3e09618384b3cd0277eeb21c4fcdb2b47f1e6c99ff59f97b7fc1`.
Optimized transaction CLI:
`ed5a32dc6867dcb8e41fc196801a06cebcc2a69fa4fd3982e9fb93da788ffc95`.
Optimized staged application host:
`3b328892694cc1d8eb58c8c3a6c5e196cfcc796da30910a57bd79783b8889081`.
The stage's `docs/audited-transactions.md` records exact bindings and verification
procedures. The service directly uses the already pinned `zeroize=1.9.0`; no
dependency version, Store/worker lock, SIGIL runtime pin, fixed-function inventory,
source/evaluation/fuel/memory/deadline ceiling or frozen-v8 fixture changed.

This is a first state-transition audit connection, **not** full model/tool/failed-
execution audit coverage, legacy audit compatibility or an externally approved
policy. Automatic/HTTP admission and use, operation/tenant audit inventory and
correlation, audit reservations/quotas, retention/archival/migration, independent
checkpoint policy and final readiness remain required. Main code is untouched
and remains v8; the staged readiness endpoint is still diagnostic 218/AC1. This
local macOS evidence does not qualify Linux, an MVP load run or any M0–M8 gate.

## Follow-up: automatic/HTTP state-transition auditing and restart admission

The isolated readiness stage now connects its actual automatic transactions to
the fixed SIGIL audit projection and atomic authenticated publication. Trusted
participant configuration opts in all fixed transactions; omission preserves the
original path. No HTTP caller can choose a chain, key, grants, observations or
signing payload, and no public audit-query/verification endpoint is introduced.

Before state initialization, audit namespaces are checked against every tenant's
domain grants, all claim/delivery namespaces and every other audit namespace.
Audit authority is not delegated to the coordinator, transaction producer, effect
worker, dispatch policy or credential. Admitted source bytes, projection and native
keys survive bootstrap without rereading mutable inputs. The real SIGIL AB1 check
validates configured metadata; it creates no fabricated execution or log entry.
The fixed projection uses the existing cached evaluator with a fresh guest instance
per call, without caching policy decisions or changing execution ceilings.

Existing authenticated history receives a bounded complete check before automatic
dispatch. Wrong keys, tombstoned heads, corrupted non-tail entries and orphan entries
refuse restart without new provider calls or domain mutations. Clean absence stays
explicitly uninitialized; it is not verified empty history. No repair, reset or
rotation is performed. Independent inventory/checkpoints remain necessary to detect
complete erasure or coherent rollback. Strict explicit verification still rejects
missing chains; this startup check does not implement final product readiness.

Verification: **433 tests passed in 1460.71 seconds**, session **95861**, terminal
**`b09665`**, exit 0, completion observed **2026-09-09 16:03:41 UTC**. This retains
all previous 378 cases and adds six SIGIL AB1 checks and 49 automatic/HTTP admission
scenarios. Every original prerequisite passed: formatting, warnings-as-errors,
**123 Store + 22 worker + 179 service = 324 native tests**, complete fixed-evaluator
verification and optimized builds. Native enumeration confirmed counts; changed
Python passed F/E9 lint. No original fixture, timeout or assertion was weakened.

Actual HTTP evidence covers model -> approved file tool -> response, follow-up,
replay and restart using the real draft v9 SIGIL entry; two tenants with identical
external session names; and restart after a real local provider send preserving
uncertain/unknown outcome. Persisted HMAC framing is independently checked. The
separate artifact-inspection test uses the explicit 218/AC1 diagnostic and does
not count as a product-ready response. No browser/live-model/Linux result is inferred.

The initial full run is retained: session **29181**, terminal **`cd3a2f`**, **132
passed, 1 failed in 934.53 seconds**. A new invalid-limits fixture mutated a shared
API configuration dictionary, leaving `database_pages=0` for a later unchanged
legacy test whose Store correctly retained 262144. The new fixture now copies its
configuration before mutation. No native code, production ceiling or legacy
expectation was changed for this correction. The full rerun above includes the
previously failing legacy reopen and all its original assertions.

Successful before/after tested stage aggregate:
`e9ba135dcb7aa495f684dee6840198bba07135a1268ac7a5b769451a55249fb6`.
Optimized staged application host:
`67ac7d442dc43d676444e2717a5575b800a8348ea1d88bdee538a6d8ab444296`.
Optimized transaction CLI:
`9bce210ebd9348eb408e136ab23e5a318cda4fac9e8538c90eb7631ecd554aa9`.
Composed audit policy: 29,057 bytes,
`06a9a2d547167b62bd76c93cd9b9587db22796b1fc3fa2f2d53fc5cf45e8ecfb`.
The stage's `docs/audited-transactions.md` records exact scope, startup protocol,
native identities and reproduction selectors. All Cargo locks, the SIGIL runtime
pin, fixed-function inventory and source/evaluation/fuel/memory/deadline ceilings
are unchanged. Main code remains untouched and v8; only this evidence is updated.

Still required: model/tool-effect and failed/denied-execution audit coverage,
complete tenant/operation inventory and correlation, audit reservations/quotas,
retention/archival/migration, independent checkpoints and actual final readiness.
There is no owner-approved pilot profile or qualifying candidate; all M0–M8 gates
remain open and the full MVP goal remains active.

## Follow-up: actual worker-lifecycle audit publication

The isolated readiness stage now couples actual effect claims and delivery or
recovery records to a separate private authenticated chain. The fixed SIGIL
policy classifies and redacts actual native facts: admitted worker artifacts and
grant ceilings, prepared input/budgets/time guard, observation shape, operation
and generation correlation, recording context, and actual recorder/batch digests.
No raw conversation/output/provider key/audit key is logged, and no public signing
or caller-supplied observation API is added. Configured identity is not a complete
record of the earlier dispatch authorization decision.

Whole-registry admission excludes overlaps with every tenant domain, claim,
delivery and other audit namespace. Audit powers remain outside every original
execution role. A native read-only batch preflight preserves the actual original
caller scope before private audit powers are added; it neither mutates nor
reserves state. Each claim/delivery publication uses one original Store commit.
No failed audit can fabricate a receipt, terminal phase, or effect retry. A full
chain can prevent dispatch; exhaustion after an actual effect leaves recording
unconfirmed. Recovery preserves uncertainty and omits old artifact/input facts
that a restarted process cannot establish from current configuration.

Verification: **600 behavioral tests passed in 2139.63 seconds**, session
**84746**, terminal **`dd7997`**, exit 0; completion observed **2026-09-09
18:37:32 UTC**. This retains the prior 433 cases, adds 113 new policy/HTTP/admission
cases, and includes 54 original recorded/owned-worker cases against the staged
binaries. Every original prerequisite passed: formatting, warnings-as-errors,
**125 Store + 22 worker + 185 service = 332 staged native tests**, complete fixed-
evaluator verification, frozen-v8 gates, and optimized builds. No skips, source/
evaluation/fuel/memory/deadline widening or relaxed original expectations were used.

The full-run source aggregate matched before/after:
`ca81e68d6d1e7eb9982dd925e814bb7c560ceb5ba61f3b1a39e682e56fbcce20`.
Optimized application host:
`8000cd4319ee916a84f52fd2b5b59a3993bc2e05b8709e162d77f6282643fc61`.
Optimized transaction CLI:
`1b71c2573e4a29b53a711808b84e11de07cfe5d23925fc86634d357b6e28163c`.
Composed effect-audit policy: **37,171 bytes**,
`df6b8a9a80ff540d4a6b2530c9677316e2322dc15cae71b4fdf99dfd25939d6c`.
All Cargo locks, SIGIL runtime/stdlib identities and the frozen-v8 fixture are
unchanged. Main's snapshot remained
`11de50c191bbbba19f3557e7792421cfe04509eb3c3de1acc88855aba7501b92`
until this evidence update; its code and v8 runtime selection are untouched.

Preliminary results are retained, not replaced by the pass: 3 passed/1 failed in
470.66 seconds (a new cancellation-race assumption); 60 passed/1 failed in 804.01
seconds (a new expected-error label); and 121 passed/1 native setup error in
1178.47 seconds (new fixtures failed to reach their invocation preconditions).
Public cancellation acknowledgement now correctly remains a request, not proof
it beat the pinned two-second transport timeout. A separate HTTP timeout case and
actual native hard-cancellation case preserve both distinctions. The malformed
reply test now specifically requires the actual protocol rejection.

Native fixture isolation alone did not remove the intermittent deadline. Startup
diagnostics exposed pre-invocation expiry, including no worker startup trace.
Unlike actual service pre-admission, the new fixtures had built/admitted a recorder
after starting the action clock. They now admit the responder first and derive its
generation from the actual incoming facts, still checked by the original native
binding rules. Only these new fixtures are serialized; existing tests, runner
settings and five-second/200ms limits are intact. Eight complete native service
repetitions passed after that correction, followed by the full 600-case run above.
The stage's `docs/effect-audit.md` records exact protocols, artifacts, reproduction
selectors and the complete failure/correction sequence.

This is real local SIGIL/HTTP execution evidence with deterministic providers,
plus explicitly controlled native mechanism tests—not live-model, browser, Linux,
load, independent-review or real control-plane qualification. Complete event and
authorization-decision coverage, audit reservations/quotas, retention/migration,
independent checkpoints and final readiness remain required. There is still no
owner-approved pilot profile or qualifying candidate. All M0–M8 gates remain open
and the full MVP goal remains active.

## Verified follow-up: actual dispatch-policy provenance in the staged worker audit

The isolated readiness stage now retains the actual successful dispatch-policy
evaluation alongside its prepared worker evidence. Private native DP1 facts bind
the admitted policy manifest, fully bound configuration digest, actual input/output
digests and byte counts, ordered actual read-set digest/count, clocks, elapsed time,
selected timeout, checked alias, selected effect-input hash, returned time guard
and opaque context hash. WP2 carries these facts through the original claim and
owned/synchronous completion paths. The SIGIL projector validates the fields and
adds redacted `dispatch_policy` metadata; raw authority, conversation and output
remain absent. Manual WP1 and abandoned recovery do not invent a policy evaluation.

This is not full decision-audit coverage: denied/failed policy evaluations and
HTTP/authentication decision inventory, reservations/quotas, retention/checkpoints
and final readiness still require work. The host still does not interpret pi
authorization or arm an effect without its original bound commit receipt. No
source, evaluation, fuel, memory, deadline or registry limit was increased.

Completed component evidence: **82 compiled-policy tests passed in 37.53 seconds**
(49 retained + 33 new; session 85001, terminal `6a0763`). **187 native service tests**
passed (170 library + 15 main + 2 transaction tests; session 26701, terminal
`a1e140`); formatting and warnings-as-errors also passed. The eight targeted native
audit cases passed in 6.82 seconds (`e8990d`). Python F/E9 lint passed (`703618`).
The two new native cases independently compare actual captured policy input/output
and read-set hashes, retain exact facts across claim/completion, exclude raw data,
and refuse invalid policy output without claiming or invoking an effect.

Broader verification was restarted as session **26121**, with **682 collected
behavioral tests**; its final passing result is recorded below. This retains the
previous 600, adds 33 compiled
provenance cases and 49 original native-dispatch cases, strengthens actual HTTP
model/tool/follow-up/tenant/recovery assertions, and retains all original native,
fixed-evaluator and frozen-v8 prerequisites. Earlier sessions 48266 and 60713
are terminal; their failures and the subsequent investigation are recorded below.

The first run, session 48266, terminated before any behavioral case on a sandbox
denial of the scripted provider's loopback socket bind: one setup error in 313.73
seconds (`47e944`). After confirming the stage fingerprint was unchanged
(`89fb10`), the identical suite was restarted with local networking permission as
session 60713. No test, source, limit or provider fixture was altered to pass it;
the suite uses deterministic local providers, not paid model access.

Live checkpoint at 2026-09-09 19:16:19 UTC (`39ce8d`): the first **146 cases**
have passed in that exact run—52 startup-admission, all 12 actual HTTP worker-audit
cases and all 82 compiled audit-policy cases. This includes real local model/tool/
follow-up and two-tenant execution, restart/cancellation/timeout distinctions,
corrupt-history rejection and refusal to dispatch after failed claim auditing.
At that checkpoint the remaining cases were running; 146 was not a completed-suite
result or an MVP gate pass. Stage fingerprint and Cargo locks also matched during
the run (`7e9a3d`).

The run subsequently ended at **146 passed, 1 setup error in 899.34 seconds**
(`52c0c1`). A repeated complete native service prerequisite passed 169 library
cases but failed the controlled hard-cancellation fixture: no worker startup was
observed before its original five-second deadline. The native result truthfully
reported no-send/reaped/deadline and phase 4. No relevant leaked workers remained
in the read-only process inspection (`fdb9da`). Investigation found another
fixture/production mismatch: the fixture used a cold fresh formatter, whereas
the actual service uses a retained cached formatter already invoked during WB1
admission before an action begins. At that failure checkpoint this mismatch was
still being corrected and tested, not established as the complete cause or a
verified fix. No deadline or cancellation assertion was relaxed.

The controlled formatter now uses `Function::cached`, an explicit pre-action
fixture boot and a persistent responder. Its boot returns a controlled admission
reply without creating an audit event; real compiled WB1 admission remains covered
by the HTTP suite, not by this fixture. An added assertion checks that one actual
formatter process serves claim and completion. The existing forced publication
delay still applies after boot. Bounded diagnostics preserve recorder timing if
the invocation precondition fails again. No old deadline, cancellation, receipt,
no-send assertion or duplicate native prerequisite was bypassed.

After this correction, all eight targeted audit cases passed in 6.60 seconds
(`6f7fd4`), warnings-as-errors passed (`0f16b6`), and **20 consecutive complete
187-test native service runs passed** (session 1717, terminal `ed8a1e`, exit 0).
Each run contained 170 library, 15 main and 2 transaction tests. These are repeated
executions of 187 unique cases, not 3,740 distinct tests. The full 682-case selector
was then restarted with local networking permission as **session 26121**. Its
inputs remained frozen until completion; quiet polls did not cause a restart.

The current rerun's stage aggregate, after the fixture correction and contract
documentation, is
`9681889cc5b153daf877268a323f778871cd0544e53bc0e42d27077ae32bbd21`
(`af70b0`). The stage's `docs/dispatch-audit.md` records the exact DP1/WP2 contract,
legacy/unavailable metadata semantics, native repetition timings and both earlier
failures. At that checkpoint the rerun had not yet qualified the complete regression
or any MVP gate. The subsequent completion below qualifies the staged regression only.

Frozen stage input aggregate for the first two attempts:
`dab1ba2b8ce6c7d3a2070d811d0218707aba1b936b7cc368c311a4647c4ae97a`
(`03486e`). Composed audit policy: **39,519 bytes**,
`94023763815c65923eef59d5d2811e4ff5b043fd77b45360345045408f4cf1f1`;
stdlib remains `b5f40e2eba41b6734f8db071`. Main remained
`b2ae2a45b4d51137c60ce4c47bb978cce2519a1392efc5d8ead6de27308ece50`
before this evidence-only append. Main code and its v8 selection are unchanged;
all M0–M8 gates remain unqualified. The prior 600-test pass applies only to its
own snapshot; the changed stage now has its separate 682-test result below.

The [audit migration inventory](audit-migration-inventory.md) now records the
reference execution-audit boundary, each current publication/observation gap and
the evidence still required for M0/M3. In particular, producer no-ops and failed
invocations are not covered merely because successful commits have signed events.

### 2026-09-09: cross-project integration review, not M0/M6 qualification

The [shared-runtime integration review](shared-runtime-integration-review.md)
records actual control-plane v19dj/v65 selection, its separate patched application
toolchain and named-KV/binary delivery boundary, alongside pi's staged revision-
checked/framed mechanisms. It proposes a real assignment/adapter/result workflow
for M6 and lists required conformance/recovery evidence. No sibling pin, selector,
source or state was changed, and no sibling service or test was run.

The AIN read-only snapshot advances the charter's historical CP324 reference to
CP400 accepted / CP401 partial, including the newer presented-v2 freeze and audit-
checker correction at commit `6050f096f1b833d01349052fa384aeb09483c72e`. Reported
proof results were not rerun. This preserves AIN as an optional future backend;
it does not establish runtime compatibility, a performance/completion claim, or
owner approval. M0–M8 thresholds and the current runtime pin remain unchanged.
At the time of this inspection, the active 682-case staged regression was a separate
unfinished verification. It subsequently completed as recorded below.

### Completed 682-case dispatch-provenance regression — 2026-09-09

The exact session **26121** finished with **682 passed in 2,250.49 seconds**
(37 minutes 30 seconds), terminal `7b2fa2`, exit 0 observed at **20:06:56 UTC**.
There were no skips or expected failures in this behavioral selector. It retains
the previous 600 cases, adds the 33 compiled DP1/WP2 cases and 49 original native-
dispatch cases, and strengthens the real HTTP lifecycle assertions. This final
result does not erase either earlier broad-run error or the controlled-formatter
investigation above. No assertion, prerequisite, deadline or resource ceiling
was weakened to obtain the pass.

The stage's tested **before/after aggregate matched**:
`9681889cc5b153daf877268a323f778871cd0544e53bc0e42d27077ae32bbd21`
(`af70b0` / `f1f233`). The exact full command, preliminary errors, fixture correction,
20 native repetitions and final artifacts are preserved in
`/private/tmp/sigil-pi-readiness-host.BiNfIvYV/docs/dispatch-audit.md`. Later edits
to the evidence documents do not redefine the tested code snapshot.

The original native formatting, warnings-as-errors, tests, optimized builds,
fixed-evaluator lifecycle and frozen-v8 prerequisites all remain in the fixture.
Post-run enumeration confirmed **334 unique staged native tests**: Store 125
(`dac26a`), worker 22 (`0be7db`), service 187 (`ff8ce7`). These prerequisite cases
are separate from the 682 behavioral cases; repeated gate executions are not
additional unique tests.

Post-run optimized artifact SHA-256 values (`f74616`):

| Staged artifact | SHA-256 |
|---|---|
| Application host | `fbbba4bc9d0350c303f9e485f93ec2a774278543945fd383c9e58c46abcd5311` |
| Transaction executable | `6a4c6160c917d0ae409582711830d8d768daebb8672160947dd3e9bcf0d7511a` |
| Claimed-worker executable | `8dcf43a002270a5b7304e77e2c333bab5844f0d76770dc715a7f6ae3e808d179` |

The three Cargo locks are unchanged. Post-run composition (`be5c41`) remains
**39,519 bytes**, SHA-256
`94023763815c65923eef59d5d2811e4ff5b043fd77b45360345045408f4cf1f1`,
stdlib `b5f40e2eba41b6734f8db071`, using the unchanged SIGIL revision
`8277a1d92d599df89e6b4391fc70fd0fa534d696` (`1d6c89`). Main's aggregate before
this result-only documentation update was
`fc0b5560753ae50cec8809942f4bdb9d55a52dae51ef1de91920566da91bc3c4`
(`db1e96`); no main application, test or runtime source was changed by this run.

The [audit migration inventory](audit-migration-inventory.md) now identifies the
actual failure-observation capture points that remain missing: native invocation
detail is reduced before policy/producer validation and publication. Next work
must preserve those private actual facts and let SIGIL interpret them, without
inventing denied authority, output, a claim, delivery or a commit receipt. Complete
request/route audit coverage, capacity reservation, quotas, retention, independent
checkpoints and final readiness remain required.

Main still selects v8. This is local staged SIGIL/HTTP/native evidence, not main
integration, complete route parity, Linux/load/recovery qualification, real-model
usefulness, independent onboarding, control-plane conformance, security clearance
or a releasable candidate. No provider spending, deployment, sibling edit, pin
change, commit or push occurred. The full goal stays active; **all M0–M8 gates
remain unqualified**.

### In progress: actual failed-evaluation observations — 2026-09-09

The isolated readiness host now has a private `Function::observe` boundary that
returns the original result plus actual facts on success or failure. It retains
admitted source/runtime identity, bounded input digest/length, selected limits,
elapsed time, native boundary/error/fault and available result/output metadata.
Only fresh execution supplies actual request/cleanup observations; cached fields
stay absent. Parsed-result hashes identify the explicit serialized parsed value,
not wire bytes or guest output. Missing output does not become an empty-string
hash, and runtime error does not become an authorization denial. Existing public
result/error behavior is retained; successful DP1 uses the captured digests.

This is a necessary capture mechanism, **not durable failure audit coverage**.
Current wrappers and failed policy/producer paths still need to intercept the
facts and pass them to fixed SIGIL interpretation/publication before dropping them.
No public observation/signing API, new grant or claim/receipt bypass was added.

Nine new native cases passed (`6f3257`), followed by an initial full 196-case service
run (`e271c1`) before the artifact-identity follow-up. A later full run failed with
**177 library passed, 2 failed in 16.17 seconds** (session 18353, `bdeb3a`): the
original hard-cancellation fixture missed worker startup after 5.0657 seconds,
truthfully retaining no-send/reaped/deadline, while one new runtime-error fixture
received `worker` instead of `application`. Claim-recording diagnostics showed
4,146 ms; the new failure did not retain its precise native fault. No stronger
root-cause claim is made from that incomplete observation.

Only the nine new evaluator fixtures now limit their mutual subprocess concurrency
with a private mutex acquired before worker creation/clocks. Original tests,
runner settings, the original audit mutex, deadlines and expected results are
unchanged; redacted facts were added to new assertion diagnostics. The complete
196-case suite then passed (`f794e9`), followed by **five consecutive full 196-case
native service passes** (session 68002, terminal `7b7c6b`, exit 0 observed at
20:31:52 UTC). Each run contains 179 library, 15 main and 2 transaction cases;
library durations were 13.01, 13.15, 15.97, 13.13 and 13.41 seconds. They are 196
unique tests repeated, not 980 distinct cases or proof against all timing variance.
Formatting and warnings-as-errors passed (`f598e5`, `169234`).

The code/evidence details are in the stage's `docs/evaluation-observations.md`.
Its pre-repetition aggregate was
`1dcbd002f48ebf06b370cb9ff42cbe6b6c98cbeab67b83fbe0059c0a2f037bfa`
(`3fb5e6`), matching after the repeated runs (`9b98f2`). The earlier 682-case pass is
for its earlier unchanged snapshot; a
broader regression is required for the changed host after the remaining failure-
audit integration. Neither main code nor its v8 selection was changed. No M0–M8
gate is qualified; complete audit coverage and the full product goal remain open.

### Focused failure-publication component regression — 2026-09-09

The readiness stage now has an explicit **audit v2** failure publisher. Native
policy/transaction owners retain actual evaluation facts through refusal and
consume them once. Fixed grantless SIGIL classifies/redacts the event; native code
appends only the authenticated head/entry batch, without a domain mutation, claim,
effect ticket, delivery assertion or retry. Audit v1 behavior is unchanged; v2
requires a concrete `evaluation` worker and rejects absent/null/mismatched opt-in.

Successful evaluation followed by mechanical refusal is distinguished from failed
evaluation, not relabeled as an authorization denial. Original subject deadlines
gate projection/append; reporting has no replacement execution window. Reporting
failure returns `audit_recording`, and an uncertain append returns
`audit_commit_uncertain`, never a receipt or proof of absence. Pre-evaluation errors,
complete actor/operation/read attribution, other execution paths and readiness/
alert policy remain separate gaps. The inventory has been updated accordingly.

The focused run **passed 224 tests in 1,510.99 seconds** (25 minutes 10 seconds),
session **54902**, terminal `94d701`, exit 0 observed **2026-09-09 21:34:36 UTC**.
There were no skips or expected failures. It includes:

- 54 compiled structural classifier cases; synthetic facts do not prove provenance.
- 8 actual compiled transaction/classifier/native publication cases, including
  signed restart-verifiable failure records, unchanged domain state and visible
  failed publication without a receipt.
- 19 actual HTTP/startup/integrity cases: failed dispatch without claim/provider
  call, invalid v2 setup rejected before state creation, and optional classifier
  executable replacement detected by the real bounded inspection registry.
- 143 original audit regressions, including successful tool turns, tenant isolation,
  restart/cancellation, original transaction behavior and startup refusals.

The original staged native formatting, warnings-as-errors, test/build steps and
fixed-evaluator lifecycle prerequisite all remain. Service enumeration confirms
**205 unique native cases** (188 library, 15 main, 2 transaction); unchanged Store
125 and worker 22 bring the staged native prerequisite count to 352. Repeated
fixture executions are not additional unique cases. This focused selector does
**not** rerun the complete previous 682 cases or all frozen-v8 compatibility evidence.

Initial follow-up errors are retained in the stage's `docs/evaluation-observations.md`:
the first new native run passed 6/9 and failed three incorrect fixture assumptions
(`04c0db`): empty Store scopes and conflating owner precheck with executable-byte
verification. Corrections were confined to the new fixtures; actual artifact
inspection has separate passing HTTP tests. Nine new native cases then passed
(`4f6ed4`). The next full 205-case service run passed, while a warnings-as-errors
check rejected a collapsible `if` (`40caad`). That style error was corrected before
the retained full prerequisites above. No original assertion, timeout or ceiling
was weakened, and there is no lint suppression.

The stage aggregate **matched before/after** (`1479ce` / `5dd34a`):
`0b8b9a804750af3e7bfa0941234d7dcb7868db4d42954039cc0f38966b7cc5dd`.
Later evidence-document edits change the whole-stage hash, not the tested code.
The classifier is 35,456 bytes with one entry, compiler-input SHA-256
`42bd3667f1cefa1517db02cef7862f945a25e36544147d71b60206c088eaf186`,
stdlib `b5f40e2eba41b6734f8db071` (`8fd39f`). The runtime pin remains
`8277a1d92d599df89e6b4391fc70fd0fa534d696` (`03a4ce`); all Cargo locks are unchanged.

Optimized artifact SHA-256 values (`5dd34a`):

| Artifact | SHA-256 |
|---|---|
| Application host | `eaf8f98f54197a0d3e759b9be47fbb52405079baafd2806293cb3ff75a4395c5` |
| Transaction executable | `31c46769793ff2be7bacdf6d7fbca5fefe1c334cce725c06649c5bea9fa11401` |
| Claimed-worker executable | `c702ad6eabb6ff068334abe363efe7fcc6a98a0391540b8c3adf34df9738a29e` |

Next: explicit v2 normal model→tool→response coverage, additional reporting-fault
evidence and the complete changed-host regression; then common execution inventory,
actual attribution and prospective audit-capacity accounting. These focused passes
do not transfer the earlier 682-case result to this changed executable.

Main's aggregate before this turn's documentation changes was
`4498f79a3da0477563e8d45b7693c0adf94574ac9810a97b3b35ef632c0f7111`
(`2e1d72`). Only main documentation changed; main still selects v8. No provider
spending, deployment, sibling edit, runtime-pin change, commit or push occurred.
This is staged local component evidence, not complete route parity, Linux/load/
restore qualification, real-model usefulness, onboarding, control-plane reuse,
independent security clearance or a releasable candidate. **All M0–M8 gates remain
unqualified and the full goal remains active.**

### V2 workflow/failure follow-up; broad regression running — 2026-09-09

The stage adds test coverage, with **no production source change**, for normal
model→tool→response behavior under both v2 audit publishers, two tenants, follow-up
continuity, replay and restart. That actual HTTP test passed in the broader run.
An additional HTTP case passed with the actual `coordinator_refused` /
`audit_recording` diagnostic after failed failure-publication, no signed event,
no claim and no provider request. This is a diagnostic, not a complete product
health/alert policy.

A new compiled publication case passed after killing the client-facing native
process once its failure response became available but before consuming it. The
existing signed record remained verifiable after restart. Invalid input did not
replay the old observation; a subsequent actual evaluation produced a second,
distinct audit identity. This is **post-publication acknowledgement loss**, not
an injected in-commit uncertainty or disk-failure test.

Three new controlled-native cases cover denied append scope, unavailable Store
pathname and a corrupt retained authenticated-log tail. Each refuses publication
without domain mutation; no successful proposal becomes a receipt. Corrupt history
is neither repaired nor advanced. All **12 evaluation-audit native cases passed
in 7.66 seconds** (session 60333, `8b6261`), and warnings-as-errors plus the full
**208-case native service suite passed** (session 12063, `6d354b`; 191 library in
19.39 seconds, 15 main, 2 transaction). Original assertions, fixture concurrency
boundaries, timeouts and limits are unchanged. The Store's existing private
pre-commit hook is not available through this service boundary; no service-level
fault switch was added. Actual in-commit uncertainty remains separate evidence.

The broad selector was collected at **766 cases** (`a78927`): the unchanged earlier
682 selectors plus 84 evaluation-audit cases. Execution session **18302** started
at **2026-09-09 21:45:48 UTC**, launch `c536fe`, using local scripted networking
only. The original native, fixed-evaluator and frozen-v8 prerequisites remain.
The three new test modules run first; their **84 cases passed** through `21a277`
(21 automatic HTTP, 9 compiled publication, 54 compiled structural cases). The
remaining 682-case portion was **still running** at this record's checkpoint.
There is no terminal 766-case result yet: resume the same live session, do not
restart it or describe this partial progress as a complete broad pass.

The frozen stage aggregate before launch was
`c7d547ba00422cc0228e1fe8736a43fd7b7e28ef0246d532cb08c4ac84adc4fc`
(`d892df`), still matching at the 84-case checkpoint (`ee44a4`). The optimized
application/transaction/claimed-worker hashes still equal the preceding 224-case
artifact hashes, and all Cargo locks are unchanged. The stage's evidence files
are deliberately not edited during this frozen run. Main changes in this turn are
documentation only; its selected service remains v8.

The [audit inventory](audit-migration-inventory.md) now identifies the actual
remaining invocation sites and native context: entry/helper calls, coordinator,
policy, transaction, claim/result producer, internal projector and separately
scoped effects. It distinguishes wrapper/native observations from real guest
executions and preserves pre-entry transport rejection as a separate surface.
It is an implementation inventory, not permission to exclude these executions.

The broad result, complete common audit/attribution, capacity, route parity and
all candidate/Linux/load/model/onboarding/control-plane/clearance evidence remain
open. No provider spending, deployment, sibling edit, pin change, commit or push
occurred. All M0–M8 gates remain unqualified; the goal is active.

### Recorder failure coupling in a separate working copy — 2026-09-09

The original 766-case execution **18302** remains live (`262eda`, 46% checkpoint
at 22:18:04 UTC). It is not restarted and its source/docs stay frozen at aggregate
`c7d547ba00422cc0228e1fe8736a43fd7b7e28ef0246d532cb08c4ac84adc4fc`
(`e5bc5a` recheck). The preceding goal turn was a verified wait on this handle,
not completion or a new product-readiness result.

Development continued in a separate source copy,
`/private/tmp/sigil-pi-recorder-audit.Jl8kYpDT`. The copy initially matched that
same aggregate (`e5a7c4`); no files in the original stage were changed. Four native
files now extend opt-in v2 failure publication to actual fixed claim/result
recorder evaluation and ER1-framing failures. This shared boundary is called by
synchronous claim/result recording, owned completion and abandoned recovery.
Subject configuration is bound to the actual admitted `RecorderConfig`, while
reported storage grants come from the actual configured effect/recording scope.
The lookup hash binds intent/claim/delivery namespaces and intent key; the full
actual WR1 input has its separate evaluation digest. Neither hash is authorization
or complete structured actor/operation/read attribution.

The existing grantless SIGIL classifier and private native empty-domain-batch
append are reused. A failure before claim cannot mint a receipt or dispatch; a
failure after send cannot erase the consumed claim or prove remote non-delivery.
The same recorder deadline bounds reporting; no fresh reporting interval or
recursive reporter is introduced. After successful ER1 framing, the observation
is retired before the lifecycle projector runs, so a later formatter/binding/
commit refusal is not mislabeled a failed recorder evaluation. Those later
failures and complete entry/helper/coordinator/no-op coverage remain open.
Audit-v1/unaudited behavior, the classifier/schema, runtime pin, source/registry/
fuel/memory/deadline limits and all original assertions are unchanged.

Verification completed for this new copy:

- Formatting and all-target warnings-as-errors passed (`143184`, 26.72 seconds).
  The attempted lower process priority was refused by the sandbox; cargo itself
  ran normally and exited zero. No permission escalation was needed.
- All **208 native service tests passed**, session 79065, terminal `4a9b45`:
  191 library tests in 27.38 seconds, 15 main and 2 transaction tests. None were
  ignored or filtered. No original fixture, assertion or deadline was relaxed.
- Eight new HTTP/SIGIL cases collected (`259468`); collection alone is not a pass.
  Source whitespace checks returned no diagnostics (`b00ddc`, `96a49f`).

The new focused execution **11546** is running (launch `4928c3`, latest `8a9fa9`),
selecting those eight cases plus the existing v2 normal tool-turn/two-tenant/
follow-up/restart case. It retains the original pinned evaluator and all native
prerequisites. The exact command is stored in this task as
`mvpRecorderAuditIntegrationCommand`; it uses the new copy's test/support paths
with `--noconftest -p conftest -p staged_transaction_regression -p no:cacheprovider
-o addopts= -xq`. This is local scripted-provider testing, not a paid-provider run.
The frozen new-copy aggregate before launch is
`802d56ae2d840fb5ccef5d557bbacadbbbbd65a95a579f95d8ab891478083f77`
(`b03881`), including its initial boundary/evidence document. Keep this new copy's
source/docs frozen until **11546** is terminal as well. The two handles refer to
different source snapshots; neither result can be transferred to the other.

The Cargo locks still match the prior record. Main's aggregate before this
documentation-only update was
`f34c1906ec2319d54554f7f4d15e1366b0d039434f30732395acd9928e3c9163`
(`19f778`). Main still selects v8. No deployment, provider spending, runtime-pin
change, sibling edit, commit or push occurred. Main integration, remaining API
parity, audit capacity/health/retention, Linux/load/restore/model/onboarding,
real control-plane reuse and independent clearance remain open. **All M0–M8
gates remain unqualified; the full goal stays active.**

Recorder follow-up checkpoint at **2026-09-09 22:23:51 UTC**: focused session
**11546 terminated**, exit 1, terminal `6c0169`, with **one setup error in 369.20
seconds**. Its native/evaluator prerequisites completed, but the sandbox denied
the scripted provider's loopback listener. No end-to-end test executed and no
test assertion, source limit or deadline is changed in response. Retry the same
nine-case command with local test-network permission, not a replacement provider
or a bypassed fixture. The original **18302** run remains live (`500d73`, past
75%); keep its source/docs frozen. Neither source has a terminal broad product
qualification from this checkpoint.

### Full evaluation-audit regression passed; recorder extension under investigation

At **2026-09-09 22:32:56 UTC**, original session **18302** completed with
**766 passed in 2,746.78 seconds** (45:46), terminal `f025eb`, exit 0, with no
skipped/xfail behavioral cases. The unchanged earlier 682-case selector and all
84 new evaluation-audit cases passed; the original native, pinned evaluator and
frozen-v8 prerequisites were retained. The staged native census is 355 unique
tests (Store 125, worker 22, service 208). The before/after aggregate matched
`c7d547ba00422cc0228e1fe8736a43fd7b7e28ef0246d532cb08c4ac84adc4fc`
(`d892df` / `3ca4bc`), and the optimized executable hashes and Cargo locks match
the earlier artifact table (`5e2f2d`). The stage's evaluation-observations document
now records the full command and terminal result. Its documentation was updated
only after the run became terminal.

The separate recorder retry **51897** received local test-network permission and
ended with **2 passed, 1 failed in 358.26 seconds**, terminal `f681a6`, exit 1 at
22:31:14 UTC. Both real preclaim failure cases passed, but the new post-send case
saw no journal event and no provider request within its unchanged 15-second
assertion window. The fixture had not reached its intended post-send boundary.
Source stayed frozen at
`20f6ce3dfe86e3a38e3c3368e330efaaf925c9975b4b78a043d76e3e071df3d0`
through that run (`be0816`). No passing post-send/recovery result is claimed.

Four additional compiled fixture-contract probes then passed in **3.26 seconds**
(`6200e7`), proving that the altered SIGIL fixture preserves the original
recorder on its untouched path and fails on its selected path. This narrows the
investigation but does not replace the failing end-to-end case. A separate,
non-qualifying local diagnostic uses the actual previously built recorder-host
artifact; the production source, required tests and deadlines remain unchanged.

Read-only integration checks confirmed that main's three native crates exactly
match the frozen v8 source (`5a2209`, `249df8`) and every overlapping staged
application/script/test file matches main (`c45c85`). This permits a careful future
integration review; no files have been promoted yet. Main still selects v8.
All M0–M8 gates remain unqualified and the full goal stays active.

The non-qualifying local reproduction completed (`cb9c3d`) against recorder-host
SHA-256 `448892370a6b73ac63223591fc81542bcdbbc8de8ef15b98818b5e7f26340bad`.
It observed one scripted provider request, one consumed claim, no delivery and
`prepared` followed by two actual evaluation-failure events. This establishes
that the intended boundary can be reached on that artifact, but it neither
explains nor clears the earlier end-to-end failure. The new test's failure message
now includes the actual first native diagnostic and claim/delivery counts, with
the same 15-second observation window and unchanged assertions. No production
source was changed for that additional visibility.

The complete focused rerun **65075** (launch `6b08f9`) is now running with local
test-network permission: the original v2 normal two-tenant tool/follow-up/restart
case first, then all eight recorder cases and four compiled fixture-contract
checks, **13 cases total**. It retains the same original native/evaluator
prerequisites. Source/docs in the recorder copy stay frozen at
`0f730baca9b02fd97abb2ad1304542cd02b0e428a92ac254e7a2eb1f0c459089`
(`fa2986`) until this handle is terminal. No new passing focused result is claimed
yet. The previous 766-case stage has no live job; its post-result documentation
aggregate is `dff4fd190463124a896af7cd25cb4479216046a1e2dacc511e0cbafa2309e5bf`
(`715227`), distinct from its tested `c7d547...` snapshot.

## Follow-up: focused recorder rerun passed; optional v9 integrated

On 2026-09-09, session **65075** completed with **13 passed in 592.40 seconds**
(9:52), exit zero, terminal `a25998`. The original normal v2 two-tenant
model/tool/follow-up/restart case, eight recorder failure/recovery/compatibility
cases and four compiled fixture-contract checks all passed. Required native and
pinned evaluator prerequisites were retained. The before/after source aggregate
matched `0f730baca9b02fd97abb2ad1304542cd02b0e428a92ac254e7a2eb1f0c459089`.
The [recorder evidence](recorder-evaluation-audit.md) retains executable hashes,
the earlier failed attempt and investigation limitations. No timing root cause
has been established; a focused rerun is not operating-envelope qualification.

The integration source at `/private/tmp/sigil-pi-recorder-audit.Jl8kYpDT`, after
its terminal-result documentation updates, had whole-stage aggregate
`b287df385e6e8f19fea40901d0658c79788cc8060c567537807d4ee741612714`
(`b1e271`). This is a post-result documentation snapshot, not a new tested artifact.
Immediately before integration, main's service/store/worker sources again matched
the exact frozen v8 baseline (`96a5c2`). Each changed destination was checked
against its recorded old hash before applying a patch; unrelated files and the
existing root README were not replaced from staging.

**161 files were integrated: 142 new files and 19 native-file updates.** A subsequent
byte comparison found every one of the 194 overlapping staged files identical
(`4e97af`). This includes new SIGIL fragments/builders, native mechanisms, all new
tests and the full frozen-v8 compatibility source/manifest. The root conftest,
whole-tree test selector, coverage thresholds, compiler/source/worker limits,
runtime pin and legacy application inputs are unchanged. Existing v8 configuration
behavior is retained; this adds support for an explicit v9 development profile,
not a selected production profile or pilot deployment. Subsequent documentation
edits distinguish this integration from the preceding stage-only evidence.

Whole-repository F/E9 lint and tracked whitespace checks passed (`e20374`).
Collection of the full test tree completed (`148deb`); collection is not a test
pass. The complete unchanged `ci.sh` source gate is next, against the pinned
private source checkout with scripted local providers and mandatory native/browser
prerequisites. No old 766/2,702 result is transferred to the changed executables.

No commit, push, external deployment, provider spending, runtime-pin change,
sibling-repository edit or cross-project message is included. Common execution
journal coverage/attribution, audit reservations/health/retention, all remaining
API routes and the candidate/Linux/load/restore/model/onboarding/control-plane/
independent-review requirements remain open. **Every M0–M8 gate remains
unqualified and the full goal remains active.**

### Integrated full-source gate launch preparation

The unchanged whole-tree selector collects **3,635 tests** (`0bb588`), compared
with 2,702 before this integration. Tracked whitespace and all 512 untracked
source-file whitespace checks passed (`ed4e8d`). Generated Cargo outputs from
the new frozen-v8 fixture are ignored exactly like the v4/v6/v7 fixture outputs;
its source, lockfiles and manifest remain tracked acceptance inputs.

The default local browser dependency lookup was unavailable. The same supported
exact-version installation used for prior browser evidence was then selected
explicitly: `PI_PLAYWRIGHT_DIR=/private/tmp/sigil-pi-browser-clean-install.LDlIoWgu/node_modules/playwright`.
The runtime preflight passed (`bcf823`): Node 26.4.0, Playwright 1.62.1 and the
installed Chromium executable. No browser fixture or version check is bypassed.
The SIGIL checkout is still the clean pinned revision
`8277a1d92d599df89e6b4391fc70fd0fa534d696` (`fa34d6`).

Planned complete source-gate invocation from the repository, with local scripted
provider/browser network permission only:

```sh
env -u CARGO_TARGET_DIR -u PI_FORGE_BIN -u PI_TOOLCHAIN_DIR -u PI_STDLIB_DIR -u ANTHROPIC_API_KEY -u OPENAI_API_KEY -u SIGIL_ALLOW_UNVERIFIED_CERT PYTHONDONTWRITEBYTECODE=1 SIGIL_ROOT=/private/tmp/sigil-pi-pinned-8277a1d9.9NXtMw PI_REQUIRE_TOOLCHAIN=1 CARGO_NET_OFFLINE=true CARGO_BUILD_JOBS=2 PI_PLAYWRIGHT_DIR=/private/tmp/sigil-pi-browser-clean-install.LDlIoWgu/node_modules/playwright ./ci.sh
```

This runs every original source/build/compile/lint/native/browser/test/coverage
gate without a staged fixture override or enumerated selector. The existing
platform-only skip and research-only xfail must remain explicitly reported;
unexpected failures are not to be reclassified to pass the run. Keep main source
and documentation frozen while it runs, record the launch and terminal handles,
and compare source fingerprints before updating this record with the outcome.

## Full-source terminal result and service-info/browser integration — 2026-09-10

The preceding goal-definition response restated the existing goal and was not
implementation progress. This continuation resumes the available safe work;
the full M0–M8 scope and acceptance thresholds are unchanged.

The exact full-source command recorded above launched as session **78811** at
**2026-09-09 23:04:53 UTC** (`6383e7`) and terminated with **exit 1**, observed
at **2026-09-10 01:10:54 UTC** (`4ae034`). Toolchain pin/rebuild, generated source
and compile gate, and F/E9 lint passed. Pytest reached 100% of its 3,635-case
collection. Its sole reported failed test was
`tests/test_guards.py::test_readme_test_count_is_current`: the README claimed
2,701 tests plus one xfail while 3,635 cases were collected. The existing
research-only xfail and macOS platform-only skip remain; the quiet terminal
output did not provide a full passed/skipped total, so none is invented here.

The source aggregate matched before launch, at terminal, and before integration
(`9fab6f` / `e9a590` / `a3d21a`):
`516ea6f6e65c9c0861ec9fc67208f47d53f571964814df251afa237827598e2e`.
The run also reported an unclosed SQLite connection ResourceWarning while
`test_npm_shape_dep_walker_matches_reference` ran. The warning surfaced in
Hypothesis; its allocation/root cause is unproven. It is retained as an unresolved
diagnostic, not mislabeled as another failed test or as fixed by a rerun.

Because `ci.sh` stops at a failed command, its coverage check did **not** run.
After terminal, the original `.coverage` was preserved unchanged and copied to
`/private/tmp/sigil-pi-main-ci-evidence.Ezc8iLeP/coverage.sqlite`. Both had SHA-256
`0668dae110040bedea50db1202a79b6bf1cc30dffa94d79e1469bba518af5e93`
(`b4f57b`, rechecked `a3d21a`). The report was generated from that copy, then the
exact checker arguments extracted from `ci.sh`, including every original
critical selector, were run with only the report path substituted. This separate
diagnostic passed (`4dfe54`): **91.23% line, 85.98% branch**, with all original
100% critical-boundary requirements passing. The report SHA-256 is
`457cbc903f1a9e9f66312211540c14eb6c95dbd82c043ecff6b8cdbfcb7d0add`.
This is the original Python-module coverage scope, not SIGIL/native/browser
coverage and not a CI PASS.

After terminal, 13 service-info/browser paths were integrated from
`/private/tmp/sigil-pi-service-info.k2A6Qxmh`. Every old destination and new source
was checked against the recorded SHA-256 preconditions (`838448`) before any
patch; all 13 resulting destination files matched the staged bytes (`bb0545`).
The new files implement SIGIL-owned legacy health/version responses with durable
request accounting, plus their composition and actual HTTP/browser tests. The
browser now distinguishes unknown totals from positive reported subtotals;
amounts, token holds, delivery uncertainty and API semantics are unchanged.
No native import, production Python policy, broader grant or increased ceiling
was introduced. Details and original failures remain in the
[service-info evidence](service-info-migration.md).

The original staged **85-case** run and later **12-case** browser rerun remain
distinct, source-bound targeted passes. They are not an 86-case common-source
result or an integrated source-gate pass. In main, the entire original selector
now collects **3,721 tests**; F/E9 lint and all **100 JavaScript tests** passed
(`873b24`). The README count is corrected to 3,720 plus its existing one honest
xfail. The exact-count guard, full test selection, coverage thresholds, native
prerequisites, runtime pin and test deadlines are unchanged. The historical
launch-preparation block was moved intact into chronological order above this
terminal checkpoint; no failure record was removed.

The separate native deployment stage is now running its corrected **57 cases**,
session **98198**, launched at **2026-09-10 01:20:40 UTC** (`f0470a`). Its source
and documentation are frozen at
`a6d16625e630b219a2964df85c533dacb29f69aff1280350654125b27b004bb6`
(`623458`). It retains the original real native/evaluator/browser prerequisites,
52 build-time checks and five actual copied-artifact scenarios. The earlier
49-case run failed before copied-host launch because its fixture compared worker
dictionary order instead of named identities; that result and the destination
validation regressions are retained in the staged deployment document. No
copied-deployment or clean-Linux qualification is claimed while it is running.

Next: verify the unchanged README guard, preserve the deployment terminal result,
integrate only verified deployment changes, and run the complete original `ci.sh`
against the integrated snapshot. Common execution journal coverage/attribution,
audit reservations/readiness/retention, the remaining route implementations and
all candidate/load/restore/live-model/onboarding/control-plane/review work remain.
No external deployment, provider spending, commit/push, runtime-pin change or
sibling-repository change occurred. **All M0–M8 gates remain unqualified.**

Post-integration check: the unchanged README exact-count guard passed **one case
in 1.04 seconds** (`994d83`), and tracked whitespace checks passed (`449dc2`).
The captured original full-CI launch/terminal output, exact command, timestamps,
source identity and separate coverage diagnostic are preserved in
`/private/tmp/sigil-pi-main-ci-evidence.Ezc8iLeP/run-record.json`. This record is
explicitly a partial raw log, not the missing intermediate transcript or a new
passing run. The original coverage database remains separately retained.

## Copied deployment passed and integrated — 2026-09-10

The preceding goal turn made implementation progress: it integrated the 13
service-info/browser paths, verified lint and 100 JavaScript tests, corrected the
README count with the unchanged guard passing, and preserved the failed full-CI
evidence. This continuation kept the separate deployment source frozen until its
actual process reported completion; no observation timeout triggered a restart.

Session **98198** passed **57 cases in 469.82 seconds** (7:49), exit zero,
terminal `4e448a`. All 52 build-time cases and five actual copied-runtime cases
passed with the original native/evaluator/browser prerequisites and no skips or
xfails. The source/documentation aggregate matched before and after (`623458` /
`323000`): `a6d16625e630b219a2964df85c533dacb29f69aff1280350654125b27b004bb6`.
The [deployment evidence](native-deployment.md) retains its exact command,
artifact identities, earlier failures, test scope and remaining release work.

Each normal/lost-ack bundle has 26 verified artifact files and 34 unique worker
roles. Read-only post-run inspection confirmed every recorded file digest, length
and mode, the missing original generated-input path, empty launch working
directories and both v2 audit publishers for both tenants (`794caf`). Basic
tenant A/B claim-delivery counts are 5/5 and 1/1 with 10/2 effect events; lost-ack
tenant A has 3/3 with six events, while tenant B has none. The original tests
verify signatures, unchanged restart state and no extra provider work. Tampered
entry/runtime/browser bytes fail actual native admission before state creation.

Browser-verification guidance prompted inspection of the connection, completed
model/tool response, mobile retained-history and lost-ack screens. The mobile
capture includes a still-pending status check; the image is not proof of a
settled operation. Completion, direct-API continuity, explicit same-key retry,
tenant isolation and restart are established by separate actual test assertions.
The unavailable named browser CLI was replaced by the existing pinned driver,
which checks initial rendering, keyboard controls, console and foreign requests.

The four deployment builder/test/documentation files were then integrated under
exact source hashes and absent-destination checks. All four copies matched
staging (`794e30`) before subsequent documentation updates. Main now collects
**3,778 tests**, whole-tree F/E9 lint passes, and the README claim is 3,777 plus
the existing research-only xfail. No original test, fixture prerequisite,
coverage threshold, source/fuel/deadline ceiling or runtime pin changed.

A read-only environment check found a local Docker engine reporting Linux
aarch64 (`d18201`) and two cached Python images, both Linux arm64 (`f7811a`).
This is **not** evidence of the required Linux x86_64 environment, native build
or clean candidate install. A separately listed stopped arm64 VM belongs to
another project and was not started or modified. No containers or images were
created, changed or deleted in these checks.

Next is the complete original `ci.sh` on the integrated snapshot, without a
targeted selector, fixture shortcut or lowered gate. Keep source and documentation
frozen while it runs. Local staged packaging does not supply a product-approved
configuration, Linux qualification, provenance/SBOM, protected hosted CI, restored
candidate, real-model usefulness, independent onboarding, actual control-plane
reuse or pilot clearance. All common-journal/attribution/capacity/retention and
remaining API-route obligations persist. **All M0–M8 gates remain unqualified;
the full goal remains active.**
