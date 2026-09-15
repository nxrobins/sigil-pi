# SIGIL-owned HTTP correlation and bounded response metadata

Status: **DEVELOPMENT INTEGRATION — staged 154-case verification and expanded
2,429-case local source gate passed**. This is a prerequisite for the legacy API
migration, not a migrated legacy route or an MVP gate pass.

## Application and native ownership

The explicit native v7 profile runs the same SIGIL API/discovery core through
the [HTTP entry wrapper](../app/pi/http_api.sigil). SIGIL selects the request's
correlation label using [one policy implementation](../app/pi/http_request_id.sigil).
The [build recipe](../scripts/compose_http_entry.py) composes this code before
execution. Python does not route requests or decide production responses.

Native supplies only a fresh independent 128-bit random label, the number of
`X-Request-ID` header values, and the first value's raw bytes. SIGIL accepts the
first value if it is 8–64 ASCII bytes drawn from letters, digits, dot, underscore,
colon and hyphen; otherwise it uses the fresh label. A later duplicate value
cannot override an invalid first one. This matches the Python reference policy.

Correlation is NOT authority, an operation identity or an idempotency key.
Changing a trace label cannot change durable replay identity or tenant scope.
The native operation identity uses separate entropy acquisition. Held facts do
not change during a request's continuation, and body fields cannot replace them.
No arbitrary header map, raw Authorization header, cookie or provider secret is
passed as metadata to SIGIL. This does not scrub secrets a caller intentionally
places in message text or in the correlation hint itself.

## Explicit protocol and limits

v7 requires automatic execution and an explicit `http.response_headers` inventory.
Older v3–v6 configurations retain their existing behavior and cannot opt into the
new metadata implicitly. Missing, null, unknown or mismatched configuration fails
before application state creation. The SIGIL bootstrap accepts exactly these
canonical inventory entries:

`retry-after`, `server-timing`, `www-authenticate`, `x-request-id`,
`x-sigil-retries`, `x-sigil-tool-calls`.

AH5 adds RF1 request facts and this inventory to AH4. HC5 retains the existing
five command fields and command set; only its reply payload becomes an HR1 frame
containing an inert content type, HH1 header fields and the response body. The
host bundle binds the version, inventory, content types and mechanism limits.
The current wrapper emits only `x-request-id`, preserving the original SIGIL
status, JSON body, continuation and action-time guard. Boot approval stays guarded,
204 and empty, without a correlation header. Host-level refusals may have no
correlation header because no application response was produced.

The native response mechanism permits only JSON and the two declared plain-text
media types. It accepts at most 16 explicitly inventoried response-header names,
8,192 encoded header bytes and 1,024 printable ASCII bytes per value. Duplicate,
undeclared, malformed and over-limit headers fail closed. Native retains HTTP
framing and browser hardening; cookies, redirects, active content, CORS and browser
security-header control are not delegated. Common proxy-effect headers are also
refused. Any other operator-selected custom header needs review against the actual
proxy environment; an `x-` prefix alone is not a universal inertness guarantee.

The existing HTTP cap remains **64 total request headers**, not 64 correlation
headers in addition to other fields. A fixture with four fixed fields accepts
60 hints and gets 431 at 61 hints. The separate RF1 mechanism accepts at most
64 hints and 65,536 aggregate raw hint bytes; the transport's stricter whole-request
limit still applies first. The 64 KiB SIGIL source limit, 4 MiB entry-input limit,
2 MiB command/output limit, eight entry evaluations, fuel, grants, worker/request
deadlines and fixed-evaluator retirement bound are unchanged.

## Evidence and remaining migration

The isolated stage passed 154 checks on local macOS arm64: 81 compiled SIGIL,
37 reference/build, 15 actual HTTP/native-service and all 21 existing HTTP/browser
cases retargeted to v7. Native formatting, Clippy, unit tests, release build and
all existing native/evaluator/older-host prerequisites remained mandatory. Both
source fingerprints matched before/after. Actual screenshots were inspected.
See the [acceptance record](mvp-acceptance.md) for exact revisions and limitations.

The expanded integrated `./ci.sh` subsequently passed on unchanged authored inputs
on 2026-09-09 UTC (session 37117, terminal `a9bc5c`, exit 0), including pin/rebuild,
generated compile checks, lint, native/evaluator/older-host prerequisites and the
whole test tree. Coverage passed at 91.19% line / 85.84% branch. The Linux-only
rename observer and declared research xfail do not become migrated-product passes;
one nonfatal unclosed-SQLite warning was reported. This is not warning-free,
protected Linux CI, immutable-package or pilot evidence.

The integrated tests use a [frozen v6 source fixture](../tests/fixtures/native-host-v6/README.md)
for actual older-host rejection and unchanged-v6-profile reopen checks, retaining
the older v4 fixture too. Neither result qualifies an in-flight v6-to-v7 upgrade:
the application bundle changes and needs explicit migration/recovery evidence.

All eleven legacy routes remain unmigrated. Request-ID parity in JSON bodies and
audit/log records, authentication error distinctions, request-rate admission,
health/readiness, usage/audit/retention and route-specific behavior remain open.
Returning a successful health/version payload without those required policies
would not complete a route. No Linux candidate, real-provider usefulness result,
independent onboarding, control-plane reuse or external-pilot clearance is claimed.
