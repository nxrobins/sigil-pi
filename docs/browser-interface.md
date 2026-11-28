# Development browser interface

Status: **INTEGRATED DEVELOPMENT FEATURE — NOT A PACKAGED OR PILOT-READY PRODUCT**.
The [MVP goal](mvp-goal.md) remains active; [acceptance](mvp-acceptance.md) records
the exact evidence and missing work. No M0–M8 gate is PASS.

The browser and direct API share the same SIGIL application. The explicit v6–v8
development profiles provide conversation discovery, retained history, operation submission,
status and cancellation. SIGIL owns authorization, model/tool sequencing, state
transitions and recovery. Plain browser JavaScript presents those results; native
code serves a fixed public-file inventory and the existing bounded transport.

## Local setup contract

This is a developer path requiring an admitted service configuration and the pinned
native/SIGIL toolchain, not a one-command end-user installation. The automatic
configuration and v6 discovery extension are documented in
[automatic service](automatic-service.md) and [discovery](session-discovery.md).
Do not substitute a test credential registry or local-provider fixture for an
approved real-provider configuration.

Build-time manifest generation takes one positional web-directory argument:

```text
python3 scripts/public_asset_manifest.py WEB_DIRECTORY
```

It prints JSON to standard output. Save that output to a new operator-reviewed
manifest file and supply its path with the existing service configuration:

```text
sigil-application-host init SERVICE_JSON PORT --public-assets MANIFEST_JSON
sigil-application-host open SERVICE_JSON PORT --public-assets MANIFEST_JSON
```

Use `init` for new state and `open` for existing state, not as interchangeable
retry modes. The original invocation without public assets is unchanged. The host
prints its actual `127.0.0.1:PORT` address and manifest digest on readiness. Open
that exact HTTP address in the browser; using `localhost`, a different origin or
a reverse proxy is not this qualified development path. Port zero requests an
available port. A ready host is not proof of product or pilot readiness.

The manifest inventories only `/`, `/_assets/app.mjs`, `/_assets/api.mjs` and
`/_assets/styles.css` for this client. Native validation permits 1–8 declared files,
a manifest of at most 16 KiB and individual nonempty UTF-8 files of at most 64 KiB.
It checks absolute paths, allowed routes/types, actual regular-file metadata and
SHA-256 hashes before application state opens. Final-component symlinks and special
files are refused. Served bytes are retained in memory: requests cannot select
arbitrary filesystem paths, and editing a source file does not change a running
host's bytes. The manifest binds bytes, not publisher identity or verification
provenance; candidate artifact admission remains a separate requirement.

The public-assets mode checks the exact local Host and any Origin/Fetch-Site
headers and applies restrictive browser response headers. It does not introduce
cross-origin access, cookies, external binding, TLS or proxy qualification.
Static files do not require product authentication; all product API decisions
still run through SIGIL. Existing request, execution and resource ceilings remain.

## Interaction and recovery semantics

- Connect with a configured bearer credential. It is held in memory, not placed
  in URLs, cookies or browser storage; the input is cleared after connection.
  Refresh requires reconnecting. This does not scrub secrets deliberately typed
  into a conversation or protect a compromised browser.
- Start, find, reopen and continue conversations. History is revision-bound and
  paged; visible history can be an older committed snapshot while status refreshes.
  Untrusted messages and tool data are rendered as text, not executable markup.
- Submissions have a fresh identity bound to their exact request bytes. A lost
  acknowledgement offers an explicit identical retry. No refresh, reconnect or
  background poll silently submits another turn. Refresh also loses the in-memory
  retry payload; it does not imply that the server forgot the accepted operation.
- Cancellation acknowledgement means a request was accepted, not that remote work
  was undone. Possibly delivered outcomes and unknown usage remain visible; the
  client does not automatically repeat an uncertain external effect.
- Tenant/view changes discard stale asynchronous results. Model-requested tools
  are labelled as requests, not as proof that authorization or execution succeeded.
- In the explicit v8 request-admission profile, rate refusals display a valid
  Retry-After value only as bounded guidance. They do not schedule retries or
  cancel service-side work. A refused submission retains its exact bytes/key for
  explicit retry; refused status reads retain the accepted operation ID without
  inventing completion. Credential refusal clears retained content and connection
  state before another credential can connect.

## Reproducible development checks

CI pins Node using [`.node-version`](../.node-version). The package declares Node
20 or later and an exact Playwright development dependency; the lockfile retains
the package checksums. From the repo, with the required Node version available:

```text
npm ci --ignore-scripts --no-audit --no-fund
npx --no-install playwright install chromium
node scripts/browser_runtime.mjs
npm run test:web
```

Linux CI installs browser operating-system dependencies with `--with-deps`.
An existing matching installation may be selected with an absolute
`PI_PLAYWRIGHT_DIR`; the checker rejects a missing or mismatched Playwright/core
package or unavailable Chromium. It does not authenticate a browser installation.
Node/Playwright are test dependencies, not production application servers.

The mandatory [source gate](../ci.sh) includes the client unit suite, native asset
tests and real HTTP/browser integration tests, retaining the native worker/store,
fixed evaluator and real older-host prerequisites. Missing browser evidence fails
instead of silently skipping. These tests use a deterministic local provider, not
billable model access. Screenshots and traces can contain conversation data; retain
only approved fixture evidence and do not publish real credentials or user content.

## Evidence and remaining work

Before integration, all 17 staged HTTP/browser cases passed on macOS arm64, with
actual screenshot inspection. They cover the normal model/tool/response and
follow-up path, browser/API continuity, lost acknowledgement, tenant switching,
unknown/expired credentials, denied submission and refresh/cancellation after an
observed external request. The latter preserves uncertainty across service reopen.
That checkpoint also had 63 lightweight JavaScript checks and 12 native asset unit tests.

The expanded 2,265-case integrated local source gate passed on 2026-09-08, with
unchanged before/after source fingerprints and 91.19% line / 85.84% branch coverage.
The mandatory native/evaluator and frozen older-host checks remained included.
The Linux-only rename-observation case was skipped on macOS; the declared strict
research-only xfail remained. This is not a Linux, immutable-candidate or
independent-review pass.
Four additional actual-browser cases subsequently passed together with the existing
17 cases: **21 passed in 603.41 seconds**, using a fresh exact-lock Playwright
installation and the same host/runtime prerequisites. Six new screenshots were
inspected. These cases are now integrated as regressions, with no application,
native policy, resource limit or grant change:

- Unknown usage and reported quota exhaustion stop the turn after the first model
  and approved file action. The browser shows the recorded failure and known/unknown
  usage; refresh and host reopen retain the result without repeating model work.
  Independent committed-record checks verify the respective retained/released holds.
- Stopping the actual service after a committed turn causes a real connection failure.
  The browser retains the old answer with a connection warning. Reopening the service
  at the same origin restores status/history reads without another submission.
- An actual tenant-A history response is held while the browser reconnects as tenant B
  using the same conversation name. Releasing that response cannot replace B's content.
  Disconnect may abort the old read; this is not server credential-rotation evidence.

The subsequent **2,269-case full-source gate passed on 2026-09-09 UTC**, including
all 21 integrated HTTP/browser cases, with matching before/after source inputs and
91.19% line / 85.84% branch coverage. Seven actual screenshots from that run were
inspected, including the new failures, outage/restoration and tenant reconnection.
The mobile screenshot shows retained history while status refresh is pending;
the accounting screenshots show terminal failure while older history refreshes.
This local macOS run uses an existing Chromium cache; it is not clean-Linux or
candidate proof. Exact execution evidence is in the [acceptance record](mvp-acceptance.md).
The explicit v8 suite subsequently passed **14 browser scenarios plus one tooling
check in 696.09 seconds**. It adds actual read/submission/status quota refusals and
real credential expiry after loading retained content, with exact accounting and
effect checks. Ten cases retain original lifecycle/boundary browser drivers. A
driver observation race was fixed by waiting for actual read-control completion,
without changing expected traces or limits; the first failed run is retained in
the acceptance record. Screenshots of quota handling, retained content, expiry
clearing and second-tenant reconnection were inspected. The client/controller now
has **95 JavaScript checks**, and the verified browser files are integrated.

The preceding 2,687-case source gate passed before these browser additions. The
expanded **2,702-case source gate passed on unchanged inputs**, with 91.23% line /
85.98% branch coverage. Neither this local scripted-provider
evidence nor the temporary staging installation qualifies a release candidate.
Broader browser qualification remains, including expiry during active effects,
server credential rotation, history conflicts/tombstones,
preclaim cancellation and the full outage/fault matrix.
All eleven legacy API route migrations, export/deletion,
real-model task scores, independent onboarding, packaging and external pilot
clearance remain required. This development browser does not narrow those gates.
