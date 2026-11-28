# v0.5.0 load measurement with local Inkling

**The published v0.5.0 archive failed the 50-client capacity test on this deployment.** All 50 clients remained active for the full hour. All 50 session setup requests failed, followed by **0 successful requests out of 1,524 measured**. The service returned 1,045 HTTP 504 turn-deadline errors and 479 HTTP 502 agent failures. The worst profile’s p95 queue wait was **119.687 seconds**, against a target below 2 seconds and a whole-turn deadline of 120 seconds. [Raw report](load-runs/v0.5.0-inkling/load-test.json)

## Workload

The release harness assigned one plain-turn client and one tool-using client to each of 25 synthetic tenants. Every tenant used the same two external session names. The run offered work for 3,600 seconds after setup. The measured interval reached 3,668.951 seconds as outstanding requests finished. The harness process ran from 01:09:15 to 02:12:25 UTC on 28 November 2026.

| Profile | Requests | Successful | HTTP 502 | HTTP 504 | Latency p50 / p95 / p99 (s) | Queue p95 (s) |
|---|---:|---:|---:|---:|---|---:|
| Plain turn | 761 | 0 | 247 | 514 | 120.023 / 120.392 / 120.813 | 119.678 |
| Tool-using turn | 763 | 0 | 232 | 531 | 120.023 / 120.344 / 120.921 | 119.687 |

All 1,524 responses supplied queue telemetry. The service includes time waiting for a compiler in that figure. Every client missed the floor of 26 successful requests. All 174 recall checks failed, and all 763 tool-profile requests failed their tool-use check. With no successful replies, this run provides no positive evidence of session isolation.

The local provider answered 1,432 of 1,438 count calls and 156 of 168 generation calls with HTTP 200, including 68 tool-use responses. Those upstream responses never produced a successful measured client turn. In a separate two-client preflight, the same release, model and adapter completed all 15 measured requests, including five tool-using turns and a successful recall in each profile. [Provider counters](load-runs/v0.5.0-inkling/run-context.json) · [Preflight report](load-runs/v0.5.0-inkling/preflight/load-test.json)

## Service resources

The release collected all 722 authenticated service samples. The compiler generation count rose from 5 to 232. The unhealthy-replacement counter stayed at zero. v0.5.0 excludes replacements explained by an expired caller budget from this counter. Of the 722 observations, 720 reported four live compilers, one reported one, and one reported zero. [Release runtime counter definitions](https://github.com/nxrobins/sigil-pi/blob/v0.5.0/runtime_client.py)

A separate Linux sampler observed the service process and its compiler children from before setup through the end of the harness. It recorded 748 usable samples and one failed sample. The longest gap between usable samples was 10.08 seconds. It observed 171 distinct compiler PIDs. The release’s host-only memory high-water mark grew by 33.8 MiB from its post-setup baseline.

| Host and compilers combined | Before setup | Sampled peak | Growth | Provisional growth limit |
|---|---:|---:|---:|---:|
| Summed resident memory | 97.0 MiB | 738.1 MiB | 641.1 MiB | 512 MiB |
| Kernel threads | 13 | 171 | 158 | 64 |
| Open file descriptors | 46 | 196 | 150 | 256 |

Summed resident memory can count shared pages more than once. Sampling can miss brief peaks. These figures cover the release process and compilers in Linux. Inkling ran on the Mac GPU. Minimum free disk space was 45.39%. The combined assessment failed on memory growth, thread growth, sampling completeness and four-compiler availability. [Verified assessment](load-runs/v0.5.0-inkling/verified.json)

## What was verified

- **Release.** SHA-256 `136dc95ac105f76ffb46a67b27a83ef8029a965140e303c6dd8a5d97f8dbb5eb`. The GitHub provenance and CycloneDX SBOM attestations verified against this archive, the v0.5.0 tag, its source commit and the signed release workflow invocation. [Release evidence](release-0.5.0.md)
- **Deployment.** One worker, a configured pool of four compilers, shipped deadlines and quotas, and the unmodified v0.5.0 load harness. The Linux x86_64 archive and load driver ran under emulation in Docker Desktop on an Apple M3 Ultra. The Linux VM had 24 vCPUs and 7.75 GiB of memory. [Environment and merge commits](load-runs/v0.5.0-inkling/environment.json)
- **Provider.** Local Ollama 0.40.17 served `inkling-2-mini:latest` at digest `d183b1321cb57f018c738d2a826d70f575656fbcb7cec67f333d15b0f6a3d178`. Its server lacked the token-count endpoint required by v0.5.0. A loopback adapter sent the same prompt to Inkling with a one-token output cap and returned the model’s measured input usage. It forwarded each generation request and response. The extra inference call and any prompt-cache reuse are part of these timings. [Fixture and reproduction commands](../../qualification/load-v0.5.0/README.md)
- **Measurement.** The raw report SHA-256 is `19c2ba215860fbece9883132530daed365890812fe13b57b1c425b1e72adb976`. The verifier reconciled source digests, the before-and-after model identity, client and profile totals, resource samples and the failure verdict. Checksums identify the locally collected evidence. [Checksums](load-runs/v0.5.0-inkling/SHA256SUMS)

Both the shipped harness and the combined resource assessment failed. The proposed 25-active-turn launch forecast still lacks product-owner approval. This engineering measurement applies to this single-worker, emulated deployment and its local Inkling provider. [Capacity criteria](../capacity.md)
