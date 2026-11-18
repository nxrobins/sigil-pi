# Recovery drill — v0.4.0 → v0.5.0 → v0.4.0

Status: **passed the controlled recovery drill and independent evidence validator**.
The report is qualification-eligible for its one-worker, local-POSIX, synthetic-state
scope. This is not a complete product-readiness sign-off.

**Migration limitation:** the fixture contained one conversation and its audit chain,
with no sandbox files or schedules needing conversion. The candidate declared `state
migration required`, but `migrate --dry-run` found no work; no actual migration or
pre-migration backup ran. This report proves migration preflight and rollback from the
pre-upgrade backup, not migration of nonempty legacy sandboxes or schedule rows.

- Test date (UTC): **2026-11-18**.
- Candidate: **v0.5.0**, SHA-256 `136dc95ac105f76ffb46a67b27a83ef8029a965140e303c6dd8a5d97f8dbb5eb`.
- Rollback: **v0.4.0**, SHA-256 `6f17f2f0a91c9f9cdc50c4cb6d0d50f2c305c55162180c933c0604f556407203`.
- Inputs: the published release assets bound to `candidate.json`, not rebuilt artifacts.
- Topology: one worker, local POSIX state, Linux x86_64, CPython 3.12.11, pinned host Z3.
- Provider: an Anthropic-shaped loopback stand-in; no real provider requests or customer state.
- Workflow: https://github.com/nxrobins/sigil-pi/actions/runs/44282969234
- Workflow source on main: `23611e7ef80eab879bbc6627f6505fa83dd965ea`; both `drill` and `attest` jobs passed.
- Raw report SHA-256: `cb1682a1b6b13d27afac73cb899f9b41b02268e00bf5a9cdb4dc71f60584ba02`.
- Fixture SHA-256: `16a3636f83be6884276e8733636fde4afca37381d0cc44e5f2914c91fb7b396f`. The run's `fixture-evidence.json` is retained
  byte for byte as `recovery-drill-fixture.json`.
- Retained attestation bundle SHA-256: `03c6d0c43df8c7222fd5e0afd4eb1781229fd0e2bd611057b323681364f4421c`.

## Measured results

| Property | Measured | Required limit |
|---|---:|---:|
| Restore to first successful turn | 0.576472 s | ≤ 14400 s |
| Rollback to first successful turn | 0.572512 s | ≤ 900 s |
| Initial backup age | 0.169923 s | ≤ 900 s |
| Online backup age at restore | 2.374933 s | ≤ 900 s |
| Online restore to first successful turn | 0.953474 s | ≤ 14400 s |
| Longest online session-lock hold | 1.089 ms | Recorded; no separate release threshold |
| Original committed files preserved | 2, including 1 conversation file and its audit chain | None lost or changed |

Both failure lists are empty. Each phase committed a real turn through its release's
service and compiler. Restored phases read the held session and sent its complete history:

| Phase | Release | Messages before | History messages sent | Messages after | Audit records before → after |
|---|---|---:|---:|---:|---:|
| `clean_install` | 0.4.0 | 0 | 0 | 2 | 0 → 2 |
| `restore_old` | 0.4.0 | 2 | 2 | 4 | 2 → 4 |
| `upgrade_new` | 0.5.0 | 4 | 4 | 6 | 4 → 8 |
| `rollback_old` | 0.4.0 | 4 | 4 | 6 | 4 → 6 |
| `backup_online_new` | 0.5.0 | 6 | 6 | 8 | 6 → 10 |
| `restore_online_new` | 0.5.0 | 10 | 10 | 12 | 14 → 18 |

Rollback restored the old release's exact `pre-upgrade-state.tar.gz` digest into a new
`rollback-state` path. It continued the four-message/four-audit-record pre-upgrade session;
the candidate's intervening turn was deliberately not carried back.

The online backup overlapped one of two answered turns. It held exactly **10 messages
and 14 audit records**; the restored service read that same pair before its next turn.
The archive preserved all captured committed files. The report records the effective
candidate settings and the explicit fixture/drill wiring; the old release does not report
its settings. These timings are for small synthetic state, not a production-scale RTO.

## Independent checks and signed bytes

The downloaded report passed `scripts/check_readiness_evidence.py::_validate_recovery_report`
against the exact frozen candidate and rollback digests. Additional inspection checked the
migration declaration/dry-run result, pre-upgrade backup digest, fresh rollback path,
session continuity and exact online message/audit-record pair. The fixture's artifact,
backup digest, tenant and session match the report. Raw files were copied without reformatting.

`gh attestation verify` succeeded against the retained bundle, constrained to the exact
repository, `recovery-drill.yml`, `refs/heads/main`, source commit above, SLSA provenance and
GitHub-hosted runners. The verified certificate's complete signer URI, issuer, source
repository/ref/digest, runner environment and invocation were compared for exact equality.
The invocation is `https://github.com/nxrobins/sigil-pi/actions/runs/44282969234/attempts/1`. To repeat:

```sh
gh attestation verify docs/evidence/recovery-drill.json --repo nxrobins/sigil-pi \
  --bundle docs/evidence/recovery-0.5.0-attestations.jsonl \
  --signer-workflow nxrobins/sigil-pi/.github/workflows/recovery-drill.yml \
  --source-ref refs/heads/main --source-digest 23611e7ef80eab879bbc6627f6505fa83dd965ea \
  --predicate-type https://slsa.dev/provenance/v1 --deny-self-hosted-runners
```

## Limits and history

Nonempty legacy-state migration, physical power loss, real-provider behavior, production
load, deployed encrypted backup scheduling/expiry and the six-category failure drill remain
unproven by this run. No human engineering/security/operations launch sign-off is asserted,
and no whole product-readiness area is promoted to PASS.

Earlier candidate and recovery records retain their original bytes under
[history/v0.4.0](history/v0.4.0/recovery-drills.md) and
[history/v0.3.0](history/v0.3.0/recovery-drills.md); see the [history index](history/README.md).
