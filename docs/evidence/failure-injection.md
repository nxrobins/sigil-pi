# Failure-injection drill — v0.5.0

Status: **all six controlled categories passed**, including committed-state integrity and
service recovery. The report is qualification-eligible for its one-worker, local-POSIX,
synthetic-state scope. This is not a complete product-readiness or launch sign-off.

- Test date: **2026-11-19** (UTC and America/Los_Angeles).
- Execution and verification: Codex; focused automated review: Hinoki, 2026-11-19.
  This review is not an independent human security approval.
- Candidate: **v0.5.0**, SHA-256 `136dc95ac105f76ffb46a67b27a83ef8029a965140e303c6dd8a5d97f8dbb5eb`.
- Published archive: `sigil-pi-0.5.0-linux-x86_64.tar.gz`; unchanged and bound to `candidate.json`.
- [Workflow, jobs and service logs](https://github.com/nxrobins/sigil-pi/actions/runs/44488831946).
- Workflow source on main: `cd03a4055839fca857b6c2f1aec9342041f01d13`. Both `drill` and `attest` jobs passed, attempt 1.
- Started/completed UTC: `2026-11-19T08:35:39+00:00` / `2026-11-19T08:40:58+00:00`; elapsed **319 seconds**.
- [Raw signed report](failure-injection.json), SHA-256 `8cb765c24b8a5695127cd6ae67b208d401f9da6200a076f107e6f33afc7b737c`.
- [Retained attestation bundle](failure-0.5.0-attestations.jsonl), SHA-256 `3973de48d58aa683f8118325f380f42d57ee8698e8d1f67625bd10e827918024`.
- Topology: GitHub-hosted Linux x86_64, CPython 3.12.11, pinned Z3 4.12.2, one worker and
  local POSIX state. Full-disk injection used a disposable **64 MiB tmpfs**.
- Provider: Anthropic-shaped loopback stand-in, with no real provider calls or customer state.
- Settings: no overrides or shortened budgets. The release reported its shipped
  120-second turn deadline, 300-second turn lease, 120-second drain timeout, two LLM retries
  and four compiler processes. The complete effective settings are in the report.

## Observed results

| Category | Result | Observation |
|---|---|---|
| Runtime crashes | PASS | Four forge children were killed. The detecting call returned stable 502 `agent_failure`; a separate next call returned 200 with four distinct replacement PIDs. Readiness returned 200. |
| Unavailable model provider | PASS | The stand-in received three requests and returned 503. The product returned stable 502 `agent_failure`, remained ready, and served a 200 turn after the provider recovered. |
| Network failure | PASS | Three connections were closed without an HTTP response. The product returned stable 502 `agent_failure`, remained ready, and served a 200 turn after the fault cleared. |
| Full disk | PASS | Writes reached ENOSPC (errno 28) after 62,914,560 ballast bytes. Chat returned a stable 500; readiness returned **503**; reclaiming space allowed a 200 recovery turn. |
| Corrupt state | PASS | A signed audit record was edited. Startup explicitly refused the signature failure without changing the corrupted chain or other committed files. After the harness restored the original record, readiness and chat returned 200. |
| Interrupted write | PASS | The host was observed stopped before replacing its committed conversation file and was killed. After the persisted lease expired, restart preserved the committed file, did not adopt the scratch write, removed the scratch file, and returned 200 for readiness and a new turn. |

The full-disk result exercises the readiness defect seen in v0.4.0: that archive returned
500 for readiness under ENOSPC; this v0.5.0 run returned 503. The v0.4.0 failed reports remain
unchanged in [failure-investigation-0.4.0.md](failure-investigation-0.4.0.md) and `failure-runs/`.
No fix is attributed retroactively to those published bytes.

The full-disk detail records the 500 status but omits the specific accepted error code;
the 503 readiness observation is recorded in prose. The harness accepts readiness 200
or 503. Likewise, the three provider requests were observed, while the harness requires
at least one; this run does not independently qualify the retry budget.

For the interrupted write, PID `3122` was observed at the rename boundary.
The committed conversation was **148 bytes** with SHA-256
`404e0ee2603a209721a1a3bdb82ebc772cf6204ad92e16a7e822744f61533457`; the unfinished scratch was **295 bytes**
with distinct SHA-256 `ea611f4cf7f8160efe47c50b32ab59556d72e4a522a38a6b8923c0727322c071`. One persisted lease expired naturally
in **299.543854 seconds**, within the shipped 300-second lease budget.
The harness neither deleted nor shortened the lease. No scratch files remained after restart.
This is a process-kill experiment at an observed write boundary, not a power-loss experiment.

## What state was preserved

Each category used a fresh state directory and committed a real turn before the fault.
The fault targeted that same session. Every baseline contained **two customer files**:
one conversation and its signed audit chain, holding two messages and four audit records.
The release's own `state_tool.py` made the before/after backups and validated their state;
the harness compared the committed prefixes and required a successful recovery turn.
Every category reports no lost or changed baseline files and no qualification failures.
Later turns may append messages/audit records; preservation does not mean the session stopped growing.

| Faulted session | Messages before → after | Audit records before → after | Baseline |
|---|---:|---:|---|
| `corrupt_state` | 2 → 4 | 4 → 8 | Preserved |
| `full_disks` | 2 → 4 | 4 → 8 | Preserved |
| `interrupted_writes` | 2 → 4 | 4 → 12 | Preserved |
| `network_failures` | 2 → 5 | 4 → 13 | Preserved |
| `runtime_crashes` | 2 → 5 | 4 → 9 | Preserved |
| `unavailable_model_providers` | 2 → 5 | 4 → 13 | Preserved |

There were no bystander sessions, nonempty sandbox files or schedules. This run therefore
makes no failure-injection preservation claim for those surfaces. Nonempty sandbox/schedule migration
and recovery evidence is separate in [recovery-drills.md](recovery-drills.md).

## Independent verification and signed bytes

The workflow verified the downloaded archive against `candidate.json` before injection:
digest, inner manifest, version, SBOM/compiler pin and packaged operator-document parity.
The release's SLSA provenance and CycloneDX SBOM attestations were reverified separately
against the exact v0.5.0 tag, source `ee84073f29fd39e00dffcf9f2d1bef3ba705e757`, release workflow,
GitHub-hosted runner and invocation `44272804229/attempts/1`; the signed SBOM equals the
published SBOM. Its retained bundle is [release-0.5.0-attestations.jsonl](release-0.5.0-attestations.jsonl).

The downloaded failure report passed `scripts/check_readiness_evidence.py::_validate_failure_report`
against the frozen candidate digest. Additional checks required exactly the six categories,
empty failure lists, the exact run URL, no configuration overrides, a real bounded tmpfs,
nonempty committed content in each faulted session, distinct replacement runtime PIDs,
actual failed provider requests, ENOSPC with 503 readiness, explicit corruption refusal,
and the observed rename boundary, lease wait and scratch cleanup.

Hinoki found two missing readiness-gate checks: a report could omit the bounded-filesystem
claim or cite a run other than the one that attested it. Both now have failing-first
regressions and fixes: the gate requires `tmpfs`, and all category URLs must identify
one run authenticated by the failure report's own attestation. Invocation URIs must name
an exact run attempt. The original report already satisfies these checks; its signed
bytes and the injection harness are unchanged. The expanded drill, workflow, attestation,
readiness, documentation and coverage validation suite passes 303 tests locally, with
one Linux-only test skipped, including 16 new cases covering these checks and malformed
inputs. CI identified an existing synthetic-report fixture that needed the new bounded
filesystem claim; it now checks rejection before supplying that claim for its schema
check. Hinoki reviewed both the gate fixes and this fixture correction without further
actionable findings.

`gh attestation verify` succeeded against the retained report bundle. Verification was
constrained to this repository, `failure-drill.yml`, `refs/heads/main`, the exact workflow
source commit, SLSA provenance and GitHub-hosted runners. The verified certificate's complete
signer URI, issuer, source repository/ref/digest, runner environment and invocation URI
were compared for exact equality, and the signed subject digest matches the raw report.
The invocation is `https://github.com/nxrobins/sigil-pi/actions/runs/44488831946/attempts/1`.
The report and bundle are retained without reformatting. To repeat the signature check:

```sh
gh attestation verify docs/evidence/failure-injection.json --repo nxrobins/sigil-pi \
  --bundle docs/evidence/failure-0.5.0-attestations.jsonl \
  --signer-workflow nxrobins/sigil-pi/.github/workflows/failure-drill.yml \
  --source-ref refs/heads/main --source-digest cd03a4055839fca857b6c2f1aec9342041f01d13 \
  --predicate-type https://slsa.dev/provenance/v1 --deny-self-hosted-runners
```

## Limits

This is one controlled run with one worker, one synthetic faulted session per category,
a local filesystem and a synthetic provider. It does not prove physical power-loss durability,
all corruption modes or write boundaries, realistic network failures, real-provider behavior,
concurrent multi-tenant load, nonempty sandbox/schedule failure handling, or the eventual
deployment's infrastructure. It does not replace the load run, deployed operations evidence,
independent security review or 30-day pilot. No human engineering/security/operations launch
sign-off is asserted, and no whole product-readiness area changes to PASS.
