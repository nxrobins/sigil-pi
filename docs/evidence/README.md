# Release evidence directory

This directory contains the published, attested v0.4.0 candidate and its passing recovery
drill with rollback to v0.3.0, not a complete product qualification. See `release-0.4.0.md`
for independent artifact/attestation verification and `recovery-drills.md` for the fresh drill.
The original v0.3.0 candidate and recovery evidence are preserved under `history/v0.3.0/`.
`product-ci.sh` fails closed until substantive reports for the exact candidate are added:

- `security-review.md`
- `security-review.json` (candidate-bound findings, threat-model review, and scan results)
- `load-test.md`
- `load-test.json` (raw output from `scripts/load_test.py`)
- `failure-injection.md`
- `failure-injection.json` (raw six-category final-topology results, produced by
  `scripts/failure_drill.py` against the published candidate)
- `recovery-drills.md`
- `recovery-drill.json` (raw output from the bundled release drill)
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
evidence references, and all three roles. Independent authenticity and adequacy still remain
human release-review responsibilities.

`recovery-drill.json` is produced by the `Recovery drill` workflow against the published
artifacts (see `docs/operations.md`); retain the run's artifact and cite the run URL as the raw
evidence location. It must be qualification-eligible and bind a distinct old release plus the
exact candidate digest to real one-worker/local-POSIX service probes in this order: clean
install, old-release restore, candidate upgrade, old-release rollback. The validator
independently enforces a backup age no greater than 15 minutes, RTO no greater than four
hours, rollback no greater than 15 minutes, and non-vacuous preservation of committed session
files.

`security-review.json` must bind the exact candidate and reviewed threat-model digest to a
dated independent reviewer, zero unresolved critical/high findings, no exceptions, and passed
dependency plus exact-artifact scans with immutable HTTPS reports. `failure-injection.json`
must bind the exact production artifact/topology and report all six required categories, each
with the injected fault, expected/observed behavior, state-integrity verification, service
recovery, and an immutable HTTPS raw-evidence reference.

Run the dispatch-only `Failure drill` workflow against the frozen candidate tag. It
verifies the published archive before booting it on an isolated Linux runner, uses a
local synthetic model endpoint (no paid model requests or customer state), and uploads
the raw JSON and service logs even if a category fails. The full-disk test is limited
to a fresh 8–256 MiB tmpfs (64 MiB by default); without mount privilege it is skipped
as a **failure**, never redirected to the host filesystem. Test doubles never mount.

The interrupted-write test uses a small external Linux `LD_PRELOAD` rename observer,
compiled from `scripts/failure_drill_rename.c`. It stops the host immediately before a
specific session-file replacement, verifies the stopped PID and unfinished temporary
file, and kills the host process group. Restart must preserve previously committed
files and must not adopt that unfinished session. Leftover scratch files are allowed
by the existing state format and excluded from committed backups; their presence alone
is not corruption. Neither the harness nor its observer is shipped in the candidate.

A passing run covers these six controlled failure cases for the one-worker/local-POSIX
topology. It does **not** certify physical power-loss durability, real-provider behavior,
capacity under load, the eventual deployment's infrastructure, or independent security
approval. Ordinary development and protected CI continue while those gates remain open.

The performance procedure and proposed approval-dependent envelope are defined in
`../capacity.md`. A short harness smoke report is never acceptable as `load-test.md`; the raw
JSON must say `qualification_eligible: true`, record at least 3,600 planned seconds and the
required concurrency, and have a passing conjunctive evaluation.
