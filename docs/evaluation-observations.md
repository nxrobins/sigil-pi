# Private observations of pure evaluations

Status: **opt-in failure publication implemented; focused component checks pass**.
Main remains v8. The preceding [682-case dispatch-audit result](dispatch-audit.md)
belongs to the earlier snapshot, not this changed host. The follow-up below records
224 focused checks; complete execution coverage and every MVP gate remain open.

## Actual capture boundary

`native/service/src/evaluation.rs` adds `Function::observe`. It invokes the same
admitted grantless fresh/cached evaluator and returns the original result alongside
native-created facts, including on failure. Constructors and fields are private
to the native module; there is no deserializer or request/guest observation API.
It grants no signing, storage, effect, retry or application authority.

The old `Function::invoke` and `invoke_observed` wrappers retain their result/error
shape. A successful `Policy::begin` now uses the actual observation's input/output
digests for the existing DP1 fields. The original capture-only checkpoint did not
intercept failures; the opt-in follow-up below now does so for policy/transaction
owners. Other callers using legacy wrappers do not acquire durable error coverage
merely because the native capture type exists.

| Fact | Meaning and limits |
|---|---|
| Fresh/cached mode | The actual selected native evaluator path; neither implies application success. |
| Admitted source/runtime hashes | Identities retained by the admitted native owner. On refusal they do not assert execution of those bytes; they are not compiler proof provenance or a signed observation. |
| Input length and optional content digest | Exact received UTF-8 input. Inputs over the existing 4 MiB bridge limit retain length but are not hashed, avoiding extra unbounded work before the original refusal. |
| Selected fuel / optional timeout | Fixed function fuel and actual selected timeout, not measured usage. Timeout is absent if the deadline refuses invocation before selection. |
| Elapsed time / native boundary / error / fault | Actual monotonic interval and mechanical result. Native runtime `application` error is not an authorization denial or a business verdict. |
| Optional request/cleanup facts | Only the fresh bridge's actual `Observation` supplies these fields. Cached `PureBridge` does not expose equivalent send/reap observations; both remain absent, even for a successful cached call. |
| Optional parsed-result digest | SHA-256 and byte count of `serde_json::to_vec` over the actual parsed runtime result, explicitly labeled `serde-json-value/v1`. Not original wire bytes or guest output. No raw result or diagnostic is retained in facts. |
| Output kind and optional digest | `not_observed`, `missing`, `non_string` or `string`. Only an actual string has its complete UTF-8 digest/length. An actual empty string is hashed; an unavailable output is not replaced by that hash. An error result can still contain an observed string without becoming success. |

These facts are an internal native type, not a frozen public wire contract.
Serialization contains metadata/digests only. Hashes are not encryption and can
expose equality; any future journal still needs private access/retention policy.
No raw credential, input, output, runtime diagnostic, path or key is published.

## Verification and retained failure

Nine new native tests exercise actual controlled runtime processes in both modes:
input/output/parsed-result digests and selected limits; runtime error with and
without output; empty/missing/non-string output; protocol fault; pre-invocation
expiry; oversized-input refusal; runtime-file replacement; and the old wrapper
contracts, including cached call IDs. These are mechanism fixtures, not compiled
SIGIL event-policy or actual application fault-journal evidence.

- Initial nine-case run passed in 3.80 seconds (`6f3257`); the first complete native
  run passed 196 cases (`e271c1`, 179 library in 13.62 seconds, 15 main and 2
  transaction), before admitted artifact identity fields were added.
- A subsequent complete run failed (`bdeb3a`, session 18353): 177 library passed,
  two failed, in 16.17 seconds. The existing hard-cancellation fixture did not
  reach worker startup in 5.0657 seconds; its actual result retained no-send,
  reaped/deadline and phase 4, with 4,146 ms spent in claim recording. A new runtime-
  error test got `worker` instead of `application`; that failure output did not
  preserve its precise native fault. Do not infer a stronger cause retrospectively.
- Only the nine new evaluator fixtures now share their own mutex, acquired before
  constructing workers or starting clocks. This bounds their mutual subprocess
  concurrency; the existing audit-fixture mutex, old tests, runner configuration,
  deadlines and assertions are unchanged. Redacted observation diagnostics were
  added to the new runtime-error assertions. This is not an offered-load test or
  proof that all timing variability has been eliminated.
- After that change, the full 196-case native suite passed (`f794e9`, library
  13.27 seconds). Then **five consecutive complete 196-case native runs passed**
  (session 68002, terminal `7b7c6b`, exit 0 observed 2026-09-09 20:31:52 UTC).
  Library durations: 13.01, 13.15, 15.97, 13.13 and 13.41 seconds. The loop exits
  immediately on any failure; it is not retry-until-pass. These are repetitions
  of 196 unique tests, not 980 unique cases.
- Formatting and warnings-as-errors passed (`f598e5`, `169234`). The source
  aggregate before the repeated runs was
  `1dcbd002f48ebf06b370cb9ff42cbe6b6c98cbeab67b83fbe0059c0a2f037bfa`
  (`3fb5e6`) and matched after completion (`9b98f2`). Later evidence-only edits
  change the whole-stage aggregate, not the tested source. A broader changed-host
  regression and new optimized build remain required.

The source, entry-evaluation, fixed-registry, fuel, memory, deadline and original
bridge limits are unchanged. The runtime pin and Cargo locks are unchanged. The
native tests neither access a real provider nor qualify Linux, load, restored
state, complete interfaces, an installed candidate or independent clearance.

## Original integration obligation

Intercept failed/refused policy and producer invocations while their private
facts still exist; bind actual admitted configuration, scoped context and relevant
observed reads. A fixed SIGIL policy must classify/redact the event. Keep evaluation,
proposal validation, attempt preparation and actual publication distinct.

Publication must have a bounded, truthful outcome, cannot extend an expired action
deadline, and cannot fabricate a claim, effect ticket, batch, delivered result or
commit receipt. Audit formatter/capacity/storage failure must stay visible without
recursive auditing or silent unaudited dispatch. Complete HTTP/route/no-op coverage,
capacity reservations, quotas, retention, independent checkpoints and M0–M8
qualification remain obligations; this native prerequisite replaces none of them.

## Opt-in failure publication follow-up — 2026-09-09

`native/service/src/evaluation_audit.rs` now connects actual policy/producer facts
to `app/pi/evaluation_audit.sigil`. The application classifier is composed by
`scripts/compose_evaluation_audit.py`; it has one entry, no storage/network/secret
grants, and **35,456 bytes**, below the unchanged 65,536-byte source cap. Its final
compiler-input SHA-256 is
`42bd3667f1cefa1517db02cef7862f945a25e36544147d71b60206c088eaf186`
(`8fd39f`), stdlib `b5f40e2eba41b6734f8db071`. The SIGIL pin remains
`8277a1d92d599df89e6b4391fc70fd0fa534d696` (`03a4ce`).

Audit configuration accepts exactly **v1 with no `evaluation` member** or **v2
with a concrete `evaluation: WorkerConfig`**. Explicit null, v2 without the member,
and v1 with it are refused. Omitting the member preserves the original v1 serialized
configuration and failure behavior. This is an internal development protocol,
not M0 approval, main integration or a release migration decision.

The optional classifier is retained/cached and boot-invoked before automatic
application state is created. EB1 has three fields: configured chain hash,
projector TM1 and canonical domain grants. It returns only
`evaluation_audit_admitted`, never a boot event or receipt. The private signing key
and authenticated head/entry scopes stay native. Runtime inspection includes both
the optional projector owner and its executable in the original bounded registry.
An owner precheck is not executable-byte verification; the latter remains separate.

EA1 has exactly ten fields:

| Index | Native-origin value |
|---|---|
| 0–1 | New audit identity and configured chain |
| 2–4 | Actual fully bound subject-config hash, subject TM1, projector TM1 |
| 5 | Actual subject storage grants, canonical sorted JSON |
| 6–7 | SHA-256 of bounded EL1 lookup-value framing and actual count (0–16) |
| 8 | Actual returned mechanical refusal code |
| 9 | Exact native `evaluation::Facts` serialization |

SIGIL validates the native struct's exact field order/types and consistency with
the admitted manifest. It distinguishes `pure_evaluation_failed` from
`pure_proposal_refused`: a successful evaluation followed by a mechanical binding
or preparation refusal is not a failed evaluation or dispatched effect. It returns
AR1 `publish` plus a redacted `sigil-pi/evaluation-failure-audit/v1` payload. That
proposal is not a signature or storage receipt. Existing chain consumers must
handle the new schema explicitly; old signed payloads are not rewritten.

`Policy::begin` and `Transaction::prepare` retain observations through their
failure paths. Successful preparation discards the pending failure observation;
later formatter/claim/commit failure cannot relabel it as a failed producer. The
owned-worker wrapper waits until the failed Attempt/Store borrow is gone, consumes
the observation once and invokes its attached publisher. A new call clears stale
observations before busy/input checks. A fresh evaluation of the same lookup values
is a new observation, not deduplicated by this mechanism.

Publication appends only the authenticated head/entry batch: it creates no domain
mutation, claim, delivery, effect ticket or retry. It checks the **original subject
deadline**, without starting a replacement reporting window. These checks gate
projection/append; they do not make a native atomic storage commit preemptible or
prove an in-progress append always returns before that deadline. Once the deadline
has expired, no append is attempted and reporting fails truthfully.

After an observed failure, successful publication preserves the original error;
projection/capacity/storage/deadline refusal returns `audit_recording`. An uncertain
native commit returns `audit_commit_uncertain`, not a receipt or proof that no record
exists. The consumed observation is not automatically replayed, and auditing the
reporter does not recurse. Pre-evaluation input/read errors still have no completed
evaluation to publish. The automatic embedding exposes the error to SIGIL and its
existing `coordinator_refused` diagnostic; complete readiness/alert policy is not
established by that diagnostic.

## Follow-up verification and retained failures

- Initial compiled structural classification: 50 passed in 16.69 seconds,
  session 33802, `e02a34`. That earlier classifier was 35,227 bytes,
  `e3d7b6e8e25ca4e8eea6a75f7e1a7dcd4324ce37fa1b6810d8fc3440c9c8ebc0`.
  Four additional cases subsequently cover actual attempt-refusal codes and empty
  subject grants. Synthetic structured facts are not native provenance evidence.
- Initial new controlled-native run: **6 passed, 3 failed in 4.67 seconds**,
  session 14787, `04c0db`. Two new transaction fixtures incorrectly used empty
  storage scopes, rejected by the original Store contract. They now use a scoped
  domain read. Another incorrectly expected the limited owner precheck to detect
  executable replacement; actual invocation already rejected it. The test now
  asserts the documented precheck, while actual HTTP tests independently prove
  artifact inspection detects the replacement. No original test or boundary changed.
- All nine new controlled-native cases then passed in 6.32 seconds, session 54457,
  `4f6ed4`. The full service suite passed 205 cases (188 library in 17.94 seconds,
  15 main, 2 transaction), `40caad`; that combined output also retained a failed
  warnings-as-errors check for a collapsible `if`. The style error was corrected
  before the complete retained prerequisite run below, without an allow/suppression.
- **224 passed in 1,510.99 seconds**, session 54902, terminal `94d701`, exit 0
  observed **2026-09-09 21:34:36 UTC**. This selector contains 54 compiled structural
  cases, 8 actual compiled transaction-publication cases, 19 actual HTTP failure/
  startup/integrity cases, and 143 original audit regressions. No skips or expected
  failures. The original native formatting, warnings-as-errors, tests, debug and
  optimized builds, and fixed-evaluator lifecycle prerequisite were retained.
  Post-run service enumeration confirms 205 unique cases; Store 125 and worker 22
  remain unchanged, for 352 unique staged native prerequisites. Repeated fixture
  runs do not add unique cases.

The tested stage aggregate matched **before and after** (`1479ce` / `5dd34a`):
`0b8b9a804750af3e7bfa0941234d7dcb7868db4d42954039cc0f38966b7cc5dd`.
Subsequent documentation edits change the whole-stage hash, not this tested code.
The three Cargo locks are unchanged. Optimized binaries from this run (`5dd34a`):

| Artifact | SHA-256 |
|---|---|
| Application host | `eaf8f98f54197a0d3e759b9be47fbb52405079baafd2806293cb3ff75a4395c5` |
| Transaction executable | `31c46769793ff2be7bacdf6d7fbca5fefe1c334cce725c06649c5bea9fa11401` |
| Claimed-worker executable | `c702ad6eabb6ff068334abe363efe7fcc6a98a0391540b8c3adf34df9738a29e` |

The focused selector is **not the full earlier 682-case regression**, its frozen-v8
compatibility coverage, or a qualification of a changed candidate. Explicit v2
normal model→tool→response coverage, fuller reporting-fault injection and the
broader changed-host run remain next. Full actor/operation/read-set correlation,
checked clocks and outer refusal-stage detail, entry/coordinator/no-op coverage,
capacity reservations, retention, independent anchors and readiness remain open.
No main code, runtime pin, sibling repository, paid provider, deployment, commit
or push was changed; all M0–M8 gates remain unqualified.

Exact focused reproduction, from the stage directory, using local scripted
provider networking only:

```sh
env -u CARGO_TARGET_DIR -u PI_FORGE_BIN -u PI_TOOLCHAIN_DIR -u PI_STDLIB_DIR PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/sigil-pi-readiness-host.BiNfIvYV:/private/tmp/sigil-pi-readiness-host.BiNfIvYV/tests:/Users/nigel/Projects/sigil-pi/tests:/Users/nigel/Projects/sigil-pi SIGIL_ROOT=/private/tmp/sigil-pi-pinned-8277a1d9.9NXtMw PI_REQUIRE_TOOLCHAIN=1 CARGO_NET_OFFLINE=true CARGO_BUILD_JOBS=2 /Users/nigel/Projects/sigil-pi/.venv/bin/python -m pytest --noconftest -p conftest -p no:cacheprovider -o addopts= -xq tests/test_evaluation_audit.py tests/test_evaluation_audit_publication.py tests/test_automatic_evaluation_audit.py tests/test_audited_transaction.py tests/test_audited_transaction_boundaries.py tests/test_automatic_effect_audit.py tests/test_automatic_audit_admission.py tests/test_automatic_effect_audit_admission.py
```

### Full changed-host regression completed — 2026-09-09

Session **18302** completed with **766 passed in 2,746.78 seconds** (45:46),
terminal `f025eb`, exit 0, observed at **22:32:56 UTC**. No behavioral case was
skipped or xfailed. This retains the original 682-case selector and adds the 84
actual/synthetic evaluation-audit cases, including the v2 normal two-tenant
model/tool/follow-up/restart case and reporting-fault coverage. The original native,
pinned 4,096-call evaluator and frozen-v8 prerequisites remain. The staged native
census is 355 unique tests: Store 125, worker 22 and service 208.

The source aggregate matched before and after execution:
`c7d547ba00422cc0228e1fe8736a43fd7b7e28ef0246d532cb08c4ac84adc4fc`
(`d892df` / `3ca4bc`). The three optimized executable hashes and Cargo locks
still match the preceding focused-224 artifact table (`5e2f2d`). That table's
binaries are now covered by this broader result; historical failures remain above.

Reproduction from this readiness directory, with local scripted-test network
permission and the pinned source checkout:

```sh
env -u CARGO_TARGET_DIR -u PI_FORGE_BIN -u PI_TOOLCHAIN_DIR -u PI_STDLIB_DIR PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/sigil-pi-readiness-host.BiNfIvYV:/private/tmp/sigil-pi-readiness-host.BiNfIvYV/tests:/Users/nigel/Projects/sigil-pi/tests:/Users/nigel/Projects/sigil-pi SIGIL_ROOT=/private/tmp/sigil-pi-pinned-8277a1d9.9NXtMw PI_REQUIRE_TOOLCHAIN=1 CARGO_NET_OFFLINE=true CARGO_BUILD_JOBS=2 /Users/nigel/Projects/sigil-pi/.venv/bin/python -m pytest --noconftest -p conftest -p staged_transaction_regression -p no:cacheprovider -o addopts= -xq tests/test_automatic_evaluation_audit.py tests/test_evaluation_audit_publication.py tests/test_evaluation_audit.py tests/test_automatic_effect_audit_admission.py tests/test_automatic_effect_audit.py tests/test_dispatch_audit.py tests/test_effect_audit.py tests/test_automatic_audit.py tests/test_automatic_audit_admission.py tests/test_audited_transaction.py tests/test_audited_transaction_boundaries.py tests/test_transaction_audit.py /Users/nigel/Projects/sigil-pi/tests/test_native_dispatch.py /Users/nigel/Projects/sigil-pi/tests/test_recorded_worker.py /Users/nigel/Projects/sigil-pi/tests/test_owned_worker.py /Users/nigel/Projects/sigil-pi/tests/test_native_settlement.py /Users/nigel/Projects/sigil-pi/tests/test_native_preclaim.py tests/test_bootstrap_admission.py tests/test_bootstrap_admission_compat.py tests/test_readiness_admission.py tests/test_lifecycle_sigil.py tests/test_bootstrap_admission_host.py tests/test_readiness_admission_host.py tests/test_readiness_storage_host.py tests/test_legacy_readiness_native_fixture.py
```

This result does **not** apply to the later recorder working copy at
`/private/tmp/sigil-pi-recorder-audit.Jl8kYpDT`. That copy has different executable
bytes and its own incomplete integration result. Main integration, complete audit
coverage/attribution/capacity/health/retention, route parity and the qualifying
candidate/Linux/load/model/onboarding/control-plane/security-review evidence all
remain open. No full M0–M8 gate is qualified by this component regression.
