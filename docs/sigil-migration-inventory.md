# SIGIL-native migration ownership inventory

Baseline: sigil-pi `e20abd0739c36b52abb5616411946cb0d9151360`; current local
implementation checked 2026-09-08. Status: **PARTIAL LOCAL SERVICE — production not migrated**.
This supports M0/M1 of [the MVP goal](mvp-goal.md), with the
[API route inventory](../config/api-migration.json) and
[execution contract](execution-contract.md). Proposed owners below are design
assignments unless an actual source is linked. Components are not production parity.

## Product decisions that must move

| Decision | Current reference implementation | Required destination / preserved boundary |
|---|---|---|
| API fields, route semantics, status/error mapping | `ProductService.dispatch`, `_json_object`, `_chat_request`, `_validate_session`, `_validate_message` in [product_service.py](../product_service.py) | [api.sigil](../app/pi/api.sigil) owns real HTTP operation submission, lookup, cancellation and retained-history retrieval, including permissions/errors. The additive [listing_api.sigil](../app/pi/listing_api.sigil) supplies discovery in explicit v5/v6 profiles; v3/v4 retains its unchanged API contract. The automatic service executes accepted turns. The development browser uses this same API. Other handlers still return 501; all eleven legacy route migrations and complete browser qualification remain |
| Credential validity policy, principal/tenant interpretation, scopes and allowed tools | `AuthRegistry`, `ProductService._require`, `_allowed_tools` | [api.sigil](../app/pi/api.sigil) validates bounded matched facts, bootstrap scope/tool consistency, validity intervals and route permissions, and selects command time bounds. The native host supplies digest matching, independently scoped storage and generic time comparisons at action initiation; complete authority/tool lifecycle and old-configuration conversion still need qualification |
| Tenant/session identity and same-session concurrency | `_internal_session`, `SessionOperationLocks`, `PiAgent.sandbox_for`, `_session_lock` | [admission.sigil](../app/pi/admission.sigil) binds tenant/session state and rejects active or unresolved reuse; the native store enforces one owner and revision-checked commits. Operation lookup/cancel also require the accepting principal. Final sharing/recovery policy and supported-topology qualification remain |
| Request/turn admission, reservations, settlement and unknown usage | `DurableQuotaStore` | [admission.sigil](../app/pi/admission.sigil) publishes reservations, operation/state/intent and deduplication atomically. [settlement.sigil](../app/pi/settlement.sigil) publishes terminal results and settles capacity while retaining unknown/overrun token holds. The automatic service invokes dispatch, never-claimed finalization and settlement. Prospective metering, cumulative charged usage, rate windows and full quota parity remain |
| Conversation state, compaction and output clipping | `SessionStore`, `compact`, `clip_tool_result` in [agent.py](../agent.py) | [turn.sigil](../app/pi/turn.sigil) serializes bounded state and completed-turn follow-ups. Its start and result interpretation run through actual admission and [turn_completion.sigil](../app/pi/turn_completion.sigil); [history.sigil](../app/pi/history.sigil) projects revision-bound retained-message pages. [listing.sigil](../app/pi/listing.sigil) selects visible session names and exact record revisions from native metadata, with live-scan pagination. Compaction/clipping, export/delete and complete recovery parity remain |
| Model request assembly, tool selection, multi-step sequencing and stopping | `PiAgent._turn_locked`, `_llm`, `_parse`, `tool_specs` | [turn.sigil](../app/pi/turn.sigil) makes these decisions in the real automatic HTTP model/file/model/follow-up path. [coordinator.sigil](../app/pi/coordinator.sigil) selects the mechanisms and continuation. Full configured tool/route coverage, changed-authority handling and quota parity remain; the legacy Python product is unchanged |
| Tool argument framing, bound operator arguments and pipeline sequencing | `PiAgent._dispatch` | [read_request.sigil](../app/pi/read_request.sigil) supplies grantless read-file preparation, and [dispatch.sigil](../app/pi/dispatch.sigil) binds actual payload/artifact/grants before the separately scoped automatic file worker. Other tools/pipelines and complete boundary qualification remain |
| Business retry and recovery decisions | `PiAgent._llm`, `_llm_error_is_transient`, `_turn_locked` | [turn.sigil](../app/pi/turn.sigil), [coordinator.sigil](../app/pi/coordinator.sigil) and shared completion policy preserve uncertain delivery without a hidden resend in actual restart/cancellation tests. The full interruption matrix, changed/removed policy reconciliation and legacy retry-compatibility review remain |
| Schedule eligibility, principal rechecks and run policy | `ProductScheduleStore`, `ProductScheduler` | SIGIL scheduling policy; timer and claim/commit primitives remain generic; ordinary turns retain ordinary authorization/quota checks |
| Tenant-scoped export, deletion and retention | `ProductDataManager`, `ProductRetentionMonitor` | SIGIL data-lifecycle policy covering transcripts, operations, reservations, delivery records, audit and tombstones |
| Audit meaning, redaction and product readiness decisions | `AuditLog`, `redact_grants`, `AuditVerificationMonitor`, `ProductService.readiness_snapshot` | SIGIL chooses semantic records/readiness policy; native signing, resource measurement and verification mechanisms remain narrow and secret-safe |
| Bootstrap policy validation and draining | [product_main.py](../product_main.py) `run` | SIGIL application admission/configuration policy plus host-independent ceilings and mechanical lifecycle supervision; no Python production entry point for a supported turn |

The product currently rejects the optional memory sidecar at bootstrap. Its research
recall/consolidation path is not silently added to this pilot. Any future addition
needs its own ownership, data-lifecycle and authorization evidence. Research `/chat`
is likewise not a substitute for authenticated product `/v1` parity.

The [audit migration inventory](audit-migration-inventory.md) separately maps the
reference execution-audit boundary to the staged state-publication, worker and
dispatch-provenance mechanisms. It explicitly retains missing refusal/failure,
execution inventory, capacity, retention, integrity/readiness and route coverage;
these mechanisms do not yet establish audit parity.

## Shared execution versus native mechanisms

The first shared SIGIL source is
[`app/shared/delivery_state.sigil`](../app/shared/delivery_state.sigil). It only
selects the next delivery phase. It has no host imports or I/O grants, and cannot
authenticate an event, commit a record or cause an effect. The 53 component checks
are not an implementation of the rest of this table.

| Responsibility | Shared SIGIL execution layer | Native host mechanism |
|---|---|---|
| Intent and delivery records | Versioned strict encoding, correlation and generic state transitions | Bounded reads and crash-durable atomic commit/claim primitives |
| Effect dispatch | Select an eligible committed intent and request its bound execution | Admit the exact artifact; resolve and enforce the opaque scoped grant; instantiate an isolated worker |
| Cancellation/restart | Preserve uncertainty and decide which records need application interpretation | Stop/reap the owned worker; invalidate/fence its authority; never silently replay a request |
| Observation | Store the exact attempt-bound delivery facts | Bounded single-attempt effect with status/body/usage facts or explicit unknown delivery |

The existing native boundary includes `http_get`, `http_post`, `http_post_hdrs`,
`http_post_secret`, `kv_get`, `kv_put`, `kv_delete`, `fs_read`, `fs_write`, and
`fs_list` declarations across authored/composed tools. These are an initial import
family inventory, not an approved whole-application import manifest: declarations
can be introduced by composition and are not equivalent to reachable calls or
granted authority. Qualification must enumerate the actual compiled imports for
every admitted artifact, compare them with the frozen manifest/host contract, and
exercise each granted/denied boundary. Memory allocation/load/store intrinsics do
not confer external I/O authority.

The repo-local [native store](native-store.md) now supplies a candidate transaction
mechanism over opaque records, with exact namespace scopes and atomic revision
checks. It is separate from the pinned `kv_*` imports and now backs the development
API's authenticated admission, not yet a complete admitted effect-execution contract.
Its stdio adapter parses storage
requests only, not product HTTP routes. The [SIGIL transaction producer](turn-transactions.md)
now constructs application state/intent batches and delivery read preconditions in
the durable integration fixture. Shared storage encoding stays separate from pi
observation/transaction policy. The [shared SIGIL executor](executor-transactions.md)
now constructs claim/delivery commits from the same transition kernel. The
[native worker bridge](native-worker.md) freezes source/input/grants, issues one-use
in-memory tickets, starts fresh runtime processes and reports bounded execution facts.
The durable fixture now uses a [native claim gate](claimed-worker.md) that requires
actual scoped intent reads, matching SIGIL-produced claim/generation bytes and its
own successful commit acknowledgement before one-use execution. Retained claims and
tombstones prevent new preparation at that coordinate after reopening. Registry
selection and result routing in this older conformance path remain fixture responsibilities. The newer
[SIGIL dispatch policy](dispatch-policy.md) validates original/current authority,
operation/state/reservation correlation, exact worker artifact/grants and input;
its native binder supplies actual scoped reads, clocks and non-secret held-worker
facts, with no native application-record interpretation. This path still uses
operator-installed bindings. The opt-in [v4 service](automatic-service.md) now connects
them to automatic SIGIL dispatch; full production migration cannot yet be claimed.

The newer [worker completion component](worker-completion.md) classifies native-bound
observations and abandoned claims in shared SIGIL, reusing the exact executor/kernel.
Its native recorder owns actual snapshots, one-use execution, claim/result receipts and
mechanical transaction binding. Version 3 forbids caller-authored outcomes/commits;
native code does not choose pi recovery or retry decisions. The new
[terminal settlement](settlement.md) uses a generic fixed transaction host to bind
actual reads/time to SIGIL's terminal operation and reservation decisions. Its native
code treats two returned context strings as opaque and checks exact snapshot/write
coordinates; it does not construct or interpret domain records. Actual delivery
interpretation and settlement are exercised in recorded full-turn fixtures and now
in the automatic v4 service.

The [native application host](native-service.md) now binds actual HTTP request bytes,
matched immutable credential facts, its own scoped storage observations and SIGIL
continuations in one owned request loop. SIGIL now calls a separately verified,
grantless admission function to produce one operation/dedup/state/intent/reservation
transaction, alongside response semantics and action time bounds. The native host checks those
opaque bounds after worker execution, before starting an action; it does not choose
credential or turn-expiry policy. This supplies an actual API admission path, not
authenticated execution of the existing durable-turn fixture: admission is connected
to reservations, conversation state and initial intent. The native claim mechanism
is separately exercised with an HTTP-admitted intent. The v4 coordinator now invokes
actual effects, delivery interpretation and settlement. The API reads retained
terminal results and gates follow-up admission on the matching terminal operation.
Function registration is fixed,
its result is data, and bootstrap has no storage handle. No product decision was
moved into a native route or record-construction implementation.

Automatic ownership additions: `app/pi/coordinator.sigil` selects discovery, aliases,
polling/recovery and transaction invocation; `app/pi/turn_completion.sigil` derives
the reducer event from actual snapshots and proposes the next atomic state/intent
commit. `native/service/src/automatic.rs` owns bounded registries and executes their
mechanisms, without parsing pi phases or record schemas. These paths have local
source evidence, not complete route parity or M1 qualification.

Never-claimed finalization: [preclaim.sigil](../app/pi/preclaim.sigil) now invokes
the exact settlement producer over a proposed terminal state and publishes all four
records in one revision-bound transaction, without inventing a claim or delivery.
[model_allowance.sigil](../app/pi/model_allowance.sigil) is the single shared
unknown/exhausted model-allowance rule used by dispatch and this finalizer. The
coordinator's current LB3 binding chooses its cancellation-aware UF2 extension before an unclaimed start; the
native delegation change supplies only read access to the existing claim namespace.
Changed/removed policy remains outside this addition.

Public cancellation: [cancel_api.sigil](../app/pi/cancel_api.sigil) owns the route,
body/owner/scope checks, exact CR1 transaction and response semantics;
[cancel_records.sigil](../app/pi/cancel_records.sigil) validates the bound request.
[dispatch_cancellable.sigil](../app/pi/dispatch_cancellable.sigil) reuses DF1 and
checks the actual cancellation snapshot at dispatch. The UF2 wrapper shares the
unchanged-policy finalization/accounting core. SIGIL's coordinator selects the held
generation, requests native cancellation and polls actual completion; no native
source changed for this slice. A `chat`-scoped terminal cancellation returns control
metadata only, not a result requiring `sessions:read`. Full boundary qualification
and full product/browser parity remain open; retained history and initial browser
progress/cancellation paths now have local development evidence.

Compile-once mechanism: the [fixed grantless evaluator](fixed-evaluator.md) is now
selected for API, admission, history and coordinator functions in the automatic development
configuration, plus listing in the explicit v6 discovery profile. The v5 discovery
profile retains fresh-process function execution. The fixed evaluator retains
freshly verified code only and supplies a fresh instance
for every current input; no application decisions or authority are cached. Other
policy/transaction/recorder and effect workers retain their previous runtime. This
is not compiler-free packaging, a new runtime pin, AIN adoption or candidate admission.

Browser transport: [web/api.mjs](../web/api.mjs) sends bounded same-origin requests;
[web/app.mjs](../web/app.mjs) renders text and manages view-local request generations.
The browser holds credentials only in memory and can explicitly retry the identical
submission identity/payload after a lost acknowledgement. It does not choose tools,
authorize work, interpret delivery records into application transitions, or run an
agent loop. The native [public asset inventory](../native/service/src/public_assets.rs)
validates and retains bounded static bytes before state startup, enforces the local
HTTP origin boundary, and leaves product requests with SIGIL. The Python manifest
builder and browser fixtures are build/test tooling, not a production API. See
[browser limitations and evidence](browser-interface.md). The integrated 2,265-case
local source gate passed. Four more browser regressions then passed in a combined
21-case staged/integrated run and were added to the repo without application or
native changes. The expanded 2,269-case full-source gate then passed on
2026-09-09 UTC, including all 21 integrated HTTP/browser cases. The broader
M4 matrix, complete legacy API migration and candidate qualification remain open.

## HTTP metadata integration

The [SIGIL correlation policy](../app/pi/http_request_id.sigil) interprets only
native-selected request-ID facts. The [SIGIL response wrapper](../app/pi/http_api.sigil)
preserves original bodies, statuses, commands and guards while adding its selected
header. The [native mechanism](../native/service/src/http_exchange.rs) admits
bounded, explicitly inventoried metadata without route, quota or credential-validity
policy. [Build composition](../scripts/compose_http_entry.py) is not a Python
runtime API. All older compiled API inputs remain unchanged.

The isolated 154-case run passed, including all 21 real HTTP/browser cases;
integrated whole-source qualification is pending. Body/log correlation parity and
all eleven legacy routes remain open. See [the HTTP contract](http-exchange.md)
and [acceptance evidence](mvp-acceptance.md).

## Completion discipline

For each decision row, add the actual SIGIL source and executable positive/negative
parity evidence before marking it migrated. Before retiring a Python-bound test,
identify the replacement SIGIL/service test covering the same property. Complete
the native-import inventory from actual compiled artifacts, including composition;
source scanning alone cannot establish admission safety.

Transport, build, packaging and test tooling may remain native/Python where they do
not implement supported runtime product decisions. The browser may present state
and transport requests, but must not become an alternate authorization or agent
implementation. Neither an executable pure kernel nor a SIGIL API facade over
`PiAgent` satisfies M1.
