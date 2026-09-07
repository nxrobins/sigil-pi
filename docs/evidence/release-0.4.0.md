# v0.4.0 publication and attestation verification

Status: **published and cryptographically verified internal-alpha prerelease**, not GA.
Verified 2026-09-07 from the published GitHub release assets, not a local rebuild.

## Source and execution identity

- Release: https://github.com/nxrobins/sigil-pi/releases/tag/v0.4.0
- Source commit: `ad0c02e056a29a4fa70b86e844cb75e88ce4776c`.
- Protected preparation PR: https://github.com/nxrobins/sigil-pi/pull/39
- Both required PR jobs passed: https://github.com/nxrobins/sigil-pi/actions/runs/34144022751
- Post-merge CI passed: https://github.com/nxrobins/sigil-pi/actions/runs/34144679242
- Release gate, packaging, both attestations and pre-publication verification passed:
  https://github.com/nxrobins/sigil-pi/actions/runs/34144708071
- Required main checks were strict and GitHub-Actions-bound, including for administrators.
  Merge used the exact tested PR head without an administrator bypass. This does not assert
  independent human approval or release-tag protection.

## Published artifact identities

| Asset | SHA-256 |
|---|---|
| `sigil-pi-0.4.0-linux-x86_64.tar.gz` | `6f17f2f0a91c9f9cdc50c4cb6d0d50f2c305c55162180c933c0604f556407203` |
| `sigil-pi-0.4.0-linux-x86_64.SBOM.cdx.json` | `52bb6c68163b93ce2da0ec763057ff1180447d924e9c0f16bde29015469a9764` |
| Retained `release-0.4.0-attestations.jsonl` | `94b2bac6cabae1606909c1025457b0ae350ba1871b7cef4b7324dd852ec84e91` |
| Rollback archive, v0.3.0 | `be060867b473b7858581a4fc8623d8db0120cce82ba4067c0fe76e21ca8f0b78` |

The new archive's outer checksum, internal manifest, VERSION, embedded SIGIL SBOM pin and
operator-document parity are checked by `scripts/build_release.py --verify`. The published
v0.3.0 rollback archive was separately downloaded and verified against its historical frozen
record. `history/v0.3.0/` preserves that record and its recovery evidence without rewriting it.

## Independent cryptographic checks

The following policy was applied outside the release runner, once for each predicate:

```sh
gh attestation verify dist/v0.4.0/sigil-pi-0.4.0-linux-x86_64.tar.gz \
  --repo nxrobins/sigil-pi \
  --signer-workflow nxrobins/sigil-pi/.github/workflows/release.yml \
  --source-ref refs/tags/v0.4.0 \
  --source-digest ad0c02e056a29a4fa70b86e844cb75e88ce4776c \
  --deny-self-hosted-runners \
  --predicate-type https://slsa.dev/provenance/v1
```

Repeat with `--predicate-type https://cyclonedx.org/bom` for the SBOM. Both exited
successfully. For retained-bundle verification, add
`--bundle docs/evidence/release-0.4.0-attestations.jsonl`.

The verified certificate identifies the public `nxrobins/sigil-pi` repository, GitHub-hosted
runner, `refs/tags/v0.4.0`, the source commit above, and the release workflow. Its invocation
is `https://github.com/nxrobins/sigil-pi/actions/runs/34144708071/attempts/1`.
The attested SBOM predicate was compared with the complete published SBOM JSON and matched.

## Limits

The new candidate also passed the distinct-version v0.3.0 → v0.4.0 → v0.3.0 recovery drill:
https://github.com/nxrobins/sigil-pi/actions/runs/34145661500. The downloaded report passed
independent machine validation; exact timings, digests and scope are in `recovery-drills.md`.

These checks establish artifact identity and signed build provenance, not production readiness,
whole-program security, independent human review, or a complete transitive SBOM. The runtime
still needs host-provided Z3; immutable runner/tag controls, security review/scans, full failure
and load qualification, deployed operations and the 30-day pilot remain separate requirements.
The current candidate's recovery result belongs in `recovery-drills.md`; the old v0.3.0 drill
is historical evidence only.
