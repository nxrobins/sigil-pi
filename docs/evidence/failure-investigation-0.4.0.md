# Failure-drill investigation — v0.4.0

Status: **NOT QUALIFYING.** This is a retained failing execution and its diagnosis,
not `failure-injection.md` launch evidence. No security, operations, or launch sign-off
is asserted. Review: Codex automated execution and source-level diagnosis, 2026-09-07 UTC;
not an independent human review.

- Published candidate: v0.4.0, SHA-256
  `6f17f2f0a91c9f9cdc50c4cb6d0d50f2c305c55162180c933c0604f556407203`.
- Harness source: `88f05960b4b4495d8457582eb327b3b98a92420e` (merged PR #41).
- [Raw workflow and logs](https://github.com/nxrobins/sigil-pi/actions/runs/34150748182).
- [Unmodified raw JSON](failure-runs/34150748182.json).
  SHA-256: `498a50881f4b1fc86abefb3ed7bf9070fbac788ede48e38bddeab0a93b734665`.
- Environment: isolated GitHub-hosted Linux x86_64, CPython 3.12.11, pinned Z3 4.12.2,
  one worker, local POSIX state, a 64 MiB disposable tmpfs for disk exhaustion, and a
  local synthetic model provider. No customer data or paid model calls.
- The workflow verified the published archive's digest, manifest, version, SBOM pin and
  operator-document parity against `candidate.json` before running the service.

## Observations and diagnosis

| Category | Raw result | Diagnosis |
|---|---|---|
| Provider unavailable | PASS | Two failed provider requests, stable 502, readiness 200, subsequent chat 200; committed state preserved. |
| Network reset | PASS | Two reset connections, stable 502, readiness 200, subsequent chat 200; committed state preserved. |
| Corrupt signed audit chain | PASS | Startup explicitly refused the signature failure; repaired state booted ready; original committed bytes preserved. |
| Runtime crash | FAIL | The detecting call returned 502 and retired the dead compiler. The harness incorrectly required that same call to succeed, instead of issuing a separate recovery call. |
| Full disk | FAIL | Actual ENOSPC (errno 28), stable chat 500, but readiness also returned 500 instead of the documented 503. Reclaiming space restored chat and committed state was preserved. |
| Interrupted write | FAIL | The pre-rename interruption and service restart ran, but backup refused active quota leases. The harness restarted before the lease-expiry wait required by the packaged runbook. Integrity qualification was therefore not established. |

`runtime_client.SupervisedRuntime` deliberately does not replay a failed forge: it retires
that connection and starts a new generation for the next caller. The corrected drill must
observe both the stable detecting-call failure and a distinct successful recovery call/PID.
No runtime implementation change is justified by this initial result alone.

The packaged `docs/runbooks.md` section **Failed graceful shutdown** explicitly requires
waiting for lease expiry before starting the replacement. The corrected drill reads the
persisted expiry, waits within its bounded recovery budget without deleting or rewriting
leases, then verifies startup reconciliation and backup. It also retains an injection's
observations when a later integrity check fails.

## Confirmed defect and unreleased fix

`ProductService.dispatch` performs durable request admission **before** routing readiness.
When the state disk is full, SQLite admission raises before the dependency snapshot can
return `503 not_ready`. The outer exception boundary instead returns `500 internal_error`.
This contradicts the readiness contract in the published `docs/api.md`.

The follow-up source fix handles admission storage failures only for `GET /v1/ready`.
It still requires valid credentials and `ops:read`, applies a local emergency rate limit
at the configured tenant bound, and marks `quota_store: false` in the content-free 503
response. It does not bypass admission for chat or other routes, disclose diagnostics,
or turn unrelated programming exceptions into healthy readiness responses. Focused
regressions cover those boundaries and recovery after the quota store becomes usable.

**That fix is not in the frozen v0.4.0 archive.** A new published candidate, new artifact-bound
failure/recovery evidence, and the remaining load, deployment, pilot and independent review
work are still required. A source test pass cannot qualify the old binary. Development
continues through protected CI while the GA gate remains closed.

The drill itself was integrated through [PR #41](https://github.com/nxrobins/sigil-pi/pull/41)
with [both required checks passing](https://github.com/nxrobins/sigil-pi/actions/runs/34150172110).
That run collected 820 cases: 818 passes, one optional unbuilt research-memory-sidecar skip,
and one strict research-only xfail. Line/branch coverage remained 91.11%/86.08%.
