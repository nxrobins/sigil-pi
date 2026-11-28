# Native deployment materialization — development only

Status: **integrated in the working tree after 57 build/native/browser checks passed
in isolation; full integrated source gate pending; not a qualified release or pilot** (2026-09-10).

The existing `scripts/build_release.py` still packages the reference Python
service. It is not a release path for the migrated SIGIL application. This new
`scripts/stage_native_deployment.py` addresses a separate prerequisite: copying
an explicitly configured native v9 application and its browser assets into a
private, self-contained artifact directory. It does not replace the old release
builder or qualify any M0–M8 gate.

## What it does

The builder requires an owner-supplied native configuration, host executable and
expected digest, browser manifest, new artifact destination and new state path.
It enumerates the actual entry, helper, coordinator, effect, dispatch policy,
recorder, transaction and both audit-publisher/evaluation worker slots. Every
selected source/runtime and browser file is copied as the exact supplied digest,
with duplicate artifact bytes stored once. Native source/runtime/static limits
and unsafe-file-mode rejection are retained; final-component symlinks and FIFOs
are refused. Strict JSON parsing rejects duplicate members and non-finite values.

Only worker source/runtime paths, public asset paths and the explicitly supplied
state path change. Credential facts, grants, secret environment references,
model/tool bindings, policy inputs, deadlines and allowances are retained. The
builder reads no secret environment values, starts no service, opens no state
store and makes no network request. Python is build/install tooling only; the
printed launch commands directly execute the native host.

The resulting directory is mode 0700; configuration/source/browser files are
0600 and executables 0700. `DEPLOYMENT.json`, written last, records all other file
digests, sizes/modes, worker-role identities and exact `init`/`open` command arrays.
It is an inventory, **not** verification provenance, a signature, a successful
native admission receipt or proof that the application is ready. The supplied
configuration contains private deployment/identity information and must not be
published as a public release asset.

## Explicit operator workflow

Run the module with `--config`, `--host`, `--host-sha256`, `--assets`, `--output`
and `--state-root`, all explicit absolute paths except the digest. The output
and state directories must be disjoint and nonexistent. Their parents must
already exist. The builder never overwrites or migrates an existing deployment
or state directory; a failed build may leave a private incomplete output without
a completion manifest. Do not launch that output.

Verify the artifact inventory against independently trusted build/provenance
evidence before executing either recorded command. Set only the intended
operator-managed secret references required by the supplied configuration.
`init` creates new state through the existing SIGIL/native bootstrap checks;
`open` reuses the **same unchanged deployment/configuration** and existing state.
The example command arrays use the existing loopback listener's ephemeral port
argument `0`; they are not public exposure or a pilot topology approval.

The configuration is deliberately path-bound. Relocation changes the native
bundle identity; this builder is not a portable archive installer, in-place
upgrade or state migration. It neither rewrites recorded operations nor claims
that a differently located deployment can resume old state. Workspace files
remain outside the artifact directory under the original explicit grants; they
are not copied into public browser assets.

## Verification and remaining work

The initial build-time run failed after 12 passing cases (`de5d2a`) because this
filesystem cleared the requested set-ID permission bit. Read-only inspection
showed actual mode 0700 (`8f5d83`), not the requested unsafe mode. The unchanged
mode predicate was factored into a directly tested helper: real-file unsafe
mode checks remain, and explicit bit-level cases cover set-ID rejection without
claiming that the filesystem created such a file. All **44 build-time cases
passed in 0.22 seconds** (`6312d5`); lint passed. Inert executable fixtures in
those tests are never run and do not establish native admission.

Five additional actual-runtime cases are prepared, bringing collection to 49
(`c1347b`). They keep the original native/evaluator/browser prerequisites and
browser assertions. The two normal/lost-acknowledgement stories run copied host,
sources, runtimes and browser files from an empty working directory, with the
generated input directory moved away and checkout-selection environment hints
removed. They exercise browser and direct API use, two tenants, actual file/model
effects, signed effect records and restart without extra provider work. Three
negative cases require modified entry, runtime or browser bytes to fail native
admission before state creation. Execution and visual inspection are pending.

This is a local scripted-provider check on the current host, not a fresh Linux
installation or real-provider acceptance. System/dynamic-library dependencies
are not bundled or independently qualified by copying an executable. A release
still needs the actual SIGIL product configuration workflow, complete route and
audit/retention behavior, Linux packaging/dependency inventory, trusted provenance
and SBOM, protected full CI, versioned upgrade/restore/rollback evidence and all
other gates in `docs/mvp-goal.md`. Model, quota, retention and operational choices
remain owner decisions. No pilot is deployed by this tooling.

## Destination-validation follow-up

The first combined 49-case run is still live in the separate original copy
`/private/tmp/sigil-pi-native-deployment.gWsvZBcL`, session **68317**, with frozen
aggregate `16f171f0ce59b5e628bb8394960f7fc46df8b9e6ef424f2c346cf7ee21aeb157`.
It includes 44 original build checks plus five actual browser/native checks.
Do not treat that source as the corrected builder below, even if it passes.

An independent two-case regression outside the frozen copy reproduced missing
destination checks (`21c780`): symlinked parent aliases could name the same actual
artifact/state directory, and a nonexistent state parent was accepted even though
native initialization requires it. No existing user deployment was changed; both
reproductions used inert files in new private test directories.

The corrected copy at `/private/tmp/sigil-pi-deployment-path-fix.cmWOTblu` compares
resolved existing parent directories, rejects non-directory/missing parents and
rechecks state-path absence immediately after exclusive artifact-directory
creation. The latter catches filesystem case/name equivalence not visible from
lexical path comparison. Distinct symlinked parents remain usable and explicit
operator path strings are retained. These checks are build-time validation, not
an upgrade/migration or a guarantee against a compromised same-user filesystem.

All **49 expanded build-time cases passed in 0.17 seconds** (`7de6c0`), including
the five added destination cases; lint passed. This is not the combined 49-case
browser run. After the original run terminates, preserve its exact result/source,
integrate the corrected files under hash preconditions, then verify the expanded
combined set described below. No passing
runtime result is transferred to changed builder bytes. Main's independent full
source gate remains frozen and this work is not yet integrated there.

The documented command-line entry point was then exercised directly, including
an output path containing spaces and rejection when required settings are
missing. The positive checks copy inert executable bytes without launching them;
they produce the same manifest as the build function and create no state or
Python runtime files. All **52 build-time cases passed in 0.31 seconds**
(`7bd97d`), with lint passing. The next combined set contains those 52 cases and
the five unchanged actual-runtime/browser cases: **57 total**, not a retroactive
pass for either the original 49-case execution or the corrected deployment.

## Original combined run stopped; corrected run preparation

Session **68317** terminated with **1 failed, 44 passed in 646.47 seconds**
(10:46), terminal `f622da`. Original native, fixed-evaluator and browser-runtime
prerequisites completed. The first real scenario then failed in its build-time
comparison before launching the copied host: it zipped original and sorted JSON
dictionary iteration order, comparing `function/history` to
`function/emergency_policy`. No copied-service startup, browser story, model
request or restart passed in this run; the other four actual cases did not run.
The source aggregate matched before/after (`530a13` / `f56caa`):
`16f171f0ce59b5e628bb8394960f7fc46df8b9e6ef424f2c346cf7ee21aeb157`.

After that terminal result, the destination fixes, expanded build tests and
their documentation were copied here using the recorded old/new hashes
(`4fd864` / `4c72f0`). The actual scenario now compares workers by their explicit
role names, requires no duplicate or missing role, and retains every byte and
non-path policy/grant/limit comparison. This corrects the test's map-order
assumption; it does not alter the native configuration or service behavior.
All 57 cases must run on the corrected snapshot with the original prerequisites.
This latest checkpoint supersedes the earlier statement that 68317 is live.

## Corrected copied-deployment verification passed

The corrected **57-case** run, session **98198**, passed **57 tests in 469.82
seconds** (7:49), exit zero, terminal `4e448a`. It launched at **2026-09-10
01:20:40 UTC** (`f0470a`). This includes all 52 build-time cases and all five
actual-runtime scenarios, with no skipped or xfailed cases and no native,
fixed-evaluator, browser, assertion or deadline bypass. The source and
documentation aggregate matched before/after (`623458` / `323000`):
`a6d16625e630b219a2964df85c533dacb29f69aff1280350654125b27b004bb6`.

Reproduce from this directory using only local scripted-provider sockets:

```sh
env -u CARGO_TARGET_DIR -u PI_FORGE_BIN -u PI_TOOLCHAIN_DIR -u PI_STDLIB_DIR -u ANTHROPIC_API_KEY -u OPENAI_API_KEY -u SIGIL_ALLOW_UNVERIFIED_CERT PYTHONDONTWRITEBYTECODE=1 SIGIL_ROOT=/private/tmp/sigil-pi-pinned-8277a1d9.9NXtMw PI_REQUIRE_TOOLCHAIN=1 CARGO_NET_OFFLINE=true CARGO_BUILD_JOBS=2 PI_PLAYWRIGHT_DIR=/private/tmp/sigil-pi-browser-clean-install.LDlIoWgu/node_modules/playwright /Users/nigel/Projects/sigil-pi/.venv/bin/python -m pytest -xq -o addopts= -p no:cacheprovider tests/test_stage_native_deployment.py tests/test_native_deployment_browser.py
```

Actual optimized artifact SHA-256 values (`323000`):

| Artifact | SHA-256 |
|---|---|
| Application host | `240215fd75234b30a956b18ef4b6e2cf276a0248a562f168f2bb6d746d9076fd` |
| Transaction executable | `a65f88478b794702bf5d404d0617696c4c662ee013637088fe9b607d536818e9` |
| Claimed worker | `f9ebbe84993804fb06054f43730ec1e63cfcb8b85ffd84dd5a28b4856f491986` |
| Fixed evaluator | `afdfa1269ff1f871967e05b60a39fa539864e102b1bc311161f5336af12ef7cb` |

The story is copied native/SIGIL/browser artifacts -> authenticated chat and
direct API -> scoped model/file effects and durable state -> rendered retained
conversation -> actual restart without additional provider work. No Python API
or Python agent is launched. The local model replies remain scripted, not M4
live-model usefulness evidence.

The actual artifacts are under
`/private/var/folders/rn/jlv2wkkx77dg6j9f57sf36jc0000gn/T/pytest-of-nigel/pytest-414`.
Read-only post-run verification checked every recorded artifact digest, byte
length and mode: each normal/lost-ack bundle has **26 files and 34 unique worker
roles**, with both v2 audit publishers for both tenants (`794caf`). The admitted
entry digest is `dda44ebb392d02f22060bf507fb363aab879382a2789948e9c6f4b54c616f5fa`.
The original generated input path remains absent and each service's launch
working directory remains empty. The two private manifests have SHA-256:

- Basic: `e0079c595618a5e036da033efcae78c61ccb13193769e365835bedc4f53dc320`.
- Lost acknowledgement: `954a1d6cfacd87f7a222bd69ce5e7217ca0e46045b25644067dc75d0dd3ea59c`.

Independent read-only SQLite inspection found basic tenant A/B claim and delivery
counts **5/5 and 1/1**, with **10 and 2** effect events. The lost-acknowledgement
case has **3 claims, 3 deliveries and 6 effect events** for tenant A and none for
tenant B (`794caf`). The tests independently verify event signatures/linkage,
unchanged state/events at restart and no new provider request from reopening.
The three copied source/runtime/asset tamper cases fail actual native admission
before creating state or performing provider work.

The browser-verification skills prompted visual inspection as well as assertions.
Their CLI is unavailable, so the existing pinned Playwright driver supplied real
screenshots, console/foreign-request checks and keyboard/control assertions.
Inspected initial connection, completed model/tool response, mobile retained
history and lost-acknowledgement screenshots. The mobile capture shows retained
history while its operation-status check is still pending; it is not a screenshot
of a settled status. Separate test assertions establish completed turns and
restart behavior. The loss screenshot explicitly warns that work may have been
accepted and offers only an explicit same-submission retry.

This closes the targeted copied-deployment test boundary, not M7. Main integration,
the complete unchanged source gate, actual product configuration, fresh Linux
x86_64 installation, dependency/provenance/SBOM evidence, versioned upgrades and
the full remaining M0–M8 criteria are still required. No public deployment,
provider spend, runtime-pin change or sibling-repository change occurred.

## Working-tree integration

All four deployment builder/test/documentation files were integrated after the
run terminated, under exact source hashes and absent-destination preconditions.
The post-result documentation snapshot had aggregate
`6a3761c648fd2cc7d8f0cdadb7bc83644b90ee6826d1164f00bbb9d0e3eccf27`
(`52aee8`), distinct from the tested snapshot above. All four copied files matched
before this integration-status documentation update (`794e30`). No application,
native, original browser-driver, compiler-pin or regression-gate source changed.
Main now collects 3,778 tests; whole-tree F/E9 lint passes. The complete original
`ci.sh` must pass against the integrated bytes before claiming a source-gate pass.
