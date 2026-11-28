# v0.5.0 load measurement with local Inkling

The published v0.5.0 service completed a 50-client, 60-minute engineering benchmark. The unmodified release harness failed its criteria. The engineering assessment, which also grades the service and compiler processes together from startup, failed. Of 1,524 measured requests, 0 succeeded. The error rate was 100.00%. 50 of 50 clients were active at the deadline, and 50 finished below the required 26 successful requests.

The harness process started at 2026-11-28T01:09:15.335860+00:00 UTC and ended at 2026-11-28T02:12:25.140186+00:00 UTC. The measured interval lasted 3,668.951 seconds including completion of outstanding requests. Setup finished before the timed interval. Failed session setups numbered 50 of 50. The 25-active-turn launch forecast still lacks owner approval. The measurements apply to one service worker on this Mac and its local model.

## Workload

| Profile | Requests | Successful | Latency p50 / p95 / p99 (s) | Queue p95 (s) | Tool calls | Continuity failures / checks | Cross-session markers |
|---|---:|---:|---|---:|---:|---:|---:|
| single step | 761 | 0 | 120.02 / 120.39 / 120.81 | 119.68 | 0 | 86 / 86 | 0 |
| tool using | 763 | 0 | 120.02 / 120.34 / 120.92 | 119.69 | 0 | 88 / 88 | 0 |

Each tenant had one plain-turn client and one tool-using client. Every tenant reused the same two external session names. Request success counts use the harness’s HTTP and response-validity check. The harness grades tool use and session recall separately. Queue percentiles cover responses carrying queue telemetry.

- **Single step** returned 504 × 514, 502 × 247. Failure classes were turn_deadline_exceeded × 514, agent_failure × 247. Tool-contract violations numbered 0, and 0 responses lacked complete telemetry. Queue samples numbered 761.
- **Tool using** returned 504 × 531, 502 × 232. Failure classes were turn_deadline_exceeded × 531, agent_failure × 232. Tool-contract violations numbered 763, and 0 responses lacked complete telemetry. Queue samples numbered 763.

## Service resources

The harness obtained 722 authenticated resource samples with 0 failed samples. The compiler replacement counter went from 0 to 0. Minimum free disk space was 45.39%.

The separate Linux process sampler collected 748 usable observations, beginning before session setup. It recorded 1 sampling errors. Its longest gap between usable samples was 10.08 seconds. The observed pool sizes were [3, 4], with 171 distinct compiler processes.

| Service host and compilers combined | First sample | Sampled peak | Growth |
|---|---:|---:|---:|
| Summed resident memory (MiB) | 97.0 | 738.1 | 641.1 |
| Kernel threads | 13 | 171 | 158 |
| Open descriptors | 46 | 196 | 150 |

The supplemental assessment recorded the following findings.

- Host and compiler rss_bytes growth exceeds the declared numeric bound.
- Host and compiler threads growth exceeds the declared numeric bound.
- Incomplete process-tree resource sampling.
- Service or process samples differ from the four-compiler topology.

Summed resident memory can count shared pages more than once. Sampling can miss brief peaks. The release harness also records host-only growth from its post-setup baseline. These figures cover the service and compilers in the Linux container. Inkling runs on the Mac GPU. The provisional 512 MiB bound and the forecast still require approval before a launch capacity claim.

## Release, provider and verification

- **Release.** The tested archive SHA-256 was `136dc95ac105f76ffb46a67b27a83ef8029a965140e303c6dd8a5d97f8dbb5eb`. The GitHub provenance and CycloneDX SBOM verified against the release workflow, tag, source commit and this digest. [Release and signed build](release-0.5.0.md)
- **Provider.** Ollama 0.40.17 served `inkling-2-mini:latest` at digest `d183b1321cb57f018c738d2a826d70f575656fbcb7cec67f333d15b0f6a3d178`. The local adapter obtained counts by issuing a real one-token Inkling request with the original prompt, then forwarded generation to Inkling. The adapter recorded 1,438 count requests and 168 generation requests. This added model traffic and permitted reuse of the prompt cache.
- **Environment.** One v0.5.0 worker and its four default SIGIL compilers ran under Linux x86_64 emulation on an Apple M3 Ultra. Docker Desktop had 24 vCPUs and 7.75 GiB of VM memory. The load generator ran in the same container. [Exact environment](load-runs/v0.5.0-inkling/environment.json)
- **Evidence.** The unmodified raw report has SHA-256 `19c2ba215860fbece9883132530daed365890812fe13b57b1c425b1e72adb976`. The independent verifier reconciled the client records, provider identity, harness source and reported verdict. These benchmark files were collected locally. [Raw report](load-runs/v0.5.0-inkling/load-test.json) · [Run context and process samples](load-runs/v0.5.0-inkling/run-context.json) · [Verification](load-runs/v0.5.0-inkling/verified.json) · [Checksums](load-runs/v0.5.0-inkling/SHA256SUMS) · [Fixture and commands](../../qualification/load-v0.5.0/README.md)
