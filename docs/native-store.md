# Native opaque-record storage mechanism

Status: **IMPLEMENTED MECHANISM / DRAFT INTEGRATION**, updated 2026-09-08.
Contract: `sigil-store/v1`. [Source](../native/store/src/store.rs),
[scoped stdio adapter](../native/store/src/main.rs),
[locked dependencies](../native/store/Cargo.lock).

This repo-local Rust component implements the atomic storage primitive needed by
[the shared execution contract](execution-contract.md). It does not replace the
SIGIL runtime, modify `SIGIL_REV`, migrate old state, or implement a new product API.
Its use by the owning runtime/control-plane work still requires coordination and
conformance review. The application/executor integration is not frozen by its existence.

## Ownership and boundary

Native storage owns opaque record bytes, namespace permissions, revision checks,
atomic batches, durable acknowledgement and independent storage ceilings. It has
no conversation, model/tool selection, scheduling, tenant allowance, retry or
delivery-phase decision code. The [SIGIL turn application](turn-reducer.md) owns
conversation decisions; the [shared SIGIL kernel](../app/shared/delivery_state.sigil)
owns delivery-phase transitions. The actual authenticated SIGIL transaction/dispatch
producer must still replace the integration fixture's directly supplied bindings.

All records live in one private local database so a batch can atomically update
application state, intent publication and reservation/deduplication records. This
does not grant an executor access to every record. An in-process `Scope` is minted
only by trusted bootstrap, is not deserializable from a guest request, and is bound
to that store's fresh boot identity. Restart requires newly admitted handles.

Each scope contains exact namespace permissions:

| Permission | Mechanism |
|---|---|
| `read` | Read records and use their revisions as transaction preconditions |
| `read_write` | Read, create, replace or tombstone a record, always with an expected revision |
| `create_only` | Create a non-null value only at revision zero; cannot read, replace or delete it |

The native-only `unused_create_slot` method checks actual create authority and
checks integrity of the retained coordinate, returning only whether it has never been used.
It exposes no value or revision, performs no reservation/write, and does not grant
general read access to a create-only scope. Values and tombstones both return false;
foreign scopes and corruption fail closed. The caller must retain exclusive store
ownership through the related action. The completion path uses this before any effect
to refuse inconsistent retained deliveries; no new stdio command or guest import exists.
Record digests are unkeyed SHA-256 checksums for corruption detection, not signatures
against someone who can rewrite both the database and its checksums. The storage
administrator remains inside the trusted boundary.

Application and executor roles can therefore have disjoint access: application
state read/write plus intent creation and delivery reads; executor intent reads,
dispatch bookkeeping and delivery creation, with no application-domain access.
Tenant namespaces are host-bound identifiers, not values accepted from a browser
or model. The native database process is trusted and can physically access the
database; guest isolation and correct authenticated handle issuance remain required.

The stdio executable has one bootstrap scope per process. Requests cannot select
another scope, add grants, submit SQL or change configuration. Its launch arguments
and bootstrap file are trusted inputs, not an authentication protocol. Do not expose
this executable directly as a browser/API service or let an untrusted client launch
it with its own bootstrap grants. Production artifact/authority admission is unfinished.

## Atomicity and acknowledgement

The store uses one SQLite connection with a process-held exclusive owner lock.
Second owners are refused, including aliases of the same directory. This excludes
concurrent brokers; it does not fence a separately running effect worker. The final
dispatcher must bind worker ownership and stop/reap it before replacement dispatch.

Store destruction closes the database before explicitly unlocking in the owning
process. This avoids a transient inherited descriptor keeping a gracefully closed
store locked. Handles check their owning process; an inherited non-owner lock copy
must not unlock the parent. An embedding host must still use fresh connections
after exec and never use inherited SQLite connections after fork. Local process
identity is a handle-lifecycle check, not a semantic operation identity.

A batch contains read-only `checks` and `writes`. Every referenced namespace must
be authorized, every key occurs at most once across the batch, and every expected
revision must match. All checks and writes run in one `BEGIN IMMEDIATE` transaction.
A successful acknowledgement follows the database commit. Conflict, malformed
input, denied permission or a logical-cap refusal publishes no part of the batch.

Existing records start at revision one. Never-created records read as revision zero
and null value. An explicit null write leaves a versioned tombstone, so deleting a
record does not make an old revision-zero create valid again. Omitting `value` is
invalid; it is not shorthand for deletion. Applications must supply their own
retention/deduplication policy; these tombstones have no automatic expiry.

Lost acknowledgement means the caller must reopen/read and reconcile against its
expected revision and immutable intent. It must not infer that a commit failed or
send an effect again. `commit_uncertain` means acknowledgement could not be established.
Storage mutation errors make the handle require reopening, even when the underlying
transaction may have rolled back. No effect dispatch is performed by this component.

The implementation selects and verifies rollback-journal `DELETE` mode with
`synchronous=EXTRA`, enables full synchronization, and synchronizes the directory
after initialization. SQLite documents the extra directory synchronization needed
for this rollback-journal durability model. The guarantee depends on the filesystem
and device honoring synchronization; our tests kill processes, not physical power.
[SQLite synchronization contract](https://sqlite.org/pragma.html#pragma_synchronous).

The new component pins Rust 1.98.0, rusqlite 0.40.2 and its lockfile's bundled
SQLite 3.53.2. Startup rejects SQLite older than 3.51.3. This is independent of the
SIGIL compiler's existing dependency tree, which was not changed. The older cached
binding was not adopted: SQLite documents a corruption defect in older WAL versions,
although this single-connection component does not enable WAL.
[SQLite WAL-reset guidance](https://sqlite.org/wal.html#walreset).
Final candidate dependency scans and independent review remain required.

## Files, integrity and limits

Initialization is explicit and refuses an existing database. Opening missing,
corrupt, foreign or unsupported-version state never creates a fresh application.
The root must already exist, be owned by the current user and deny group/other
access. Database/lock files must be private regular files without hard-link aliases;
symlinks are refused. The live handle checks directory, lock and database identities.
The operator must also keep ancestors and the state root outside every guest's
filesystem authority; these checks are not a defense against a malicious host owner.

Opening state checks the schema, SQLite integrity, frozen limits and all record
digests. Each digest binds namespace, key, revision, null/non-null distinction and
value bytes. It detects accidental corruption/relocation but is **not authentication**:
an actor with direct database write access can recompute it. No provider secret or
authority credential is supplied to the store by the integration fixture.

Default independent ceilings (not tenant quotas or an approved pilot profile):

- 2 MiB per UTF-8 value; 8 MiB of value bytes and 64 total entries per transaction.
- 128 MiB of accounted record data and 100,000 records, including tombstones.
- 262,144 database pages of 4,096 bytes (1 GiB); bounded database cache, no mmap.
- 16 MiB per newline-terminated JSON transport frame; 64 KiB bootstrap input.

Namespace/key identifiers have bounded ASCII syntax and are database keys, not
filesystem paths. Numeric revisions are integers from zero through signed-64-bit
maximum; overflow fails closed. Bootstrap limits are retained and must match on
restart. Changing them or the schema requires an explicit migration decision.
Encoded-frame overhead can impose a stricter limit than decoded-value ceilings.

The protocol supports only `get` and atomic `commit`. There is no live backup,
export/delete-retention workflow, compaction, expiry/revocation, credential service,
artifact admission, worker lifecycle or network interface. Do not copy a live
database as a claimed backup. Restoring old state also requires reconciliation
against effects newer than the backup before any dispatcher is allowed to resume.

### Exact stdio shapes

The executable is launched by a trusted host with `init` or `open`, an existing
private root directory and a bootstrap JSON file. Bootstrap has exactly `version`
(integer 1), `limits` (the six fields above) and `grants` (an array of distinct
`{namespace, access}` objects). Invalid/duplicate grants are rejected before database
initialization. The first stdout line is a `ready` record naming `sigil-store/v1`
and the bundled SQLite version. This is local mechanism readiness, not product health.

Requests and responses are one UTF-8 JSON object per LF-terminated frame:

```json
{"op":"get","namespace":"application","key":"conversation"}
```

Successful reads return `{"status":"ok","record":{"revision":1,"value":"opaque bytes"}}`.
Atomic writes use the following shape; all `checks` and `writes` are mandatory arrays
and a write's `value` is mandatory, either a string or explicit null:

```json
{"op":"commit","checks":[{"namespace":"delivery","key":"op:1","revision":1}],"writes":[{"namespace":"application","key":"conversation","revision":1,"value":"next state"},{"namespace":"intent","key":"op:2","revision":0,"value":"next effect"}]}
```

A successful commit returns `{"status":"ok","receipt":{"revision":2}}`. The receipt
is the store-local batch revision, not an operation ID, authority proof, tenant API
response or semantic ordering requirement for independent work. Errors contain only
`status:error` and one of the native `Error` codes; no input/state bytes are echoed.
Unknown and duplicate fields, wrong types, changed scope fields and malformed/trailing
JSON are refused. Oversize or unterminated frames terminate the adapter. A broken
response stream after a commit has an unknown acknowledgement outcome; reconcile
records on reopening rather than treating transport failure as non-commit.

## Verification and next integration

### Bounded key discovery

The native library and trusted stdio adapter now expose a read-only discovery
primitive used for restart and the automatic service loop:

```json
{"op":"keys","namespace":"application","after":null,"limit":128}
```

It returns `status:ok` and `page:{keys:[...],next:...}`. The namespace must have
actual `read` or `read_write` authority; create-only scope cannot reveal names.
`limit` is an integer from 1 to 128. `after` is optional/null for the start or a
valid native key of at most 256 ASCII bytes; an empty string is invalid. Keys use
binary lexical ordering and the cursor is exclusive. A full page returns its last
key as `next`; a full final page therefore needs one more empty page to discover
exhaustion. A shorter/empty page returns null.

Tombstones remain discoverable. Normal bounded record-integrity checks run before
exposing each key; values and revisions are not returned. Oversized/corrupt key data
is rejected before unbounded allocation. The page does not mutate metadata or mint
a claim, receipt, snapshot or capability. Consumers must read actual records and
use the existing revision/claim protocol before acting on them. Native code does
not inspect pi phases or choose runnable work.

Multiple pages are not a frozen snapshot. A new key inserted before the cursor
will be found by a later rescan, not necessarily the current traversal. SIGIL owns
rescan/eligibility policy; restart can discard volatile cursors and inspect retained
records again without treating a retained claim as fresh work. One page is bounded,
but its record validation can read up to the page size times the configured value
cap; no measured service latency or full operating-envelope claim follows from this.

This additive stdio/library operation changes no on-disk schema, existing get/commit
semantics, bootstrap scope issuance or runtime pin. Its presence must be bound to
the exact admitted implementation; the existing `sigil-store/v1` label alone does
not prove an older binary supports it. The v4 automatic coordinator now uses it
through the native command interpreter. It is not exposed through the public HTTP
action interpreter or a conversation-discovery route. The next discovery design
is recorded separately in [session-discovery-plan.md](session-discovery-plan.md).

### Existing and new conformance evidence

[Native tests](../native/store/src/store_tests.rs) exercise atomicity, read/write/create
permissions, identical keys in separate namespaces, stale revisions, tombstones,
boot-bound handles, duplicate owners, unsafe paths, corruption and resource refusal.
Two tests kill a real child at the transaction boundary: before commit, and after
commit before reply. The full-database test forces SQLite's page ceiling; it is not
an actual full-volume `ENOSPC` drill. The child entrypoint is test scaffolding.

[Protocol tests](../tests/test_native_store.py) exercise the actual Rust executable,
including ambiguous/duplicate fields, unauthorized scope changes, explicit null,
startup refusal and restart after commit. The required whole-tree source gate now
builds from the lockfile and runs formatting, Clippy and native unit tests before
these tests; a missing native toolchain cannot silently qualify the required gate.
The [discovery process tests](../tests/test_native_discovery.py) additionally cover
pagination, tombstones, restart, disjoint namespaces, create-only refusal and malformed
requests. Native tests cover maximum pages/keys, corrupted records, stale/foreign
scopes, poisoned handles and rescan after insertion before a cursor.

[Durable turn tests](../tests/test_durable_turn.py) run the real SIGIL reducers,
isolated model/file effects and native transactions with separate application and
executor scopes. They restart storage after intent publication, delivery recording
and application interpretation. Recovery before a claim remains unclaimed; recovery
after a claim remains possibly delivered—even if the fixture knows no request was
sent. A separate case observes an actual request before killing its worker and
retains uncertainty without replay.

The later [SIGIL application](turn-transactions.md) and [shared executor](executor-transactions.md)
now emit every commit forwarded unchanged by these tests, including intent/delivery
read preconditions and atomic dispatch/delivery writes.
These are stronger integration facts than an in-memory proposal snapshot, but they
do not satisfy M2 on their own. The [native worker bridge](native-worker.md) now
supplies one-use physical execution and its generation/observation facts, but the
older fixture still selects the registry, binds actors/attempt keys and forwards
snapshots. The newer [claim](claimed-worker.md), [policy](dispatch-policy.md) and
[completion](worker-completion.md) paths own actual reads, receipt ordering,
observations and result commits, including abandoned-claim recovery. Registry selection
and application interpretation are still fixture-driven. Next, connect these bindings on
the actual service path, including per-operation authority, budgets, cancellation
and every crash point. All M0–M8 gates remain unqualified.
