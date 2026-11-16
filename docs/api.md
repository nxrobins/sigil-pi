# sigil-pi product API

API version: `v1`. Implementation version: the exact value returned by
`GET /v1/version` and stored in `VERSION`.

The endpoints in this document are the product contract. The historical `/chat` endpoint
in `agent.py` is the research interface: at most one shared bearer token (`PI_AUTH_TOKEN`,
M18), with no tenants, scopes, or quotas. It must not be exposed as a product endpoint.

## Authentication and tenancy

Every request uses `Authorization: Bearer <token>`. Missing and invalid credentials receive
the same stable authentication challenge without revealing configured principals.

`PI_AUTH_FILE` points to a JSON object containing a `tokens` list. Each production entry
contains only the SHA-256 digest of a high-entropy bearer token, a principal name, its tenant,
scopes, exact tools, optionally the names of the tenant secrets its tools may use
(`"secrets"`, each matching `[a-z0-9_]{1,64}`) and the hosts its `fetch` may reach
(`"net_allowlist"`, lowercase host names or `*.` and a name; `*` alone is refused), and
integer `not_before_unix` / `expires_unix` bounds. Secret values never go in this file: they
are the tenant's, under `PI_TENANT_SECRETS_DIR` (docs/operations.md). See
`config/auth.example.json`. Raw tokens belong in the caller's secret manager and must never
be committed or written to this file. Expired, future, and unknown credentials all receive
the same `invalid_credential` response.

Generate a digest without writing the token to shell history by reading it from a protected
file or secret-manager process and passing its bytes to a SHA-256 tool. The two example
digests are inert placeholders; no corresponding credential is shipped.

The authenticated tenant—not request JSON—selects the tenant namespace. An external session
name is hashed together with that tenant identity before it reaches conversation storage,
the sandbox, audit records, memory, or scheduling. The same external session name in two
tenants therefore identifies different internal sessions.

V1 rotation uses overlapping credentials with the same tenant, principal, scopes, tools,
secrets and net_allowlist.
Conflicting policy for one principal fails configuration. The operator atomically installs
the overlap policy and performs a rolling worker restart, migrates clients, then removes the
old digest with another rolling restart before its expiry. There is intentionally no live
auth-file reload; restart is the documented policy boundary.

### Scopes

| Scope | Permitted operations |
|---|---|
| `chat` | `POST /v1/chat`, and each scheduled turn: a schedule's creator must hold it |
| `ops:read` | health, readiness, version, and process metrics |
| `schedules:read` | list the authenticated tenant's schedules |
| `schedules:write` | create, update, and delete the authenticated tenant's schedules; creating or updating one also needs `chat` |
| `sessions:read` | export one of the authenticated tenant's sessions |
| `sessions:delete` | delete one of the authenticated tenant's sessions |

A credential without a scope its route requires is answered `403 permission_denied`, and the
message names each scope it lacks, as in `credential lacks required permission: chat`.

Schedule read and write are deliberately separate. A scheduled turn is the model acting for its
creator, so it needs the authority a chat turn needs: creating or replacing a schedule needs
`chat` as well as `schedules:write`, and the scheduler checks the creator's current policy
before each run (audit F-28). Revoking the creator, or its `schedules:write` or `chat` scope,
prevents later execution: the run starts no turn and makes no model call, is recorded with
`last_status` `policy_revoked` and counted in the metrics' `scheduled_runs`, and the schedule
stays listed for its owner to fix or delete; deleting needs `schedules:write` alone. Changing
the creator's tool list changes what future executions may invoke.

## Common response rules

- JSON responses use `Content-Type: application/json`.
- `Cache-Control: no-store` prevents authenticated data from being cached.
- `X-Request-ID` is returned on every response, the listener's own refusals included. A valid
  caller-supplied ID is preserved; otherwise the service creates one.
- The `Server` header is `sigil-pi`. It names no interpreter, library or version.
- Structured request logs contain the request ID, route, outcome, stable failure
  classification, principal, and a tenant digest. They never contain bearer tokens, session
  names, messages, or replies.
- Product errors have one stable shape:

```json
{
  "error": {
    "code": "permission_denied",
    "message": "credential lacks required permission: chat"
  },
  "request_id": "request-1234"
}
```

Unexpected exceptions and agent diagnostics are not returned to callers.

Every error has that shape, `Content-Type: application/json`, `Cache-Control: no-store` and
the status's standard reason phrase, including the errors the listener writes before a request
reaches a route, and none repeats anything from the request but a valid `X-Request-ID`. The
listener's own errors are 400 `malformed_request` (a request line it cannot read), 400
`invalid_content_length` (a `Content-Length` that is not one run of digits, two that differ, a
body that ends or stops before it, or any `Transfer-Encoding`: request bodies are framed by
`Content-Length` alone), 414 `request_line_too_long`, 431 `request_head_too_large`, 501
`method_not_supported` and 505 `http_version_not_supported`. An answer to `HEAD` carries its
error's headers and no body. A `GET` request line without a version is read and answered
as HTTP/1.0, headers included, on every supported Python; any other method without a
version gets 400 `malformed_request`. A request head that ends before the line end or blank
line that completes it is not processed and gets no answer.

The request log has one `http_request` event for each request that reaches a route or is
refused for its framing (`invalid_content_length`), and a request is answered even when that
line cannot be written. When the client goes away before its answer is written, a second
event, `http_response_failed`, carries only that request's ID, its status and the class
`client_disconnected`. A connection that ends before a request is read (a TLS handshake that
fails or times out, a head that times out or is cut off) and the listener's
`malformed_request`, 414, 431, 501 and 505 are not logged and are not in
`sigil_pi_http_requests_total`; `sigil_pi_http_errors_total` counts them by class:
`tls_handshake_failed`, `request_timeout`, `request_aborted`, `client_disconnected` and the
error codes above.

While the service drains for shutdown, `POST /v1/chat`, the schedule routes and the session
export and delete routes answer `503 service_draining`; a request already being served
finishes. The operational endpoints keep answering.

## `POST /v1/chat`

Required scope: `chat`.

Request fields are exact; unknown fields are rejected:

```json
{"session":"project-1","message":"Summarize the files."}
```

- `session`: 1–128 ASCII letters, digits, `.`, `_`, or `-`; must start with a letter or
  digit.
- `message`: non-empty UTF-8 string, at most 256 KiB.
- Whole request body: at most 1 MiB.

Success:

```json
{
  "request_id": "request-1234",
  "session": "project-1",
  "reply": "...",
  "usage": {"input_tokens": 123, "output_tokens": 45}
}
```

The service enforces per-tenant request-rate and active-turn limits. A rejected request is
`429` with `Retry-After`. Model output naming a tool outside the principal's configured tool
set becomes a denied tool result; hiding a tool from the prompt is not the authorization
control.

A model reply is used only when the provider reports that it finished: a `stop_reason` of
`end_turn`, `tool_use` or `stop_sequence`. A reply cut off at the output cap
(`PI_MAX_TOKENS`) or by the model's context window, a refusal, a paused turn, or any other
stop reason fails the turn with `502 agent_failure`. No tool call in that reply runs and the
reply is not added to the conversation. The user message, the tool steps the turn completed
before that reply, and the tokens the provider reported for it are kept and metered, as for
any failed turn, so the retry rules below apply.

Interactive and scheduled turns also share durable daily-token, aggregate conversation/
sandbox-storage, and signed-audit quotas across workers. The host reserves capacity before
each model attempt, including retries, and settles validated provider usage afterward.
The independent turn ceiling returns `429 turn_token_budget_exhausted` before a generation
that cannot fit; completed tool results remain in session history for a follow-up. Daily
exhaustion is `429 token_quota_exceeded` with `Retry-After`. Uncertain attempts remain charged.
A provider allowance overrun returns `502 provider_token_budget_exceeded` before tools run;
its actual usage remains charged. See operations.md for counting headroom and receipt rules.
Storage and audit exhaustion are
`507 storage_quota_exceeded` and `507 audit_quota_exceeded`. Session deletion credits its
storage and audit ledger entries. Limits and reservations are operator-configured and are
part of the published deployment contract.

Every turn has a hard operator-configured wall-clock deadline covering session and forge
queueing plus execution. Expiry returns `504` with `turn_deadline_exceeded`, cancels the forge
by killing its compiler, and persists the bounded partial transcript. The killed compiler is
retired and replaced on the next forge, so readiness is unaffected: it fails only when
repeated replacements cannot produce a working compiler. Disconnecting the HTTP client is not
a cancellation request; accepted work continues until completion or the server deadline. V1
has no separate cancellation endpoint.

A turn whose audit record cannot be written fails with `500 audit_write_failed` and no reply:
the service never answers with work it could not record. The guests that ran before the
failure are not undone, and the transcript keeps the user message and the steps completed
before it, as for any other failed turn; the tenant is charged for those steps. A scheduled
run that fails this way lists `last_status` `audit_write_failed`.

### Retries, idempotency, and cancellation

V1 does not accept or interpret an `Idempotency-Key`. Mutation requests (`POST` and `DELETE`)
are therefore not replay-safe after an ambiguous transport outcome: an accepted chat turn may
have committed a bounded partial or complete transcript even when its response was lost. A
client must not automatically replay a chat, schedule mutation, or deletion merely because it
received a transport error, `500`, `502`, or `504`; it should first reconcile state with the session
export or schedule-list operation, or ask the user before creating another turn. Reusing the
same request ID correlates logs but does not deduplicate work.

Authenticated `GET` requests are read-only and may be retried. A `429` response is rejected
before the requested operation begins and may be retried after its `Retry-After` delay. Other
stable 4xx responses require the caller or its authorization/configuration to change. The host
retries only the model-provider call, only for transient `429`/5xx failures, and only within
the configured bounded retry budget; tool mutations and whole chat turns are never
automatically replayed.

Cancellation is deadline-based in v1. The pinned runtime protocol has no cancel operation, so
the server terminates an over-running forge by replacing its compiler; other tenants' turns are
unaffected. A client disconnect does not cancel accepted work, and
there is no cancellation endpoint or cancellation token. The server's hard turn deadline is
authoritative and returns the stable `turn_deadline_exceeded` outcome described above.

## Schedules

### `POST /v1/schedules`

Required scopes: `schedules:write` and `chat`, because a schedule has the model act for its
creator. A credential that lacks either is answered `403 permission_denied` naming what it
lacks, and nothing is stored or replaced.

```json
{
  "name": "daily-report",
  "session": "project-1",
  "message": "Prepare the daily report.",
  "every_ms": 86400000
}
```

All four fields are required and unknown fields are rejected. Name, session, and message use
the chat limits; `every_ms` must be an integer of at least `60000` (one minute); a smaller
value is refused with `400 invalid_interval`. A schedule that an earlier release stored with a
smaller interval is listed as stored and runs no more often than once a minute. Replacing an
existing schedule keeps its last-run mark so editing it does not cause an immediate duplicate
run. Each tenant has a configured schedule-count quota (`PI_MAX_SCHEDULES_PER_TENANT`) and a
schedule-byte quota (`PI_MAX_SCHEDULE_BYTES_PER_TENANT`, default 8 MiB): the most bytes its
schedules' messages may take in the schedule store, counted as JSON text, where a quote, a
backslash or a control character takes two to six bytes. A schedule past either quota is
refused with `409 schedule_quota_exceeded` (message `tenant schedule quota reached` or
`tenant schedule byte quota reached`). A tenant already over the byte quota, because it was
lowered, keeps its schedules and may replace one with a message that takes no more bytes than
the one it replaces.

### `GET /v1/schedules`

Required scope: `schedules:read`. Returns only the authenticated tenant's schedules,
including their last-run time and stable last status (`ok`, `failed`, `deadline_exceeded`,
`quota_exceeded`, `policy_revoked`, `policy_invalid`, or `audit_write_failed`).
`policy_revoked` means the creator's policy, when the entry fell due, did not allow the run:
the credential was removed or had expired, or lacked `schedules:write` or `chat`. No turn
started. A scheduled run is at-least-once. If
the host cannot record that a run finished, because the schedule store could not be written
or the process ended mid-turn, the schedule's last-run time is unchanged and the turn runs
again once `PI_TURN_LEASE_SECONDS` has passed.

### `DELETE /v1/schedules/{name}`

Required scope: `schedules:write`. Deletes only the authenticated tenant's matching schedule.

## Operational endpoints

All require `ops:read`.

- `GET /v1/health`: process liveness. It does not assert dependency readiness.
- `GET /v1/ready`: returns `200 ready` only when every configured dependency is ready and the
  service is not draining; otherwise `503 not_ready`. Its content-free `dependencies` object
  names the fixed product components (`runtime`, `quota_store`, `schedule_store`,
  `state_storage`, `audit_verification`, `retention`, and `scheduler`) as booleans, and
  `draining` states whether graceful shutdown caused the failure. Probe exceptions become
  `false` and never return their diagnostics. `scheduler` is the liveness of the thread that
  runs scheduled turns: `true` while that thread is alive and has completed a tick or recorded
  a scheduled run within `PI_TURN_LEASE_SECONDS` plus the one-second tick interval, and `false`
  before the scheduler starts and after it stops. `audit_verification` and `retention` are
  also `false` once that monitor's thread has ended without being stopped, and after a check
  that raised an error the monitor does not handle, until a check completes again.
  `audit_verification` is also `false` from the first audit record the service could not
  write until the process is replaced.
- `GET /v1/version`: API and implementation versions.
- `GET /v1/metrics`: bounded, low-cardinality process metrics without tenant or principal
  labels. It currently reports request status counts, stable error classes, active/total/error
  turns, cumulative turn-latency and queue-wait histograms, aggregate turn/queue time,
  input/output tokens, tool calls, model retries, uptime, aggregate dependency readiness,
  per-component dependency readiness, and graceful-drain state. The
  product entry point also exposes process peak memory, active
  threads, open file descriptors where the OS supports them, state/audit/sandbox bytes, and
  disk capacity. Those process figures describe the host process. The compilers, where guests
  run, are reported separately under `resources`: `compiler_process_count` and, summed over
  every live compiler process, `compiler_resident_memory_bytes`, `compiler_thread_count` and
  `compiler_open_fd_count`, read from `/proc` on Linux and through libproc on macOS; a total
  that cannot be read for every compiler is `null`, never a partial sum. `runtime_generation`
  counts the compilers the process has started, and `runtime_unhealthy_replacements_total`
  the replacements not explained by a caller's expired turn budget. An `effective_settings`
  object reports the configuration in force: `model`, `endpoint_host` (the provider
  endpoint's host name only), `thinking` (the thinking setting sent with every model request)
  and every numeric limit, deadline and quota under its `PI_*` variable's name without the
  prefix, lowercased (for example `turn_deadline_seconds`, `tokens_per_day`). It never
  contains a secret, a credential, a path, the fetch allowlist or the system prompt. Chat
  responses expose per-turn queue duration in `Server-Timing` and integer
  tool/retry counts in `X-Sigil-Tool-Calls` and `X-Sigil-Retries`.
  Process-level quota totals cover daily tokens, storage, audit bytes, active leases, and
  reservations without tenant labels.
  It also reports automatic signed-audit verification checks, failures, last status, and
  chain/record counts, and the audit records that could not be written
  (`write_failures_total`, with `writes_healthy`). Active-state retention reports checks,
  failures, last status, deleted session/schedule totals, and the configured duration; a
  failed sweep also makes readiness fail. A `scheduler` object reports the scheduler's
  liveness: `healthy` (what readiness reports), `thread_alive`, `ticks_total` and
  `tick_failures_total` (ticks completed, and ticks in which the schedule store failed),
  `last_tick_unix` and `last_tick_age_seconds` (the last completed tick),
  `last_progress_age_seconds` (seconds since the scheduler last completed a tick or recorded
  a scheduled run; `healthy` is judged by this age, because one tick lasts as long as every
  turn due in it) and `stall_after_seconds` (the bound on that age). The time and the two
  ages are `null` until there is something to measure. A `scheduled_runs` object counts the
  scheduled runs this process has recorded, by stable last status, each status listed under
  `GET /v1/schedules` from zero. A run is counted when it is recorded, so one whose record
  could not be written is counted once, when it runs again; a turn held back by a drain is
  not a run.
- `GET /v1/metrics/prometheus`: the same bounded operational snapshot in Prometheus 0.0.4
  text format (`text/plain; version=0.0.4; charset=utf-8`). Turn duration and queue wait are
  cumulative millisecond histograms with fixed buckets from 10 ms through 120 seconds and an
  explicit `+Inf` bucket. The only exporter labels are API/build version, HTTP method,
  normalized product route, HTTP status, stable failure class, fixed dependency name,
  scheduled-run status, and histogram bound. Customer tenant, principal, session, message, reply, file, and model
  values are never labels. The compiler figures are label-free gauges
  (`sigil_pi_compiler_process_count`, `sigil_pi_compiler_resident_memory_bytes`,
  `sigil_pi_compiler_thread_count`, `sigil_pi_compiler_open_fd_count`), and each numeric
  effective setting is a label-free gauge `sigil_pi_setting_<name>`; the model, the endpoint
  host and the thinking setting are not exported. The scheduler's liveness is exported as
  label-free gauges named `sigil_pi_scheduler_` plus the JSON field, from
  `sigil_pi_scheduler_healthy` to `sigil_pi_scheduler_stall_after_seconds`; a field that is
  `null` in JSON is omitted. `scheduler` joins the fixed values of the `dependency` label.
  Recorded scheduled runs are the counter `sigil_pi_scheduled_runs_total`, labelled by
  `status`, one of the fixed statuses of `scheduled_runs`.

Both metrics forms are process-local. The supported launch topology remains one product
worker until an approved aggregation design and a new capacity qualification exist. The
packaged Grafana dashboard and alert contract are deployment inputs, not proof that a collector,
dashboard, or alert route was deployed or exercised; those evidence gaps remain recorded in
`docs/product-readiness.md` in the repository at this release's version tag.

## Session data lifecycle

Memory is disabled in the product entry point until it participates in this lifecycle.

### `GET /v1/sessions/{session}/export`

Required scope: `sessions:read`. Returns a schema-versioned bundle containing the selected
tenant-scoped conversation, sandbox files (base64 encoded), schedules, and signed audit
records. The export is serialized against chat, scheduled turns, and deletion for that
session. Symlinks are rejected rather than followed. `PI_MAX_EXPORT_BYTES` bounds the bundle
(default 10 MiB); larger exports fail explicitly until a streaming export is implemented.

### `DELETE /v1/sessions/{session}`

Required scope: `sessions:delete`. Removes the conversation, sandbox, schedules, and audit
chain belonging to that tenant-scoped session. It also removes any scratch file that an
interrupted save of the conversation left beside it, and syncs each removal before the
session's ledger row is removed. A session of which only such a scratch file exists is still
`404 session_not_found`; the scratch file is removed all the same. It is serialized against chat and scheduled
turns. The scheduler re-checks a due entry after acquiring the same lock, so a stale due
snapshot cannot resurrect a session after deletion.

Deletion from backups remains governed by the not-yet-complete backup retention design; this
endpoint proves active-storage deletion only.

### Automatic active-storage retention

The product entry point deletes inactive conversations, regular sandbox files, schedules,
and signed audit chains after `PI_RETENTION_DAYS` (provisional default: 90 days). It performs
an initial sweep before accepting traffic and repeats it every
`PI_RETENTION_INTERVAL_SECONDS` (default: 3600), so ordinary expiry completes within one
configured sweep interval. Accepted interactive and scheduled turns refresh session
activity; schedule creation/update/execution refreshes schedule activity and keeps its
associated session active. Export and list operations do not extend retention.

Each sweep verifies every signed audit chain before deleting anything. Session cleanup uses
the same cross-worker lock as chat, scheduling, export, and explicit deletion, re-checks the
durable activity record after acquiring that lock, removes the registry row last, and is
safe to retry after interruption. A sweep failure emits a content-free critical event and
makes readiness fail. A request racing after the final eligibility check may start a new
empty retained session after the expired history is removed; reaching the retention boundary
does not guarantee revival of expired history.

This policy covers active product storage only. Backup expiry/deletion, encryption-at-rest
evidence, and final approval of the duration remain release blockers.

## Transport and compatibility

Binding to a non-loopback or wildcard address without a configured TLS server context is a
startup error. Plain HTTP is supported only on loopback, including when a local reverse proxy
terminates TLS.

A request carries its credential in its headers and is authenticated before any of its body
is read: a request without a valid credential is answered `401` without its body being read.
A body is framed by one `Content-Length` of decimal digits, at most 1 MiB; an invalid,
conflicting or oversized length, a body that ends before it, or one that has not arrived
within the request deadline is answered `400 invalid_content_length`. Header lines may total
64 KiB, and a longer head is answered `431`. The server handles a bounded number of
connections at once, and a connection beyond them waits until one ends, so a client should
set its own connect and response timeouts.

Within `v1`, fields may be added to success responses but existing fields and error meanings
will not be removed or changed. Breaking request or authorization changes require a new API
version. Requiring `chat` to create a schedule and to run a scheduled turn (audit F-28) is a
deliberate narrowing of authorization made within `v1` to close a privilege gap; the
changelog records it. The candidate runtime/OS/model/topology boundary is listed in
`docs/support-matrix.md`; it remains a launch target until the protected release and pilot
validate it.

Export, backup, and on-disk compatibility are separate from the HTTP API version and follow
the fail-closed schema policy in `docs/state-compatibility.md`.
