# Recovery drill — v0.2.0 → v0.3.0

Status: **qualifying distinct-version recovery drill.** This is the evidence for
product-readiness area 4's release-recovery requirement. It is not, by itself, the
whole of area 4: deployed encrypted backup scheduling within RPO and a candidate-bound
final-topology failure drill remain separate.

- Test date (UTC): **2026-08-25**
- Test identity: `sigil-pi-v1-release-recovery`, schema 1
- Candidate (new) artifact: **v0.3.0**, SHA-256 `be060867b473b7858581a4fc8623d8db0120cce82ba4067c0fe76e21ca8f0b78`
- Rollback (old) artifact: **v0.2.0**, SHA-256 `eec63a412cd19f67de668cd1ed3c868b1a66248092b236820623a1d4df064943`
- Both artifacts are the **published** GitHub release assets, not local builds. The candidate
  is the exact archive named by `docs/evidence/candidate.json`; the old release is the
  `rollback_from` target that record names. The workflow verifies both against the frozen
  record before drilling anything.
- Topology: 1 worker, `local-posix` state, real service probe =
  True. Host: `Linux-6.17.0-1022-azure-x86_64-with-glibc2.39`, Python 3.12.11.
- Raw evidence: `recovery-drill.json` (SHA-256 `b0c7a61ef7cd5c74fbf761532b368375e62042dcf6f63e3b99a0f0b28a705005`), produced by
  `scripts/release_drill.py` and rechecked independently by
  `scripts/check_readiness_evidence.py`. The pre-upgrade backup's provenance is
  `recovery-drill-fixture.json`.

## Result: qualification-eligible, no failures

| Property | Measured | Threshold |
|---|---:|---:|
| Restore-to-readiness (RTO) | 0.200 s | ≤ 14400 s (4 h) |
| Rollback-to-readiness | 0.106 s | ≤ 900 s (15 min) |
| Backup age at drill (RPO) | 0.370 s | ≤ 900 s (15 min) |
| Committed session files preserved | 1 (lost/changed: 0) | ≥ 1, none lost |

- `qualification_eligible`: **True**; `qualification_failures`:
  none.
- Phases, in order, each booting the installed release and probing `/v1/ready` then
  `/v1/version`: clean_install, restore_old, upgrade_new, rollback_old.

## Procedure

1. A clean Linux runner installs the pinned Z3 shared library into the loader path and
   downloads both published releases.
2. The inputs are bound to the frozen candidate record: `verify_release` re-hashes the
   candidate, checks its inner manifest, VERSION, SBOM SIGIL pin and payload parity, and the
   old release is confirmed to be the recorded `rollback_from` target by digest.
3. `scripts/drill_fixture.py` produces the pre-upgrade backup honestly: it boots the OLD
   release, runs one real `/v1/chat` turn (committing a session, sandbox, signed audit chain
   and durable quota rows through the release's own forge path, against a loopback
   Anthropic-shaped mock provider), drains cleanly, and backs up with the old release's own
   `state_tool.py`. The backup's age is therefore genuinely seconds, not a fixture timestamp.
4. `scripts/release_drill.py` installs both releases, atomically switches a `current` link,
   and runs the four phases above against real embedded-runtime services, re-backs up the
   final state, and byte-compares every committed session/sandbox/audit file with the input
   backup.

## Exceptions

None.

## Reviewer

Produced and independently machine-validated in CI (GitHub Actions run 32879239427). Human
release-review sign-off is recorded separately in `release-signoff.json` and is not asserted
here.
