# Recovery drill — v0.3.0 → v0.4.0

Status: **qualifying distinct-version recovery drill.** This proves the current candidate's
controlled release-recovery requirement, not all of product-readiness area 4. Deployed
encrypted backup scheduling within RPO and the six-category final-topology failure drill
remain separate requirements.

- Test date (UTC): **2026-09-07**.
- Candidate: **v0.4.0**, SHA-256 `6f17f2f0a91c9f9cdc50c4cb6d0d50f2c305c55162180c933c0604f556407203`.
- Rollback target: **v0.3.0**, SHA-256 `be060867b473b7858581a4fc8623d8db0120cce82ba4067c0fe76e21ca8f0b78`.
- Inputs: the published GitHub assets bound to `candidate.json`, not local rebuilds.
- Topology: one worker, local POSIX state, Linux x86_64, CPython 3.12.11, real service probes.
- Raw run: https://github.com/nxrobins/sigil-pi/actions/runs/34145661500
- Raw `recovery-drill.json` SHA-256:
  `c7426abb78bbd0b431e97971fa6049a704f0e06a8c843be71cf0a4788a3bbd09`.
- Fixture provenance `recovery-drill-fixture.json` SHA-256:
  `9e623f262a941730c2227c6f604ee2502c606c3fa4c2899fec82aee2e5328ad0`.
  This is the run's `fixture-evidence.json`, retained byte-for-byte under the evidence name.

## Results

| Property | Measured | Required limit |
|---|---:|---:|
| Restore-to-readiness | 0.200665 s | ≤ 14400 s |
| Rollback-to-readiness | 0.105542 s | ≤ 900 s |
| Backup age at drill | 0.942863 s | ≤ 900 s |
| Committed customer files preserved | 2, including 1 session file | ≥ 1 session file, none lost/changed |

`qualification_eligible` is true, and both qualification and mechanics failure lists are
empty. The four installed-service phases passed in order: clean_install, restore_old,
upgrade_new, rollback_old. Each phase probes `/v1/ready` and `/v1/version`.

## Procedure and validation

1. The clean Linux runner installs pinned Z3 and downloads both published releases.
2. The candidate is re-hashed and manifest/version/SBOM/payload-verified against the frozen
   record. The old archive must match that record's rollback digest.
3. The old release commits a real chat turn through its own forge against a loopback,
   Anthropic-shaped mock provider. It drains and creates its own fresh state backup.
4. The drill installs and probes old/new/old releases, restores state, and compares committed
   session/sandbox/audit files against the backup.
5. The downloaded raw report was independently checked locally with
   `scripts/check_readiness_evidence.py::_validate_recovery_report`, including the exact
   candidate and rollback bindings, all timing limits, topology, phase ordering and continuity.

This is a small synthetic-state drill, not a production-scale RTO measurement or evidence of
a deployed 15-minute backup schedule. No schema migration is claimed.

## Review and history

No exceptions were reported. These are automated execution and independent machine-validation
results; human release/security/operations sign-offs are not asserted.

The preceding v0.2.0 → v0.3.0 drill and its original record/raw files are retained unchanged in
[history/v0.3.0](history/v0.3.0/recovery-drills.md).
