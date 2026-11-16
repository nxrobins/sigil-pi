# sigil-pi v1 launch-capacity declaration

This is a proposed engineering envelope, not launch evidence and not yet a product-owner
forecast. It becomes the v1 capacity commitment only when an accountable product owner
approves or replaces the forecast, and a digest-bound qualification run passes against the
exact release candidate. `docs/product-readiness.md`, in the repository at this release's version tag, remains authoritative.

## Proposed launch load

| Property | Proposed v1 value |
|---|---:|
| Forecast peak | 25 simultaneously active turns |
| Mandatory qualification target | 50 concurrent clients (`max(2x forecast, 50)`) |
| Test duration | 60 continuous minutes |
| Product workers | 1 process, 4 pinned SIGIL runtime connections (default pool) |
| Tenant distribution | at least 25 credentials, no more than 2 active clients per tenant |
| Traffic mix | 50% single-step turns, 50% turns that must invoke `list_dir` |
| Session pattern | the same two external session names reused in every tenant |

The one-worker topology is intentional: current metrics are process-local rather than
aggregated. Each connection serializes its own forges, while the bounded compiler pool
allows separate sessions to overlap. The qualification must measure queueing with the exact
pool size, model HTTP timeout and token ceiling recorded in metrics; a two-client regression
check does not establish the 50-client capacity target. A multi-worker launch declaration requires
aggregate/per-worker metrics and a new qualification run.

A compiler holds about 7 MiB idle and about 120 to 130 MiB once it has served turns (measured
2026-11-15 on macOS arm64 with SIGIL 09939979; current resident memory, not peak). Four
compilers can reach the 512 MiB growth bound by themselves, and fifty, which the 50-client
target implies for model calls that last seconds, would need about 6 GiB. The bound and the
target cannot both hold, and which one changes is not yet decided.

## Conjunctive pass criteria

A run passes only if every condition below is true:

1. It uses the exact candidate identified by a lowercase SHA-256 digest, runs for at least
   3,600 seconds, offers at least 50 concurrent clients, and names the person who approved
   the 25-turn forecast. The report records the provider endpoint's host, the model and every
   limit the service reports in force (`effective_settings` in `/v1/metrics`); a service that
   reports none, or whose settings change during the run, cannot pass.
2. Total non-success response rate is strictly below 0.5%. Transport failures, malformed
   responses, stable 4xx responses, and 5xx responses all count as failures.
3. The worse of the two traffic profiles has queue-wait p95 strictly below 2,000 ms. Latency
   and queue p50/p95/p99 are reported separately for single-step and tool-using turns using
   nearest-rank percentiles rounded to 1 ms.
4. Every tool-profile turn invokes at least one tool and no single-step turn invokes a tool.
5. Every session-continuity check recalls its own opaque setup marker; no reply contains a
   marker belonging to another tenant/session. Setup, continuity, tool, or isolation failure
   is disqualifying.
6. The measured process remains within all provisional bounds: peak-memory growth at most
   512 MiB, at most 64 additional threads, at most 256 additional open file descriptors,
   state growth at most 10 GiB, audit growth at most 512 MiB, and disk free space never below
   20%.
7. Every authenticated resource sample succeeds, every turn contains queue/tool/retry
   telemetry, every load client is still issuing requests at the deadline, by its own
   account, and terminates after the run; and every client completes at least one successful
   request for each 135 seconds of the planned run (26 over the hour).
8. The first turn after a compiler replacement pays one spawn plus one handshake (~16 ms
   measured locally) out of its own budget and therefore has a different latency profile;
   `sigil_pi_runtime_unhealthy_replacements_total` must not climb during a qualifying run.
   The harness reads `runtime_unhealthy_replacements_total` with every resource sample; a
   sample without it, a rise, or a counter that goes backwards fails the run.

These are initial engineering bounds and need operations approval before they become release
limits. Passing them does not replace the 30-day pilot SLOs, security review, recovery drills,
or approval of the implemented tenant quota values and reservations.

## Qualification procedure

Create a `0600` credential file outside the repository. It contains an `ops:read` token and
at least 25 tenant chat tokens whose policies authorize `list_dir`:

```json
{
  "ops_token": "from-secret-manager",
  "tenants": [
    {"tenant": "load-tenant-01", "chat_token": "from-secret-manager"}
  ]
}
```

Run the harness from the release checkout, replacing the digest and accountable approver:

```sh
.venv/bin/python scripts/load_test.py \
  --target https://candidate.example \
  --credentials /secure/load-credentials.json \
  --artifact-digest <64-lowercase-hex> \
  --version 1.0.0 \
  --forecast-approved-by "<accountable product owner>" \
  --output /secure/load-report.json
```

The harness refuses group/world-readable or symlinked credentials, never copies credentials
or tenant names into its report, creates the report as `0600`, and refuses to overwrite it.
It also refuses non-loopback plaintext targets. It refuses redirects, so the bearer token goes
only to the target, and builds one TLS context for the run before any client starts.
`--minimum-requests-per-client N` raises the per-client floor and cannot lower it. A response
the harness does not expect is a failed request and its client carries on; a client that
stops for any other reason is named in `clients.records` with what stopped it, and the run
cannot pass. `scripts/check_readiness_evidence.py` requires the client records, the floor,
the unchanged counter, the recorded configuration and complete telemetry from the report
itself, and recomputes the resource bounds from its samples; a report from the earlier
harness, or from a release that does not report its settings (the frozen v0.4.0), does not
validate. Use `--smoke-test` for a deliberately short
mechanics check; smoke reports are always marked `qualification_eligible: false` and exit
nonzero, even if their measured assertions pass.

After a qualifying pass, retain the raw JSON in the approved evidence system and write
`docs/evidence/load-test.md` with the artifact digest, date, topology, command parameters,
raw-evidence location, result, exceptions, and reviewer. No qualifying run has occurred yet.
