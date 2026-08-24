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
   `dependency_components`, `audit_verification`, and `retention` metric objects. Record
   `sigil_pi_runtime_generation` and `sigil_pi_runtime_unhealthy_replacements_total` too: a
   climbing replacement count is the signal that the compiler, not a tenant, is at fault.
3. Use the failed component to select the recovery path. For `audit_verification`, use
   [Audit verification failure](#audit-verification-failure); for `retention`, use
   [Retention cleanup failure](#retention-cleanup-failure); for `state_storage` or
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

## Failed graceful shutdown

Trigger: the supervisor reports nonzero exit/drain timeout or no clean-shutdown marker.

1. Keep the instance out of readiness and do not run offline backup—the missing marker is an
   intentional consistency refusal.
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
