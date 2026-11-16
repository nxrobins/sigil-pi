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

The clean-shutdown marker and backup manifest share the single `SCHEMA_VERSION` constant in
`state_tool.py`; product shutdown imports that constant rather than duplicating it. The API
export schema is intentionally independent because an exported customer document is not an
service-state archive.

## Backup authentication

`state_tool.py` authenticates backups but does not encrypt them: confidentiality at rest
belongs to the storage, an encrypted volume or bucket. The manifest authentication tag
(audit F-12, decision D5 (a)) is the one format change since the first backup, made as a
versioned format change rather than a migration:

- Versioned: the tag's scheme, `v1`, is written in the tag, and its key's label names it too.
  A later scheme gets a new name; this release refuses one it does not know rather than read
  it as untagged.
- No implicit migration: an archive written before the tag is never rewritten or tagged in
  place. Restore refuses it with a stable message that names `--skip-manifest-authentication`,
  the one way to restore it, and the managed cycle refuses to expire it; the operator moves an
  older release's managed archives out of the managed directory (`docs/operations.md`).
- Dry run and restart: restore never overwrites, so restoring into a new path is the dry run
  of any restore, and a restore that stops installs nothing and can be run again.
- The state directory's format does not change, so the state a restore installs is the same
  for every release.
- Rollback: releases before manifest authentication (v0.1.0 to v0.4.0) read the header as one
  they do not know and ignore it, so they restore a tagged backup, and their managed cycles
  expire tagged archives, without authenticating them. To keep the check across a rollback,
  restore the backup with this release into a new path, which authenticates it, and point the
  older release at that path. `tests/test_backup_authentication.py` restores a tagged backup
  with each of those releases' own `state_tool.py` where the release tags are present.

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
event. `scripts/release_drill.py` restores an online backup the candidate took with its own
tool while it served turns, and runs a turn on it; `tests/test_recovery_drill_online_backup.py`
covers it against stand-in releases and `tests/test_recovery_drill_real_bundle.py` against
real bundles of the tree.
Those tests do not substitute for the clean-host upgrade/rollback/recovery drill required for
general availability.
