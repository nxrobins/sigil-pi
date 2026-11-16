# Changelog

All notable product-boundary changes are recorded here. The format follows Keep a
Changelog; version numbers follow Semantic Versioning once a non-development release exists.

## [Unreleased]

### Fixed

- The 100% coverage inventory covers the server, the scheduler and the agent's security
  boundaries (audit 2026-11-10, F-17). It named 23 entries, 15% of `product_service.py` and
  nothing in `agent.py`. It now names 145: the listener, TLS, request framing, answers and
  drain; the dispatch path; the readiness snapshot and the loops it reads; every function of
  `ProductScheduler` and `ProductScheduleStore` and the cross-worker session lock; the
  tenant tool grants; the agent's tool dispatch, sandboxes, session locks, grant redaction
  and signed audit chain; and the validator of release evidence. New tests take the paths no
  test had taken (an unknown route's 404, a schedule body without exactly its four fields, a
  body that is JSON but not an object, a scheduler with no time for a turn, a claim that can
  be neither run nor given back, a leased entry, a directory in place of the schedule store,
  an audit chain that is empty, has blank lines or ends in a torn record), and pin two
  handlers that every test had fed only an `OSError`: a log sink that fails with
  `ValueError` (a closed stdout) and a claim the store refuses with any error. A guard fails
  on a coverage pragma, or any line coverage.py would leave out of the measure, in a module
  the gate measures, and `docs/product-readiness.md` is held to `ci.sh`'s inventory.
  `scripts/check_readiness_evidence.py` is measured. CI's workflow holds a read-only token
  and its checkouts no longer leave it in `.git/config`, and `ci.sh` refuses a SIGIL
  checkout whose `Cargo.lock`, `Cargo.toml`, `rust-toolchain.toml` or `proofs/lean` is not
  the pinned commit's, as the release build does.

- Every answer the product listener writes follows the error contract, and no traceback
  reaches stderr when a client goes away (audit 2026-11-10, F-25). Every answer named the
  interpreter and its version (`Server: BaseHTTP/0.6 Python/3.12.13`); it now says
  `Server: sigil-pi`. The standard library's own answers to a request the server cannot read
  or route were HTML pages, some without a status line, with reason phrases that repeated the
  request ("501 Unsupported method ('PUT')"); they are now product errors with a request id,
  `Cache-Control: no-store` and the standard reason phrase: 400 `malformed_request`, 414
  `request_line_too_long`, 431 `request_head_too_large`, 501 `method_not_supported` and 505
  `http_version_not_supported`, counted in `sigil_pi_http_errors_total` by class and kept out
  of the request series the error-rate alert divides. A request line without a version is
  answered as HTTP/1.0, with headers. The transport's 400 `invalid_content_length` is logged
  and counted as a dispatched error, with `X-Request-ID` and `Cache-Control`, and a request
  with a `Transfer-Encoding` now gets it too (a chunked body was read as empty). A request head
  that ends before it is complete is no longer processed. A client that leaves before its
  answer is written is counted as `client_disconnected`, with an `http_response_failed` event
  carrying only the request id, status and class; a failed or timed-out handshake and a head
  that times out or is cut off are counted (`tls_handshake_failed`, `request_timeout`,
  `request_aborted`) and write no log line. A request is answered even when its log line
  cannot be written, where it was dropped with a traceback, and a drain now waits, within its
  budget, for the answers of requests already dispatched.
- Each tenant's tools use only that tenant's credentials and allowlist (audit 2026-11-10,
  F-09). The product handed every tenant the deployment's `PI_SECRET_*` and
  `PI_NET_ALLOWLIST`: a principal allowed `gh_issues` queried GitHub with the operator's
  token, and one allowed `fetch` reached every host allowlisted for anyone. A principal's
  entry in the auth file now names the secrets its tools may use (`"secrets"`) and the hosts
  its `fetch` may reach (`"net_allowlist"`); two credentials for one principal must agree on
  both. The values are the tenant's, read once at startup from
  `PI_TENANT_SECRETS_DIR/<SHA-256 of the tenant>/<name>`: regular files and directories owned
  by the service's user with no group or other access, no symbolic links inside. A policy
  that names a secret its tenant has no value for stops startup. Chat and scheduled turns
  pass the principal's grants to the agent, a scheduled turn its creator's as the policy
  stands when it runs, and a principal's turn never falls back to the deployment's: a
  tenant with none is refused by the runtime before any request leaves. Product mode refuses
  `PI_SECRET_*` and `PI_NET_ALLOWLIST` at startup, naming the replacement; the research host
  keeps them.
- A product turn whose audit record cannot be written now fails closed (audit 2026-11-10,
  F-04). The product host did not run its audit log strict: with a session's chain unwritable,
  a chat turn answered `200` with its reply after two guests had run and neither was recorded,
  and the operator got two tracebacks on stderr, no metric and no event. The host now builds
  its log strict, which is not a setting. Such a turn answers `500 audit_write_failed` with no
  reply, and a scheduled run is recorded `audit_write_failed`, however the turn unwound after
  the failure (the settlement that follows it on the same disk used to replace it, answering
  `500 internal_error`). Each one is counted in `audit_verification.write_failures_total`,
  logged as a content-free critical `audit_write` event, and fails readiness through the
  `audit_verification` component until the process is replaced: a later verification passes,
  because the chain a failed append leaves is a valid prefix. The new `audit_write_failed`
  alert reads `audit_verification.writes_healthy`, and the new runbook section "Audit write
  failure" covers it, including a torn chain. The tenant is charged for the steps the turn
  completed before the step whose record failed.
- A client without a credential can no longer hold the product server's threads and memory
  (audit 2026-11-10, F-08). The listener read a declared body of up to 1 MiB, or waited for it
  under the socket timeout, before it authenticated the request; it started a thread for every
  connection, with no cap; and its only clock started again at every read. 200 connections that
  sent headers and no credential held 200 threads, and a client sending a byte every 2 seconds
  stayed connected for 13 seconds against a 3-second timeout. A request is now authenticated from
  its headers before any of its body is read, and a refused one is answered and closed at once. At
  most 128 connections are handled at a time; the next ones wait in the listen queue and are
  served as connections end. A request's TLS handshake, head and body must arrive within 60
  seconds of the server taking its connection, however they are paced; the deadline does not
  bound the turn. Header lines may total 64 KiB, where the standard library allowed 6.4 MiB.
  Content-Length must be one run of digits, so `+38`, `3_8` and two different lengths are `400
  invalid_content_length`, and a body that ends before its declared length is refused instead
  of processed.
- One tenant's schedules no longer hold up another's (audit 2026-11-10, F-06). The scheduler
  ran every due turn inline on its one thread, in store order: with one tenant holding three due
  schedules of 2 seconds each, another tenant's due schedule started 6.0 seconds late, and at the
  defaults (32 schedules per tenant, a 120-second deadline) one tenant could hold the others back
  for an hour. Scheduled turns now run on a pool, at most 4 at once and at most 1 of a tenant's at
  a time, so another tenant's due schedule starts at once, and one that falls due during a long
  tick starts within an interval. A full pool serves first the tenant it served least recently,
  and of a tenant's schedules the one due longest. A claim's lease now runs from when the claim is
  made: the tick read the clock once, which shortened leases late in a long tick. An idle tick
  reads the schedule store once, not once per schedule. Since one turn that does not return no
  longer stops the other tenants' schedules, the scheduler now also reports itself unhealthy
  while any turn has run for longer than its stall bound. `POST /v1/schedules` refuses an
  `every_ms` below 60000 (one minute) with `400 invalid_interval`. A schedule an earlier release
  stored below that still loads and is listed as stored, and runs no more often than once a
  minute.
- The recovery drill (`scripts/release_drill.py`, shipped as `bin/sigil-pi-release-drill`)
  runs a real turn in every phase (audit 2026-11-10, F-18). Each phase started a release,
  asked it for `/v1/ready`, `/v1/version` and `/v1/metrics` with an operations-only credential
  in a tenant of its own, and compared files afterwards. No phase read the restored
  conversation or ran a turn, so a candidate that could not read the old release's sessions
  passed, and the restore and rollback figures were times to readiness. Each phase now reads
  one session through the release (its conversation and audit chain must be exactly what was
  committed, and then what the phases before it left), runs a turn in it against a stand-in
  provider on loopback, and reads it again: the candidate continues a conversation the old
  release wrote, and the rollback continues one the candidate wrote. `restore_rto_seconds`
  and `rollback_seconds` now run to the first successful turn. A failing `/v1/version` or
  `/v1/metrics` request fails the phase at once; it used to repeat the readiness wait and
  pass when the wait ran out. The session is the one `scripts/drill_fixture.py` commits
  (`--tenant` and `--session` name another). It is read and continued with a credential in
  its own tenant (`chat`, `sessions:read`), and readiness is asked with the drill's own
  (`ops:read`), so the wait is not charged to that tenant's request window. The report gains
  each phase's `turn`, and `configuration.wiring`, `.provider`, `.session` and the settings
  each release reports as in force; a candidate that reports none is not
  qualification-eligible. A drill that stops writes a report that names the phase and the
  reason, which can never qualify, and each phase keeps its service's log under
  `<work-dir>/logs`. The final comparison judges the continued session by content and every
  other committed file byte for byte, by the rule the failure drill uses. In the
  `Recovery drill` workflow the two tags reach the shell through the environment and are
  checked to be release tags (they were written into the script), the token can only read,
  and the report, the fixture's evidence and the service logs are uploaded when the drill
  fails; neither drill workflow leaves its token in the checkout. The copy of the drill
  inside the frozen v0.4.0 candidate has none of this.
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
- Shutdown starts no more scheduled turns, counts every piece of a tenant's work, and waits
  for all of it on one budget. A stop request used to leave the scheduler running every
  entry that was due: with one scheduled turn in flight and five entries due, four more
  turns started after the request, the drain failed, and no clean-shutdown marker was
  written, so the offline backup refused to run. The scheduler now looks before each entry
  and again directly before it asks the agent. A turn that was claimed but had not started
  gives its claim back: nothing is recorded, the tenant is charged nothing, and the entry
  runs when the service next starts. Scheduled turns respect the draining state that chat
  turns respect and are counted with them. `GET /v1/schedules`, `POST /v1/schedules`,
  `DELETE /v1/schedules/{name}`, `GET /v1/sessions/{session}/export` and
  `DELETE /v1/sessions/{session}` are counted as work in flight as well, and are refused
  with `503 service_draining` once a drain has begun, as `POST /v1/chat` is. Before, only
  chat turns were counted: a drain could report complete, and the marker that lets an
  offline backup run could be written, while a deletion was halfway through. The
  operational endpoints keep answering during a drain. `PI_DRAIN_TIMEOUT_SECONDS` is now
  the budget of the whole drain: the listener, the scheduler, the retention and audit
  monitors and the work in flight are waited for at once, and shutdown gives up at the one
  deadline. It used to wait for each in turn with the whole budget, four budgets in the
  worst case. A retention sweep deletes nothing more once it has been asked to stop, and a
  chat turn stays counted until its quota has been released. An unclean shutdown now
  reports `work in flight did not drain within N seconds`. This fix is not present in the
  frozen v0.4.0 candidate.
- The agent's table of per-session locks no longer grows with every session the process has
  seen. `PiAgent` runs the turns of one session one at a time under a lock it keeps in a
  table keyed by session id, and an entry was never removed: 300 sessions left 300 entries,
  and deleting all 300 left 300. The product's turns go through the same agent, so its
  workers leaked an entry per session too, although the product's own lock table is
  reference-counted. An entry now lives only while a turn holds the session's lock or waits
  on it, so the table is bounded by the turns in flight. An entry in use is never dropped,
  whatever else arrives, so the turns of one session still run one at a time. This fix is
  not present in the frozen v0.4.0 candidate.
- The load harness accounts for every client, enforces each documented criterion and
  records the configuration it measured. A load client that met a response its classifier
  did not expect (an `error` field that is not an object, a body cut short, a `reply` that
  is not a string) ended without recording anything; the report still counted it as
  offered, and the only volume floor was "more than zero requests". Such a response is now
  a failed request and the client carries on, and a client that stops for any other reason
  is named in the report. The report gains a record for each client, and a run passes only
  if every client was still issuing requests at the deadline and completed one successful
  request for each 135 seconds of the run (26 over the hour;
  `--minimum-requests-per-client` raises that floor and cannot lower it).
  `runtime_unhealthy_replacements_total` is read with every resource sample and must not
  move, which is criterion 8 of `docs/capacity.md` and was read by nothing. A turn answered
  without its tool or retry telemetry is counted, where the missing figure was read as zero
  (criterion 7); a setup reply that is not a valid one is a setup failure; and a client
  that fails during setup no longer holds the run until its setup timeout. The report
  records the provider endpoint's host, the model and every limit the service reports in
  force, and a service that reports none, or whose settings change during the run, cannot
  pass. Redirects are refused: the harness followed them, and sent its bearer token to
  whatever origin one named. The TLS context is built once for the run: building one for
  every request added 5 ms to each reported latency, and 70 ms at the median with 50
  clients. `scripts/check_readiness_evidence.py` requires all of this from a load report's
  own records and recomputes the resource bounds from its samples, where it trusted the
  report's summary. A load report from the earlier harness no longer validates, and neither
  does one from a release that does not report its settings; the frozen v0.4.0 candidate
  does not.
- The recovery drill restores an online backup that the candidate took while it served
  turns (audit 2026-11-10, F-03 and F-18). Until `state_tool.py backup --online` a backup
  needed the service stopped, and the drill still restored only its input, a backup the
  fixture takes offline, so the 15-minute objective rested on a path production cannot use.
  After its four phases the drill now restores the state the rollback left with the
  candidate's own state tool and starts the candidate on it. While the candidate serves turns
  in the session, the first held in flight at the provider until the backup has started, its
  `state_tool.py backup --online` copies the live state from another process. The archive is
  restored into a new path, and the candidate starts on it, reads the session exactly as the
  archive holds it and runs a turn. The report's new `online_backup` records the backup's
  mode, its age when its restore began (`age_at_restore_seconds`, held to the RPO threshold),
  the longest time it held one session's lock (`max_session_lock_hold_ms`), the turns run
  while it was taken, the recovery to the first turn on the restored state, and the two
  starts. A turn that fails or goes astray while the backup runs, an archive that is not the
  session at a turn boundary, a candidate whose tool cannot back up online, and a candidate
  that cannot continue the restored session each stop the drill. The drills now create their
  credentials files and the recovery report with mode 0600, never through a path that already
  exists, where they wrote them and then changed the mode; a truncated or corrupt backup or
  release archive stops the drills with their own error; and whatever stops the recovery drill,
  it writes its report and the command exits 2, as the failure drill's now does too.
- The release workflow (`.github/workflows/release.yml`) tests, builds and signs in separate
  jobs, and releases only a tag on `main` (audit 2026-11-10, F-16). One job held
  `contents: write`, `id-token: write` and `attestations: write` while it installed the
  development packages, ran the whole suite, built, packaged, signed and published from one
  workspace, so anything the tests ran could change the files that were then signed; and
  nothing checked where the tag pointed, so branch protection did not constrain what was
  released. The workflow now grants nothing by default. The `gate` job, which can only
  read, refuses a tag that is not a release tag, that differs from `VERSION`, or whose
  commit is not on `main`, before it fetches or builds anything, and then runs `./ci.sh`.
  The `build` job, which can only read, starts from a fresh checkout on a fresh runner,
  restores no cache and installs no Python package, and packages a compiler that
  `scripts/build_release.py` builds itself from the pinned source (it used to package the
  gate's, with `--no-build`). The `publish` job alone can write and sign, and checks out
  nothing: it takes the build job's artifact, checks the archive against the digest the
  build job reported as a job output, then signs, verifies and publishes it. Every step
  that reads the tag checks it first, and no value is pasted into a script. The gate no
  longer upgrades pip to whatever the package index serves. The frozen v0.4.0 candidate
  was released by the one-job workflow.

- The gate's Python environment is pinned to exact files, and every test and CI job has a
  time limit (audit 2026-11-10, F-16 and F-17). `requirements-dev.lock` pinned versions but
  not hashes, and the forge job upgraded pip to whatever the index served. Each requirement
  now carries the sha256 of every file PyPI publishes for its version, and the lock itself
  requires hashes and refuses source builds, so any install of it, whatever the command,
  gets exactly the locked files or fails. `ci.sh`'s header and both CI jobs install it with
  `--require-hashes`, CI keeps the pip its pinned Python ships, and `ci.sh` refuses a `.venv`
  that differs from the lock, naming each package. `pytest-timeout` 2.4.0 joins the lock:
  every test now has a 1200-second limit (`pytest.ini`; the slowest takes about 25 s), a
  test that reaches it fails with `Failed: Timeout (>1200.0s) from pytest-timeout.` and the
  run goes on, and a venv without the plugin is an error. The standalone and forge jobs stop
  after 45 and 90 minutes, where GitHub's default is six hours. Rebuild an existing `.venv`
  with `.venv/bin/pip install --require-hashes -r requirements-dev.lock`. The release
  workflow has no job limit yet.
- The readiness gate takes a drill report only as the bytes its own workflow run signed
  (audit 2026-11-10, F-19). `scripts/check_readiness_evidence.py` checked what
  `docs/evidence/failure-injection.json` and `recovery-drill.json` said and that they named the
  candidate's digest, and nothing else, so a report typed by hand, or a real one edited after
  its run, passed. Each drill workflow now signs its report with `actions/attest` as SLSA
  provenance, in an `attest` job that alone holds `id-token: write` and
  `attestations: write`, runs nothing of the repository's, checks the report against the
  digest the drill job recorded, and reads the signature back as the gate does. The gate
  runs `gh attestation verify` on each report and requires an attestation of its exact bytes
  by that drill's workflow in `nxrobins/sigil-pi`, run from `main` on a GitHub-hosted runner,
  and checks that identity again in what gh printed; so `product-ci.sh` now needs `gh`,
  signed in, and the network. It checks the drill reports before the documents people write,
  and prints each attestation it accepted. The recovery report must also show a turn in every
  start, recovery measured to the first successful turn, and the online backup the drill now
  takes; the report committed from the v0.4.0 run shows none of them and is no longer
  evidence. Commit a drill report byte for byte as the run's artifact holds it.

- A release bundle lists what its compiler is made of, and pins that compiler for its startup
  check (audit 2026-11-10, F-16). The v0.4.0 SBOM named two components, while the compiler is
  built from a Cargo.lock of 338 packages, links SIGIL's Lean kernel with the Lean runtime, and
  loads the host's libz3. `scripts/build_release.py` now lists every package SIGIL's Cargo.lock
  resolves for sigil-mcp, less what its workspace crates use only in tests: 292 at the current
  pin, a superset of what one platform links (cargo resolves 266 of them for this build, across
  all targets), each with its version and, from crates.io, its purl and the lock's SHA-256. It adds Z3, the Lean
  toolchain and the Rust toolchain, each naming the file its version was read from: the
  `z3_version.h` beside `Z3_SYS_Z3_HEADER`, and SIGIL's `proofs/lean/lean-toolchain` and
  `rust-toolchain.toml` at the pinned ref. The bundle now carries `app/SIGIL_REV` with the
  `sha256_<platform>` digest of its compiler. `toolchain.verify_binary`, which runs before every
  compiler start and replacement, found no pin in any earlier bundle and checked nothing; one
  changed byte in `runtime/target/release/sigil-mcp` now stops startup with
  `configuration error: forge binary does not match SIGIL_REV`. The build refuses a SIGIL
  checkout whose Cargo.lock, Cargo.toml, rust-toolchain.toml or proofs/lean differ from the
  pinned ref's (SIGIL_REV's tree pin covers only crates/ and stdlib/), compiles with
  `cargo build --locked`, and stops before compiling when it cannot name the Z3 it builds
  against. `--verify` refuses a candidate whose pin is not the compiler it carries; v0.4.0,
  which carries no pin, verifies as before, and its startup still checks no digest. The
  release workflow's gate, build and publish jobs stop after 90, 45 and 15 minutes, and its
  gate installs the lock with `--require-hashes`.

- Several test-suite pins held less than they said (audit 2026-11-10, F-17). The audit
  chain's property tests now mutate every field of a record `AuditLog` really wrote, and
  assert that each mutation changed the record: the hand-written field list named a key no
  record has, whose examples returned early, and missed four keys every record has.
  `write_file` is given escaping paths on the real compiler (`..`, another session's
  sandbox, absolute paths, and symlinks to a file and a directory outside). The
  failure-injection matrix binds `network_failures` and `unavailable_model_providers` to
  tests that fail the model provider behind the real product socket, agent and compiler, and
  its check runs every bound test and requires each to pass, where it only collected them
  and so accepted a skipped or failing one. The tool-test matrix says what each column tests,
  and its check keeps that true. The credential guards decode the tools' byte stores before
  scanning, with a fixture that proves the scan fires; the session-path guard reads the
  function its message names, `SessionStore._path` rather than `AuditLog._path`, and now
  checks both. The deadline loop on the cross-worker session lock is tested against another
  worker process. A test run outside `ci.sh` puts its own interpreter first on PATH, so the
  bundles it boots start. Test-only; no product behaviour changes.
- A tenant's schedule messages count against a byte quota, `PI_MAX_SCHEDULE_BYTES_PER_TENANT`
  (default 8 MiB), beside the quota on how many schedules it holds (audit 2026-11-10, F-27).
  They were charged to no quota, and the schedule store, one JSON document that every scheduled
  run reads and rewrites, keeps a control character as six bytes: one tenant's 32 schedules
  could add 48 MiB to it, and 20 tenants at 8 MiB each made a claim take over half a second. A
  message counts the bytes it takes in the store, escapes included. `POST /v1/schedules` refuses
  a schedule that would take its tenant past the quota with `409 schedule_quota_exceeded`
  ("tenant schedule byte quota reached"); a tenant already over it keeps its schedules and may
  replace one with a message no larger. The default holds 32 plain-text messages of 256 KiB,
  what a tenant could hold before. Not changed: a scheduled run still reads and rewrites the
  whole store, so its cost still grows with the number of tenants, now by at most the quota
  each; making it independent of them needs a new on-disk format for schedules. The scheduler's
  health report now judges a turn in flight by its age as reported, to the millisecond, so it no
  longer reports itself unhealthy with a progress age that is within its bound.

### Added

- A strict audit mode, `AuditLog(strict=True)`: an audit append that fails raises
  `AuditWriteError`, so the forge whose guest already ran fails, its result is withheld, and
  the turn ends with that error instead of a reply, including when the same fault also fails
  the session save. The research host's default is unchanged (the failure is printed and
  the turn continues).
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
