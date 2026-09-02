"""The six-category failure drill is safe, bound, and anti-vacuous.

The drill itself can only produce evidence on the release's own platform, so
what is pinned here is everything that does NOT need a bootable bundle: the
report contract `check_readiness_evidence` will accept, the fail-closed input
rules, and — most importantly — that a drill run with an injected service can
never claim to be qualifying evidence.
"""

import http.client
import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agent import AuditLog, SessionStore
from conftest import PI_ROOT
from product_service import DurableQuotaStore, _internal_session
from scripts.build_release import APP_FILES, DOC_FILES, build_release
from scripts.check_readiness_evidence import FAILURE_CATEGORIES, _validate_failure_report
from scripts.failure_drill import (
    CATEGORIES,
    FailureDrillError,
    run_drill,
)
from state_tool import CLEAN_MARKER


AUDIT_KEY = "failure-drill-test-audit-key-with-sufficient-entropy"
EVIDENCE_URL = "https://example.invalid/failure-drill/run/1"


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True,
        capture_output=True, text=True).stdout.strip()


def _fake_sigil(tmp_path):
    root = tmp_path / "SIGIL"
    (root / "crates").mkdir(parents=True)
    (root / "crates" / "runtime.txt").write_text("runtime source")
    stdlib = root / "stdlib" / "sigil"
    stdlib.mkdir(parents=True)
    for name in ("http", "json", "kv"):
        (stdlib / f"{name}.sigil").write_text(f"module sigil::{name};\n")
    binary = root / "target" / "release" / "sigil-mcp"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"fake immutable runtime")
    binary.chmod(0o755)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "crates", "stdlib"], check=True)
    subprocess.run([
        "git", "-C", str(root), "-c", "user.name=test", "-c",
        "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
    pin = tmp_path / "SIGIL_REV"
    pin.write_text(
        f"crates = {_git(root, 'rev-parse', 'HEAD:crates')}\n"
        f"stdlib = {_git(root, 'rev-parse', 'HEAD:stdlib')}\n"
        f"ref = {_git(root, 'rev-parse', 'HEAD')}\n")
    return root, pin


def _artifact(tmp_path, version="1.0.0"):
    sigil, pin = _fake_sigil(tmp_path)
    app = tmp_path / "app-source"
    for relative in (
            *APP_FILES, *DOC_FILES,
            "config/auth.example.json", "config/alert-policy.json",
            "config/grafana-slo-dashboard.json"):
        destination = app / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PI_ROOT / relative, destination)
    for source in (PI_ROOT / "tools").iterdir():
        if source.is_file() and source.suffix in (".sigil", ".json"):
            destination = app / "tools" / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    (app / "VERSION").write_text(version + "\n")
    return build_release(
        sigil_root=sigil, output_dir=tmp_path / "out", app_root=app,
        pin_file=pin, build_runtime=False, platform_tag="test")[0]


def _audit_key_file(tmp_path):
    key = tmp_path / "audit-key"
    key.write_text(AUDIT_KEY)
    key.chmod(0o600)
    return key


class _FakeService:
    """A service that commits real, verifiable state without a real bundle.

    The drill's own state-integrity check runs the release's real
    `state_tool.py`, which verifies signed audit chains — so a fake that wrote
    plausible-looking bytes would fail for the wrong reason. This commits
    genuine sessions and genuine signed records; only the HTTP surface and the
    process are simulated.
    """

    def __init__(self, release_root, state_dir, *, audit_key, label, endpoint, **_):
        self.release_root = Path(release_root)
        self.state_dir = Path(state_dir)
        self.audit_key = audit_key
        self.label = label
        self.endpoint = endpoint
        self.mode = "healthy"
        self.running = False
        self.turns = 0
        self._committed = 0

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self):
        self.running = True
        return {"ready": True, "dependencies": {"runtime": True}}

    def try_start(self):
        """Boot expecting a possible fail-closed refusal (corrupt state)."""
        audit = self.state_dir / "audit"
        chains = sorted(audit.glob("*.jsonl")) if audit.is_dir() else []
        for chain in chains:
            if "TAMPERED" in chain.read_text():
                return False, "existing audit chains failed signature verification"
        self.running = True
        return True, ""

    def readiness(self):
        return (200, {"status": "ready"}) if self.running else (503, {"status": "unready"})

    def chat(self, session, message, timeout=45):
        """Really call the drill's provider, so provider modes are exercised."""
        if not self.running:
            return 503, {"error": {"code": "service_draining"}}
        if self.mode == "full":
            return 507, {"error": {"code": "storage_exhausted"}}
        request = urllib.request.Request(
            self.endpoint, data=b"{}", method="POST",
            headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                response.read()
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            # Both a provider outage and a transport fault reach the product
            # boundary as the same stable error.
            return 502, {"error": {"code": "agent_failure",
                                   "message": "agent turn failed"}}
        self.turns += 1
        self._commit(session)
        return 200, {"reply": "committed", "usage": {}}

    def runtime_pids(self):
        return [424242] if self.running else []

    def kill_runtime(self):
        # The supervised runtime is replaced on the next forge; the host lives.
        return [424242]

    def kill(self):
        self.running = False

    def exhaust_state_filesystem(self):
        self.mode = "full"
        return "simulated ENOSPC (injected service)"

    def relieve_state_filesystem(self):
        self.mode = "healthy"

    def drain(self):
        self.running = False
        (self.state_dir / CLEAN_MARKER).write_text(json.dumps({
            "schema_version": 1,
            "product_version": (self.release_root / "app" / "VERSION").read_text().strip(),
            "clean_shutdown_unix": int(time.time()),
        }))

    # ── real committed state ─────────────────────────────────────────────
    def _commit(self, name):
        # Keyed by the session the CALLER named, exactly as the real service
        # does: deriving its own would make two different turns share one
        # audit chain and look like state corruption.
        session = _internal_session("failure-drill-tenant", name)
        store = SessionStore(self.state_dir / "sessions")
        store.save(session, [{"role": "user", "content": f"committed {name}"}])
        audit = AuditLog(self.state_dir / "audit", key=self.audit_key.encode())
        audit.record(session, "tool", "source", "input", "output", None, None, 1)
        quota = DurableQuotaStore(
            self.state_dir / "product-quotas.sqlite3",
            max_concurrent_turns=10, tokens_per_day=1000,
            token_reservation_per_turn=10, storage_bytes_per_tenant=1_000_000,
            storage_reservation_per_turn=10, audit_bytes_per_tenant=1_000_000,
            audit_reservation_per_turn=10)
        lease = quota.acquire_turn("failure-drill-tenant")
        lease.release(
            session_id=session, usage={"input_tokens": 1, "output_tokens": 1},
            storage_bytes=store._path(session).stat().st_size,
            audit_bytes=audit._path(session).stat().st_size)
        self._committed += 1


def _run(tmp_path, **overrides):
    # Defaults are only MATERIALIZED when not overridden: _audit_key_file
    # rewrites the key's mode, so building it unconditionally would silently
    # repair the very permissions a fail-closed case is trying to set.
    kwargs = {
        "artifact": overrides.pop("artifact", None) or _artifact(tmp_path),
        "audit_key_file": overrides.pop("audit_key_file", None)
                          or _audit_key_file(tmp_path),
        "evidence_url": EVIDENCE_URL,
        "output": tmp_path / "failure-injection.json",
        "work_dir": tmp_path / "work",
        "service_factory": _FakeService,
        "now_fn": lambda: 1_700_000_000,
    }
    kwargs.update(overrides)
    return run_drill(**kwargs)


def test_drill_covers_exactly_the_six_categories_the_gate_requires():
    """A category renamed here and not there would silently stop being evidence."""
    assert set(CATEGORIES) == FAILURE_CATEGORIES


def test_injected_service_produces_a_complete_but_non_qualifying_report(tmp_path):
    report = _run(tmp_path)

    assert report["schema_version"] == 1
    assert report["test"] == "sigil-pi-v1-failure-injection"
    assert set(report["categories"]) == FAILURE_CATEGORIES
    for name, result in report["categories"].items():
        assert result["passed"] is True, name
        assert result["state_integrity_verified"] is True, name
        assert result["service_recovered"] is True, name
        assert result["evidence_url"] == EVIDENCE_URL
        # Observed behavior is assembled from what happened, never canned.
        assert result["observed_behavior"].strip()
        assert result["injection"].strip()
        assert result["expected_behavior"].strip()

    # Every category must have had something committed to lose, or its
    # integrity claim would be vacuous.
    for name, result in report["categories"].items():
        assert result["continuity"]["committed_customer_files"] > 0, name

    assert report["topology"]["production_artifact"] is False
    assert report["qualification_eligible"] is False
    assert "service was injected rather than the real release" in \
        report["qualification_failures"]
    assert json.loads((tmp_path / "failure-injection.json").read_text()) == report
    assert (tmp_path / "failure-injection.json").stat().st_mode & 0o077 == 0


def test_a_real_shaped_report_satisfies_the_readiness_validator(tmp_path):
    """The drill's output is the gate's input; drift between them is the bug."""
    report = _run(tmp_path)
    # Promote exactly the two facts an injected run cannot earn, then require
    # the real validator to accept everything else unchanged.
    report["topology"]["production_artifact"] = True
    report["qualification_eligible"] = True
    report["qualification_failures"] = []
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "failure-injection.json").write_text(json.dumps(report))
    validated = _validate_failure_report(
        evidence,
        artifact_sha256=report["artifact"]["sha256"],
        version=report["artifact"]["version"])
    assert validated["test"] == "sigil-pi-v1-failure-injection"


def test_a_category_that_does_not_fail_closed_fails_the_drill(tmp_path):
    """Anti-vacuity: a service that shrugs off corruption must not pass."""

    class _NeverRefuses(_FakeService):
        def try_start(self):
            self.running = True
            return True, ""

    report = _run(tmp_path, service_factory=_NeverRefuses)
    corrupt = report["categories"]["corrupt_state"]
    assert corrupt["passed"] is False
    assert corrupt["failures"]
    assert report["qualification_eligible"] is False
    assert any("corrupt_state" in failure for failure in report["qualification_failures"])


def test_a_category_that_loses_committed_state_fails_the_drill(tmp_path):
    """The integrity claim is a real before/after comparison, not a constant."""

    class _LosesState(_FakeService):
        def kill(self):
            super().kill()
            for path in sorted((self.state_dir / "sessions").glob("*.kv")):
                path.unlink()

    report = _run(tmp_path, service_factory=_LosesState)
    interrupted = report["categories"]["interrupted_writes"]
    assert interrupted["state_integrity_verified"] is False
    assert interrupted["passed"] is False
    assert interrupted["continuity"]["lost_or_changed"]
    assert report["qualification_eligible"] is False


@pytest.mark.parametrize("mode,match", [
    ("existing_output", "overwrite"),
    ("world_readable_key", "group or other"),
    ("plain_evidence_url", "https"),
    ("existing_work_dir", "must not already exist"),
])
def test_drill_fails_closed_on_its_input_and_output_contract(tmp_path, mode, match):
    overrides = {}
    if mode == "existing_output":
        (tmp_path / "failure-injection.json").write_text("existing evidence")
    elif mode == "world_readable_key":
        key = _audit_key_file(tmp_path)
        key.chmod(0o644)
        overrides["audit_key_file"] = key
    elif mode == "plain_evidence_url":
        overrides["evidence_url"] = "http://example.invalid/insecure"
    else:
        work = tmp_path / "work"
        work.mkdir()
        overrides["work_dir"] = work
    with pytest.raises(FailureDrillError, match=match):
        _run(tmp_path, **overrides)
