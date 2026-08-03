"""M13 — the proof-carrying dispatch log.

Every guest execution in this host goes through ONE function (`_forge`), so
recording there makes gaps structurally impossible: there is no way to run a
guest without producing a record. Each record names the code that ran
(source hash), what it was permitted to touch (grants), and the data boundary
(input/output hashes) — and carries the hash of the record before it, so the
sequence cannot be edited after the fact without breaking.

Two properties carry the weight, and both are tested adversarially rather than
asserted:

  REDACTION  — grants for the LLM call literally contain the API key. Logging
               them verbatim would write the key to disk, turning the feature
               into its own worst bug. Secret VALUES are redacted; secret
               NAMES survive, because which secret was available is exactly
               what an auditor needs.
  INTEGRITY  — mutating ANY field of ANY record must break verification, with
               an anti-vacuity case proving the verifier still passes on an
               untouched log (so the suite cannot start reporting integrity it
               has stopped checking).
"""
import json
import sys

import pytest
from hypothesis import given, settings, strategies as st

from conftest import API_KEY, PI_ROOT, make_agent, msg, text, tool_use  # noqa: F401

sys.path.insert(0, str(PI_ROOT))

from agent import (  # noqa: E402
    GENESIS_HASH,
    AuditLog,
    entry_hash,
    redact_grants,
    verify_chain,
)


# ── redaction: the constraint that keeps the feature from being the bug ──


def test_secret_values_are_redacted_names_are_kept():
    """WHICH secret a forge could reach is the audit-relevant fact; the value
    is the thing that must never be written down."""
    out = redact_grants({"secret": ["anthropic=sk-ant-REAL", "github=ghp_REAL"],
                         "net": ["api.anthropic.com"]})
    assert out["secret"] == ["anthropic=<redacted>", "github=<redacted>"]
    assert out["net"] == ["api.anthropic.com"], "non-secret grants are the record"


def test_redaction_handles_odd_secret_shapes():
    """A grant with no '=' has no value half to redact, and must not be
    silently dropped (it would understate what the forge could reach)."""
    assert redact_grants({"secret": ["bare"]})["secret"] == ["bare=<redacted>"]
    assert redact_grants({"secret": []})["secret"] == []
    assert redact_grants(None) is None
    assert redact_grants({}) == {}


def test_redaction_never_returns_the_original_object():
    """The caller's grants dict is live state (it is also what gets forged);
    redaction must not mutate it."""
    grants = {"secret": ["anthropic=sk-ant-REAL"]}
    out = redact_grants(grants)
    assert grants["secret"] == ["anthropic=sk-ant-REAL"], "redaction mutated its input"
    assert out is not grants


@settings(max_examples=200, deadline=None)
@given(st.text(min_size=1, max_size=40), st.text(min_size=1, max_size=40))
def test_no_secret_value_survives_redaction(name, value):
    """Property: for ANY name/value, the value half is replaced outright and
    the name half is preserved. Asserted STRUCTURALLY rather than by searching
    the rendered JSON for the value — a substring search reports false leaks,
    because JSON-escaping a control character in the NAME (\\u001f) emits
    characters that a short value can coincidentally match. The invariant that
    actually matters has no such ambiguity: everything after the first '=' is
    exactly '<redacted>'."""
    [entry] = redact_grants({"secret": [f"{name}={value}"]})["secret"]
    head, sep, tail = entry.partition("=")
    assert sep == "=" and tail == "<redacted>", f"value survived in {entry!r}"
    # `name=value` splits at the FIRST '=', so a name containing one keeps
    # only its leading segment — correct for the format, and pinned here.
    assert head == name.split("=", 1)[0]


# ── the chain ────────────────────────────────────────────────────────────


def _mk(log, session, n, kind="tool:read_file"):
    for i in range(n):
        log.record(session=session, kind=kind, source=f"src{i}",
                   input_text=f"in{i}", output=f"out{i}", err=None,
                   grants={"fs": ["/sandbox"]}, fuel=20_000_000)


def test_records_form_a_verifiable_chain(tmp_path):
    log = AuditLog(tmp_path / "audit")
    _mk(log, "s1", 4)
    records = log.read("s1")
    assert len(records) == 4
    assert records[0]["prev_hash"] == GENESIS_HASH
    for a, b in zip(records, records[1:]):
        assert b["prev_hash"] == entry_hash(a)
    ok, problem = verify_chain(records)
    assert ok, problem


def test_empty_log_verifies(tmp_path):
    ok, problem = verify_chain(AuditLog(tmp_path / "audit").read("never-used"))
    assert ok, problem


def test_chain_continues_across_a_fresh_host(tmp_path):
    """Durability: a restart must EXTEND the chain, not start a new one —
    otherwise a crash silently splits the record into unlinked segments."""
    _mk(AuditLog(tmp_path / "audit"), "s1", 2)
    _mk(AuditLog(tmp_path / "audit"), "s1", 2)   # brand-new instance
    records = AuditLog(tmp_path / "audit").read("s1")
    assert len(records) == 4
    ok, problem = verify_chain(records)
    assert ok, problem
    assert [r["seq"] for r in records] == [0, 1, 2, 3]


def test_sessions_have_independent_chains(tmp_path):
    log = AuditLog(tmp_path / "audit")
    _mk(log, "alpha", 2)
    _mk(log, "beta", 2)
    for s in ("alpha", "beta"):
        rs = log.read(s)
        assert len(rs) == 2 and rs[0]["prev_hash"] == GENESIS_HASH
        assert verify_chain(rs)[0]


def test_raw_session_id_never_appears_in_a_filename(tmp_path):
    """Same discipline as SessionStore: the id is hashed into the path, so a
    traversal-shaped session id cannot escape the audit directory."""
    log = AuditLog(tmp_path / "audit")
    _mk(log, "../../etc/passwd", 1)
    files = list((tmp_path / "audit").iterdir())
    assert len(files) == 1
    assert "passwd" not in files[0].name and ".." not in files[0].name


# ── tamper-evidence, proven both ways ────────────────────────────────────


RECORD_FIELDS = ["kind", "source_sha256", "input_sha256", "output_sha256",
                 "grants", "fuel", "seq", "prev_hash", "session"]


@settings(max_examples=60, deadline=None)
@given(st.integers(min_value=0, max_value=3),
       st.sampled_from(RECORD_FIELDS))
def test_mutating_any_field_of_any_record_breaks_the_chain(tmp_path_factory, idx, field):
    """THE integrity property. Every field is covered, not just the payload:
    editing `grants` to understate what a forge could reach must be as
    detectable as editing the output."""
    log = AuditLog(tmp_path_factory.mktemp("audit"))
    _mk(log, "s1", 4)
    records = log.read("s1")
    assert verify_chain(records)[0], "anti-vacuity: the untouched chain must verify"

    if field not in records[idx]:
        return
    records[idx][field] = "TAMPERED"
    ok, problem = verify_chain(records)
    # The final record has nothing after it to link against — a documented
    # boundary of any hash chain (see the module docstring in agent.py), so
    # tampering there is only detectable against an externally-held head.
    if idx == len(records) - 1 and field != "prev_hash":
        return
    assert not ok, f"tampering with {field!r} at index {idx} went undetected"
    assert problem


def test_deleting_a_record_from_the_middle_breaks_the_chain(tmp_path):
    log = AuditLog(tmp_path / "audit")
    _mk(log, "s1", 4)
    records = log.read("s1")
    del records[1]
    ok, _ = verify_chain(records)
    assert not ok, "a removed record went undetected"


def test_reordering_records_breaks_the_chain(tmp_path):
    log = AuditLog(tmp_path / "audit")
    _mk(log, "s1", 4)
    records = log.read("s1")
    records[1], records[2] = records[2], records[1]
    assert not verify_chain(records)[0], "reordering went undetected"


def test_appending_does_not_parse_the_whole_log(tmp_path):
    """SWEEP: `record` originally re-read and re-parsed the ENTIRE log to find
    the previous hash, making each append O(n) and a session O(n²) — measured
    at 0.13ms/record at n=50 rising to 1.02ms at n=800. That bites hardest on
    exactly the long sessions an audit log is most valuable for.

    Asserted structurally rather than by timing: appending must not go through
    the full-parse path at all. A timing assertion would be flaky; this one
    states the actual invariant."""
    log = AuditLog(tmp_path / "audit")
    _mk(log, "s1", 40)

    def boom(*a, **k):
        raise AssertionError(
            "record() parsed the whole log to append — that is the O(n²) bug")

    real_read, log.read = log.read, boom
    try:
        log.record(session="s1", kind="tool:x", source="s", input_text="i",
                   output="o", err=None, grants=None, fuel=1)
    finally:
        log.read = real_read
    records = log.read("s1")
    assert len(records) == 41
    ok, problem = verify_chain(records)
    assert ok, problem


def test_appending_onto_a_torn_tail_is_refused(tmp_path):
    """SWEEP: with the tail read directly, a torn final line must REFUSE the
    append rather than chain a new record onto garbage — that would bury the
    truncation under valid-looking links."""
    log = AuditLog(tmp_path / "audit")
    _mk(log, "s1", 2)
    p = log._path("s1")
    p.write_text(p.read_text() + '{"seq": 2, "kind": "tor')
    with pytest.raises(ValueError, match="torn|truncated"):
        log.append_or_raise(session="s1", kind="tool:x", source="s",
                            input_text="i", output="o", err=None,
                            grants=None, fuel=1)


def test_a_torn_final_line_is_reported_not_ignored(tmp_path):
    """Append-only means a crash mid-write can leave a partial last line.
    That must be REPORTED — everything before it is intact and still
    trustworthy, but silently dropping it would hide a truncation."""
    log = AuditLog(tmp_path / "audit")
    _mk(log, "s1", 3)
    p = log._path("s1")
    p.write_text(p.read_text() + '{"seq": 3, "kind": "tool:tr')
    with pytest.raises(ValueError, match="truncated|torn|line 4"):
        log.read("s1")


# ── signing: from tamper-EVIDENT to tamper-PROOF against a writer ────────
#
# M13's chain proves internal consistency: it catches a careless edit, but
# anyone who can write the file can also recompute every downstream link and
# produce a chain that verifies. Signing each record with a key held OUTSIDE
# the audit directory closes that: rewriting now requires the key too.
#
# HONEST BOUNDARY, pinned below: HMAC is symmetric, so whoever can VERIFY can
# also FORGE. This defends the record against someone who reaches the storage
# — a compromised backup, a tampering process, an operator covering tracks
# after the fact — but NOT against the host at the moment of writing. Nothing
# short of asymmetric signing (or an external notary) can.


KEY = b"audit-signing-key-not-in-the-audit-dir"
OTHER_KEY = b"a-different-key-entirely"


def test_signed_records_carry_a_signature(tmp_path):
    log = AuditLog(tmp_path / "audit", key=KEY)
    _mk(log, "s1", 3)
    records = log.read("s1")
    assert all(r.get("sig") for r in records)
    ok, problem = verify_chain(records, key=KEY)
    assert ok, problem


def test_verification_with_the_wrong_key_fails(tmp_path):
    log = AuditLog(tmp_path / "audit", key=KEY)
    _mk(log, "s1", 3)
    ok, problem = verify_chain(log.read("s1"), key=OTHER_KEY)
    assert not ok and "signature" in problem.lower()


def test_a_coherently_rewritten_chain_is_caught_by_the_signature(tmp_path):
    """THE point of signing. An attacker who edits a record AND recomputes
    every downstream prev_hash produces a chain that passes the unsigned
    check — this is exactly what M13 documented as its remaining gap. With a
    key they do not hold, the forgery is detected."""
    log = AuditLog(tmp_path / "audit", key=KEY)
    _mk(log, "s1", 4)
    records = log.read("s1")

    # a competent forgery: change a grant, then re-link everything after it
    records[1]["grants"] = {"fs": ["/etc"]}
    prev = entry_hash(records[0])
    for r in records[1:]:
        r["prev_hash"] = prev
        prev = entry_hash(r)

    assert verify_chain(records)[0], \
        "anti-vacuity: the rewrite must defeat the UNSIGNED check, or this " \
        "test proves nothing about what signing adds"
    ok, problem = verify_chain(records, key=KEY)
    assert not ok and "signature" in problem.lower()


def test_signed_log_still_verifies_unsigned_for_structure(tmp_path):
    """A verifier without the key can still check ORDER and LINKAGE — useful
    for a third party who has the log but not the secret."""
    log = AuditLog(tmp_path / "audit", key=KEY)
    _mk(log, "s1", 3)
    assert verify_chain(log.read("s1"))[0]


def test_unsigned_log_is_reported_when_a_key_is_expected(tmp_path):
    """If the operator verifies WITH a key, an unsigned record must not pass
    quietly — that is how a stripped signature would hide."""
    log = AuditLog(tmp_path / "audit")          # no key: unsigned
    _mk(log, "s1", 2)
    ok, problem = verify_chain(log.read("s1"), key=KEY)
    assert not ok and "unsigned" in problem.lower()


def test_stripping_a_signature_is_detected(tmp_path):
    log = AuditLog(tmp_path / "audit", key=KEY)
    _mk(log, "s1", 3)
    records = log.read("s1")
    del records[1]["sig"]
    ok, problem = verify_chain(records, key=KEY)
    assert not ok and ("unsigned" in problem.lower() or "signature" in problem.lower())


@settings(max_examples=60, deadline=None)
@given(st.integers(min_value=0, max_value=3), st.sampled_from(RECORD_FIELDS))
def test_signing_covers_every_field(tmp_path_factory, idx, field):
    """Property: with the chain re-linked after the edit (a competent
    forgery), the signature must still catch a change to ANY field — a
    signature over a subset would leave a silently-editable region."""
    log = AuditLog(tmp_path_factory.mktemp("audit"), key=KEY)
    _mk(log, "s1", 4)
    records = log.read("s1")
    if field not in records[idx]:
        return
    records[idx][field] = "TAMPERED"
    if field != "prev_hash":
        # Re-link so the LINKAGE check cannot be what objects — leaving only
        # the signature to catch it. Skipped when the mutated field IS
        # prev_hash, because re-linking would overwrite the mutation and
        # restore the record byte-identically: there would be nothing left to
        # detect. That case is covered by the unsigned chain test above.
        prev = GENESIS_HASH
        for r in records:
            r["prev_hash"] = prev
            prev = entry_hash(r)
    ok, _ = verify_chain(records, key=KEY)
    assert not ok, f"signature did not cover {field!r}"


def test_the_signing_key_never_lands_in_the_audit_directory(tmp_path):
    """A key stored beside the records it signs protects nothing. Pinned as a
    property of the writer, not merely documented."""
    log = AuditLog(tmp_path / "audit", key=b"SUPER-SECRET-SIGNING-KEY")
    _mk(log, "s1", 3)
    blob = b"".join(p.read_bytes() for p in (tmp_path / "audit").rglob("*"))
    assert b"SUPER-SECRET-SIGNING-KEY" not in blob


# ── the verifier: a log nobody can check is decoration ───────────────────


def test_verify_audit_dir_checks_every_chain_and_names_the_broken_one(tmp_path):
    from agent import verify_audit_dir
    log = AuditLog(tmp_path / "audit")
    _mk(log, "good", 3)
    _mk(log, "tampered", 3)

    p = log._path("tampered")
    lines = p.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["grants"] = {"fs": ["/etc"]}          # understate what a forge reached
    lines[1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    p.write_text("\n".join(lines) + "\n")

    report = verify_audit_dir(tmp_path / "audit")
    assert report["ok"] is False
    assert report["chains"] == 2
    assert report["records"] == 6
    [(name, problem)] = report["problems"]
    assert name == p.name and problem


def test_verify_audit_dir_passes_on_clean_logs(tmp_path):
    """Anti-vacuity for the verifier itself: it must be capable of saying yes,
    or 'no problems found' means nothing."""
    from agent import verify_audit_dir
    log = AuditLog(tmp_path / "audit")
    _mk(log, "a", 3)
    _mk(log, "b", 2)
    report = verify_audit_dir(tmp_path / "audit")
    assert report == {"ok": True, "chains": 2, "records": 5, "problems": []}


def test_verify_audit_dir_on_a_missing_directory_is_empty_not_an_error(tmp_path):
    from agent import verify_audit_dir
    assert verify_audit_dir(tmp_path / "nope")["chains"] == 0


def test_verify_audit_dir_reports_a_torn_file_rather_than_crashing(tmp_path):
    """A torn tail must be a REPORTED problem, not an exception that aborts
    verification of the other chains."""
    from agent import verify_audit_dir
    log = AuditLog(tmp_path / "audit")
    _mk(log, "fine", 2)
    _mk(log, "torn", 2)
    p = log._path("torn")
    p.write_text(p.read_text() + '{"seq": 2, "ki')
    report = verify_audit_dir(tmp_path / "audit")
    assert report["ok"] is False
    assert len(report["problems"]) == 1
    assert "torn" in report["problems"][0][1] or "truncated" in report["problems"][0][1]


def test_env_flag_parsing(monkeypatch):
    from agent import _env_flag
    monkeypatch.delenv("PI_TEST_FLAG", raising=False)
    assert _env_flag("PI_TEST_FLAG", True) is True
    for off in ("0", "false", "FALSE", "no", "off", ""):
        monkeypatch.setenv("PI_TEST_FLAG", off)
        assert _env_flag("PI_TEST_FLAG", True) is False, f"{off!r} should disable"
    for on in ("1", "true", "yes", "on"):
        monkeypatch.setenv("PI_TEST_FLAG", on)
        assert _env_flag("PI_TEST_FLAG", False) is True, f"{on!r} should enable"


# ── wired into the loop: completeness ────────────────────────────────────


def test_every_guest_execution_is_recorded(scripted_llm, tmp_path, mcp):
    """A turn with one tool call runs FOUR guests — llm, parse, the tool, and
    its shape stage. All four must appear, in order. This is the property the
    single-chokepoint design exists to make automatic."""
    agent = make_agent(scripted_llm, tmp_path, mcp)
    (agent.sandbox_for("s1") / "f.txt").write_text("hi")
    scripted_llm.script = [
        msg([tool_use("t", "read_file", {"path": "f.txt"})]),
        msg([text("done")]),
    ]
    assert agent.turn("s1", "read it") == "done"
    records = agent.audit.read("s1")
    kinds = [r["kind"] for r in records]
    assert kinds == ["llm", "parse", "tool:read_file",
                     "llm", "parse"], kinds
    assert verify_chain(records)[0]


def test_recorded_grants_match_the_minimality_log(scripted_llm, tmp_path, mcp):
    """The audit log and grant_log must agree about what each tool was
    permitted — two independent records of the same fact."""
    agent = make_agent(scripted_llm, tmp_path, mcp)
    sb = agent.sandbox_for("s1")
    (sb / "f.txt").write_text("hi")
    scripted_llm.script = [
        msg([tool_use("t", "read_file", {"path": "f.txt"})]),
        msg([text("done")]),
    ]
    agent.turn("s1", "go")
    tool_records = [r for r in agent.audit.read("s1")
                    if r["kind"].startswith("tool:")]
    from_audit = [(r["kind"].split(":", 1)[1], r["grants"]) for r in tool_records]
    from_grant_log = [(name, redact_grants(grants))
                      for name, grants in agent.grant_log
                      if not name.endswith(".shape")]
    assert from_audit == from_grant_log


def test_a_failed_forge_is_recorded_with_its_error(scripted_llm, tmp_path, mcp):
    """Denials are the most audit-relevant events of all — a -403 means the
    language refused something. It must be IN the record, not missing from it."""
    agent = make_agent(scripted_llm, tmp_path, mcp)
    scripted_llm.script = [
        msg([tool_use("t", "read_file", {"path": "../escape.txt"})]),
        msg([text("blocked")]),
    ]
    agent.turn("s1", "climb out")
    denied = [r for r in agent.audit.read("s1") if r["kind"] == "tool:read_file"]
    assert len(denied) == 1
    assert denied[0]["output_sha256"] is None
    assert "403" in denied[0]["error"] or "404" in denied[0]["error"]


def test_the_api_key_never_reaches_the_audit_log(scripted_llm, tmp_path, mcp):
    """THE canary. The LLM forge's grants literally contain the key, so this
    is the exact path by which the feature could become a key-disclosure bug.
    Nothing in any audit file may contain it — mirrors the leak canary in
    tests/test_net_grant.py."""
    agent = make_agent(scripted_llm, tmp_path, mcp)
    scripted_llm.script = [msg([text("hi")])]
    agent.turn("s1", "hello")
    blob = b"\n".join(p.read_bytes()
                      for p in (tmp_path / "audit").rglob("*.jsonl"))
    assert blob, "no audit output to check — the canary would pass vacuously"
    assert API_KEY.encode() not in blob, "THE API KEY IS IN THE AUDIT LOG"
    # ...and the record still says WHICH secret was reachable
    llm = [r for r in agent.audit.read("s1") if r["kind"] == "llm"][0]
    assert llm["grants"]["secret"] == ["anthropic=<redacted>"]


def test_audit_can_be_disabled(scripted_llm, tmp_path, mcp):
    agent = make_agent(scripted_llm, tmp_path, mcp)
    agent.audit = AuditLog(tmp_path / "audit-off", enabled=False)
    scripted_llm.script = [msg([text("hi")])]
    agent.turn("s1", "hello")
    assert agent.audit.read("s1") == []
    assert not (tmp_path / "audit-off").exists() or \
        not list((tmp_path / "audit-off").glob("*.jsonl"))
