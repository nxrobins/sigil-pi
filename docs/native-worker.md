# Native one-use worker mechanism

Status: **EXECUTABLE LOCAL COMPONENT — NOT AUTHENTICATED PRODUCT DISPATCH**.
Protocol: `sigil-worker/v1`. Sources: [bridge](../native/worker/src/lib.rs),
[Unix transport](../native/worker/src/transport.rs), [stdio adapter](../native/worker/src/main.rs),
and [locked dependencies](../native/worker/Cargo.lock).

This repo-local Rust component supplies fixed-source, fixed-grant execution with
one-use in-memory tickets and bounded worker transport. It contains no conversation,
company, authorization-policy, scheduling-policy or business-retry implementation.
It does not replace the product API or modify the pinned SIGIL runtime.

## Prepared execution and observations

A trusted local caller boots one bridge with a fixed runtime identity, SIGIL source,
grants and independent fuel/time ceilings. `prepare` holds the exact input, requested
fuel and monotonic deadline, then returns a random ticket/generation and input digest.
It does not start a worker. Only one unexpired preparation may exist at a time.

`execute` accepts only the matching ticket. It consumes that preparation before
any launch, send or failure; repeating the ticket cannot execute again in that bridge.
A wrong ticket does not consume the valid pending preparation. Every execution uses
a fresh runtime process and guest instance. It has no automatic retries.

In the [durable fixture](../tests/test_durable_turn.py), the sequence is:

1. Read the committed minimal intent and prepare its fixed effect in the native bridge.
2. Have the shared SIGIL executor produce a claim containing that native generation.
3. Commit the claim, then execute the ticket once.
4. Have SIGIL produce a delivery from the correlated observation, commit it, then let
   the pi SIGIL application interpret the result and produce its next state/intent.

The fixture now uses a [native durable-claim gate](claimed-worker.md) around this
bridge. That gate binds actual scoped intent reads, the prepared generation and its
own successful claim commit before allowing execution; a retained claim prevents
preparation at that coordinate after reopening. The bare bridge still has no storage
knowledge. Authenticated artifact/input/grant selection and semantic operation
admission remain product-integration requirements.
The draft uses the same random value for ticket and generation; neither is an
authenticated operation identity or a secret authorization credential. The stdio
channel is trusted and must not be exposed as a public or guest-callable API.

An execution observation contains:

| Field | Meaning |
|---|---|
| `generation` | The consumed preparation's identity, not a process ID |
| `request_may_have_run` | At least one forge-request byte was successfully written to the child pipe; a deliberately conservative signal, not proof of HTTP delivery |
| `worker_reaped` | The directly owned child was reaped, or no child was started; not proof that every descendant or remote effect stopped |
| `fault` | Static local failure code, or null when a valid runtime result was observed |
| `result` | Strictly decoded runtime result, or null; its own `status` may be `error` |

Initialization traffic alone does not set `request_may_have_run`. A runtime error,
HTTP response, or stopped local process is not a business-success/rollback verdict.
No observed forge bytes plus trusted runtime behavior can establish that this bridge
did not request the effect; callers still need the full committed-attempt context.
If a claim exists but its owner's observation was lost, SIGIL preserves possible
delivery even when a test fixture knows the request had not actually been sent.

## Bootstrap and authority limits

The version-1 bootstrap requires absolute runtime/source paths with exact lowercase
SHA-256 digests. It rejects final-component symlinks, nonregular files, group/other
writable files, setuid/setgid modes, oversized files and digest mismatches. Source is
limited to 65,536 bytes, decoded once as UTF-8 and held in memory; changes to the
source path after preparation cannot replace the held program. Runtime bytes are
checked again before each launch, with a 256 MiB ceiling.

This is **not race-free authenticated artifact admission**: parent path components
are not descriptor-bound, runtime launch resolves a pathname after hashing, and a
trusted local package/filesystem is assumed. Hashes alone do not establish publisher
identity, verification provenance, host-contract compatibility or target approval.
The real configured runtime still compiles and verifies SIGIL on every execution;
there is no AOT deserialization or compile-once performance claim here.

The bootstrap fixes `net`, `fs` and `secret_env` for the bridge lifetime. Per-request
source/runtime/grant changes, extra fields, duplicate decoded JSON keys at any depth,
and type-coerced numbers are rejected. Bootstrap grants are operator/fixture inputs,
not SIGIL-authorized operation grants; full grant validation and policy admission
remain required before product use. The current tests select separate provider-only
and filesystem-only bridges, not a combined application-wide grant.

Only explicitly named parent environment secrets are loaded. Secret names and values
are bounded, and empty/newline/NUL-bearing values are refused. Provider values enter
the privileged MCP grant request, not guest input; the pinned runtime performs secret
injection. Each child starts with an empty environment except `LANG=C.UTF-8`, so
ambient credentials, proxy configuration and the unverified-certificate override are
not inherited. Worker stderr is counted and discarded, not returned in observations.
This is not a general output-redaction guarantee: an authorized remote provider can
return arbitrary content, and malformed/malicious approved runtimes are not admitted
by these tests. Secret-safe release admission and product redaction still matter.

## Time, cancellation and resource bounds

- Per-preparation fuel is positive and at most the configured ceiling (bootstrap
  maximum 1 billion). Its monotonic deadline starts during preparation, includes
  the wait for a claim and runtime launch, and is at most 300 seconds.
- Initialization is further limited to five seconds within that same deadline.
  Nonblocking stdin/stdout/stderr and polls of at most 20 ms prevent partial frames,
  blocked writes or continuous stderr from bypassing the transport's timer checks.
- Input is at most 4 MiB; each encoded child request/response including LF is at
  most 16 MiB; aggregate worker stderr is at most 256 KiB. JSON escape expansion can
  make a size-valid input exceed the wire ceiling and be refused before forge send.
- Duplicate/invalid JSON, wrong response IDs, unexpected envelope fields and multiple
  frames in a single read retire the worker. Only the correlated result is returned;
  buffered trailing output cannot become a later operation's result because the
  process is never reused. Inner results are also parsed with duplicate-key rejection.
- Cancellation before execution consumes the pending ticket without launch. The Rust
  API also accepts a concurrent cancellation flag during execution. The serial stdio
  adapter currently exposes only pre-execution cancellation, not in-flight cancellation.
- A stopped worker's process group receives SIGKILL before the leader is reaped,
  avoiding signals to a reused PID. The direct child is polled for reaping for up to
  two seconds. If cleanup cannot be confirmed, the bridge refuses subsequent work;
  it does not claim the old worker stopped. Handles also check their owning process.

This is bounded process/pipe supervision under a responsive local OS, not a proof
that filesystem reads, process creation, parsing, uninterruptible kernel waits or
all cleanup paths finish within a universal hard wall-clock bound. OS-level CPU,
memory, descriptor and process limits and the supported deployment envelope remain
to be qualified. Killing a local process cannot undo an external side effect.

On Linux the child also requests a parent-death SIGKILL and rechecks parent identity
after installing it. Linux defines this against the creating **thread**, and it is
not automatically inherited by descendants. macOS has no equivalent mechanism in
this implementation. The ready flag reports which code was compiled, not successful
qualification of parent-death behavior. This local macOS run does not test the Linux
path or establish whole-tree fencing across controller crashes. See the primary
[Linux parent-death contract](https://man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html)
and [Rust child-process hooks](https://doc.rust-lang.org/std/os/unix/process/trait.CommandExt.html).

## Verification and remaining integration

The required pytest fixture builds this component from its lockfile and runs Rust
formatting, Clippy and native tests before the executable integration cases. Tests
cover ticket replay, pre-start and in-flight cancellation, deadlines with partial
handshakes/responses and blocked writes, stdout/stderr limits, protocol confusion,
runtime replacement and a clean child environment. Real solver-verified SIGIL cases
cover immutable source/input, one observed provider request before timeout, recovery
with a fresh worker, scoped provider-secret injection, and durable model/file/response
execution with native storage restarts. These are local component assertions, not
an independent security review or a Rust/SIGIL coverage percentage.

Process-fault unit fixtures run serially: their deliberate pipe floods and short
deadlines must not prevent a neighboring fixture from reaching its intended phase.
The concurrent cancellation test still signals an actively running worker from a
second thread. Production deadlines and fault assertions are unchanged; this test
isolation is not a substitute for the product's separate concurrent load qualification.

The [claim gate](claimed-worker.md) now enforces actual snapshot/receipt binding and
create-once execution at a retained claim coordinate. The newer
[SIGIL dispatch policy](dispatch-policy.md) consumes this bridge's actual held
source/runtime hashes, grants and secret names before the gate is prepared; secret
values and environment-variable names are excluded from that metadata. The
[native-bound completion path](worker-completion.md) now supplies this bridge's actual
observations to the fixed SIGIL producer and owns its exact result commit. Abandoned
claims recover conservatively without asserting old-worker termination. The new
[automatic v4 API service](automatic-service.md) embeds these mechanisms; older
component evidence remains scoped to its controlled fixtures. Still required: complete authenticated
immutable operation/authority/artifact/input/grant admission; compiled artifact/import
provenance and actual output routing; qualified Linux owner-death/cancellation;
application quota/expiry/revocation and API cancellation policy.
[All M0–M8 gates](mvp-goal.md) remain unqualified. AIN compatibility
and a real control-plane consumer need their separate reviews; this mechanism makes
no AIN backend or cross-project conformance claim.
