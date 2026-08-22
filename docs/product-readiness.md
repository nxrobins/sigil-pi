# sigil-pi v1.0 product-readiness record

This document is the release gate for sigil-pi v1.0. It translates the product-readiness
goal into evidence that can be checked from a clean checkout or from the production pilot.
An item is `PASS` only when the linked evidence exists and proves the complete criterion.
Implemented code without its required operational evidence remains `PARTIAL`.

Last audited: 2026-08-22, against `origin/main` `c04446d` plus the `product-readiness-rebased`
branch (the 2026-08-20 tree rebased onto it).

## Decision

**NOT READY — internal alpha.** The research implementation has a strong automated test
base, but none of the ten blocking product areas has complete evidence yet. The local
official gate now passes against an isolated checkout of the exact pinned SIGIL commit and a
candidate bundle is byte-reproducible, but no protected signed release run, clean-host timed
deployment, independent review, qualifying distinct-version recovery drill, load test, or
30-day pilot has occurred.

No percentage-complete score is used: product readiness is conjunctive, not an average.

## Evidence rules

- `PASS`: objective evidence covers the entire criterion and is repeatable.
- `PARTIAL`: meaningful implementation or evidence exists, but a required property is
  missing or covers only a narrower topology.
- `FAIL`: current evidence contradicts the criterion.
- `MISSING`: no adequate implementation or evidence exists.
- `EXTERNAL`: the repository can prepare the test, but an independent party or elapsed
  production-equivalent run is required to produce the final evidence.

## Blocking areas

| # | Area | State | Current authoritative evidence | Evidence required to pass |
|---|---|---|---|---|
| 1 | Reproducible release | PARTIAL | `ci.sh` pins/rebuilds SIGIL trees as a solver-verifying compiler (`--features sigil-mcp/solver`; Z3 pinned and SHA256-verified in CI); Python/dev inputs, Rust/Python versions, and every CI action are commit/version locked; the full forge job is mandatory. `scripts/build_release.py` bundles the exact runtime/stdlib, manifests, checksum, capacity/API/operations/state-compatibility docs, release drill, and CycloneDX SBOM. The expanded local gate passed on 2026-08-20 against SIGIL `eb9f1715dd3fadd3c9fe1f6ccb19c17f49a2ac0b`, and two fresh real candidate builds were byte-identical; the non-bundled local verification record holds their digest. The drill safely installs and manifest-verifies exact archives, starts old/new/rollback releases, and emits digest-bound timing/continuity evidence. A tag workflow gates the build and requests signed GitHub provenance/SBOM attestations. No protected tag result, immutable runner/base-image pin, or qualifying distinct-version clean-host drill evidence exists. The solver-verifying binary links `libz3` dynamically, so the bundle depends on a host-provided Z3 of the pinned release; the SBOM, reproducibility record, and drill do not cover that dependency yet (found 2026-08-22). | A mandatory protected clean CI/tag run that builds the pinned runtime, passes the full suite, publishes and independently verifies the signed artifact + checksum + SBOM, followed by the candidate-bound clean-host install/upgrade/rollback drill. |
| 2 | Authentication and tenant isolation | PARTIAL | `product_service.py` authenticates every v1 route, derives tenant identity only from the credential, namespaces sessions, separates chat/ops/schedule/data scopes, enforces per-principal tools against hostile model output, and requires TLS off-loopback. Product credentials require bounded validity windows; future/expired/unknown values fail identically; overlapping rolling-restart rotation is permitted only for identical principal policy, including scheduled ownership. Cross-tenant chat/schedule/export/delete and lifecycle tests pass. The legacy endpoint remains outside the product surface. External-IdP/live revocation are unsupported, and independent final-topology review remains absent. | Independently review cross-tenant behavior across every final state surface and the documented rolling-restart identity lifecycle; approve the static-policy boundary for v1. |
| 3 | Security assurance | PARTIAL | Host-injected secrets, capability manifests, fail-closed network grants, redirect defenses, path isolation, input bounds, hard turn deadlines, and signed audit chains have adversarial tests. Since 2026-08-22 the suite forges through the product client against a solver-verifying compiler, so every tool's Z3 obligations are discharged under test; before that it forged through SIGIL's bench client with the verification override set, and the product client — which strips that override — had never forged a real turn against the binary the gate builds. The general in-guest taint proof has a documented interprocedural gap but product startup excludes memory/guest-secret modes. Request/concurrency/daily-token/conversation+sandbox-storage/schedule/audit quotas are durably reserved and settled across supported workers and scheduled turns; exhaustion fails with stable errors and deletion credits state. The GA gate now requires candidate-bound raw security evidence and independently checks a dated independent reviewer, the exact threat-model digest, zero unresolved critical/high findings, no exceptions, and passed dependency plus exact-artifact scans. Default limits and all external security evidence still require approval/execution. | Approve the final quota values; run and publish exact-candidate artifact/dependency scans and an independent threat-model/security review with no unresolved critical/high findings. |
| 4 | Reliability and recovery | PARTIAL | Product state operations share POSIX session locks; schedules and quotas use cross-process transactions/leases. A hard deadline covers session/forge queueing and execution, kills wedged runtime work, audits the failure, preserves bounded partial state, releases capacity, returns stable `504`, and drops readiness; startup requires leases to outlive it. Signals drain accepted work. Active retention re-checks under the session lock and removes its registry row last; interrupted-cleanup and retry tests pass. `state_tool.py` provides clean-drain, bounded, checksummed, signed-audit-verified offline backup/atomic restore plus locked managed backup creation/expiry; publication cannot overwrite, and expiry validates every managed digest/manifest/file invariant before deleting any file. It snapshots and integrity-checks the durable usage/activity ledger and rejects active leases. `config/failure-injection-matrix.json` binds all six required categories to collected tests. The bundled release drill enforces a <=15-minute backup age, <=4-hour restore-to-readiness, <=15-minute rollback, real old/new/old service probes, and non-vacuous committed-session continuity. The GA gate independently validates raw recovery evidence and all six final-topology failure categories, including state integrity, service recovery, and immutable raw-evidence links. Deployment of encrypted <=15-minute backup scheduling and approved expiry, a qualifying distinct-version recovery drill, and a candidate-bound final-topology failure drill remain absent. | Prove the final topology under concurrency/restarts; deploy and exercise encrypted backups/expiry within RPO; pass the release recovery drill at RPO <=15m/RTO <=4h/rollback <=15m; run the six-category failure matrix against the exact release topology and publish both digest-bound reports. |
| 5 | Performance and resource control | PARTIAL | History/tool results, hard turn duration, per-tenant daily tokens, conversation+sandbox storage, schedules, and audit growth are bounded, with defined disconnect/deadline cancellation behavior. `docs/capacity.md` declares a proposed 25-turn forecast and exact 50-client/60-minute conjunctive gate. `scripts/load_test.py` enforces digest/forecast binding; separate single/tool latency; error/queue limits; tool, continuity, and isolation checks; and explicit memory/thread/FD/storage/audit/disk bounds. A single MCP connection still serializes forges. The forecast/resource bounds are not owner-approved and no qualifying report exists. | Obtain product/operations approval for the declaration and quota reservations, then pass and publish the digest-bound 60-minute run; prove forge serialization meets capacity and add aggregate metrics before claiming more than one worker. |
| 6 | Production observability | PARTIAL | Authenticated health/readiness/version endpoints plus JSON and Prometheus metrics, and secret-safe structured request logs include request IDs and failure classes. Readiness now fails conjunctively and reports fixed, content-free runtime, quota-store, schedule-store, state-storage, audit-verification, and retention component health; probe exceptions fail closed without diagnostics. Fixed-bucket turn-latency/queue histograms and bounded metrics cover request/error/turn/queue/token/tool/retry/dependency totals; aggregate tenant token/storage/audit usage and active reservations; and process peak memory, threads, FDs, state/audit/sandbox size, and disk capacity without customer labels. Startup/periodic monitors verify signed audit chains and enforce active retention; either failure drops readiness and emits a content-free critical event plus bounded metrics. `config/grafana-slo-dashboard.json` packages aggregate/component readiness, service-error, successful-turn, latency, queue, disk, lifecycle, activity, and resource views. `config/alert-policy.json` maps principal signals to exact thresholds, severity, owner role, and resolving procedures in `docs/runbooks.md`; tests bind exporter names, dashboard queries/thresholds, dependency labels, event paths, runbook anchors, and low-cardinality rules. Collector/dashboard deployment, multi-worker aggregation, routed/tested alert delivery, named human owners, and exercised runbooks remain absent. | Deploy the authenticated scrape and dashboard in the candidate environment, route and test the actionable policy, assign named owners, and exercise every principal runbook against the exact candidate. |
| 7 | Data lifecycle and privacy | PARTIAL | Authenticated, tenant-scoped export and immediate active-storage deletion cover conversations, sandbox files, schedules, and audit records under the same lock as chat/scheduling; deletion credits the durable aggregate ledger. An enforced, configurable provisional 90-day active-state policy sweeps at startup and hourly by default, verifies audit integrity first, re-checks activity under the session lock, handles schedule-only/legacy state, deletes the registry last, and exposes failure health/metrics. Per-tenant conversation+sandbox and audit quotas prevent unbounded active growth. Product memory is rejected. Offline backup/restore preserves these surfaces and the quota/activity ledger with schema/checksum/audit/SQLite validation and refuses overwrite. A locked managed cycle requires an explicit backup-retention duration and private directory, creates a digest/time-bound backup, and deletes only fully validated expired managed archives. `docs/state-compatibility.md` defines every schema surface, the no-implicit-migration rule, versioned migration requirements, and distinct unchanged-schema versus migrated-state rollback paths; shutdown and backup share one schema constant. Final duration/legal approval, deployed backup scheduling/deletion evidence, encryption-at-rest evidence, and digest-bound migration/recovery drills remain absent. | Approve and publish the active and backup retention/deletion SLA; deploy and exercise managed backup expiry on encrypted storage; then execute the documented backup, restore, upgrade, migration, and rollback compatibility drills on the exact candidate. |
| 8 | Stable product contract | PARTIAL | The `/v1` contract defines authentication, scopes, stable errors, request IDs, tenant/session rules, usage/limits, caller and provider retry rules, absence of idempotency keys, ambiguous-outcome reconciliation, deadline-only cancellation, chat, schedules, lifecycle, operations, TLS, and additive compatibility. Startup rejects weak/missing signing config, plaintext external bind, unknown tools/scopes, memory, and invalid bounds. A changelog, security-reporting process, state-compatibility contract, and candidate support matrix now exist. A license choice, final model/capacity/retention values, release notes for v1.0, verified private reporting channel, and named engineering/security/operations ownership remain absent. | Make and publish the remaining business/governance decisions, verify the reporting channel, and record named ownership/sign-off. |
| 9 | Quality gate | PARTIAL | The 2026-08-22 complete pinned-source gate (`docs/evidence/local-verification-2026-08-22.md`) passed all 724 collected cases (723 passes and one strict research-only xfail) at 91.97% line and 87.48% branch coverage with `toolchain.py` now measured, including the 100% requirement for every critical boundary below, forging through the product client against a solver-verifying compiler for the first time (the 2026-08-20 run: 630 cases, 92.29%/87.10%, through SIGIL's bench client). `config/tool-test-matrix.json` is manifest-complete and names collected happy-path, malformed-input, denied-capability, boundary, and end-to-end evidence for every production tool; uniform per-tool malformed and zero-capability executions prevent a new tool from inheriting the claim indirectly. `config/failure-injection-matrix.json` is likewise exact and machine-checked against collected tests for all six required failure classes. Evidence-gate tests adversarially reject security, load, recovery, failure-injection, pilot, artifact, scan, topology, timing, continuity, and sign-off weaknesses; observability tests bind Prometheus names, dashboard queries/SLO thresholds, component health, alert events, low-cardinality rules, and real runbook anchors. Coverage also includes malformed config/state, hostile tool authorization, cross-worker locks/leases, crash reconciliation, audit monitoring, dependency probe failure, runtime failure, startup/drain, deterministic release, workflow/evidence guards, atomic/non-overwriting managed backup expiry, restore, and capacity-evidence validation. Protected CI, candidate-bound load/failure/release scenarios, and zero-Sev-1/Sev-2 review remain incomplete. | Repeat the exact gate in protected CI; complete all release scenarios and zero-Sev-1/Sev-2 review. |
| 10 | Launch validation | EXTERNAL | No production-equivalent 30-day pilot evidence exists. | A continuous 30-day pilot meeting both SLOs, with no isolation/corruption/security breach, successful restore/rollback drills, bounded resources, and a runbook-driven simulated incident. |

## Critical security coverage inventory

`ci.sh` requires 100% line and branch coverage for these explicitly identified boundaries;
the ordinary >=85% global thresholds cannot average away a missed branch in them:

| Boundary | Enforced code |
|---|---|
| Verified forge/runtime integrity | All of `runtime_client.py` and `sigil_compose.py`: removal of the unverified-certificate escape hatch, bounded/matched MCP protocol responses, kill-on-deadline behavior, deterministic stdlib composition, and module-path containment |
| Credential and tenant policy | `AuthRegistry._active`, `authenticate`, and `policy`; `_internal_session`; `ProductService._allowed_tools`, `_require`, `_validate_session`, `_validate_message`, and `_chat_request` |
| Durable tenant resource isolation | `DurableQuotaStore.acquire_turn` and `settle_turn` |
| Data attribution, lifecycle, and retention | `DurableQuotaStore.registered_session_inactive` and `remove_registered_session`; `ProductScheduleStore.internal_active`; `ProductDataManager.verify_quota_registry`, `export`, `delete_internal_files`, and `delete`; `ProductRetentionMonitor.tick` |
| Fail-closed availability and transport | `ProductService.is_ready` and `validate_transport` |

The inventory is pinned by a guard test as well as the coverage checker. Adding a new
production security boundary requires either placing it inside an identified boundary or
expanding this inventory deliberately. Complete coverage proves test execution of these
paths; it does not replace adversarial review of whether the inventory or implementation is
correct.

## v1 security boundary

Product v1 may use only host-injected secrets that are structurally absent from guest
memory. A manifest or tool that introduces a secret into guest memory is outside the v1
product contract and must fail the production validation gate. The open interprocedural
taint-analysis boundary may remain as research work, but no production guarantee may rely
on it.

The absence of a shell or unrestricted host access is a product invariant, not missing
functionality.

## Required launch evidence index

The following paths are reserved for final, reviewable evidence. A placeholder is not a
pass; each document must identify the tested artifact digest and date.

| Evidence | Intended path or system |
|---|---|
| Release digest, signature, SBOM | CI release attestation and `dist/` release assets |
| Mandatory clean CI run | Protected release workflow run |
| Threat model | `docs/security/threat-model.md` |
| Independent security review | `docs/evidence/security-review.md` or linked immutable report |
| Load and latency report | `docs/evidence/load-test.md` |
| Failure-injection report | `docs/evidence/failure-injection.md` |
| Backup/restore and rollback drills | `docs/evidence/recovery-drills.md` |
| 30-day SLO dashboard | Linked immutable dashboard export in `docs/evidence/pilot.md` |
| API and operations documentation | `docs/api.md` and `docs/operations.md` |
| Engineering/security/operations sign-offs | `docs/evidence/release-signoff.md` |

## Gate command

The general-availability gate is `./product-ci.sh`. It runs the full pinned-source gate,
requires a non-development version, builds the exact candidate, and validates substantive
external reports plus a digest-bound, dated, three-role sign-off. It fails closed today
because the external evidence does not exist. The validator can check structure and binding;
independence, authenticity, and adequacy remain human release-review responsibilities.
