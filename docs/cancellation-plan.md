# Public cancellation: historical design note

Status: **SUPERSEDED DESIGN NOTE — not approved as the M0 contract**.

The local implementation is now described in [cancellation](cancellation.md),
with exact completed checks in [MVP acceptance](mvp-acceptance.md). The proposal
below predates that implementation. In particular, its suggestion to return a
terminal result from the `chat`-scoped cancel route was rejected after a regression
test reproduced result-read permission bypass: the implemented route returns only
control metadata, while full results require `sessions:read`.

The existing draft route is POST /v1/operations/{operation}/cancel with `chat`
scope. Keep the existing accepting-principal AND tenant ownership check; tenant
membership alone must not cancel another principal's operation. M0 approval and
final request/response schemas remain pending. Use the implementation document for
current behavior, not this historical proposal.

Avoid rewriting OQ2 to record a request: settlement currently requires its original
revision. A separate create-once cancellation record at an operation-derived key
in the existing operation namespace can preserve original authority/reservation
records without introducing a seventh domain namespace. SIGIL must validate and
construct the exact record; the native host must not parse its product meaning.
Discovery already skips non-operation keys and rescans, but load/retention must
include these additional keys if this layout is adopted.

The public handler should read the actual operation and cancellation slot, check
owner/scope, and atomically create the request with the observed operation revision
as a precondition. Matching repeats and lost acknowledgement must not create another
request. A terminal operation can return its retained result; a cancellation request
is not a stopped-worker or remote-rollback acknowledgement. Public requests cannot
provide native generation, claim, receipt, effect result, scope or clock fields.

A cancellation read in preclaim alone is insufficient. The service can accept an
HTTP cancellation between an eligible no-op and the subsequent start command.
The actual dispatch policy must therefore consume the current cancellation snapshot
under its owned read/claim boundary and bind absence into its decision. A volatile
coordinator flag or earlier check is not a fence. Version that input/binding explicitly;
never let an LB2/UF1/DF1 deployment silently acquire a stronger cancellation claim.

The coordinator currently stays in its pending poll branch. It must interleave an
actual cancellation-record read while a worker is pending; otherwise a request is
not observed until the worker completes. SIGIL selects the actual active lane and
generation; the existing native cancel mechanism only signals that held worker.
Never turn a signal-issued response into a claim that cleanup or rollback happened.

Extend never-claimed finalization to cover a bound cancellation request, checking
that actual claim/delivery are absent in its same atomic transaction. Preserve any
earlier observed usage: "current action unsent" does not mean the entire operation
never called a model. If a claim already exists, collect/recover through the actual
worker result path; cancellation after send may remain uncertain. A recorded model
result must retain its observed usage. It cannot be discarded or reclassified as
unsent to obtain a cleaner cancellation result.

Specify races explicitly: cancellation versus model completion, next-intent commit,
delivery interpretation, terminal settlement, expiry and service restart. An already
observed final answer may win a cancellation race; an observed tool request must not
authorize another effect after cancellation has been accepted. Final API facts must
separate request accepted, actual local termination, and remote delivery uncertainty.

Required tests include an explicitly controlled gap between eligibility and start:
commit cancellation through the public API there, then prove the later actual
dispatch refuses without creating a claim or contacting the provider. Do not use a
passing preclaim-only test as evidence for this race.

Other required tests: wrong tenant/principal/scope and malformed payload; duplicate and
lost-ack cancellation; before claim, during held provider, after delivery, after
terminal publication and across restart; exact native generation binding; no extra
provider/file effect; preserved known/unknown/overrun usage and no double release;
another tenant's API remains responsive. Full lifetime/load/browser/security gates
remain independent and cannot be inferred from this functional slice.
