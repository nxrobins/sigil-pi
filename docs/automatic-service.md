# Automatic SIGIL service, development protocol v4

Status: **LOCAL DEVELOPMENT INTEGRATION — not an MVP candidate**.
Run results and remaining gates are recorded in [MVP acceptance](mvp-acceptance.md).

The HTTP service can now complete an admitted model → permitted file → model →
response turn and a same-conversation follow-up without a Python step driver.
The SIGIL [coordinator](../app/pi/coordinator.sigil) chooses discovery, worker
selection, polling/cancellation, abandoned-claim recovery, delivery interpretation and settlement.
The native [command interpreter](../native/service/src/automatic.rs) supplies only
fixed aliases, actual observations, bounded execution and scoped storage mechanisms.
The provider in local integration tests is deterministic, not a real-model benchmark.

## Configuration and authority

HTTP configuration `version: 4` requires an `automatic` participant registry.
Version 3 rejects this registry and retains its admission-only reference behavior.
The same `AH3`/`HC3` API contract and existing durable record schemas remain in use.
Version numbers of worker configs and the trusted claimed-worker stdio adapter are
separate protocols; a v4 HTTP config does not expose that adapter publicly.

Each current credential must have exactly one participant. Its fixed, grantless
coordinator receives current credential facts and an opaque application binding.
The native registry permits at most 16 total effect lanes and eight transactions
per participant. Current pi boot policy accepts two effect aliases (model/file)
and three transactions (interpret/settle/preclaim), so it currently supports at most eight
configured credentials. This is a development ceiling, not a qualified pilot capacity.

The coordinator's read-only scope is delegated from that credential's domain grants
plus its reserved claim/delivery namespaces. Reserved namespaces are disjoint across
participants and cannot overlap any credential's domain namespaces. Effect lanes
have claim read/write, delivery create-only and delegated domain reads. Transaction
grants cannot exceed the parent credential's scope plus claim/delivery read access.
Neither read-write nor create-only access to reserved records can be delegated to
these transactions.
These native delegation ceilings do not replace SIGIL's tenant/application policy.

Every effect has fixed source/runtime hashes, grants, a pure dispatch policy and a
pure recorder. Automatic dispatch templates must contain exactly one placeholder
for each of current credential facts, the actual bundle and actual worker facts;
static literal authority snapshots are rejected. The host binds these facts, and
validates all effect/transaction templates, grants and artifacts before initializing
application storage. SIGIL validates the coordinator binding at boot. Generic
delivery interpretation legitimately has no credential input: it consumes already
recorded observations under its fixed scope, not new effect authority.

The v4 bundle hashes the full serialized automatic registry alongside the API and
admission/history artifacts, limits and the fixed-grantless evaluator contract. It binds
paths and aliases too; moving or changing a deployment is not an implicit compatible
upgrade. Credential facts are supplied from the actual current registry rather than
treated as bundle-authenticated literals. This digest is not a signature, verified
provenance, native executable identity or an approved artifact-admission pipeline.

## Execution and recovery

The single storage owner alternates queued HTTP work and one due coordinator
instruction, with bounded participant rotation. SIGIL's read-only key scan discovers
retained operations; it does not accept phase-filtered native work selection.
Volatile continuations and scan cursors are discarded on restart.

Unclaimed work first invokes [never-claimed finalization](preclaim.md). It either
atomically publishes a cancellation or expiry/validity/allowance failure without inventing delivery,
or returns an eligible no-op. Eligible work still passes the actual [dispatch policy](dispatch-policy.md) and
[receipt-bound owned lane](owned-worker.md). A private native generation and context
identify active work. Only actual thread completion can produce an effect observation;
HTTP requests cannot supply results, grants, receipts, snapshots or completion commands.
Effect workers still receive one-use execution and fresh runtime processes.

The current binding uses cancellation-aware `UF2` finalization and `DF2` dispatch.
Both receive the actual cancellation key/snapshot. The actual dispatch read/claim
boundary excludes a cancellation interleaving after its read, and rechecks requests
committed after an earlier eligible no-op. While a worker is held, SIGIL interleaves
cancellation reads and polls, signals only the actual held alias/generation, then
waits for the actual observation. A signal acknowledgement is not stop/rollback
proof. [Public cancellation](cancellation.md) documents results and races.

SIGIL distinguishes a matching active handle from an abandoned durable claim.
Recovery records uncertainty and never sends the effect. The native registry also
refuses recovery while any of that participant's lanes owns the same intent coordinate.
An orphaned worker may still finish externally: recovery does not certify its death
or undo a request that was sent. Its late result cannot overwrite the recovered claim.

The fixed `TF1` [delivery interpreter](../app/pi/turn_completion.sigil) reads actual
state, intent, delivery and unused-next-intent snapshots through the generic native
transaction mechanism. It derives the reducer event itself, reuses the existing
conversation reducer, and atomically advances state plus the next intent (or terminal
state). Every actual read is covered exactly once by a revision check/write.
No caller-authored `PE1` event or result payload is accepted on this path.
SIGIL then invokes [settlement](settlement.md) to publish the terminal operation,
release the active-turn reservation, and retain token holds for unknown/overrun usage.

Internal frames use the existing length-prefixed codec:

- `CL1`: current facts, bundle, `LB3`, actual clock, native stage/observation,
  held continuation, actual `AF1` array, transaction aliases, read namespaces,
  claim namespace and delivery namespace.
- `LB3`: model alias, file alias, interpretation alias, settlement alias, preclaim alias.
  The preceding `LB1`/`LB2` bindings are rejected, not automatically migrated.
- `AF1`: registered alias, actual active generation, opaque dispatch context.
- `KC1`: SIGIL-owned volatile step, cursor, operation, session, current/next key,
  selected alias and generation.
- `LC1`: bounded command, three arguments, continuation and minimum delay.
- `TF1`: fixed state/intent/delivery namespaces, session, operation, current/next
  keys, native time and four actual `SR1` snapshots; output is the existing `TX1`.
- `DF2`: the eleven `DF1` fields plus the actual cancellation lookup key and snapshot.
- `UF2`: the fifteen `UF1` fields plus the actual cancellation lookup key and snapshot;
  output is the same generic `TX1`. Its terminal batch covers all eight actual reads.

## Fixed grantless evaluator

Only v4 API entry, admission, history and coordinator functions use the
[cached evaluator bridge](../native/worker/src/pure.rs). Each has its own process with one
fixed admitted source and **no network, filesystem or secret grants**. The process
is reaped after at most 4,096 calls. Inputs, fuel and time remain bounded; the selected
executable's complete hash is checked on every invocation. Timeout/protocol failures
retire the process without replaying the call; unconfirmed cleanup poisons the lane.

The default development configuration now selects the
[fixed grantless executable](fixed-evaluator.md) for these four roles. It compiles
and solver-verifies the exact source once per process, refuses changed source or
effect grants, and invokes the same pinned runtime with a fresh store/instance,
memory and fuel for every input. Current authority, cancellation and other facts
are still interpreted by SIGIL on every call. It caches code, not decisions/results.
Its exact binary hash is included in the normal bundle; existing deployment
compatibility is not inferred. This is in-memory fresh compilation, not accepting
untrusted precompiled bytes, compiler-free packaging, an AIN backend or a qualified
artifact-admission pipeline. The original MCP worker remains the effect/runtime
reference and still verifies each forge. Dispatch, recording and transaction
producers retain that fresh-process path for now.

Automatic HTTP tests use the optimized native host while still requiring debug
formatting, warnings-denied Clippy, native tests and builds first. Debug hashing of
the 27 MiB runtime materially inflated turn time; optimization changes neither the
operation deadlines nor the integrity/proof checks. The newer fixed evaluator has
additional mandatory native protocol/isolation/supervision/lifetime gates. Passing these tests does not
qualify latency, concurrent throughput or memory/process lifetime.

## Explicit remaining work

Never-claimed expiry, inactive validity, unknown usage and exhausted allowance now
have a SIGIL terminal/accounting path, with current qualification recorded separately.
Changed/removed authority and other mechanism refusals still need explicit handling
and can leave accepted operations unresolved. Public cancellation now includes a
durable request, actual dispatch-time check and pending status, with partial local
qualification recorded separately. The full cancellation/interruption matrix,
confirmed local-stop presentation and intermediate tool/progress semantics remain
open; a cancellation signal alone is insufficient evidence of completion.

Other legacy handlers, browser chat, full tool/route isolation, quotas/audit/retention,
all interruption points, concurrent load, supported Linux lifecycle/restore, real
model usefulness, actual control-plane reuse, candidate packaging and independent
review remain open. Loopback-only transport and the pinned runtime are unchanged.
