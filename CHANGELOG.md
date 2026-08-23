# Changelog

All notable product-boundary changes are recorded here. The format follows Keep a
Changelog; version numbers follow Semantic Versioning once a non-development release exists.

## [Unreleased]

### Added

- Authenticated, tenant-namespaced `/v1` chat, operations, scheduling, export, and deletion
  APIs with distinct scopes and stable errors.
- Fail-closed TLS startup rules, cross-worker session/schedule locks and leases, durable
  request/concurrency quotas, graceful draining, and automatic signed-audit verification.
- Deterministic release bundle with pinned SIGIL runtime/stdlib, checksum, internal manifest,
  CycloneDX SBOM, commit-pinned CI actions, and tag provenance/SBOM attestation workflow.
- Validated offline backup/restore and an independent 85% line / 85% branch host coverage
  gate.

### Changed

- Product runtime no longer imports benchmark helpers and strips the benchmark-only
  unverified-certificate override from the compiler child.
- The forge is resolved through `toolchain.py` (explicit path, installed release, or source
  checkout) by both hosts, and both forge through the vendored `runtime_client`; SIGIL's
  bench client is no longer imported anywhere at runtime. The research host additionally
  carries a single shared bearer token, a state-directory lock, `/health`, a step-boundary
  turn budget, and SSE step events (upstream PRs #22–#27); none of these is the product
  contract.
- Product memory remains disabled until its complete quota, export, retention, and recovery
  lifecycle exists.

### Fixed

- The release candidate is frozen rather than rebuilt at gate time. `product-ci.sh` used to
  rebuild it from the working tree, so the digest every evidence artifact binds to moved whenever
  the readiness record was edited — recording a passing result invalidated the evidence for it.
  The gate now verifies the published archive named by `docs/evidence/candidate.json`, and the
  readiness record is no longer packaged inside the artifact it describes.
- One tenant's turn hitting the hard deadline no longer ends forging for every other tenant.
  The killed compiler is retired and replaced on the next forge — re-verified against
  `SIGIL_REV` each time — instead of latching a closed client for the life of the process.

### Security

- Tool authorization is enforced against hostile model output, not only advertised tool
  schemas.
- The gate and the release builder compile a solver-verifying compiler (`--features
  sigil-mcp/solver`), and the suite forges through the product client, which strips the
  benchmark-only verification override; a solver-off build fails closed at the first forge.
- Production audit records require a separately injected HMAC key and are verified before
  readiness.

## [0.1.0-dev] - 2026-08-20

- Internal-alpha release candidate. Not approved for general production availability.
