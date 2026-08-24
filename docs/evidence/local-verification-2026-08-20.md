# Local candidate verification — 2026-08-20

Status: **mechanics evidence only; not release approval**.

- Candidate version: `0.1.0-dev`
- Platform: `darwin-arm64`
- Pinned SIGIL commit: `eb9f1715dd3fadd3c9fe1f6ccb19c17f49a2ac0b`
- Candidate archive SHA-256:
  `b5812d391144a9288873a7734cb45b1623ad9673dc649ab4cd99a23887b44220`
- Candidate SBOM SHA-256:
  `d8cc2a15d6edcf66e008ea29c57e7b96d2f23d5fcc078d5e3443f9951b059eca`
- Local mandatory gate: 629 passed, 1 strict research-only xfail; 92.29% line and
  87.10% branch coverage. Every explicitly inventoried critical security boundary passed
  its separate 100% line-and-branch requirement; every production tool has manifest-bound
  happy, malformed, denied-capability, boundary, and end-to-end evidence. The exact six-class
  failure-injection inventory is also machine-checked against collected regression tests;
  local full-disk and interrupted-commit injections preserve prior session/schedule state,
  contain diagnostic details, and release request capacity.
- Reproducibility: two fresh invocations of `scripts/build_release.py` against the same
  pinned, clean runtime produced byte-identical archives and SBOM sidecars. The archive
  includes the authenticated Prometheus exporter contract, Grafana SLO dashboard, alert
  policy, runbooks, atomic non-overwriting backup/restore and managed-expiry mechanics, and
  their manifest checksums. The candidate's bundled `backup-cycle` created a private,
  digest/time-bound archive from the fresh stopped-state fixture before the recovery drill.

The candidate is derived from the current uncommitted working tree. The build directories
were `/private/tmp/sigil-pi-candidate-20260820-backup-retention-a` and
`/private/tmp/sigil-pi-candidate-20260820-backup-retention-b`; they are local and ephemeral,
not an evidence-retention system.

- Release-recovery mechanics: the bundled launcher performed real authenticated embedded-
  runtime probes for clean install, restore, upgrade, and rollback on local POSIX state. All
  four processes became ready and shut down cleanly; restore-to-readiness was 0.198881 seconds,
  rollback-to-readiness was 0.125824 seconds, and two committed customer files including one
  session were byte-preserved. Raw report:
  `local-recovery-mechanics-2026-08-20.json` (SHA-256
  `b1b0a6c6d993a67ea29503563c17079af2ca30d7f93780bbfc77902b40823924`). It is intentionally
  not qualification-eligible because both inputs were the same `0.1.0-dev` artifact.

This does not satisfy the protected clean-CI/tag run, signed provenance/SBOM attestation,
qualifying distinct-version clean-host install/upgrade/rollback, independent review,
qualifying 60-minute load run,
recovery/failure drills, named sign-offs, or 30-day pilot gates. The general-availability
gate therefore remains intentionally closed.
