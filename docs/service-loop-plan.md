# Automatic SIGIL service loop: next integration boundary

Status: **HISTORICAL INTEGRATION PLAN — positive automatic path now implemented;
remaining qualification is open, not the M0 contract freeze**.
Inspected 2026-09-08 against the local source under the 1,837-case qualification run.
This connects existing mechanisms toward the full [MVP goal](mvp-goal.md); it does
not replace any gate or narrow the product to a scripted demonstration.

Update 2026-09-08: [development HTTP v4](automatic-service.md) now supplies the
coordinator, bounded registry/discovery, current-facts binding, actual delivery
interpretation and settlement. The real HTTP model/file/model/follow-up check passes.
The sections below retain the original integration requirements; references to
missing basic orchestration describe the pre-v4 baseline. Never-claimed expiry and
allowance finalization is now [implemented separately](preclaim.md); changed/removed
authority, public cancellation and complete fault/load evidence remain required.

## What is still outside the service

The actual HTTP host owns admission and terminal lookup. The recorded full-turn
fixture still chooses the model/file worker, looks up the current operation/sequence,
invokes application interpretation and requests terminal settlement. Those choices
must move into SIGIL execution in the running service. A Python launcher may assemble
fixed configuration, but cannot become the production conversation driver.

There are two concrete native integration gaps:

1. Bounded, scoped [key discovery](native-store.md#bounded-key-discovery) is now
   implemented in the native store and trusted stdio adapter, after the 1,837-case
   baseline completed. It still needs exposure to the SIGIL coordinator so it can
   inspect retained operations and choose eligible work. Native discovery does not
   filter records by pi phase, model/tool names or tenant policy.
2. The reference `Attempt` exclusively borrows the store and worker through the synchronous effect
   and recording call. `Engine::request` needs the same store. Putting that call inside
   the current request loop would delay status requests, cancellation and other tenants
   until the effect finished. The new [owned worker](owned-worker.md) releases that
   borrow after an actual claim and checked handoff; its private completion channel
   supports pending checks and cancellation. It is exposed only through the native
   library and explicit trusted v4 conformance adapter, not connected to the HTTP
   owner/coordinator. The existing 30-second HTTP wait is not a solution to the goal's
   responsive interfaces or measured queue-wait requirement.

## Required ownership and lifecycle

Keep one storage/application owner and bounded supervised effect execution. The owner
must remain able to process SIGIL API decisions while an external worker is running.
Do not remove exclusion with shared raw SQLite access or move tenant/sequence decisions
into native scheduling branches.

The native effect lifecycle now has an owned, non-serializable in-flight handle.
Its service integration must preserve these requirements:

- Prepare from the fixed artifact and grants selected by SIGIL, with actual authority,
  state and intent checks. A caller-authored ticket, result or receipt cannot arm work.
- Commit the exact SIGIL claim and retain the actual receipt. Recheck the selected
  time guard immediately before starting the fixed one-use worker. Only then release
  the storage borrow while execution proceeds; the immutable claim/intent binding and
  worker ownership must remain held in the native handle.
- Return actual completion or pending/cancellation facts to the storage owner through
  a host-owned channel. A failed channel or controller loss is not a successful effect
  and does not authorize replay. No public HTTP/stdio request supplies worker results.
- Re-read and bind current claim/intent/delivery records before SIGIL result recording.
  Conflicting or stale completion cannot advance newer work. Recording keeps its own
  bounded interval after dispatch expiry. Persist uncertainty when an observation or
  cleanup acknowledgement cannot be established.
- Keep cancellation mechanical: SIGIL authorizes the request and chooses durable
  cancellation state; native code signals/stops/reaps its actual owned worker. Product
  acknowledgement must distinguish a request to stop, confirmed local stop and unknown
  external outcome. Killing a worker cannot roll back a sent request.

The existing synchronous claim path remains a conformance reference during this
refactor. The new public mechanism must not expose its internal result-persistence
continuation as a caller-authored observation API. It must also retain an independent
in-flight ceiling and prevent replacement dispatch from reusing a claimed coordinate.

## SIGIL service decisions

A fixed grantless SIGIL coordinator should consume actual bounded discovery/read
observations and current installed authority facts. It owns operation eligibility,
worker selection, delivery interpretation, stopping/recovery, settlement and when to
rescan. It calls the existing admission/dispatch/turn/executor/settlement components;
do not copy their policies into a second implementation.

Native registries bind opaque aliases to fixed artifacts, exact scoped capabilities
and bounded mechanisms. A coordinator command cannot supply a replacement artifact,
grant set, secret, clock, claim result or storage scope. Dispatch policy must still
validate the selected alias against actual held-worker facts, not just trust a matching
name. The eventual configured bundle must bind the coordinator, effect bindings and
native host contract; the current API/admission-only digest is insufficient for that.

Discovery cursors are traversal hints, not claims or stable multi-page snapshots.
Keys inserted before a cursor require another scan; tombstones cannot turn into fresh
dispatch slots. Restart may discard volatile cursors and reconstruct from durable
records without resending claimed/uncertain work. A stored operation with a removed
principal, changed policy or expired deadline must fail closed and receive explicit
SIGIL recovery/accounting handling, not be run under another principal's authority.

The application also needs a defined definitely-unsent path when eligibility expires
before a claim. The existing abandoned-claim recorder covers a different state and
must not be used to invent a claim or an observed response for work never dispatched.
API progress/cancellation records, new internal command/config versions and state
compatibility require explicit contracts and tests before release.

## Success evidence for this integration

The decisive positive test starts only the real service plus a controlled provider
and permitted workspace. It submits through HTTP, receives durable acceptance, and
polls a terminal file-grounded response. No test-only command may choose a worker,
interpret a delivery, construct a state transaction or request settlement between
submission and completion. A follow-up uses the same path and retains the prior result.

The same service must then demonstrate:

- Pending status and an authenticated cancellation decision while an effect is held
  open; another tenant's admitted API request cannot depend on that effect returning.
- Restart after admission, claim, effect send, result commit and terminal commit, with
  truthful uncertainty and no silent repeat of possibly delivered work.
- Denied/expired/revoked credentials, mismatched worker bindings and exhausted
  reservations cause no unauthorized effect; two tenants with the same session name
  cannot inspect or mutate one another's state.
- Forged/duplicate/late results, cancelled handles, conflicting state and a lost
  acknowledgement cannot double-release reservations or advance a newer operation.
- Existing native/contract/API regression gates remain intact, and both the old
  reference behavior and intentional version changes remain documented.

These are integration criteria, not a replacement for real-model/browser usefulness,
all legacy routes/tools, complete quotas/audit/retention, Linux lifetime/load/restore,
actual control-plane reuse, candidate evidence, independent review or operator sign-off.
