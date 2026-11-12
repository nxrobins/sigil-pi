# Changelog

All notable product-boundary changes are recorded here. The format follows Keep a
Changelog; version numbers follow Semantic Versioning once a non-development release exists.

## [Unreleased]

### Fixed

- An authenticated, authorized `/v1/ready` probe now returns content-free `503 not_ready`
  when durable request admission cannot write its quota store (for example, a full state
  disk). The fallback remains rate-limited at the configured tenant bound; other endpoints
  still fail closed. This fix is not present in the frozen v0.4.0 candidate.
- A compiler exchange is bounded end to end. The forge deadline (`PI_MCP_TIMEOUT_SECONDS`,
  or the turn's remaining budget when that is shorter) now covers writing the request and
  reading every byte of the response, not only the response's first byte. A compiler that
  stalls partway through a response, trickles it, or stops reading its input is killed at
  the deadline and the turn ends with its usual timeout; before, it held the caller, and
  every forge queued behind it, until the compiler moved on. A response longer than 64 MiB
  is refused and its compiler replaced (the pinned compiler's longest legitimate response
  is about 35 MiB), and each compiler stderr line is kept to its first 4,096 characters.
  This fix is not present in the frozen v0.4.0 candidate.
- A client that connects to the TLS listener and never sends a byte no longer stalls it. The
  TLS handshake ran inside `accept()`, on the one thread that serves the listener and with no
  timeout, so such a client held up every other client, and shutdown, for as long as it stayed
  connected. Each handshake now runs on its connection's own thread, bounded as a whole by
  `PI_SOCKET_TIMEOUT_SECONDS`. A handshake that fails or times out closes the connection with
  no response and no traceback. This fix is not present in the frozen v0.4.0 candidate.
- `product_main.py` and `state_tool.py` refuse an interpreter above the supported range at
  startup, as they already did below it: Python 3.12 to 3.14, as the support matrix states.
  They checked only the floor, so the product started and served on Python 3.15.1, which
  is what the bundle's launcher (`exec python3`) runs on a host whose default `python3` is
  3.15. The refusal comes before any of the project's other modules is loaded and exits
  with status 1, naming the range and the interpreter found. The range is defined once,
  in `toolchain.py`. The release SBOM's `python.requires` is generated from it, and
  `pyproject.toml`'s `requires-python` now reads `>=3.12,<3.15` (it said `>=3.12`), so pip
  refuses the same interpreters. This fix is not present in the frozen v0.4.0 candidate.
- Conversations, research-host schedules and audit records are synced to stable storage
  before the write returns. A save goes to a uniquely named scratch file beside its target,
  is synced, renamed into place, and the directory synced; each audit append is synced, and
  the one that creates a chain also syncs its directory; a store syncs each directory it
  creates into its parent. None of these writes was synced before, so after a power loss or
  a kernel crash the quota ledger, which is synced, could be ahead of the files. A save that
  fails at any step, including on a signal, now removes its scratch file; before, a failed or
  interrupted save left `<hash>.kv.tmp` holding the conversation until the next save of the
  same session, and deleting the session did not remove it.
  `SessionStore.delete` removes a conversation with any scratch a killed save left, including
  that older name; the product's deletion and retention do not call it yet, and the
  product's schedule store is not yet synced. This fix is not present in the frozen v0.4.0
  candidate.
- The product's own state is synced the same way, deleting a session removes its scratch
  files, and startup reports files that are gone. `product-schedules.json` and the
  clean-shutdown marker are written through the same scratch, sync, rename, sync path as
  conversations, so a failed save leaves no scratch file, and startup syncs the removal of
  the previous marker before it opens state. `DELETE /v1/sessions/{session}` and retention
  now remove the scratch files a killed save left beside a conversation, which held the
  conversation and survived both, and sync each removal before the registry row is removed.
  They zero the row's recorded bytes first, so an interrupted deletion is retried and is not
  mistaken for lost files. A chat turn settles its quota while it still holds the session
  lock; settled after it, a `DELETE` that took the lock in between left the tenant charged
  for the files it had just removed. Startup removes the scratch that killed saves left
  (conversations, schedules, the marker) and emits one content-free `state_reconciliation`
  event with counts. It is `critical` when a registry row recorded bytes for a
  conversation and sandbox, or for an audit chain, of which nothing is left: before, such a
  row was set to zero without a word. Startup continues either way. This fix is not present
  in the frozen v0.4.0 candidate.
- `state_tool.py` makes a backup durable before anything depends on it, and no longer holds
  the state in memory. `backup-cycle` published its new archive, deleted the expired ones
  and synced only the deletions; the new archive was never synced, so a power loss could
  leave the old backups gone and the new one incomplete. The cycle now syncs the archive's
  bytes before publishing its name and the directory after, and deletes nothing until both
  have succeeded: a failed sync stops the cycle with every older archive in place. `backup`
  syncs its archive the same way before it reports success, and `restore` syncs every
  restored file and directory before it installs the tree, and the install after it; neither
  synced anything before. `backup` read the whole state into memory before writing, up to
  the 10 GiB default limit, and `restore` read each member whole; both now copy in bounded
  chunks. The archive format is unchanged, byte for byte: archives written before and after
  this change restore with either version. An archive is now written in two passes, so a
  state file that changes between them fails the backup (`state changed while the backup
  was written`) rather than producing an archive that contradicts its own manifest. A
  sandbox file that a tenant's tool named `product-quotas.sqlite3` is backed up as the file
  it is; it was opened as the quota ledger, which ended every backup of that state with an
  unhandled error, or archived other bytes than the file's when it was a database. Backup
  and restore now need a filesystem that can sync files and directories, as the cycle's
  deletions already did. This fix is not present in the frozen v0.4.0 candidate.
- The scheduler outlives an error from its schedule store and reports its own liveness. One
  `OSError` from the store, while reading the entries or recording a finished run (a full
  disk during a scheduled turn was enough), ended the scheduler thread: every tenant's
  schedules stopped until the process was restarted, while `GET /v1/ready` answered `200`.
  A failed tick is now counted and the next one runs; a run of failed ticks is logged as one
  content-free critical `scheduler_tick` event, and the tick that ends it as an
  informational one. A run whose completion could not be recorded keeps its claim and runs
  again when its lease expires, and a claim the store refuses fails its tick instead of
  leaving no trace. Readiness gains a `scheduler` component and both metrics formats a
  `scheduler` object (`healthy`, `thread_alive`, `ticks_total`, `tick_failures_total`,
  `last_tick_unix`, `last_tick_age_seconds`, `last_progress_age_seconds`,
  `stall_after_seconds`): the scheduler is ready while its thread is alive and it has
  completed a tick or recorded a scheduled run within `PI_TURN_LEASE_SECONDS` plus the
  one-second tick interval. `config/alert-policy.json` gains the `scheduler_not_running`
  alert, and `docs/runbooks.md` its procedure. The audit and retention monitors no longer
  end when their log sink raises (a full disk under stdout): a log line that cannot be
  written leaves their verdict standing, an error they did not handle fails them closed
  until a check completes again, and a monitor whose thread has ended without a stop reports
  itself unhealthy. A background loop started after `stop()` runs again, and `start()` on
  one that is still stopping raises instead of returning. This fix is not present in the
  frozen v0.4.0 candidate.
- A model reply is no longer used without regard to why it ended. Nothing read `stop_reason`,
  so a reply the provider cut off at the output cap was returned as complete and its tool
  calls, possibly truncated, were run; and a refusal, which the provider answers with HTTP
  200 and empty content, became an empty successful reply. The reply parser
  (`tools/parse_reply.sigil`) now passes the stop reason to the host, which reads it before
  it acts on any part of a reply. `end_turn`, `tool_use` and `stop_sequence` are whole
  replies. Any other value, including one the provider adds later, ends the turn with a
  typed error (`ModelOutputTruncated`, `ModelRefused`, `ModelReplyIncomplete`): no tool call
  in that reply runs, the reply does not enter the transcript, and its tokens are still
  metered. The product answers `502 agent_failure`; the research host answers `500` with
  the reason. Every model request now sets `thinking` (`adaptive` unless configured, where
  it was left to the model's default), and the thinking blocks of a tool-using turn go
  back to the provider unchanged with each tool result instead of being dropped; they are
  not kept in the saved conversation. The agent's default output cap, which thinking and
  the answer share, is 16,000 tokens (it was 1,024), and the research host reads
  `PI_MAX_TOKENS` and `PI_THINKING` (`adaptive`, `disabled` or `between_tools`). This fix
  is not present in the frozen v0.4.0 candidate.
- The product's default token reservation is sized to what a turn can spend, so nine tenths
  of a tenant's daily token limit can be used, and the product host takes its output cap and
  thinking setting from the agent. A turn is admitted only while the tokens the tenant has
  used, the reservations of its turns in flight and one more reservation fit in
  `PI_TOKENS_PER_DAY`. `PI_TOKEN_RESERVATION_PER_TURN` defaulted to 4,000,000 against a limit
  of 10,000,000, so a tenant was refused with `429 token_quota_exceeded` once it had used
  about 6,000,000 tokens. The default is now 1,000,000 and a tenant is admitted until it has
  used 9,000,000. A turn at the default bounds (8 steps, each sending a 256 KiB transcript
  and receiving a reply at the output cap) is about 980,000 tokens at the provider's figure
  of 2.5 characters to a token. The admission rule is unchanged, and so is a reservation that
  a deployment sets itself. A deployment that raised `PI_MAX_STEPS`, `PI_MAX_TOKENS` or
  `PI_MAX_HISTORY_BYTES`, or that configures a system prompt above about 6 KiB, and relied on
  the old default should set the reservation to match: a turn that spends more than its
  reservation can take the day's total past the limit by the difference. The product host's
  default for `PI_MAX_TOKENS` is now the agent's, 16,000, where it kept 1,024. The cap is the
  most tokens one model reply may use, thinking included, and a reply that reaches it fails
  the turn with `502`. Startup requires the reservation to cover `PI_MAX_STEPS` x
  `PI_MAX_TOKENS`, which is now 128,000 at the defaults and was 8,192, so a deployment that
  set a smaller reservation and no cap must raise the reservation or set a lower cap. The
  product reads `PI_THINKING` (`adaptive`, `disabled` or `between_tools`), sends it on every
  model request and reports it in `effective_settings`; any other value fails startup.
  Without it a deployment whose model rejects `adaptive` would fail every turn with `502`,
  now that every request sets `thinking`. A chat request whose deadline passes while it waits
  for its session is charged nothing; it was charged the tokens of the turn its thread had
  served before, which can happen only to a caller that reuses a thread, and the listener
  gives every request its own. This fix is not present in the frozen v0.4.0 candidate.
- The failure drill's verdicts are about the state its faults touch, and its report records
  the configuration it measured. Each category committed one session and then injected its
  fault into other sessions, so `state_integrity_verified` meant that a bystander's files
  had survived: a release that lost the session it was writing when the fault hit would have
  passed. Every fault now goes to the session the category committed. The turn that fails,
  the save that is interrupted and the audit chain that is corrupted are that session's own,
  and it is compared by content: its committed messages and audit records must still open
  the conversation and the chain, and every other committed file must be byte-identical. The
  interrupted-write case stops the release as it replaces the session's existing
  conversation file; it used to require that the file did not exist yet. It finds the
  scratch file as the one file that appeared beside the target, not by name (releases name
  it uniquely since 2026-11-10, so the drill could no longer find it), and fails if the
  restart adopts the write or leaves that scratch file behind. The corrupt-state case reads
  the committed files between the refused start and its own repair, which used to hide
  whatever the release had done to the corrupted chain. The service is started with the
  timers and limits the release ships: the drill set a 60-second lease (shipped: 300),
  30-second turn and compiler deadlines (120 and 90) and one model retry (2), and recorded
  none of it. The report gains a `configuration` object with the wiring the drill supplies,
  any `--service-setting` override beside the shipped value, and the settings the release
  itself reports as in force; a run that shortens a timer or a retry budget is not
  qualification-eligible. The recovery drill and its fixture leave the same timers to each
  release and record the settings they keep. All three drill scripts refuse an interpreter
  outside the supported range, from the one definition in `toolchain.py`; they checked only
  the floor. A qualifying failure drill now waits out the shipped 300-second lease, and
  needs a candidate that reports its settings and removes a killed save's scratch file at
  startup. The frozen v0.4.0 candidate does neither, and the `scripts/release_drill.py` it
  ships has none of this.
- The agent's table of per-session locks no longer grows with every session the process has
  seen. `PiAgent` runs the turns of one session one at a time under a lock it keeps in a
  table keyed by session id, and an entry was never removed: 300 sessions left 300 entries,
  and deleting all 300 left 300. The product's turns go through the same agent, so its
  workers leaked an entry per session too, although the product's own lock table is
  reference-counted. An entry now lives only while a turn holds the session's lock or waits
  on it, so the table is bounded by the turns in flight. An entry in use is never dropped,
  whatever else arrives, so the turns of one session still run one at a time. This fix is
  not present in the frozen v0.4.0 candidate.

### Added

- A strict audit mode, `AuditLog(strict=True)`: an audit append that fails raises
  `AuditWriteError`, so the forge whose guest already ran fails, its result is withheld, and
  the turn ends with that error instead of a reply, including when the same fault also fails
  the session save. The research host's default is unchanged (the failure is printed and
  the turn continues), and the product host does not enable strict mode yet.
- A dispatch-only, candidate-bound six-category failure drill, with bounded tmpfs exhaustion,
  observed pre-rename host interruption, signed-state continuity checks and retained raw
  results. Runtime recovery uses a separate call after fault detection; host recovery waits
  for persisted leases to expire as required by the packaged runbook.
- A socket-level test harness for the product server. `tests/conftest.py` starts the real
  `serve_product()` on an ephemeral loopback port, plain or with a throwaway TLS certificate
  generated at test time, and `tests/test_product_socket.py` drives POST, DELETE, invalid
  `Content-Length` and TLS round trips through it over real connections. The TLS tests fail
  rather than skip when `openssl` is missing. Test-only; no product behaviour changes.
- A lifecycle test harness for the product scheduler, whose `start()` and `stop()` no test
  had run. `tests/conftest.py` builds a real `ProductScheduler` over a real schedule store,
  runs its real thread with an injected clock and a scripted agent, and stops and joins it
  in teardown, failing a test whose scheduler does not stop rather than hanging.
  `tests/test_product_scheduler_lifecycle.py` pins a due entry running once, the cadence on
  the injected clock, `stop()` on an idle scheduler and with a turn in flight, and a session
  deletion racing a claim, and requires a real-thread lifecycle test for every class in the
  product host that starts its own thread. Test-only; no product behaviour changes.
- The operations metrics measure the compiler processes, where guests run, and record the
  configuration in force. Every resource figure used to describe the Python host alone, so a
  guest that grew the compiler moved none of them. `GET /v1/metrics` now also reports, under
  `resources`, `compiler_process_count` and the compilers' resident memory, threads and open
  file descriptors summed over every live compiler (`compiler_resident_memory_bytes`,
  `compiler_thread_count`, `compiler_open_fd_count`), read from `/proc` on Linux and from the
  kernel through libproc on macOS; a figure that cannot be read is `null`, never a partial
  sum. A new `effective_settings` block reports the model, the provider endpoint's host name
  and every numeric limit, deadline and quota as the components received them, and no
  secret, credential or path. The Prometheus form exports the compiler figures and the
  numeric settings as label-free gauges (`sigil_pi_compiler_*`, `sigil_pi_setting_*`); the
  string settings are not exported. `PI_MCP_TIMEOUT_SECONDS`, `PI_AUDIT_VERIFY_SECONDS`,
  `PI_MAX_SCHEDULES_PER_TENANT`, `PI_MAX_EXPORT_BYTES` and `PI_LLM_RETRIES` are now read with
  the other settings, so a value that is not a positive integer (for `PI_LLM_RETRIES`, not
  an integer) fails startup before the state directory is touched. This change is not
  present in the frozen v0.4.0 candidate.
- Online backups: `state_tool.py backup --online` and `backup-cycle --online` back up a
  state while the service is serving it, with no clean-shutdown marker and no stop. A
  backup needed the service stopped, and on one worker a stop every 15 minutes leaves 0.9
  seconds each within 99.9% availability. The tool, from its own process, takes the locks
  the service takes, one at a time: each session's `locks/<digest>.lock` while it copies
  that session's conversation, sandbox and audit chain into a private directory beside the
  output, then the schedule store's lock for the store. It waits for a busy lock in the
  kernel's queue for at most `--lock-wait-seconds` (default 150; keep it above
  `PI_TURN_DEADLINE_SECONDS`), and a lock it cannot take fails the whole backup: no session
  is skipped. The quota ledger is captured last, through SQLite's backup API, and made to
  agree with what was copied: the rows of sessions created meanwhile are dropped, a session
  deleted meanwhile is left out whole, turn leases are dropped, and each remaining row
  records the bytes it recorded while its session's lock was held, so startup on a restored
  tree reports nothing lost. The copy is then validated and archived exactly as a drained
  state is. The archive is schema 1 and every release restores it; its manifest adds `mode`
  and an `online` object (sessions archived, created and deleted meanwhile, the longest time
  one session's lock was held, and how long the capture took), has no `product_version` or
  `clean_shutdown_unix`, and its `created_unix` is the moment the backup began, which backup
  age is counted from. Each session is captured at a turn boundary; the archive is not one
  instant across sessions. Customer files that no registry row attributes fail the backup,
  as they fail an offline one, and so does a backup directory whose filesystem cannot keep
  two state file names apart (one that ignores case). The private copy needs room for the
  state uncompressed and is removed however the backup ends; a killed `backup-cycle` leaves
  it, and the next cycle removes it. Offline backups are unchanged and remain the
  pre-upgrade path. This change is not present in the frozen v0.4.0 candidate.

## [0.4.0] - 2026-09-07

This is an internal-alpha release candidate, published as a GitHub prerelease, not a
general-availability declaration. Production memory remains disabled. The frozen candidate
record is advanced only after the published asset and its attestations have been verified;
the v0.3.0 recovery drill does not qualify this new artifact.

### Added

- Research-host WAVE semantic memory protocol v2: negotiated startup, host-served semantic
  embeddings, model-identity checks, reindexing, and bounded memory lifecycle operations.
- A guard requires Python package metadata to agree with the release `VERSION`.

### Changed

- sigil-pi is public, and `main` requires pull requests, up-to-date GitHub Actions checks
  for both CI jobs, and resolved conversations, including for administrators. There is no
  independent approval requirement while the repository has a single maintainer. Public
  visibility enables the existing provenance/SBOM attestation steps for this release;
  successful signing must still be verified from the published artifact.
- The candidate workflow marks releases as prereleases and does not promote them to Latest.
- The SIGIL toolchain pin moves to the public `nxrobins/sigil` (`8277a1d9`, the 2026-09-05
  export of private development; rustc 1.98.0 via its `rust-toolchain.toml`). CI and the
  release workflow fetch it anonymously and prove the pinned ref is public before checking
  it out; the `SIGIL_REPO_TOKEN` secret is no longer read.
- Every forge declares the `ephemeral` host profile and every sigil-serve config carries
  `"host_profile": "ephemeral"`. The new toolchain's CSIR v9 verifier refuses a host call
  under `@Internal` control to an undeclared host (`I013`), which is the shape of every tool
  here; the declaration is what the tools were missing, not a relaxation of anything.
- Building the pinned compiler needs the Lean toolchain SIGIL pins in
  `proofs/lean/lean-toolchain` (via elan/lake): it statically links a Lean-built kernel.
  Build-time only; CI installs it from SIGIL's pin, and `ci.sh` names it when missing.

### Fixed

- Python package metadata now agrees with `VERSION` (it had remained at 0.1.0).
- The forge job silently pointed at the wrong repository. GitHub repository names are
  case-insensitive, so the capitalised private name resolved to the new public `nxrobins/sigil`
  the day the private repo was renamed — at a ref only the private one had. The workflows now
  spell the public repo canonically, and a guard pins it.

## [0.3.0] - 2026-08-24

### Added

- `docs/evidence/candidate.json`: the first frozen-candidate record, written from the published
  v0.2.0 asset with `rollback_from` = v0.1.0. With it, `./product-ci.sh` runs end to end for the
  first time and fails closed at the first genuinely external evidence report.
- `scripts/drill_fixture.py`: produces the recovery drill's pre-upgrade backup honestly — boots
  the old release, commits one real turn through its own forge path against a local
  Anthropic-shaped mock provider, drains cleanly, and backs up with the old release's own
  `state_tool.py`.
- `.github/workflows/recovery-drill.yml` (dispatch-only): the qualifying distinct-version drill
  on a clean Linux runner, bound to the frozen candidate and its recorded rollback target before
  anything is drilled, with the pinned `libz3` installed into the loader's default path.

### Fixed

- `ARCHIVE_NAME_RE` lacked `_` and rejected every real Linux artifact
  (`...-linux-x86_64.tar.gz`) on first contact; the fixtures' `test-platform` tag had hidden it.

### Notes

- The payload-parity gate caught its first real drift: documenting the drill workflow edited
  `docs/operations.md`, so the v0.2.0 archive's operator documents no longer matched the
  repository and `verify_release` refused it — which is why this release exists. An edit to any
  parity-checked operator document requires a new candidate; that is the freeze working, not
  overhead to be optimized away.

## [0.2.0] - 2026-08-23

No functional changes from 0.1.0. This release exists so that a release with a
recorded rollback target can exist: `docs/evidence/candidate.json` requires
`rollback_from` to name a distinct published release, the recovery drill requires
two distinct versions, and 0.1.0 was the first release — so nothing could roll
back to anything. 0.2.0 names 0.1.0 as its rollback target, which is what makes
the frozen-candidate record writable and the qualifying distinct-version
rollback drill possible for the first time.

## [0.1.0] - 2026-08-23

### Added

- Authenticated, tenant-namespaced `/v1` chat, operations, scheduling, export, and deletion
  APIs with distinct scopes and stable errors.
- Fail-closed TLS startup rules, cross-worker session/schedule locks and leases, durable
  request/concurrency quotas, graceful draining, and automatic signed-audit verification.
- Deterministic release bundle with pinned SIGIL runtime/stdlib, checksum, internal manifest,
  CycloneDX SBOM, commit-pinned CI actions, and tag provenance/SBOM attestation workflow. The
  attestation steps are skipped, with a recorded warning, while the repository is user-owned and
  private — GitHub does not persist attestations for those — so a release cut today is
  deliberately unattested and cannot satisfy the reproducible-release readiness area.
- Validated offline backup/restore and an independent 85% line / 85% branch host coverage
  gate.

### Changed

- Product runtime no longer imports benchmark helpers and strips the benchmark-only
  unverified-certificate override from the compiler child.
- The forge is resolved through `toolchain.py` (explicit path, installed release, or source
  checkout) by both hosts, and both forge through the vendored `runtime_client`; SIGIL's
  bench client is no longer imported anywhere at runtime. The research host additionally
  carries a single shared bearer token, a state-directory lock, `/health`, a step-boundary
  turn budget, and SSE step events (upstream PRs #22–#27); none of these is the product
  contract.
- Product memory remains disabled until its complete quota, export, retention, and recovery
  lifecycle exists.

### Fixed

- The release candidate is frozen rather than rebuilt at gate time. `product-ci.sh` used to
  rebuild it from the working tree, so the digest every evidence artifact binds to moved whenever
  the readiness record was edited — recording a passing result invalidated the evidence for it.
  The gate now verifies the published archive named by `docs/evidence/candidate.json`, and the
  readiness record is no longer packaged inside the artifact it describes.
- One tenant's turn hitting the hard deadline no longer ends forging for every other tenant.
  The killed compiler is retired and replaced on the next forge — re-verified against
  `SIGIL_REV` each time — instead of latching a closed client for the life of the process.

### Security

- Tool authorization is enforced against hostile model output, not only advertised tool
  schemas.
- The gate and the release builder compile a solver-verifying compiler (`--features
  sigil-mcp/solver`), and the suite forges through the product client, which strips the
  benchmark-only verification override; a solver-off build fails closed at the first forge.
- Production audit records require a separately injected HMAC key and are verified before
  readiness.

## [0.1.0-dev] - 2026-08-20

- Internal designation used while the product boundary was being built. Never tagged and
  never published; superseded by 0.1.0, which is the first tagged release.
