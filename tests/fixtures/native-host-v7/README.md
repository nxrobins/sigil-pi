# Frozen v7 host — compatibility fixture only

These 40 files are a byte-identical snapshot of the current native store, worker
and application host, taken before integrating v8 request admission. The v7 main
source gate passed in session 37117 (`a9bc5c`, 2026-09-09 UTC); its native sources
also retained their mandatory checks in the subsequent isolated 246-case v8 run
(`a1a51b`). This snapshot is test input, not a deployment package or a second
maintained runtime. Snapshot creation alone is not a build or compatibility pass.

It includes v7 HTTP correlation/response metadata, the older v3–v6 profiles, fixed
grantless evaluation and public assets. It does not include AH6/HC6, v8 request
facts, grouped reads or request admission. Tests must use this actual older host
to check rejection of v8 and unchanged-v7-profile state reopening. Such checks
do not qualify in-flight v7-to-v8 application-bundle migration.

`manifest.json` binds all source files, Cargo manifests/lockfiles and Rust pins.
The lexically sorted relative-name SHA-256 aggregate is:

`1d8a89ed9c5eaa3457c80a7a82e2c26e934d4088fd441538005ce83626538178`

`tests/legacy_request_native_support.py` validates the exact inventory and content
before formatting, Clippy, native tests and builds. Both test hosts continue to
use the explicitly pinned SIGIL compiler/evaluator prerequisites. No compiler
snapshot or exception to artifact admission is included. Build outputs are
ignored. The source retains the parent repository's licensing.

Do not refresh this fixture during normal refactoring. A new compatibility
baseline needs a new version, reviewed bytes and its own evidence.
