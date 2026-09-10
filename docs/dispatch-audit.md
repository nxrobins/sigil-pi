# Dispatch-policy provenance in worker audit records

Status: isolated readiness stage; main remains v8. This is component evidence,
not candidate admission or an M0–M8 pass.

## Boundary and ownership

The native policy owner now retains evidence of the actual successful fixed
policy invocation that produced a bound DW1 proposal. It does not reinterpret
SIGIL authorization, accept an external authorization result, or add a signing
operation. A private, non-serializable DispatchEvidence is created only inside
Policy::begin after the actual returned alias and intent read binding pass.

The admitted policy manifest and digest of its fully bound Config are retained
at Policy::new. That configuration includes the actual credential/bundle literals,
ordered input template, read grants, alias, effect binding and claim namespace.
The digest is SHA-256 over serde_json serialization of that typed configuration:
struct declaration order, ordered input vectors and BTreeMap key order. It is not
a general canonical-JSON or path-independent artifact identity. Retain the exact
private deployment configuration for independent reconstruction.

Input/output hashes and byte counts refer to the complete actual policy request
and returned DW1, not reconstructed fixtures or caller-provided claims. No raw
credential, input, output, binding, conversation or opaque application context is
logged. Hashes are not encryption and can reveal equality; the audit remains a
private authenticated record, with export/access/retention still requiring policy.

## Versioned facts

Existing WA1 keeps its exact 28-field envelope. Its prepared field remains WP1 for
manual preparation with no policy evidence. Successful native Policy::begin
instead constructs WP2: the same five prepared fields plus one DP1 record. The
private owned handoff retains that exact prepared evidence for completion;
synchronous execution uses the same recorder path.

DP1 has exactly 16 fields:

| Index | Actual native fact |
|---|---|
| 0 | TM1 policy source/runtime digests and selected fuel/configured timeout ceiling |
| 1 | SHA-256 of the fully bound typed policy configuration |
| 2–3 | Actual framed policy input SHA-256 and UTF-8 byte count |
| 4–5 | Actual returned DW1 SHA-256 and UTF-8 byte count |
| 6–7 | Ordered actual read-set SHA-256 and read count |
| 8–9 | Clock supplied to the policy, then the checked post-evaluation clock |
| 10 | Monotonic policy interval in milliseconds, including reads and input construction |
| 11 | Timeout actually selected by the native policy invocation |
| 12 | Held effect alias checked against DW1 |
| 13–14 | Hash of DW1's exact selected effect input and its returned TG1 |
| 15 | Hash of DW1's opaque returned application context |

The read-set digest covers DS1 with one ordered DQ1 per actual template read.
Each DQ1 contains namespace, key hash, actual revision, some/none value presence,
and the complete value hash (empty bytes for none). Missing, tombstoned and actual
empty values therefore remain distinguishable. These use the existing four-byte
marker/eight-decimal-byte-length framing, not ambiguous concatenation. Only the
aggregate digest and count are published, not keys or raw snapshots.

SIGIL validates DP1 shape, hashes, bounds, policy ceilings and clock ordering. It
requires the selected effect-input hash and TG1 to match the actual prepared
fields. SIGIL adds an optional dispatch_policy object to the existing v1 lifecycle
event; this is an additive field, not a rewrite of old retained signed payloads.
Old/manual WP1 and abandoned records project null; older persisted v1 events can
lack the field. Consumers must not equate absence/null with an observed denial.

No policy proposal, digest or audit payload arms an effect. The original claim
and authenticated append still need one actual Store commit receipt. A subsequent
refusal retains the original evaluation evidence without claiming dispatch.
On abandoned recovery, the existing native publisher omits all original effect
and prepared facts, including DP1, even if the current configuration looks equal.
The earlier committed prepared event can retain evidence; the recovery record
cannot manufacture evidence of the lost execution.

## What this does not prove

A successful DW1 and committed claim do not establish successful business effect,
external delivery, remote cancellation, or future revocation. Policy elapsed time
is not provider latency. The TM1/config hashes are not independently authenticated
compiler verification provenance or a qualifying release manifest.

This does not publish denied-policy, malformed-policy-output or failed-invocation
events. Failures before a valid bound proposal still create no claim or effect.
Full pre-claim rejection/error inventory, HTTP/authentication decisions, audit
capacity reservation/settlement, quotas, retention/migration, independent anchors
and final readiness remain open. It does not replace live-model/browser/Linux/
load/control-plane evidence or independent security review.

## Verification

Component evidence: **82 compiled audit-policy cases passed in 37.53 seconds**
(49 retained + 33 new; session 85001, terminal `6a0763`). The initial native service
run passed 187 tests (170 library + 15 main + 2 transaction; `a1e140`). All original
formatting/warnings-as-errors gates remain.

The broader selector retains the previous 600, adds the 33 new pure cases and
49 original native-dispatch cases, and strengthens actual HTTP lifecycle
assertions. Its final rerun **passed all 682 tests in 2,250.49 seconds**
(37 minutes 30 seconds), session 26121, terminal `7b2fa2`, exit 0 observed at
2026-09-09 20:06:56 UTC. There were no skips or expected failures in this selector.
This is local macOS source/mechanism evidence, not a full integrated product gate,
Linux qualification, real-model task score or release-candidate result.

Preliminary broad runs:

- Session 48266: zero behavioral passes, one fixture setup error in 313.73 seconds
  (`47e944`), because the sandbox denied the scripted localhost server's socket.
  The unchanged run was restarted with local networking permission; no paid
  provider or deployment was involved.
- Session 60713: 146 behavioral passes, one repeated native prerequisite error in
  899.34 seconds (`52c0c1`). All 52 startup-admission cases, 12 actual HTTP lifecycle
  cases and 82 compiled policy cases passed. The repeated native gate passed 169
  library tests but the hard-cancellation fixture did not observe worker startup
  before its original five-second deadline. Native truthfully retained a no-send,
  reaped deadline observation and unknown phase 4. No relevant leaked worker
  remained in a subsequent read-only process check (fdb9da).

The new fixture's earlier recorder pre-admission correction was insufficient.
Another concrete mismatch was identified: EffectAudit::fixture used a cold Fresh
formatter, whereas real Admitted::new retains a Cached formatter and performs its
WB1 admission before any Attempt. The controlled fixture now uses Cached with a
pre-action fixture boot, a persistent JSON-RPC responder, and an assertion that
one actual formatter process serves both claim and completion. This is not
compiled boot-policy evidence: the fixture boot has a deliberately controlled
reply and creates no audit publication. Only the real HTTP tests validate WB1.

All original effect-initiation/recording deadlines and hard-cancellation/receipt/
no-send assertions remain. The fixture's forced publication delay remains after
boot. Bounded failure diagnostics report recorder timing, not raw private input.
No duplicate-prerequisite bypass, retry-on-failure or runner serialization change
was introduced; only the already-scoped audit fixture mutex is retained.

After the cached-lifecycle correction, all 8 targeted native audit cases passed
in 6.60 seconds (`6f7fd4`), warnings-as-errors passed (`0f16b6`), and **20 consecutive
complete 187-test native service runs passed** (session 1717, terminal `ed8a1e`).
Library durations: 11.13, 11.19, 11.20, 14.17, 10.65, 10.83, 11.18, 10.86, 14.61,
10.88, 10.85, 11.01, 14.36, 10.55, 10.98, 10.95, 10.82, 14.29, 10.83, 11.05 seconds.
These are repeated executions of 187 unique tests, not 3,740 distinct cases and
not a replacement for the full 682-case rerun, which subsequently passed above.

The composed SIGIL audit policy remains 39,519 bytes,
`94023763815c65923eef59d5d2811e4ff5b043fd77b45360345045408f4cf1f1`;
stdlib `b5f40e2eba41b6734f8db071`, original 65,536-byte source cap,
eight entry evaluations and all original runtime ceilings are unchanged.
The failed broad runs used frozen stage aggregate
`dab1ba2b8ce6c7d3a2070d811d0218707aba1b936b7cc368c311a4647c4ae97a`.

The successful run's **before/after aggregate matched**:
`9681889cc5b153daf877268a323f778871cd0544e53bc0e42d27077ae32bbd21`
(`af70b0` before, `f1f233` after). This hashes sorted stage file names/contents,
excluding target, Python and tool caches. Subsequent evidence-only documentation
changes have a different aggregate; the tested code snapshot is not redefined.

The full prerequisite fixture retains formatting, warnings-as-errors, all native
tests, debug/optimized builds, the original pinned fixed-evaluator lifecycle gate
and frozen-v8 checks. Post-run native enumeration confirmed **334 unique staged
native tests**: Store 125 (`dac26a`), worker 22 (`0be7db`), service 187 (`ff8ce7`).
These are prerequisite tests, separate from the 682 behavioral cases; repeated
fixture invocations do not increase the unique count. The full reproduction
selector below does not omit duplicate prerequisite execution.

Post-run artifact identities (`f74616`):

| Artifact | SHA-256 |
|---|---|
| `native/service/target/release/sigil-application-host` | `fbbba4bc9d0350c303f9e485f93ec2a774278543945fd383c9e58c46abcd5311` |
| `native/service/target/release/sigil-transaction` | `6a4c6160c917d0ae409582711830d8d768daebb8672160947dd3e9bcf0d7511a` |
| `native/service/target/release/sigil-claimed-worker` | `8dcf43a002270a5b7304e77e2c333bab5844f0d76770dc715a7f6ae3e808d179` |
| `native/store/target/debug/sigil-store` | `20bc4bd722e9ba38cdca40dc82e50398ad08e7e0c82a598d748ff62a3771f5c5` |
| `native/worker/target/debug/sigil-worker` | `2a611e8ab41b07c1150d0b5ec716ea2f1ce06de3021ce30a78b3a9b9320be312` |
| `native/service/Cargo.lock` | `6e0c513bb54c980a7967e6f8ac9d1ff7a01963ff4ee68d179c90f9cde18595eb` |
| `native/store/Cargo.lock` | `aa0a2004ec94ccab8518ad9bf45cef389515565655457810db404fe44a3f5c64` |
| `native/worker/Cargo.lock` | `96a8e8fe4a5b35e51e32e93b354485ba6210f94b5704f6bc480a2771f4899c53` |

The locks are unchanged. Composition was rechecked after completion (`be5c41`)
against SIGIL `8277a1d92d599df89e6b4391fc70fd0fa534d696` (`1d6c89`). No actual
provider access, deployment, pin change, commit or main-code integration occurred.
Main remains v8. Complete failure/denial audit coverage, quotas, retention and all
M0–M8 qualification remain open.

## Exact full-run reproduction

Run from `/private/tmp/sigil-pi-readiness-host.BiNfIvYV` with permission for local
scripted-server sockets. Those deterministic providers incur no model charges.
The original external prerequisite gates remain required.

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
  tests/test_dispatch_audit.py tests/test_effect_audit.py \
  tests/test_automatic_audit.py tests/test_automatic_audit_admission.py \
  tests/test_audited_transaction.py tests/test_audited_transaction_boundaries.py \
  tests/test_transaction_audit.py \
  /Users/nigel/Projects/sigil-pi/tests/test_native_dispatch.py \
  /Users/nigel/Projects/sigil-pi/tests/test_recorded_worker.py \
  /Users/nigel/Projects/sigil-pi/tests/test_owned_worker.py \
  /Users/nigel/Projects/sigil-pi/tests/test_native_settlement.py \
  /Users/nigel/Projects/sigil-pi/tests/test_native_preclaim.py \
  tests/test_bootstrap_admission.py tests/test_bootstrap_admission_compat.py \
  tests/test_readiness_admission.py tests/test_lifecycle_sigil.py \
  tests/test_bootstrap_admission_host.py tests/test_readiness_admission_host.py \
  tests/test_readiness_storage_host.py tests/test_legacy_readiness_native_fixture.py
```
