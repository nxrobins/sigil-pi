#!/bin/zsh
# General-availability gate. Unlike ci.sh, this intentionally fails until all
# external evidence and three named sign-offs exist for the exact artifact.
set -e
cd "$(dirname "$0")"

version=$(tr -d '\n' < VERSION)
case "$version" in
  *-dev|"") echo "FAIL: product-ci requires a non-development VERSION" >&2; exit 1 ;;
esac

./ci.sh

# THE CANDIDATE IS FROZEN, NEVER REBUILT. This used to run build_release.py and
# gate against whatever came out, which made the candidate a function of the
# working tree: appending one line to docs/product-readiness.md moved the digest
# (measured 2026-08-23), and every evidence file binds to that digest. So
# recording a passing result invalidated the evidence for it. README.md does the
# same thing for a duller reason — its test count changes with every test added.
#
# Now the gate VERIFIES the published archive that docs/evidence/candidate.json
# names: same bytes, same digest, whatever the working tree has since become.
record="docs/evidence/candidate.json"
test -f "$record" || {
  echo "FAIL: no frozen candidate at $record. Tag the release, let the release" >&2
  echo "      workflow publish and attest it, then record the PUBLISHED asset's" >&2
  echo "      digest there. See docs/evidence/README.md." >&2; exit 1; }
file=$(.venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["file"])' "$record")
archive="${CANDIDATE_DIR:-dist}/$file"
test -f "$archive" || {
  echo "FAIL: the frozen candidate is not present at $archive." >&2
  echo "      Fetch the published asset:  gh release download v$version -D dist" >&2
  echo "      A local rebuild is NOT a substitute — it is a different candidate." >&2; exit 1; }
.venv/bin/python scripts/build_release.py --verify "$archive" --record "$record"

.venv/bin/python scripts/check_readiness_evidence.py \
  --evidence docs/evidence --artifact "$archive" --version "$version"

echo "PRODUCT READINESS GATE PASS: $version"
