#!/usr/bin/env python3
"""Production entry point for sigil-pi's authenticated v1 HTTP service."""

import os
import json
import signal
import sys
import threading
import time
from pathlib import Path

from agent import (
    MAX_HISTORY_BYTES,
    MAX_STEPS,
    MAX_TOOL_RESULT_BYTES,
    PI_ROOT,
    SIGIL_ROOT,
    AuditLog,
    PiAgent,
    SessionStore,
    _env_int,
    _parse_allowlist,
    load_system_prompt,
    secrets_from_env,
    verify_audit_dir,
)
from product_service import (
    AuditVerificationMonitor,
    AuthRegistry,
    ConfigError,
    ProductService,
    ProductScheduler,
    ProductScheduleStore,
    ProductDataManager,
    ProductRetentionMonitor,
    SessionOperationLocks,
    DurableQuotaStore,
    serve_product,
    tls_context,
    validate_transport,
)
from runtime_client import ProductionSigilMCP
from state_tool import CLEAN_MARKER, SCHEMA_VERSION


def _required_env(name):
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"{name} is required for the product service")
    return value


def _positive_env_int(name, default):
    value = _env_int(name, default)
    if value <= 0:
        raise ConfigError(f"{name} must be positive, got {value}")
    return value


def _version():
    value = (PI_ROOT / "VERSION").read_text().strip()
    if not value:
        raise ConfigError("VERSION must not be empty")
    return value


def _server_tls_context():
    cert = os.environ.get("PI_TLS_CERT")
    key = os.environ.get("PI_TLS_KEY")
    if bool(cert) != bool(key):
        raise ConfigError("PI_TLS_CERT and PI_TLS_KEY must be configured together")
    return tls_context(cert, key) if cert else None


def run():
    """Construct and run the product service until SIGINT/SIGTERM."""
    state_dir = Path(os.environ.get("PI_STATE", PI_ROOT / ".pi-state"))
    api_key = _required_env("ANTHROPIC_API_KEY")
    audit_key = _required_env("PI_AUDIT_KEY").encode()
    if len(audit_key) < 32:
        raise ConfigError("PI_AUDIT_KEY must be at least 32 bytes")
    max_credential_days = _positive_env_int("PI_MAX_CREDENTIAL_LIFETIME_DAYS", 90)
    auth = AuthRegistry.from_file(
        _required_env("PI_AUTH_FILE"), require_expiry=True,
        max_lifetime_s=max_credential_days * 86400)
    endpoint = os.environ.get("PI_ENDPOINT", "https://api.anthropic.com/v1/messages")
    host = os.environ.get("PI_HOST", "127.0.0.1")
    port = _positive_env_int("PI_PORT", 8080)
    socket_timeout = _positive_env_int("PI_SOCKET_TIMEOUT_SECONDS", 30)
    drain_timeout = _positive_env_int("PI_DRAIN_TIMEOUT_SECONDS", 120)
    turn_deadline = _positive_env_int("PI_TURN_DEADLINE_SECONDS", 120)
    turn_lease = _positive_env_int("PI_TURN_LEASE_SECONDS", 300)
    retention_days = _positive_env_int("PI_RETENTION_DAYS", 90)
    retention_interval = _positive_env_int(
        "PI_RETENTION_INTERVAL_SECONDS", 3600)
    if turn_lease <= turn_deadline:
        raise ConfigError(
            "PI_TURN_LEASE_SECONDS must be greater than PI_TURN_DEADLINE_SECONDS")
    max_history_bytes = _positive_env_int("PI_MAX_HISTORY_BYTES", MAX_HISTORY_BYTES)
    max_tool_result_bytes = _positive_env_int(
        "PI_MAX_TOOL_RESULT_BYTES", MAX_TOOL_RESULT_BYTES)
    max_steps = _positive_env_int("PI_MAX_STEPS", MAX_STEPS)
    max_tokens = _positive_env_int("PI_MAX_TOKENS", 1024)
    token_reservation = _positive_env_int(
        "PI_TOKEN_RESERVATION_PER_TURN", 4_000_000)
    if token_reservation < max_steps * max_tokens:
        raise ConfigError(
            "PI_TOKEN_RESERVATION_PER_TURN must cover PI_MAX_STEPS * PI_MAX_TOKENS")
    quota_config = {
        "requests_per_minute": _positive_env_int("PI_REQUESTS_PER_MINUTE", 60),
        "max_concurrent_turns": _positive_env_int("PI_MAX_CONCURRENT_TURNS", 2),
        "turn_lease_s": turn_lease,
        "tokens_per_day": _positive_env_int("PI_TOKENS_PER_DAY", 10_000_000),
        "token_reservation_per_turn": token_reservation,
        "storage_bytes_per_tenant": _positive_env_int(
            "PI_STORAGE_BYTES_PER_TENANT", 1024 * 1024 * 1024),
        "storage_reservation_per_turn": _positive_env_int(
            "PI_STORAGE_RESERVATION_PER_TURN", 16 * 1024 * 1024),
        "audit_bytes_per_tenant": _positive_env_int(
            "PI_AUDIT_BYTES_PER_TENANT", 256 * 1024 * 1024),
        "audit_reservation_per_turn": _positive_env_int(
            "PI_AUDIT_RESERVATION_PER_TURN", 1024 * 1024),
    }
    for reservation, limit in (
            ("token_reservation_per_turn", "tokens_per_day"),
            ("storage_reservation_per_turn", "storage_bytes_per_tenant"),
            ("audit_reservation_per_turn", "audit_bytes_per_tenant")):
        if quota_config[reservation] > quota_config[limit]:
            raise ConfigError(
                f"PI_{reservation.upper()} must not exceed PI_{limit.upper()}")
    tls = _server_tls_context()
    validate_transport(host, tls)

    memory_bin = os.environ.get("PI_MEMORY_SIDECAR")
    if memory_bin:
        raise ConfigError(
            "PI_MEMORY_SIDECAR is not supported by product v1 until memory "
            "export, deletion, backup, and restore are implemented")

    # An offline backup is accepted only after this process has completed a
    # clean drain. Remove the previous proof before opening any product state;
    # a crash or failed drain therefore leaves no marker that could authorize
    # an inconsistent copy.
    state_dir.mkdir(parents=True, exist_ok=True)
    clean_marker = state_dir / CLEAN_MARKER
    clean_marker.unlink(missing_ok=True)
    clean_shutdown = False

    stop = threading.Event()

    def request_stop(signum, frame):
        del signum, frame
        stop.set()

    old_term = signal.signal(signal.SIGTERM, request_stop)
    old_int = signal.signal(signal.SIGINT, request_stop)
    try:
        with ProductionSigilMCP.spawn(
                SIGIL_ROOT / "target" / "release" / "sigil-mcp",
                timeout_s=_positive_env_int("PI_MCP_TIMEOUT_SECONDS", 90)) as mcp:
            mcp.initialize()
            store = SessionStore(state_dir / "sessions")
            sandbox_root = state_dir / "sandboxes"
            sandbox_root.mkdir(parents=True, exist_ok=True)
            agent = PiAgent(
                endpoint, api_key, store=store, sandbox_root=sandbox_root,
                mcp=mcp, model=os.environ.get("PI_MODEL", "claude-sonnet-5"),
                max_tokens=max_tokens,
                net_allowlist=_parse_allowlist(os.environ.get("PI_NET_ALLOWLIST", "")),
                max_history_bytes=max_history_bytes,
                max_tool_result_bytes=max_tool_result_bytes,
                max_steps=max_steps,
                secrets=secrets_from_env(),
                audit=AuditLog(state_dir / "audit", key=audit_key),
                system_prompt=load_system_prompt(
                    os.environ.get("PI_SYSTEM"),
                    Path(os.environ.get("PI_SYSTEM_FILE", PI_ROOT / "AGENTS.md"))),
                llm_retries=_env_int("PI_LLM_RETRIES", 2),
                memory=None,
            )
            schedule_store = ProductScheduleStore(
                state_dir / "product-schedules.json",
                max_per_tenant=_positive_env_int(
                    "PI_MAX_SCHEDULES_PER_TENANT", 32))
            session_locks = SessionOperationLocks(state_dir / "locks")
            quota_store = DurableQuotaStore(
                state_dir / "product-quotas.sqlite3", **quota_config)
            data_manager = ProductDataManager(
                agent, schedule_store, session_locks,
                max_export_bytes=_positive_env_int(
                    "PI_MAX_EXPORT_BYTES", 10 * 1024 * 1024),
                quota_store=quota_store)
            registered_sessions = quota_store.registered_sessions()
            data_manager.verify_quota_registry(registered_sessions)
            quota_store.reconcile_registered(data_manager.footprint)
            audit_monitor = AuditVerificationMonitor(
                lambda: verify_audit_dir(state_dir / "audit", key=audit_key),
                interval_s=_positive_env_int("PI_AUDIT_VERIFY_SECONDS", 60))
            if not audit_monitor.check():
                raise ConfigError("existing audit chains failed signature verification")
            retention_monitor = ProductRetentionMonitor(
                quota_store, data_manager, schedule_store, session_locks,
                retention_s=retention_days * 86400,
                interval_s=retention_interval,
                precheck=audit_monitor.check)
            if not retention_monitor.tick():
                raise ConfigError("initial product retention sweep failed")
            service = ProductService(
                agent, auth, version=_version(),
                schedule_store=schedule_store,
                session_locks=session_locks,
                data_manager=data_manager,
                quota_store=quota_store,
                audit_monitor=audit_monitor,
                retention_monitor=retention_monitor,
                turn_deadline_s=turn_deadline,
                resource_root=state_dir,
            )
            scheduler = ProductScheduler(
                schedule_store, agent, auth, session_locks=session_locks,
                quota_store=quota_store, lease_s=turn_lease,
                data_manager=data_manager,
                turn_deadline_s=turn_deadline)
            server = serve_product(service, host=host, port=port,
                                   ssl_context=tls, socket_timeout_s=socket_timeout)
            audit_monitor.start()
            retention_monitor.start()
            scheduler.start()
            scheme = "https" if tls else "http"
            print(f"sigil-pi {_version()} product API on {scheme}://{host}:{port}",
                  file=sys.stderr)
            stop.wait()

            # Fail readiness and reject new turns before the listener stops.
            service.start_draining()
            server.shutdown()
            scheduler_drained = scheduler.stop(drain_timeout)
            retention_drained = retention_monitor.stop(drain_timeout)
            audit_monitor_drained = audit_monitor.stop(drain_timeout)
            requests_drained = service.wait_for_drain(drain_timeout)
            server.server_close()
            if (not scheduler_drained or not retention_drained
                    or not audit_monitor_drained or not requests_drained):
                raise RuntimeError(
                    f"active turns did not drain within {drain_timeout} seconds")
            clean_shutdown = True
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
        if clean_shutdown:
            marker_tmp = clean_marker.with_suffix(".tmp")
            marker_tmp.write_text(json.dumps({
                "schema_version": SCHEMA_VERSION,
                "product_version": _version(),
                "clean_shutdown_unix": int(time.time()),
            }, sort_keys=True, separators=(",", ":")) + "\n")
            marker_tmp.chmod(0o600)
            marker_tmp.replace(clean_marker)


def main():
    try:
        run()
    except ConfigError as e:
        sys.exit(f"configuration error: {e}")


if __name__ == "__main__":
    main()
