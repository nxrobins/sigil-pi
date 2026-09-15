# SIGIL-native sigil-pi MVP goal and acceptance criteria

Established: 2026-09-07. Status: **ACTIVE — defined, not achieved**.

## Goal

Deliver a pilot-ready sigil-pi MVP: one permission-scoped workspace assistant,
usable through a first-party browser chat and a documented API, with API behavior,
conversation orchestration, and application policy implemented in SIGIL. Both
interfaces must use the same application and durable action/result contract.
Demonstrate that one control-plane workflow can reuse that execution contract
without introducing either application's domain policy into the native host.

Use the pinned, verified Wasmtime-based SIGIL execution path first. Keep the
contract suitable for evaluation by a future AIN backend without claiming that
such a backend exists, is compatible, or is required to finish this MVP.

Completion means **every M0–M8 criterion below is PASS on the qualifying candidate**.
Writing this document, implementing a demonstration, or passing source tests alone
does not complete the goal. This is an invite-only pilot target, not v1.0 general
availability: [the existing GA record](product-readiness.md) and `product-ci.sh`
remain separate and are not weakened or replaced.

## Bounded product scope

- First supported task: inspect explicitly permitted workspace files, answer a
  question grounded in their contents, and answer a follow-up using the retained
  conversation. The default acceptance task is read-only; no shell or unrestricted
  filesystem/network access is introduced. Existing permitted mutation tools still
  need authorization, recovery, and compatibility tests if shipped.
- Browser chat and API are equal clients of one service. Users can start, find,
  reopen, and continue their conversations and see task state and actionable errors.
  A frontend proxy may transport requests, but cannot implement separate product
  authorization or agent behavior. Streaming tokens are not required; bounded
  polling or progress events are acceptable.
- One Linux x86_64 deployment, one application worker, local POSIX durable storage,
  and supervised isolated execution. At least two separately authenticated tenants
  must be exercised. This does not claim multi-host or multi-worker support.
- One explicitly selected, tested model/provider configuration. Model access,
  final quotas, retention periods, and operational ownership must be recorded
  before qualification; they are not silently chosen by a benchmark.
- sigil-pi remains independently usable. Running the control-plane application is
  not a prerequisite for standalone chat/API access.
- Preserve the existing product API's chat, schedule, session export/delete, and
  operations behaviors through a route-by-route migration matrix. New conversation
  discovery/history and operation-status behavior must be specified. An intentional
  breaking change requires an explicit version/migration decision before release;
  features cannot disappear by being omitted from tests.

## Ownership boundary

| Layer | Owns | Must not own |
|---|---|---|
| Native SIGIL host/runtime | Artifact admission, isolated execution, transport, storage atomicity/durability primitives, credential/secret mechanisms, clocks, metering/reservation mechanisms, resource ceilings, cancellation mechanics | Conversation choices, company/agent/assignment rules, application authorization decisions, tenant allowance policy, business retries or recovery transitions |
| Shared SIGIL execution layer | Versioned durable intent/delivery protocol, operation correlation, generic dispatch/recovery bookkeeping using the host primitives | Either product's domain state or a grant that silently combines every tool's privileges |
| sigil-pi SIGIL application | API shapes/validation, identity interpretation and authorization policy, tenant allowances, conversations/context, model/tool sequencing, stopping, scheduling policy, result interpretation and recovery decisions | Direct unrestricted host access or trust in caller/model-supplied authority |
| Control-plane SIGIL application | Its own identity/authorization, companies, agents, assignments, budget/scheduling policy, work history and operator behavior | sigil-pi conversation internals or control over standalone pi's application policy |

The host may reject an invalid artifact, unauthorized capability use, malformed
transport, unavailable storage, or exhausted independent resource ceiling. Runtime
facts do not require native product policy: SIGIL may interpret current time,
credential facts, reservations, and observed delivery results. Raw provider secrets
remain outside guest memory. Moving a rule from Python to Rust is not a SIGIL migration.

## Success criteria

All statuses start **NOT QUALIFIED**. Existing implementation/tests are reusable
baselines, not evidence that the migrated product has passed these gates.

### M0 — Frozen contract and explicit scope

- A versioned execution contract defines operation and attempt identities, payload
  binding, authenticated authority provenance, tenant scope, deadlines, dependencies,
  intent/state commitment, dispatch, result recording, cancellation, and recovery.
- The storage contract states which transitions are atomic, what acknowledgement
  guarantees, and how a crash between writes is recovered. Two independent writes
  cannot stand in for an unproved transaction. Concurrent dispatch is either safely
  claimed/fenced or explicitly excluded and rejected by the supported topology.
- Admission/revocation rules specify dispatch-time checks, policy epochs or equivalent
  evidence, expiry during a running operation, and the limits of cancelling external
  effects. Do not promise live revocation if the selected policy uses restart rotation.
- Freeze the API migration matrix, fixture/rubric set, provider/model, numeric quotas,
  retention policy, topology, and evidence procedures before qualification runs.
  Record product-owner approval for the pilot profile. Later changes require explicit
  versioning and rerunning affected evidence, not lowering thresholds after a failure.

### M1 — SIGIL owns the actual product

- Every supported product API route's request/response semantics and every agent-loop
  product decision execute SIGIL code. Browser and direct API requests reach this same
  path. No Python API or Python agent loop is required for a supported production turn.
- An ownership inventory identifies each product decision and the SIGIL source that
  implements it; it also inventories all native host imports and their contracts.
  Native code and the browser contain no alternate domain-policy implementation.
- Contract fixtures cover every migrated route: success, malformed input, denied
  access, resource exhaustion, and applicable state transitions. All pass against the
  real new service. The Python implementation remains a reference/test oracle during
  migration, with intentional differences recorded rather than hidden.

### M2 — Durable, truthful action execution

- A real SIGIL-driven turn completes model call -> approved tool -> returned tool
  result -> final response, committing resumable state at the specified boundaries.
  Tools execute in fresh isolated instances with separately scoped grants.
- An operation's identity is bound to immutable payload and authority context. Repeated
  acceptance of the same identity/payload does not duplicate work within the documented
  deduplication window; the same identity with a changed payload is rejected. Late,
  mismatched, or duplicate results cannot advance another operation or newer attempt.
- Fault tests stop execution before intent commit, after commit/before dispatch, after
  dispatch/before result commit, and after result commit/before client acknowledgement.
  Each recovered state matches the contract, with no lost acknowledged state or silent
  repeat of an uncertain external effect.
- Distinguish definitely not dispatched, observed response, and possibly delivered.
  An HTTP response is an observation, not proof of a successful business effect.
  Cancellation acknowledgement distinguishes stopped local execution from unknown remote
  outcome. No claim of universal exactly-once external delivery is permitted.

### M3 — Security and resource isolation survive migration

- Automated adversarial tests pass for every API/state surface using at least two
  tenants with identical external session names. No cross-tenant read/write, unauthorized
  tool invocation, forged authority, or provider-secret exposure is observed. Rejected
  credentials cannot trigger billable work. Model output never confers permission.
- Grant widening, artifact/proof/host-contract mismatches, unapproved modules, and invalid
  bootstrap values fail admission. Dispatch cannot exceed the authorized operation's
  scope, expiry rules, or host ceilings. Each native primitive is tested at its boundary.
- Request/turn/token/storage/audit/schedule limits, retention, signed audit verification,
  health/readiness and hard deadlines continue to work. A killed worker cannot permanently
  deny another tenant service. No broad grant, guest-held-secret mode, or research proof
  assumption may replace the existing product safety boundary.
- Expiry/revocation and quota exhaustion are tested before dispatch, during execution,
  and across restart, according to the explicitly frozen policy. All safety cases must
  pass; successful task scores cannot average away a security failure.

### M4 — Both interfaces are usable and genuinely equivalent

- Browser users can authenticate, start/find/reopen a conversation, submit a task, see
  progress and tool outcomes, and handle permission, quota, timeout and uncertain-result
  states. Refresh/reconnect does not silently submit the same action again. Core controls
  work with a keyboard; automated browser tests exercise the complete service path.
- The documented API supports the same task and state lifecycle with runnable examples,
  stable error/status semantics, operation correlation, retry/cancellation guidance,
  and tenant-scoped history/export/deletion. The UI uses these same supported operations.
- Freeze 20 task prompts and expected factual outcomes over a controlled workspace before
  live-model runs. All deterministic integration fixtures pass. With the named real model,
  at least 18 of 20 tasks succeed through EACH interface, including tool use and follow-up
  continuity; no unauthorized action or cross-tenant result is allowed. Report both passes
  and failures, actual tool traces, usage, and latency. Mock-provider results do not satisfy
  this live-model criterion.
- Two people who did not implement the feature each complete the first supported task
  through both interfaces using only the supplied onboarding instructions, within
  15 minutes per interface after receiving access. Record assistance and failures; code
  edits or developer-only intervention fail this criterion.

### M5 — Recovery and a bounded operating envelope

- Candidate-bound tests pass all six existing fault categories: runtime crash, provider
  outage, full disk, corrupt state, network failure, and interrupted writes. Adapt expected
  behavior to the frozen SIGIL contract without dropping state-integrity or fail-closed
  assertions. Exercise M2's per-action crash points in addition to these categories.
- A 30-minute run with two simultaneously offered clients in separate tenants and at least
  100 completed turns uses the real candidate runtime and a deterministic local provider.
  Completed turns are split equally between single-step and tool-required tasks. Require
  zero unexpected errors, zero isolation/continuity failures, correct tool invocation,
  and queue-wait p95 below 2 seconds for each profile. Injected negative tests run separately
  and cannot be removed from their own failure report to improve this workload's score.
- After warm-up, memory growth is at most 512 MiB, additional threads at most 64, additional
  open descriptors at most 256, state growth at most 10 GiB, audit growth at most 512 MiB,
  and disk free space stays at least 20%. Every usage/health sample succeeds and configured
  quotas remain enforced. Freeze workload payloads, budgets and host configuration in M0.
- This is an MVP engineering floor, not the proposed 25-turn GA forecast or a real-provider
  capacity claim. Qualify any larger external pilot envelope separately before exposure.
- Restore/rollback exercises preserve committed conversations and operation records,
  including uncertain delivery. Require backup age <=15 minutes, restore-to-ready <=4 hours,
  and rollback <=15 minutes on the supported topology. Test a versioned state migration or
  explicitly qualify an unchanged-schema upgrade; rollback must not redispatch old intents.

### M6 — Reuse is demonstrated, not merely designed

- One real control-plane workflow runs against the SAME versioned execution mechanisms
  used by pi, with its own SIGIL policy and disjoint domain storage. It exercises an intent,
  observed result, and interrupted/uncertain recovery. No application-specific native branch
  is added to make that second consumer pass.
- Record both application revisions, shared contract/runtime revisions, topology, grants,
  and passing conformance/fault results. A toy second consumer or the control plane's older
  two-slot demonstration alone is insufficient. Do not infer cross-replica guarantees from
  a single serialized executor.
- Coordinate the integration in the owning repositories; this goal does not authorize
  overwriting another task's work or silently changing another project's runtime pin.

### M7 — Releasable, independently installable candidate

- Protected source CI and all new contract, browser, security-boundary and fault suites pass
  without bypassing required checks. Retain current regression coverage; replace retired
  Python-bound tests with equivalent SIGIL/boundary evidence before removing their gates.
- Package and identify one immutable candidate containing both interfaces and their required
  dependencies; verify checksums, provenance/SBOM, exact runtime pin, and operator documents.
  A fresh supported host can install, configure and execute the task without a developer
  checkout or hand-edited source. Compiler/solver installation may remain documented.
- Evidence is bound to candidate digest, source/runtime/contract versions, date, topology,
  procedure, raw results and reviewer. Source-only or mock-only evidence is labeled as such.
  Old candidate reports, including v0.4.0, cannot qualify a new executable by association.

### M8 — Safe external-pilot entry

- A named operator approves model/provider, spending and quota limits, pilot capacity,
  data retention/deletion, TLS, secret handling, encrypted backup scheduling/expiry, and
  incident ownership. Test access provisioning, export/delete, restore and alert delivery
  in the actual pilot environment. Record named engineering and operations sign-off.
- Independent security review of the final application/host/effect boundary and candidate-
  bound dependency/artifact scans are complete. No unresolved critical/high security finding
  or Sev-1/Sev-2 product defect remains. Review must cover both browser and API entry points,
  durable authority, uncertain delivery, and the supported deployment topology.
- Development, fixture work, local demonstrations and ordinary protected CI continue while
  external review is pending. Do not expose the external pilot or claim pilot readiness
  before clearance. Public source availability is not deployment approval.

## AIN boundary and explicit non-goals

AIN is a potential future execution foundation, not a workflow API or a current production
replacement. The research checkpoint reviewed for this goal was AIN20 324; subsequent research
progress does not silently qualify this product or modify its pinned toolchain.

A [dated integration follow-up](shared-runtime-integration-review.md) records the
2026-09-09 control-plane and AIN working-tree inspection, including CP400 accepted /
CP401 partial status and concrete host/protocol differences. It updates the review
inputs, not the acceptance thresholds, runtime pin or M0 approval status.

The contract must use stable semantic operation/authority identities and explicit causal
dependencies, not Wasm pointers, process IDs, or interaction-net node addresses. Serial MVP
execution is acceptable; a mandatory global semantic ordering of otherwise independent work
is not introduced merely because the first executor is serial. Reordering requires a supported
independence argument and matching observable behavior, not an assumption about tool names.

Before freezing M0, review these boundaries with the AIN and control-plane work and record
resolved decisions or explicit limitations. This is an interface review, not a dependency on
completing their research or either application's unrelated roadmap. Backend-specific proof,
artifact, engine, target and host-contract admission remain explicit. Do not promise a drop-in
AIN backend or completion/performance guarantees for an open-ended external-service agent.

Not required for this MVP: an AIN runtime or its remaining proofs, compiler-free/AOT deployment,
GPU/hardware optimization, a new general-purpose distributed runtime, actor-system adoption,
multiple providers, billing/SSO, public anonymous access, unrestricted tools, or the control
plane's overall product completion. SIGIL-to-Wasm verification and Wasmtime-to-executable
compilation are separate steps; compile-once/caching may be used without weakening admission.

The v1.0 GA requirements, including the qualifying GA load test and 30-day pilot, remain
governed by [product-readiness.md](product-readiness.md). Passing this MVP does not pass GA.

## Delivery order and completion rule

1. Freeze M0 and the ownership/conformance inventory; retain the Python service as an oracle.
2. Build M1/M2's first authenticated, tenant-scoped durable model/tool/response slice; prove
   scoped dispatch and crash/cancellation behavior before expanding routes or tool authority.
3. Exercise a real control-plane consumer early (M6); refine and version shared mechanisms
   using both consumers rather than constructing a speculative framework first.
4. Complete API migration, browser chat, security/resource controls and operator flows
   (M1, M3, M4). Existing unrelated development may continue alongside these changes.
5. Freeze/package the candidate; qualify recovery, load, live-model usefulness, clean install,
   and onboarding (M4, M5, M7). Security preparation/review can run alongside development;
   its final acceptance must cover the actual qualifying candidate.
6. Obtain M8 clearance and record the complete evidence index and sign-offs.

Track **shared-mechanism conformance**, **sigil-pi MVP acceptance**, **control-plane application
readiness**, and **AIN research claims** separately. No percentage-complete average is used.
For each M0–M8 row, retain status, missing work, evidence references, and decision owner in a
candidate-bound acceptance report. That report must show nine PASS results before this goal
can be marked complete; M0–M7 without M8 is an engineering candidate, not a cleared pilot.

This goal authorizes scoped development, not unspecified spending, external deployment,
public promotion, destructive migration, cross-repository changes, or unscheduled review work
by other agents. Obtain the needed decisions/authority at those boundaries and keep working
on unaffected development. This charter itself does not start those external actions.
