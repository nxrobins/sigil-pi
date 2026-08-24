# sigil-pi product operations

This is the candidate operations guide while v1 readiness evidence is being completed.
Following it does **not** by itself satisfy the timed install, upgrade, rollback, recovery,
load, independent-review, or 30-day pilot gates recorded in `docs/product-readiness.md` in the
sigil-pi repository at this release's version tag (that record is deliberately not packaged here).

## Supported candidate topology

- Python 3.12–3.14 on Linux or macOS, on a local POSIX filesystem with correct `flock`
  semantics.
- Exactly one product worker process on the host and local state volume. The implementation
  uses OS file locks and transactional leases defensively, but more than one worker is not a
  supported launch topology until telemetry is aggregated and that topology passes a new
  capacity qualification.
- Additional product workers, network filesystems, multiple hosts, Windows, memory sidecars,
  and the legacy research endpoints are unsupported and must not be presented as v1 product
  configurations.

This is deliberately narrower than a typical distributed service. The 60-minute capacity
test must run with exactly one worker before this topology becomes a launch commitment. The
proposed envelope and exact conjunctive gate are in `docs/capacity.md`.

## Required configuration

- `ANTHROPIC_API_KEY`: injected at runtime from the deployment's secret manager.
- `PI_AUDIT_KEY`: a high-entropy HMAC key injected separately from the audit storage. Product
  startup refuses unsigned audit operation.
- `PI_AUTH_FILE`: mounted JSON authorization policy containing token digests and bounded Unix
  validity windows, not tokens. Product startup requires at least one currently active entry.
- `PI_MAX_CREDENTIAL_LIFETIME_DAYS` (default `90`) caps every configured bearer lifetime.
- `SIGIL_ROOT`: checkout containing the pinned `sigil-mcp` binary for source operation. The
  candidate release bundle sets this to its included immutable runtime automatically. The
  binary must be a solver-verifying build (`--features sigil-mcp/solver`, which `ci.sh` and
  the release builder pass): the product client never sets `SIGIL_ALLOW_UNVERIFIED_CERT`,
  so a solver-off build fails closed at the first forge with `R817`. That build links
  `libz3` dynamically; the host must provide the Z3 release the binary was built against.

The product entry point is:

```sh
python3 product_main.py
```

It serves the versioned API only. Do not use `PI_SERVE=1 python3 agent.py` as a product
process; that command serves the research endpoint, whose single shared token (if any)
authenticates callers without tenant isolation, scopes, or quotas.

For a release candidate, verify the outer checksum before extracting, then run `bin/sigil-pi`.
The bundle includes the product sources, exact SIGIL binary and required stdlib, internal
manifest, CycloneDX SBOM, and operations/API documentation. Tag builds run the mandatory
gate before GitHub signs provenance and SBOM attestations. A workflow definition is not
release evidence until a protected tag run succeeds and its attestation is independently
verified.

## Network and TLS

`PI_HOST` defaults to `127.0.0.1` and `PI_PORT` to `8080`. A non-loopback bind requires both
`PI_TLS_CERT` and `PI_TLS_KEY`; otherwise startup fails. A deployment may instead keep the
service on loopback and use a local production proxy for TLS, connection management, and
edge denial-of-service protection.

## Initial resource controls

- `PI_REQUESTS_PER_MINUTE` (default `60`) per authenticated tenant.
- `PI_MAX_CONCURRENT_TURNS` (default `2`) per authenticated tenant.
- `PI_TOKENS_PER_DAY` (default `10,000,000`) provider-reported input plus output tokens per
  tenant per UTC day; `PI_TOKEN_RESERVATION_PER_TURN` (default `4,000,000`) is reserved before
  each interactive or scheduled turn.
- `PI_STORAGE_BYTES_PER_TENANT` (default `1 GiB`) for aggregate conversations and regular
  sandbox files; `PI_STORAGE_RESERVATION_PER_TURN` defaults to `16 MiB`.
- `PI_AUDIT_BYTES_PER_TENANT` (default `256 MiB`) for aggregate signed audit chains;
  `PI_AUDIT_RESERVATION_PER_TURN` defaults to `1 MiB`.
- `PI_TURN_LEASE_SECONDS` (default `300`) bounds recovery of a tenant concurrency slot after
  an unclean worker exit. Startup requires it to exceed `PI_TURN_DEADLINE_SECONDS`.
- `PI_TURN_DEADLINE_SECONDS` (default `120`) is the hard wall-clock deadline for an
  interactive or scheduled turn, including same-session and forge-serialization waits.
- `PI_MAX_SCHEDULES_PER_TENANT` (default `32`).
- `PI_MAX_EXPORT_BYTES` (default `10 MiB`) for one in-memory session export.
- `PI_SOCKET_TIMEOUT_SECONDS` (default `30`) for inbound socket inactivity.
- `PI_MCP_TIMEOUT_SECONDS` (default `90`) for one forge protocol response; a timeout kills
  the wedged compiler process rather than leaving a request blocked indefinitely.
- `PI_AUDIT_VERIFY_SECONDS` (default `60`) for automatic full signed-chain verification.
  A failed check makes readiness fail and emits a content-free critical structured event.
- `PI_RETENTION_DAYS` (provisional default `90`) for inactive conversations, regular sandbox
  files, schedules, and signed audit chains. `PI_RETENTION_INTERVAL_SECONDS` (default `3600`)
  bounds the delay between eligibility and the next sweep.
- Existing agent bounds: `PI_MAX_STEPS`, `PI_MAX_HISTORY_BYTES`, and
  `PI_MAX_TOOL_RESULT_BYTES`.

All product numeric limits must be positive; invalid values fail startup. An empty
`PI_NET_ALLOWLIST` continues to deny the general fetch tool.

Tenant request, active-turn, daily token, conversation/sandbox storage, and audit accounting
is stored in `product-quotas.sqlite3` using transactional SQLite, so adding workers on the
supported shared-local-filesystem topology does not multiply quotas. Scheduled turns acquire
the same reservations as interactive turns. Admission includes active reservations from every
worker; completion replaces them with provider-reported usage and exact file sizes. A turn can
settle above a reservation only by its already-bounded single-turn usage, after which further
turns are refused. Operators must therefore keep reservations at or above the measured
single-turn maximum; startup additionally proves the token reservation covers the configured
maximum output tokens across all steps.

Token exhaustion returns `429 token_quota_exceeded` with `Retry-After` to the next UTC day.
Storage or audit exhaustion returns `507 storage_quota_exceeded` or
`507 audit_quota_exceeded`. Deleting a session credits its measured storage and audit usage.

## Active-state retention and deletion

Product startup verifies all signed audit chains, reconciles exact session/audit sizes and
activity from supported files, and completes an initial retention sweep before opening the
listener. A background sweep then runs at the configured interval. Accepted interactive and
scheduled turns refresh session activity. Creating, updating, or running a schedule refreshes
its activity and keeps the associated session active; listing or exporting data does not
extend its life.

The sweep first verifies all signed audit chains so retention cannot hide a detected
integrity failure. It selects inactive ledger rows, takes the same per-session cross-worker
lock used by chat/scheduling/export/delete, re-checks activity, removes schedules and files,
then removes the durable registry row last. An interrupted sweep therefore leaves an
attributable row that startup reconciliation and the next sweep can safely retry. Legacy
schedules without per-entry timestamps conservatively use the schedule-store file mtime as
their first upgraded retention boundary. Schedule-only entries have an independent sweep
because no turn may yet have created a session ledger row.

Sweep failure makes `/v1/ready` fail and emits a content-free `retention_cleanup` event at
`critical` severity. The authenticated metrics endpoint reports retention checks, failures,
last check, cumulative deleted session/schedule counts, and configured duration. Route the
critical event with the audit-verification event and investigate the state volume before
restarting or overriding anything. There is no retention bypass knob; all numeric values
must be positive.

Authenticated `DELETE /v1/sessions/{session}` remains the immediate active-storage deletion
path. The automatic policy covers active state only. It does not delete already-created
offline backups, and the provisional 90-day value is not a launch approval or legal policy.

At the hard turn deadline, the host interrupts the active forge by killing the compiler
process, records a content-free `turn_deadline_exceeded` audit event, persists the bounded
partial conversation, releases the session/concurrency locks, and returns stable HTTP `504`.
It then **retires** that compiler: the next forge starts a fresh, re-verified one, and turns
for other tenants are unaffected. Readiness fails, and a supervisor restart is required, only
when repeated replacements cannot produce a working compiler.

A replacement costs roughly one spawn plus one handshake (~16 ms measured locally) and is
charged to the turn that finds the gap, never to the turn that was killed. Replacements not
explained by a caller's own expired budget — a compiler that stops answering the base
`PI_MCP_TIMEOUT_SECONDS` watchdog, or fails its handshake — count against runtime health and
are exported as `sigil_pi_runtime_unhealthy_replacements_total`; alert on its rate. A client disconnect does not independently cancel accepted work; this preserves the
single committed session order. The server deadline remains authoritative.

The product runtime client forcibly removes `SIGIL_ALLOW_UNVERIFIED_CERT` from the compiler
child environment. That benchmark-only escape hatch cannot be enabled for product traffic,
even by an inherited operator environment variable.

Python runtime dependencies are declared in `requirements-runtime.lock` (currently standard
library only); the complete test/tool environment is pinned in `requirements-dev.lock`.

## Credential rotation and revocation

Rotation is explicit and restart-bound:

1. Generate a new high-entropy token in the approved secret manager. Add its digest to the
   auth file with an overlapping `not_before_unix` / `expires_unix` window and exactly the
   same tenant, principal, scopes, and tools as the old credential.
2. Atomically replace the mounted policy and perform a graceful rolling restart of workers.
   Conflicting duplicate-principal policy, lifetime above the configured maximum, or a file
   with no currently active credential fails startup.
3. Migrate the client to the new token. Unknown, expired, and not-yet-valid tokens all fail
   identically, so validity state is not disclosed at the API boundary.
4. Remove the old digest and perform a second rolling restart before expiration. Emergency
   revocation uses the same removal/restart path.

There is no live policy reload or external identity-provider feed in v1. The deployment must
retain enough healthy workers behind its local proxy for rolling restart; otherwise rotation
causes the ordinary documented single-worker restart interruption.

Product startup verifies all existing audit chains before accepting traffic. The background
monitor then repeats verification, reports chain/record counts and failure totals in the
authenticated metrics endpoint, and emits `audit_verification` at `critical` severity on
failure without including file names, record contents, or verifier diagnostics. Route that
event to the deployment alert manager; the repository does not yet contain evidence for a
tested alert receiver or response runbook.

The authenticated JSON metrics response includes process peak memory, active thread/open-FD
counts, state/audit/sandbox byte totals, disk capacity, and fixed-bucket turn-latency and
queue-wait histograms when product startup supplies its state root. The equivalent
`GET /v1/metrics/prometheus` endpoint uses Prometheus text format. Readiness and both metric
formats identify runtime-process health, SQLite quota integrity, schedule decoding, active
state-root accessibility, signed-audit verification, and retention health separately. A
probe exception fails that component closed without returning a path or diagnostic. Chat responses include
per-turn queue duration and tool/retry counts. These are the inputs to `scripts/load_test.py`;
they are process-local, so the current qualifying topology is one worker. A short
`--smoke-test` checks mechanics but is deliberately ineligible as the required 60-minute
evidence.

Give the collector a dedicated credential with only `ops:read`, delivered through the
approved secret manager into a root/collector-readable credential file. Do not put the bearer
token in repository configuration, command arguments, or dashboard variables. A representative
Prometheus scrape job behind the production TLS endpoint is:

```yaml
scrape_configs:
  - job_name: sigil-pi
    scheme: https
    metrics_path: /v1/metrics/prometheus
    authorization:
      type: Bearer
      credentials_file: /run/secrets/sigil-pi-metrics-token
    tls_config:
      ca_file: /run/secrets/sigil-pi-metrics-ca.pem
    static_configs:
      - targets: ["sigil-pi.example:443"]
```

Import `config/grafana-slo-dashboard.json`, select that Prometheus data source when prompted,
and keep the dashboard read-only. It includes aggregate and component readiness, 5xx and successful-turn ratios,
turn p50/p95/p99, queue p95, disk, lifecycle integrity, activity, and storage/resource panels.
The latency dashboard intentionally reports aggregate turns; the digest-bound load report is
the authoritative source for the required separate single-step and tool-using percentiles.
Verify the scrape returns `200`, that a deliberately stopped canary makes the readiness panel
fall, and that the dashboard covers the whole pilot window before recording evidence.

The machine-readable launch alert contract is `config/alert-policy.json`; every alert has a
severity, named owner role, exact metric/event condition, persistence/window rule, and a
resolving procedure in `docs/runbooks.md`. Deployments must translate this contract into their
authenticated collector/alert router without adding tenant, principal, session, or content
labels. Repository validation proves exporter/dashboard/policy/runbook consistency only.
Readiness still requires collector and dashboard deployment evidence, routed alert tests,
named humans for the owner roles, and timed execution of the principal runbooks against the
exact candidate.

## Graceful shutdown

`SIGTERM` and `SIGINT` cause readiness to fail, reject new turns, stop the listener, and wait
up to `PI_DRAIN_TIMEOUT_SECONDS` (default `120`) for accepted turns to finish. Failure to
drain is a non-clean shutdown and must be surfaced by the process supervisor.

A successful drain writes `.product-clean-shutdown.json` in `PI_STATE`. Startup removes the
old marker before opening state. The backup command refuses to run without it, so a crash or
failed drain cannot be mistaken for a consistent offline recovery point.

## Offline backup and restore

Backups currently require a maintenance stop. First send `SIGTERM` and wait for the product
process to exit successfully. With `PI_AUDIT_KEY` injected from the secret manager:

```sh
python3 state_tool.py backup \
  --state /var/lib/sigil-pi \
  --output /secure-backups/sigil-pi-state.tar.gz
```

The command verifies every signed audit chain, validates conversation and schedule state,
rejects symlinks/special files, enforces a 10 GiB default aggregate bound, writes a 0600
archive without overwriting an existing path, and records a per-file size and SHA-256
manifest. The quota database is included
because its token and tenant/session usage ledger is durable security state; backup validates
SQLite integrity, schema, nonnegative hashed usage records, and zero active turn leases, and
captures committed WAL contents through SQLite's backup API. The per-session activity field
used by retention is validated and preserved. Lock files remain operational and are
regenerated.

For an operator-approved backup-retention duration, use the managed one-shot cycle while the
service remains cleanly stopped:

```sh
python3 state_tool.py backup-cycle \
  --state /var/lib/sigil-pi \
  --backup-dir /secure-backups/sigil-pi \
  --retention-days <approved-backup-retention-days>
```

The backup directory must be a real `0700` directory on approved encrypted storage. The
cycle takes an exclusive local lock, creates a `0600` archive whose name binds its creation
time and SHA-256, then validates the digest, schema, manifest, file type, link count, and mode
of every managed archive before deleting the first expired one. It ignores unrelated names,
refuses linked/tampered/malformed managed files, never overwrites an existing backup, and
returns a content-free JSON record of the created and deleted digests. Run it from the
deployment scheduler only after a successful clean drain; alert on any nonzero exit and
retain its JSON result in the evidence system.

This repository supplies the locked creation/expiry mechanism, not the scheduler, encrypted
volume, or approved duration. Because backups are offline, a deployment must demonstrate
that its drain-and-cycle schedule both keeps backup age within 15 minutes and preserves the
published availability objective; otherwise this topology cannot pass GA.

Restore only into a path that does not exist:

```sh
python3 state_tool.py restore \
  --backup /secure-backups/sigil-pi-state.tar.gz \
  --state /var/lib/sigil-pi-restored
```

Restore rejects traversal, links, duplicate/undeclared members, checksum mismatch, unknown
schemas, oversized archives, existing targets, corrupt conversations/schedules, and invalid
audit signatures. It stages and validates the complete tree before atomically installing it.
Keep the backup on approved encrypted storage with access controls and the approved managed
retention policy. Repository tests prove creation, validation, non-overwrite, expiry, and
restore mechanics only; readiness still requires production scheduling no more than 15
minutes apart plus timed restore, RPO, RTO, deletion, and rollback evidence.

The supported upgrade, schema-migration, and rollback boundary is defined in
`docs/state-compatibility.md`. In particular, create the pre-upgrade backup with the old
release, restore and validate it into a new state path with the candidate, and never let a
candidate mutate the only production copy. An unknown or unattributed state schema is a hard
stop, not an invitation to attempt an in-place upgrade.

## Clean-install, upgrade, and rollback drill

The qualifying distinct-version drill runs in the `Recovery drill` workflow
(`.github/workflows/recovery-drill.yml`, dispatch-only): a hosted Linux runner is both a clean
host and the platform the published artifacts are built for. The workflow verifies the candidate
against `docs/evidence/candidate.json` and the old release against its `rollback_from` before
drilling anything, produces the pre-upgrade backup with `scripts/drill_fixture.py` — which boots
the old release, commits one real turn through its own forge path, drains cleanly, and backs up
with the old release's own `state_tool.py` — and preserves `recovery-drill.json` as a run
artifact. The pinned Z3 shared library is installed into the loader's default path first, because
the drill's probe strips `LD_LIBRARY_PATH` exactly as a production host would.

Every bundle includes `bin/sigil-pi-release-drill`. Run the copy from the candidate against a
distinct currently deployed artifact, the candidate artifact, and a production-equivalent
backup created no more than 15 minutes earlier. Supply the audit key through a private regular
file so it never appears in the command line or report:

```sh
bin/sigil-pi-release-drill \
  --old-artifact /releases/sigil-pi-0.9.0-linux-x86_64.tar.gz \
  --new-artifact /releases/sigil-pi-1.0.0-linux-x86_64.tar.gz \
  --state-backup /secure-backups/pre-upgrade-state.tar.gz \
  --audit-key-file /run/secrets/sigil-pi-audit-key \
  --work-dir /var/tmp/sigil-pi-v1-drill-20260820 \
  --output /secure-evidence/recovery-drill.json
```

The work directory and output must not already exist. The harness rejects unsafe archive
members, verifies every inner release-manifest checksum, installs both releases separately,
and atomically switches a `current` link. It then executes real authenticated service probes
through the embedded compiler in this order: clean old install, restored old release,
candidate upgrade, and old-release rollback. Each process must become ready and shut down
cleanly. The harness re-backs up the final state and byte-compares every committed session,
sandbox, and signed-audit file with the input backup.

The command fails unless the artifacts are distinct versions, the input backup is at most 15
minutes old and contains at least one committed session, RTO is at most four hours, rollback
is at most 15 minutes, and all customer files survive. Its 0600 JSON report binds every phase
to the old or new archive SHA-256. `--mechanics-only` permits a deliberately non-qualifying
same-artifact rehearsal but preserves the reasons in the report; it must never be used as GA
evidence. The readiness validator independently rechecks the raw report rather than trusting
its top-level pass flag.

## Cognitive memory

Memory remains outside the v1 production support set until its export/deletion and backup
lifecycle are implemented. Product startup therefore rejects `PI_MEMORY_SIDECAR` rather
than silently offering a state surface that cannot satisfy the product data contract.

## Current operational blockers

Before this guide can become a production runbook, the project still needs an immutable
runner/base-image pin, successful signed tag evidence, encrypted automated backup scheduling
and exercised deletion evidence, final approval of active and backup retention durations,
metrics/alerts, load evidence, and exercised
upgrade/rollback/recovery/incident procedures. The authoritative state and required evidence
are maintained in `docs/product-readiness.md` in the repository at this release's version tag.
