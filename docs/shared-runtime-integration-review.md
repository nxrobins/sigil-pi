# Shared runtime integration review: pi, control plane and AIN

Observed: 2026-09-09. **Read-only engineering review, not an approved M0 contract,
an M6 integration result, or readiness of any of the three projects.**

The ownership boundary in [the MVP goal](mvp-goal.md) remains appropriate. Current
implementations are aligned in intent, but they are not interchangeable runtimes
or wire-compatible consumers. Keep pi's current pinned Wasmtime path while making
the shared action/result boundary explicit. AIN remains optional future research.

## What was actually inspected

- **sigil-pi:** main remains v8. The isolated readiness stage is
  `/private/tmp/sigil-pi-readiness-host.BiNfIvYV`, with SIGIL pinned to
  `8277a1d92d599df89e6b4391fc70fd0fa534d696`. Its newer mechanisms and pending
  verification are recorded in [the acceptance record](mvp-acceptance.md).
  They are not an admitted release or a frozen shared runtime. The draft semantic
  contract is [sigil-action/v1](execution-contract.md).
- **Control plane:** inspected `/Users/nigel/Projects/sigil-control-plane` at
  HEAD `3cdb48b`, including its substantial working-tree updates. The application
  profile still selects **v19dj**, and the live ViewSpec selector is **v65/v31**.
  The README describes v19dk as a tested successor candidate, not the active line.
  Reported build/browser results were read, not rerun here. HEAD alone does not
  identify the working-tree application; relevant file hashes appear below.
- **AIN:** inspected the active research record in
  `/Users/nigel/Projects/SIGIL/.worktrees/ain20-l5`, initially at
  `cd2a6e086d78f1eae0f6f5c62ec1b189fe328137`, then checked its newer committed
  record at `6050f096f1b833d01349052fa384aeb09483c72e` during this review.
  Its dated CP401 entries name
  **CP400 as the latest accepted checkpoint; CP401 is partial/not accepted**.
  This is newer than the charter's historical CP324 review. No proof, benchmark
  or production build was rerun. The older bridge worktree and the SIGIL main
  export are not substituted for this research record.

No sibling source, state, pin or selector was changed; no service was started in
those projects. This snapshot is not a claim about work completed after inspection.

## Reuse boundary: aligned responsibilities, different mechanisms

The control-plane service has two serialized SIGIL tools. `control_plane` has no
outbound authority; it reads domain, intent and delivery roots and writes only
domain and intent roots. `adapter_executor` cannot access the domain root: it
reads intent/delivery and writes delivery. Its sole outbound grant is the exact
POST target `http://127.0.0.1:13101/agent`, not a general network grant. See
`app/service.tenancy-runtime-v19dj.json:192` in the control-plane checkout.

The adapter source, `app/src/adapter_executor_v3.sigil`, documents and implements
one serialized executor over two fixed company slots. It commits a running marker
before its single non-redirecting `http_post_once_status` call. A matching running
record after interruption becomes `delivery_unknown` without redispatch; a
matching terminal delivery is replayed without rewriting. Its stated assumptions
include immutable intent until terminal consumption and no cross-replica CAS.

| Boundary | Current difference | Required integration decision / evidence |
|---|---|---|
| Intent, claim and result | Control plane uses binary intent/delivery layouts and fixed slot keys. Pi uses framed proposals/records, including DW1 and SD1, and revision-bound claims. | Specify exact semantic identities, immutable payload/authority binding and encoding. A SIGIL adapter or versioned migration must reject malformed, late and mismatched records. Similar field names are not compatibility. |
| Durable state | Control plane uses named KV grants and serialized execution. Pi's local Store supplies revision-checked atomic batches and receipt-bound effect initiation. | Preserve each application's acknowledged-state guarantees. Prove any multi-record transition/recovery protocol; do not treat independent writes as a transaction or infer cross-replica fencing. |
| External exchange | Control plane's host patch provides exact method/target, no redirects and distinct response/not-sent/uncertain observations. Pi uses separately held effect workers and hard cancellation. | Preserve the exact outbound restriction and observation distinctions. Worker termination cannot erase an uncertain external effect. Neither existing primitive is accepted as equivalent without boundary tests. |
| Artifact admission | Control plane's application builder uses a different compiler revision and an explicit patched host profile. Pi's staged workers use the pinned revision above. | Bind compiler/proof provenance, exact artifact, host ABI, grants, engine configuration and target for each admitted tool. A certificate flag or matching source name cannot qualify a different host. |
| Execution budgets | The composed control-plane application is 1,276,296 bytes; its adapter is 53,729 bytes. Pi's staged source ceiling is 65,536 bytes, with eight entry evaluations and at most eight fixed functions. | Reuse bounded mechanisms, not a wholesale application-host swap. Do not raise pi's limits to fit the large application. The adapter fitting the byte ceiling does not prove its imports, proof profile or behavior are supported. |
| Policy ownership | Both applications already separate application choices from effect privilege. Their identity, allowance and recovery rules differ. | Keep those rules in each application's SIGIL code. Shared native code supplies facts, durable mechanisms and independent ceilings, with no application-specific exception added to pass M6. |

The active control-plane toolchain lock is **`app/toolchain.lock.json`**, consumed
through the application builder's lock verification, not the older
`kernel/sigil-toolchain-lock.json`. It pins
`dc1b40b1d104139f9172a4766af901e16685ef47` and eleven declared patches, including
delivery semantics, service/topology admission, serialization and engine changes.
This review checks the declared integration dependency, not the installed patched
binary or correctness of every patch. Pi's pin is not changed by reading it.

## Proposed real second-consumer slice for M6

Use the existing control-plane assignment-triggered work flow: accept an authorized
assignment/wake, publish its immutable effect intent, perform the bounded adapter
exchange, and let control-plane SIGIL interpret the correlated result into domain
state. Run it on the same versioned execution mechanisms used by a pi
model -> approved tool -> result -> response turn, with separate domain roots.

Before implementation in that repository, the owning work must agree on the
selected workflow/revisions and exact integration change. Required conformance
evidence includes:

1. Real intent -> observed result -> application interpretation, with actual
   scoped grants, receipts and both application/runtime identities recorded.
2. Restart before dispatch and after possible dispatch but before result commit;
   recovery preserves uncertainty and does not silently resend.
3. Replays, changed payloads, late/wrong attempt results and a second tenant cannot
   advance the wrong operation or obtain domain/effect privileges.
4. Cancellation and expiry have the stated local/remote semantics. A replacement
   executor cannot race a still-authorized old worker under the supported topology.
5. No application-specific native branch, broad outbound fallback, source-limit
   increase or unapproved compiler/proof substitution is introduced.

This is a proposed test target, not an owner-approved plan or a completed M6 test.
The existing two-slot adapter demonstration, a toy second consumer, or a common
schema document alone cannot satisfy M6. Control-plane product readiness remains
separate from this narrower reuse criterion.

## What the newer AIN record changes—and does not change

The CP401 entry distinguishes a proved original initialization target from a
refused newer compiler/entrance pairing and a successful diagnostic mutant control.
It explicitly does not claim an all-premise step counterexample or a repair.
Whole step-direction coverage, conditional settlement, address injectivity,
ambient-interface coverage and the full audit remain open. It makes no general
completion, performance, independence-reflection, public-trace or source-to-net
adequacy claim. These are the record's stated results and limits, not a new proof
assessment by pi.

The record advanced during inspection: a presented-v2 candidate and exact control
targets were frozen, with no v2 law/control proof run at that entry. It also
corrects an earlier full-body audit checker that could stop at the first line;
the earlier weak check is expressly not retroactively strong evidence. The newer
freeze retains v1 initialization and its E1 refusal. CP400 accepted / CP401
partial status and the open whole-interface obligations are unchanged. The hash
below identifies this newer committed record, not a moving working-tree claim.

The practical consequence is to preserve the existing backend boundary:

- Stable operation/authority identities and explicit causal dependencies must not
  depend on Wasm pointers, process IDs or interaction-net node addresses.
- Keep artifact/proof/host admission and observable conformance specific to each
  backend. Compiler provenance is not interchangeable merely because the source
  language is SIGIL.
- Serial execution is acceptable for the MVP; future parallelism or reordering
  must establish independence and matching observable effects. No AIN performance
  or completion assumption justifies relaxed limits or a weaker runtime boundary.
- Do not wait for AIN completion to develop or qualify this MVP, and do not call a
  future AIN runtime a drop-in replacement before it passes the contract tests.

## M0 decisions still requiring resolution

This inspection supplies concrete inputs to the required cross-project review; it
does not replace review with the owning work. Freeze the supported host/ABI and
artifact-admission profile, action/result schema and migration, dispatch-time
authority/revocation rules, durable transition guarantees, single-owner replacement
behavior and exact outbound semantics. Record the selected real control-plane
workflow and conformance evidence procedure. Pilot model, quotas, retention and
operational ownership still require their separate approvals.

All M0–M8 criteria remain unchanged and unqualified. Independent security review
can proceed alongside development; external-pilot clearance still depends on it.

## Snapshot identities

Paths in this table are relative to the named checkout above. Hashes identify the
bytes read, not proof of runtime admission, current external deployment or passing
product tests.

| Checkout / file | SHA-256 |
|---|---|
| Control plane: `app/application.profile.json` | `187218729a3ee016f0b6f22e826d152bd276106197a45933a8548c754383a78e` |
| Control plane: `ui/live-viewspec.json` | `e61242e39a722b9b61fb68ad3ba0843b0e71ef7607582817b41536ce4588a4f7` |
| Control plane: `app/service.tenancy-runtime-v19dj.json` | `5a4fa924b52239c517bbf322d6692e0e22efdad5a9e67e81cc6ff1299e185b4e` |
| Control plane: `app/toolchain.lock.json` | `2c4e55ba3fa66565c138284ef771179f6f9c7e453fa8e4b883876b114781126f` |
| Control plane: `app/src/control_plane_v19dj.sigil` | `0e5d4e961670b5ecf7cb29fe46d47d23b1060e87303f6f2e6b1d92ee59b09bbb` |
| Control plane: `app/build/control_plane_tenancy_runtime_v19dj.composed.sigil` | `4e7f274adf0c8201bcf7f18331227e4b280e4df49964b267927e326ef1692d76` |
| Control plane: `app/src/adapter_executor_v3.sigil` | `cc50217f75156f52245547662638b2d9ed18fdb1d493c1117735bfc0d8d59605` |
| Control plane: `app/build/adapter_executor_ten001_v3.composed.sigil` | `700ee978d3e5c71bf21744dd75fb60b31a0205c300e34f95c082ad8ff66a7006` |
| AIN l5 worktree at `6050f096f1b833d01349052fa384aeb09483c72e`: `docs/experiments/sigil-inets-v20/CHECKPOINTS.md`, CP401 results from line 37727 and subsequent v2 freeze | `dea507e8eda9fd6041ac3a66bc2289f3d3d08f01f04c9bd7212cdd01e3ef2d71` |
