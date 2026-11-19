# Release evidence directory

This directory contains the published, attested v0.5.0 candidate. See `release-0.5.0.md`
for independent artifact and attestation verification. Its signed recovery drill with rollback
to v0.4.0 passed with two nonempty sandbox files and two schedules: both migrations
executed, and their bytes and metadata survived rollback and online restore.
`recovery-drills.md` records the measurements and limits. The initial v0.5.0 run remains
under `history/v0.5.0-initial/`; prior releases remain under `history/v0.4.0/` and
`history/v0.3.0/`.
The v0.4.0 failure drills in `failure-investigation-0.4.0.md` and `failure-runs/` remain
historical, non-qualifying evidence. The signed v0.5.0 six-category failure drill now
passes for one-worker/local-POSIX synthetic state, including 503 readiness under full disk.
See `failure-injection.md`, the exact raw report and `failure-0.5.0-attestations.jsonl`.
This controlled run does not qualify the eventual deployment's infrastructure or load.
The expanded failure fixture now requires two bystander sessions, two nonempty sandbox
files in each of three sessions, and two API-created schedules whose first runs completed
before injection. The currently retained report predates this expansion and does not
establish that coverage; a new main-branch run and signed report are required.
`product-ci.sh` fails closed until substantive reports for the exact candidate are added:

- `security-review.md`
- `security-review.json` (candidate-bound findings, threat-model review, and scan results)
- `load-test.md`
- `load-test.json` (raw output from `scripts/load_test.py`)
- `failure-injection.md`
- `failure-injection.json` (raw signed six-category results for the recorded topology, produced by
  `scripts/failure_drill.py` against the published candidate)
- `recovery-drills.md`
- `recovery-drill.json` (workflow output: the release drill plus persisted-state verification,
  augmented before hashing and signing; retain the downloaded bytes exactly)
- `recovery-drill-fixture.json` (provenance of the pre-upgrade backup: the real turn that committed it; not read by the validator, kept so the backup's freshness is auditable)
- `pilot.md`
- `release-signoff.json`
- `candidate.json`

## The candidate is frozen, not rebuilt

`candidate.json` names the one published archive all of this evidence is about: schema version,
version, tag, platform, file name, SHA-256, the SIGIL pin it was built from, the published asset
URL, and the `rollback_from` release a recovery drill must restore.

This exists because the gate used to REBUILD the candidate from the working tree, which made the
digest a function of the repository: appending one line to `docs/product-readiness.md` moved it
(measured 2026-08-23), so recording a passing result invalidated the evidence for it. `README.md`
did the same for a duller reason — its test count changes whenever a test is added. `product-ci.sh`
now verifies the published archive instead: re-hashing it and checking its inner manifest, its
`VERSION`, its SBOM's SIGIL pin, and payload parity for the documents an operator acts on.

The order matters, and it is not the intuitive one:

1. tag the version;
2. let the release workflow build, publish and attest it;
3. fill `candidate.json` from the **published asset** — never a local build or a workflow
   artifact, since a record generated beside a rebuild always agrees with it;
4. collect evidence against that artifact;
5. run `./product-ci.sh`.

Two consequences worth stating. The gate deliberately asserts no relationship between the
candidate and the tree's *current* `SIGIL_REV`: a toolchain bump during a pilot must not fail a
candidate that was built correctly before it. And an edit to any parity-checked operator document
(`docs/api.md`, `docs/operations.md`, `docs/runbooks.md`, `docs/security/threat-model.md`,
`docs/state-compatibility.md`) requires a new candidate and a fresh evidence run — that is the
price of the archive and the repository agreeing about what an operator is told to do.

`docs/product-readiness.md` is deliberately NOT packaged: its in-bundle copy would say
"NOT READY — internal alpha" for the life of the release. Its canonical location is this
repository at the version tag.

Reports must identify the artifact SHA-256, test date, topology, procedure, raw evidence
location, result, exceptions, and reviewer. `release-signoff.json` schema version 1 binds the
same digest to protected CI, signed provenance/SBOM attestations, a dashboard, zero unresolved
Sev-1/Sev-2 defects, a dated 30+ day pilot meeting both SLOs and all safety/drill assertions,
and named engineering, security, and operations sign-offs.

Placeholders do not pass: each report must be substantive and the sign-off validator checks
the exact candidate digest, dates, SLO thresholds, required drill/safety booleans, HTTPS
evidence references, and all three roles. The two drill reports, `failure-injection.json`
and `recovery-drill.json`, are evidence only as the bytes their workflow run signed: each
drill workflow signs its report in its `attest` job, and the validator asks GitHub for an
attestation of the committed file's exact bytes, signed by that drill's workflow
(`.github/workflows/failure-drill.yml` or `recovery-drill.yml`) in `nxrobins/sigil-pi`, run
from `main` on a GitHub-hosted runner, as SLSA provenance. So commit a drill report byte for
byte as the run's artifact holds it (`gh run download <run-id> -n <artifact> -D <dir>`, then
copy the JSON file; do not reformat it), and run the gate where `gh` is installed and signed
in, with the network. To check one report by hand:

    gh attestation verify docs/evidence/recovery-drill.json --repo nxrobins/sigil-pi \
      --signer-workflow nxrobins/sigil-pi/.github/workflows/recovery-drill.yml \
      --source-ref refs/heads/main --predicate-type https://slsa.dev/provenance/v1 \
      --deny-self-hosted-runners

(`failure-injection.json` with `failure-drill.yml` likewise.) The gate checks the drill
reports before the documents people write and prints `attested: <report> (sha256 ...) signed
by ... in <run>` for each. Independence and adequacy, and the authenticity of the documents
and sign-offs people write, still remain human release-review responsibilities.

`recovery-drill.json` is produced by the `Recovery drill` workflow against the published
artifacts (see `docs/operations.md`); retain the run's artifact and cite the run URL as the raw
evidence location. It must be qualification-eligible and bind a distinct old release plus the
exact candidate digest to real one-worker/local-POSIX service probes in this order: clean
install, old-release restore, candidate upgrade, old-release rollback. The validator
independently enforces a backup age no greater than 15 minutes, RTO no greater than four
hours, rollback no greater than 15 minutes, and non-vacuous preservation of committed session
files.

Each restored-state phase's `turn` records that the release read the session and ran a turn in it
(`read_before_turn`, the message and audit-record counts before and after, the requests the
stand-in provider received and how much of the conversation they carried).
`evaluation.restore_rto_seconds` and `rollback_seconds` are measured to the first successful
turn (`evaluation.measured_to`). `configuration` records the wiring, the stand-in provider,
the session, and the settings each release reported as in force; a candidate that reports
none is not qualification-eligible. The run's artifact also holds `fixture-evidence.json`
(which now names the tenant and session of the committed turn) and the service logs. The
validator requires every phase to commit a turn. Restored-state phases must first read
the session and send its complete history; `clean_install` starts without a session.

`online_backup` records the online backup the candidate took while it served turns, and its
restore: `mode` (`online`), `created_unix` (when the backup began; backup age counts from it),
`age_at_restore_seconds` (from then to the start of its restore, at most
`thresholds.maximum_backup_age_seconds` to qualify), `max_session_lock_hold_ms` (the longest
time it held one session's lock, from its manifest, whose whole capture report is in
`capture`), `backup_seconds`, `turns_while_backing_up` (how many turns ran, that the first was
in flight when the backup started, how many overlapped it and how many the archive holds),
`session_in_backup`, `continuity` (every committed file of the state it was taken from is in
the archive), `restore_rto_seconds` (to the first successful turn on the restored state,
`measured_to`) and, in `phases`, the candidate's two starts, `backup_online_new` and
`restore_online_new`, each with its `turn`. The validator independently checks
`online_backup`, including artifact bindings, timing, overlapping turns, preserved
sessions and a successful turn after restore. The drill itself checks the archive's
exact message/audit-record pair against its per-turn reads of the session files.

New workflow runs use `drill_fixture.py --migration-state`: the old release's real
`write_file` tool writes two nonempty files, and its schedule API creates two schedules
whose first runs must finish successfully, reach the provider with their scheduled messages,
and commit replies in the archived conversation. No legacy state files are hand-written.
After the drill, `verify_recovery_state.py` independently reads the stopped state and
archives and appends `state_fixture_verification` **before** the report is hashed and
signed. It compares file bytes by digest and size, and every schedule's identity, message,
interval, timestamps, status and lease fields across restore, upgrade, rollback, online
backup and online restore. When formats change, both migrations must actually execute;
their read-only pre-migration archives must preserve the old state, and rollback must
restore the checked archive. A failed comparison fails the workflow and prevents signing.
This supplements future runs; it does not extend the scope of earlier signed evidence.

`security-review.json` must bind the exact candidate and reviewed threat-model digest to a
dated independent reviewer, zero unresolved critical/high findings, no exceptions, and passed
dependency plus exact-artifact scans with immutable HTTPS reports. `failure-injection.json`
must bind the exact production artifact/topology and report all six required categories, each
with the injected fault, expected/observed behavior, state-integrity verification, service
recovery, and an immutable HTTPS raw-evidence reference.

Each new failure category seeds three sessions through the released service: the faulted
session and two bystanders of the same tenant. The provider requests two real `write_file`
calls per session with distinct nonempty bytes. Each bystander also receives an API-created
daily or weekly schedule. Its first run must reach the provider, commit a reply, and leave
a completed schedule row with no lease before injection begins. The baseline backup must
match the seeded session, audit and sandbox bytes and all canonical schedule fields.
Provider observations are scoped to that category. No release timer or retry setting is
changed to accommodate these fixtures.

The report's per-category `continuity` records bystander message/audit counts and before/after
digests, every session's sandbox file sizes and hashes, full before/after schedule rows and
schema, and each schedule's completed-turn evidence. The faulted session may grow while
preserving its committed prefixes; bystander files and all sandbox files must be unchanged.
Schedules are compared by persisted fields rather than raw SQLite bytes because quota rows
legitimately change during turns. A corrupt-audit startup refusal must leave schedule rows
unchanged before the harness repairs the audit record, as well as preserving customer files.
The gate requires all these surfaces and binds scheduled-turn observations to their stored
rows. Schedules are dormant during fault injection after their first completed runs; this
does not test a fault during an in-flight scheduled turn or a cross-tenant workload.

Run the dispatch-only `Failure drill` workflow against the frozen candidate tag. It
verifies the published archive before booting it on an isolated Linux runner, uses a
local synthetic model endpoint (no paid model requests or customer state), and uploads
the raw JSON and service logs even if a category fails. The full-disk test is limited
to a fresh 8–256 MiB tmpfs (64 MiB by default); without mount privilege it is skipped
as a **failure**, never redirected to the host filesystem. Test doubles never mount.

The interrupted-write test uses a small external Linux `LD_PRELOAD` rename observer,
compiled from `scripts/failure_drill_rename.c`. It stops the host immediately before a
specific session-file replacement, verifies the stopped PID and unfinished temporary
file, and kills the host process group. The stop comes while the save replaces the
committed session's file: after the conversation is written in full to a scratch file and
before that file takes the committed one's name. Restart must preserve previously committed
files, must not adopt that unfinished session, and must remove the scratch file: a scratch
file left after the restart fails the category. Neither the harness nor its observer is
shipped in the candidate.

The report carries `configuration`: the wiring the harness gave the release, any setting a
rehearsal overrode, and the settings the release itself reports as in force. A run with a
shortened timer or retry budget, or against a release that reports no settings, is not
qualification-eligible. `--service-setting NAME=VALUE` starts the release with that `PI_*`
setting in place of the shipped one, for rehearsals only; the report records it.

A passing run covers these six controlled failure cases for the one-worker/local-POSIX
topology. It does **not** certify physical power-loss durability (the host has synced its state writes
since audit F-13; no drill tests that they reach stable storage), real-provider behavior,
capacity under load, the eventual deployment's infrastructure, or independent security
approval. Ordinary development and protected CI continue while those gates remain open.

The performance procedure and proposed approval-dependent envelope are defined in
`../capacity.md`. A short harness smoke report is never acceptable as `load-test.md`; the raw
JSON must say `qualification_eligible: true`, record at least 3,600 planned seconds and the
required concurrency, and have a passing conjunctive evaluation.
