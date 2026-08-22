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

out="dist/readiness-$version"
mkdir -p "$out"
.venv/bin/python scripts/build_release.py \
  --sigil-root "${SIGIL_ROOT:-$(pwd)/../SIGIL}" --output "$out" --no-build
archive=$(find "$out" -maxdepth 1 -name 'sigil-pi-*.tar.gz' -print -quit)
test -n "$archive" || { echo "FAIL: release builder produced no archive" >&2; exit 1; }

.venv/bin/python scripts/check_readiness_evidence.py \
  --evidence docs/evidence --artifact "$archive" --version "$version"

echo "PRODUCT READINESS GATE PASS: $version"
