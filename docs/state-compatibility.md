# sigil-pi v1 state and migration compatibility

This is the state-compatibility contract for product releases. It covers the state directory,
backup format, session export format, upgrade preflight, and rollback boundary. A
release must not infer or silently rewrite an unknown schema.

## Versioned surfaces

| Surface | Current schema | Compatibility rule |
|---|---:|---|
| Clean-shutdown marker | 1 | Required for an offline backup and not used by an online one; any other schema is rejected |
| Backup manifest | 1 | Restore accepts exactly schema 1 and verifies every declared file. An online backup writes schema 1 with two added fields, `mode` and `online`, and with `product_version` and `clean_shutdown_unix` null; restore ignores fields it does not know, so every release restores it |
| Quota/activity SQLite database | v1 table contract | Integrity, required tables, value bounds, active leases, and session attribution are validated before backup and after restore; an online backup first drops turn leases, and the rows of sessions it did not capture, from its copy |
| Session export document | 1 | Additive fields may be introduced within schema 1; incompatible changes require a new export schema |
| Conversations, schedules, and signed audit chains | v1 structural contract | Malformed or unattributed state is rejected rather than guessed or dropped |

The clean-shutdown marker and backup manifest share the single `SCHEMA_VERSION` constant in
`state_tool.py`; product shutdown imports that constant rather than duplicating it. The API
export schema is intentionally independent because an exported customer document is not an
service-state archive.

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
`tests/test_online_backup.py` covers online backups: every byte is read under the lock that
guards it, the ledger copy agrees with what was copied, and for any interleaving of turns,
deletions and new sessions with a backup the archive restores and boots with no critical
event. `scripts/release_drill.py` restores an online backup the candidate took with its own
tool while it served turns, and runs a turn on it; `tests/test_recovery_drill_online_backup.py`
covers it against stand-in releases and `tests/test_recovery_drill_real_bundle.py` against
real bundles of the tree.
Those tests do not substitute for the clean-host upgrade/rollback/recovery drill required for
general availability.
