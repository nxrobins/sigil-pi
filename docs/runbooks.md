# sigil-pi v1 operator runbooks

These runbooks cover the principal product failure modes. They assume the one-host local-POSIX
topology, authenticated operational endpoints, structured logs, an external process
supervisor, encrypted backup storage, and an alert router implementing
`config/alert-policy.json`. Following a document is not exercise evidence: every launch
environment must route the alert and record a timed drill against the exact candidate.

Never bypass authentication, audit verification, TLS, retention, quota, or state-integrity
checks to restore readiness. Preserve the request ID and artifact digest in every incident
record; never paste credentials, messages, replies, raw audit records, session names, or
customer files into tickets or alert annotations.

## Service not ready

Trigger: `service_not_ready` or authenticated `GET /v1/ready` returns `503`.

1. Stop the rollout and keep any still-ready instance serving. Do not repeatedly restart all
   workers or delete the state directory.
2. Record `GET /v1/version`, the candidate digest, readiness status, and the content-free
   `dependency_components`, `audit_verification`, `retention`, and `scheduler` metric
   objects. Record `sigil_pi_runtime_generation` and
   `sigil_pi_runtime_unhealthy_replacements_total` too: a climbing replacement count is the
   signal that the compiler, not a tenant, is at fault.
3. Use the failed component to select the recovery path. For `audit_verification`, read the
   `audit_verification` metric object: `writes_healthy=false` means an audit record could
   not be written, so use [Audit write failure](#audit-write-failure), which also covers a
   chain that then fails verification; otherwise use
   [Audit verification failure](#audit-verification-failure). For `retention`, use
   [Retention cleanup failure](#retention-cleanup-failure); for `scheduler`, use
   [Scheduler not running](#scheduler-not-running); for `state_storage` or
   `quota_store`, use [Corrupt state or failed restore](#corrupt-state-or-failed-restore);
   and for `schedule_store`, preserve the file and follow that same corrupt-state procedure.
   A failed `runtime` component means the host tried repeatedly to obtain a working compiler
   and could not. An ordinary forge-deadline kill no longer appears here: that compiler is
   retired and replaced automatically, and only exhausted replacement fails this component.
   Replace the instance and preserve the generation-tagged stderr (`[genN]` prefixes identify
   which compiler produced which line). Never edit a failed dependency merely to turn its
   gauge green.
4. Verify the replacement reports the expected version and `200 ready`, then send one
   synthetic tenant-isolated canary through the normal client. Confirm no new critical event
   and no increase in stable error classes.
5. Escalate to engineering if readiness does not recover after one controlled replacement;
   preserve logs and a state-volume snapshot before any further action.

## Unhealthy compiler replacement

Trigger: `compiler_replaced_unhealthy`, a rise in
`sigil_pi_runtime_unhealthy_replacements_total` (`runtime_unhealthy_replacements_total` in
the JSON metrics).

The host replaced a compiler for a reason other than a caller's own expired turn budget: the
compiler did not finish an exchange within `PI_MCP_TIMEOUT_SECONDS`, sent a response over the
64 MiB cap, or failed its handshake. The turn whose forge met the fault failed; the service is
still ready, because a new compiler took the old one's place. Repeated replacements within a
short window exhaust the runtime, and readiness then fails through the `runtime` component.

1. Record `GET /v1/version`, the digest, `runtime_generation`, the counter's value and the
   time of each rise, and the request IDs of the turns that failed around them. Do not copy
   compiler stderr or guest output into the alert channel.
2. Read the generation-tagged compiler stderr (`[genN]` prefixes) of the replaced
   generations, and check the host's memory, CPU and open file descriptors against their
   limits: a starved host makes a healthy compiler miss its deadline.
3. If the rises follow one tenant's turns, a guest may be driving the compiler into the
   fault: preserve that session's audit chain and treat it as a possible
   [Isolation or secret-exposure incident](#isolation-or-secret-exposure-incident).
4. Correct the host fault. Never raise `PI_MCP_TIMEOUT_SECONDS` to silence the alert, and
   never put another binary in place of the bundled compiler.
5. Verify the counter stays flat for a full window, readiness is `200`, and one synthetic
   canary turn is answered `200`. If readiness has failed, use
   [Service not ready](#service-not-ready). Escalate to engineering with the stderr excerpts
   if the counter keeps rising on a healthy host.

## Compiler does not match its pin

Trigger: startup, or a compiler replacement, fails with `configuration error: forge binary does
not match SIGIL_REV: runtime/target/release/sigil-mcp does not match the SIGIL_REV pin for
<platform>: pinned <a> actual <b>`.

1. The bundled compiler is not the one the bundle was built with: a damaged copy or a swapped
   binary. Do not edit `app/SIGIL_REV` and do not start the service from another binary.
2. Verify the release archive against its published digest and attestation
   (`docs/operations.md`), reinstall the bundle from it, and start again.
3. If the reinstalled bundle reports the same mismatch, or the installed binary changed without
   a deployment, treat the host as compromised: preserve it and notify the security owner.

## Audit verification failure

Trigger: critical `audit_verification` event, `audit_verification_failed`, or
`audit_verification.healthy=false`.

1. Treat this as a security incident. Stop rollout and retention/deletion activity; readiness
   already fails closed. Restrict state-volume access and notify the named security owner.
2. Record only chain/record counts, times, version, digest, and the incident ID. Do not copy
   verifier diagnostics or file names into the alert channel.
3. Cleanly drain the process if possible. Preserve a forensic copy of the state volume and
   external audit-key metadata separately. The ordinary backup command is expected to reject
   an invalid chain; do not weaken it.
4. Security determines whether the cause is truncation, unauthorized modification, key
   mismatch/rotation, storage failure, or software defect. Restore only a separately verified
   backup into a new path using the recovery drill; never rewrite a chain to make it pass.
5. Return traffic only after the restored/current chain verifies, readiness is `200`, the
   critical event clears, and security records containment plus disposition. Any suspected
   unauthorized modification remains a launch-blocking incident.

## Audit write failure

Trigger: critical `audit_write` event, `audit_write_failed`,
`audit_verification.writes_healthy=false`, a chat turn answered `500 audit_write_failed`, or a
schedule listing `last_status` `audit_write_failed`.

The host could not append the audit record of a guest that had already run. That turn failed
and its result was withheld, so no client received work without a record; but the guest ran,
and any change it made in the session's sandbox has no record. Readiness fails through the
`audit_verification` component and stays failed until the process is replaced: a verification
that passes afterwards proves nothing, because the chain a failed append leaves is a valid
prefix and still verifies.

1. Stop the rollout and notify the named security owner: an execution ran that the audit trail
   does not record. Record `write_failures_total`, the first failure time, the request IDs
   answered `audit_write_failed`, `GET /v1/version`, and the digest. Do not copy file names,
   OS errors, or verifier diagnostics into the alert channel.
2. Find the environmental fault: state-volume free space (for a full volume use
   [Disk capacity or full disk](#disk-capacity-or-full-disk)), ownership and permissions of the
   audit directory, open file descriptors against the process limit, and I/O errors in the host
   and supervisor logs. While the fault lasts, every turn that runs a guest fails the same way.
3. A torn final record, an append the fault cut short, fails every later turn of that session
   until the chain is archived, fails verification at the next check, and stops a replacement
   from starting. Never truncate, edit, or delete the chain in place: follow
   [Audit verification failure](#audit-verification-failure), under which security archives the
   torn chain or restores a separately verified backup.
4. Correct the fault and replace the instance gracefully. Startup verifies every chain before
   it serves, and the replacement starts with `write_failures_total` at 0.
5. Verify readiness `200`, `audit_verification.writes_healthy=true`, a stable
   `write_failures_total`, and one synthetic canary turn answered `200`, and that security has
   recorded the disposition of the unrecorded executions. Escalate to engineering if writes
   fail again on a healthy volume; preserve the logs and a state-volume snapshot first.

## Retention cleanup failure

Trigger: critical `retention_cleanup` event, `retention_cleanup_failed`, or
`retention.healthy=false`.

1. Stop rollout and confirm audit verification remains healthy. Do not manually remove quota
   registry rows: cleanup deliberately deletes the registry last so retry remains attributable.
2. Check state-volume free space, permissions, file types/symlinks, schedule-store integrity,
   and the content-free retention counters. Preserve the first failure time and request IDs.
3. Correct the environmental fault. Restart one instance gracefully; startup reconciliation
   and the initial sweep safely retry incomplete cleanup under the session lock.
4. Verify `retention.healthy=true`, readiness `200`, the failure counter is stable, and active
   sessions/schedules remain present. Confirm the published deletion SLA was not breached.
5. Escalate to engineering/privacy owners if retry fails or the deletion SLA is at risk. Do
   not disable or lengthen retention as an incident workaround.

## Scheduler not running

Trigger: `scheduler_not_running`, `scheduler.healthy=false`, a failed `scheduler` readiness
component, or a critical `scheduler_tick` event.

1. Stop the rollout. No tenant's schedule runs on this instance while the alert stands.
   Interactive turns still work, but readiness fails with the scheduler, so an edge that acts
   on readiness stops routing to the instance. Record the content-free `scheduler` metric
   object, `GET /v1/version`, and the first failure time.
2. Read that object. `thread_alive=false` means the scheduler thread has ended, and only a
   restart brings it back. `thread_alive=true` with `last_progress_age_seconds` above
   `stall_after_seconds` means the thread is alive but has neither completed a tick nor
   recorded a scheduled run for longer than the schedule lease plus the tick interval:
   either every tick is failing (`tick_failures_total` is rising and `last_tick_age_seconds`
   keeps growing) or one scheduled turn or store operation has not returned. Scheduled turns
   run on a pool, so one turn that does not return no longer stops the other tenants'
   schedules, and their runs keep `last_progress_age_seconds` within the bound.
   `healthy=false` with `thread_alive=true` and `last_progress_age_seconds` within
   `stall_after_seconds` means a scheduled turn has been in flight for longer than
   `stall_after_seconds`: that tenant's schedules wait behind it.
3. A critical `scheduler_tick` event followed by an informational one with `status=ok` is a
   run of failed ticks that ended: the scheduler kept running and needs no restart, but find
   the cause before it returns. A tick fails when the schedule store cannot be read or
   rewritten, so check open file descriptors against the process limit, state-volume free
   space and permissions, and the `schedule_store` readiness component. For a full volume
   use [Disk capacity or full disk](#disk-capacity-or-full-disk); for a store that cannot be
   decoded, preserve the file and use
   [Corrupt state or failed restore](#corrupt-state-or-failed-restore).
4. Correct the environmental fault. If the thread has ended or is still stuck, replace the
   instance gracefully. A scheduled run whose completion could not be recorded keeps its
   claim until `PI_TURN_LEASE_SECONDS` has passed and then runs again, so expect that one
   turn to repeat; do not edit the schedule store to release the claim.
5. Verify `scheduler.healthy=true`, readiness `200`, a stable `tick_failures_total`,
   `last_tick_age_seconds` returning to about one second while no scheduled turn is running,
   and that one synthetic schedule runs and lists `last_status` `ok`. Escalate to engineering
   if a replacement stops again; preserve the logs and a state-volume snapshot first.

## Disk capacity or full disk

Trigger: `disk_capacity_critical`, an `ENOSPC` supervisor event, or a stable `500`/`507`
coincident with state-volume exhaustion.

1. Stop new rollout and shed traffic through the authenticated edge. Do not delete active
   session, audit, quota, or schedule files by hand.
2. Record free/total disk metrics and growth rates for state, audit, and sandboxes. Identify
   whether approved encrypted backup storage or the active state volume is exhausted.
3. Reclaim only approved expired backups through the deployment retention system, or add
   capacity. Authorized session deletion and normal active retention are the only supported
   active-state reclamation paths.
4. Gracefully replace any instance that encountered a failed write. Startup reconciliation
   repairs exact quota sizes; atomic session/schedule writes retain the prior committed copy.
5. Verify disk free ratio is above 20%, readiness is `200`, audit verification passes, quota
   reconciliation is clean, and a canary session survives restart. Escalate if any corrupt or
   unattributed state is detected.

## Model provider or network failure

Trigger: `service_error_rate_slo`, repeated stable `agent_failure`, provider retry growth, or
provider/network alerting.

1. Confirm product readiness, TLS, DNS/egress, provider status, and that the configured endpoint
   host matches its minimal network grant. Never widen the allowlist as a diagnostic shortcut.
2. Use request IDs and stable failure classes; do not inspect or export customer prompts.
   Distinguish provider `429`/5xx from permanent authorization/grant failures.
3. Allow only the host's bounded transient provider retries. Do not replay whole chat turns
   after ambiguous `502`, `504`, or transport outcomes; reconcile through session export or ask
   the user because a bounded transcript may already be committed.
4. Restore the approved endpoint/credential/network path or shift traffic through the
   pre-approved provider procedure. Verify error-rate recovery and one synthetic single/tool
   canary without changing product capability grants.
5. Escalate to operations/provider owners when the 0.5% service-error objective is breached;
   record duration and affected stable error classes for pilot SLO accounting.

## Corrupt state or failed restore

Trigger: startup reports corrupt/unattributed state, a restore rejects its archive, or an
audit/quota/state integrity check fails.

1. Keep the affected copy offline and immutable. Do not edit JSON, SQLite, audit chains, or
   manifests in place and do not point another release at the only production state copy.
2. Record the stable error, artifact and backup digests, schema versions, and operation phase.
   Preserve the rejected input under incident access controls.
3. Select the newest independently verified encrypted backup within the RPO and run the
   bundled release drill into a new work/state path. A traversal, checksum, schema, audit,
   quota, active-lease, or attribution rejection is a hard stop.
4. Switch traffic only after old/candidate/rollback probes, committed-session continuity,
   readiness, version, and audit checks all pass. Keep the pre-incident and restored copies
   until security/operations approve disposal.
5. Escalate immediately if no backup meets RPO, restore exceeds four hours, or customer state
   differs; these are launch/SLO incidents, not recoverable warnings.

## State reconciliation failure

Trigger: critical `state_reconciliation` event at startup.

1. The ledger recorded bytes for sessions whose files are now gone: `sessions_missing_storage`
   counts sessions with no byte left of their conversation and sandbox,
   `sessions_missing_audit` those with no byte left of their audit chain. Treat it as data
   loss until shown otherwise. The service is serving; do not restart it to clear the event,
   which is emitted once, by the startup that finds the loss.
2. Record the counts, the time, the version, the digest and the incident ID. The event names
   no tenant and no session. To find them, compare `session_usage` in the newest verified
   backup's `product-quotas.sqlite3` with the live ledger: the affected rows now record zero
   bytes for the lost surface.
3. Find the cause before changing anything: a power loss on a host that is not power-loss
   durable (see the operations guide), a failed or replaced volume, a restore that brought
   back the ledger without the files, or files removed by hand.
4. Restore the affected sessions from the newest verified backup into a new path with the
   recovery drill, or tell the affected tenants what was lost. Do not edit the ledger to hide
   the loss.
5. A missing audit chain is also a gap in the audit record: notify the security owner, as for
   an audit verification failure.

## Failed graceful shutdown

Trigger: the supervisor reports nonzero exit/drain timeout or no clean-shutdown marker.

1. Keep the instance out of readiness and do not run offline backup—the missing marker is an
   intentional consistency refusal. An online backup does not look at the marker. Take the
   next one once the service is ready again.
2. Record active-turn count, scheduler/retention/audit monitor status, deadline, version, and
   signal time. Do not force a second state writer onto the same session.
3. Allow the lease timeout to expire, then start one replacement. Startup reconciles stale
   leases and exact usage before accepting traffic.
4. Verify readiness, zero stale active leases, audit integrity, and a synthetic continuity
   canary. Then perform a new clean drain before authorizing a backup.
5. Escalate if state reconciliation or the replacement drain fails; preserve the volume before
   manual investigation.

## Isolation or secret-exposure incident

Trigger: any cross-tenant marker, unauthorized tool/state access, suspected credential output,
or critical/high security finding.

1. Immediately stop affected traffic and revoke credentials through the documented
   digest-removal/rolling-restart path. Preserve state, logs, audit key custody records, and
   exact artifacts; do not delete evidence.
2. Notify security and privacy owners. Use tenant/session hashes and incident IDs only in broad
   channels. Never reproduce the suspected secret or customer content to confirm it.
3. Verify audit chains and determine affected principals, tenants, surfaces, and time window
   under restricted forensic access. Any confirmed cross-tenant access is a launch blocker.
4. Restore service only from reviewed artifacts/state after credential rotation, containment,
   and explicit security approval. Exercise the tenant-isolation canaries before traffic.
5. Record incident severity, resolution, customer/legal notifications, and pilot impact. The
   GA gate requires no unresolved critical/high security incident.
