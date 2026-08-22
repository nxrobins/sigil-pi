# Local gate verification — 2026-08-22

Status: **mechanics evidence only; not release approval.** Supersedes the 2026-08-20 record
for everything below; that record remains the pre-rebase baseline and is not retracted.

- Tree: branch `product-readiness-rebased` — the 2026-08-20 product tree rebased onto
  `origin/main` `c04446d` (PRs #14, #22–#27) — at `55d8396` (the textual resolution and the
  seam reconciliation) plus the then-uncommitted solver-verifying toolchain change that
  became the following commit.
- Platform: `darwin-arm64`, CPython 3.14.6, rustc 1.96.1, Homebrew Z3 4.16.0.
- Pinned SIGIL commit: `eb9f1715dd3fadd3c9fe1f6ccb19c17f49a2ac0b` in a dedicated worktree
  (`/Users/nigel/Projects/sigil-pinned-eb9f1715`); `ci.sh` step 1 confirmed the crates/stdlib
  tree hashes and rebuilt `sigil-mcp`/`sigil-serve` **with the `solver` feature**.
  `sigil-mcp` SHA-256 (this build, links `libz3.4.16.dylib`): `91c1e04579deac6ddf2f626fe66afee2ad5e973c5fadf4ad7d164180200bec12`.
- Command: `SIGIL_ROOT=/Users/nigel/Projects/sigil-pinned-eb9f1715 PI_REQUIRE_TOOLCHAIN=1 ./ci.sh`
- Result: **CI PASS.** 724 collected — 723 passed, 1 strict research-only xfail; 0 skipped
  (`PI_REQUIRE_TOOLCHAIN=1` turns a missing toolchain into an error, so no forge test could
  skip). Wall-clock ≈ 2 minutes.
- Coverage gate (independent line/branch, `scripts/check_coverage.py`): **91.97% line,
  87.48% branch**, every inventoried critical boundary at 100% line and branch.
  `toolchain.py` is measured for the first time.

| Module | Line | Branch |
|---|---:|---:|
| `agent.py` | 86.65% | 84.29% |
| `product_main.py` | 92.03% | 81.25% |
| `product_service.py` | 94.30% | 90.77% |
| `runtime_client.py` | 100.00% | 100.00% |
| `scripts/build_release.py` | 91.43% | 81.82% |
| `scripts/load_test.py` | 93.99% | 91.22% |
| `sigil_compose.py` | 100.00% | 100.00% |
| `state_tool.py` | 91.59% | 81.25% |
| `toolchain.py` | 95.41% | 90.48% |

## What changed since the 2026-08-20 record

1. **The suite now forges through the product client.** `toolchain.client()` hands out the
   vendored `runtime_client.ProductionSigilMCP`, which strips `SIGIL_ALLOW_UNVERIFIED_CERT`.
   On 2026-08-20 the suite forged through SIGIL's bench client with that override set, so the
   product client had never forged a real turn against the binary the gate builds.
2. **That exposed a product-blocking defect.** The default `cargo build -p sigil-mcp` is a
   solver-off compiler whose forge gate fails closed (`R817`); against it, 197 tests failed
   (run of 2026-08-22 10:30). `ci.sh` and `scripts/build_release.py` now build with
   `--features sigil-mcp/solver,sigil-serve/solver`; CI pins Z3 4.12.2. Every tool's Z3
   obligations discharge under test.
3. **New dependency recorded, not hidden:** the solver-verifying binary links `libz3`
   dynamically. The release bundle, SBOM, and reproducibility record do not yet cover it
   (threat model risk 9; readiness area 1).

## What this does not satisfy

No candidate bundle was rebuilt for this run, so the 2026-08-20 archive digests are no
longer current for this tree; the protected clean-CI/tag run, signed provenance/SBOM
attestation, qualifying distinct-version clean-host drill, independent review, qualifying
60-minute load run, recovery/failure drills, named sign-offs, and 30-day pilot gates all
remain open. The general-availability gate (`./product-ci.sh`) remains intentionally closed.
