# Worker lifecycle audit publication (staged v9)

Status: local component work, not MVP qualification. This lives in the isolated
readiness stage; main remains v8. See the main MVP goal and acceptance record for
the unchanged M0–M8 gates.

## Ownership and admission

Trusted automatic participant configuration may enable `effect_audit` for all
its effect lanes. Its shape is the existing audit configuration v1: a grantless
SIGIL worker, native key environment reference, chain identity, separate head and
record namespaces, and bounded payload/count/byte limits. Omission retains the
original unaudited path; explicit null is rejected. This optional embedding is
not an approved pilot policy.

The complete registry is checked before state creation. Audit namespaces cannot
overlap any tenant domain, claim, delivery, transaction-audit or other effect-audit
namespace. Audit access cannot be delegated to the coordinator, transaction
producer, effect worker, dispatch policy or credential. The projector has no
network, filesystem or secret grants. Key bytes remain native. Authentication
is not encryption; access, export and retention still require product policy.

WB1 contains actual configured recorder/projector manifests, the original storage
scope, the admitted effect worker manifest, and opaque actual credential facts
with their native digest. The supplied pi SIGIL policy interprets CF2 and returns
WB2 with admitted AT1 identity metadata. Native code does not parse principal,
tenant or application authorization rules. Admission creates no execution, receipt
or signed event. Static grant metadata is retained completely or rejected, never
truncated. The additional audit profile bounds do not enlarge an existing ceiling.

Admitted artifacts, formatter and native key are retained across state opening.
Every existing configured effect chain receives a bounded full integrity check
before automatic dispatch. Wrong keys, corrupt historical entries, tombstoned
heads and orphan entries refuse restart. Clean absence remains uninitialized,
not verified healthy history. Independent inventory/checkpoints are still needed
to detect complete erasure or coherent rollback.

## Actual lifecycle facts and atomic publication

The native recorder supplies a private WA1 record from actual preparation,
invocation and held storage bindings. There is no HTTP, CL1 or stdio operation
that accepts caller-provided audit events, observations, grants or receipts.

- EM1 identifies the admitted effect source/runtime and complete worker grant
  ceilings, including secret names but never their values.
- WP1 identifies the actual prepared input digest/byte count, selected fuel and
  timeout, and checked time guard. This is the input before host secret
  substitution, not a hash of network wire bytes.
- WO1 retains actual observation kind, generation, may-have-run/reaping flags,
  native fault, runtime status and output shape/length. Only a complete retained
  string has an output digest; missing, non-string and oversized output do not
  become the digest of an empty or truncated string.
- Other facts bind actual intent/claim/delivery coordinates, original claim and
  observer generations, recorder input/output and exact returned-batch digests,
  actual recorder budgets/timing, and admitted participant context.

The supplied SIGIL policy emits `sigil-pi/worker-lifecycle-audit/v1`. It omits raw
conversation, input, output, provider keys and audit keys. Identity is the actual
recording participant's configured context; it is not a substitute for a complete
record of the earlier dispatch authorization decision.

| Event | Meaning at that point | What it does not establish |
|---|---|---|
| prepared | The pre-dispatch claim and its audit publication committed together | That no effect happened later, or that a remote request was delivered |
| observed | The actual local worker observation was recorded | Successful business effect or known remote cancellation |
| refused | A local gate refused invocation after the claim | A remote execution or response |
| abandoned | Recovery found a retained claim without its original local owner | The old worker's current executable, input, grants or remote outcome |

Abandoned recovery retains the old claim generation and a distinct recovery
observer generation, but deliberately omits EM1/WP1. Current configuration cannot
prove the old execution's artifacts or input. Recording timestamps describe
record production, not provider latency. A cancellation acknowledgement means
requested: the pinned HTTP shim's independent two-second timeout can produce an
observed runtime error before hard cancellation takes effect. These outcomes
cannot be rewritten to one another.

Claim and delivery publication each use one original Store transaction containing
the original validated batch plus authenticated append/head update. Only its
actual receipt arms dispatch or confirms a terminal phase. A read-only native
`check_batch` first checks the original caller scope and original per-batch
ceilings; the publisher's private audit scope cannot compensate for missing
caller authority. That preflight performs no I/O, does not reserve capacity,
check retained revisions, or acknowledge a commit.

The original initiation guard is checked again after audit projection/head read.
Recording an already observed effect has its own original bounded recording
interval. No deadline is reset or widened. Failed publication has no unaudited
fallback, fabricated receipt, phase, or effect retry. A full chain can prevent a
claim; exhaustion after execution can leave an observation uncommitted and require
truthful uncertain recovery. This is not yet a complete reservation/quota policy.

## Evidence boundaries

The compiled-policy suite has 49 cases. Twelve HTTP cases use the actual draft v9
SIGIL entry, real automatic/owned-worker mechanisms and deterministic local
providers: model/file-tool/response, follow-up, replay, tenant separation,
restart, cancellation/transport-error distinction, tampered history and failure
to publish a claim. Fifty-two startup cases reject invalid scopes, keys, limits,
artifacts and boot replies before state creation or provider sends.

Six native component fixtures check actual commit receipts, original-scope
preservation, capacity before/after invocation, unchanged initiation expiry and
actual hard-cancellation facts. They use controlled protocol responders, not
compiler or real-model evidence. Their cancellation input is checked separately
from compiled SIGIL projection and real HTTP behavior. Two Store tests establish
that the original-scope preflight neither mutates nor reserves state.

No browser, live-model, Linux, load, independent-review or real control-plane
qualification is inferred. Full event inventory, denied/failed-decision coverage,
dispatch-decision provenance, reservations/quotas, retention/archival/migration,
independent checkpoints and final product readiness remain open.

## Verification record

Full regression: **600 passed in 2139.63 seconds**, session **84746**, terminal
**dd7997**, exit 0; completion observed **2026-09-09 18:37:32 UTC**. This retains
the prior 433 behavioral cases, adds 113 new cases, and includes 54 original
recorded/owned-worker cases. No skips or weakened original expectations were used.

All original prerequisites passed: formatting, warnings-as-errors, debug and
optimized builds, the complete fixed-evaluator lifecycle verification, and the
frozen-v8 fixture gates. Native enumeration confirms **125 Store + 22 worker +
185 service = 332 staged native tests**. Changed Python passes F/E9 lint.

Before/after tested stage aggregate:
`ca81e68d6d1e7eb9982dd925e814bb7c560ceb5ba61f3b1a39e682e56fbcce20`.
The unchanged main snapshot before this documentation update:
`11de50c191bbbba19f3557e7792421cfe04509eb3c3de1acc88855aba7501b92`.

Composed effect-audit policy: **37,171 bytes**,
`df6b8a9a80ff540d4a6b2530c9677316e2322dc15cae71b4fdf99dfd25939d6c`.
Optimized application host:
`8000cd4319ee916a84f52fd2b5b59a3993bc2e05b8709e162d77f6282643fc61`.
Optimized transaction CLI:
`1b71c2573e4a29b53a711808b84e11de07cfe5d23925fc86634d357b6e28163c`.
Optimized claimed-worker CLI:
`7dfb24e80f89006842b2b13c5b823df14f6f24a32bacf8584200abc4cbb644c2`.

SIGIL remains pinned at `8277a1d92d599df89e6b4391fc70fd0fa534d696`;
stdlib identity `b5f40e2eba41b6734f8db071`. Cargo lock identities are unchanged:
service `6e0c513bb54c980a7967e6f8ac9d1ff7a01963ff4ee68d179c90f9cde18595eb`,
Store `aa0a2004ec94ccab8518ad9bf45cef389515565655457810db404fe44a3f5c64`,
worker `96a8e8fe4a5b35e51e32e93b354485ba6210f94b5704f6bc480a2771f4899c53`.
The original 65,536-byte source cap, eight entry evaluations, five configured
entry functions, fuel/memory/deadline limits and frozen-v8 snapshot are unchanged.

Exact local reproduction, from the existing stage:

```sh
env -u CARGO_TARGET_DIR -u PI_FORGE_BIN -u PI_TOOLCHAIN_DIR -u PI_STDLIB_DIR \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH=/private/tmp/sigil-pi-readiness-host.BiNfIvYV:/private/tmp/sigil-pi-readiness-host.BiNfIvYV/tests:/Users/nigel/Projects/sigil-pi/tests:/Users/nigel/Projects/sigil-pi \
  SIGIL_ROOT=/private/tmp/sigil-pi-pinned-8277a1d9.9NXtMw \
  PI_REQUIRE_TOOLCHAIN=1 CARGO_NET_OFFLINE=true CARGO_BUILD_JOBS=2 \
  /Users/nigel/Projects/sigil-pi/.venv/bin/python -m pytest \
  --noconftest -p conftest -p staged_transaction_regression -p no:cacheprovider \
  -o addopts= -xq \
  tests/test_automatic_effect_audit_admission.py tests/test_automatic_effect_audit.py \
  tests/test_effect_audit.py tests/test_automatic_audit.py \
  tests/test_automatic_audit_admission.py tests/test_audited_transaction.py \
  tests/test_audited_transaction_boundaries.py tests/test_transaction_audit.py \
  /Users/nigel/Projects/sigil-pi/tests/test_recorded_worker.py \
  /Users/nigel/Projects/sigil-pi/tests/test_owned_worker.py \
  /Users/nigel/Projects/sigil-pi/tests/test_native_settlement.py \
  /Users/nigel/Projects/sigil-pi/tests/test_native_preclaim.py \
  tests/test_bootstrap_admission.py tests/test_bootstrap_admission_compat.py \
  tests/test_readiness_admission.py tests/test_lifecycle_sigil.py \
  tests/test_bootstrap_admission_host.py tests/test_readiness_admission_host.py \
  tests/test_readiness_storage_host.py tests/test_legacy_readiness_native_fixture.py
```

The full selectors retain the prior regression suite and additionally include
the original recorded-worker and owned-worker suites against the staged binaries.
All native/evaluator prerequisites, formatting, warnings-as-errors, original
fixtures and deadlines remain required. The stage test support documents the
fixture overrides; they select binaries, not different expected outcomes.

## Preliminary failures retained

1. Session 38532, terminal a69718: 3 passed, 1 failed in 470.66 seconds. The new
   HTTP cancellation test assumed an acknowledgement guaranteed a native
   cancelled fault. The retained audit actually showed an observed runtime error,
   consistent with the pinned two-second HTTP timeout. The corrected test requires
   requested acknowledgement and truthful uncertain outcome, with exactly the
   legitimate cancellation/transport-error alternatives. A separate uncancelled
   HTTP error case and an actual native hard-cancellation case were added.
2. Session 86212, terminal 06a099: 60 passed, 1 failed in 804.01 seconds. An invalid
   WB2 reply was correctly rejected as protocol, outside the new test's expected
   error labels. That case now specifically requires protocol rejection, retaining
   the no-state/no-send assertions.
3. Session 82473, terminal 84c301: 121 passed, 1 setup error in 1178.47 seconds.
   All 113 new behavioral cases passed, but a repeated native gate failed two new
   invocation preconditions. A fixture-only mutex did not eliminate the issue:
   diagnostics subsequently showed pre-invocation deadline/refusal, including a
   5.418-second attempt with no worker startup trace. Merely repeating a passing
   subset was not accepted as a solution.
4. The new fixtures had constructed/admitted their generation-specific recorder
   after starting the action clock, unlike the real service's pre-admission.
   They now admit a controlled responder first; it copies the actual incoming
   WF1 generation and remains subject to every original native binding check.
   Only these six new fixtures are serialized before their clocks begin. The
   existing helper/tests, runner settings and five-second/200ms limits are intact.
   All six targeted cases passed in 5.13 seconds (7abe63), followed by eight full
   185-test native service repetitions (f27e78). Library durations were 9.65,
   9.60, 9.51, 9.56, 12.59, 11.50, 9.48 and 9.46 seconds. These repetitions do not
   replace the complete integration rerun above.

Earlier build-only corrections included a reserved SIGIL variable name and a
missing test-only Rust import. Neither changed execution limits or old fixtures.

## Dispatch-provenance follow-up

[The subsequent DP1/WP2 contract](dispatch-audit.md) adds actual successful policy
evaluation provenance and preserves unavailable evidence on recovery. It also
records a further controlled-formatter lifecycle correction and its repeated
native checks. Its broader regression passed 682 tests in 2,250.49 seconds,
session 26121, terminal `7b2fa2`, with matching before/after source fingerprints.
See that contract for the changed snapshot, 334 native prerequisite cases and
executable identities. The earlier 600-test pass above applies only to its own
recorded snapshot; neither result establishes complete audit coverage or MVP
readiness.
