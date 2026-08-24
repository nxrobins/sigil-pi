"""Product startup fails closed before spawning the compiler."""

import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import product_main
from product_service import ConfigError
from state_tool import SCHEMA_VERSION


def _base_env(tmp_path, monkeypatch):
    token = "startup-test-token-with-high-entropy-placeholder"
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": [{
        "sha256": hashlib.sha256(token.encode()).hexdigest(),
        "principal": "user",
        "tenant": "tenant",
        "scopes": ["chat"],
        "tools": [],
        "not_before_unix": int(time.time()) - 60,
        "expires_unix": int(time.time()) + 86400,
    }]}))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "provider-test-key")
    monkeypatch.setenv("PI_AUDIT_KEY", "a" * 32)
    monkeypatch.setenv("PI_AUTH_FILE", str(auth))
    monkeypatch.setenv("PI_STATE", str(tmp_path / "state"))


def test_product_requires_auth_provider_and_signing_credentials(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "PI_AUDIT_KEY", "PI_AUTH_FILE"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        product_main.run()


def test_product_rejects_short_audit_key_before_runtime_spawn(tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    monkeypatch.setenv("PI_AUDIT_KEY", "short")
    with pytest.raises(ConfigError, match="at least 32"):
        product_main.run()


def test_product_requires_bounded_credential_validity_before_runtime_spawn(
        tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    path = tmp_path / "auth.json"
    document = json.loads(path.read_text())
    document["tokens"][0].pop("not_before_unix")
    document["tokens"][0].pop("expires_unix")
    path.write_text(json.dumps(document))
    with pytest.raises(ConfigError, match="requires not_before"):
        product_main.run()


def test_product_rejects_memory_until_it_has_full_data_lifecycle(tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    monkeypatch.setenv("PI_MEMORY_SIDECAR", "/some/research-sidecar")
    with pytest.raises(ConfigError, match="not supported"):
        product_main.run()


def test_external_plaintext_bind_fails_before_runtime_spawn(tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    monkeypatch.setenv("PI_HOST", "0.0.0.0")
    with pytest.raises(ConfigError, match="require TLS"):
        product_main.run()


def test_positive_product_knobs_reject_zero(monkeypatch):
    monkeypatch.setenv("PI_TEST_POSITIVE", "0")
    with pytest.raises(ConfigError, match="positive"):
        product_main._positive_env_int("PI_TEST_POSITIVE", 1)


def test_turn_lease_must_outlive_hard_deadline_before_runtime_spawn(tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    monkeypatch.setenv("PI_TURN_DEADLINE_SECONDS", "300")
    monkeypatch.setenv("PI_TURN_LEASE_SECONDS", "300")
    with pytest.raises(ConfigError, match="must be greater"):
        product_main.run()


def test_token_reservation_covers_turn_output_bound_before_runtime_spawn(
        tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    monkeypatch.setenv("PI_MAX_STEPS", "10")
    monkeypatch.setenv("PI_MAX_TOKENS", "100")
    monkeypatch.setenv("PI_TOKEN_RESERVATION_PER_TURN", "999")
    with pytest.raises(ConfigError, match="must cover"):
        product_main.run()


def test_resource_reservation_cannot_exceed_tenant_limit_before_runtime_spawn(
        tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    monkeypatch.setenv("PI_STORAGE_BYTES_PER_TENANT", "10")
    monkeypatch.setenv("PI_STORAGE_RESERVATION_PER_TURN", "11")
    with pytest.raises(ConfigError, match="must not exceed"):
        product_main.run()


def test_retention_configuration_rejects_zero_before_runtime_spawn(
        tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    monkeypatch.setenv("PI_RETENTION_DAYS", "0")
    with pytest.raises(ConfigError, match="PI_RETENTION_DAYS"):
        product_main.run()


def _install_happy_runtime(monkeypatch, *, scheduler_drains=True,
                           retention_drains=True):
    events = []

    class ImmediateEvent:
        def wait(self, timeout=None):
            events.append(("stop_wait", timeout))
            return True

        def set(self):
            events.append(("stop_set", None))

    class FakeMCP:
        is_healthy = True

        def initialize(self, timeout_s=None):
            # Fired by SupervisedRuntime.__enter__ now, not by run() itself:
            # the handshake belongs to whoever owns the connection's lifecycle.
            events.append(("mcp_initialize", None))

        def close(self):
            events.append(("mcp_close", None))

        _proc = SimpleNamespace(kill=lambda: None)

    class FakeRuntime:
        @classmethod
        def spawn(cls, path, timeout_s=90.0, generation=0):
            events.append(("mcp_spawn", (path, timeout_s)))
            return FakeMCP()

    class FakeAgent:
        manifest = {}
        memory = None

        def __init__(self, endpoint, api_key, **kwargs):
            events.append(("agent", (endpoint, api_key)))
            self.store = kwargs["store"]
            self.sandbox_root = kwargs["sandbox_root"]
            self.audit = kwargs["audit"]
            self._mcp = kwargs["mcp"]

    class FakeScheduler:
        def __init__(self, *args, **kwargs):
            events.append(("scheduler_init", None))

        def start(self):
            events.append(("scheduler_start", None))

        def stop(self, timeout):
            events.append(("scheduler_stop", timeout))
            return scheduler_drains

    class FakeRetention:
        def __init__(self, *args, **kwargs):
            events.append(("retention_init", None))

        def tick(self):
            events.append(("retention_tick", None))
            return True

        def start(self):
            events.append(("retention_start", None))

        @staticmethod
        def is_healthy():
            return True

        def stop(self, timeout):
            events.append(("retention_stop", timeout))
            return retention_drains

    class FakeServer:
        def shutdown(self):
            events.append(("server_shutdown", None))

        def server_close(self):
            events.append(("server_close", None))

    monkeypatch.setattr(product_main, "threading", SimpleNamespace(Event=ImmediateEvent))
    monkeypatch.setattr(product_main, "ProductionSigilMCP", FakeRuntime)
    # The forge is resolved through toolchain.py immediately before the spawn;
    # this test has no compiler and must not need one.
    fake_toolchain = SimpleNamespace(forge_bin=Path("/nonexistent/sigil-mcp"),
                                     origin="test double")
    monkeypatch.setattr(product_main.toolchain, "resolve",
                        lambda require=True: fake_toolchain)
    monkeypatch.setattr(product_main.toolchain, "verify_binary",
                        lambda path, rev=None: None)
    monkeypatch.setattr(product_main, "PiAgent", FakeAgent)
    monkeypatch.setattr(product_main, "ProductScheduler", FakeScheduler)
    monkeypatch.setattr(product_main, "ProductRetentionMonitor", FakeRetention)
    def serve(service, *args, **kwargs):
        events.append((
            "readiness_dependencies",
            service.readiness_snapshot()["dependencies"]))
        return FakeServer()

    monkeypatch.setattr(product_main, "serve_product", serve)
    monkeypatch.setattr(product_main, "load_system_prompt", lambda *args: None)
    return events


def test_product_happy_shutdown_writes_backup_consistency_marker(tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    events = _install_happy_runtime(monkeypatch)
    product_main.run()

    marker = json.loads(
        (tmp_path / "state" / ".product-clean-shutdown.json").read_text())
    assert marker["schema_version"] == SCHEMA_VERSION
    assert marker["product_version"]
    assert [name for name, _ in events].index("server_shutdown") < [
        name for name, _ in events].index("server_close")
    assert ("scheduler_stop", 120) in events
    assert ("retention_tick", None) in events
    assert ("retention_start", None) in events
    assert ("retention_stop", 120) in events
    dependencies = dict(events)["readiness_dependencies"]
    assert dependencies == {
        "audit_verification": True,
        "quota_store": True,
        "retention": True,
        "runtime": True,
        "schedule_store": True,
        "state_storage": True,
    }


def test_missing_toolchain_is_a_config_error_before_runtime_spawn(tmp_path, monkeypatch):
    """Resolution happens after every configuration check and before the
    compiler is spawned: an operator gets one actionable startup error naming
    the paths tried, and no runtime process is ever started for it."""
    _base_env(tmp_path, monkeypatch)
    events = _install_happy_runtime(monkeypatch)

    def missing(require=True):
        raise product_main.toolchain.ToolchainNotFound(
            "no SIGIL toolchain found. Tried, in order:\n  /nowhere/sigil-mcp")

    monkeypatch.setattr(product_main.toolchain, "resolve", missing)
    with pytest.raises(ConfigError, match="no SIGIL toolchain found"):
        product_main.run()
    assert all(name != "mcp_spawn" for name, _ in events)


def test_pinned_binary_mismatch_is_a_config_error_before_runtime_spawn(
        tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    events = _install_happy_runtime(monkeypatch)
    monkeypatch.setattr(product_main.toolchain, "verify_binary",
                        lambda path, rev=None: f"{path} does not match the pin")
    with pytest.raises(ConfigError, match="does not match SIGIL_REV"):
        product_main.run()
    assert all(name != "mcp_spawn" for name, _ in events)


def test_happy_runtime_spawns_the_resolved_forge_binary(tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    events = _install_happy_runtime(monkeypatch)
    product_main.run()
    assert ("mcp_spawn", (Path("/nonexistent/sigil-mcp"), 90)) in events


def test_every_compiler_generation_is_re_verified_against_the_pin(tmp_path, monkeypatch):
    """A respawn reads the binary off disk again. A host that swapped it
    underneath a running service must not have it silently adopted mid-flight,
    so the SIGIL_REV check runs per generation and not just at startup."""
    _base_env(tmp_path, monkeypatch)
    _install_happy_runtime(monkeypatch)
    problems = ["", "sigil-mcp does not match the pin for macos_arm64"]
    spawned = []

    def verify(path, rev=None):
        return problems[len(spawned)] or None

    class Recording:
        @classmethod
        def spawn(cls, path, timeout_s=90.0, generation=0):
            spawned.append(generation)
            return SimpleNamespace(initialize=lambda timeout_s=None: {},
                                   close=lambda: None, is_healthy=True,
                                   _proc=SimpleNamespace(kill=lambda: None))

    monkeypatch.setattr(product_main, "ProductionSigilMCP", Recording)
    runtime = product_main.build_runtime(Path("/nonexistent/sigil-mcp"), 90, verify)
    with runtime:
        assert spawned == [1]
        runtime._client = None            # the previous generation was retired
        with pytest.raises(ConfigError, match="does not match"):
            runtime.forge("next generation", timeout_s=30)
    assert spawned == [1], "a mismatched binary must never be spawned"


def test_failed_drain_never_authorizes_offline_backup(tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    _install_happy_runtime(monkeypatch, scheduler_drains=False)
    with pytest.raises(RuntimeError, match="did not drain"):
        product_main.run()
    assert not (tmp_path / "state" / ".product-clean-shutdown.json").exists()


def test_failed_retention_drain_never_authorizes_offline_backup(
        tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    _install_happy_runtime(monkeypatch, retention_drains=False)
    with pytest.raises(RuntimeError, match="did not drain"):
        product_main.run()
    assert not (tmp_path / "state" / ".product-clean-shutdown.json").exists()


def test_startup_refuses_customer_state_without_quota_registry(tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    sessions = tmp_path / "state" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / ("a" * 64 + ".kv")).write_text("[]")
    _install_happy_runtime(monkeypatch)
    with pytest.raises(ConfigError, match="supported state migration"):
        product_main.run()
