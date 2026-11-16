"""Fail-closed evidence for every production-readiness failure category.

Specialized crash, provider, corruption, network, and audit tests live beside
their implementations. This module injects the previously missing filesystem
failures and verifies that the evidence matrix cannot silently lose a required
category, point at a test pytest no longer collects, or count a test that did
not run and pass, and that the failures that start at the model provider's
end of the network are injected there (audit 2026-11-10, F-17).
"""

import ast
import errno
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

import toolchain

from agent import SessionStore
from conftest import PI_ROOT
from product_service import AuthRegistry, Principal, ProductScheduleStore, ProductService


REQUIRED_FAILURES = {
    "runtime_crashes",
    "unavailable_model_providers",
    "full_disks",
    "corrupt_state",
    "network_failures",
    "interrupted_writes",
}
CHAT_TOKEN = "failure-injection-chat-token-with-test-only-entropy"


def _principal():
    return Principal(
        principal_id="planner",
        tenant_id="tenant-a",
        scopes=frozenset({"schedules:read", "schedules:write"}),
        tools=frozenset(),
    )


def _is_scratch_of(path, committed_path):
    """A scratch file agent.write_durably creates for `committed_path`."""
    path = Path(os.fsdecode(path))
    return (path.parent == committed_path.parent
            and re.fullmatch(rf"\.{re.escape(committed_path.name)}\.[0-9a-f]{{16}}\.tmp",
                             path.name) is not None)


def test_full_disk_preserves_committed_session_state(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions")
    store.save("session", [{"role": "user", "content": "committed"}])
    committed_path = store._path("session")
    committed_bytes = committed_path.read_bytes()
    real_open = os.open

    def no_space(path, flags, *args, **kwargs):
        # Where the save's scratch file is created, before any byte is written.
        if flags & os.O_CREAT and _is_scratch_of(path, committed_path):
            raise OSError(errno.ENOSPC, "injected full disk")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", no_space)
    with pytest.raises(OSError) as error:
        store.save("session", [{"role": "user", "content": "uncommitted"}])
    assert error.value.errno == errno.ENOSPC
    assert committed_path.read_bytes() == committed_bytes
    assert store.load("session") == [{"role": "user", "content": "committed"}]


def test_full_disk_preserves_committed_schedule_state(tmp_path, monkeypatch):
    """The schedules are rows of a SQLite ledger (schedule store schema 2,
    audit F-27). The full disk is SQLite's own, SQLITE_FULL, raised where the
    write needs a page the ledger cannot have; the store reports it as ENOSPC,
    and the committed schedule is as it was."""
    path = tmp_path / "schedules.json"
    store = ProductScheduleStore(path, clock=lambda: 100.0)
    principal = _principal()
    store.put(
        principal, name="daily", session="work", message="committed", every_ms=1000)
    key = ProductScheduleStore._key("tenant-a", "daily")
    committed = store.get(key, with_message=True)
    real_connect = sqlite3.connect

    def no_space(*args, **kwargs):
        # The ledger may grow by no page: the write that needs one fails.
        db = real_connect(*args, **kwargs)
        db.execute(f"PRAGMA max_page_count={db.execute('PRAGMA page_count').fetchone()[0]}")
        return db

    monkeypatch.setattr(sqlite3, "connect", no_space)
    with pytest.raises(OSError) as error:
        store.put(
            principal, name="daily", session="work", message="uncommitted " * 2000,
            every_ms=1000)
    monkeypatch.undo()
    assert error.value.errno == errno.ENOSPC
    assert store.get(key, with_message=True) == committed
    [entry] = store.for_tenant("tenant-a")
    assert entry["message"] == "committed"


def test_full_disk_at_product_boundary_is_stable_and_releases_capacity():
    class FullDiskAgent:
        manifest = {}
        memory = None
        _mcp = object()

        def turn_with_usage(self, *args, **kwargs):
            raise OSError(errno.ENOSPC, "private state path: no space left")

        @staticmethod
        def turn_telemetry():
            return {}

    auth = AuthRegistry([{
        "sha256": hashlib.sha256(CHAT_TOKEN.encode()).hexdigest(),
        "principal": "chat-user",
        "tenant": "tenant-a",
        "scopes": ["chat"],
        "tools": [],
    }])
    service = ProductService(
        FullDiskAgent(), auth, max_concurrent_turns=1, log_sink=lambda record: None)
    headers = {"Authorization": f"Bearer {CHAT_TOKEN}", "X-Request-ID": "disk-full-test"}
    payload = json.dumps({"session": "work", "message": "hello"}).encode()

    for _ in range(2):
        status, _, body = service.dispatch("POST", "/v1/chat", headers, payload)
        assert status == 500
        assert body["error"] == {
            "code": "internal_error", "message": "internal service error"}
        rendered = json.dumps(body)
        assert "private" not in rendered and "space" not in rendered
    assert service.metrics.snapshot()["active_turns"] == 0


def test_interrupted_session_replace_preserves_committed_state(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions")
    store.save("session", [{"role": "user", "content": "committed"}])
    committed_path = store._path("session")
    committed_bytes = committed_path.read_bytes()
    uncommitted = [{"role": "user", "content": "uncommitted"}]
    real_replace = os.replace
    boundary = {}

    def interrupted(candidate, target, *args, **kwargs):
        if Path(os.fsdecode(target)) == committed_path:
            boundary["scratch"] = Path(os.fsdecode(candidate))
            boundary["bytes"] = boundary["scratch"].read_bytes()
            raise OSError(errno.EINTR, "injected interruption before commit")
        return real_replace(candidate, target, *args, **kwargs)

    monkeypatch.setattr(os, "replace", interrupted)
    with pytest.raises(OSError) as error:
        store.save("session", uncommitted)
    assert error.value.errno == errno.EINTR
    # Observed AT the boundary: the scratch file was complete when the
    # rename was refused. (This read `temp_path.exists()` after the failure
    # until 2026-11-10, which pinned the scratch file the failure left behind:
    # audit F-30.)
    assert boundary.get("bytes") == json.dumps(uncommitted, ensure_ascii=False).encode(), \
        "injection did not reach the post-write commit boundary"
    assert _is_scratch_of(boundary["scratch"], committed_path)
    assert not boundary["scratch"].exists(), "the failed save left its scratch file behind"
    assert committed_path.read_bytes() == committed_bytes
    assert SessionStore(store.dir).load("session") == [
        {"role": "user", "content": "committed"}]


class _CommitInterrupted:
    """A connection whose COMMIT is interrupted: what the transaction holds at
    that moment is recorded first, through the transaction itself."""

    def __init__(self, db, key, boundary):
        self._db, self._key, self._boundary = db, key, boundary

    def __getattr__(self, name):
        return getattr(self._db, name)

    def execute(self, sql, *args):
        if sql == "COMMIT":
            self._boundary["message"] = self._db.execute(
                "SELECT message FROM schedule_messages WHERE key=?", (self._key,)).fetchone()
            raise OSError(errno.EINTR, "injected interruption before commit")
        return self._db.execute(sql, *args)


def test_interrupted_schedule_replace_preserves_committed_state(tmp_path, monkeypatch):
    """The replace is a transaction on the ledger (schedule store schema 2,
    audit F-27): interrupted at its commit, it leaves the committed schedule
    as it was and the new one in no file."""
    path = tmp_path / "schedules.json"
    store = ProductScheduleStore(path, clock=lambda: 100.0)
    principal = _principal()
    store.put(
        principal, name="daily", session="work", message="committed", every_ms=1000)
    key = ProductScheduleStore._key("tenant-a", "daily")
    committed = store.get(key, with_message=True)
    real_connect = sqlite3.connect
    boundary = {}
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: _CommitInterrupted(
        real_connect(*args, **kwargs), key, boundary))
    with pytest.raises(OSError) as error:
        store.put(
            principal, name="daily", session="work", message="uncommitted",
            every_ms=1000)
    monkeypatch.undo()
    assert error.value.errno == errno.EINTR
    # Observed AT the boundary: the transaction held the whole new schedule
    # when its commit was refused. (Schema 1 pinned the scratch file such a
    # failure left behind until 2026-11-10: audit F-30.)
    assert boundary.get("message") == ("uncommitted",), (
        "injection did not reach the commit boundary")
    left = [p.name for p in tmp_path.iterdir()
            if p.is_file() and b"uncommitted" in p.read_bytes()]
    assert left == [], f"the failed replace left the new schedule in {left}"
    assert store.get(key, with_message=True) == committed
    [entry] = ProductScheduleStore(path).for_tenant("tenant-a")
    assert entry["message"] == "committed"


def test_failure_injection_matrix_is_complete_and_points_to_collected_tests():
    matrix = json.loads(
        (PI_ROOT / "config" / "failure-injection-matrix.json").read_text())
    assert set(matrix) == REQUIRED_FAILURES
    for category, evidence in matrix.items():
        assert isinstance(evidence, list) and evidence, f"{category} has no evidence"
        assert all(isinstance(node, str) and node.startswith("tests/") for node in evidence)

    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only"],
        capture_output=True, text=True, cwd=PI_ROOT)
    assert collected.returncode == 0, collected.stdout + collected.stderr
    node_ids = set(re.findall(r"^(tests/\S+::test_\S+)$", collected.stdout, re.M))
    for category, evidence in matrix.items():
        for expected in evidence:
            assert any(node == expected or node.startswith(expected + "[")
                       for node in node_ids), (
                f"{category} points to an uncollected test: {expected}")


# ── the matrix's tests run and pass ──────────────────────────────────────

# The skip conftest.needs_toolchain() takes on a machine with no compiler.
NO_TOOLCHAIN_SKIP = "no SIGIL toolchain"
# This module's own matrix checks: a matrix that bound one would run it again
# from inside itself.
MATRIX_CHECKS = "tests/test_failure_injection.py::test_failure_injection_matrix"


def _matrix():
    return json.loads(
        (PI_ROOT / "config" / "failure-injection-matrix.json").read_text())


def _toolchain_absent():
    """True on a machine with no compiler that is not required to have one:
    the standalone CI job, where every forge test skips by design."""
    return toolchain.resolve(require=False) is None and not toolchain.required()


def _run_tests(node_ids, *, root, scratch):
    """Run `node_ids` in a fresh pytest under `root` and read its JUnit
    report, not its text: {node id: [(outcome, detail), ...]}, one entry per
    case, where outcome is "passed", "failure", "error" or "skipped" (an
    expected failure is reported as skipped)."""
    report = Path(scratch) / "matrix-run.xml"
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"--junitxml={report}", *sorted(set(node_ids))],
        capture_output=True, text=True, cwd=root)
    assert report.exists(), (
        f"the bound tests did not run (exit {completed.returncode}):\n"
        f"{completed.stdout[-4000:]}\n{completed.stderr[-4000:]}")
    cases = {}
    for case in ElementTree.parse(report).getroot().iter("testcase"):
        node = case.get("classname").replace(".", "/") + ".py::" + case.get("name")
        problem = next((child for child in case
                        if child.tag in ("failure", "error", "skipped")), None)
        outcome = "passed" if problem is None else problem.tag
        detail = "" if problem is None else (
            f"{problem.get('message') or ''}\n{problem.text or ''}".strip())
        cases.setdefault(node, []).append((outcome, detail))
    return cases


def _unproven(evidence, cases, *, toolchain_absent):
    """Every bound id whose tests did not all run and pass, with what
    happened instead. A parametrized id stands for all of its cases."""
    problems = []
    for expected in evidence:
        ran = [(node, outcome, detail) for node, results in sorted(cases.items())
               if node == expected or node.startswith(expected + "[")
               for outcome, detail in results]
        if not ran:
            problems.append(f"{expected}: did not run")
        for node, outcome, detail in ran:
            if outcome == "passed":
                continue
            if (outcome == "skipped" and toolchain_absent
                    and NO_TOOLCHAIN_SKIP in detail):
                continue
            problems.append(f"{node}: {outcome}: {detail[:3000]}")
    return problems


def test_failure_injection_matrix_tests_run_and_pass(tmp_path):
    """Collecting a test proves only that it exists. A bound test that was
    skipped, or that failed, satisfied the check above (audit 2026-11-10,
    F-17). The bound tests now run, in a fresh pytest, and every case of
    every one must pass. A skip is missing evidence, except the no-toolchain
    skip on a machine that has no compiler and is not required to have one
    (the standalone CI job). The gate sets PI_REQUIRE_TOOLCHAIN, and there
    that skip is itself a failure."""
    matrix = _matrix()
    bound = {node for evidence in matrix.values() for node in evidence}
    assert not any(node.startswith(MATRIX_CHECKS) for node in bound), (
        "the matrix binds one of its own checks")
    cases = _run_tests(bound, root=PI_ROOT, scratch=tmp_path)
    absent = _toolchain_absent()
    problems = {category: _unproven(evidence, cases, toolchain_absent=absent)
                for category, evidence in sorted(matrix.items())}
    assert not any(problems.values()), "\n".join(
        f"{category}: {problem}" for category, found in problems.items()
        for problem in found)


def test_failure_injection_matrix_run_refuses_what_did_not_pass(tmp_path):
    """The run above can fire. Against a throwaway suite, a skipped test, a
    failing one, an erroring one, an expected failure, one failing case of a
    parametrized test and an id that names no test are each reported; only
    the passing test is accepted. The no-toolchain skip is accepted only
    where no toolchain is present and none is required."""
    suite = tmp_path / "suite"
    (suite / "tests").mkdir(parents=True)
    (suite / "tests" / "test_probe.py").write_text(textwrap.dedent("""\
        import pytest

        @pytest.fixture()
        def broken():
            raise RuntimeError("the fixture broke on purpose")

        def test_passes():
            pass

        def test_skips():
            pytest.skip("skipped on purpose")

        def test_needs_a_toolchain():
            pytest.skip("no SIGIL toolchain (set SIGIL_ROOT or PI_FORGE_BIN)")

        def test_fails():
            assert False, "failed on purpose"

        def test_errors(broken):
            pass

        # Bound to a name, not written as a decorator: the README guard
        # counts xfail decorators in this directory, and this is not one.
        expected_failure = pytest.mark.xfail(strict=True, reason="fails on purpose")

        @expected_failure
        def test_expected_failure():
            assert False

        @pytest.mark.parametrize("case", ["good", "bad"])
        def test_one_case_fails(case):
            assert case == "good"
        """))
    names = ["test_passes", "test_skips", "test_needs_a_toolchain", "test_fails",
             "test_errors", "test_expected_failure", "test_one_case_fails"]
    evidence = [f"tests/test_probe.py::{name}" for name in names]
    cases = _run_tests(evidence, root=suite, scratch=tmp_path)
    missing = "tests/test_probe.py::test_that_does_not_exist"

    reported = "\n".join(_unproven(evidence + [missing], cases, toolchain_absent=False))
    for refused in ("test_skips: skipped", "test_needs_a_toolchain: skipped",
                    "test_fails: failure", "test_errors: error",
                    "test_expected_failure: skipped", "test_one_case_fails[bad]: failure",
                    f"{missing}: did not run"):
        assert refused in reported, f"{refused!r} was accepted:\n{reported}"
    assert "test_passes" not in reported and "[good]" not in reported

    without_toolchain = "\n".join(_unproven(evidence, cases, toolchain_absent=True))
    assert "test_needs_a_toolchain" not in without_toolchain
    assert "test_skips: skipped" in without_toolchain


# ── where the provider-side failures are injected ────────────────────────

# The failures that start outside the product, at the model provider's end of
# the network. Each category must bind at least one test that makes the
# failure happen there and watches the product answer: a test that sends its
# request through serve_product (the `product_server` fixture) to the real
# agent forging on the real compiler (the `mcp` fixture). The research host's
# retry probe scripts `_forge`, a scripted agent has no provider at all, and
# the load generator's classifier runs no product code (audit 2026-11-10,
# F-17).
PROVIDER_SIDE_FAILURES = {"network_failures", "unavailable_model_providers"}
PRODUCT_BOUNDARY_FIXTURES = {"product_server", "mcp"}


def _requested_fixtures(node_id):
    """The parameters of the module-level test function `node_id` names."""
    path, _, name = node_id.partition("::")
    tree = ast.parse((PI_ROOT / path).read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name.split("[", 1)[0]:
            return {argument.arg for argument in node.args.args}
    raise AssertionError(f"{node_id}: no module-level test function of that name")


def test_provider_side_failures_are_injected_at_the_product_boundary():
    matrix = _matrix()
    assert PROVIDER_SIDE_FAILURES <= REQUIRED_FAILURES
    for category in sorted(PROVIDER_SIDE_FAILURES):
        at_the_boundary = [
            node for node in matrix[category]
            if PRODUCT_BOUNDARY_FIXTURES <= _requested_fixtures(node)]
        assert at_the_boundary, (
            f"{category} binds no test that injects the failure at the product "
            f"boundary (one taking {sorted(PRODUCT_BOUNDARY_FIXTURES)}): "
            f"{matrix[category]}")
