"""Strict audit mode: an append failure fails the forge.

Every guest execution goes through `PiAgent._forge`, which writes its record
AFTER the guest has run. The default log (`AuditLog.record`, the research
host's contract) catches an append failure, prints it and lets the turn go
on, so a full disk or an I/O error leaves executions with no record. The
chain that remains is a valid prefix and still verifies, so the gap can
never be found afterwards. Found by the 2026-11-10 product audit (F-04),
whose reproduction drove the product API with an unwritable chain: HTTP
200, two guests run, zero records, two tracebacks on stderr.

A STRICT log raises `AuditWriteError` instead. Pinned here, against the real
`PiAgent` over a scripted compiler double (the agent loop is under test, so
no toolchain is needed except for the one end-to-end check):

- a strict append failure fails the forge and the turn, the result is
  withheld, and no guest runs after it: the property test walks every forge
  kind and every position in a turn;
- the failure reaches the caller AS `AuditWriteError`, even when the fault
  behind it also fails the turn's session save, and even when the compiler
  had already raised or the deadline had passed;
- the default is unchanged, and its swallow branch is now tested;
- by syntax: the compiler is reached only through `_forge`, `_forge` never
  wraps a record in a handler, and every forge path names its session.
"""
import ast
import errno
import hashlib
import itertools
import json
import os
import re
import time
import types
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from conftest import PI_ROOT, make_agent, msg, text

import agent as agent_module
from agent import AuditLog, AuditWriteError, PiAgent, SessionStore, verify_chain

KEY = b"strict-audit-test-key-0123456789abcdef"
SESSION = "s1"

# The probe tool and its shape stage are answered by the double and never
# compiled. The shape must not `use sigil::json;`, or dispatch would compose
# it against the stdlib and the test would need a toolchain.
TOOL_SRC = "// probe tool: answered by the compiler double, never compiled\n"
SHAPE_SRC = "// probe shape stage: answered by the compiler double, never compiled\n"
AUDIT_KIND = {"LLM": "llm", "PARSE": "parse",
              TOOL_SRC: "tool:probe", SHAPE_SRC: "shape:probe"}


def frame(tag, payload):
    """One bridge frame, as parse_reply emits it (see agent.decode_frames)."""
    return f"{tag}{len(payload.encode()):08d}{payload}"


class Compiler:
    """A compiler double for the REAL PiAgent. It answers each guest by its
    source and logs every execution under the audit kind `_forge` records it
    as, so a test can count the guests that RAN against the records that were
    WRITTEN. The first `tool_steps` parse replies ask for the probe tool; the
    next one answers with text. `fault(kind)` returning True makes that
    guest raise, as a crashed or killed compiler does."""

    def __init__(self, tool_steps=0, fault=None):
        self.ran = []
        self.tool_steps = tool_steps
        self.parses = 0
        self.fault = fault

    def forge(self, source, *, input="", fuel=0, grants=None, timeout_s=None):
        kind = AUDIT_KIND[source]
        self.ran.append(kind)
        if self.fault is not None and self.fault(kind):
            raise RuntimeError("compiler crashed mid-forge")
        if kind == "parse":
            self.parses += 1
            if self.parses <= self.tool_steps:
                out = frame("u", f"tu{self.parses}\x1fprobe\x1f" + '{"arg":"x"}')
            else:
                out = frame("t", "done")
            out += frame("g", "1|1")
        else:
            out = {"llm": "{}", "tool:probe": "raw", "shape:probe": "shaped"}[kind]
        return {"status": "ok", "data": {"output_text": out}}


def build(root, compiler, *, strict=True):
    """A real PiAgent over `compiler`, with a one-tool manifest whose tool has
    a shape stage, so a turn exercises all four forge kinds."""
    root = Path(root)
    tools = root / "probe-tools"
    tools.mkdir(parents=True)
    (tools / "probe.sigil").write_text(TOOL_SRC)
    (tools / "probe_shape.sigil").write_text(SHAPE_SRC)
    (root / "manifest.json").write_text(json.dumps({"probe": {
        # absolute paths: dispatch joins them onto PI_ROOT, which keeps them
        "source": str(tools / "probe.sigil"),
        "shape": str(tools / "probe_shape.sigil"),
        "args": ["arg"], "path_args": [], "grants": {},
        "spec": {"name": "probe", "description": "probe",
                 "input_schema": {"type": "object",
                                  "properties": {"arg": {"type": "string"}},
                                  "required": ["arg"]}},
    }}))
    log = AuditLog(root / "audit", key=KEY, strict=strict)
    host = PiAgent("https://example.invalid/v1/messages", "sk-test-not-a-real-key",
                   store=SessionStore(root / "sessions"),
                   sandbox_root=root / "sandboxes",
                   manifest_path=root / "manifest.json", mcp=compiler, audit=log)
    # Composed sources are inputs to forging; the double ignores them.
    host._llm_src, host._parse_src = "LLM", "PARSE"
    return host, log


def fail_append_number(log, n):
    """The n-th append from now on (0-based) fails as a full disk does,
    before any byte is written. Every other append is real."""
    real = log.append_or_raise
    calls = itertools.count()

    def append(*args, **kwargs):
        if next(calls) == n:
            raise OSError(errno.ENOSPC, "No space left on device (injected)")
        return real(*args, **kwargs)

    log.append_or_raise = append


def is_session_scratch(path):
    """The scratch file a session save creates (agent.write_durably)."""
    return re.fullmatch(r"\.[0-9a-f]{64}\.kv\.[0-9a-f]{16}\.tmp",
                        Path(os.fsdecode(path)).name) is not None


def full_disk(monkeypatch, *, sessions_too=False):
    """Writes fail with ENOSPC; reads still work. Audit appends always fail;
    with `sessions_too`, so does the session store's atomic save, where it
    creates its scratch file, before any byte is written."""
    real_open, real_os_open = Path.open, os.open

    def open_(path, mode="r", *args, **kwargs):
        if "a" in mode and path.suffix == ".jsonl":
            raise OSError(errno.ENOSPC, "No space left on device (injected)")
        return real_open(path, mode, *args, **kwargs)

    def os_open(path, flags, *args, **kwargs):
        if sessions_too and flags & os.O_CREAT and is_session_scratch(path):
            raise OSError(errno.ENOSPC, "No space left on device (injected)")
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_)
    monkeypatch.setattr(os, "open", os_open)


def state_of(path):
    """What is at the chain's path, for a before/after comparison."""
    if path.is_dir():
        return ("directory", sorted(p.name for p in path.iterdir()))
    return ("file", path.read_bytes())


def exception_chain(error):
    seen = []
    while error is not None and error not in seen:
        seen.append(error)
        error = error.__cause__ or error.__context__
    return seen


# ── the log itself ───────────────────────────────────────────────────────


def test_strict_record_raises_a_content_free_error_and_writes_nothing(
        tmp_path, monkeypatch, capsys):
    """The unit contract. The message is fixed text: the research host
    writes a RuntimeError's message to its client, so it must carry no path,
    no session hash and no OS error text. The OS error stays reachable as
    the cause, for the operator."""
    log = AuditLog(tmp_path / "audit", key=KEY, strict=True)
    log.record(SESSION, "llm", "src", "in", "out", None, None, 1)
    chain = log._path(SESSION)
    before = chain.read_bytes()
    full_disk(monkeypatch)
    with pytest.raises(AuditWriteError) as raised:
        log.record(SESSION, "parse", "src", "in", "out", None, None, 1)
    assert isinstance(raised.value.__cause__, OSError)
    assert raised.value.__cause__.errno == errno.ENOSPC
    message = str(raised.value)
    for leak in (str(tmp_path), chain.name,
                 hashlib.sha256(SESSION.encode()).hexdigest()[:16],
                 "No space", "Errno"):
        assert leak not in message, f"{leak!r} leaked into {message!r}"
    assert chain.read_bytes() == before, "a failed append must write nothing"
    assert capsys.readouterr().err == "", \
        "strict mode raises; printing as well would be a second, uncontrolled log"


def test_default_record_prints_an_append_failure_and_returns(
        tmp_path, monkeypatch, capsys):
    """THE SWALLOW BRANCH, which had no test until now. It is the research
    host's contract and stays: never raise into the turn, be loud on stderr."""
    log = AuditLog(tmp_path / "audit", key=KEY)
    log.record(SESSION, "llm", "src", "in", "out", None, None, 1)
    before = log._path(SESSION).read_bytes()
    full_disk(monkeypatch)
    assert log.record(SESSION, "parse", "src", "in", "out", None, None, 1) is None
    err = capsys.readouterr().err
    assert "Traceback" in err and "No space left on device" in err
    assert log._path(SESSION).read_bytes() == before


def test_strict_mode_is_opt_in_and_the_research_host_keeps_its_default(tmp_path):
    """The brief keeps the research host's default: a fresh log, the log a
    PiAgent builds for itself, and the one the research main() builds are
    all non-strict. Making the research host strict is a deliberate edit to
    this test, and to the README's account of the gap."""
    assert AuditLog(tmp_path / "audit").strict is False
    host = PiAgent("https://example.invalid/v1/messages", "unused",
                   store=SessionStore(tmp_path / "sessions"),
                   sandbox_root=tmp_path / "sandboxes", mcp=None)
    assert host.audit.strict is False
    tree = ast.parse((PI_ROOT / "agent.py").read_text())
    main = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "main")
    built = [n for n in ast.walk(main) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "AuditLog"]
    assert len(built) == 1, "the research main() should build exactly one AuditLog"
    assert "strict" not in {k.arg for k in built[0].keywords}


def test_a_strict_log_must_be_enabled(tmp_path):
    """A disabled log records nothing, so 'strict' would promise a record for
    every forge and deliver none. Refused where it is configured."""
    with pytest.raises(ValueError, match="strict"):
        AuditLog(tmp_path / "audit", enabled=False, strict=True)


# ── the agent: a strict failure fails the forge and the turn ─────────────


def _tear_the_tail(chain):
    chain.write_bytes(chain.read_bytes() + b'{"seq": 2, "ki')


def _replace_with_a_directory(chain):
    chain.unlink()
    chain.mkdir()


@pytest.mark.parametrize("fault", ["full_disk", "torn_tail", "not_a_file"])
def test_strict_append_failure_fails_the_turn_and_withholds_the_reply(
        tmp_path, monkeypatch, capsys, fault):
    """R5a at the agent boundary, with three real faults: ENOSPC at the
    append (a full disk), a torn final record (the log refuses to chain onto
    it), and the chain path not being a file (an OS error any user gets,
    root included). The LLM guest runs, its record cannot be written, and
    the turn raises instead of answering: the parse guest never runs and
    nothing is appended. Before strict mode the turn returned its reply."""
    compiler = Compiler()
    host, log = build(tmp_path, compiler)
    assert host.turn(SESSION, "one") == "done"
    chain = log._path(SESSION)
    if fault == "full_disk":
        full_disk(monkeypatch)
    elif fault == "torn_tail":
        _tear_the_tail(chain)
    else:
        _replace_with_a_directory(chain)
    before = state_of(chain)
    ran = len(compiler.ran)
    with pytest.raises(AuditWriteError):
        host.turn(SESSION, "two")
    assert compiler.ran[ran:] == ["llm"], \
        "exactly one guest is unrecorded, and nothing runs after it"
    assert state_of(chain) == before, "nothing may be appended"
    assert "Traceback" not in capsys.readouterr().err


def test_the_default_log_lets_the_turn_succeed_with_a_gap(
        tmp_path, monkeypatch, capsys):
    """The research default, end to end: this is the gap itself, kept on
    purpose and pinned so that the README's account of it has a test behind
    it. The turn answers, both guests ran, neither has a record, and the only
    trace is one traceback per failed append."""
    compiler = Compiler()
    host, log = build(tmp_path, compiler, strict=False)
    assert host.turn(SESSION, "one") == "done"
    before = log._path(SESSION).read_bytes()
    full_disk(monkeypatch)
    ran = len(compiler.ran)
    assert host.turn(SESSION, "two") == "done"
    assert compiler.ran[ran:] == ["llm", "parse"]
    assert log._path(SESSION).read_bytes() == before
    assert capsys.readouterr().err.count("Traceback") == 2


@settings(max_examples=80, deadline=None)
@given(tool_steps=st.integers(min_value=0, max_value=3), data=st.data())
def test_no_guest_runs_unrecorded_unless_its_turn_fails(tmp_path_factory, tool_steps, data):
    """THE GUARD for this defect class. For any turn shape (0 to 3 tool steps,
    each with a shape stage, so every forge kind appears) and any append in
    it failing, a strict log gives exactly this: the turn raises
    AuditWriteError; every guest before the failure has its record; the guest
    whose append failed is the only unrecorded one and the LAST to run; and
    the chain left behind verifies. With no failure, every guest has its
    record. A swallow anywhere on the path, in the log, in _forge, in
    dispatch or in the loop, breaks one of these counts."""
    forges = 4 * tool_steps + 2   # llm+parse per step; tool+shape per tool step
    fail_at = data.draw(st.one_of(st.none(), st.integers(0, forges - 1)),
                        label="fail_at")
    compiler = Compiler(tool_steps=tool_steps)
    host, log = build(tmp_path_factory.mktemp("strict"), compiler)
    if fail_at is None:
        assert host.turn(SESSION, "go") == "done"
    else:
        fail_append_number(log, fail_at)
        with pytest.raises(AuditWriteError) as raised:
            host.turn(SESSION, "go")
        assert raised.value.__cause__.errno == errno.ENOSPC
    records = log.read(SESSION)
    if fail_at is None:
        assert len(compiler.ran) == len(records) == forges
    else:
        assert len(records) == fail_at
        assert len(compiler.ran) == fail_at + 1
    assert [r["kind"] for r in records] == compiler.ran[:len(records)]
    assert verify_chain(records, key=KEY) == (True, None)


@pytest.mark.parametrize("append_fails", [False, True])
def test_a_strict_failure_outranks_the_compilers_own_error(tmp_path, append_fails):
    """When the guest itself raised, _forge records the failure and then
    re-raises. If that record fails too, the caller must see the AUDIT
    failure: the compiler's error alone would read as an ordinary agent
    failure and hide that an execution went unrecorded. The compiler's error
    stays in the exception chain. The control (append_fails=False) shows the
    compiler's own error still propagating, recorded, when the record works."""
    compiler = Compiler(fault=lambda kind: kind == "llm")
    host, log = build(tmp_path, compiler)
    if append_fails:
        fail_append_number(log, 0)
        with pytest.raises(AuditWriteError) as raised:
            host.turn(SESSION, "go")
        chain = exception_chain(raised.value)
        assert any(isinstance(e, OSError) and e.errno == errno.ENOSPC for e in chain)
        assert any(isinstance(e, RuntimeError) and "compiler crashed" in str(e)
                   for e in chain)
        assert log.read(SESSION) == []
    else:
        with pytest.raises(RuntimeError, match="compiler crashed") as raised:
            host.turn(SESSION, "go")
        assert not isinstance(raised.value, AuditWriteError)
        [record] = log.read(SESSION)
        assert record["error"] == "runtime_exception"
    assert compiler.ran == ["llm"]


@pytest.mark.parametrize("append_fails", [False, True])
def test_a_strict_failure_is_not_reported_as_a_deadline(tmp_path, monkeypatch, append_fails):
    """The product maps TimeoutError to 504 turn_deadline_exceeded. A guest
    that outran the deadline and then could not be recorded must surface as
    the audit failure, or the missing record would hide behind a timeout.
    agent.py's clock is replaced for this test only, so 'the deadline passed
    while the guest ran' needs no sleep. The control (append_fails=False)
    proves the clock really drives the deadline path."""
    clock = {"now": 1000.0}
    fake_time = types.ModuleType("time")
    fake_time.__dict__.update(vars(time))
    fake_time.monotonic = lambda: clock["now"]
    monkeypatch.setattr(agent_module, "time", fake_time)

    def deadline_passes_mid_forge(kind):
        clock["now"] += 60.0
        return True

    compiler = Compiler(fault=deadline_passes_mid_forge)
    host, log = build(tmp_path, compiler)
    if append_fails:
        fail_append_number(log, 0)
    expected = AuditWriteError if append_fails else TimeoutError
    with pytest.raises(expected):
        host.turn_with_usage(SESSION, "go", deadline_monotonic=clock["now"] + 30.0)
    assert compiler.ran == ["llm"]
    if append_fails:
        assert log.read(SESSION) == []
    else:
        [record] = log.read(SESSION)
        assert record["error"] == "turn_deadline_exceeded"


class WrappingStore(SessionStore):
    """A store that reports a failed save as its own error, chained to the
    OS error: the shape a hardened save (one that cleans up its scratch file)
    may well take."""

    def save(self, session_id, messages):
        try:
            super().save(session_id, messages)
        except OSError as error:
            raise RuntimeError("session could not be saved") from error


@pytest.mark.parametrize("store_wraps_its_error", [False, True])
def test_a_full_disk_that_also_fails_the_session_save_still_surfaces_as_an_audit_failure(
        tmp_path, monkeypatch, store_wraps_its_error):
    """The realistic trigger, a full disk, also fails the session save in the
    turn's `finally`. That OSError replaced AuditWriteError at the turn
    boundary (reproduced 2026-11-10 by emulating strict mode), so a caller
    that classifies failures by type, as the product's stable error and
    metric must, would have seen a generic failure and never learned that a
    record was missing. The later failure is named on the audit error, not
    dropped; the cause is still the append's; the committed session is
    untouched. With a store that wraps its own error, the audit failure sits
    one link further back, and is found there too."""
    compiler = Compiler()
    host, _ = build(tmp_path, compiler)
    if store_wraps_its_error:
        host.store = WrappingStore(host.store.dir)
    assert host.turn(SESSION, "one") == "done"
    committed = host.store.load(SESSION)
    full_disk(monkeypatch, sessions_too=True)
    with pytest.raises(AuditWriteError) as raised:
        host.turn(SESSION, "two")
    later = "RuntimeError" if store_wraps_its_error else "OSError"
    notes = getattr(raised.value, "__notes__", [])
    assert any("unwound" in n and later in n for n in notes), notes
    assert raised.value.__cause__.errno == errno.ENOSPC
    assert host.store.load(SESSION) == committed


def test_a_session_save_failure_alone_still_propagates(tmp_path, monkeypatch):
    """Anti-vacuity for the test above: only an in-flight audit failure takes
    precedence. A save that fails on a turn whose records were all written
    still fails the turn with its own error, exactly as before."""
    compiler = Compiler()
    host, log = build(tmp_path, compiler)
    real_os_open = os.open

    def os_open(path, flags, *args, **kwargs):
        if flags & os.O_CREAT and is_session_scratch(path):
            raise OSError(errno.ENOSPC, "No space left on device (injected)")
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", os_open)
    with pytest.raises(OSError) as raised:
        host.turn(SESSION, "go")
    assert not isinstance(raised.value, AuditWriteError)
    assert raised.value.errno == errno.ENOSPC
    assert [r["kind"] for r in log.read(SESSION)] == ["llm", "parse"]


def test_strict_mode_fails_a_real_turn_on_the_real_compiler(
        scripted_llm, tmp_path, mcp, monkeypatch):
    """End to end on the real compiler: the LLM guest really runs (the mock
    provider receives the second request), its record cannot be written, and
    the turn fails instead of answering."""
    host = make_agent(scripted_llm, tmp_path, mcp)
    host.audit = AuditLog(tmp_path / "audit", key=KEY, strict=True)
    scripted_llm.script = [msg([text("hi")])]
    assert host.turn(SESSION, "one") == "hi"
    chain = host.audit._path(SESSION)
    before = chain.read_bytes()
    full_disk(monkeypatch)
    with pytest.raises(AuditWriteError):
        host.turn(SESSION, "two")
    assert len(scripted_llm.requests) == 2, "the second LLM guest must really have run"
    assert chain.read_bytes() == before


# ── source guards ────────────────────────────────────────────────────────


def _qualified_nodes(tree):
    """(qualified name of the enclosing def, node) for every node."""
    def walk(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = f"{scope}.{child.name}" if scope else child.name
            else:
                inner = scope
            yield inner, child
            yield from walk(child, inner)
    yield from walk(tree, "")


def test_the_compiler_is_reached_only_through_forge():
    """Every guest is audited because `_forge` is the only caller of the
    compiler (test_guards.py pins the one `self._mcp.forge(` call site by
    regex). This pins it by syntax across every module a turn runs through,
    so an alias (`m = self._mcp; m.forge(...)`) or a `getattr` cannot open
    a second, unaudited path either."""
    hits = []
    for name in ("agent.py", "product_service.py", "product_main.py"):
        tree = ast.parse((PI_ROOT / name).read_text())
        for scope, node in _qualified_nodes(tree):
            if isinstance(node, ast.Attribute) and node.attr == "forge":
                hits.append((name, scope))
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and node.func.id == "getattr" and len(node.args) >= 2
                  and isinstance(node.args[1], ast.Constant)
                  and node.args[1].value == "forge"):
                hits.append((name, scope))
    assert hits == [("agent.py", "PiAgent._forge")], hits


def test_every_forge_path_in_the_host_names_its_session():
    """`_forge` writes a record only when it is given a session, so a call
    path that omits one runs its guest with no record at all, strict or not:
    the same gap by another door. Every call of the four forge paths in
    agent.py passes `session=`, and never a literal None."""
    paths = {"_forge", "_llm", "_parse", "_dispatch"}
    tree = ast.parse((PI_ROOT / "agent.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr in paths
             and ast.unparse(n.func.value) == "self"]
    assert {n.func.attr for n in calls} == paths, "anti-vacuity: every path is called"
    unnamed = [(n.func.attr, n.lineno) for n in calls
               if not any(k.arg == "session" and not (
                   isinstance(k.value, ast.Constant) and k.value.value is None)
                   for k in n.keywords)]
    assert unnamed == [], f"forge paths called without a session: {unnamed}"


def test_forge_never_wraps_its_audit_records_in_a_handler():
    """The same defect class, at the chokepoint: a `try/except` around one of
    _forge's `self.audit.record(...)` calls would swallow a strict failure
    as surely as the default log does. The record in the compiler-failure
    path sits INSIDE an except clause, which is fine (a raise there
    propagates); what is forbidden is a record inside the BODY of a try that
    has handlers."""
    tree = ast.parse((PI_ROOT / "agent.py").read_text())
    forge = next(node for scope, node in _qualified_nodes(tree)
                 if scope == "PiAgent._forge" and isinstance(node, ast.FunctionDef))
    parents = {child: parent for parent in ast.walk(forge)
               for child in ast.iter_child_nodes(parent)}
    records = [n for n in ast.walk(forge) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute) and n.func.attr == "record"
               and ast.unparse(n.func.value) == "self.audit"]
    assert len(records) == 2, "one record per path: guest returned, guest raised"
    for call in records:
        node = call
        while node is not forge:
            parent = parents[node]
            if isinstance(parent, ast.Try) and parent.handlers and node in parent.body:
                raise AssertionError(
                    f"_forge line {call.lineno}: an audit record inside a try "
                    f"with handlers can be swallowed")
            node = parent
