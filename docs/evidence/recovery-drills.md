# Recovery drill — v0.4.0 → v0.5.0 → v0.4.0

Status: **passed the controlled recovery drill, nonempty legacy-state verification and
independent evidence validator**. The report is qualification-eligible for its one-worker,
local-POSIX, synthetic-state scope. This is not a complete product-readiness sign-off.

The fixture now contains two nonempty files written by v0.4.0's real `write_file` tool and
two schedules created through its API. Both schedules reached the loopback provider with
their exact messages; their committed user/reply pairs were verified in the backup after
a clean drain. Both upgrade and online preparation executed the
sandbox layout 1 → 2 and schedule store schema 1 → 2 migrations. Rollback restored the
checked, read-only pre-migration archive into a fresh path with v0.4.0's own tool.
The frozen v0.5.0 operator guide describes the initial conversation-only fixture; this
workflow's opt-in nonempty fixture extends that example. The documented migration and
rollback commands are unchanged.

- Test date (UTC): **2026-11-18**.
- Candidate: **v0.5.0**, SHA-256 `136dc95ac105f76ffb46a67b27a83ef8029a965140e303c6dd8a5d97f8dbb5eb`.
- Rollback: **v0.4.0**, SHA-256 `6f17f2f0a91c9f9cdc50c4cb6d0d50f2c305c55162180c933c0604f556407203`.
- Inputs: unchanged published assets bound to `candidate.json`, not rebuilt artifacts.
- Topology: one worker, local POSIX state, Linux x86_64, CPython 3.12.11, pinned host Z3.
- Provider: an Anthropic-shaped loopback stand-in; no real provider requests or customer state.
- Workflow: https://github.com/nxrobins/sigil-pi/actions/runs/44364212495
- Workflow source on main: `27831d45eed4be19d514ff30e1cbabe57bc68edb`; both `drill` and `attest` jobs passed.
- Raw signed report SHA-256: `47232e4b91106d80388f3be3b83254bf6c1736f467f39d832c13dac48e4d615d`.
- Fixture SHA-256: `3ce2f3277b1c205d332b4bc01fe027551f6ee7b85174483581ca5d3a58a94136`; included in the signed report. The downloaded fixture
  is retained byte for byte as `recovery-drill-fixture.json`.
- Retained attestation bundle SHA-256: `8a8de55a6999ee0e6aa4b3c080b2de0fb09edce5c3ad5d83ebead3059d8b484e`.

## Measured results

| Property | Measured | Required limit |
|---|---:|---:|
| Restore to first successful turn | 0.442216 s | ≤ 14400 s |
| Rollback to first successful turn | 0.438111 s | ≤ 900 s |
| Initial backup age | 0.870783 s | ≤ 900 s |
| Online backup age at restore | 1.717543 s | ≤ 900 s |
| Online restore to first successful turn | 1.665642 s | ≤ 14400 s |
| Longest online session-lock hold | 0.734 ms | Recorded; no separate release threshold |
| Original committed customer files preserved | 4 | None lost or changed |
| Legacy sandbox files / schedules migrated | 2 / 2, in each of two migrations | Exact preservation |

Both failure lists are empty. Each phase committed a real turn through its release's service
and compiler. Restored phases read the held session and sent its complete history:

| Phase | Release | Messages before | History messages sent | Messages after | Audit records before → after |
|---|---|---:|---:|---:|---:|
| `clean_install` | 0.4.0 | 0 | 0 | 2 | 0 → 2 |
| `restore_old` | 0.4.0 | 8 | 8 | 10 | 10 → 12 |
| `upgrade_new` | 0.5.0 | 10 | 10 | 12 | 12 → 16 |
| `rollback_old` | 0.4.0 | 10 | 10 | 12 | 12 → 14 |
| `backup_online_new` | 0.5.0 | 12 | 12 | 14 | 14 → 18 |
| `restore_online_new` | 0.5.0 | 16 | 16 | 18 | 22 → 26 |

The online backup held exactly **16 messages and 22 audit records**;
the restored service read that same pair before its next turn. The backup overlapped
one of two answered turns. Rollback continued the pre-migration conversation; the candidate's
intervening turn was deliberately not carried back. Timings describe small synthetic state.

## Nonempty migration evidence

| File | Bytes | SHA-256 |
|---|---:|---|
| `migration-first.txt` | 52 | `631a1b7388128c32446cdaabeb6257416f00df6eee87a0a6114c21f58911887e` |
| `migration-second.txt` | 38 | `2387255cf8994689759756d3047f1986cf0efda78ffc7aef2287e9ebf82d10e8` |

The `migration-daily` and `migration-weekly` schedules preserve their distinct messages,
intervals (86400000 and 604800000 ms), tenant/principal/session identities, creation/update
and last-run timestamps, successful status and empty leases. Their long intervals keep
them from becoming due again during recovery. This run proves preservation, not execution
of a migrated schedule under a newly provisioned principal.

`state_fixture_verification` records eight independent persisted-state observations:
old restore, new upgrade, old rollback, online source, online restore, online archive, and
both pre-migration archives. Every observation contains the exact file sizes/digests and
complete normalized schedule fields. Each migration reports one sandbox move and two
schedule rows, and both pre-migration archives were checked as mode `0400`:

| Stage | Pre-migration archive | SHA-256 |
|---|---|---|
| upgrade | `pre-migration-state.tar.gz` | `11e7e050c70f479320437a6d35d259ae5fc71711c2a5be3ba1e17c8aa51488e3` |
| online | `online-pre-migration-state.tar.gz` | `669312b0031122ecced6bcb88e62cac8f44778ace89b6afea83abda67449088c` |

Hinoki reviewed the fixture, real-bundle rehearsal and verifier. Its two findings were
fixed: migrate-capable candidates must declare both formats explicitly, and scheduled
turns must be independently observed at the provider and in the archived conversation.
Local tests included nine deliberately wrong outcomes rejected by the verifier; all 115
affected tests and documentation checks passed. Both protected Linux checks passed on
`9166c18db72279d4876f5d81cf199ddacd1756c9` in
[CI run 44360635812](https://github.com/nxrobins/sigil-pi/actions/runs/44360635812)
before [PR #55](https://github.com/nxrobins/sigil-pi/pull/55) was merged. The full gate's
line and branch coverage were 94.83% and 92.05%. This drill then used the published Linux assets.

## Independent checks and signed bytes

The downloaded report passed `scripts/check_readiness_evidence.py::_validate_recovery_report`
against the exact frozen candidate and rollback digests. Additional independent checks bound
the fixture digest and backup to the report, required both actual migrations, compared all
eight observations with the fixture, checked rollback's pre-migration digest and fresh path,
matched the online archive's message/audit pair to its restore, and required the fixture's
two scheduled-turn observations to name the saved schedules and confirm provider receipt
and committed replies. The workflow's producer makes those observations; the report binds
their exact fixture bytes by digest.

The workflow appended the persisted-state observations before hashing and signing the
report. The downloaded report and fixture were copied without reformatting.
`gh attestation verify` succeeded against the retained bundle, constrained to the exact repository,
`recovery-drill.yml`, `refs/heads/main`, source commit, SLSA provenance and GitHub-hosted
runners. The verified certificate's complete signer URI, issuer, source repository/ref/digest,
runner environment and invocation were compared for exact equality. The invocation is
`https://github.com/nxrobins/sigil-pi/actions/runs/44364212495/attempts/1`. To repeat:

```sh
gh attestation verify docs/evidence/recovery-drill.json --repo nxrobins/sigil-pi \
  --bundle docs/evidence/recovery-0.5.0-attestations.jsonl \
  --signer-workflow nxrobins/sigil-pi/.github/workflows/recovery-drill.yml \
  --source-ref refs/heads/main --source-digest 27831d45eed4be19d514ff30e1cbabe57bc68edb \
  --predicate-type https://slsa.dev/provenance/v1 --deny-self-hosted-runners
```

## Limits and history

Physical power loss, real-provider behavior, production load, deployed encrypted backup
scheduling/expiry and the six-category failure drill remain unproven by this run. No human
engineering/security/operations launch sign-off is asserted, and no whole product-readiness
area is promoted to PASS.

The first v0.5.0 run, which had no nonempty legacy state and performed only migration
preflight, retains its original signed bytes and limits under
[history/v0.5.0-initial](history/v0.5.0-initial/recovery-drills.md). Earlier releases remain
under [history/v0.4.0](history/v0.4.0/recovery-drills.md) and
[history/v0.3.0](history/v0.3.0/recovery-drills.md); see the [history index](history/README.md).
