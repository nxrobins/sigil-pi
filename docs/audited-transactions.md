# Audited fixed SIGIL transactions — staged v2 contract

Status: trusted CLI and opt-in automatic/HTTP state-transition integration verified,
**not** a complete product audit migration, readiness verdict, or pilot gate.
The main checkout remains v8. No deployment recipe opts into this contract.

## What this connection establishes

The actual fixed SIGIL settlement producer reads native-bound scoped snapshots,
emits its transaction, and has that transaction's membership and revisions checked
against every actual read. A second fixed grantless SIGIL program chooses a bounded
audit representation of host-observed facts. The authenticated entry, its chain head
and the domain mutations then use **one original Store commit**. The native host
does not choose a conversation, settlement outcome, redaction policy, or retry.

`native/service/src/audited_transaction.rs` provides `AuditedTransaction` for trusted
native embedding. The shared transaction implementation factors preparation from
publication; its original `Transaction::apply` still publishes the original batch
and returns the original response. Automatic participants can now opt into the
audited mechanism for all their fixed transactions through trusted configuration,
never through an HTTP request. Omission preserves the original automatic path.

## Explicit admission and transport

`sigil-transaction` configuration version 1 must omit `audit` and continues to
announce `sigil-transaction/v1`. Version 2 **requires** an explicit non-null `audit`
object and announces `sigil-transaction/v2`. Both open an existing Store only.

The audit object has exactly these fields:

- `version`: 1, naming this audit binding contract.
- `worker`: an ordinary pinned source/runtime worker configuration for the fixed
  SIGIL projection. Network, filesystem and secret grants must all be empty,
  for both the projection and the transaction producer.
- `key_env`: a valid named native environment variable containing 32–1,024 raw
  bytes. No implicit default, random replacement, fallback key or key persistence.
- `chain`: one fixed 64-character lowercase hexadecimal chain identifier.
- `heads` and `records`: two valid, distinct namespaces outside the producer's
  complete domain-grant map.
- `limits`: explicit authenticated-log payload, count and encoded-byte ceilings,
  subject to every original Store and authenticated-log ceiling.

The operator must supply a suitably unpredictable key; a length check does not
measure entropy. The native loader and retained log object erase their owned key
buffers on drop. This does not erase the parent/process environment or all crypto
temporaries. Worker processes clear their inherited environment and receive no
secret grant. Neither the key nor its environment-variable name enters guest facts,
events, receipts, checkpoints or public errors.

Admission reads and verifies source bytes into immutable native memory. Later
source-file edits do not change that admitted program or its recorded digest;
they are not a live code-update or revocation channel. Reopening requires source
admission again. Runtime executables are separately rechecked before invocation.

The native publication scope contains the **unchanged** domain permissions plus
the explicitly configured two log namespaces. The producer's read scope is not
widened. The projection has no Store scope at all. Both namespace slots count
against the original 64-namespace ceiling. The authenticated append mechanism
restricts entries to create-only publication and head updates to authenticated CAS;
the guest has no arbitrary-signing or audit-rewrite command.

The only state-transition request is `{"op":"apply","values":[...]}` with the
existing exact count and 512-byte per-value limits. Inputs, snapshots, clocks,
hashes, batches, receipts, chains, keys and audit payloads cannot be supplied as
extra request fields. `{"op":"verify_audit"}` verifies only the configured chain,
returns its checkpoint, and explicitly reports
`"independent_checkpoint_checked": false`. It is refused on v1. Trusted library
embeddings may pass an independently retained checkpoint to `verify`.

No authorization policy or deployment-wide tenant namespace inventory is inferred
from the trusted CLI. The automatic embedding performs its own admission below;
it does not expose a public audit-query, verification or signing API.

## Automatic-service binding and restart

An automatic participant may explicitly include `transaction_audit`, containing
the audit object above. Explicit null and unsupported inner versions are refused.
The complete configuration remains bundle-bound; omitting the new field preserves
the old serialized configuration. No HTTP/CL1 wire command, registered entry
function or per-transaction audit opt-out is added. Interpret, settle and preclaim
use the same admitted chain/key/namespaces/limits for that participant.

Before creating or opening state, native admission checks audit namespaces against
every credential's domain grants, every participant's claim/delivery namespaces,
and all other audit namespaces. Audit privileges are not delegated to coordinators,
transaction producers, effect workers, dispatch policies or credentials. The actual
producer bytes, projection and native key are retained across bootstrap instead of
being discarded and reread after storage initialization. Store/log limit preflight
retains the original ceilings.

The fixed SIGIL projection also evaluates `AB1` at startup. Its four fields are the
configured chain, producer TM1, projection TM1 and canonical original grants JSON.
Only the exact `transaction_audit_admitted` result is accepted. This checks real
configuration metadata; it is not a fabricated TA1 execution or an audit publication.
It produces no log, receipt or healthy-history assertion. The standalone v2 CLI
retains its TA1 contract and does not acquire this automatic-specific boot exchange.

The projection uses the existing cached fixed evaluator, with immutable admitted
source and a fresh guest instance on each invocation. It does not cache policy
decisions or reuse guest state. Runtime inspection includes its actual executable;
source-path edits do not replace admitted bytes. All original evaluation, source,
fuel, memory and deadline bounds remain.

Before any automatic tick or effect dispatch, startup inspects each participant's
chain once using the bounded, complete native HMAC/history check. A valid-length
wrong key, tombstone, orphan or damaged non-tail entry refuses startup. Clean absence
means only that no head and no records in that exact chain prefix were observed;
it remains uninitialized, not a verified empty history. No chain is repaired,
replaced, rotated or initialized by this inspection. Full-history erasure or rollback
to a pre-initialization snapshot still requires an independent inventory/checkpoint
to distinguish from first use. This is not final application readiness.

The automatic coordinator receives its original context and actual Store receipt.
Transient authenticated checkpoints are not independently retained by this adapter;
no freshness or complete product-audit inventory is claimed.

## Native observation and SIGIL event policy

The host constructs `TA1` from the real producer invocation. It contains an
entropy-generated invocation identity; configured chain; admitted producer and
projection `TM1` manifests; the complete original domain-grant map; wall-clock
samples before reading snapshots and after producer evaluation; preparation elapsed milliseconds; selected
fuel and timeout; and SHA-256 digests/UTF-8 byte lengths of the actual input,
returned output and exact SIGIL batch text, plus validated check/write counts.

`TM1` binds source/runtime digests and the function's configured fuel and capped
timeout ceilings. The selected timeout in `TA1` is the timeout actually passed
to the producer invocation after reads, not its original configured ceiling.
Preparation elapsed time includes snapshot reading and transaction production;
it is not a measure of model/provider latency or an exact CPU/fuel consumption.
These structures cannot be deserialized as caller-provided execution evidence.

`app/pi/transaction_audit.sigil` validates all fields, canonical grant order,
lengths and consistency, and emits `AR1`. Its event schema is
`sigil-pi/state-transition-audit/v1`, with event `pure_transition_publication`.
It records the identities, complete storage permissions, empty external grants,
timing observations, selected budgets and content hashes/sizes. It does **not**
receive or copy raw conversation text, snapshots, input/output text or key bytes.
Hashes can still reveal equality or support guessing low-entropy content; they
are not anonymization. Audit storage still needs access and retention policy.

An empty transaction is explicitly `none` with an empty payload: no state change,
commit receipt or audit growth. A nonempty transaction requires `publish` with a
nonempty payload. A projection cannot silently skip an actual domain mutation.
The host interprets this publication coupling, not the JSON event's product meaning.
The projection is itself pure and is not recursively audited by this contract.

## Atomicity, failure and bounds

One exclusive mutable Store borrow spans the actual reads, both evaluations and
publication. The original producer deadline is shared, **not added** to a fresh
second deadline. Fresh time checks remain before publication. The existing Store
I/O/commit limitations are unchanged: this is not an interruptible kernel commit.
Both execution instances are grantless and supervised under the unchanged fuel,
memory, source-size, runtime-admission and timeout mechanisms.

An audited update consumes two additional original batch slots: one entry and one
head. Thus the producer may have at most 62 combined checks/writes in this contract;
no 64-slot batch is silently split, dropped or widened. All 62 full-length namespace
permissions fit the existing 16 KiB fact/event bound without truncation. The shared
SIGIL application source-size ceiling remains 65,536 bytes. No HTTP entry function
or registered-function inventory is added by this stdio connection.

Audit refusal, failed projection, changed runtime executable, invalid artifact at
admission, bad grants, missing key,
oversized event, exhausted log capacity or invalid original transaction means no
unaudited fallback commit. Acknowledgement returns the actual **whole-store**
receipt and a separate authenticated-head checkpoint. `commit_uncertain` remains
explicit; it is not a receipt or permission to retry. Successful acknowledgement
means the audit and state update were published together. Losing acknowledgement
does not imply that either was absent; recovery must read committed state.

Full verification and append-tail authentication have the different scopes
documented in [authenticated-log storage](authenticated-log.md). Neither a no-op
nor a successful append proves complete historical integrity. Missing/corrupt
chains fail explicit verification; no automatic repair, reset, truncation or
replacement chain is provided. An independent latest checkpoint is necessary
to reject coherent rollback; checking one chain cannot establish a complete
application audit inventory.

## Remaining MVP work

This connection audits successful state-transition publications, **not** model/tool
dispatch, uncertain effects, failed evaluations, denied requests, cancellations,
or every execution. It does not replace the old signed audit format. Product-wide
event inventory/correlation, audit reservations/settlement and quotas, retention,
archival/migration, independent-checkpoint policy and final readiness remain open.
The automatic connection covers only its fixed state-transition publications.
Owner-approved profiles, full browser/API audit semantics and every remaining
event/effect surface still need qualification. M0–M8 remain unqualified.

## Verified evidence — 2026-09-09

The complete run passed **378 behavioral tests**: 75 new audit-policy/native
integration checks, all 28 unchanged native settlement tests, all 8 unchanged
native preclaim tests, and the previous 267 bootstrap/readiness/lifecycle/storage/
frozen-v8 regressions. Session **80939**, terminal **`4f9a8a`**, exit 0, completion
observed **2026-09-09 14:47:03 UTC**. The repository's quiet settings suppressed
the usual duration/count summary; a post-run collection independently confirmed
342 staged tests plus 28 settlement and 8 preclaim tests. No skipped case appeared.

Every original prerequisite remained: formatting, warnings-as-errors checks,
**120 Store + 22 worker + 179 service = 321 native tests**, complete fixed-evaluator
verification (including its 4,096-call lifecycle), debug builds and optimized host
builds. Two added service tests enforce exact request fields. Native test enumeration
confirmed the counts after completion. All changed Python passed F/E9 lint.

The real compiled application test proves settlement and its audit share one
receipt, verifies tags with independent Python HMAC framing, reconstructs the
actual producer input/output/batch hashes, verifies after restart, and rejects
duplicate settlement/audit growth. Further cases cover a real after-commit kill
before consuming acknowledgement; original read-only permissions; an installed
seven-record Store refusing six existing plus two audit records atomically;
orphan/tombstoned logs; missing/wrong keys; altered runtime bytes; schema and scope
bounds; malformed/skip/empty audit proposals; and caller-forged request fields.
The existing Store suite separately retains actual before/after-commit crash tests.
No pre-commit process kill is claimed for the new CLI integration test itself.

Two preliminary failures are retained in the development record. The first was a
warnings-as-errors failure for an oversized CLI enum variant, corrected with a
boxed variant without disabling the check. The second was an incorrect new-test
assumption that source-file mutation revokes already-admitted immutable bytes.
Code inspection confirmed that execution uses the admitted copy. Separate tests
now prove its original digest/behavior and the refusal of a changed runtime
executable. No existing fixture or execution ceiling was loosened. The full pass
above followed both corrections.

Tested stage aggregate was identical before and after the run:
`379449800aaff7b288dfb99e39d332487bbf96cafc65080b97f7957b560a117a`.
Documentation updates follow this snapshot. Relevant identities:

| Artifact | Bytes / identity |
|---|---|
| Composed SIGIL policy | 28,562 bytes; `37f2cdce146b3e09618384b3cd0277eeb21c4fcdb2b47f1e6c99ff59f97b7fc1` |
| SIGIL stdlib | `b5f40e2eba41b6734f8db071`; five authored composition inputs retained |
| Optimized transaction CLI | `ed5a32dc6867dcb8e41fc196801a06cebcc2a69fa4fd3982e9fb93da788ffc95` |
| Optimized application host | `3b328892694cc1d8eb58c8c3a6c5e196cfcc796da30910a57bd79783b8889081` |
| Native audited transaction | `9f522ac6509a5075fdfd0692599345f4d90bea369476d7d383da7baaf82399eb` |
| Shared transaction producer | `d75a3574127f6c8b0e34eb9a0a716b25024eb5c158df8ddfedf87d9510b6f6cb` |
| Service Cargo lock | `6e0c513bb54c980a7967e6f8ac9d1ff7a01963ff4ee68d179c90f9cde18595eb` |

The service directly uses the already pinned `zeroize=1.9.0` dependency. No
dependency package version, Store/worker lock, runtime pin, fixed-function
inventory, original 65,536-byte source ceiling, eight-entry-evaluation limit,
fuel/memory/deadline limit or frozen-v8 fixture was changed.

Run from the stage using the same pinned environment described in its README.
Explicitly load the original `conftest` and `staged_transaction_regression`
plugins, disable only duplicate automatic conftest loading with `--noconftest`,
and disable pytest's cache provider. The regression plugin selects staged
executables through the full existing prerequisite fixture; it does not replace
original test functions, assertions, inputs, timeouts or native gates. Select:

```text
tests/test_audited_transaction.py
tests/test_audited_transaction_boundaries.py
tests/test_transaction_audit.py
/Users/nigel/Projects/sigil-pi/tests/test_native_settlement.py
/Users/nigel/Projects/sigil-pi/tests/test_native_preclaim.py
tests/test_bootstrap_admission.py
tests/test_bootstrap_admission_compat.py
tests/test_readiness_admission.py
tests/test_lifecycle_sigil.py
tests/test_bootstrap_admission_host.py
tests/test_readiness_admission_host.py
tests/test_readiness_storage_host.py
tests/test_legacy_readiness_native_fixture.py
```

This is local macOS component evidence, not Linux/pilot qualification. Main code
remains unchanged; no candidate, spending, deployment or cross-project modification
is authorized by this result. All M0–M8 acceptance gates remain open.

## Verified follow-up — automatic/HTTP integration, 2026-09-09

Full rerun: **433 passed in 1460.71 seconds**, session **95861**, terminal
**`b09665`**, exit 0, completion observed **2026-09-09 16:03:41 UTC**. This includes
all prior 378 cases, six AB1 SIGIL checks and 49 automatic/HTTP admission scenarios.
All original prerequisites passed: formatting, warnings-as-errors, **123 Store +
22 worker + 179 service = 324 native tests**, complete pinned fixed-evaluator
verification and debug/optimized builds. Native test enumeration confirmed counts;
changed Python passed F/E9 lint. No case was skipped.

Actual HTTP evidence uses the real draft v9 SIGIL entry for model -> approved file
tool -> response, follow-up, replay, two tenants with identical session names, and
restart after a real provider send. HMAC framing is independently checked over
persisted entries. Boot produces no fabricated audit event; repeated acceptance
does not grow audit history; uncertain recovery records state transitions without
asserting effect success. The separate runtime-inspection test alone uses the
explicit diagnostic 218/AC1 probe, not a product-ready response.

Startup tests cover all-registry namespace collisions, attempted delegation to
other execution roles, missing/invalid keys, bounds, malformed/altered artifacts,
guest network/filesystem/secret grants, and refused SIGIL boot protocol/results.
Existing-history tests reject a correctly sized wrong key, a tombstoned head,
an altered non-tail entry with a valid Store checksum, and an orphan entry before
any automatic work. Refused reopen preserves all durable rows and causes no new
provider request. Restoring the correct original key reopens unchanged history.
Three added native tests cover optional absence, independent-checkpoint rejection
of absence, scoped prefixes, tombstones/orphans/history corruption, keys, scope,
deadlines and read-only behavior. Strict `verify` still rejects missing chains.

The initial full run is retained: session **29181**, terminal **`cd3a2f`**, **132
passed, 1 failed in 934.53 seconds**. A new negative test set `database_pages=0`
through a shared API-fixture limits dictionary; a later unchanged legacy API test
then correctly refused to reopen a Store whose recorded limit was 262144. The new
fixture now deep-copies its configuration before mutation. No production code,
limit or legacy assertion was changed to resolve that failure. The complete
successful rerun above followed the correction and includes the failed legacy case.

Tested source aggregate was identical before and after the successful run:
`e9ba135dcb7aa495f684dee6840198bba07135a1268ac7a5b769451a55249fb6`.
Documentation updates follow that snapshot. Main remained unchanged at aggregate
`3e3403fbb4a7dbb9f58411c531581687fd31dab99ad83727f3c0968c2b32e47d`
before this evidence update.

| Artifact | Bytes / SHA-256 |
|---|---|
| Composed SIGIL audit policy | 29,057; `06a9a2d547167b62bd76c93cd9b9587db22796b1fc3fa2f2d53fc5cf45e8ecfb` |
| Optimized automatic/HTTP host | `67ac7d442dc43d676444e2717a5575b800a8348ea1d88bdee538a6d8ab444296` |
| Optimized transaction CLI | `9bce210ebd9348eb408e136ab23e5a318cda4fac9e8538c90eb7631ecd554aa9` |
| Native automatic adapter | `9775c64c8868b801475f4d0836b77bbb158989e9d1f92842e2a05fb4e14aad84` |
| Native audited transaction | `3b7247300b2a4a29e023433ac9122793a41b627a9cfd851e87211626227c5665` |
| Native transaction producer | `9a3d09309f4fb802ac7484cd1a8ce8a4610c3518fd89dc520780455c99892160` |
| Native authenticated log | `932fbd93ff50f431f48aeee1293b60ca869f231b96fba732c7fb9184f2dfcad2` |

The stdlib identity remains `b5f40e2eba41b6734f8db071`; all three Cargo locks,
SIGIL runtime pin, five-entry-function inventory, eight entry evaluations, source/
fuel/memory/deadline bounds and frozen-v8 fixture are unchanged. The same full
command/environment above was used, adding `-o addopts=` so pytest reports totals
and duration, and prepending these two files to its selector list:

```text
tests/test_automatic_audit.py
tests/test_automatic_audit_admission.py
```

This is not model/tool-effect or failed/denied-execution audit coverage. Complete
event/tenant/operation inventory and correlation, reservations/quotas, retention,
archival/migration, independent checkpoints and real final readiness remain.
No pilot profile is approved, no browser/live-model/Linux qualification is inferred,
and no M0–M8 gate is PASS. Main code remains v8; this opt-in integration stays staged.

Subsequent [worker-lifecycle audit work](effect-audit.md) adds a separate private
chain for actual claims and delivery/recovery publications. Its full regression
retains these transaction cases and passes 600 behavioral cases plus the 332
staged native tests and original evaluator/frozen-fixture prerequisites. That
document records its exact scope and preliminary failures; it does not turn this
state-transition component into complete product audit coverage or MVP readiness.
