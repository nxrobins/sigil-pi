# Public cancellation and truthful outcomes

Status: **LOCAL DEVELOPMENT IMPLEMENTATION — not an M0-frozen or pilot contract**.
Current qualification is recorded in [MVP acceptance](mvp-acceptance.md). The
preceding [design note](cancellation-plan.md) is historical, not the current API.

## Public request and result access

`POST /v1/operations/{operation}/cancel` is implemented by the actual
[SIGIL API](../app/pi/cancel_api.sigil), using the existing `chat` scope. The current
credential must be active and match BOTH the accepting principal and tenant.
Another principal, including one in the same tenant, receives 404. Cancellation
does not grant permission to read the answer.

The operation identifier is exactly 64 lowercase hexadecimal characters. The body
must be empty or an empty JSON object, with ordinary JSON whitespace allowed and
a 1,024-byte maximum. Unknown fields, caller-supplied authority/clock/generation,
non-object JSON and malformed/trailing content are refused with 400. The native
HTTP limits and authentication mechanism remain unchanged.

For an accepted operation, a successful durable request returns 202 with the
original operation identity/status location, `status: "accepted"`,
`cancellation_requested: true`, `cancellation_status: "requested"`, and the usual
`replayed` flag. A matching repeat returns the same identity with `replayed: true`.
An acknowledgement means the request is retained, **not that a worker has stopped
or a remote effect has been undone**. A lost acknowledgement is recovered through
the same cancellation URL or normal operation lookup.

For an already-terminal operation, the route returns 200 with only `operation`,
terminal `status`, `cancellation_status: "terminal"` and `status_url`. It does not
create another record or return the reply, usage or error detail. Full terminal
content still requires `sessions:read` through `GET /v1/operations/{operation}`.
The same read scope and accepting-principal/tenant checks apply to pending lookup;
it now reports a retained cancellation request, while absence preserves the prior
accepted response. This is not a complete intermediate-progress API.

An active operation bound to a different deployment bundle returns 409
`operation_version_mismatch`. An inconsistent cancellation slot returns 409
`cancellation_conflict`; actual read/commit failure returns 503. A failed response
does not undo a commit that has already happened. Malformed retained records fail
closed, not as a fresh cancellation opportunity.

## Durable request and dispatch boundary

`CR1` has five length-prefixed fields: operation identity, accepting principal,
tenant, admitted bundle and cancellation-request seconds. Its key is
`<operation>:cancel` in the existing operations namespace; no seventh domain
namespace or guest grant is added. SIGIL validates revision one, exact identities
and bundle, and a request time between creation and current native time. Only
revision zero AND empty value represents absence; tombstones and malformed,
cross-owner or future records are refused.

The API reads the actual accepted operation and cancellation slot. One atomic
commit checks the original `OQ2` revision one and creates `CR1` at revision zero.
It does not rewrite the operation, reservation, conversation, intent, claim or
delivery. The API acknowledges only an actual successful native commit receipt.

The automatic deployment selects the cancellation-aware `DF2` dispatch component.
Its first eleven fields reuse the exact `DF1` policy inputs and implementation;
two additional fields bind the cancellation lookup key and actual scoped `SR1`
snapshot. Existing authority, payload, grant, quota and deadline checks still run.
A valid cancellation request refuses dispatch; an inconsistent record fails closed.
The native policy owner retains exclusive storage ownership from actual reads
through claim/handoff, so an API cancellation cannot interleave inside that boundary.
This covers a cancellation committed after an eligible preclaim no-op but before
the later actual start. An earlier coordinator read is not the dispatch fence.

## Finishing without inventing an outcome

The cancellation-aware `UF2` finalizer extends the fifteen `UF1` fields with the
actual cancellation key and snapshot. It requires the same immutable records and
unchanged settlement policy, including actual claim AND delivery absence. A valid
request selects `cancelled` before expiry/allowance reasons. Its terminal batch
has four checks and four writes, covering all eight actual reads exactly once:
intent/claim/delivery/cancellation checks plus operation/reservation/capacity/state
writes. Prior observed usage is retained; unknown/overrun holds are not released.
Never claiming the current action does not imply the entire turn used no tokens.

For a held worker, the SIGIL coordinator interleaves actual cancellation reads
with polling. SIGIL selects the held alias and native generation, requests the
existing native signal, then polls for actual completion. A signal-issued boolean
is not a completion or delivery observation. After-send cancellation can remain
`uncertain`; restart recovery never silently resends the effect. An already
observed final answer may win the race and remain `done`. Observed tool requests
and tool results still pass through normal interpretation; `UF2`/`DF2` prevent
starting a later action after cancellation is committed.

The fixed five-alias application binding is now `LB3`. Old `LB1`/`LB2` bindings
and old `DF1`/`UF1` inputs are not silently accepted by the new components. Original
`DF1` and `UF1` remain conformance references, not the selected automatic deployment.
HTTP configuration is still development v4, but the changed sources/registry change
its bundle. No deployment migration or old-operation compatibility is inferred.
No native source or runtime pin was changed for this cancellation slice.

## Evidence boundaries and remaining work

Local tests cover public ownership/scope/body refusal, concurrent duplicates,
lost acknowledgement/restart, pending lookup, the controlled eligibility/start gap,
before-first-claim cancellation, after-send uncertainty and terminal result races.
The gap test uses real HTTP plus the native conformance adapter to choose the
injection boundary; it is not presented as an automatically driven production turn.
Automatic-service cases use actual HTTP/SIGIL sequencing and local providers.
See the acceptance record for completed suites, failures and source identities.

Automatic cancellation after an observed model/requested tool and after an observed
file/before the next model also passed focused local checks: prior usage and restart
accounting are preserved with no later effect. The remaining interruption matrix,
bounded responsiveness/load and supported Linux process-lifetime evidence still
need qualification. Public responses do not yet
expose independently confirmed local-stop facts. Changed/removed principal, epoch,
policy, profile or bundle still requires explicit recovery/reconciliation; accepting
a request is not proof that settlement under changed policy can complete. Full
quota/charged-usage accounting, retention of cancellation records, audit, complete
API/browser behavior and every M0–M8 gate remain open.
