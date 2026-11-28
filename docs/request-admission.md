# SIGIL-owned request admission

Status: **DEVELOPMENT INTEGRATION — the expanded browser integration passed its
2,702-case source gate on unchanged inputs**. This is not legacy-route parity or MVP qualification.
See the [acceptance record](mvp-acceptance.md) for exact evidence and limitations.

## Ownership and explicit configuration

The native v8 profile adds AH6/HC6 request facts and a bounded `read_many` mechanism.
It requires automatic execution and the same explicit response-header inventory
as v7. Older profiles do not inherit request admission or new commands implicitly.
Native code supplies observations, scoped mechanisms and independent ceilings;
SIGIL owns credential validity, request allowance, route permissions and responses.

[request_entry.sigil](../app/pi/request_entry.sigil) wraps the same SIGIL API core.
It calls the grantless [request_policy.sigil](../app/pi/request_policy.sigil), which
combines [request_rate.sigil](../app/pi/request_rate.sigil) with the unchanged
submission decoder. Python provides build composition and test transport only.

The build entry points are
`scripts.compose_request_entry.compose_request_entry(repo, stdlib, request_limit=...)`
and `scripts.compose_request_policy.compose_request_policy(repo, stdlib)`.
The request limit is a mandatory positive signed-64-bit build input, embedded in
the executable and bound in its source/input digests. There is no implicit pilot
default. Changing it changes the application bundle and requires explicit
compatibility/recovery review, not a silent live-policy edit.

The current statically configured integration fixture is
[request_api_support.py](../tests/request_api_support.py). It registers the fixed
policy with no I/O grants and preserves all existing worker, effect, store, fuel,
memory, timeout and eight-entry-evaluation bounds. It is test/development setup,
not production packaging or an approved deployment profile.

## Durable request path

1. SIGIL validates native-matched credential facts and current validity. Missing
   or inactive credentials cannot read the request window or start effects.
2. SIGIL requests the tenant's actual `request-window` snapshot. The pure policy
   returns either nonadmission or a conditional request-count update plus a held
   body plan. Authorized operation submissions use the unchanged decoder; their
   400/413 errors are deferred until accounting is committed. Other routes and
   missing route scopes keep the raw body for the original permission precedence.
3. The entry commits the proposed accounting update and requires the actual
   positive native receipt before invoking domain work or releasing the deferred
   error. Exhaustion returns 429 and bounded Retry-After without another write.
   A failed commit does not authorize the held body or create an operation.
4. For operation submissions, SIGIL selects the three independently known dedup,
   session and active-budget coordinates and validates their combined observation.
   It interprets the session's prior-operation reference and reads that record
   separately before calling the unchanged turn-admission function.

The longest demonstrated follow-up uses eight entry evaluations: request-window
read, policy call, accounting commit, three-record read, prior-operation read,
turn-admission call, operation commit, reply. A successful quota charge does not
guarantee later acceptance, successful work or a delivered client response.

RW1 counts use epoch-aligned 60-second windows, shared by credentials in the same
tenant. They persist across restart/credential rotation. A wall-clock observation
does not prove trusted time. Refusal to move an existing window backwards is a
draft difference from the Python reference and remains subject to M0 review.

## Native facts and read boundaries

AH6 preserves AH5's first fifteen fields and adds a non-secret exact-`Bearer `
presentation bit and a subsecond bit from the SAME sample as its whole-second
clock. Raw authorization, arbitrary headers, cookies and provider keys are not
added to guest input. SIGIL interprets these observations; native transport
normalization and application errors remain distinct compatibility surfaces.

HC6 alone admits `read_many`: one through three unique exact namespace/key pairs,
at most 4,096 query bytes, and one scoped SQLite snapshot. Every address is
authorized before reading. The complete framed result is bounded by 2 MiB; no
healthy prefix is exposed when another member fails. SIGIL validates RM1/RB1/RR1
count, coordinates, revisions, presence and framing. Snapshots do not replace
conditional revision checks during later writes. No domain traversal, tenant
policy, new grant or broader execution/storage limit is introduced in the host.

The v8 entry at fixture limit 2 is 63,349 bytes, below the existing 65,536-byte
limit. [sigil_layout.py](../scripts/sigil_layout.py) removes only supported layout
whitespace; it preserves quoted bytes and refuses unsupported lexical forms.
Tests compare full token/literal streams through the pinned compiler's real
lexer, including a changed-literal negative control. Oversize layout provenance
is test data, never an admitted executable or exception to verification.

## Compatibility and qualification limits

The [frozen v7 host](../tests/fixtures/native-host-v7/README.md) contains forty
byte-bound original native files. Real-executable tests show it rejects v8 before
creating state and against an existing populated store. With an UNCHANGED v7
configuration, old -> new -> old host reopening retains a tool turn, a new
follow-up, history/discovery, response correlation, tenant boundaries and replay
without additional effects. This does not qualify changing the application bundle
to v8 while old operations are in flight.

The staged suite used a local deterministic provider, not real-model usefulness
qualification. Full grouped-read failure/replay precedence, header normalization,
emergency readiness, request-ID body/log parity, all successful legacy routes,
the complete fault matrix and M0 pilot-policy
approval remain open. Original v7 tests retain their whole-state assertions;
v8 tests separately require exact request-accounting changes and unchanged domain
records for reads/replays/terminal control. No readiness gate is marked PASS.

The added v8 browser suite passed fourteen real browser cases plus a fixture-sharing
check on unchanged inputs. Quota refusals retain the exact submission for explicit
retry or the already accepted operation for explicit status checks, without automatic
retry or fabricated completion. Real credential expiry clears actual retained content;
the refused request adds no accounting charge and a second tenant reconnects without
the first tenant's content. Existing lifecycle/recovery drivers remain required.
Only bounded Retry-After guidance changed in the browser client; no browser policy,
timer, native limit or grant was added. These changes are integrated, but the expanded
whole-source run, broader browser boundaries and candidate evidence remain required.
