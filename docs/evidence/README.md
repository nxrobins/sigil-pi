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
