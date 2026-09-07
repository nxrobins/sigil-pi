# sigil-pi v1.0 product-readiness record

This document is the release gate for sigil-pi v1.0. It translates the product-readiness
goal into evidence that can be checked from a clean checkout or from the production pilot.
An item is `PASS` only when the linked evidence exists and proves the complete criterion.
Implemented code without its required operational evidence remains `PARTIAL`.

Last status reconciliation: 2026-09-07, against main `faf83b8` and the v0.4.0 release
preparation. Historical evidence keeps its original artifact, date, and scope; it is not
silently transferred to the new candidate.

## Decision

**NOT READY — internal alpha.** Versions 0.1.0–0.3.0 have been published; v0.4.0 is being
prepared as a prerelease. A tag makes a candidate nameable and verifiable, not production-ready.
None of the ten blocking product areas has complete evidence yet.

There is now a qualifying distinct-version recovery drill for v0.3.0 with rollback to v0.2.0
(`docs/evidence/recovery-drills.md`). The latest main source gate passed on 2026-09-06 with
788 collected cases (787 passes, one strict research-only xfail), 91.11% line coverage and
86.08% branch coverage in [CI run 34020044494](https://github.com/nxrobins/sigil-pi/actions/runs/34020044494).
The repository is public and main-branch protection was enabled on 2026-09-07, after that run.
Both named GitHub Actions checks, an up-to-date branch, a pull request and resolved
conversations are required, including for administrators; independent approval is not required
for the sole maintainer. This is CI enforcement, not independent review or tag protection.

Public visibility removes the previous attestation obstacle. A new successfully attested
release still needs to be published and independently verified. The frozen v0.3.0 candidate
and its recovery evidence remain unchanged during v0.4.0 preparation. Independent security
review/scans, qualifying load/failure tests, deployed operational evidence and a 30-day pilot
remain missing. The new artifact needs its own recovery evidence as well.

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
| 1 | Reproducible release | PARTIAL | Releases v0.1.0–v0.3.0 exist but were published without attestations while the repository was private. Public visibility and required main-branch checks are now configured; v0.4.0 must pass the protected PR checks and tag workflow before its provenance/SBOM attestations can be claimed. The builder bundles the pinned solver-verifying runtime/stdlib, checksum, manifest, operations documents, release drill and CycloneDX SBOM. CI pins actions, Python, Rust, Z3 and the SIGIL ref (including its Lean build toolchain). The v0.3.0 distinct-version recovery drill passed on a clean Linux runner with host-provided pinned Z3; it is not evidence for v0.4.0. Mutable hosted-runner inputs, the incomplete transitive/runtime-library SBOM, release-tag protection, and independent clean-host operator qualification remain gaps. Historical byte-reproducibility results are retained in the local verification records, not asserted for the new artifact. | Publish and independently verify the new artifact, checksum and both attestations after protected CI; complete release-tag controls, dependency/reproducibility evidence and the exact-candidate clean-host install/upgrade/rollback qualification. |
| 2 | Authentication and tenant isolation | PARTIAL | `product_service.py` authenticates every v1 route, derives tenant identity only from the credential, namespaces sessions, separates chat/ops/schedule/data scopes, enforces per-principal tools against hostile model output, and requires TLS off-loopback. Product credentials require bounded validity windows; future/expired/unknown values fail identically; overlapping rolling-restart rotation is permitted only for identical principal policy, including scheduled ownership. Cross-tenant chat/schedule/export/delete and lifecycle tests pass. The legacy endpoint remains outside the product surface. External-IdP/live revocation are unsupported, and independent final-topology review remains absent. | Independently review cross-tenant behavior across every final state surface and the documented rolling-restart identity lifecycle; approve the static-policy boundary for v1. |
| 3 | Security assurance | PARTIAL | Host-injected secrets, capability manifests, fail-closed network grants, redirect defenses, path isolation, input bounds, hard turn deadlines, and signed audit chains have adversarial tests. Since 2026-08-22 the suite forges through the product client against a solver-verifying compiler, so every tool's Z3 obligations are discharged under test; before that it forged through SIGIL's bench client with the verification override set, and the product client — which strips that override — had never forged a real turn against the binary the gate builds. The general in-guest taint proof has a documented interprocedural gap but product startup excludes memory/guest-secret modes. Request/concurrency/daily-token/conversation+sandbox-storage/schedule/audit quotas are durably reserved and settled across supported workers and scheduled turns; exhaustion fails with stable errors and deletion credits state. The GA gate now requires candidate-bound raw security evidence and independently checks a dated independent reviewer, the exact threat-model digest, zero unresolved critical/high findings, no exceptions, and passed dependency plus exact-artifact scans. Default limits and all external security evidence still require approval/execution. | Approve the final quota values; run and publish exact-candidate artifact/dependency scans and an independent threat-model/security review with no unresolved critical/high findings. |
| 4 | Reliability and recovery | PARTIAL | Product state operations share POSIX session locks; schedules and quotas use cross-process transactions/leases. A hard deadline covers session/forge queueing and execution, kills wedged runtime work, audits the failure, preserves bounded partial state, releases capacity, and returns stable `504`; the killed compiler is retired and replaced on the next forge — with its `SIGIL_REV` pin re-checked — so an expired turn no longer denies service to other tenants, and readiness drops only when replacement itself keeps failing; startup requires leases to outlive it. Signals drain accepted work. Active retention re-checks under the session lock and removes its registry row last; interrupted-cleanup and retry tests pass. `state_tool.py` provides clean-drain, bounded, checksummed, signed-audit-verified offline backup/atomic restore plus locked managed backup creation/expiry; publication cannot overwrite, and expiry validates every managed digest/manifest/file invariant before deleting any file. It snapshots and integrity-checks the durable usage/activity ledger and rejects active leases. `config/failure-injection-matrix.json` binds all six required categories to collected tests. The bundled release drill enforces a <=15-minute backup age, <=4-hour restore-to-readiness, <=15-minute rollback, real old/new/old service probes, and non-vacuous committed-session continuity. The GA gate independently validates raw recovery evidence and all six final-topology failure categories, including state integrity, service recovery, and immutable raw-evidence links. A qualifying distinct-version recovery drill (v0.2.0->v0.3.0, published artifacts bound to `candidate.json`, real service probes on one local-POSIX worker) passed RPO 0.4s / RTO 0.20s / rollback 0.11s with one committed session preserved, independently rechecked by the evidence validator (`docs/evidence/recovery-drills.md`). Deployment of encrypted <=15-minute backup scheduling and approved expiry, and a candidate-bound final-topology failure drill, remain absent. | Prove the final topology under concurrency/restarts; deploy and exercise encrypted backups/expiry within RPO; run the six-category failure matrix against the exact release topology and publish its digest-bound report. (The distinct-version release recovery drill at RPO/RTO/rollback thresholds is DONE — `docs/evidence/recovery-drills.md`.) |
| 5 | Performance and resource control | PARTIAL | History/tool results, hard turn duration, per-tenant daily tokens, conversation+sandbox storage, schedules, and audit growth are bounded, with defined disconnect/deadline cancellation behavior. `docs/capacity.md` declares a proposed 25-turn forecast and exact 50-client/60-minute conjunctive gate. `scripts/load_test.py` enforces digest/forecast binding; separate single/tool latency; error/queue limits; tool, continuity, and isolation checks; and explicit memory/thread/FD/storage/audit/disk bounds. A single MCP connection still serializes forges. The forecast/resource bounds are not owner-approved and no qualifying report exists. | Obtain product/operations approval for the declaration and quota reservations, then pass and publish the digest-bound 60-minute run; prove forge serialization meets capacity and add aggregate metrics before claiming more than one worker. |
| 6 | Production observability | PARTIAL | Authenticated health/readiness/version endpoints plus JSON and Prometheus metrics, and secret-safe structured request logs include request IDs and failure classes. Readiness now fails conjunctively and reports fixed, content-free runtime, quota-store, schedule-store, state-storage, audit-verification, and retention component health; probe exceptions fail closed without diagnostics. Fixed-bucket turn-latency/queue histograms and bounded metrics cover request/error/turn/queue/token/tool/retry/dependency totals; aggregate tenant token/storage/audit usage and active reservations; and process peak memory, threads, FDs, state/audit/sandbox size, and disk capacity without customer labels. Startup/periodic monitors verify signed audit chains and enforce active retention; either failure drops readiness and emits a content-free critical event plus bounded metrics. `config/grafana-slo-dashboard.json` packages aggregate/component readiness, service-error, successful-turn, latency, queue, disk, lifecycle, activity, and resource views. `config/alert-policy.json` maps principal signals to exact thresholds, severity, owner role, and resolving procedures in `docs/runbooks.md`; tests bind exporter names, dashboard queries/thresholds, dependency labels, event paths, runbook anchors, and low-cardinality rules. Collector/dashboard deployment, multi-worker aggregation, routed/tested alert delivery, named human owners, and exercised runbooks remain absent. | Deploy the authenticated scrape and dashboard in the candidate environment, route and test the actionable policy, assign named owners, and exercise every principal runbook against the exact candidate. |
| 7 | Data lifecycle and privacy | PARTIAL | Authenticated, tenant-scoped export and immediate active-storage deletion cover conversations, sandbox files, schedules, and audit records under the same lock as chat/scheduling; deletion credits the durable aggregate ledger. An enforced, configurable provisional 90-day active-state policy sweeps at startup and hourly by default, verifies audit integrity first, re-checks activity under the session lock, handles schedule-only/legacy state, deletes the registry last, and exposes failure health/metrics. Per-tenant conversation+sandbox and audit quotas prevent unbounded active growth. Product memory is rejected. Offline backup/restore preserves these surfaces and the quota/activity ledger with schema/checksum/audit/SQLite validation and refuses overwrite. A locked managed cycle requires an explicit backup-retention duration and private directory, creates a digest/time-bound backup, and deletes only fully validated expired managed archives. `docs/state-compatibility.md` defines every schema surface, the no-implicit-migration rule, versioned migration requirements, and distinct unchanged-schema versus migrated-state rollback paths; shutdown and backup share one schema constant. Final duration/legal approval, deployed backup scheduling/deletion evidence, encryption-at-rest evidence, and digest-bound migration/recovery drills remain absent. | Approve and publish the active and backup retention/deletion SLA; deploy and exercise managed backup expiry on encrypted storage; then execute the documented backup, restore, upgrade, migration, and rollback compatibility drills on the exact candidate. |
| 8 | Stable product contract | PARTIAL | The `/v1` contract defines authentication, scopes, stable errors, request IDs, tenant/session rules, usage/limits, caller and provider retry rules, absence of idempotency keys, ambiguous-outcome reconciliation, deadline-only cancellation, chat, schedules, lifecycle, operations, TLS, and additive compatibility. Startup rejects weak/missing signing config, plaintext external bind, unknown tools/scopes, memory, and invalid bounds. A changelog, security-reporting process, state-compatibility contract, and candidate support matrix now exist. A license choice, final model/capacity/retention values, release notes for v1.0, verified private reporting channel, and named engineering/security/operations ownership remain absent. | Make and publish the remaining business/governance decisions, verify the reporting channel, and record named ownership/sign-off. |
| 9 | Quality gate | PARTIAL | Main `faf83b8` passed the full pinned-source gate in CI run 34020044494 on 2026-09-06: 788 collected cases, 787 passes, one strict research-only xfail, 91.11% line and 86.08% branch coverage, with every critical boundary below required at 100%. Both CI jobs became required main-branch checks on 2026-09-07. The v0.4.0 preparation adds one package-version guard and must pass a fresh protected PR run. `config/tool-test-matrix.json` is manifest-complete with happy, malformed, denied, boundary and end-to-end cases; `config/failure-injection-matrix.json` binds all six failure categories to collected tests. The suite covers adversarial evidence validation, runtime replacement, tenant/resource isolation, lifecycle/recovery, deterministic packaging, and observability contracts. Unit/integration coverage is not final-topology load/failure evidence or an independent defect review. | Pass the new protected CI/release runs; complete all candidate-bound release scenarios and zero-Sev-1/Sev-2 review. |
| 10 | Launch validation | EXTERNAL | No production-equivalent 30-day pilot evidence exists. | A continuous 30-day pilot meeting both SLOs, with no isolation/corruption/security breach, successful restore/rollback drills, bounded resources, and a runbook-driven simulated incident. |

## Critical security coverage inventory

`ci.sh` requires 100% line and branch coverage for these explicitly identified boundaries;
the ordinary >=85% global thresholds cannot average away a missed branch in them:

| Boundary | Enforced code |
|---|---|
| Verified forge/runtime integrity | All of `runtime_client.py` and `sigil_compose.py`: removal of the unverified-certificate escape hatch, bounded/matched MCP protocol responses, kill-on-deadline behavior, generational compiler replacement with per-generation re-verification, deterministic stdlib composition, and module-path containment |
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
| Backup/restore and rollback drills | `docs/evidence/recovery-drills.md` — distinct-version drill DONE (v0.2.0->v0.3.0); encrypted-backup scheduling and the failure drill remain |
| 30-day SLO dashboard | Linked immutable dashboard export in `docs/evidence/pilot.md` |
| API and operations documentation | `docs/api.md` and `docs/operations.md` |
| Engineering/security/operations sign-offs | `docs/evidence/release-signoff.md` |

## Gate command

The general-availability gate is `./product-ci.sh`. It runs the full pinned-source gate,
requires a non-development version, **verifies the published candidate named by
`docs/evidence/candidate.json` — re-hashing it and checking its inner manifest, `VERSION`, SBOM
SIGIL pin and payload parity — and never rebuilds it**, then validates substantive external
reports plus a digest-bound, dated, three-role sign-off.

The candidate is frozen deliberately. While the gate rebuilt it from the working tree, the digest
every evidence artifact binds to moved whenever this record was edited, so recording a result
invalidated the evidence for it. This document is therefore not packaged inside the release; its
canonical location is this repository at the version tag. It fails closed today
because the external evidence does not exist. The validator can check structure and binding;
independence, authenticity, and adequacy remain human release-review responsibilities.
