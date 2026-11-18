# v0.5.0 publication and attestation verification

Status: **published and cryptographically verified internal-alpha prerelease**, not GA.
Verified 2026-11-18 from the published GitHub release assets, not a local rebuild.

## Source and execution identity

- Release: https://github.com/nxrobins/sigil-pi/releases/tag/v0.5.0
- Source commit: `ee84073f29fd39e00dffcf9f2d1bef3ba705e757` (merged main; annotated tag).
- Protected preparation PR: https://github.com/nxrobins/sigil-pi/pull/52
- Both required PR jobs passed: https://github.com/nxrobins/sigil-pi/actions/runs/44269764730
- Both post-merge main CI jobs passed: https://github.com/nxrobins/sigil-pi/actions/runs/44272752028
- Release gate, fresh-runner build, publication, provenance and SBOM signing passed:
  https://github.com/nxrobins/sigil-pi/actions/runs/44272804229
- Compiler: published SIGIL commit `09939979163f7bcca5443b1060c93b0eed4ddb28`.

The Linux main and release gates both reported 94.83% line and 92.05% branch coverage.

## Published artifact identities

| Asset | SHA-256 |
|---|---|
| `sigil-pi-0.5.0-linux-x86_64.tar.gz` | `136dc95ac105f76ffb46a67b27a83ef8029a965140e303c6dd8a5d97f8dbb5eb` |
| `sigil-pi-0.5.0-linux-x86_64.SBOM.cdx.json` | `9d0fa18e63505e4c47ed89cc5134cda804043cb3c23dcf2ddc9d24eeaa9a9fc4` |
| Retained `release-0.5.0-attestations.jsonl` | `eed3dde128a46bdb765c2035b7050f55fcf1d9868a15dfa56fa6e25bfa723576` |
| Rollback archive, v0.4.0 | `6f17f2f0a91c9f9cdc50c4cb6d0d50f2c305c55162180c933c0604f556407203` |

The three published assets' sizes and SHA-256 values match GitHub's release metadata.
The checksum sidecar matches the archive. `scripts/build_release.py --verify` accepts
its internal manifest, VERSION, SIGIL source/tree pins, embedded compiler digest and
all five operator-document parity checks. An additional archive inspection found no
duplicate, traversal or special-file members; the manifest lists every regular payload
file. The runtime is ELF64 x86-64, SHA-256 `adf7abe0c536637e826a5e9d5e16838f589527f4cdfa6df71bec8caee5d26b55`.
The published and bundled SBOM JSON match (297 components).

The v0.4.0 rollback asset was independently downloaded, checksum/manifest/SBOM-verified
against its historical operator documents, and both of its attestations verified against
tag `v0.4.0` and source `ad0c02e056a29a4fa70b86e844cb75e88ce4776c`.
Its original candidate and recovery records are retained unchanged under `history/v0.4.0/`.

## Independent cryptographic checks

Outside the release runner, `gh attestation verify` succeeded for both
`https://slsa.dev/provenance/v1` and `https://cyclonedx.org/bom`, constrained to:

- repository `nxrobins/sigil-pi`;
- signer workflow `nxrobins/sigil-pi/.github/workflows/release.yml`;
- source ref `refs/tags/v0.5.0` and digest `ee84073f29fd39e00dffcf9f2d1bef3ba705e757`;
- GitHub-hosted runners (`--deny-self-hosted-runners`).

The verified certificate's complete signer URI, issuer, source repository/ref/digest,
runner environment and invocation were independently compared for exact equality.
The invocation is `https://github.com/nxrobins/sigil-pi/actions/runs/44272804229/attempts/1`. The signed SBOM predicate equals the
complete published SBOM. Raw signed bundles are retained in
`release-0.5.0-attestations.jsonl`; add `--bundle` with that path to verify them again.

## Evidence sequencing and limits

`candidate.json` freezes this published archive with rollback to v0.4.0. The signed
v0.4.0 → v0.5.0 → v0.4.0 recovery run passed, including complete-history turns and
online backup/restore: https://github.com/nxrobins/sigil-pi/actions/runs/44282969234.
The exact report bytes, attestation and independent checks are recorded in
`recovery-drills.md`. The conversation/audit-only fixture needed no migration work;
nonempty legacy-state migration remains unproven by that run. Earlier recovery evidence
remains historical and is not transferred to this candidate.

The document-freshness guard now checks dates of the verification records the frozen
threat model actually cites. It still requires a verified record, at least one citation,
every cited record to be recognized and dated, the model date, and absence of stale global
provenance claims. Twelve regressions cover the sequencing conflict and fail-closed cases;
five failed before the correction, all 37 document/release-build checks passed afterward,
and Hinoki found no blocker. No operator-document parity or published artifact was changed.

These checks establish artifact identity and signed build provenance, not production
readiness, independent human approval, complete transitive dependency coverage, physical
power-loss durability, real-provider behavior or a production-scale recovery result.
Release-tag controls, security scans/review, load/failure qualification, deployed operations
and the 30-day pilot remain separate requirements. This release requires state migration;
rollback must restore the pre-upgrade or pre-migration backup into a fresh path.
For v0.4.0 → v0.5.0, run the candidate's `state_tool.py migrate` as described in the
[state-compatibility procedure](../state-compatibility.md) and
[migration runbook](../runbooks.md#state-migration).
