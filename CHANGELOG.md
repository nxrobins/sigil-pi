# Changelog

All notable product-boundary changes are recorded here. The format follows Keep a
Changelog; version numbers follow Semantic Versioning once a non-development release exists.

## [0.3.0] - 2026-08-24

### Added

- `docs/evidence/candidate.json`: the first frozen-candidate record, written from the published
  v0.2.0 asset with `rollback_from` = v0.1.0. With it, `./product-ci.sh` runs end to end for the
  first time and fails closed at the first genuinely external evidence report.
- `scripts/drill_fixture.py`: produces the recovery drill's pre-upgrade backup honestly — boots
  the old release, commits one real turn through its own forge path against a local
  Anthropic-shaped mock provider, drains cleanly, and backs up with the old release's own
  `state_tool.py`.
- `.github/workflows/recovery-drill.yml` (dispatch-only): the qualifying distinct-version drill
  on a clean Linux runner, bound to the frozen candidate and its recorded rollback target before
  anything is drilled, with the pinned `libz3` installed into the loader's default path.

### Fixed

- `ARCHIVE_NAME_RE` lacked `_` and rejected every real Linux artifact
  (`...-linux-x86_64.tar.gz`) on first contact; the fixtures' `test-platform` tag had hidden it.

### Notes

- The payload-parity gate caught its first real drift: documenting the drill workflow edited
  `docs/operations.md`, so the v0.2.0 archive's operator documents no longer matched the
  repository and `verify_release` refused it — which is why this release exists. An edit to any
  parity-checked operator document requires a new candidate; that is the freeze working, not
  overhead to be optimized away.

## [0.2.0] - 2026-08-23

No functional changes from 0.1.0. This release exists so that a release with a
recorded rollback target can exist: `docs/evidence/candidate.json` requires
`rollback_from` to name a distinct published release, the recovery drill requires
two distinct versions, and 0.1.0 was the first release — so nothing could roll
back to anything. 0.2.0 names 0.1.0 as its rollback target, which is what makes
the frozen-candidate record writable and the qualifying distinct-version
rollback drill possible for the first time.

## [0.1.0] - 2026-08-23

### Added

- Authenticated, tenant-namespaced `/v1` chat, operations, scheduling, export, and deletion
  APIs with distinct scopes and stable errors.
- Fail-closed TLS startup rules, cross-worker session/schedule locks and leases, durable
  request/concurrency quotas, graceful draining, and automatic signed-audit verification.
- Deterministic release bundle with pinned SIGIL runtime/stdlib, checksum, internal manifest,
  CycloneDX SBOM, commit-pinned CI actions, and tag provenance/SBOM attestation workflow. The
  attestation steps are skipped, with a recorded warning, while the repository is user-owned and
  private — GitHub does not persist attestations for those — so a release cut today is
  deliberately unattested and cannot satisfy the reproducible-release readiness area.
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

- Internal designation used while the product boundary was being built. Never tagged and
  never published; superseded by 0.1.0, which is the first tagged release.
