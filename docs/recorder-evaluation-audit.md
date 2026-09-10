# Recorder evaluation-failure coupling

2026-09-09. **Staged implementation; not complete execution-audit coverage or MVP
qualification.** This working copy is `/private/tmp/sigil-pi-recorder-audit.Jl8kYpDT`.
It was copied from the frozen readiness stage at aggregate
`c7d547ba00422cc0228e1fe8736a43fd7b7e28ef0246d532cb08c4ac84adc4fc`
before changing four native source files and adding eight integration cases.
The original 766-case run continues against its unchanged original directory.
Its result does not automatically apply to these new executable bytes.

## Actual boundary

The fixed claim/result recorder now uses the existing private `Function::observe`
boundary. Under an explicitly enabled audit-v2 profile, an actual evaluation
failure or malformed ER1 framing is passed to the same admitted grantless SIGIL
failure classifier. The native authenticated append still has an empty domain
batch. There is no caller-supplied observation, signing operation, claim, delivery
receipt or dispatch command on this path.

The shared `propose_bound` call receives the actual exclusively borrowed Store.
Its callers cover synchronous claim/result recording, owned-worker completion and
abandoned recovery. A failed recorder before claim cannot authorize invocation;
a failure after send cannot undo the consumed claim or prove remote non-delivery.
Recovery records a new actual recorder evaluation, not a replayed old observation.

Subject configuration comes from the actual admitted, typed `RecorderConfig`;
the separate storage-grant metadata comes from that recorder's actual configured
effect/recording scope. Audit signing grants are not included. EA1's bounded
lookup hash contains, in order, the actual intent namespace, claim namespace,
delivery namespace and intent key. The complete actual WR1 input has its own
evaluation digest. These hashes are not yet full structured actor/operation/read
attribution and are not substitutes for authorization.

The recorder observation is consumed on failure or retired immediately after
successful ER1 framing, before invoking the lifecycle projector. Later formatter,
proposal-binding or commit errors are not falsely labeled failed recorder
evaluations. They still require their own complete audit coverage. Other entry,
helper, coordinator and no-op coverage obligations remain open.

The classifier, EA1 schema, source ceiling, function registry, worker grants,
fuel, memory and deadline limits are unchanged. Failure reporting uses the same
recorder invocation deadline. Result persistence already has its own bounded
interval after dispatch; reporting does not create a further interval. An append
is not a preemptible commit. `audit_recording` and `audit_commit_uncertain` remain
failures, not receipts, proof of absence or instructions to replay an append.
Audit-v1 and unaudited callers retain their original behavior.

## Verification checkpoint

- Formatting and all-target warnings-as-errors passed; terminal `143184`,
  26.72 seconds. The attempted lower process priority was unavailable in the
  sandbox; cargo itself ran normally and exited zero. No permission escalation
  or dependency/lock change was made.
- All **208 native service tests passed**, terminal `4a9b45`: 191 library tests
  in 27.38 seconds, 15 main tests and 2 transaction tests; none ignored or filtered.
- The eight new real HTTP/SIGIL integration cases collected successfully
  (`259468`). Collection is not execution. They cover preclaim failure, result
  failure after send, failed restart recovery without redispatch, unavailable
  failure reporting, and retained v1 behavior, with runtime-error/malformed-output
  variants. Their passing result is not yet established at this checkpoint.

The source aggregate after native checks, before adding this documentation, was
`abf7631d091fef661ed5ac78380063a960cae12933dba24af95297de196d968f`
(`8407f5`). No main service integration, external provider spending, deployment,
runtime-pin change, sibling-repository edit, commit or push is included. All full
MVP gates remain unqualified.

### First integration attempt: setup blocked by local socket permission

Session 11546 ended with **one setup error after 369.20 seconds**, terminal
`6c0169`, observed at 2026-09-09 22:23:51 UTC. The required native/evaluator build
and test fixtures completed, then the scripted provider's `127.0.0.1` listener
was denied by the sandbox (`PermissionError: Operation not permitted`). **No
behavioral case executed.** This is not a product assertion failure or a passing
integration result. The source stayed frozen at
`802d56ae2d840fb5ccef5d557bbacadbbbbd65a95a579f95d8ab891478083f77`
through this run. The retry requests local test-network permission and retains
the exact nine-case selector, all prerequisites, assertions and limits. No
production source change is needed to address the setup permission.

### Loopback-enabled retry and investigation

Session 51897 ended with **2 passed, 1 failed in 358.26 seconds**, terminal
`f681a6`, at 22:31:14 UTC. Both actual preclaim failure cases passed. The first
post-send case had no journal event and no provider request during its unchanged
15-second observation window. Its state contained accepted domain records but
no claim, delivery or audit entry. The tested aggregate remained
`20f6ce3dfe86e3a38e3c3368e330efaaf925c9975b4b78a043d76e3e071df3d0`.

Four new compiled fixture-contract checks passed in 3.26 seconds (`6200e7`): the
selected fault path fails and the untouched recorder path produces byte-identical
output to the real original SIGIL recorder. A separate local diagnostic against
the exact already-built executable reached one provider request, one consumed
claim, two failure events and no delivery (`cb9c3d`). That diagnostic is not the
required integration rerun and does not prove why the earlier run failed.

The new test now includes the actual first native diagnostic and claim/delivery
counts if its original wait fails; no assertion, deadline or production source
was changed. The next complete focused run executes the existing normal v2
two-tenant turn first, then all eight recorder cases and the four fixture-contract
checks, retaining all native/evaluator prerequisites. Neither a timing cause nor
a completed recorder fix is claimed without that evidence.

### Complete focused rerun passed

Session **65075 passed all 13 cases in 592.40 seconds** (9:52), terminal `a25998`,
exit zero, on 2026-09-09. This includes the existing v2 normal two-tenant
model/tool/follow-up/restart case, all eight recorder cases and all four compiled
fixture-contract checks. No cases were skipped or xfailed, and all original
native/evaluator prerequisites remained. The before/after aggregate matched
`0f730baca9b02fd97abb2ad1304542cd02b0e428a92ac254e7a2eb1f0c459089`.

The earlier failed run is retained above. No production code or wait/assertion
was changed between its failure and this pass; only failure diagnostics and the
four fixture-contract probes were added, and the normal v2 case ran first. The
independent 766-case run had finished before this rerun. Concurrency/timing is
not established as the earlier failure's cause. Full source verification and
operating-envelope qualification remain necessary; one focused pass does not
resolve every reliability question or qualify an MVP gate.

The host/transaction/claimed-worker hashes for the focused run are respectively
`448892370a6b73ac63223591fc81542bcdbbc8de8ef15b98818b5e7f26340bad`,
`71113277c9dfce0cf9a5b3c6f324c00d17ec0fc3169718f515d8950efad7f2ab`, and
`d4cfc228a13724d49cfbc44ed2019ec6e811be8b17aff5b5bd1933a81daae531`.
They differ from the base 766-case artifacts. Integration must retain both sets
of evidence and test the resulting complete repository; it cannot relabel the
base result as a full regression pass for these newer binaries.

### Repository integration checkpoint

After the focused run was terminal, these mechanisms and tests were integrated
into `/Users/nigel/Projects/sigil-pi` as optional v9 development support. Existing
v8 configurations and the frozen-v8 compatibility source remain intact. The
complete repository source gate is pending; neither the focused 13-case pass nor
the earlier base 766-case pass qualifies the integrated repository or a pilot.
Earlier staged paths and source hashes above are historical evidence, not a
claim that the new main checkout has already passed. The current status and
remaining full-goal requirements are in [the acceptance record](mvp-acceptance.md).
