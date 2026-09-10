# Frozen v6 host — compatibility fixture only

These 36 files are a byte-identical snapshot of the native store, worker and
application host from the full local source gate that passed on 2026-09-09 UTC
(session 48313, terminal `d7d29b`, observed 00:07:32). They were copied and compared
before the main host gained v7 HTTP metadata support. They are test inputs, not
another maintained product runtime or deployment package.

The snapshot includes v5/v6 discovery, fixed grantless evaluation and public assets.
It lets tests prove that an actually older executable rejects v7 before opening
state, and that the new host can reopen retained v6 state with an unchanged v6
application bundle. This does NOT qualify upgrading in-flight operations from v6
to v7, whose application/host bundle identity changes.

`manifest.json` inventories the original sources, Cargo lockfiles and Rust pins.
The sorted relative-name SHA-256 aggregate is:

`afb5134fe14255f55c80df446697de2e3e5d664ec6c7c0170a8e930f50ff3e2f`

`tests/legacy_http_native_support.py` validates inventory and content before the
original formatting, Clippy, native tests and build gates. Both sides still use
the explicitly selected SIGIL compiler pin; no compiler snapshot is included.
Build outputs are ignored. The source retains the parent repository's licensing.
Do not update this fixture as part of normal product refactoring.
