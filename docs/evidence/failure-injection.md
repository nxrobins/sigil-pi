# Expanded failure drill for v0.5.0

All six categories passed against the published Linux x86_64 prerelease. In each category, the faulted session kept its committed conversation and audit prefixes and the service recovered. Both bystander conversations and signed chains remained byte-identical. All six sandbox files and both completed schedules survived. [Signed report](failure-injection.json) · [Workflow run and service logs](https://github.com/nxrobins/sigil-pi/actions/runs/45989775783/attempts/1)

## What ran

| Item | Verified value |
|---|---|
| Candidate archive SHA-256 | `136dc95ac105f76ffb46a67b27a83ef8029a965140e303c6dd8a5d97f8dbb5eb` |
| Workflow source on `main` | [`894ef62bfced56792d18ae109926e928c7fbe5e2`](https://github.com/nxrobins/sigil-pi/commit/894ef62bfced56792d18ae109926e928c7fbe5e2) |
| Start and end (UTC) | 2026-11-27T11:21:38+00:00 to 2026-11-27T11:27:32+00:00 |
| Elapsed | 354 seconds |
| Host | GitHub-hosted Linux x86_64, Python 3.12.11, one worker, local POSIX state, 64 MiB tmpfs |
| Provider | Synthetic loopback endpoint |
| Shipped settings | 120-second turn deadline, 300-second lease, 120-second drain, two LLM retries and four compiler processes |

Each category started with its own state directory and one synthetic tenant. The fault targeted one session. Two bystanders each held a conversation, a signed audit chain, and a daily or weekly schedule. Each of the three sessions held two distinct nonempty files written through the released service. Each schedule ran successfully before injection, reached the provider, committed a reply, and cleared its lease.

## Observations

The counts below are for the faulted session. The six sandbox files, both bystander sessions, and both schedule rows were checked separately in every row.

| Failure | Messages before → after | Audit records before → after | Observed behavior |
|---|---:|---:|---|
| Runtime crash | 4 → 7 | 10 → 15 | The detecting call returned 502. A new turn returned 200 with 4 replacement processes. Their PIDs were distinct from all 4 killed PIDs. |
| Provider unavailable | 4 → 7 | 10 → 19 | The loopback provider returned HTTP 503 to 3 requests. Chat returned 502 `agent_failure` and recovered to 200. |
| Network failure | 4 → 7 | 10 → 19 | The loopback provider closed 3 connections before responding. Chat returned 502 `agent_failure` and recovered to 200. |
| Full disk | 4 → 6 | 10 → 14 | Filling the tmpfs consumed 62,914,560 ballast bytes and reached ENOSPC. Chat returned 500, readiness returned 503, and a turn after space reclamation returned 200. |
| Corrupt audit record | 4 → 6 | 10 → 14 | Startup refused the invalid audit signature. The refused start preserved the damaged chain, the other committed files and the schedule rows. Restoring the original chain allowed a successful turn. |
| Interrupted write | 4 → 6 | 10 → 18 | The observer stopped the host before it replaced the committed conversation. After the host was killed, one persisted lease expired naturally in 299.316 seconds. Restart discarded the scratch write and served a new turn. |

The interrupted write left an 896-byte temporary file beside a 749-byte committed file. Their SHA-256 digests differed. Restart preserved the committed file and removed the temporary file. The full-disk check permits either 200 or 503 for readiness during ENOSPC.

## Verification and retention

The workflow verified the candidate against its frozen record before starting it. Codex separately verified the release's SLSA provenance and CycloneDX signatures against the exact tag, source commit, GitHub-hosted runner and release invocation. The signed SBOM matched the separately downloaded published SBOM. [Release evidence](release-0.5.0.md)

Both the drill and signing jobs passed on attempt 1. The retained SLSA attestation verified against GitHub's OIDC issuer, the exact repository, `failure-drill.yml`, `refs/heads/main`, the source commit above and the run attempt. All six category URLs matched the authenticated run. [Attestation bundle](failure-0.5.0-attestations.jsonl)

| Retained bytes | SHA-256 |
|---|---|
| Raw report | `142cdffd25c3448710ed5204a9db85ba6d8a71f45cc585feeea5ecd05059e989` |
| Attestation bundle | `f4850103353378a5a8bd8b31f0d158f16a1fa6f56a04cde84bcce5f2d1c2ab37` |

The readiness validator accepted the downloaded report for the frozen v0.5.0 digest. Verification also recomputed the expected sizes and SHA-256 digests of the session-specific seed payloads, checked the unchanged bystander digests, and matched the daily and weekly schedule rows to their tenant, principal, sessions, messages, intervals and completed first runs. Codex and Hinoki reviewed the harness and validator in [PR #58](https://github.com/nxrobins/sigil-pi/pull/58).

The signature can be checked from the repository root.

```sh
gh attestation verify docs/evidence/failure-injection.json --repo nxrobins/sigil-pi \
  --bundle docs/evidence/failure-0.5.0-attestations.jsonl \
  --signer-workflow nxrobins/sigil-pi/.github/workflows/failure-drill.yml \
  --source-ref refs/heads/main --source-digest 894ef62bfced56792d18ae109926e928c7fbe5e2 \
  --predicate-type https://slsa.dev/provenance/v1 --deny-self-hosted-runners
```

## Scope

The schedules were dormant during injection. In-flight scheduled turns, cross-tenant load, remote provider behavior, physical power loss and deployment infrastructure remain outside this experiment. The original single-session result and its signed bytes remain available in the [November 19 drill record](history/v0.5.0-initial/failure-injection.md).
