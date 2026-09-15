# SIGIL health/version migration — development

Status: **integrated in the working tree; original staged 85-case run and updated 12-case browser run passed; full integrated CI pending; not pilot-ready** (2026-09-10).
The full M0–M8 goal and all eleven legacy routes remain required.

The original working copy was `/private/tmp/sigil-pi-service-info.k2A6Qxmh`. During
the staged checks main was frozen for its 3,635-case source gate, session 78811, at aggregate
`516ea6f6e65c9c0861ec9fc67208f47d53f571964814df251afa237827598e2e`.
That run is now terminal; the integration checkpoint below records the subsequent
main-file changes. Runtime pins, sibling repositories and external deployments
are unchanged. Earlier pending/live statements below are historical checkpoints.

## Actual implementation

`app/pi/service_info.sigil` implements GET `/v1/health` and `/v1/version` in
the production SIGIL entry. `scripts/compose_service_info_entry.py` is a
build-only recipe over the existing v9 entry. It introduces no native import,
new runtime role, Python request dispatcher or test-only response wrapper.

- Authentication and expiry precede accounting; matched requests reach the
  existing observed read, SIGIL request policy and confirmed durable commit.
- The `ops:read` scope is checked before returning information. Permission
  refusals still consume admitted request capacity; missing/invalid credentials
  do not. A repeated request-correlation label does not deduplicate a GET.
- Health means liveness, not dependency readiness, and remains available during
  draining when normal accounting works. Version returns a bounded explicit
  source-bound label. It is not proof that a release was packaged or qualified.
- These two routes preserve the Python product's success/error envelopes,
  request IDs, authentication challenge and retry header behavior. Known native
  storage/clock failure observations become the existing generic 500/internal
  error, not an emergency admission or a fabricated healthy response. Other
  routes keep their existing behavior, including unfinished readiness.
- Source, entry, fuel, timeout, registry and capability ceilings are unchanged.
  The maximum supported build inputs produce 65,524 source bytes, below the
  original 65,536-byte limit. The early composition exceeded that limit and was
  rejected; shorter local names and equivalent control layout brought it under
  the bound without increasing the limit or dropping error cases.

The new build label is explicitly bound in `configuration/service-version` as
well as in the compiler-input digest. Use `0.0.0-unset` for a development label;
release packaging must supply its actual independently verified identity later.

## Verification history

The first 51-case attempt failed at its manifest fixture before executing any
compiled behavior (`33e3cd`): it treated `configuration/request-limit` as a file.
The fixture now checks canonical configuration bytes as well as file hashes;
the build label was also added as an explicit hashed configuration input.

The complete rerun passed **51 cases in 60.11 seconds**, session 30511,
terminal `1b128b`. It includes actual pinned-SIGIL entry/policy execution against
the independent Python product oracle, normal and malformed GET bodies,
authentication, scope, rate exhaustion, observed failure codecs, draining,
malformed receipts and preserved bootstrap/non-route transitions. These receipt
inputs are explicit test fixtures, not claims of actual durable commitment.

The post-run stage aggregate was
`eedc925214b6b7d2a42954d33e2dddedf1bac260286af15ce660f0ca27fea233`
(`5cf3d4`); no matching before-run aggregate was captured for that second attempt.
The first 51 cases had service-info source SHA-256
`48e315a174f3962122aeb528ace2de772f1ee36ba0bfe9e0f71b35d4471e6af9`
and builder SHA-256
`fa64b9f05b580ec47883a0a4183af636e1bf041f1a7e265cf3f8e56ab3947382`.
Do not treat this post-run snapshot alone as candidate-bound evidence.

Subsequent tests add maximum-label/limit compilation and correlation variants,
bringing the compiled suite to 68 cases. Six new HTTP tests use the actual v9
host with authenticated transaction/effect/evaluation audits enabled, the real
SIGIL entry and original native/evaluator prerequisites. They cover two-tenant
accounting/no provider work, denied scope, actual storage outage/recovery,
restart, draining, and an audited model/tool/follow-up/restart regression.
Collection of those six cases passed (`b16fe6`); execution is still required.

## Remaining acceptance work

Run all 68 compiled cases and all six actual HTTP cases without changing native
prerequisites, source/fuel/deadline ceilings or assertions. Preserve any failures.
Then verify broader browser/API compatibility before main integration. A pass
would establish targeted behavior, not all M1 routes or a release candidate.

Common HTTP execution journaling, complete fault-path envelope parity, global
metrics/readiness/retention and the remaining nine legacy routes are still open.
All eleven remain unqualified until their complete required evidence exists;
these two are not removed from the migration matrix on a component-only pass.
Real-model usefulness, Linux/candidate/load/restore/onboarding, actual control-plane
reuse and independent security/operational clearance are not supplied here.

## Complete targeted verification passed

Session **33989** completed with **74 passed in 984.62 seconds** (16:24),
terminal `48f0cb`, exit zero, observed at **2026-09-09 23:42:47 UTC**. This includes
all 68 compiled cases (including the largest supported build inputs and request-ID
variants) and all six actual HTTP cases described above. The original native
formatting, locked all-target Clippy, native tests/builds and real fixed-evaluator
prerequisites were retained, without a fixture override or changed deadlines.
There were no skipped or xfailed cases in this targeted run.

The stage source matched before and after execution:
`05c9b18581d83fcd74f1efdc66f9aeccaf8f290fda03a8e4bbf10631dd3865ec`
(`f09ad8` / `13e94a`). This documentation update is after that terminal result.
Main's independent source gate remained live, with its original source aggregate
unchanged at `516ea6f6e65c9c0861ec9fc67208f47d53f571964814df251afa237827598e2e`
(`e1e3eb`). The two suites ran concurrently against separate native build/state
directories; that fact is retained as part of the environment record.

Exact targeted reproduction from this directory, with scripted local HTTP test
network permission only:

```sh
env -u CARGO_TARGET_DIR -u PI_FORGE_BIN -u PI_TOOLCHAIN_DIR -u PI_STDLIB_DIR -u ANTHROPIC_API_KEY -u OPENAI_API_KEY -u SIGIL_ALLOW_UNVERIFIED_CERT PYTHONDONTWRITEBYTECODE=1 SIGIL_ROOT=/private/tmp/sigil-pi-pinned-8277a1d9.9NXtMw PI_REQUIRE_TOOLCHAIN=1 CARGO_NET_OFFLINE=true CARGO_BUILD_JOBS=2 /Users/nigel/Projects/sigil-pi/.venv/bin/python -m pytest -xq -o addopts= -p no:cacheprovider tests/test_service_info_entry.py tests/test_service_info_http.py
```

This supersedes the pending-targeted-execution statements above. Broader actual
browser/API regression, complete HTTP execution journaling and fault-envelope
coverage remain necessary before claiming a complete route migration. Main
integration must wait for its frozen full-source run, then retain the original
regression gates. All full M0–M8 gates remain unqualified.

Optimized artifacts inspected after the run (`a63000`):

| Artifact | SHA-256 |
|---|---|
| Application host | `b460255c9176bf4e575c9b1fc39ddc2423f483dc6f4d8318f4388249ec9e08a0` |
| Transaction executable | `bc9aaa609b6c206222c2342a0733e4479c7995f2adecc2c36f4e8d20b9e2a48f` |
| Claimed worker | `2a64c6df7c7ee3ba7cbcf48a90356121b28ece2d15dbf2bfd5fd24fb9fb36ba3` |
| Fixed evaluator | `675ecf23a02d89d56b758f8d45ae6b2cd71ed98ea45c08d9cca32e7833534193` |

The service, store, worker and evaluator lock hashes respectively are
`6e0c513bb54c980a7967e6f8ac9d1ff7a01963ff4ee68d179c90f9cde18595eb`,
`aa0a2004ec94ccab8518ad9bf45cef389515565655457810db404fe44a3f5c64`,
`96a8e8fe4a5b35e51e32e93b354485ba6210f94b5704f6bc480a2771f4899c53`, and
`6b9cbf8d82922ffd5e7584ed16c65f6c7f5f880db937607bbe08b1958d9d5082`.
These artifacts belong to this actual run; older stage or main artifact hashes
are not substituted for them.

## Browser regression preparation

The static test deployment now optionally admits the original browser bundle
using the same real service-info entry and both v2 audit publishers. This is a
test-fixture extension, not a production proxy or native policy change. Recovery
reuses the exact existing asset bytes/manifest. The entry, builder, original
browser drivers, native prerequisites and execution ceilings are unchanged.

Eleven new cases add the full ten-scenario v8 browser regression on this entry
and a rendered-browser information-route check. The latter compares actual
browser HTTP replies with the independent Python oracle, checks correlation and
auth headers, and independently verifies that only five expected request-counter
mutations occurred, with no domain/effect/audit records or provider calls. The ten
unchanged lifecycle scenarios retain tool/follow-up flow, tenant isolation, lost
acknowledgement, cancellation uncertainty, real service outage/reopen, old-tenant
response suppression, accounting stops and credential/scope refusals. The original
v6/v7/v8 tests remain present on their original profiles.

Collection and Python/JavaScript lint passed (`d33776`): **85 cases**, comprising
the original 68 compiled + six actual HTTP checks and eleven browser checks.
The browser-verification skills require initial rendering, screenshot, console
and control checks before continuing through the story. Their named browser CLI
is unavailable here, so the existing pinned Playwright drivers provide those
checks and artifacts. Only scripted loopback providers are used. No pilot/model
acceptance, independent onboarding or final-candidate claim follows from this run.

Run all 85 together with their original native and exact browser-runtime gates;
freeze this stage during execution and retain every failure. Main's independent
full-source gate remains running and must not be modified or qualified using this
staging evidence.

## Complete 85-case HTTP/browser run passed

Session **75417** passed **85 cases in 1,176.32 seconds** (19:36), exit zero,
terminal `ac9c57`, observed at **2026-09-10 00:16:19 UTC**. There were no skipped
or xfailed cases. This run retained all 68 compiled cases, all six actual HTTP
cases, the information-route browser check and all ten original browser lifecycle
scenarios. No original assertion, timeout, source/entry/fuel limit or native
prerequisite was weakened. The fixture plan (`f7e03f`) confirms the browser runtime
and real fixed evaluator; module-local readiness fixtures repeat the original
native gates, rather than bypassing them.

Before/after source aggregate (`bc1279` / `5bf50e`):
`2163bb3ae291247b425307aab8015b2a8e95e4aed8c59923d3b5d73266669029`.
The native host, transaction, claimed worker and evaluator hashes (`3f61e7`) match
the four artifacts in the previous table. The two production SIGIL/build files
remain `48e315a...` / `fa64b9...`, with their full hashes recorded above.

Exact command, using only local scripted-provider sockets and the installed
browser, from this stage:

```sh
env -u CARGO_TARGET_DIR -u PI_FORGE_BIN -u PI_TOOLCHAIN_DIR -u PI_STDLIB_DIR -u ANTHROPIC_API_KEY -u OPENAI_API_KEY -u SIGIL_ALLOW_UNVERIFIED_CERT PYTHONDONTWRITEBYTECODE=1 SIGIL_ROOT=/private/tmp/sigil-pi-pinned-8277a1d9.9NXtMw PI_REQUIRE_TOOLCHAIN=1 CARGO_NET_OFFLINE=true CARGO_BUILD_JOBS=2 PI_PLAYWRIGHT_DIR=/private/tmp/sigil-pi-browser-clean-install.LDlIoWgu/node_modules/playwright /Users/nigel/Projects/sigil-pi/.venv/bin/python -m pytest -xq -o addopts= -p no:cacheprovider tests/test_service_info_entry.py tests/test_service_info_http.py tests/test_service_info_browser.py tests/test_service_info_browser_regression.py
```

The story verified is browser connection/task entry -> the actual SIGIL HTTP
entry -> durable request/operation state and scoped model/tool execution ->
documented responses and retained conversation rendering. Artifacts are in
`/private/var/folders/rn/jlv2wkkx77dg6j9f57sf36jc0000gn/T/pytest-of-nigel/pytest-406`.
The information-route deployment's observed compiler-input hash is
`5f280b674515c4f400aa00d80633bf90e32c3323c381db85a555378b90a7e358` (`d77e29`),
with both audit publishers at version two for each tenant. Independent read-only
SQLite inspection found exactly `a.budget/request-window` revision three and
`b.budget/request-window` revision two, with no other rows (`da0c05`). This is
targeted information-route evidence, not a reason to omit the still-required
common HTTP/entry execution journal.

| Boundary | Evidence in this run |
|---|---|
| Initial and connected UI | Both information-route screenshots inspected; actual modules, controls and no error overlay. |
| Browser -> SIGIL API | Six actual info requests match the independent legacy oracle, including correlation, auth/scope errors and challenge headers. |
| API -> durable state | Only five expected request-counter mutations; no provider request or effect/domain change for info requests. |
| Model/tool -> response -> UI | Actual file bytes returned to the scripted provider, retained follow-up, direct-API continuity, two tenants and mobile reopen; screenshots inspected. |
| Recovery/error UI | Lost acknowledgement, real outage/reopen, cancellation uncertainty, old-tenant response suppression and quota/unknown accounting all passed. |

Visual inspection also found a **display clarity gap**: unknown usage was labelled
`Reported tokens: 0 in / 0 out`, or a partial amount without explicitly naming it
a subtotal. The API's `known: false` flag and unknown accounting state were intact,
and no automatic retry was observed. Nevertheless M4 requires clear uncertainty,
so the display is being corrected separately. The initial controller regression
reproduced this in three cases (`590ced`: ten passed, three failed); the proposed
fix passed all **100** JavaScript tests (`0956f4`, 3,696.32 ms). Those are DOM-double
unit tests, not a replacement for the actual browser regression after the fix.
Do not apply this 85-pass result to changed browser bytes.

Main's independent 3,635-case gate remains running on unchanged source
`516ea6f6e65c9c0861ec9fc67208f47d53f571964814df251afa237827598e2e` (`9431bb`).
Its original README-count guard independently failed (`eb236c`): the status line
still claims 2,702 cases. Wait for its full terminal report, preserve all failures,
then correct that documentation without changing the guard. No M0–M8 gate is
qualified by either this targeted run or the pending full-source run.

## Usage-display follow-up prepared

After the 85-case run terminated and its source was checked, the two-file UI fix
was copied from `/private/tmp/sigil-pi-usage-display.Xoy65ZTO` using exact old/new
SHA-256 preconditions and `apply_patch`; byte comparison passed (`8950aa`). The
new browser controller hash is
`650729ba9a9728b4efed96b484d0a865d1f1a06d88f53244b0e6f46cb3fa9d1b`, and its
expanded unit-test file is
`092437d354ba53e407cc84281f220cc9d5b2b72aec0970cd8bd5752454dd255c` (`317dd2`).
The API result, authority, amounts, usage-known flag, budget holds and all retry
rules are unchanged. Known totals retain their original wording. Unknown totals
are explicitly unknown; nonzero reported amounts remain visible as a subtotal,
while zero placeholders are not presented as reported zero consumption.

The existing accounting and cancellation browser drivers now have additional
unknown-total assertions; no old assertion, case or timeout was removed. One
new actual-browser case exercises two model/file pairs: the first model reports
2 input / 1 output tokens, the second omits usage. Existing SIGIL policy permits
the already-authorized file read but refuses a further billable model call. The
test requires the real API subtotal, retained full token reservation, four actual
claim/delivery pairs, signed effect history, tenant separation and no extra model
request after refresh/restart. It does not introduce a new application policy.

Lint/collection passed (`fed763`): **12 actual-browser cases** (all previous eleven
plus the new subtotal case). Run these with every original native/evaluator and
browser prerequisite after the UI change. The prior 85 run remains evidence only
for its original source/browser bytes; no current whole-source/candidate claim is
made by combining the separate counts. Main is still frozen for its own full run.

## Updated browser verification passed

Session **20742** completed with **12 passed in 854.67 seconds** (14:14), exit
zero, terminal `01b390`, observed at **2026-09-10 00:37:51 UTC**. All eleven
previous browser cases and the new partial-usage case ran with their original
native, fixed-evaluator and pinned-browser prerequisites. There were no skips
or xfails, and no original assertion or execution ceiling was relaxed.

Before/after aggregate (`27e200` / `5e53ff`):
`91f1b390798cda56ae781cde23f802699f07181f431599ed316a2c971e24779d`.
The four native/evaluator artifact hashes in the earlier table are unchanged
(`5e53ff`). This result concerns the updated browser bytes; it does not relabel
the old 85-case run as an 86-case run on a common candidate.

Exact command from this stage, with local scripted-provider socket permission:

```sh
env -u CARGO_TARGET_DIR -u PI_FORGE_BIN -u PI_TOOLCHAIN_DIR -u PI_STDLIB_DIR -u ANTHROPIC_API_KEY -u OPENAI_API_KEY -u SIGIL_ALLOW_UNVERIFIED_CERT PYTHONDONTWRITEBYTECODE=1 SIGIL_ROOT=/private/tmp/sigil-pi-pinned-8277a1d9.9NXtMw PI_REQUIRE_TOOLCHAIN=1 CARGO_NET_OFFLINE=true CARGO_BUILD_JOBS=2 PI_PLAYWRIGHT_DIR=/private/tmp/sigil-pi-browser-clean-install.LDlIoWgu/node_modules/playwright /Users/nigel/Projects/sigil-pi/.venv/bin/python -m pytest -xq -o addopts= -p no:cacheprovider tests/test_service_info_browser.py tests/test_service_info_browser_regression.py
```

Actual browser artifacts are under
`/private/var/folders/rn/jlv2wkkx77dg6j9f57sf36jc0000gn/T/pytest-of-nigel/pytest-407`.
The new subtotal case's `02-accounting-stop.png` and `03-reopened.png` were
visually inspected: both explicitly state that totals are unknown and show
`2 in / 1 out` as a reported subtotal, not a final total. Reopening retains both
model/file pairs. The test checks console errors, foreign requests, single
submission, no cancellation, tenant isolation, the held reservation, signed
effect observations and unchanged results across actual service restart.

Independent read-only inspection (`1566f9`) confirmed four claim records, four
delivery records and eight effect events for tenant A, no tenant B effect
records, and the retained budget hold. The actual served controller SHA-256 is
`650729ba9a9728b4efed96b484d0a865d1f1a06d88f53244b0e6f46cb3fa9d1b`.
The browser-verification guidance prompted the visual inspection in addition
to assertions; its unavailable CLI was replaced by the existing pinned
Playwright drivers, not mocked service replies.

The display follow-up is verified at this targeted boundary. Main integration
still waits for session **78811**, live beyond 57% at this checkpoint, with its
source aggregate unchanged (`feeb05`). Preserve its full failure/coverage report
before editing or integrating. Common journals, remaining route migration,
candidate qualification and every full M0–M8 gate remain open.

## Working-tree integration — 2026-09-10

Main session **78811** terminated with exit 1 (`4ae034`); its sole reported test
failure was the README's stale 2,702 collection claim versus 3,635 collected cases.
The original full CI stopped before its coverage command and did not pass.
The full source remained at `516ea6f6e65c9c0861ec9fc67208f47d53f571964814df251afa237827598e2e`
through terminal and the pre-integration check (`a3d21a`).

After terminal, all 13 intended service-info, browser, test and documentation
paths were integrated using their exact old/new SHA-256 preconditions (`838448`).
All 13 destination files then matched the staged bytes (`bb0545`), before this
documentation update. No native source, runtime pin, production Python dispatcher,
original fixture prerequisite, grant, source ceiling or test deadline changed.

Whole-tree collection is now **3,721 cases** and F/E9 lint passed; the unchanged
JavaScript suite passed **100 tests**, with no failures or skips (`873b24`). The
README collection claim is updated to 3,720 tests plus its existing research-only
xfail; the guard itself is unchanged. The actual source suite must still pass on
these integrated bytes. The old staged 85 and updated 12 results remain separate
source-bound evidence, not an invented combined 86-case or full-CI pass.

The complete integrated gate, common execution journaling, full legacy-route
parity and all other MVP acceptance requirements remain open. The deployment
materialization checks are running separately and are not yet integrated here.
