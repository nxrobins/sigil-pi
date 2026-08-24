# Release evidence directory

This directory intentionally contains no passing evidence yet. `product-ci.sh` fails closed
until independent, substantive reports for the exact non-development artifact are added:

- `security-review.md`
- `security-review.json` (candidate-bound findings, threat-model review, and scan results)
- `load-test.md`
- `load-test.json` (raw output from `scripts/load_test.py`)
- `failure-injection.md`
- `failure-injection.json` (raw six-category final-topology results)
- `recovery-drills.md`
- `recovery-drill.json` (raw output from the bundled release drill)
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

`recovery-drill.json` must be qualification-eligible and bind a distinct old release plus the
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

The performance procedure and proposed approval-dependent envelope are defined in
`../capacity.md`. A short harness smoke report is never acceptable as `load-test.md`; the raw
JSON must say `qualification_eligible: true`, record at least 3,600 planned seconds and the
required concurrency, and have a passing conjunctive evaluation.
