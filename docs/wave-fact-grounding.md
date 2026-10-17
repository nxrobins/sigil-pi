# Experimental Wave fact grounding in the research host

The `agent.py` research host can opt in with `PI_MEMORY_FACT_GROUNDING=1`.
Authenticated product `/v1` memory remains disabled. This is an experimental
integration with synthetic validation; production observations are unavailable.

## Reproducible dependencies

Use the SIGIL commit and `crates`/`stdlib` trees in `SIGIL_REV`, and the Wave
commit in `WAVE_REV`. These pins are reviewed together with their companion PRs.
Build SIGIL's `sigil-mcp` and `sigil-serve` with `--features solver`, and build
Wave's `wave-memory-sidecar`. A runtime without the advertised bounded HTTP
capability is refused; an older sidecar without the required safeguards is also
refused before messages can be recorded.

The published Wave pin is `57427623b9f203de7841193367399e52d233182b`
from [Wave PR #84](https://github.com/nxrobins/wave-agent/pull/84).
Its review, native executable tests, documentation and lint checks passed.
Required GitHub Linux/Windows CI confirmation for this exact commit remains
pending and blocks publication of this integration. If CI requires a new source
commit, update the pin and validate that revision before publication.

The SIGIL pin remains `63073a5c21e35c2bf7161c415eeed49b05717be0`
from [SIGIL PR #6](https://github.com/nxrobins/sigil/pull/6), whose required
GitHub CI passed. Full host validation, required Linux coverage and fresh real
SIGIL/Bonsai/BGE/Jev HTTP acceptance with isolated stores remain required against
the pinned dependencies; companion checks do not establish those results.

The real protocol test accepts a pinned local sidecar through
`PI_TEST_WAVE_SIDECAR`; an explicitly configured missing binary is a failure.
Wave is private, so public CI exercises the hermetic protocol double rather than
fetching its source. Full cross-repository validation is recorded against exact
commits in the companion PR evidence. Live providers are not required by CI.

## Connection and safeguards

Set `PI_MEMORY_SIDECAR` to that binary, `PI_MEMORY_EMBEDDING_URL` to the local
BGE endpoint, `PI_MEMORY_EMBEDDING_MODEL=bge-small-en-v1.5`, and
`PI_MEMORY_FACT_GROUNDING=1`. Supply `TYPESAFE_API_KEY` to the research host.
Normal text-model and state configuration still applies. Provider API keys,
`PI_SECRET_*`, `PI_AUTH_TOKEN` and `PI_AUDIT_KEY` are stripped from the sidecar's
environment; provider transport belongs to the host.

Only `fact_support` and `fact_revision` judgments go to Jev, through the existing
secret-injected SIGIL HTTP guest and signed audit chain. Extraction and chat use
the configured text model. Wave validates complete typed responses against their
original requests and keeps the native 0.8 proof floor, quarantine, temporal and
ownership checks, and retirement replay barriers. Provider scores are preserved;
weak or malformed answers cannot supply proof.

Grounded roots carry a `FACT_GROUNDING` marker. Reopening without grounding is
refused. The inspector separates current facts, pending quarantine, review
evidence, retirement barriers and learning receipts. Raw experience history is
not a list of current facts.

## Replies and receipts

A new user message is recorded before reply generation. `memory_receipt` in JSON
and SSE distinguishes `awaiting_review` (`recorded=true`, `fact_verified=false`)
from buffered, failed and unavailable delivery. A buffer is not durable storage.
A reply-provider failure does not discard a correction already stored as evidence.

For recognized conversational memory updates, acknowledgements are rendered from
that receipt. They cannot claim verification, withdrawal or reinstatement before
review. The delivered text is also saved in the conversation and assistant-action
history. Complete assertions such as `Nadia now lives in Bergen.` use this same
receipt path. Messages mixing updates with other work, and tool turns, retain
the ordinary reply path.
The receipt recognizer also accepts complete paragraphs made only of supported
residence-record and provenance clauses, up to 8 KiB of UTF-8. Periods and
semicolons separate clauses; at least one clause must name a residence assertion,
retraction, original-record confirmation, or resolved residence uncertainty.
Every clause must match. An added question, tool request, historical query,
conditional or fictional framing, or unrelated statement keeps the entire message on the ordinary
path. This is a finite grammar, not general paragraph understanding.

Supported provenance clauses describe a verified current residence record, an
obsolete earlier residence and its asserted replacement, unreliable earlier
confirmation, an unverified residence pending an original-record check, no other
confirmed residence, a mistaken uncertainty notice, confirmation of the original
record rather than a move, explicit withdrawal, or a fresh assertion after an
earlier retraction rather than replay. For example:

> Maren Doss's residence uncertainty is resolved. I checked the original record and confirmed it was correct: Maren Doss lives in Tidecross today. The uncertainty notice was mistaken; this verifies the original record, not a move.

> Verified NEW update after the withdrawal: Maren Doss now lives in Tidecross again. This is a fresh confirmed residence assertion dated after the earlier retraction, not a replay of the old record.

These messages receive the same saved, buffered, failed, or unconfirmed receipt
as a short update. The words “verified” and “confirmed” in user input do not make
them verified facts. The host does not resolve ownership, check consistency,
distinguish restoration from fresh admission, or change inspector state here;
those decisions remain with Wave and its fact-only judgments.

Pending evidence is flushed before the final acknowledgement is rendered and
recorded as an assistant action. Each pending record owns its receipt, so a flush
by another session can confirm that record without replacing the other session's
receipt. Delivery snapshots the final user receipt; subsequent flushes cannot
rewrite an already delivered acknowledgement or JSON/SSE receipt.

Explicit current-residence questions naming one known subject use a fresh
inspector read immediately before delivery. Quarantine and missing current records
produce an unresolved answer. The initial snapshot establishes only the owner for
this turn; it never supplies cached facts for final delivery. If initial retrieval
or inspection is unavailable, final inspection retries owner qualification for
an explicit current-residence question. If that inspection is also unavailable,
the reply reports that current memory cannot be verified. A previously qualified
owner also receives an unavailable answer when final inspection fails. Unrecognized
questions avoid inspection, and successfully established unknown or ambiguous
owners retain the ordinary reply path.
Negation and historical scope are preserved. This bounded renderer does not guess
pronoun ownership or make general model-generated conversation deterministic.
Supported questions may end with the exact presentation directive
`? Answer in one sentence` (case insensitive, with an optional final period).
`Return only the current residence of NAME` is also supported, with an optional
final period. These forms require the whole utterance and an exact known owner;
additional tasks, historical qualifiers and arbitrary trailing clauses stay on
the ordinary path.

Final inspection, deterministic rendering and assistant-action recording hold
one memory protocol lock against host-triggered consolidation. The lock uses the
foreground short-lock limit and remaining turn budget. If consolidation is busy,
current answers report unavailable and pending evidence stays buffered. Provider
callbacks cannot run inside this finalization transaction. A later consolidation
may change the facts; the next turn reads a new inspector snapshot.

## Provider timing and completed learning

Host-authorized `http_timeout_ms` is an integer in 1..60000 and bounds the entire
caller operation across redirects and body reading. Network/secret authority,
redirect validation, body/worker limits and solver verification still apply.
Ordinary tools retain their existing short windows. Grounded chat/extraction and
fact support/revision get at most 30 seconds per call, further bounded by the
remaining whole-turn deadline. All turns start `PI_TURN_DEADLINE_S` at
entry, including session waits, and keep any stricter external or enclosing
deadline. Capability negotiation and chat, extraction and Jev retry waits share
that deadline; waits stop at expiry without another provider call, and late replies
fail with the recorded evidence receipt intact. The same absolute deadline bounds memory
locks, pending-record flushing, sidecar responses and local embedding callbacks.
MCP rejects exhausted caller budgets before writing or flushing a request, including
forge dispatch after capability negotiation and replacement initialization.
Serialization, nonblocking unbuffered pipe writes, readiness waits and complete
newline-framed response reads consume the same absolute request deadline.
The original monotonic expiry passes unchanged through the agent, supervisor,
replacement initialization and client; scheduling pauses and partial lines do not
renew it. The independent compiler and handshake watchdogs still distinguish
runtime failures from a caller exhausting its own budget. Timed-out or desynchronized compilers
are killed and reaped; subsequent operations may replace them without replay.
Research startup uses a supervised compiler: a failed operation is not replayed,
and the next operation can start a newly verified child within its remaining budget.

`committed` describes storage durability; `learning.complete` establishes that
required provider responses completed. Missing, empty and malformed responses are
counted. Incomplete grounded learning discards speculative changes, can commit
conservative quarantine and proven withdrawals, and retains the source frontier
for retry. The receipt and frontier survive reopening. The host's 64-callback cap
is unchanged; interrupted operations report an incomplete review and unconfirmed
storage commit.

## Correction and reinstatement

Complete source input, clocks and stable source IDs are retained. Assistant
receipts and generated summaries remain linked context rather than independent
user assertions. Withdrawal assertion, original temporal scope and continued
effectiveness are separate proofs, each retaining the confidence floor.

A credible unresolved correction holds the exact revision out of current retrieval.
A verified withdrawal resolves that hold even when no replacement is proposed.
Restoring the original record requires source-bound proof addressing the correction
and retains its provenance. A fresh assertion is reviewed as new evidence with a
new source revision. Retired evidence cannot replay into a current claim.

A resolved matching current revision retains its complete earlier review separately
from later input. Repeating an old correction inside fiction does not create a new
correction; a new real assertion remains reviewable. The boundary cannot release an
already-held record or use incomplete or mismatched provenance.

## Live evaluation

From the pinned Wave checkout, with Bonsai at `127.0.0.1:8082`, BGE at
`127.0.0.1:18083` and Jev credentials in the host environment:

```sh
SIGIL_ROOT=/path/to/pinned-sigil python3 /path/to/pinned-wave/scripts/eval_sigil_pi_grounding.py \
  --wave /path/to/pinned-wave --pi /path/to/sigil-pi \
  --output /path/to/new-isolated-evaluation --port 8873
```

The harness uses real HTTP chat, SIGIL forges, Bonsai extraction, BGE embeddings,
Jev and DreamCycle commits in fresh stores. It checks correction, uncertainty,
quarantine across restart, original-record restoration, fictional quotes, withdrawal,
fresh reinstatement and a second restart. Replies, receipts, retrieval, inspector
and persisted history are compared. Missing initial admission or incomplete
providers cannot count as successful lifecycle checks. No production store is opened.

The earlier local synthetic recovery passed 125 checks over 19 messages and two
restarts, eight live controls and four scripted fault cases. The isolated PR rerun
and exact commit evidence are linked from the companion PRs. Synthetic outcomes do
not estimate a production error rate or close the separate native Wave monitor
incident. General answer generation and production memory admission require their
own validation before rollout.
