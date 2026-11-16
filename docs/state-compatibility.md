# sigil-pi v1 state and migration compatibility

This is the state-compatibility contract for product releases. It covers the state directory,
backup format, session export format, upgrade preflight, and rollback boundary. A
release must not infer or silently rewrite an unknown schema.

## Versioned surfaces

| Surface | Current schema | Compatibility rule |
|---|---:|---|
| Clean-shutdown marker | 1 | Required for an offline backup and not used by an online one; any other schema is rejected |
| Backup manifest | 1 | Restore accepts exactly schema 1 and verifies every declared file. An online backup writes schema 1 with two added fields, `mode` and `online`, and with `product_version` and `clean_shutdown_unix` null; restore ignores fields it does not know, so every release restores it |
| Backup manifest authentication tag | `v1` | The `SIGILPI.manifest_tag` pax header of the `manifest.json` member: `v1:` and the hex HMAC-SHA256 of the manifest's exact bytes, under HMAC-SHA256(audit key, `sigil-pi backup manifest v1`). Restore and the managed cycle refuse a tag that does not match and a scheme other than `v1`; restore refuses an archive without a tag unless given `--skip-manifest-authentication`, and the managed cycle always refuses one. Releases before manifest authentication ignore the header |
| Quota/activity SQLite database | v1 table contract | Integrity, required tables, value bounds, active leases, and session attribution are validated before backup and after restore; an online backup first drops turn leases, and the rows of sessions it did not capture, from its copy. Since schedule store schema 2 it also holds the schedules, captured with the registry in one snapshot |
| Session export document | 1 | Additive fields may be introduced within schema 1; incompatible changes require a new export schema |
| Conversations and signed audit chains | v1 structural contract | Malformed or unattributed state is rejected rather than guessed or dropped |
| Schedule store | 2 | Each schedule is a row of `schedules`, and its message a row of `schedule_messages`, in `product-quotas.sqlite3`; `product-schedules.json` holds only the schema-2 marker, which is not a JSON object, so a release before schema 2 refuses the state instead of serving it with no schedules. Schema 1, the schedules as one JSON document at `product-schedules.json`, is refused by the service, `backup` and `backup-cycle` before anything changes, naming `state_tool.py migrate`; `restore` restores it as it was and says that it needs migrating. Every row is validated before backup and after restore, as malformed or unattributed state is. See [Schedule store schema 2](#schedule-store-schema-2) |
| State layout (sandbox directory names) | 2 | A session's sandbox is `sandboxes/<64 hex>`, the full SHA-256 of its internal id, the name its conversation and audit chain carry. Layout 1, before it, used the first 16 hex characters. The service, `backup` and `backup-cycle` refuse layout-1 state, or a migration that has not finished, before changing anything, naming `state_tool.py migrate`; `restore` restores a layout-1 archive as it was and says that it needs migrating. See [State layout 2](#state-layout-2-sandbox-names) |

The clean-shutdown marker and backup manifest share the single `SCHEMA_VERSION` constant in
`state_tool.py`; product shutdown imports that constant rather than duplicating it. The API
export schema is intentionally independent because an exported customer document is not an
service-state archive.

## Backup authentication

`state_tool.py` authenticates backups but does not encrypt them: confidentiality at rest
belongs to the storage, an encrypted volume or bucket. The manifest authentication tag
(audit F-12, decision D5 (a)) is the one change to the backup format since the first backup,
made as a versioned format change rather than a migration:

- Versioned: the tag's scheme, `v1`, is written in the tag, and its key's label names it too.
  A later scheme gets a new name; this release refuses one it does not know rather than read
  it as untagged.
- No implicit migration: an archive written before the tag is never rewritten or tagged in
  place. Restore refuses it with a stable message that names `--skip-manifest-authentication`,
  the one way to restore it, and the managed cycle refuses to expire it; the operator moves an
  older release's managed archives out of the managed directory (`docs/operations.md`).
- Dry run and restart: restore never overwrites, so restoring into a new path is the dry run
  of any restore, and a restore that stops installs nothing and can be run again.
- The tag does not change the state directory's format: a restore installs the same state
  with or without it. State layout 2 (below) is a separate change, with its own migration.
- Rollback: releases before manifest authentication (v0.1.0 to v0.4.0) read the header as one
  they do not know and ignore it, so they restore a tagged backup, and their managed cycles
  expire tagged archives, without authenticating them. They refuse migrated state whatever
  its tag, so the tagged backup a rollback gives them is the migration's pre-migration backup,
  of state layout 1 ("State layout 2: sandbox names"). To keep the check across a rollback,
  restore that backup with this release into a new path, which authenticates it, and point the
  older release at that path. `tests/test_backup_authentication.py` restores a tagged
  pre-migration backup with each of those releases' own `state_tool.py` where the release tags
  are present.

## Supported starting point

Version 1.0 establishes the first supported product-state baseline. Research endpoint state,
state without the durable tenant/session quota registry, memory-sidecar state, an unknown
backup schema, and state from an undocumented topology are not supported in-place upgrade
inputs. Product startup or restore fails with an actionable migration/schema error instead of
opening such state.

There is no implicit research-to-v1 migration. A future migration must be a versioned,
standalone, restartable command with input/output schema validation, a dry-run mode, an
immutable pre-migration backup, and tests for success, interruption, repeated execution,
corrupt input, and rollback. Its release notes must name the exact source and target versions.
The migrations so far are two versioned steps of one command, `state_tool.py migrate`: state
layout 1 to state layout 2, and the schedule store from schema 1 to schema 2, each described
below. A new format adds a step to that command, never a second command.

## State layout 2: sandbox names

State layout 1 named a session's sandbox directory by the first 16 hex characters (64 bits)
of the session's digest. A tenant who knew another tenant's session could search about 2^64
session names of its own for one whose digest began the same way, and its tools would then
share the other tenant's directory (audit 2026-11-10, F-23). Layout 2 names the directory by
the whole digest, as the conversation and the audit chain are named. A release with layout 2
never opens layout-1 state, and never migrates it implicitly: the service refuses it at
startup before it removes the clean-shutdown marker or changes anything else, and `backup`
and `backup-cycle` refuse it, each naming the command below. `restore` restores a layout-1
archive as it was, so that the pre-upgrade backup restores into a new path, and prints a note
on stderr that the state needs migrating.

`state_tool.py migrate` is the only command that changes the layout. Like `backup`, it reads
the audit key from `PI_AUDIT_KEY`:

```sh
python3 state_tool.py migrate --state /var/lib/sigil-pi-restored --dry-run
python3 state_tool.py migrate --state /var/lib/sigil-pi-restored \
  --backup /secure-backups/pre-migration-state.tar.gz
```

- It runs only on a drained state, one that carries the clean-shutdown marker (`restore`
  writes one), and one run at a time. It validates its input with every check a backup
  makes, accepting layout 1, and its output as layout 2.
- `--dry-run` lists every move, `sandboxes/<16 hex>` to `sandboxes/<64 hex>`, and every
  problem, and changes nothing. It exits 2 when there is a problem.
- Before its first rename it writes an immutable backup of the state as it is to `--backup`:
  a new path, read-only, never overwritten. It then writes a journal,
  `.state-migration.json`, naming that backup and its SHA-256, and moves each directory with
  one atomic rename. Once the output validates, it removes the journal.
- It is restartable. A run stopped at any point is finished by running the same command
  again. With the journal present it resumes with the backup the journal names, which must
  be intact. Until it finishes, the service and `backup` refuse the state as a migration that
  has not finished. A run stopped after its backup and before its journal changed nothing
  else; run it again with a new `--backup` path, because a backup is never overwritten.
- It never merges or overwrites a directory. A layout-1 directory whose 16 characters begin
  the digests of two registered sessions, the collision F-23 describes, holds files no tool
  can attribute to one of them; a directory whose full name already exists would be merged
  into it. Either is reported, and the run stops before it changes anything; see
  `docs/runbooks.md#state-migration`.
- Run on a state already at layout 2, it reports no moves and writes nothing.

Research endpoint state is not migrated. The research host refuses to start on a state that
holds a layout-1 sandbox, which it would otherwise pass over: rename each such directory to
the full SHA-256 of its session id, or move it out of the state directory, first.

The first release with layout 2 declares `state migration required`, from state layout 1 to
state layout 2. To upgrade, restore the pre-upgrade backup into a new path with the
candidate's `state_tool.py`, run the candidate's `migrate` on that path (the dry run first),
then validate and start the candidate there. The rollback path: the old release never opens
migrated state, because its startup refuses a full-width sandbox directory as unattributed
customer state. Roll back by restoring the pre-upgrade backup, or the migration's
pre-migration backup, into a new path with the old release's `state_tool.py`, and pointing
the old release at that path.

## Schedule store schema 2

Schedule store schema 1 kept every tenant's schedules in one JSON document,
`product-schedules.json`. Every claim, completion and release of a scheduled run read and
rewrote the whole document under one lock, and the scheduler read it once a second, so one
tenant's claim cost time in proportion to every other tenant's schedules: about half a second
with twenty tenants at their allowance (audit 2026-11-10, F-27). Schema 2 keeps each schedule
as rows of the quota ledger, `product-quotas.sqlite3`: its state in `schedules`, its message
in `schedule_messages`. A claim, a completion and a release read and change their own
schedule's row and no other; the scheduler's look reads no message; a turn reads its own.
Every change is one SQLite transaction, synced before it returns. A message that is deleted,
with its schedule, its session or by retention, or replaced, is overwritten in the ledger
(`secure_delete`), and the ledger's log, which still held it, is emptied after the change. An
online backup captures the schedules with the registry in one snapshot. `product-schedules.json`
holds only a marker, a JSON array that names schema 2: a release before schema 2 reads it as
a corrupt store, so its readiness fails and its `state_tool.py` refuses the state, rather than
finding no schedules. The service writes the marker when it first opens a state, before it
holds any schedule.

The service never reads or converts a schema-1 document: it refuses to start on one, before it
removes the clean-shutdown marker or changes anything else, and `backup` and `backup-cycle`
refuse it, each naming `state_tool.py migrate`. `restore` restores a schema-1 archive as it was,
so that the pre-upgrade backup restores into a new path, and prints a note on stderr that the
state needs migrating.

The schedule store's step is the second step of the same command, with the same `--dry-run`,
`--backup`, clean-shutdown marker, single run and journal as the sandbox step above. A state
at layout 1 runs both steps, sandboxes first, under one pre-migration backup and one journal,
which names the steps its run takes. The report names them in `steps`; the sandbox step's part
is `migration`, `from_layout`, `to_layout` and `moves`, and the schedule store's is
`schedule_store`, with `from_schema`, `to_schema` and the number of `schedules` it converts.

- Its input check refuses, before anything changes, a document that is not a JSON object, an
  entry that no release wrote (a key that is not its tenant's digest and its name, a tenant,
  name or session no release accepts, an internal session that is not its tenant's session, an
  interval that is not a positive integer, an interval or a time past a signed 64-bit integer
  (what a row holds; earlier releases stored any interval sent, and such a schedule never
  ran), a message that is not valid UTF-8 text, an unknown
  or missing field, a lease without its end), and schedule rows already in the ledger that are
  not exactly the document's. It names the entry and what is wrong with it, and never merges
  schedules; see `docs/runbooks.md#state-migration`.
- An entry from a release before retention has no times, and schema 1 read its document's
  mtime as both, so that the first sweep after an upgrade kept it. The step writes that
  boundary into the row.
- Its one change to the ledger is one transaction, the tables and every row. Then the marker
  replaces the document. A run stopped before that commit has changed nothing in the ledger;
  one stopped after it finds the rows equal to the document, and writes the marker; one
  stopped after the marker finishes with the output's validation. Run on a state already at
  layout 2 with a schema-2 store, the command reports no steps and writes nothing.

The first release with schedule store schema 2 declares `state migration required`, from
schedule store schema 1 to schedule store schema 2, with state layout 1 to state layout 2 when
it comes from a release before both. The upgrade is the one above: restore the pre-upgrade
backup into a new path with the candidate's `state_tool.py`, run the candidate's `migrate` on
that path (the dry run first), then validate and start the candidate there. The rollback path:
the old release never opens a state with a schema-2 store, because it reads the marker as a
corrupt store. Roll back by restoring the pre-upgrade backup, or the migration's pre-migration
backup, which holds the schema-1 document, into a new path with the old release's
`state_tool.py`, and pointing the old release at that path. Schedule changes made since the
upgrade are not in it.

## Upgrade and rollback rule

Before upgrading, an operator must cleanly drain the old release and create a verified backup
with the old release's `state_tool.py`. Restore that backup into a new path with the candidate
release and validate/start the candidate against the restored copy before switching traffic.
While the old release predates manifest authentication its backup carries no tag: record its
SHA-256 when it is written, check it before the restore, and restore it with
`--skip-manifest-authentication`.
Never test a candidate by mutating the only copy of production state.

Every release must declare one of these states in its release notes:

- `state schema unchanged`: the new release reads and writes the same schema; rollback may
  reuse the cleanly drained state after the rollback drill proves the old release can open it.
- `state migration required`: the old release must never open migrated state; rollback restores
  the immutable pre-upgrade backup into a new path and points the old release at that path.

An in-place schema change without an explicit migration and rollback path is a release blocker.
The supported rollback objective is at most 15 minutes; the restore/recovery objective is at
most four hours, with production backups, taken online, no more than 15 minutes apart,
their age counted from the manifest's `created_unix` (for an online backup, the moment it
began). Repository tests prove
format mechanics and rejection behavior, while the digest-bound release drill must prove the
timings and data continuity on the exact artifacts and supported topology. The drill follows
this rule: the old release's tool backs its drained state up, the candidate's tool restores
that backup into a new path (with `--skip-manifest-authentication` only for an archive without
a tag, whose SHA-256 it recorded when the old release wrote it) and migrates it there, the dry
run first, and the candidate starts on it. It drills a release as `state migration required`
when the migration moved the state or the two releases' `migrate --dry-run` name different
state formats (a release without the command predates them all), and then rolls back by
restoring the pre-migration backup, or the pre-upgrade backup, into a new path with the old
release's tool; otherwise it rolls back onto the state the candidate left
(`docs/operations.md`, "Clean-install, upgrade, and rollback drill").

## Current automated evidence

Regression tests cover clean backup/restore round trips, SQLite WAL capture and integrity,
unknown/malformed schemas, corrupt conversations and schedules, invalid audit signatures,
unsafe archive paths, duplicate or undeclared members, checksum mismatch, active leases,
unattributed customer state, an existing restore target, and atomic staged installation. A
property test pins the backup archive, byte for byte, to the one the writer produced before
it streamed, and a failed sync is pinned to stop a managed cycle before it deletes.
`tests/test_backup_authentication.py` covers the manifest's authentication: a change to any
manifest field, any archived file's digest or the tag, and a different audit key, are each
refused before restore writes anything; the managed cycle deletes nothing beside an archive
that does not authenticate or has no tag; the flag restores an untagged backup and says so,
and never skips a tag that is there.
`tests/test_online_backup.py` covers online backups: every byte is read under the lock that
guards it, the ledger copy agrees with what was copied, and for any interleaving of turns,
deletions and new sessions with a backup the archive restores and boots with no critical
event. `tests/test_sandbox_width.py` covers state layout 2: every surface of a session,
the sandbox included, is named by its full digest; export, deletion, retention, the storage
quota and both kinds of backup find a sandbox there; a session whose digest shares 64 bits
with another's sees none of its files, on the real compiler too; and the migration's dry
run, success, a kill at each of its steps followed by a second run, repeated execution,
corrupt input, a shared or occupied directory reported and never merged, and the rollback
restore. `tests/test_schedule_store_rows.py` covers schedule store schema 2: a claim, a
completion, a release or a recorded run writes and reads only its own schedule's row whatever
other tenants hold, and the scheduler's look reads no message; every operation answers as
schema 1 did, a property over any sequence of them; the service, `backup` and `backup-cycle`
refuse a schema-1 store and the old release refuses a schema-2 one; offline and online backups
round-trip the rows and refuse rows no release wrote; the migration step's dry run, success,
both steps from layout 1, a kill at each of its points followed by a second run, repeated
execution, corrupt input, rows it never merges, legacy timestamps, and the rollback restore
with the old release's own code; and, for P2's guards, every commit synced in the log, a
deleted or replaced message in no file, a commit a crash tears, and a full disk.
`scripts/release_drill.py` restores an online backup the candidate took with its own
tool while it served turns, and runs a turn on it; `tests/test_recovery_drill_online_backup.py`
covers it against stand-in releases and `tests/test_recovery_drill_real_bundle.py` against
real bundles of the tree. `tests/test_recovery_drill_state_migration.py` covers the drill's
upgrade and rollback across a format change: against real bundles of `c41f3e5`, the tree
before manifest authentication and state layout 2 (where that commit and the toolchain are
present), the candidate restores the old release's untagged backups with the flag, migrates a
layout-1 sandbox, and the old release rolls back onto the pre-migration or the pre-upgrade
backup; against stand-in releases whose state tools write no tag or have no `migrate`, the
same decisions; and the flag is never given for a tagged archive.
Those tests do not substitute for the clean-host upgrade/rollback/recovery drill required for
general availability.
