# sigil-pi candidate support matrix

Status: internal alpha; this matrix is a validation target, not a GA support commitment.

| Surface | Candidate support | Explicitly unsupported |
|---|---|---|
| Product API | Authenticated `/v1` endpoints in `product_service.py` | Legacy `/chat` and `/schedule*` research endpoints |
| SIGIL | Commit `eb9f1715dd3fadd3c9fe1f6ccb19c17f49a2ac0b`, with crates/stdlib tree hashes in `SIGIL_REV`, built with the `solver` feature | Any other or dirty SIGIL source/binary; solver-off builds (they fail closed at the first forge) |
| Z3 | The official Z3 release the binary was built against, present on the host as a shared library (CI pins 4.12.2, SHA256-verified, as SIGIL's solver lane does; local validation used Homebrew 4.16.0) | Hosts without `libz3`; an unpinned or mismatched Z3 for a signed release |
| Python | CI/release target CPython 3.12.11; local validation also runs on CPython 3.14.6 | Python <3.12, >3.14, PyPy, or untested patch versions for a signed release |
| Rust build | rustc 1.96.1 with the pinned SIGIL `Cargo.lock` | Floating stable/nightly toolchains |
| OS | Candidate release workflow: Ubuntu 24.04 hosted runner; local evidence: macOS arm64 | Windows and untested Linux distributions/architectures |
| Topology | One product worker on one host using one local POSIX state filesystem with correct `flock` and SQLite semantics | Multiple product workers until aggregate telemetry and a new capacity qualification exist; multiple hosts; NFS/network filesystems; object-store state |
| Transport | TLS 1.2+ at the product process, or plaintext loopback behind a local TLS proxy | Non-loopback plaintext |
| Identity | Expiring bearer-digest policy with explicit scopes/tools and rolling-restart overlap rotation | Anonymous/public access; external IdP integration; live revocation feed |
| Model/provider | Anthropic Messages-compatible configured endpoint/model; final named model validation is pending | Providers or models not tested in the release pilot |
| State | Conversations, sandboxes, schedules, signed audit records, durable request/turn/token/storage/audit quotas, configurable active-state retention (provisional 90-day default), and locked offline backup creation/expiry mechanics | Cognitive memory sidecar; guest-held secrets; unencrypted or unscheduled production backups |
| Capabilities | Committed manifest tools with explicit principal allowlists and minimal SIGIL grants | Arbitrary shell, arbitrary host filesystem, unrestricted network/tools |

The launch capacity, final active/backup retention periods, encrypted backup scheduler,
final model identifier,
and named operational/security ownership remain blocking decisions. Unsupported selections
must fail startup where the process can detect them; infrastructure topology violations must
be rejected by deployment policy and the release review.
