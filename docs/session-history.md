# Retained conversation history

Status: **DEVELOPMENT INTEGRATION — focused regression and full local source CI passed;
candidate/pilot qualification remains open**.
This is one part of [M1/M4](mvp-goal.md), not a complete transcript archive or
qualification of the browser, conversation discovery, export or deletion.

The integrated 136-case history/API/automatic regression passed, followed by the
complete 2,139-case source-tree gate at 18:29 UTC on 2026-09-08. Its unchanged-source
fingerprint, exact pinned runtime, coverage and limitations are recorded in the
[acceptance checkpoint](mvp-acceptance.md#complete-historyapi-source-gate-passed--2026-09-08).
This is local macOS source evidence, not a Linux release candidate or an MVP gate pass.

## Request and authority

`GET /v1/sessions/{session}/messages`

The request requires a currently active credential with `sessions:read`. SIGIL
selects that credential's tenant state namespace and the requested session key.
The native host supplies the actual scoped storage observation; the fixed grantless
[SIGIL history function](../app/pi/history.sigil) projects it into the response.
[SIGIL route logic](../app/pi/history_api.sigil) interprets that result. Neither JSON body fields nor
query values can select another namespace, supply observations, or confer authority.
Authorization is checked before reporting query errors. GET body bytes are ignored
within the existing transport/body bound, just as for operation lookup.

Session names use the existing submission identifier grammar. Query values are
canonical decimal ASCII, with no percent escapes, signs, duplicate or unknown keys,
empty values, leading zeroes or empty query string.

| Parameter | Contract |
|---|---|
| `limit` | Optional integer 1–50; default 20 |
| `revision` | Optional positive signed-64-bit record revision, transported as exact decimal bytes |
| `offset` | Optional integer 0–10000; default 0; explicitly providing it requires `revision` |

Conversation history is **shared by authorized principals within the same tenant**,
consistent with existing session identity/export semantics. Operation lookup and
cancellation additionally require the accepting principal; those owner checks do
not make the conversation principal-private. The final M0 sharing policy still
requires product-owner approval before qualification.

## Paging a changing conversation

Begin without a revision. If `next_offset` is not null, send it with the returned
`state_revision` on the next request:

```http
GET /v1/sessions/project-notes/messages?limit=20
GET /v1/sessions/project-notes/messages?revision=7&offset=20&limit=20
```

The second line is an example only: use the actual returned revision and offset.
The response represents one committed conversation revision, not a snapshot retained
indefinitely on the server. A concurrent turn can change that record. In that case,
`409 history_changed` means restart from the first page; do not combine pages from
different revisions or automatically resubmit the original task.

Revisions are strings in responses so JavaScript cannot round them. Message indices
are stable only within that revision, not permanent message identifiers. Paging
coordinates are neither credentials nor signed authority: changing session or
credential performs that request's ordinary scoped read again.

## Response meaning

The response contains `session`, latest `operation`, string `state_revision`,
`state_phase`, `history_kind: "retained_context"`, `offset`, `messages` and nullable
`next_offset`.

Each message has an integer `index`, a `user`, `assistant` or `tool` role, and a
typed `content` array:

| Content type | Public fields and meaning |
|---|---|
| `text` | `text`: retained user/assistant text |
| `tool_call` | `call_id`, `name`, `arguments`, `origin: "model_request"`: a model request, not proof of approval or dispatch |
| `tool_result` | `call_id`, `text`, `is_error`: retained result, including application-generated permission denials |

Provider call IDs correlate blocks locally; they are not global operation/attempt
identities. `state_phase` reports committed application state, not proof of a live
worker, local cancellation completion, successful remote effect, or terminal
settlement. Operation lookup keeps its separate result/status contract.

The projection excludes system configuration, authority facts, profiles, budgets,
reservations and raw state records. It does not remove user-authored text merely
because that text resembles a secret. Client rendering must treat all strings and
tool arguments as untrusted data; JSON transport tests do not establish browser
script-injection safety.

## Bounds, errors and retention

A page's message array is capped at 1,100,000 serialized UTF-8 bytes independently
of `limit`. Pagination stops before exceeding that bound; it never skips a message
or clips its content to advance. The 10,000-element coordinate ceiling matches the
existing pinned JSON library; it does not expand the runtime's array limit.

| Condition | Response |
|---|---|
| Missing or tombstoned session | `404 session_not_found` |
| Revision changed | `409 history_changed` |
| Invalid query or out-of-range offset | `400 invalid_history_request` |
| Failed storage observation | `503 storage_unavailable` |
| Malformed internal state or another host refusal | Existing fail-closed host error; never successful empty history |

All reads are non-mutating and return `Cache-Control: no-store`. They cannot submit
work, reserve capacity, acknowledge cancellation, or advance an operation.

The source is the bounded retained model context, not an append-only journal.
Temporary tool-batch results are not invented as completed history. The legacy
application compacts older complete turn segments; migration of that behavior,
output clipping, retention, audit history, export/delete and conversation discovery
remain required work. This route alone does not promise indefinite retention or
every intermediate execution event.

The application bundle must include both the API route and the fixed history
projection. Adding it changes bundle identity; previously accepted work must not be
silently redispatched under a changed bundle. Changed-bundle recovery remains an
explicit open qualification item in [the execution contract](execution-contract.md).
