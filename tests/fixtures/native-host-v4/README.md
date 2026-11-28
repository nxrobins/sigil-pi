# Frozen older native host — compatibility fixture only

This is a byte-for-byte source snapshot of the native store, worker bridge and
application host that passed the complete sigil-pi source gate on 2026-09-08 at
18:29 UTC, before metadata support. It is test data, NOT another product runtime,
an alternate implementation to maintain, or an artifact to package for deployment.

The store supports stdio v1. The application host supports v3/v4 and AH3/HC3.
Rebuilding these exact sources makes it possible to verify that an actually older
executable rejects stdio v2 / application-host v5/v6 BEFORE state is opened, and
that the new host can continue an existing v4 operation when its profile and
application bundle are unchanged. A tiny reimplementation of a version check
would not supply that evidence.

All 34 source/manifests/lockfiles were compared against the passing baseline before
the main native implementation was updated. `manifest.json` records each input.
The aggregate uses sorted relative filenames and SHA-256 digest records:

`7a6c2838f514b67a34fef88e99674c0e123527365585a38b8d4bb8406b79b0d5`

`tests/legacy_native_support.py` verifies both inventory and content before the
existing formatting, Clippy, native-test and build checks. Build outputs are ignored.
The fixture retains the original Rust/toolchain and dependency pins and the parent
repository's licensing. It does not contain an old SIGIL compiler: both sides use
the explicitly pinned SIGIL runtime selected by the test environment.

Do not update these sources as part of a normal product refactor. If another
historical contract needs a fixture, add a separately identified checkpoint with
its evidence. This compatibility result does not qualify an in-flight-operation
upgrade from v4 to v6, whose application bundle identity is different.
