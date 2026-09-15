# SIGIL conversation reducer: executable first slice

Status: **IMPLEMENTED COMPONENT / DRAFT INTERNAL CODECS**, 2026-09-07.
Sources: [turn application](../app/pi/turn.sigil),
[pure helpers](../app/pi/turn_helpers.sigil),
[read-file preparation](../app/pi/read_request.sigil).
This advances M1/M2 of [the MVP goal](mvp-goal.md); it does not qualify either gate.

SIGIL now constructs the model request, interprets the response, selects permitted
tools, incorporates their results and chooses whether to continue or stop. The
production API and `PiAgent` have not switched to this implementation. The new
execution driver is a **test fixture**, not another production Python agent loop.

## What this component owns

The application is a pure function of prior state, a supplied observation,
configuration and time. It has no native effect imports or I/O grants. It returns
serialized next state plus at most one proposed action:

`start → model → approved tool → model → response`

Several requested tools execute serially, with distinct sequence numbers for the
actions actually proposed. A tool missing from the supplied approved catalog
produces a correlated error for the model, never an executable tool proposal.
Grant enforcement is still required independently by the worker; catalog membership
is application policy, not permission to execute an arbitrary artifact.

Completed conversations can accept a new operation in the same session and retain
their history. A follow-up cannot reuse the preceding operation ID. Only a completed
state currently permits a follow-up; recovery from failed, cancelled or uncertain
work needs additional SIGIL policy before full service migration.

Results must match the state's operation and current sequence. Old results cannot
advance a newer state. Re-evaluating the same inputs produces the same proposal;
it does **not** establish durable deduplication. A caller supplying an old state
could still obtain its old proposal, so an authenticated expected-revision check
and atomic commitment must surround every real transition.

## Draft codecs

All records begin with a four-byte marker including LF. Each field is eight ASCII
decimal digits of UTF-8 byte length followed by exactly that many bytes. There are
no extra separators or trailing bytes. Nested records contain serialized bytes,
not guest pointers. Decimal-valued fields use canonical nonnegative decimal text
without leading zeroes, except the framing lengths themselves.

| Marker | Fields in order |
|---|---|
| `PE1` + LF | prior state, event, operation ID, sequence, payload, configuration, current Unix time in seconds, deadline in the same unit |
| `PC1` + LF | model, maximum output tokens, maximum model steps, system prompt, tool specifications JSON, history byte cap, tool-result byte cap |
| `PT1` + LF | operation ID, phase, sequence, model steps, configuration, history JSON, pending tools JSON, pending cursor, tool results JSON, reply text, input tokens, output tokens, usage-known flag, deadline, error, session, submission key |
| `PD1` + LF | next state, action, sequence, tool name, action input |
| `PF1` + LF | executor-bound workspace root, model tool-arguments JSON |

`start` uses a validated [PS1 submission](submission-codec.md), configuration and
deadline, with sequence zero. Session/key grammar is rechecked when reading PS1;
an apparently canonical frame is not proof that an approved decoder produced it.
Following observations must leave configuration/deadline empty and use the stored
values. The proposal action equals its state's phase: `model`, `tool`, `done`,
`failed`, `uncertain` or `cancelled`. Terminal proposals contain no tool name/input.

The HTTP admission path supplies Unix seconds, matching `AH3`, `AP2` and `TG1`.
Pure component fixtures use synthetic same-unit clock values. An earlier draft of
this table mislabeled the unit as milliseconds; no stored timestamps are converted
by the reducer or by this documentation correction.

The local operation/sequence correlation is not the shared contract's authenticated
intent/attempt identity. Authority issuance, application revision, payload binding,
tenant identity, reservations, executor ownership and delivery provenance remain
outside these codecs. They must be supplied by [the durable contract](execution-contract.md),
not inferred from a successful invocation or caller-supplied identifier.

## Observations and stopping

- `ok`: interpret a model response, or include a tool's returned text. Malformed
  model fields, duplicate decoded fields used by the reducer, duplicate tool-use
  IDs and invalid usage numbers cannot create the next tool action. Unknown
  provider metadata is not a universal strict JSON schema or duplicate-key check.
- `tool_error`: include a correlated tool error and let SIGIL select the next
  action. It is not accepted in the model phase.
- `unknown`: stop as `uncertain` with `possibly_delivered`. There is no automatic
  retry. Usage is conservatively marked unknown, not settled as unspent.
- `cancelled_unsent` / `expired_unsent`: stop with the supplied definitely-unsent
  fact. The executor may supply these only when the delivery protocol establishes
  that fact; killing a worker after dispatch is not sufficient.
- `error`: stop on an observed effect failure. Transport failure with ambiguous
  delivery must instead be `unknown`; this label does not undo a remote effect.

The application checks step, history, tool-result and deadline limits before
further dispatch. It records available integer usage and keeps missing/uncertain
usage explicit. Quota reservation/settlement and a trusted clock source are not
implemented by the reducer. Native execution must also impose independent limits.

Configuration is bounded to 64 model steps, 8,192 output tokens per request, 32
tool specifications, 65,536 bytes each for system prompt/catalog, 1,048,576 bytes
of history and 262,144 bytes per tool result. The prototype accepts at most 32
model content blocks and a 1 MiB model response. Aggregate record/allocation/fuel
limits may reject a combination even when each individual field fits. These are
component ceilings, not a frozen pilot capacity profile. Current overflow behavior
is fail-closed; context compaction and result clipping parity are still required.
Negative tool returns are internal failures, **not direct HTTP status mappings**.

`PF1` preparation validates the path type and rejects empty/NUL/LF/CR paths. It
joins relative paths to the bound root and preserves absolute paths. The separate
filesystem worker, not this string adapter, must canonicalize and enforce its own
grant against parent-directory, absolute-path and symlink escapes.

## Build and evidence boundary

[The fixed build recipe](../scripts/compose_application.py) composes the pinned
JSON library, shared UTF-8 validator and application fragments. It removes line
comments lexically, preserving strings and block comments, to fit the pinned
forge's unchanged 64 KiB source ceiling. It reports hashes of every authored input,
the selected stdlib and final compiler input separately. No verification bypass,
source-limit increase or runtime policy is introduced. Build hashes alone are not
authenticated artifact admission or candidate provenance.

[Reducer tests](../tests/test_turn_reducer.py) cover deterministic sequencing,
follow-ups, serial and denied tools, correlation, terminal behavior, deadlines,
usage, malformed input/state and bounded results. [Composition tests](../tests/test_application_composition.py)
check lexical preservation, the fixed component registry, exact markers, hashes
and the source ceiling. The submission decoder's 108 regression tests also cover
the extracted shared UTF-8 validator.

[Execution tests](../tests/test_turn_execution.py) run actual isolated SIGIL
submission, reducer, provider and filesystem artifacts against a local scripted
provider. They check separate grants, exact file bytes reaching the next model
request, denied workspace escapes, and absence of the injected provider key from
guest inputs/state. A hanging provider observes one request; supervision kills and
reaps the worker, then a fresh runtime interprets the saved proposal as uncertain
without sending the request again.

That saved proposal is a test snapshot, **not crash-durable acceptance**. The fixture
supplies its registry, observations, clock and grants directly. It proves neither
authenticated per-operation grants nor a production dispatcher. There is no real
model usefulness result, production HTTP/browser flow, durable commit/claim,
whole-application admission, second consumer or qualifying release candidate yet.

A subsequent [durable integration suite](../tests/test_durable_turn.py) uses the
[native transactional store](native-store.md) for actual atomic state/intent and
delivery commits, with storage-process restarts between effects. The later
[SIGIL transaction producer](turn-transactions.md) now constructs application
commits in those tests, using the same `reduce_turn` function. A later
[shared executor](executor-transactions.md) now constructs claim/delivery
transactions in SIGIL as well, and the [native worker bridge](native-worker.md)
supplies one-use effect execution and generation-correlated observations. The
fixture still binds actors, selects the registry and enforces claim-before-dispatch
ordering; the native bridge does not authenticate those decisions. That evidence
does not change the scope of the original snapshot/cancellation tests or qualify
the authenticated product service.

Before wiring the product service, implement authenticated admission and durable
state/intent/delivery commitment, prove the per-action crash boundaries, finish
policy/parity work and replace fixture-supplied facts with qualified mechanisms.
Freeze/version these draft codecs through M0; never reinterpret stored records
silently after a layout or semantic change.
