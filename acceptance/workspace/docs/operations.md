# Harbor operations

## Deployment

The deployment owner is Jon Bell. Run these steps in order:

1. Verify the candidate checksum.
2. Pause new indexing submissions.
3. Wait for running work to finish and record any uncertain result.
4. Take a backup of the settled state.
5. Start the candidate and check readiness.
6. Resume submissions only after readiness succeeds.

The readiness path is `/readyz`. Do not confuse it with `/healthz`, which only
reports that the process is responding.

## Failure and recovery

A provider timeout after a request was sent is an uncertain result. Do not retry
automatically. The operator first reconciles the operation identity with the
provider's record. Current configuration permits only one attempt.

A failed readiness check keeps submissions paused. Restore the last settled backup
and start the previous verified candidate; check `/readyz` before resuming work.

The demo recovery target is 30 minutes. This is a Harbor fixture value, not the
sigil-pi MVP's recovery acceptance threshold.

## Audit

Record the candidate version, operation identity, observed result and operator name.
Never put bearer tokens or provider keys into the audit record.
