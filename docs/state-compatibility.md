# sigil-pi v1 state and migration compatibility

This is the state-compatibility contract for product releases. It covers the state directory,
offline backup format, session export format, upgrade preflight, and rollback boundary. A
release must not infer or silently rewrite an unknown schema.

## Versioned surfaces

| Surface | Current schema | Compatibility rule |
|---|---:|---|
| Clean-shutdown marker | 1 | Required for offline backup; any other schema is rejected |
| Offline backup manifest | 1 | Restore accepts exactly schema 1 and verifies every declared file |
| Quota/activity SQLite database | v1 table contract | Integrity, required tables, value bounds, active leases, and session attribution are validated before backup and after restore |
| Session export document | 1 | Additive fields may be introduced within schema 1; incompatible changes require a new export schema |
| Conversations, schedules, and signed audit chains | v1 structural contract | Malformed or unattributed state is rejected rather than guessed or dropped |

The clean-shutdown marker and backup manifest share the single `SCHEMA_VERSION` constant in
`state_tool.py`; product shutdown imports that constant rather than duplicating it. The API
export schema is intentionally independent because an exported customer document is not an
offline service-state archive.

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
most four hours, with production backups no more than 15 minutes apart. Repository tests prove
format mechanics and rejection behavior, while the digest-bound release drill must prove the
timings and data continuity on the exact artifacts and supported topology.

## Current automated evidence

Regression tests cover clean backup/restore round trips, SQLite WAL capture and integrity,
unknown/malformed schemas, corrupt conversations and schedules, invalid audit signatures,
unsafe archive paths, duplicate or undeclared members, checksum mismatch, active leases,
unattributed customer state, an existing restore target, and atomic staged installation.
Those tests do not substitute for the clean-host upgrade/rollback/recovery drill required for
general availability.
