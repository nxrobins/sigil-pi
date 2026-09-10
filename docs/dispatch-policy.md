# SIGIL dispatch policy and native fact binding

Status: development component, **not pilot clearance**. The original DF1 conformance
path below remains a reference; the automatic v4 service now selects its DF2 extension.
See the [MVP goal](mvp-goal.md) and [evidence record](mvp-acceptance.md).

`app/pi/dispatch.sigil` decides whether a retained operation's next intent is
eligible to run on one operator-bound effect worker. `native/service/src/policy.rs`
supplies actual scoped storage reads, current wall-clock facts, fixed bootstrap
authority/bundle/binding values and the held worker's non-secret metadata. It then
passes an accepted proposal directly into the existing [durable claim gate](claimed-worker.md).
There is no native model/tool branch or native interpretation of pi's records.

The HTTP v3 reference only admits and looks up accepted or
[settled terminal operations](settlement.md); it does not automatically invoke this
dispatcher. The [v4 automatic service](automatic-service.md) now connects the
cancellation-aware DF2 policy to owned execution, result interpretation and settlement.
The legacy Python product is unchanged. These paths do not establish full route parity.

## SIGIL decisions

The policy requires five actual retained records: operation `OQ2`, reservation
`BR1`, conversation `PT1`, intent `SI1` and outstanding-reservation counter `BH1`.
It checks operation/tenant/principal/session/key/sequence correlation, the original
application bundle, admitted profile, original deadline, reserved status and held
capacity. Missing records, tombstones, changed bindings and terminal states refuse.
An executable intent must retain revision 1; rewriting its bytes does not mint a
new authorized action.

Current authority must match the original principal, tenant, policy epoch, scope
set, tool set, six namespaces, turn bound and exact profile. Equivalent credential
rotation may change its validity interval but cannot extend the original operation
deadline. The selected `TG1` is the intersection of original authority/operation
bounds and current credential validity. This is restart-bound authority supplied
by trusted bootstrap, not live revocation or fresh authentication of a stdio caller.
The v3 reference application-bundle digest covers HTTP entry/admission code and their
declared engine ceilings; its separate dispatch bindings are trusted bootstrap inputs.
The newer v4 bundle also hashes the full automatic registry, including policy/effect
bindings. Neither identifies the native executable or establishes authenticated
artifact provenance; candidate admission remains required.

The implementation reuses the admission tool-filtering routine, state decoder,
model request builder and file argument adapter. The model payload must exactly
match the committed intent and the **filtered admitted** catalog, not the broader
operator catalog. A file payload must match the current pending tool and the
original tool permission. The first supported file role is `read_file` only.

Each worker's actual source/runtime hashes and grants must match its operator
binding. Model workers receive one exact network host and one named secret, with
no filesystem grant. Provider URLs must match that host; HTTP is restricted to
`localhost` or `127.0.0.1`, otherwise HTTPS is required. Optional ports are canonical
integers from 1 through 65535. File workers receive one exact absolute workspace
root, with no network or secrets; the root `/` is refused. Path containment,
including symlink/parent traversal, remains enforced by the actual worker runtime.
The argument adapter itself does not claim path containment.

Before another model call, unknown reported usage is refused. Reported input must
be below the input reservation, and reported output plus the next call's maximum
output must fit the output reservation. **This is not prospective input-token
metering, provider billing reconciliation, a spending guarantee or settlement.**
Those product controls remain required before enabling a pilot.

## Current cancellation-aware dispatch

[DF2](../app/pi/dispatch_cancellable.sigil) wraps the exact DF1 implementation,
adding a cancellation lookup key and its actual operation-namespace snapshot.
The current automatic binding supplies four lookup values instead of three: operation,
session, intent and cancellation key. A valid request refuses new dispatch (`499`);
inconsistent retained cancellation fails closed (`409`). Without a request, DF2
returns the exact original DW1 decision. The native store remains exclusively owned
from actual reads through claim/handoff, covering cancellation committed after an
earlier eligible finalizer no-op. There is no native cancellation-policy branch.
See [cancellation](cancellation.md) for records, request acknowledgement and races.
Old DF1 frames remain valid only for the explicitly selected reference component;
they are rejected by DF2. Current automatic deployments require the LB3 binding.

## Draft wire records

All records use a four-byte marker and eight decimal digits of UTF-8 byte length
before each field; trailing bytes are refused. These are data, not capabilities.

| Record | Ordered fields |
|---|---|
| `DF1` (11) | current CF2; current application bundle; requested operation ID; current Unix seconds; actual OQ2 SR1; actual BR1 SR1; actual PT1 SR1; actual SI1 SR1; actual BH1 SR1; actual intent key; selected EF1 |
| `EF1` (7) | alias; operator EB1; actual source hash; actual runtime hash; network-grant JSON; filesystem-grant JSON; secret-name JSON |
| `EB1` (7) | tenant; role (`model`/`read_file`); expected source hash; expected runtime hash; endpoint/workspace argument; exact network/filesystem grant; secret name (empty for file) |
| `DW1` (7) | alias; exact worker input; intent namespace; intent key; intent revision; TG1; opaque context |
| `DX1` (5) | operation ID; sequence; principal; tenant; application bundle |

Pi places DX1 inside DW1's opaque context. The native host parses only the shared
mechanical DW1 envelope; it neither interprets DX1 nor constructs pi policy.
DF1 errors are SIGIL refusals: malformed input `400`, denied authority/effect `403`,
expired operation `408`, inconsistent records `409`, unknown/exhausted reported
usage `429`. Native embedding errors remain static mechanism codes; they are not
yet a public API error mapping.

## Native-bound execution path

`sigil-claimed-worker/v2` requires a fixed grantless policy worker in addition to
the fixed effect worker, existing store and executor scope. Version 1 remains the
trusted claim-gate conformance path; version 2 rejects direct `prepare` requests.

An operator-owned input template has bounded literal fields, caller-value slots,
clock fields, scoped reads with fixed namespaces and literal/value-slot keys, and
held-worker facts. There are at most 16 caller values of 512 bytes each, 32 input
fields and 4 MiB of framed input. Policy source remains subject to the unchanged
64 KiB verified forge limit. Policy execution has no I/O grants, read handles are
read-only, and its independent timeout is at most 30 seconds. Effect execution
retains the fixed worker's fuel/timeout ceilings.

The pi fixture template accepts only three lookup values: operation ID, session
name and intent key. CF2, bundle, namespaces and EB1 come from bootstrap; snapshots,
clock and EF1 metadata come from the host. This provenance depends on admitting
the **correct operator template**: the generic host does not recognize CF2 or
forbid an operator from constructing an inappropriate template. Full bootstrap/
package admission remains an MVP requirement.

The host exclusively borrows the same store while reading, invoking policy,
preparing, claiming and executing. The selected alias must equal the fixed worker,
and its intent coordinate/revision must match a positive retained snapshot actually
read for the policy. There is no caller-visible gap in which storage can be replaced
before preparation. Fresh time checks run after policy and at the existing claim
and execution boundaries. A policy proposal alone never arms execution: only the
gate's own successful, exactly bound claim commit does that.

Worker metadata exposes hashes, network/filesystem grants, secret **names**, and
ceilings only. It does not expose source text, secret values or environment-variable
names. The worker still checks its pinned executable before launch and uses fresh
isolated execution with host-injected secrets. This does not establish signed
verification provenance or eliminate trusted package-path assumptions.

The trusted stdio controller retains scoped get/commit between attempts; it is
not an untrusted user or model interface. The pi fixture's executor scope does not
expose the policy's operation, conversation, reservation or counter reads; admitting
the equivalent restricted scope in a product bootstrap remains required. During an attempt
it may only claim, execute or cancel. Actual result recording/routing is still
driven by the trusted integration fixture, so fabricated result provenance is not
claimed to be solved by this path. Cancellation, remote-effect uncertainty,
old-worker cleanup and backup-rollback limits from the claim/worker contracts
continue to apply. Checking authority at initiation is not completion before expiry.

The additional [version-3 completion path](worker-completion.md) closes the manual
result-provenance gap within this local mechanism. It requires a fixed grantless
SIGIL completion worker, owns actual claim/result commits and observations, refuses
arbitrary commit/claim/execute commands, and records abandoned-claim uncertainty
without resending. Version 2 retains the manual behavior described above. Neither
version is the automatic HTTP service dispatcher.

## Remaining product work

The [v4 automatic service](automatic-service.md) now connects the admitted worker
registry, current authority, SIGIL discovery/selection, result interpretation and
[terminal settlement](settlement.md). Complete pre-claim refusal/expiry handling and
public cancellation remain open. Qualify full quotas, tool/route parity, artifact admission,
the browser and the remaining M0–M8 gates. The fixture's fixed registry, scripted
provider and controlled transport are not a substitute for those product paths.
