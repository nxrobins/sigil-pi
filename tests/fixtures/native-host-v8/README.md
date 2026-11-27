# Frozen v8 host — compatibility fixture only

These 46 files are an exact snapshot of main's native store, worker and service
before readiness host changes. They include v8 AH6/HC6 request facts, grouped
reads, and all earlier supported profiles. The 2,702-case source gate passed
on 2026-09-09 (session 37072, terminal 2ef97c); main's production native sources
were unchanged when this snapshot was taken. No readiness draft code is included.

This is test input, not a second maintained runtime or a deployable package.
Snapshot creation and inventory validation are not compilation or compatibility
evidence. Future tests must execute this actual old host against the old profile,
prove refusal of new profiles, and check documented state reopening/recovery.
Unchanged-profile reopening does not prove an in-flight bundle migration.

`manifest.json` binds all source, tests, Cargo manifests/lockfiles and Rust pins.
The lexically sorted relative-name SHA-256 aggregate is:

`b2ddb658d807c3bf0b826baf235e73004200633edb6c196d3ba985177b56d639`

`tests/legacy_readiness_native_support.py` validates the exact file inventory and
content before original formatting, Clippy, test and binary-build gates. The
SIGIL compiler/evaluator remains separately pinned and verified by its original
prerequisites; this snapshot does not bypass artifact admission.

Do not refresh this snapshot during refactoring. A new baseline needs a new
version, explicit source provenance and its own evidence. Target outputs are
excluded from the manifest. Source retains the parent repository's licensing.
