# sigil-pi product operations

This is the candidate operations guide while v1 readiness evidence is being completed.
Following it does **not** by itself satisfy the timed install, upgrade, rollback, recovery,
load, independent-review, or 30-day pilot gates recorded in `docs/product-readiness.md` in the
sigil-pi repository at this release's version tag (that record is deliberately not packaged here).

## Supported candidate topology

- Python 3.12–3.14 on Linux or macOS, on a local POSIX filesystem with correct `flock`
  semantics. The bundle's launcher runs the first `python3` on `PATH`; outside 3.12–3.14,
  `bin/sigil-pi` and `state_tool.py` exit with status 1 before reading configuration or
  state, naming the supported range and the version found.
- Conversations, schedules, signed audit records and the clean-shutdown marker are synced to
  stable storage before the operation that wrote them returns. A whole-file save (a
  conversation, `product-schedules.json`, the marker) goes to a uniquely named scratch file
  beside its target (`.<name>.<16 hex>.tmp`); the file is synced, renamed into place, and its
  directory synced. Each audit record is synced as it is appended. Deleting a session syncs
  each removal before its ledger row is removed. On Linux this survives a power loss. On
  macOS, `fsync` does not flush the drive's own cache, and neither these files nor the SQLite
  ledger request `F_FULLFSYNC`, so a macOS host is not power-loss durable. Files a tool
  writes into a session sandbox are written by the compiler, which does not sync them. A host
  killed mid-save can leave a scratch file; it is never read as state, is excluded from
  backups, and is removed when its session is deleted and at the next startup.
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
- `PI_TENANT_SECRETS_DIR` (required when any principal's policy names a secret): a directory
  the operator mounts holding each tenant's tool credentials at
  `<dir>/<SHA-256 hex of the tenant name>/<secret name>`, one value per file: one line of
  printable ASCII without spaces, at most 4096 bytes; one trailing newline is ignored. The
  directory, each tenant directory and each file must be owned by the user the service runs
  as and give no access to its group or others (0700 directories, 0600 or 0400 files), and
  nothing inside it may be a symbolic link (the directory itself may be reached through one).
  A Kubernetes secret volume's files are links into `..data`: copy them into such a
  directory first. Compute a tenant's directory name with
  `printf %s "$TENANT" | shasum -a 256`. Values are read once at startup, so a change takes a
  restart, as an auth-file change does. Errors name paths, never values.
- `PI_STATE` (default `.pi-state` beside `product_main.py`): the state directory, which holds
  conversations, sandboxes, schedules, signed audit chains and `product-quotas.sqlite3`. Set
  it to a directory on the local POSIX state volume; the examples below use
  `/var/lib/sigil-pi`.
- `SIGIL_ROOT`: checkout containing the pinned `sigil-mcp` binary for source operation. The
  candidate release bundle sets this to its included immutable runtime automatically. The
  binary must be a solver-verifying build (`--features sigil-mcp/solver`, which `ci.sh` and
  the release builder pass): the product client never sets `SIGIL_ALLOW_UNVERIFIED_CERT`,
  so a solver-off build fails closed at the first forge with `R817`. That build links
  `libz3` dynamically; the host must provide the Z3 release the binary was built against.
  `toolchain.py` prefers an explicit compiler (`PI_FORGE_BIN` with `PI_STDLIB_DIR`) or an
  installed release (`PI_TOOLCHAIN_DIR`) to `SIGIL_ROOT`, under the bundle's launcher too:
  leave both unset for a release bundle, whose startup refuses a compiler that does not
  match its pin.

The product entry point is:

```sh
python3 product_main.py
```

It serves the versioned API only. Do not use `PI_SERVE=1 python3 agent.py` as a product
process; that command serves the research endpoint, whose single shared token (if any)
authenticates callers without tenant isolation, scopes, or quotas.

For a release candidate, verify the outer checksum before extracting, then run `bin/sigil-pi`.
The bundle includes the product sources, exact SIGIL binary and required stdlib, internal
manifest, CycloneDX SBOM, and operations/API documentation. The SBOM lists every crate
SIGIL's Cargo.lock resolves for the compiler (a superset of what one platform links), the Z3
it loads from the host and the Lean and Rust toolchains linked into it, each with the file its
version was read from. `app/SIGIL_REV` pins the bundled compiler's digest; startup and every
compiler replacement refuse a compiler that does not match it, with
`configuration error: forge binary does not match SIGIL_REV`. Reinstall the bundle from its
release archive; never edit `app/SIGIL_REV`.

A tag build runs in three jobs. The gate refuses a tag that is not on `main` or does not match
`VERSION`, then runs the mandatory gate with a token that can only read. The build job builds
the bundle from a fresh checkout at the SIGIL pin, with no cache and no Python packages. Only
the publish job, which checks out nothing, can write and sign: it checks the archive against
the digest the build job reported, then GitHub signs provenance and SBOM attestations. A
workflow definition is not release evidence until a protected tag run succeeds and its
attestation is independently verified.

## Network and TLS

`PI_HOST` (default `127.0.0.1`) and `PI_PORT` (default `8080`) are the listener's address. A
non-loopback bind requires both `PI_TLS_CERT` and `PI_TLS_KEY` (the certificate chain and
private key files, PEM); otherwise startup fails. A deployment may instead keep the service
on loopback and use a local production proxy for TLS, connection management, and edge
denial-of-service protection. The listener itself authenticates before it reads a body, caps
the connections it handles at once (`PI_MAX_CONNECTIONS`) and bounds how long a request may
take to arrive (`PI_REQUEST_DEADLINE_SECONDS`). It does not limit one client's share of the
cap: a client that opens many slow connections can fill it and delay others, each connection
until its deadline. Per-client limits need a proxy.

## Model provider and prompt

- `PI_ENDPOINT` (default `https://api.anthropic.com/v1/messages`): the Anthropic
  Messages-compatible endpoint. Its host name is the only `net` grant of the forge that calls
  the model, and the endpoint must also implement token counting (see below).
- `PI_MODEL` (default `claude-sonnet-5`): the model every turn uses.
- `PI_THINKING` (default `adaptive`; `disabled` or `between_tools`) is sent as `thinking` on
  every model request and reported in `effective_settings`. Any other value fails startup.
  Name the value the configured model accepts; a value the model refuses fails every turn
  with `502 agent_failure`. `adaptive` is refused by Opus 4.5, Sonnet 4.5 and Haiku 4.5,
  `disabled` by Opus 5.5, Sonnet 5.5, Fable and Mythos, `between_tools` by every model but
  Sonnet 5.5.
- `PI_LLM_RETRIES` (default `2`): host-side retries of a model call that failed with `408`,
  `429`, a 5xx or a transport error, the first after 0.5 s and each next one after four times
  the wait before it. A grant denial or another 4xx is never retried. `0` turns retries off and a negative value fails startup. Each retry is admitted
  against the token quotas as its own attempt.
- `PI_SYSTEM` (unset by default) and `PI_SYSTEM_FILE` (default `AGENTS.md` beside
  `product_main.py`, read only if it exists) make up the system prompt: `PI_SYSTEM` first,
  then the file. It is sent with every model request, so its size counts against every turn's
  tokens.

The provider key is `ANTHROPIC_API_KEY`, one operator key that every tenant's model calls
use. No tool or guest receives it: the runtime receives it as the model call's secret grant
and substitutes it in its HTTP shim. The compiler's environment carries no `PI_*` or
`ANTHROPIC_*` value.

## Initial resource controls

- `PI_REQUESTS_PER_MINUTE` (default `60`) per authenticated tenant.
- `PI_MAX_CONCURRENT_TURNS` (default `2`) per authenticated tenant.
- `PI_TOKENS_PER_DAY` (default `10,000,000`) bounds each tenant's daily model-call debits.
- `PI_MAX_TOKENS_PER_TURN` bounds one interactive or scheduled turn, independently of the
  daily quota. Its default is `PI_TOKEN_RESERVATION_PER_TURN` (legacy setting, default
  `1,000,000`). Capacity is reserved before each generation attempt, including retries;
  a turn no longer reserves its entire ceiling at admission. The legacy fixed-reservation
  value is retained for older embedders, capped at the daily limit; product turns do not use it.
- `PI_STORAGE_BYTES_PER_TENANT` (default `1 GiB`) for aggregate conversations and regular
  sandbox files; `PI_STORAGE_RESERVATION_PER_TURN` defaults to `16 MiB`.
- `PI_AUDIT_BYTES_PER_TENANT` (default `256 MiB`) for aggregate signed audit chains;
  `PI_AUDIT_RESERVATION_PER_TURN` defaults to `1 MiB`.
- `PI_TURN_LEASE_SECONDS` (default `300`) bounds recovery of a tenant concurrency slot after
  an unclean worker exit. Startup requires it to exceed `PI_TURN_DEADLINE_SECONDS`. It is
  also the lease on a claimed schedule entry, so a scheduled run whose completion could not
  be recorded runs again after it, and, with the one-second tick interval added, the time
  after which a scheduler that has completed nothing is reported not ready.
- `PI_TURN_DEADLINE_SECONDS` (default `120`) is the hard wall-clock deadline for an
  interactive or scheduled turn, including same-session waits, compiler queueing, token counting and generation.
- `PI_MAX_SCHEDULES_PER_TENANT` (default `32`).
- `PI_MAX_SCHEDULE_BYTES_PER_TENANT` (default `8 MiB`): the most bytes one tenant's schedule
  messages may take in the schedule store, counted as stored (JSON escapes included). The
  default holds 32 plain-text messages of 256 KiB. Every scheduled run reads and rewrites the
  whole store under its lock, so a run costs time in proportion to the sum of every tenant's
  schedules: about 27 ms per 8 MiB on the development Mac. Lower it to bound that cost.
- `PI_MAX_EXPORT_BYTES` (default `10 MiB`) for one in-memory session export.
- `PI_SOCKET_TIMEOUT_SECONDS` (default `30`) for inbound socket inactivity: each read of a
  request and each write of an answer waits at most this long. On a direct TLS listener it
  also bounds each client's whole TLS handshake, which runs on that connection's own thread:
  a client that has not completed its handshake within the timeout is disconnected without a
  response, and no other client waits for it.
- `PI_REQUEST_DEADLINE_SECONDS` (default `60`) for receiving a whole request: its TLS
  handshake, head and body must arrive within this many seconds of the listener taking the
  connection, however they are paced. A client whose head is unfinished at the deadline is
  disconnected without a response, and one whose body is unfinished is answered
  `400 invalid_content_length`. The deadline bounds neither the turn, which
  `PI_TURN_DEADLINE_SECONDS` bounds, nor the answer.
- `PI_MAX_CONNECTIONS` (default `128`) for connections handled at once, each on its own
  thread. At the cap the listener takes no connection until one ends: the kernel completes
  about 128 more, which wait unanswered in the listen queue and are then served in turn, and
  leaves further attempts unanswered, so their connect times out or is retried. Keep it well
  below the process's open-file limit: each connection holds a descriptor.
- A request is authenticated from its headers before any of its body is read; one without a
  valid credential is answered `401` at once and its connection closed. Header lines may
  total 64 KiB (`431` past it). `Content-Length` must be one run of digits, at most 1 MiB: an
  invalid, conflicting or oversized length, or a body that ends before it, is
  `400 invalid_content_length`.
- `PI_COMPILER_POOL_SIZE` (default `4`, maximum `32`) independently supervised compiler
  processes per product worker, warmed concurrently before readiness. Each compiler runs
  one forge at a time; separate sessions
  can overlap. Same-session ordering is unchanged. Setting `1` restores serial execution.
- `PI_MODEL_HTTP_TIMEOUT_MS` (default and maximum `60000`) is SIGIL's total deadline for
  one provider HTTP request, including headers and the complete body, capped by the turn's
  remaining time. It applies to counting and generation. The runtime must advertise
  `single-request-v1`; older binaries fail closed. Redirects are refused in this mode.
  Other guests retain SIGIL's existing default timeout and redirect policy.
- `PI_MCP_TIMEOUT_SECONDS` (default `90`) for one whole forge exchange, and for a compiler's
  initialization handshake: writing the request and reading every byte of its response share
  one deadline, the turn's remaining budget when that is shorter. A compiler that stalls
  before or partway through a response, trickles it, or stops reading its input is killed at
  the deadline rather than leaving a request blocked, and its replacement also spends the
  caller's remaining turn budget. A response longer than 64 MiB is refused and its compiler
  replaced; the pinned compiler's longest legitimate response, a 5 MiB tool output of control
  characters escaped twice, is 35 MiB.
- `PI_AUDIT_VERIFY_SECONDS` (default `60`) for automatic full signed-chain verification.
  A failed check makes readiness fail and emits a content-free critical structured event.
- `PI_RETENTION_DAYS` (provisional default `90`) for inactive conversations, regular sandbox
  files, schedules, and signed audit chains. `PI_RETENTION_INTERVAL_SECONDS` (default `3600`)
  bounds the delay between eligibility and the next sweep.
- Agent bounds: `PI_MAX_STEPS` (default `8`) model round trips one turn may spend;
  `PI_MAX_HISTORY_BYTES` (default `256 KiB`) bounds the transcript a turn sends and saves,
  which drops earlier turns and never the turn in progress; and `PI_MAX_TOOL_RESULT_BYTES`
  (default `16 KiB`) bounds one tool result.
- `PI_MAX_TOKENS` (default `16000`) caps one model reply. The cap covers the model's
  thinking as well as its answer, so a small value can leave no room for the answer. A reply
  that reaches it is not used: the turn fails with `502 agent_failure`. Every model attempt
  is admitted for the whole cap, so it also sets how much of `PI_MAX_TOKENS_PER_TURN` and
  the daily quota one attempt needs.

All product numeric limits must be positive (`PI_LLM_RETRIES` may be `0`); invalid values
fail startup. Product startup refuses `PI_SECRET_*` and `PI_NET_ALLOWLIST`, which would give
every tenant's tools the same credentials and hosts: name secrets in a principal's
`"secrets"` and hosts in its `"net_allowlist"` in `PI_AUTH_FILE`. A principal whose policy
names no host cannot use `fetch`; a policy naming a secret its tenant has no value for stops
startup.

Tenant request, active-turn, daily token, conversation/sandbox storage, and audit accounting
is stored in `product-quotas.sqlite3` using transactional SQLite, so adding workers on the
supported shared-local-filesystem topology does not multiply quotas. Scheduled turns acquire
the same token budgets as interactive turns. Before generation the agent calls the provider's
`/v1/messages/count_tokens` endpoint through SIGIL with the exact model, messages, system,
tools and thinking settings. It atomically debits the estimated input, the larger of 1,024
or 10% input headroom (rounded up), and the complete requested output cap. Both the turn
ceiling and remaining daily capacity must fit. There is no Python provider HTTP path.

A validated usage receipt replaces that attempt's debit with ordinary input, cached input
(read and creation), and output tokens. An unavailable/invalid receipt, timeout, disconnect,
crash or expired lease retains the whole debit; each retry needs another reservation.
SQLite transactions prevent concurrent workers from spending the same daily capacity.
Debits belong to their dispatch UTC day; a late receipt never credits a newer day's balance.
A provider that exceeds its allowance is charged at actual usage and stops the turn before
any tools run (`502 provider_token_budget_exceeded`). Provider token counting is an estimate,
so this is enforced admission with conservative headroom, not a guarantee about the provider's
final invoice. See the [provider's token-counting contract](https://platform.claude.com/docs/en/build-with-claude/token-counting).
Counting failures stop before generation. Compatible provider endpoints must implement counting.

Daily exhaustion returns `429 token_quota_exceeded` with `Retry-After` to the next UTC day.
Turn exhaustion returns `429 turn_token_budget_exhausted` with completed tool progress saved;
a new user turn can continue if daily capacity remains. The host keeps the configured output
cap instead of silently shrinking it and producing a truncated answer. Storage and audit
reservations still reconcile to exact measured file sizes on completion.
Storage or audit exhaustion returns `507 storage_quota_exceeded` or
`507 audit_quota_exceeded`. Deleting a session credits its measured storage and audit usage.

Scheduled turns run on a pool, at most four at once and at most one of a tenant's at a time,
so one tenant's schedules cannot hold up another's. The scheduler's own thread runs one of
them and each other has a thread of its own; while they run, the scheduler looks for due
schedules again whenever one finishes and at least every second. When all four are busy, the
tenant served least recently goes first, and of a tenant's schedules the one due longest. The
pool size, the per-tenant cap and the one-minute floor on `every_ms` are fixed in this
release, not settings. Scheduled turns share the compiler pool (`PI_COMPILER_POOL_SIZE`)
with chat turns.

## Active-state retention and deletion

Product startup verifies all signed audit chains, reconciles exact session/audit sizes and
activity from supported files, and completes an initial retention sweep before opening the
listener. Before it reconciles, startup removes the scratch files that killed saves left
(conversations, schedules and the clean-shutdown marker) and reports the result as one
content-free `state_reconciliation` event carrying four counts: `sessions`,
`sessions_missing_storage`, `sessions_missing_audit` and `scratch_files_removed`. The event is
`info` with status `ok`, or `critical` with status `failed` when a ledger row recorded bytes
for a conversation and sandbox, or for an audit chain, of which nothing is left on disk.
Startup continues either way and sets the row to what is on disk, so the event is the only
trace of the loss: route it with the other critical events (`docs/runbooks.md`, State
reconciliation failure). It does not detect a file that is present but older or smaller than
the ledger recorded. A background sweep then runs at the configured interval. Accepted interactive and
scheduled turns refresh session activity. Creating, updating, or running a schedule refreshes
its activity and keeps the associated session active; listing or exporting data does not
extend its life.

The sweep first verifies all signed audit chains so retention cannot hide a detected
integrity failure. It selects inactive ledger rows, takes the same per-session cross-worker
lock used by chat/scheduling/export/delete, re-checks activity, zeroes the row's recorded
bytes, removes schedules and files (scratch files included, each removal synced), then removes
the durable registry row last. An interrupted sweep therefore leaves an attributable row that
startup reconciliation and the next sweep can safely retry, and that startup does not report
as lost files. Legacy
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
backups, and the provisional 90-day value is not a launch approval or legal policy.

At the hard turn deadline, the host interrupts the active forge by killing the compiler
process, records a content-free `turn_deadline_exceeded` audit event, persists the bounded
partial conversation, releases the session/concurrency locks, and returns stable HTTP `504`.
It then **retires** that compiler: the next forge starts a fresh, re-verified one, and turns
for other tenants are unaffected. Readiness fails, and a supervisor restart is required, only
when repeated replacements cannot produce a working compiler.

A replacement costs roughly one spawn plus one handshake (~16 ms measured locally) and is
charged to the turn that finds the gap, never to the turn that was killed. Replacements not
explained by a caller's own expired budget — a compiler that does not finish an exchange
within the base `PI_MCP_TIMEOUT_SECONDS` watchdog, sends a response over the 64 MiB cap, or
fails its handshake — count against runtime health and are exported as
`sigil_pi_runtime_unhealthy_replacements_total`; alert on its rate. The alert policy's
`compiler_replaced_unhealthy` warns on any rise within five minutes, before repeated
replacements exhaust the runtime and readiness fails; `docs/runbooks.md` has the procedure. A
client disconnect does not independently cancel accepted work; this preserves the single
committed session order. The server deadline remains authoritative.

The product runtime client forcibly removes `SIGIL_ALLOW_UNVERIFIED_CERT` from the compiler
child environment. That benchmark-only escape hatch cannot be enabled for product traffic,
even by an inherited operator environment variable.

Python runtime dependencies are declared in `requirements-runtime.lock` (currently standard
library only); the complete test/tool environment is pinned in `requirements-dev.lock`, by
version and by the sha256 of every file PyPI publishes for that version; the lock itself
requires hashes and refuses source builds, so every install of it gets exactly those files or
fails. Every test has a 1200-second limit (`pytest.ini`), and every CI job a
`timeout-minutes`.

## Credential rotation and revocation

Rotation is explicit and restart-bound:

1. Generate a new high-entropy token in the approved secret manager. Add its digest to the
   auth file with an overlapping `not_before_unix` / `expires_unix` window and exactly the
   same tenant, principal, scopes, tools, `secrets` and `net_allowlist` as the old
   credential.
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
tested alert receiver or response runbook. The host runs its audit log strict: a record that
cannot be written fails its turn with `500 audit_write_failed`, counts in
`audit_verification.write_failures_total`, emits `audit_write` at `critical` severity, and
fails readiness until the process is replaced; it raises the `audit_write_failed` alert
(runbook: Audit write failure).

Scheduled turns run on one background thread. An error from the schedule store fails the tick
it happens in, not the thread: the tick is counted in `scheduler.tick_failures_total`, the
next one runs a second later, and a run of failed ticks emits one content-free
`scheduler_tick` event at `critical` severity, then one at `info` severity carrying
`failed_ticks` when a tick completes again. `GET /v1/ready` reports `scheduler: false`, and
the `scheduler_not_running` alert fires, when that thread is not alive or has neither
completed a tick nor recorded a scheduled run for `PI_TURN_LEASE_SECONDS` plus one second;
`docs/runbooks.md` has the procedure. The audit and retention monitors run under the same
rule: an event they cannot write is dropped and their last verdict stands, any other error
fails the monitor closed until its next check completes, and a monitor whose thread has ended
reports itself unhealthy.

The authenticated JSON metrics response includes process peak memory, active thread/open-FD
counts, state/audit/sandbox byte totals, disk capacity, and fixed-bucket turn-latency and
queue-wait histograms when product startup supplies its state root. Those are the host
process's figures. The compiler processes, where guests run, are reported beside them as a
process count and totals of resident memory, threads and open descriptors (`compiler_*`), and
`effective_settings` records the model, the provider's host name, the thinking setting and
every numeric limit and deadline in force, so a drill or load report can state the
configuration it measured. The equivalent
`GET /v1/metrics/prometheus` endpoint uses Prometheus text format. Readiness and both metric
formats identify runtime-process health, SQLite quota integrity, schedule decoding, active
state-root accessibility, signed-audit verification, retention health, and scheduler liveness
separately. A
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

`SIGTERM` and `SIGINT` cause readiness to fail and refuse new work: `POST /v1/chat`, `GET`
and `POST /v1/schedules`, `DELETE /v1/schedules/{name}`, `GET /v1/sessions/{session}/export`
and `DELETE /v1/sessions/{session}` answer `503 service_draining`, and no further scheduled
turn starts. A scheduled turn that was claimed but had not started gives its claim back, is
charged nothing, and runs when the service next starts. The listener, the scheduler, the
retention and audit monitors and the work already in flight are then waited for at once, and
`PI_DRAIN_TIMEOUT_SECONDS` (default `120`) is the budget of the whole drain. Within the same
budget the drain waits for the answers of requests already dispatched to be written. An
answer still unwritten at the deadline (a client reading slowly) does not make the drain
fail: the clean-shutdown marker vouches for state, which is settled. The health, readiness,
version and metrics endpoints keep answering until the listener stops. Work still running at
the deadline makes the shutdown non-clean (`work in flight did not drain within N seconds`),
and the process supervisor must surface it.

A successful drain writes `.product-clean-shutdown.json` in `PI_STATE` and syncs it. Startup
removes the old marker, and syncs the removal, before opening state. An offline backup
refuses to run without it, so a crash or failed drain cannot be mistaken for a consistent
offline recovery point. An online backup does not use the marker.

## Backup and restore

Routine backups are taken online, while the service serves, with no stop. Run the tool on the
service's host as the service's user, with `PI_AUDIT_KEY` injected from the secret manager:

```sh
python3 state_tool.py backup --online \
  --state /var/lib/sigil-pi \
  --output /secure-backups/sigil-pi-state.tar.gz
```

From its own process the tool takes the locks the service takes, one at a time: each
session's `locks/<digest>.lock` while it copies that session's conversation, sandbox and audit
chain, then the schedule store's lock for the store. A session's turns wait only while that
one session is copied, and the manifest records the longest such hold. The tool waits for a
busy lock for at most `--lock-wait-seconds` (default 150; keep it above
`PI_TURN_DEADLINE_SECONDS`), and a lock it cannot take fails the whole backup with exit
status 2 and nothing published: no session is skipped. The quota ledger is captured last,
through SQLite's backup API, and made to agree with what was copied: rows of sessions created
meanwhile are dropped, a session deleted meanwhile is left out whole, turn leases are dropped,
and each remaining row records the bytes it recorded while its session's lock was held. The
copy is then validated and archived exactly as a drained state is.

What an online archive holds: each session as it was at one turn boundary, at some moment
between the start of the backup and the capture of the ledger. It is not one instant across
sessions. The manifest's `created_unix` is the moment the backup began, and backup age is
counted from it. A session created while the backup ran is in the next one. The manifest also
carries `mode` (`online`) and `online`: `sessions`, `sessions_created_meanwhile`,
`sessions_deleted_meanwhile`, `max_session_lock_hold_ms` and `capture_seconds`. Its
`product_version` and `clean_shutdown_unix` are null.

While it runs, the tool keeps a private copy of the state, uncompressed and unencrypted, in a
`0700` directory beside the output (`.<output name>.<8 characters>.online`). The output's
volume needs room for it, must be approved to hold it, and must keep file names apart as the
state's volume does: on a volume that ignores case the backup fails. The copy is removed
however the backup ends, except when the process is killed: the next `backup-cycle --online`
removes what a killed cycle left, and after a killed `backup --online` the operator removes
it.

An offline backup remains the pre-upgrade backup. Send `SIGTERM`, wait for the product process
to exit successfully, and run the same command without `--online`; it refuses a state without
the clean-shutdown marker.

The command verifies every signed audit chain, validates conversation and schedule state,
rejects symlinks/special files, enforces a 10 GiB default aggregate bound, writes a 0600
archive without overwriting an existing path, and records a per-file size and SHA-256
manifest. The archive is written in two passes over the state, in bounded memory however
large the state is: the first hashes every file for the manifest and the second copies the
files into the archive, checking each against the manifest. A state file that changes between
the passes fails the backup (`state changed while the backup was written: <path>`) and nothing
is published. The archive is synced to stable storage before it is given its name, and its
directory after, so the command reports success only for an archive that would survive a
power loss. The quota database is included
because its token and tenant/session usage ledger is durable security state; backup validates
SQLite integrity, schema, nonnegative hashed usage records, and zero active turn leases (an
online backup drops the leases of turns in flight from its copy of the ledger), and captures
committed WAL contents through SQLite's backup API. Only the ledger at the state root is
treated as a database; a sandbox file of the same name is archived as the file it is. The per-session activity field
used by retention is validated and preserved. Lock files remain operational and are
regenerated.

For an operator-approved backup-retention duration, use the managed one-shot cycle, online:

```sh
python3 state_tool.py backup-cycle --online \
  --state /var/lib/sigil-pi \
  --backup-dir /secure-backups/sigil-pi \
  --retention-days <approved-backup-retention-days>
```

The backup directory must be a real `0700` directory on approved encrypted storage. That
storage must support `fsync` on files and directories: where it does not, `backup`,
`backup-cycle` and `restore` fail rather than report an archive or a restored tree they could
not make durable. The
cycle takes an exclusive local lock, creates a `0600` archive whose name binds its creation
time and SHA-256, then validates the digest, schema, manifest, file type, link count, and mode
of every managed archive before deleting the first expired one. Before that deletion it syncs
the new archive and the directory entry that names it; if a sync fails, the cycle exits
nonzero with every older archive in place. It ignores unrelated names,
refuses linked/tampered/malformed managed files, never overwrites an existing backup, and
returns a content-free JSON record of the created and deleted digests. Run it from the
deployment scheduler with `--online`, as the service's user, no more than 15 minutes apart;
alert on any nonzero exit and retain its JSON result, which carries `mode` and the `online`
capture report, in the evidence system. Without `--online` the cycle is the offline one and
needs a clean drain first.

This repository supplies the locked creation/expiry mechanism, not the scheduler, encrypted
volume, or approved duration. Backups are taken online, so the 15-minute interval needs no
stop and takes nothing from the availability objective. A deployment must still demonstrate
that its `backup-cycle --online` schedule keeps backup age within 15 minutes; otherwise this
topology cannot pass GA.

Restore only into a path that does not exist:

```sh
python3 state_tool.py restore \
  --backup /secure-backups/sigil-pi-state.tar.gz \
  --state /var/lib/sigil-pi-restored
```

Restore rejects traversal, links, duplicate/undeclared members, checksum mismatch, unknown
schemas, oversized archives, existing targets, corrupt conversations/schedules, and invalid
audit signatures. It stages and validates the complete tree before atomically installing it.
Each restored file is written in bounded memory and synced as it is written; the staged
tree's directories are synced before the install and the target's parent directory after it.
Restore therefore makes one sync per restored file: measured in a Linux container, 5,000
files of 200 bytes restore in 3.8 s (0.24 s before). If the final directory sync fails,
`restore` exits nonzero although the tree is installed, and `backup` likewise although the
archive is in place: check or remove it before retrying, because neither command overwrites.
These are plain `fsync` calls, as for conversations and audit records: on macOS they do not
flush the drive's own cache.
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
with the old release's own `state_tool.py` — and uploads `recovery-drill.json`, the fixture's
evidence and the log of every service it started as a run artifact, whether the drill passed
or failed. A drill that stops still writes its report, which names the phase and the reason
and can never qualify. The pinned Z3 shared library is installed into the loader's default path first, because
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

Every phase reads one session and runs a turn in it. By default that is the session
`scripts/drill_fixture.py` commits (tenant `drill-fixture-tenant`, session
`drill-fixture-session`). Against a production-equivalent backup, name a tenant and a session
the backup holds with `--tenant` and `--session`. The drill works on a restored copy, and its
turns are answered by a stand-in provider on loopback: no request reaches the model provider
and nothing is written to the production state. Choose a session well under
`PI_MAX_HISTORY_BYTES` (256 KiB as shipped) and `PI_MAX_EXPORT_BYTES`: a release drops the
oldest turns of a conversation that outgrows the first, which the drill reports as committed
messages lost, and refuses an export larger than the second.

The work directory and output must not already exist. The harness rejects unsafe archive
members, verifies every inner release-manifest checksum, installs both releases separately,
and atomically switches a `current` link. It then starts the real service in this order:
clean old install, restored old release, candidate upgrade, and old-release rollback. In each
phase the process must become ready, answer `/v1/version` and `/v1/metrics`, serve the
session's conversation and audit chain exactly as they were committed (and then as the phases
before it left them), run one turn in that session through the embedded compiler, show that
turn committed after what was there, and shut down cleanly. The candidate therefore continues
a conversation the old release wrote, and the rollback continues one the candidate wrote. Any
other answer stops the drill at that phase. Readiness, version and metrics are asked with a
credential of the drill's own tenant (`ops:read`); the session is read and continued with one
in the session's tenant (`chat`, `sessions:read`), which is charged three requests per phase.
The harness then backs up the final state and compares it with the input backup: the session
the phases continued must still open with its committed messages and audit records and must
hold the three turns run on the restored state, and every other committed session, sandbox
and signed-audit file must be byte-identical. Each phase's service log is kept under
`<work-dir>/logs`.

After the four phases the drill takes and restores an online backup, the path the 15-minute
backup objective rests on. The candidate's own `state_tool.py` restores the state backed up
after the rollback into a new directory (`<work-dir>/online-state`), and the candidate starts
on it, reads the session and runs a turn, as in every phase. While the candidate then serves
turns in the session, its `state_tool.py backup --online` copies the live state from another
process, as the service's own user and with the default `--lock-wait-seconds` (150, above the
shipped turn deadline), into `<work-dir>/online-backup.tar.gz`. The first of those turns is
held in flight at the stand-in provider until the backup has started, and further turns run,
one at a time, until it ends: at least two and at most 20, which with the phases' own requests
stays inside the shipped 60 requests a minute. Every one of them must be answered and
committed in order, and the archive must hold the session exactly as one of them left it, the
turn that was in flight when the backup started included. The candidate's state tool then
restores the archive into another new directory (`<work-dir>/online-restored-state`), and the
candidate starts on it, reads the session exactly as the archive holds it, and runs a turn.
These two starts log to `<work-dir>/logs/5-backup_online_new-service.log` and
`6-restore_online_new-service.log`.

The command fails unless the artifacts are distinct versions, the input backup is at most 15
minutes old and contains at least one committed session, RTO is at most four hours, rollback
is at most 15 minutes, all customer files survive, the session held a committed conversation
and audit chain, the candidate reports its effective settings in `/v1/metrics` with the three
settings the drill supplies in force, the candidate's online backup succeeded, has mode
`online` and records its longest session lock hold, the turns run while it was taken were all
answered and committed, the archive holds the session at one of their boundaries and every
committed file of the state it was taken from, and the candidate continued the session
restored from it. Restore and rollback are timed to the first successful turn, not to
readiness. An online backup whose restore began more than `--max-backup-age-seconds` after it
was taken cannot qualify, and its restore is timed, like the others, to the first successful
turn. A candidate whose state tool has no `--online` (every release before audit 2026-11-10)
fails the drill at `backup_online_new`. The command exits 2 when a phase fails and 1 when the
run completes but cannot qualify. Its 0600 JSON report binds every phase to the old or new
archive SHA-256. `--mechanics-only` permits a deliberately non-qualifying
same-artifact rehearsal but preserves the reasons in the report; it must never be used as GA
evidence. The readiness validator independently rechecks the raw report rather than trusting
its top-level pass flag.

Run both drill workflows from `main` (`gh workflow run <workflow> --ref main ...`). Each run
signs its report in an `attest` job and then verifies the signature as the readiness gate
will; a run from another branch is signed but fails that last step, because the gate accepts
only reports signed from `main`. A drill that failed keeps its report in the run's artifact,
unsigned.

## Cognitive memory

Memory remains outside the v1 production support set until its export/deletion and backup
lifecycle are implemented. Product startup therefore rejects `PI_MEMORY_SIDECAR` rather
than silently offering a state surface that cannot satisfy the product data contract.

## Current operational blockers

Before this guide can become a production runbook, the project still needs an immutable
runner/base-image pin, release-tag protection, independent security review and scans,
encrypted automated backup scheduling and exercised deletion evidence, final approval of
active and backup retention durations, a deployed metrics collector with routed and tested
alerts, load evidence, and exercised upgrade/rollback/recovery/incident procedures. Signed tag
evidence exists: v0.4.0's provenance and SBOM attestations were verified independently
(`docs/evidence/release-0.4.0.md` in the repository). The authoritative state and required
evidence are maintained in `docs/product-readiness.md` in the repository at this release's
version tag.
