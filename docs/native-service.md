# SIGIL API admission through a native service

Status: **LOCAL DEVELOPMENT PATH — NOT A COMPLETE AGENT SERVICE OR PILOT**.
The [SIGIL handler](../app/pi/api.sigil) serves real HTTP requests through the
[native application host](../native/service/src/main.rs). Python is test/build
tooling, not the request dispatcher, authorization implementation or agent policy.

Four routes are implemented: `POST /v1/operations` atomically publishes an operation,
deduplication entry, conversation state, first model intent, reservation counter and
reservation; `GET /v1/operations/{operation}` reports retained acceptance or a
[durably settled terminal result](settlement.md); and
`POST /v1/operations/{operation}/cancel` records a [durable cancellation request](cancellation.md);
and `GET /v1/sessions/{session}/messages` returns [revision-bound retained history](session-history.md).
Pending lookup reports a retained cancellation request. Terminal cancellation
returns control metadata only: its `chat` scope does not permit reading the answer,
which remains behind `sessions:read` on GET.
The opt-in [v4 automatic service](automatic-service.md) now executes the first
model/file/model turn and follow-up; v3 remains the admission-only reference. The
[admission contract](admission.md) states exactly what acceptance now establishes.
The old two-record queue and the separate durable-turn fixture are not presented
as complete authenticated execution.

The 11 legacy routes and other planned additions have SIGIL-owned scope checks,
but their handlers return `501 route_not_migrated` in this prototype. The Python
product service is unchanged and remains the compatibility reference. An opt-in
[development browser](browser-interface.md) now uses the same API, including
discovery in the v6 profile. Full browser/API parity and M1/M2/M3 qualification
remain incomplete.

The optional `--public-assets MANIFEST_JSON` flag serves an explicit, hash-checked
static inventory from the same loopback listener. It adds no product authorization
or agent decisions to native code. The original invocation remains supported;
this is not a packaged deployment or permission to expose the service externally.

The explicit [v7 HTTP profile](http-exchange.md) adds SIGIL-owned correlation and
bounded response metadata to the same API core. Its staged 154-case verification
passed; the integrated whole-source gate is pending. This does not migrate the
eleven legacy routes or qualify a v6-to-v7 in-flight-operation upgrade.

## Ownership and trusted data flow

1. The bounded native HTTP layer compares the presented bearer digest against every
   configured digest using constant-time equality and selection. Matching supplies
   a fact, not a native authorization verdict.
2. The host holds the matched immutable credential facts and native store scope.
   The Authorization header is removed before the guest envelope is constructed;
   it is not put in records, logs or continuations. This is not a general scrubber
   for secrets a user writes into message text. The host supplies time, a fresh
   random operation identity, configured bundle identity and function-name inventory.
3. SIGIL checks credentials, permissions, request shape and deduplication. It chooses
   reads, calls to fixed grantless admission/history functions, one atomic commit, or an HTTP
   response. SIGIL validates both credential/grant consistency and nested model/budget
   profiles during bootstrap, before native state is opened.
4. The host performs each command within its independent ceiling and matched scope,
   checking the SIGIL-selected time window after worker execution and decoding,
   immediately before action initiation. Actual observations return to the entry
   application. A pure function's output is data, not a native command.
5. SIGIL interprets the result and issues the HTTP response. Acceptance follows the
   actual six-record commit acknowledgement, not a proposed transaction.

The host contains no product route table, scope-name policy, credential-expiry rule, tenant
allowance calculation, conversation sequencing or product-record construction.
Its bounded `read`/`call`/`commit`/`reply` dispatcher is a mechanism. An unmatched
credential has no storage scope even if faulty entry code requests I/O.

## Fixed grantless functions

Version 3 adds an owner-configured registry of at most eight named functions.
The current SIGIL API accepts exactly the `admission` and `history` functions at bootstrap;
unexpected/missing registrations fail before state opening. Native aliases are
bounded ASCII alphanumeric/underscore names. Every entry/function has independently
checked, fixed source and runtime hashes and fuel/time ceilings. The native host
rejects network, filesystem and secret grants for **all** of these workers.

Only the entry application can request a function call. The callee receives just
the selected bytes, not the original HTTP envelope, raw bearer header or native
scope handle. An admission call deliberately includes the facts and actual snapshots
the entry application selected. A function cannot read storage, call another
function or execute a returned `HC3` command; its returned bytes go back to the
entry application as a `call` observation. The registry is not an effect-executor
registry and does not authorize provider/file operations.

The same bounded continuation mechanism runs bootstrap with **no open store and no
scope**, allowing SIGIL to validate profiles in the admission component first.
The native host opens/initializes storage only after a guarded 204/empty approval.
A bootstrap attempt to read or commit fails independently.

The bundle identity is SHA-256 over a stable serialization of the host contract
label, step limit, entry source/runtime hashes and ceilings, and named function
source/runtime hashes and ceilings. It is bound into operation/reservation records.
It identifies configured code and limits; it is **not** a signature, trusted
verification provenance, the native host executable digest, or complete artifact
admission. Source/runtime checks retain the [worker bridge's](native-worker.md)
trusted-local-path assumptions. No AOT or alternative-backend safety is inferred.

## Admission, replay and conversations

The exact PS1 decoder canonicalizes session/message/submission-key bytes in SIGIL.
Field order, JSON whitespace and equivalent string escapes do not change identity.
Unknown fields, duplicate decoded keys, type coercion and malformed input are refused.

Deduplication is tenant/principal/submission-key scoped. Its native key uses
hex-encoded principal UTF-8, a separator and the validated key, avoiding delimiter
ambiguity. Matching replay returns the original ID with `replayed:true`, even when
reservation capacity is full. Changed canonical payload returns 409. Current
credential policy is checked on replay; original authority/bundle records are not
rewritten or widened. Retained tombstones cannot reopen a key.

New admission reads actual conversation and budget snapshots, calls the SIGIL
producer and forwards its one exact compare-and-set batch. Conversation identity
remains tenant scoped. Distinct submissions cannot overwrite an active/unresolved
conversation; same-tenant principals share this contention and reservation capacity.
Completed conversations can supply retained history only after their terminal
operation is settled, with both conversation and operation revisions checked.
Polling requires `sessions:read` and the accepting tenant AND principal; another
identity receives 404. This owner-only operation policy does not make conversation
history principal-private. The history route requires current `sessions:read` and
shares retained messages with authorized principals in the same tenant. It performs
no writes and exposes no system configuration or authority records. Final sharing/
recovery policy remains an M0 decision.

The [admission document](admission.md) defines all six records and reservation
semantics. Capacity currently covers outstanding reservations only. The separate
[settlement mechanism](settlement.md) atomically publishes typed terminal results and
settles known capacity while retaining unknown/overrun token holds. The v4 SIGIL
coordinator invokes it automatically; v3 does not. There is no cumulative charged-usage ledger,
request-rate window or retention job in this path. Restart preserves reservations.
These are not yet complete product quotas or qualified provider-spending limits.

One serialized application owner processes requests; the native store rejects a
second owner. Concurrent duplicates converge on one admitted identity.
Multi-host/multi-worker execution is unsupported. A lost response is unknown
acceptance; recover using the same key/payload or retained operation. Queued requests
whose response channel closes are dropped before processing, but a started commit
may finish after client disconnection. No response error rolls back committed state.

## Internal records and configuration

Records use four-byte markers and eight-digit UTF-8 byte lengths; these envelopes
are never accepted as HTTP request bodies.

| Marker | Fields |
|---|---|
| `AH3` | method, raw path/query, body, matched facts, current seconds, fresh identity, stage, actual observation, held continuation, bundle identity, registered function names JSON, purpose (`boot`/`request`) |
| `HC3` | command, first argument, second argument, held continuation, time guard |
| `TG1` | inclusive not-before seconds, exclusive not-after seconds |
| `CB1` | credential facts, JSON array of actual `CG1` grant records |
| `CG1` | namespace, access mode |
| `SR1` | read outcome, record revision, retained value or empty absence/tombstone |
| `SC1` | commit outcome, global receipt revision |

The JSON host configuration has `version: 3`, a fixed grantless `worker`, a
`functions` name-to-worker map, `state_root`, independent store `limits` and
`credentials`. Worker configurations remain version 1, a separate contract.
Each credential has only its SHA-256 digest, opaque `CF2` facts and native namespace
grants. SIGIL checks the **actual six read/write grants** against the facts.
Unknown/duplicate config fields, duplicate credential digests, invalid aliases,
hash mismatches and widened guest grants are refused.

CF2 bounds principal/tenant/epoch identifiers and credential time/tool/scope policy
as before. Every tenant has six pairwise distinct namespaces; different tenants
cannot alias any of them. Same-tenant credentials share the exact namespace/profile
binding. SIGIL validates the nested BP1/PC1 profile through the actual registered
function before state opening. See [the admission codec table](admission.md#draft-codecs)
for these schemas and limits. Test profiles are not approved pilot configurations.

Old configuration versions and AH1/AH2/HC1/HC2 are refused, not implicitly upgraded.
CF1/DQ1/OQ1 do not prove a six-record admission and are refused by the new application.
Startup never erases or rewrites retained data. An existing development database
requires explicit matching config/schema handling; no internal-data migration,
qualified upgrade or pilot rollback procedure is claimed.

## Time-bound action initiation

TG1 uses canonical nonnegative integer seconds at most 9,007,199,254,740,991, with
`before < until`. Signed, leading-zero, overflowing, malformed, trailing and
unknown-version records fail decoding. Reads, calls and commits require a guard;
bootstrap approval requires one before storage opening.

SIGIL selects credential validity for authenticated replies and initial reads,
and the tighter operation/credential deadline for admission work. Bootstrap checks
the currently active credential set at each decision; a future-only credential
does not authorize state opening while no credential is active. Invalid-credential
refusals contain no protected data and use an unguarded reply. Native code does not
infer sensitivity from status/body or independently reinterpret credential policy.

After worker execution and full command decoding, the host re-reads time, rejects
rollback against its process-local high-water and checks the selected bounds and
its monotonic ceiling. It checks the monotonic ceiling again after the clock read.
Replies are checked before returning protected bytes to the HTTP layer. An invalid
guard or native-boundary refusal produces the transport's generic 503.

This is an **action-initiation boundary**, not a guarantee that filesystem work,
worker computation or HTTP delivery finishes before expiry. Kernel scheduling and
storage can take time after a check. The host cannot undo a started commit. An
acknowledgement can fail after all six records were durably published; subsequent
replay must preserve that decision. There is no durable trusted clock high-water,
live revocation in this API mechanism. The
[dispatch mechanism](dispatch-policy.md) checks authority at its native-bound boundary.

## Transport and operating limitations

The binary accepts `init <config-path> <port>` or `open <config-path> <port>`.
Port zero selects a local test port. Binding is **127.0.0.1 only**, with no external
override. Init creates exactly one new private directory and refuses existing state;
open never substitutes initialization for missing/corrupt state. The stdout
`sigil-application-host/v3` or `/v4` ready record indicates transport startup, not product
`/v1/ready` or pilot clearance.

Locked Hyper/Tokio HTTP/1 accepts origin-form targets, one Authorization header,
bounded fixed-length bodies and no transfer encoding/trailers. Connections close
after one request. Limits remain 64 headers/64 KiB, 4 KiB path, 1 MiB body, five-second
header/body arrival, 32 connections, 16 queued requests, eight entry continuation
steps and one executing application request. Each application request has the
configured monotonic ceiling, capped at 30 seconds; each function also keeps its
own ceiling. Responses are non-cacheable JSON.

There is no TLS, approved CORS/browser session flow, complete product audit,
tenant quota/retention parity, live-revocation service or graceful pilot drain.
Blocking kernel/storage work and worker ownership retain their documented limits.
These safeguards are not M5's measured operating envelope.

## Evidence and next integration

[API tests](../tests/test_native_api.py) exercise actual HTTP, native workers,
solver-verified SIGIL and scoped storage. They cover canonical replay/restart,
two tenants and same-tenant contention, forged context fields, profile/grant/code
registration refusal, guarded commands, real capacity rollback and function outputs
being treated as data rather than native commands. Native boundary tests retain
clock, scope, strict framing and atomicity cases. [Admission tests](../tests/test_admission.py)
check the pure proposal independently and apply the exact SIGIL batch to real storage.

These are local source results, not independent security review, complete route
parity, real-model usefulness or candidate-bound qualification. The opt-in
[automatic integration](automatic-service.md) now connects dispatch, interpretation
and settlement. Remaining work includes the complete recovery/cancellation boundary
matrix and full API/browser
and operator behavior. The actual control-plane consumer and all M0–M8 qualifications
remain separate requirements.
