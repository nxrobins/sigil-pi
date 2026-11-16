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
| Quota/activity SQLite database | v1 table contract | Integrity, required tables, value bounds, active leases, and session attribution are validated before backup and after restore; an online backup first drops turn leases, and the rows of sessions it did not capture, from its copy |
| Session export document | 1 | Additive fields may be introduced within schema 1; incompatible changes require a new export schema |
| Conversations, schedules, and signed audit chains | v1 structural contract | Malformed or unattributed state is rejected rather than guessed or dropped |
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
The one migration so far, state layout 1 to state layout 2, is described below.

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
timings and data continuity on the exact artifacts and supported topology.

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
restore. `scripts/release_drill.py` restores an online backup the candidate took with its own
tool while it served turns, and runs a turn on it; `tests/test_recovery_drill_online_backup.py`
covers it against stand-in releases and `tests/test_recovery_drill_real_bundle.py` against
real bundles of the tree.
Those tests do not substitute for the clean-host upgrade/rollback/recovery drill required for
general availability.
