# sigil-pi product API

API version: `v1`. Implementation version: the exact value returned by
`GET /v1/version` and stored in `VERSION`.

The endpoints in this document are the product contract. The historical `/chat` endpoint
in `agent.py` is an unauthenticated loopback research interface and must not be exposed as
a product endpoint.

## Authentication and tenancy

Every request uses `Authorization: Bearer <token>`. Missing and invalid credentials receive
the same stable authentication challenge without revealing configured principals.

`PI_AUTH_FILE` points to a JSON object containing a `tokens` list. Each production entry
contains only the SHA-256 digest of a high-entropy bearer token, a principal name, its tenant,
scopes, exact tools, and integer `not_before_unix` / `expires_unix` bounds. See
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

V1 rotation uses overlapping credentials with the same tenant, principal, scopes, and tools.
Conflicting policy for one principal fails configuration. The operator atomically installs
the overlap policy and performs a rolling worker restart, migrates clients, then removes the
old digest with another rolling restart before its expiry. There is intentionally no live
auth-file reload; restart is the documented policy boundary.

### Scopes

| Scope | Permitted operations |
|---|---|
| `chat` | `POST /v1/chat` |
| `ops:read` | health, readiness, version, and process metrics |
| `schedules:read` | list the authenticated tenant's schedules |
| `schedules:write` | create, update, and delete the authenticated tenant's schedules |
| `sessions:read` | export one of the authenticated tenant's sessions |
| `sessions:delete` | delete one of the authenticated tenant's sessions |

Schedule read and write are deliberately separate. A scheduled turn runs under its creator's
current policy. Revoking the creator or its `schedules:write` scope prevents later execution;
changing its tool list changes what future executions may invoke.

## Common response rules

- JSON responses use `Content-Type: application/json`.
- `Cache-Control: no-store` prevents authenticated data from being cached.
- `X-Request-ID` is returned on product-dispatched responses. A valid caller-supplied ID is
  preserved; otherwise the service creates one.
- Structured request logs contain the request ID, route, outcome, stable failure
  classification, principal, and a tenant digest. They never contain bearer tokens, session
  names, messages, or replies.
- Product errors have one stable shape:

```json
{
  "error": {
    "code": "permission_denied",
    "message": "credential lacks required permission"
  },
  "request_id": "request-1234"
}
```

Unexpected exceptions and agent diagnostics are not returned to callers.

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

Interactive and scheduled turns also share durable daily-token, aggregate conversation/
sandbox-storage, and signed-audit quotas across workers. The host reserves capacity before
work and settles provider-reported token usage plus exact file sizes afterward. Daily-token
exhaustion is `429 token_quota_exceeded` with `Retry-After`; storage and audit exhaustion are
`507 storage_quota_exceeded` and `507 audit_quota_exceeded`. Session deletion credits its
storage and audit ledger entries. Limits and reservations are operator-configured and are
part of the published deployment contract.

Every turn has a hard operator-configured wall-clock deadline covering session and forge
queueing plus execution. Expiry returns `504` with `turn_deadline_exceeded`, cancels the forge,
persists the bounded partial transcript, and drops readiness until the compiler process is
restarted. Disconnecting the HTTP client is not a cancellation request; accepted work
continues until completion or the server deadline. V1 has no separate cancellation endpoint.

### Retries, idempotency, and cancellation

V1 does not accept or interpret an `Idempotency-Key`. Mutation requests (`POST` and `DELETE`)
are therefore not replay-safe after an ambiguous transport outcome: an accepted chat turn may
have committed a bounded partial or complete transcript even when its response was lost. A
client must not automatically replay a chat, schedule mutation, or deletion merely because it
received a transport error, `502`, or `504`; it should first reconcile state with the session
export or schedule-list operation, or ask the user before creating another turn. Reusing the
same request ID correlates logs but does not deduplicate work.

Authenticated `GET` requests are read-only and may be retried. A `429` response is rejected
before the requested operation begins and may be retried after its `Retry-After` delay. Other
stable 4xx responses require the caller or its authorization/configuration to change. The host
retries only the model-provider call, only for transient `429`/5xx failures, and only within
the configured bounded retry budget; tool mutations and whole chat turns are never
automatically replayed.

Cancellation is deadline-based in v1. A client disconnect does not cancel accepted work, and
there is no cancellation endpoint or cancellation token. The server's hard turn deadline is
authoritative and returns the stable `turn_deadline_exceeded` outcome described above.

## Schedules

### `POST /v1/schedules`

Required scope: `schedules:write`.

```json
{
  "name": "daily-report",
  "session": "project-1",
  "message": "Prepare the daily report.",
  "every_ms": 86400000
}
```

All four fields are required and unknown fields are rejected. Name, session, and message use
the chat limits; `every_ms` must be a positive integer. Replacing an existing schedule keeps
its last-run mark so editing it does not cause an immediate duplicate run. Each tenant has a
configured schedule-count quota.

### `GET /v1/schedules`

Required scope: `schedules:read`. Returns only the authenticated tenant's schedules,
including their last-run time and stable last status (`ok`, `failed`, `deadline_exceeded`,
`quota_exceeded`, `policy_revoked`, or `policy_invalid`).

### `DELETE /v1/schedules/{name}`

Required scope: `schedules:write`. Deletes only the authenticated tenant's matching schedule.

## Operational endpoints

All require `ops:read`.

- `GET /v1/health`: process liveness. It does not assert dependency readiness.
- `GET /v1/ready`: returns `200 ready` only when every configured dependency is ready and the
  service is not draining; otherwise `503 not_ready`. Its content-free `dependencies` object
  names the fixed product components (`runtime`, `quota_store`, `schedule_store`,
  `state_storage`, `audit_verification`, and `retention`) as booleans, and `draining` states
  whether graceful shutdown caused the failure. Probe exceptions become `false` and never
  return their diagnostics.
- `GET /v1/version`: API and implementation versions.
- `GET /v1/metrics`: bounded, low-cardinality process metrics without tenant or principal
  labels. It currently reports request status counts, stable error classes, active/total/error
  turns, cumulative turn-latency and queue-wait histograms, aggregate turn/queue time,
  input/output tokens, tool calls, model retries, uptime, aggregate dependency readiness,
  per-component dependency readiness, and graceful-drain state. The
  product entry point also exposes process peak memory, active
  threads, open file descriptors where the OS supports them, state/audit/sandbox bytes, and
  disk capacity. Chat responses expose per-turn queue duration in `Server-Timing` and integer
  tool/retry counts in `X-Sigil-Tool-Calls` and `X-Sigil-Retries`.
  Process-level quota totals cover daily tokens, storage, audit bytes, active leases, and
  reservations without tenant labels.
  It also reports automatic signed-audit verification checks, failures, last status, and
  chain/record counts. Active-state retention reports checks, failures, last status, deleted
  session/schedule totals, and the configured duration; a failed sweep also makes readiness
  fail.
- `GET /v1/metrics/prometheus`: the same bounded operational snapshot in Prometheus 0.0.4
  text format (`text/plain; version=0.0.4; charset=utf-8`). Turn duration and queue wait are
  cumulative millisecond histograms with fixed buckets from 10 ms through 120 seconds and an
  explicit `+Inf` bucket. The only exporter labels are API/build version, HTTP method,
  normalized product route, HTTP status, stable failure class, fixed dependency name, and
  histogram bound. Customer tenant, principal, session, message, reply, file, and model
  values are never labels.

Both metrics forms are process-local. The supported launch topology remains one product
worker until an approved aggregation design and a new capacity qualification exist. The
packaged Grafana dashboard and alert contract are deployment inputs, not proof that a collector,
dashboard, or alert route was deployed or exercised; those evidence gaps remain recorded in
`docs/product-readiness.md`.

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
chain belonging to that tenant-scoped session. It is serialized against chat and scheduled
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

Within `v1`, fields may be added to success responses but existing fields and error meanings
will not be removed or changed. Breaking request or authorization changes require a new API
version. The candidate runtime/OS/model/topology boundary is listed in
`docs/support-matrix.md`; it remains a launch target until the protected release and pilot
validate it.

Export, backup, and on-disk compatibility are separate from the HTTP API version and follow
the fail-closed schema policy in `docs/state-compatibility.md`.
