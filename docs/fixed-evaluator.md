# Fixed grantless evaluator

Status: **DEVELOPMENT INTEGRATION — expanded qualification in progress; not a candidate**.
The default [automatic-service fixture](automatic-service.md) now selects this
executable for entry, admission, history and coordinator functions. Original MCP effect,
policy, recorder and transaction workers remain unchanged. This component addresses
measured repeated-compilation cost, not a change to SIGIL application policy or an
AIN backend. Exact completed and pending checks are in [MVP acceptance](mvp-acceptance.md).

## Motivation and boundary

After adding cancellation reads, the unchanged two-tenant automatic test exceeded
its 115-second publication wait in both a combined and isolated run. A passive
timing wrapper around that same test measured pending GETs at about 2.1 seconds.
The initial model arrived around 46 seconds, the committed tool phase around 69
seconds, and the next model intent around 115 seconds. The provider was local and
deterministic. These are diagnostic timings, not a qualified workload or latency claim.

The pinned MCP forge compiles and verifies the source on every invocation. The
service reuses its grantless process and immutable Wasmtime module cache, but that
does not eliminate SIGIL compilation. Status requests now require three entry
evaluations, while the same serialized owner advances bounded coordinator steps.
The additional work reveals an operating-envelope gap; widening a test deadline
would not satisfy the product's eventual load gate.

`native/evaluator` is a separate executable with one admitted source per supervised
process. The existing parent bridge still binds the executable/source hashes,
clears the child environment, prohibits effect grants, enforces input/fuel/deadline
limits, and reaps/replaces the process after 4,096 evaluations. It is selected only
by the current local development configuration, not a published package or pilot.

## Compile once, execute current policy every time

- The first source is compiled with `CompileLimits::default()`, the explicit
  `ephemeral` host profile and mandatory solver features. The evaluator requires
  the successful compilation's own `solver_verified` verdict. No environment
  override or caller-supplied verification claim is accepted.
- The in-memory artifact retains exact source bytes, their digest and freshly
  compiled Wasm. Later calls must supply identical source bytes. Only immutable
  code is reused: there is no result, authority, input or guest-state cache.
- Each invocation executes with empty `IoGrants` through the pinned runtime's
  fresh Wasmtime store/instance, fresh memory and fuel. Current credential, time,
  reservation, cancellation and storage facts remain inputs to SIGIL on every call.
- There is no serialization/deserialization of precompiled native code and no
  acceptance of external Wasm. The artifact, engine/host implementation and target
  live in the same pinned executable/process. Signed provenance and packaging
  remain separate M0/M7 requirements; a local digest is not that qualification.
- Source is capped at 65,536 bytes, input/output at 4 MiB, framed transport at
  16 MiB, fuel at one billion and process life at 4,096 evaluations. The runtime's
  existing 16 MiB guest memory ceiling and Wasmtime fuel backstop remain intact.
  The parent must still enforce wall-clock supervision; this is not a standalone
  safe public service or an unsupervised compiler daemon.

The small stdio protocol accepts only the existing parent's initialization and
grantless `sigil_forge` calls, with strict fields, duplicate-key rejection and
ordered request IDs. Unknown fields, grants, host profiles, source changes and
invalid compilation terminate the executable with static error categories, not
source/input/compiler-detail logs. Guest execution errors remain observations of
that evaluation; they cannot authorize a native command by themselves.

## Pins and adoption criteria

Compiler/runtime dependencies use the same full public SIGIL revision as `SIGIL_REV`:
`8277a1d92d599df89e6b4391fc70fd0fa534d696`. Rust is pinned to 1.98.0. The lock was
seeded from that SIGIL checkout to avoid unrelated transitive upgrades. The only
registry name/version/checksum difference is `serde_json` 1.0.150, already required
by this repository's native bridge; the upstream lock used 1.0.149. The local
evaluator and bridge are the other added packages. The initial broad dependency
resolution was discarded before any test or adoption claim.

Qualification of this development integration requires:

1. Native compile/verification, protocol, source-binding, fresh-memory/input/fuel,
   grant denial, bounded output and lifecycle tests; formatting and warnings-denied
   static checks. The parent timeout/reaping/no-resend boundaries must still hold.
2. Actual service runs using only a fixed bootstrap executable/hash substitution,
   with unchanged SIGIL sources, effect lanes, privileges, deadlines and expected
   results. In particular, the original two-tenant test must complete and retain
   its isolation/canary checks; cancellation/restart scenarios must not regress.
3. Explicit bundle/backend identity and documented compatibility behavior. Old
   accepted work cannot silently inherit a changed deployment bundle.
4. Integrate the fixture/build gates and run the complete source gate. Faster
   focused tests alone are not M5 throughput, process/memory lifetime, supported
   Linux evidence, independent artifact review or pilot clearance.

Effect workers remain fresh one-use processes using their current separately scoped
runtime. This prototype must not acquire I/O or application decisions to make a
scenario pass. Both interfaces and the actual control-plane consumer remain required.

The mandatory fixture now runs formatting, warnings-denied release Clippy, all
native unit/integration tests (including the actual 4,096-call parent/child recycle),
and a locked optimized executable build. The new build/test command ceiling is
15 minutes to accommodate compiling the pinned runtime plus that full lifecycle
test; it does not alter any existing service, effect, operation or test deadline.
The fixture requires solver headers and uses the same explicit Z3 discovery
convention as the existing source gate. Toolchain-required runs cannot silently skip it.
