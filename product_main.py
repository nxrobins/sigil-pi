#!/usr/bin/env python3
"""Production entry point for sigil-pi's authenticated v1 HTTP service."""

import itertools
import os
import json
import signal
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import toolchain

# The bundle ships docs/support-matrix.md, which lists Python <3.12 and >3.14 as
# unsupported and requires that "unsupported selections must fail startup where
# the process can detect them"; the launcher runs whatever python3 the host has.
# Nothing enforced it at first, so a bundle would happily start, bind a port and
# serve real signed-audit turns on an interpreter its own packaged documentation
# forbids. Then only the floor was checked, and the product started and served on
# Python 3.15.1 (audit F-24). Both bounds now come from the one definition in
# toolchain.py. Checked at import, before any work.
if not (toolchain.MINIMUM_PYTHON <= sys.version_info[:2]
        < toolchain.FIRST_UNSUPPORTED_PYTHON):
    raise SystemExit(toolchain.unsupported_python_message(sys.version_info))

from agent import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_THINKING,
    MAX_HISTORY_BYTES,
    MAX_STEPS,
    MAX_TOOL_RESULT_BYTES,
    PI_ROOT,
    THINKING_MODES,
    AuditLog,
    PiAgent,
    SessionStore,
    _env_int,
    _parse_allowlist,
    ensure_directory,
    load_system_prompt,
    scratch_files,
    sync_directory,
    verify_audit_dir,
    write_durably,
)
from product_service import (
    AuditVerificationMonitor,
    AuthRegistry,
    CompilerProcesses,
    ConfigError,
    InFlightWork,
    MAX_CONNECTIONS,
    MIN_SCHEDULE_EVERY_MS,
    REQUEST_DEADLINE_SECONDS,
    ProductService,
    ProductScheduler,
    ProductScheduleStore,
    ProductDataManager,
    ProductRetentionMonitor,
    SessionOperationLocks,
    DurableQuotaStore,
    TenantSecrets,
    drain_service,
    serve_product,
    tls_context,
    validate_transport,
)
from runtime_client import ProductionSigilMCP, SupervisedRuntime, RuntimePool
from state_tool import CLEAN_MARKER, SCHEMA_VERSION

# The daily quota and the backward-compatible default for the independent
# turn ceiling. Product turns debit each model attempt; the old fixed
# reservation remains available to embedders that have not opted in.
DEFAULT_TOKENS_PER_DAY = 10_000_000
DEFAULT_TOKEN_RESERVATION_PER_TURN = 1_000_000


def build_runtime(forge_bin, timeout_s, verify, *, processes=None, pool_size=1):
    """The product's forging endpoint: one address over many compilers.

    A forge that outruns its turn's deadline is killed — that is what the hard
    deadline promises, and the pinned protocol offers no gentler interrupt. The
    connection dies with it, so the endpoint replaces it instead of dying too;
    before this, one tenant's expired turn ended forging for every tenant until
    the process was restarted.

    ``verify`` runs on EVERY generation. A respawn re-reads the binary from
    disk, and a toolchain swapped underneath a running service must not be
    adopted silently — the SIGIL_REV pin is a claim about what is executing
    now, not about what was executing at startup.

    Every compiler is spawned here, so registering each with ``processes``
    (a CompilerProcesses) lets the metrics measure all of them, whichever
    runtime holds them (audit F-18).
    """
    generations = itertools.count(1)

    def spawn():
        problem = verify(forge_bin)
        if problem:
            raise ConfigError(f"forge binary does not match SIGIL_REV: {problem}")
        client = ProductionSigilMCP.spawn(
            forge_bin, timeout_s=timeout_s, generation=next(generations))
        return client if processes is None else processes.track(client)

    # A cold extracted binary can take longer than five seconds to start
    # under host verification/load. Use the configured MCP bound at startup;
    # replacements also remain bounded by the requesting turn's residue.
    if type(pool_size) is int and pool_size == 1:
        return SupervisedRuntime(spawn, handshake_timeout_s=timeout_s)
    return RuntimePool(spawn, size=pool_size, handshake_timeout_s=timeout_s)


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


def _endpoint_host(endpoint):
    """The provider's host name and nothing else from PI_ENDPOINT, which can
    carry a user name, a password, a path or a query."""
    try:
        return urllib.parse.urlsplit(endpoint).hostname
    except ValueError:
        return None


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
    # Tool credentials and the hosts tools may reach are each tenant's (audit
    # F-09): a principal's policy names them, and the values are its tenant's,
    # under PI_TENANT_SECRETS_DIR. The deployment-wide settings the research
    # host reads gave every tenant's tools the operator's, so a deployment
    # that still sets them stops here rather than sharing them, or dropping
    # them without a word. Set-but-empty is unset, as the research host reads it.
    shared = sorted(name for name, value in os.environ.items()
                    if name.startswith("PI_SECRET_") and name != "PI_SECRET_" and value)
    if shared:
        raise ConfigError(
            f"{', '.join(shared)} would give every tenant's tools the same credential, "
            f"and product mode refuses it: name each secret in the \"secrets\" of the "
            f"principals in PI_AUTH_FILE that may use it, and put each tenant's value "
            f"in PI_TENANT_SECRETS_DIR/<SHA-256 of the tenant>/<name>")
    if _parse_allowlist(os.environ.get("PI_NET_ALLOWLIST", "")):
        raise ConfigError(
            "PI_NET_ALLOWLIST would let every tenant's fetch reach the same hosts, and "
            "product mode refuses it: list the hosts in the \"net_allowlist\" of the "
            "principals in PI_AUTH_FILE that may reach them")
    max_credential_days = _positive_env_int("PI_MAX_CREDENTIAL_LIFETIME_DAYS", 90)
    auth = AuthRegistry.from_file(
        _required_env("PI_AUTH_FILE"), require_expiry=True,
        max_lifetime_s=max_credential_days * 86400)
    # Read once, with its permissions checked: a policy naming a secret its
    # tenant has no value for stops startup here, before any state is opened.
    tenant_secrets = TenantSecrets.from_directory(
        os.environ.get("PI_TENANT_SECRETS_DIR") or None, auth.principals)
    endpoint = os.environ.get("PI_ENDPOINT", "https://api.anthropic.com/v1/messages")
    model = os.environ.get("PI_MODEL", "claude-sonnet-5")
    host = os.environ.get("PI_HOST", "127.0.0.1")
    port = _positive_env_int("PI_PORT", 8080)
    socket_timeout = _positive_env_int("PI_SOCKET_TIMEOUT_SECONDS", 30)
    # A request's handshake, head and body must arrive within the deadline,
    # and at most PI_MAX_CONNECTIONS connections are handled at a time (audit
    # F-08). Keep the cap well below the process's open-file limit.
    request_deadline = _positive_env_int(
        "PI_REQUEST_DEADLINE_SECONDS", REQUEST_DEADLINE_SECONDS)
    max_connections = _positive_env_int("PI_MAX_CONNECTIONS", MAX_CONNECTIONS)
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
    # The agent's defaults, not the product's own: the product kept 1,024
    # after the agent's cap became 16,000 (audit F-10).
    max_tokens = _positive_env_int("PI_MAX_TOKENS", DEFAULT_MAX_TOKENS)
    # Sent as `thinking` on every model request. No value suits every model
    # (agent.py, DEFAULT_THINKING), so the operator names it. The agent
    # refuses a value it does not have, but only once the state directory
    # and a compiler exist; refuse it here, with the other settings.
    thinking = os.environ.get("PI_THINKING") or DEFAULT_THINKING
    if thinking not in THINKING_MODES:
        raise ConfigError(
            f"PI_THINKING must be one of {', '.join(THINKING_MODES)}, got {thinking!r}")
    token_reservation = _positive_env_int(
        "PI_TOKEN_RESERVATION_PER_TURN", DEFAULT_TOKEN_RESERVATION_PER_TURN)
    max_turn_tokens = _positive_env_int("PI_MAX_TOKENS_PER_TURN", token_reservation)
    pool_size = _positive_env_int("PI_COMPILER_POOL_SIZE", 4)
    if pool_size > 32:
        raise ConfigError("PI_COMPILER_POOL_SIZE must not exceed 32")
    http_timeout_ms = _positive_env_int("PI_MODEL_HTTP_TIMEOUT_MS", 60_000)
    if http_timeout_ms > 60_000:
        raise ConfigError("PI_MODEL_HTTP_TIMEOUT_MS must not exceed 60000")
    quota_config = {
        "requests_per_minute": _positive_env_int("PI_REQUESTS_PER_MINUTE", 60),
        "max_concurrent_turns": _positive_env_int("PI_MAX_CONCURRENT_TURNS", 2),
        "turn_lease_s": turn_lease,
        "tokens_per_day": _positive_env_int(
            "PI_TOKENS_PER_DAY", DEFAULT_TOKENS_PER_DAY),
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
    # Retain a valid legacy fixed reservation for older embedders/readers.
    # Product turns use only per-attempt debits and the independent ceiling.
    quota_config["token_reservation_per_turn"] = min(
        token_reservation, quota_config["tokens_per_day"])
    for reservation, limit in (
            ("storage_reservation_per_turn", "storage_bytes_per_tenant"),
            ("audit_reservation_per_turn", "audit_bytes_per_tenant")):
        if quota_config[reservation] > quota_config[limit]:
            raise ConfigError(
                f"PI_{reservation.upper()} must not exceed PI_{limit.upper()}")
    mcp_timeout = _positive_env_int("PI_MCP_TIMEOUT_SECONDS", 90)
    audit_verify_interval = _positive_env_int("PI_AUDIT_VERIFY_SECONDS", 60)
    max_schedules = _positive_env_int("PI_MAX_SCHEDULES_PER_TENANT", 32)
    max_export_bytes = _positive_env_int("PI_MAX_EXPORT_BYTES", 10 * 1024 * 1024)
    llm_retries = _env_int("PI_LLM_RETRIES", 2)
    # The configuration in force, as the metrics report it (audit F-18): a
    # drill or load report can then say what it measured. Built from the
    # values handed to the components below, and never from a secret, a
    # credential or a path; tests/test_operations_metrics.py ties every
    # variable read here to a line of this block or to its reason for absence.
    effective_settings = {
        "model": model,
        "endpoint_host": _endpoint_host(endpoint),
        "max_credential_lifetime_days": max_credential_days,
        "socket_timeout_seconds": socket_timeout,
        "request_deadline_seconds": request_deadline,
        "max_connections": max_connections,
        "drain_timeout_seconds": drain_timeout,
        "turn_deadline_seconds": turn_deadline,
        "turn_lease_seconds": turn_lease,
        "mcp_timeout_seconds": mcp_timeout,
        "audit_verify_seconds": audit_verify_interval,
        "retention_days": retention_days,
        "retention_interval_seconds": retention_interval,
        "max_history_bytes": max_history_bytes,
        "max_tool_result_bytes": max_tool_result_bytes,
        "max_steps": max_steps,
        "max_tokens": max_tokens,
        "max_tokens_per_turn": max_turn_tokens,
        "compiler_pool_size": pool_size,
        "model_http_timeout_ms": http_timeout_ms,
        "thinking": thinking,
        "llm_retries": llm_retries,
        **{key: quota_config[key] for key in (
            "requests_per_minute", "max_concurrent_turns", "tokens_per_day",
            "token_reservation_per_turn", "storage_bytes_per_tenant",
            "storage_reservation_per_turn", "audit_bytes_per_tenant",
            "audit_reservation_per_turn")},
        "max_schedules_per_tenant": max_schedules,
        "max_export_bytes": max_export_bytes,
    }
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
    # an inconsistent copy. The removal is synced, or a power loss could bring
    # the marker back to vouch for state that was not drained (audit F-13).
    # What a stop killed while writing the marker left goes with it:
    # write_durably's scratch, and the `.tmp` name older releases used.
    ensure_directory(state_dir)
    clean_marker = state_dir / CLEAN_MARKER
    for stale in (clean_marker, clean_marker.with_suffix(".tmp"),
                  *scratch_files(clean_marker)):
        stale.unlink(missing_ok=True)
    sync_directory(state_dir)
    clean_shutdown = False

    # The forge is resolved through the same seam as the research host
    # (toolchain.py): the release bundle's launcher points SIGIL_ROOT at its
    # embedded runtime, which has the source layout, so nothing here knows
    # which arrangement answered. Resolved AFTER every configuration check
    # above and BEFORE the compiler is spawned: a missing or half-installed
    # toolchain is a configuration error naming every path tried, and a
    # binary that does not match SIGIL_REV's sha256 pin (once one is
    # published for this platform) is refused rather than run.
    try:
        runtime = toolchain.resolve()
    except toolchain.ToolchainNotFound as e:
        raise ConfigError(str(e)) from e
    stop = threading.Event()

    def request_stop(signum, frame):
        del signum, frame
        stop.set()

    compilers = CompilerProcesses()
    old_term = signal.signal(signal.SIGTERM, request_stop)
    old_int = signal.signal(signal.SIGINT, request_stop)
    try:
        with build_runtime(
                runtime.forge_bin, mcp_timeout, toolchain.verify_binary,
                processes=compilers, pool_size=pool_size) as mcp:
            store = SessionStore(state_dir / "sessions")
            sandbox_root = ensure_directory(state_dir / "sandboxes")
            agent = PiAgent(
                endpoint, api_key, store=store, sandbox_root=sandbox_root,
                mcp=mcp, model=model,
                max_tokens=max_tokens,
                thinking=thinking,
                http_timeout_ms=http_timeout_ms,
                # None deployment-wide: each turn is given its principal's,
                # and a principal's turn never falls back to these (F-09).
                secrets={},
                net_allowlist=[],
                max_history_bytes=max_history_bytes,
                max_tool_result_bytes=max_tool_result_bytes,
                max_steps=max_steps,
                # Strict, and not a setting (audit F-04): a forge whose record
                # cannot be written fails with its result withheld, and the
                # turn answers 500 audit_write_failed instead of reporting
                # work that has no record.
                audit=AuditLog(state_dir / "audit", key=audit_key, strict=True),
                system_prompt=load_system_prompt(
                    os.environ.get("PI_SYSTEM"),
                    Path(os.environ.get("PI_SYSTEM_FILE", PI_ROOT / "AGENTS.md"))),
                llm_retries=llm_retries,
                memory=None,
            )
            # Refuses an every_ms below one minute (audit F-06). A schedule an
            # earlier release stored below it loads as written and runs no
            # more often than that.
            schedule_store = ProductScheduleStore(
                state_dir / "product-schedules.json", max_per_tenant=max_schedules,
                min_every_ms=MIN_SCHEDULE_EVERY_MS)
            session_locks = SessionOperationLocks(state_dir / "locks")
            quota_store = DurableQuotaStore(
                state_dir / "product-quotas.sqlite3", **quota_config)
            data_manager = ProductDataManager(
                agent, schedule_store, session_locks,
                max_export_bytes=max_export_bytes, quota_store=quota_store)
            # Refuses files the registry cannot attribute, removes the scratch
            # killed saves left, and reports (a critical event; startup goes
            # on) any row whose recorded files are gone.
            data_manager.reconcile_at_startup()
            audit_monitor = AuditVerificationMonitor(
                lambda: verify_audit_dir(state_dir / "audit", key=audit_key),
                interval_s=audit_verify_interval)
            if not audit_monitor.check():
                raise ConfigError("existing audit chains failed signature verification")
            retention_monitor = ProductRetentionMonitor(
                quota_store, data_manager, schedule_store, session_locks,
                retention_s=retention_days * 86400,
                interval_s=retention_interval,
                precheck=audit_monitor.check)
            if not retention_monitor.tick():
                raise ConfigError("initial product retention sweep failed")
            # One count of work in flight for the listener's requests and the
            # scheduler's turns: draining refuses both and waits for both.
            work = InFlightWork()
            scheduler = ProductScheduler(
                schedule_store, agent, auth, session_locks=session_locks,
                quota_store=quota_store, lease_s=turn_lease,
                data_manager=data_manager,
                turn_deadline_s=turn_deadline, work=work,
                # A scheduled turn's record that could not be written is
                # counted where a chat turn's is, and fails readiness.
                audit_monitor=audit_monitor,
                tenant_secrets=tenant_secrets, max_tokens_per_turn=max_turn_tokens)
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
                compiler_pids=compilers.pids,
                effective_settings=effective_settings,
                # Readiness and the metrics report this scheduler's liveness.
                scheduler=scheduler,
                work=work,
                tenant_secrets=tenant_secrets, max_tokens_per_turn=max_turn_tokens,
            )
            server = serve_product(service, host=host, port=port,
                                   ssl_context=tls, socket_timeout_s=socket_timeout,
                                   request_deadline_s=request_deadline,
                                   max_connections=max_connections)
            audit_monitor.start()
            retention_monitor.start()
            scheduler.start()
            scheme = "https" if tls else "http"
            print(f"sigil-pi {_version()} product API on {scheme}://{host}:{port}",
                  file=sys.stderr)
            stop.wait()

            # Fail readiness and refuse new work before the listener stops,
            # then wait for the listener, the three loops and the work in
            # flight, all at once, on the one budget PI_DRAIN_TIMEOUT_SECONDS.
            if not drain_service(
                    service, server, (scheduler, retention_monitor, audit_monitor),
                    drain_timeout):
                raise RuntimeError(
                    f"work in flight did not drain within {drain_timeout} seconds")
            clean_shutdown = True
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
        if clean_shutdown:
            # Synced like the state it vouches for, so that a power loss
            # after a clean stop cannot leave the marker missing or partial.
            write_durably(clean_marker, (json.dumps({
                "schema_version": SCHEMA_VERSION,
                "product_version": _version(),
                "clean_shutdown_unix": int(time.time()),
            }, sort_keys=True, separators=(",", ":")) + "\n").encode())
            clean_marker.chmod(0o600)


def main():
    try:
        run()
    except ConfigError as e:
        sys.exit(f"configuration error: {e}")


if __name__ == "__main__":
    main()
