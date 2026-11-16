# sigil-pi v1 threat model

Status: draft for independent review. Last updated 2026-11-15.

This model covers the authenticated `product_main.py` / `product_service.py` v1 surface. It
does not cover the research endpoint in `agent.serve`: its single shared bearer token (M18)
authenticates a caller without distinguishing tenants, so that endpoint is supported only on
a trusted developer's loopback interface and is not part of the product deployment.

## Security objectives

1. One authenticated tenant cannot read, modify, schedule work in, invoke tools for, export,
   or delete another tenant's sessions.
2. A model or tenant can invoke only the exact tools granted to its authenticated principal,
   and every tool remains bounded by its SIGIL capability manifest.
3. Production credentials never enter guest memory, model-visible transcripts, customer
   exports, structured request logs, audit files, or release artifacts.
4. Untrusted model, user, file, and network content cannot escape the tenant sandbox or widen
   a forge's declared capabilities.
5. Failure is bounded: input, concurrency, scheduling, context, tool output, compiler
   response, network operations, and shutdown have explicit limits.
6. Security-relevant actions remain attributable without making audit storage a second copy
   of customer conversations.

## Assets

- Anthropic and provider API credentials held by the host runtime.
- Bearer credentials held by customers; only their SHA-256 digests are in the auth policy.
- Conversation transcripts, sandbox files, schedules, and any future memory records.
- Tool and system-prompt integrity.
- SIGIL source, compiled guests, grants, and verification result.
- Audit-chain signing key and audit records.
- Usage/cost accounting and availability capacity.

## Trust boundaries

```text
untrusted client
    | TLS + bearer authentication
    v
product HTTP boundary (trusted host)
    | principal -> tenant/session/tool policy
    v
PiAgent orchestration (trusted host)
    | audited forge requests, one at a time per session
    v
SIGIL compiler/runtime (trusted enforcement boundary)
    | minimal grant + host-injected secret
    +--> model/API provider (external trusted recipient of its own credential)
    +--> tenant sandbox / schedule / session / audit storage
```

The model, model output, user prompts, recalled text, fetched content, tool inputs, and files
inside a tenant sandbox are untrusted. The host process, production auth policy, SIGIL
compiler/runtime at the pinned revision, operating system, TLS termination, and secret
manager are trusted. A fully compromised live host is outside the containment claim: it can
read host-held credentials and rewrite behavior before audit signing.

## Attacker capabilities

The design assumes an attacker may:

- create arbitrary authenticated prompts within one compromised tenant credential;
- know or guess external session and schedule names used by other tenants;
- induce arbitrary syntactically valid or malformed model tool calls;
- control content in sandbox files and on an allowlisted network host;
- operate an allowlisted redirect endpoint or slow/dribbling HTTP peer;
- send malformed, oversized, concurrent, or slow HTTP requests;
- restart a worker or interrupt a state write;
- read or modify copied audit storage without possessing the separately held audit key.

The design does not assume that an ordinary tenant can write the host filesystem, change the
auth policy, change environment variables, replace the pinned runtime, or read the secret
manager directly. Those are infrastructure/operator compromise scenarios.

## Implemented controls

### Identity and isolation

- Every v1 route authenticates a bearer credential.
- Product bearer credentials have bounded not-before/expiry windows. Unknown, future, and
  expired values fail identically; overlapping rotation entries must have identical policy.
- Tenant identity comes only from the credential. Request JSON has no tenant field.
- Internal session identity hashes both tenant and external session, so equal external names
  across tenants cannot collide.
- Scopes separate chat, operational reads, schedule reads/writes, and session export/delete.
- Tool authorization is enforced on untrusted model output as well as filtered from the model
  contract.
- Product memory is disabled until it supports tenant export, deletion, backup, and restore.

### Guest and secret containment

- Each tool runs as a separate ephemeral forge with its own minimal capability manifest.
- Filesystem grants resolve only inside the tenant session sandbox; path traversal is denied.
- A tool's credentials and the hosts its fetch may reach are its tenant's (audit F-09). A
  principal's policy names the secrets its tools may use and the hosts its fetch may reach;
  the values are read at startup from that tenant's directory under `PI_TENANT_SECRETS_DIR`
  and given to that principal's turns only. A principal whose policy names none has none: its
  secret-bearing and network tools are refused by the runtime before any request leaves, and
  a principal's turn never falls back to deployment-wide values. Product startup refuses
  `PI_SECRET_*` and `PI_NET_ALLOWLIST`. Redirect targets are revalidated.
- Provider secrets are substituted in the host HTTP shim and never enter guest memory.
- The product MCP client removes the benchmark-only unverified-certificate override before
  launching the compiler, and the gate and release builder compile the toolchain with the
  `solver` feature, so every forge's capability-flow and refinement obligations are
  discharged by Z3 rather than skipped; a solver-off binary fails closed (`R817`). The test
  suite forges through that same client, so the tools' obligations are discharged under
  test.
- V1 prohibits tools that place secrets in guest memory. The documented interprocedural
  taint-analysis gap is therefore not a product security dependency.

The model provider key is not a tenant credential. `ANTHROPIC_API_KEY` is one operator key
that every tenant's model calls use; no tool or guest receives it (the runtime receives it as
the model call's secret grant). Each tenant's use of it is metered and bounded by that
tenant's daily token quota (`PI_TOKENS_PER_DAY`), not separated by key, so every tenant's
prompts reach the provider under the operator's account. A tenant that needs its own provider
account needs its own deployment. The compiler process does not inherit the service's
settings or credentials: its environment carries no `PI_*` or `ANTHROPIC_*` value, so neither
the provider key, the audit key nor the `PI_TENANT_SECRETS_DIR` path is in it. The provider
key reaches a forge only as that call's secret grant, and a tenant secret value only as the
secret grant of a forge that runs for that tenant's principal.

### Bounds and failure behavior

- Request, message, session, history, system prompt, tool result, schedule count, tenant
  request rate, concurrent turns, daily tokens, aggregate conversation/sandbox storage, and
  aggregate audit growth are bounded. Transactional SQLite reserves capacity across workers
  before interactive or scheduled work and settles exact file sizes plus provider usage.
- The listener authenticates a request from its headers before it reads the body, handles at
  most `PI_MAX_CONNECTIONS` connections at once, requires each request's handshake, head and
  body within `PI_REQUEST_DEADLINE_SECONDS`, and bounds a head to 64 KiB of header lines
  (audit F-08). A client without a credential can hold one connection slot until the
  deadline, and none of its body is read; many slow connections can still fill the cap and
  delay other clients.
- Product schedule execution re-checks the creator's current scope and tool policy.
- Schedule claims use a cross-worker lease; session operations use per-session OS file locks.
- SIGIL HTTP shims have connect/total watchdogs and bounded outstanding worker threads at the
  pinned runtime. Model-provider requests have a total deadline (`PI_MODEL_HTTP_TIMEOUT_MS`,
  at most 60 seconds and never past the turn's) that includes the response body, and refuse
  redirects. Each MCP exchange, the request written and its whole response read, has one host
  deadline that kills a wedged compiler, including one that stalls partway through a response
  or stops reading its input; a response is capped at 64 MiB and each compiler stderr line at
  4,096 characters.
- A hard whole-turn deadline includes session/forge queueing and execution. Expiry kills the
  runtime, audits the stable failure class, persists bounded partial state, releases capacity,
  and returns `504`. The killed compiler is retired and replaced on the next forge, so one
  tenant's expired turn cannot deny service to another; readiness fails only when replacement
  itself keeps failing. Every replacement re-checks the binary against `SIGIL_REV`. Startup
  requires turn leases to outlive the deadline.
- Non-loopback plaintext product binds fail at startup.
- Stable errors never expose exception or forge diagnostic strings.
- Draining fails readiness, refuses new chat turns, schedule changes, exports and deletions
  with `503 service_draining`, starts no further scheduled turn, and waits, within one
  `PI_DRAIN_TIMEOUT_SECONDS` budget, for the work already accepted, exports, deletions and
  schedule changes included.
- CI requires 100% line and branch coverage for the explicitly inventoried forge-runtime,
  credential/tenant-policy, tenant tool grant, quota, lifecycle/retention, server, dispatch,
  readiness, scheduler, agent (tool dispatch, sandboxes, session locks, grant redaction, audit
  chain) and release-evidence boundaries in `docs/product-readiness.md`; global coverage
  cannot mask a missed branch in them, and no module the gate measures may carry a coverage
  pragma.

### Records and data lifecycle

- Audit records contain hashes and lengths rather than prompts/replies, redact secret values,
  form per-session hash chains, and are HMAC-signed in product mode.
- Startup and a periodic monitor verify every signed audit chain. Verification failure makes
  readiness fail and emits a content-free critical event plus bounded failure metrics.
- Structured request logs omit token, session, prompt, reply, and raw tenant identifiers.
- Tenant-authorized export rejects symlinks and is size bounded.
- Active-storage deletion removes the session transcript, sandbox, schedules, and audit
  chain, and any scratch file an interrupted save of the transcript left, under the same
  session operation lock used by chat and scheduling.
- A provisional 90-day active-state policy sweeps at startup and hourly by default. It
  verifies signed chains before deletion, re-checks inactivity under the cross-worker session
  lock, deletes the durable registry row last for crash-safe retry, and drops readiness with
  a content-free critical event on failure. Schedule-only and legacy-timestamp state have
  explicit conservative handling.
- A backup is taken online, by a separate process that runs as the service's user and takes
  each session's lock and the schedule store's lock in turn, or offline, with proof of a clean
  drain. Either way it verifies signed audit chains and structured state, preserves and
  integrity-checks the durable quota/usage ledger, records per-file hashes, and restore
  validates into a new tree before atomic install.
- While an online backup runs, a private unencrypted copy of the state exists in a `0700`
  directory beside the output. A backup that is killed leaves it until the next managed cycle
  or the operator removes it.

## Known blocking risks

These items prevent a production-readiness security sign-off today:

1. Tenant quota reservations bound concurrent admission, and active storage has an enforced
   provisional retention period, but final per-turn resource reservations and the retention
   duration still require launch-owner approval and load/policy evidence.
2. Online and offline backup, restore and locked retention-expiry mechanics exist, but deployed
   15-minute scheduling, approved encryption at rest, exercised expiry evidence, independent
   authorization, key rotation, and timed recovery drills are not implemented or evidenced.
3. A deterministic bundle and signed tag workflow exist, and v0.4.0's provenance and SBOM
   attestations were verified independently against the exact repository, release workflow,
   tag, source commit and GitHub-hosted runner (`docs/evidence/release-0.4.0.md`). Release
   tags are not protected, earlier releases are unattested, and the hosted runner base image
   remains mutable even though actions and language toolchains are pinned.
4. Authentication uses a static digest file with enforced expiry. Rotation and emergency
   revocation are graceful rolling-restart operations; there is no live reload, external
   identity-provider integration, or revocation feed.
5. Model-provider requests have a product deadline (`PI_MODEL_HTTP_TIMEOUT_MS`), but other
   guests' HTTP calls keep the pinned runtime's own per-call timeout, which is not a product
   setting; the whole-turn deadline still bounds them.
6. Operational metrics are initial process metrics, not the complete alerting/SLO set.
7. Independent design review, penetration testing, dependency/artifact scanning, and the
   production-equivalent pilot have not occurred.
8. POSIX `flock` and a shared local SQLite database are the cross-worker coordination
   mechanisms, so the supported topology is limited to workers sharing one correctly
   configured POSIX filesystem. Network filesystems with weaker locking semantics are
   unsupported.
9. The solver-verifying compiler links `libz3` dynamically, so the release bundle depends
   on a host-provided Z3 of the pinned release. The SBOM names that dependency and the Z3
   version the compiler was built against; the reproducibility evidence and the clean-host
   drill do not yet cover it (found 2026-08-22, when the merged suite first forged through
   the product client).

## Required review questions

The independent reviewer must, at minimum, try to disprove:

- cross-tenant isolation for chat, tools, schedules, export, delete, audit, and every future
  state surface;
- authorization enforcement when hostile model output names hidden tools;
- absence of raw bearer/provider secrets from every guest/log/export/artifact channel;
- correctness of redirect/SSRF defenses and external TLS enforcement;
- schedule lease and delete behavior under multiple workers, crash, delay, and policy change;
- retention eligibility, audit precheck, crash ordering, and access races across workers;
- path and symlink containment under concurrent export/delete/tool activity;
- stable-error non-disclosure and structured-log redaction;
- fail-closed behavior under corrupt auth, schedule, session, and audit state;
- resource bounds under authenticated and unauthenticated denial-of-service traffic.

Findings must name severity, exploit preconditions, affected asset, proof/reproduction, owner,
and remediation. Product readiness requires zero unresolved critical or high findings.
