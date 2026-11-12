"""F-18: the operations metrics measure the compiler and record the settings in force.

Audit 2026-11-10, F-18: "The memory, thread and file-descriptor bounds measure
the Python host (product_service.py:1928), not the compiler process where
guests run." ``ProductService.resource_snapshot`` read the host's own
``getrusage``, ``threading.active_count()`` and descriptor table, so a guest
that grew the compiler moved none of them, and the capacity run's resource
criterion (docs/capacity.md, criterion 6) was evidence about the wrong
process. Nor did anything record the configuration in force: the failure
drill ran with a 60-second lease and one model retry and its report did not
say so, and a load report cannot say which provider, model or limits it
measured.

So these tests boot ``product_main.run()`` the way an operator starts it, over
a REAL compiler process (a stand-in that can grow on command, or the pinned
compiler itself), and read ``/v1/metrics`` the way the load harness does:

- the compiler's resident memory, threads and open descriptors are reported as
  totals over the live compiler processes, with their count; they move when
  the compiler grows, and equal the compiler's own count of itself;
- a dead compiler is not counted and its replacement is; several compilers
  (the pool that A5 builds) are summed;
- ``effective_settings`` reports the model, the provider's host and every
  limit and deadline, exactly as the components received them, and no secret;
- a guard ties every environment variable ``product_main`` reads to a
  reported setting or to a stated reason for leaving it out.
"""

import ast
import gc
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import textwrap
import time
import weakref
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from conftest import PI_ROOT, needs_toolchain

import product_main
import runtime_client
from product_service import AuthRegistry, ConfigError, ProductService

MIB = 1024 * 1024
GROW_BYTES = 64 * MIB   # what the stand-in compiler allocates on "GROW"
GROW_THREADS = 8        # ... the threads it starts
GROW_FDS = 32           # ... and the descriptors it opens
OPS_TOKEN = "p4-operations-token-with-test-only-entropy-0123456789"
PROVIDER_KEY = "provider-key-for-a-test-that-never-calls-the-provider"
AUDIT_KEY = "audit-key-for-the-operations-metrics-tests-0123456789"

# A stand-in sigil-mcp: a real child process speaking the protocol over real
# pipes. "GROW" makes it allocate, start threads and open descriptors, so the
# test knows exactly how the compiler changed; every answer carries the
# process's own count of itself, which is the oracle the metrics must match.
_STAND_IN = '''\
import json, os, sys, threading, time

held = []


def open_fds():
    count = 0
    for fd in range(4096):
        try:
            os.fstat(fd)
        except OSError:
            continue
        count += 1
    return count


def grow():
    held.append(os.urandom(GROW_BYTES))     # incompressible, so it stays resident
    for _ in range(GROW_THREADS):
        thread = threading.Thread(target=time.sleep, args=(3600,), daemon=True)
        thread.start()
        held.append(thread)
    for _ in range(GROW_FDS):
        held.append(os.open(os.devnull, os.O_RDONLY))


while True:
    line = sys.stdin.readline()
    if not line:
        break
    request = json.loads(line)
    result = {}
    if request["method"] == "tools/call":
        if request["params"]["arguments"]["source"] == "GROW":
            grow()
        me = {"pid": os.getpid(), "threads": threading.active_count(),
              "fds": open_fds()}
        envelope = {"status": "ok", "data": {"output_text": json.dumps(me)}}
        result = {"content": [{"type": "text", "text": json.dumps(envelope)}]}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                                 "result": result}) + "\\n")
    sys.stdout.flush()
'''


def _stand_in(tmp_path):
    """Launched through /bin/sh, because a shebang line has a length limit
    this venv's interpreter path can exceed. ``exec`` makes the measured
    process the interpreter itself, not a shell."""
    script = tmp_path / "stand_in_compiler.py"
    script.write_text(
        f"GROW_BYTES = {GROW_BYTES}\nGROW_THREADS = {GROW_THREADS}\n"
        f"GROW_FDS = {GROW_FDS}\n" + _STAND_IN)
    launcher = tmp_path / "sigil-mcp"
    launcher.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))}\n")
    launcher.chmod(0o755)
    return launcher


def _operator():
    return {"sha256": hashlib.sha256(OPS_TOKEN.encode()).hexdigest(),
            "principal": "operator", "tenant": "operations",
            "scopes": ["ops:read"], "tools": []}


def _credentials(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "auth.json"
    now = int(time.time())
    path.write_text(json.dumps({"tokens": [{
        **_operator(), "not_before_unix": now - 60, "expires_unix": now + 86400}]}))
    return path


def _boot(tmp_path, monkeypatch, during, *, forge_bin=None, env=(),
          auth_dir=None, state_dir=None):
    """Run ``product_main.run()`` until it serves, call ``during(service)``,
    then let it drain and stop. Returns what each component was given.

    The compiler is a real process: the stand-in at ``forge_bin``, or the
    pinned compiler when ``forge_bin`` is None. The provider is never called,
    the scheduler and the retention sweep are idle doubles, and the listener
    is replaced by ``during``; everything else is the product's own."""
    seen = SimpleNamespace(agent=None, spawn_timeouts=[], scheduler=None,
                           retention=None, serve=None, drains=[], auth=None,
                           service=None, result=None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("PI_AUDIT_KEY", AUDIT_KEY)
    monkeypatch.setenv(
        "PI_AUTH_FILE", str(_credentials(auth_dir or tmp_path / "auth")))
    monkeypatch.setenv("PI_STATE", str(state_dir or tmp_path / "state"))
    for name, value in dict(env).items():
        monkeypatch.setenv(name, value)

    class ImmediateStop:
        def wait(self, timeout=None):
            return True

        def set(self):
            pass

    class RecordingAgent:
        manifest = {}
        memory = None

        def __init__(self, endpoint, api_key, **kwargs):
            seen.agent = {"endpoint": endpoint, **kwargs}
            self.store = kwargs["store"]
            self.sandbox_root = kwargs["sandbox_root"]
            self.audit = kwargs["audit"]
            self._mcp = kwargs["mcp"]

    def spawn(binary_path, timeout_s=90.0, generation=0, **kwargs):
        seen.spawn_timeouts.append(timeout_s)
        return runtime_client.ProductionSigilMCP.spawn(
            binary_path, timeout_s=timeout_s, generation=generation, **kwargs)

    class IdleScheduler:
        def __init__(self, *args, **kwargs):
            seen.scheduler = kwargs

        def start(self):
            pass

        def stop(self, timeout_s):
            seen.drains.append(("scheduler", timeout_s))
            return True

    class IdleRetention:
        def __init__(self, *args, **kwargs):
            seen.retention = kwargs

        def tick(self):
            return True

        def start(self):
            pass

        @staticmethod
        def is_healthy():
            return True

        @staticmethod
        def snapshot():
            return {"healthy": True}

        def stop(self, timeout_s):
            seen.drains.append(("retention", timeout_s))
            return True

    class Listener:
        def shutdown(self):
            pass

        def server_close(self):
            pass

    def serve(service, **kwargs):
        seen.serve = kwargs
        seen.service = service
        seen.result = during(service)
        return Listener()

    def from_file(path, **kwargs):
        seen.auth = kwargs
        return AuthRegistry.from_file(path, **kwargs)

    if forge_bin is not None:
        monkeypatch.setattr(
            product_main.toolchain, "resolve",
            lambda require=True: SimpleNamespace(forge_bin=forge_bin, origin="stand-in"))
        monkeypatch.setattr(
            product_main.toolchain, "verify_binary", lambda path, rev=None: None)
    monkeypatch.setattr(product_main, "threading", SimpleNamespace(Event=ImmediateStop))
    monkeypatch.setattr(product_main, "PiAgent", RecordingAgent)
    monkeypatch.setattr(product_main, "ProductionSigilMCP", SimpleNamespace(spawn=spawn))
    monkeypatch.setattr(product_main, "ProductScheduler", IdleScheduler)
    monkeypatch.setattr(product_main, "ProductRetentionMonitor", IdleRetention)
    monkeypatch.setattr(product_main, "serve_product", serve)
    monkeypatch.setattr(product_main, "AuthRegistry", SimpleNamespace(from_file=from_file))
    monkeypatch.setattr(product_main, "load_system_prompt", lambda *args: None)
    product_main.run()
    return seen


def _ops():
    return {"Authorization": f"Bearer {OPS_TOKEN}"}


def _metrics(service):
    """``GET /v1/metrics`` as a client receives it: through JSON."""
    status, _, body = service.dispatch("GET", "/v1/metrics", _ops())
    assert status == 200, body
    return json.loads(json.dumps(body))["metrics"]


def _prometheus(service):
    status, _, payload = service.dispatch("GET", "/v1/metrics/prometheus", _ops())
    assert status == 200, payload
    return payload.body


def _forge(runtime, command):
    """The stand-in's own count of itself, after it handled ``command``."""
    result = runtime.forge(command, timeout_s=30)
    return json.loads(result["data"]["output_text"])


def _eventually(read, done, *, within=10.0):
    deadline = time.monotonic() + within
    value = read()
    while not done(value) and time.monotonic() < deadline:
        time.sleep(0.05)
        value = read()
    return value


# ── the compiler's resources ─────────────────────────────────────────────


def test_the_metrics_measure_the_compiler_process_not_only_the_host(
        tmp_path, monkeypatch):
    forge_bin = _stand_in(tmp_path)

    def during(service):
        before = _metrics(service)["resources"]
        grown = _forge(service.agent._mcp, "GROW")
        after = _metrics(service)["resources"]
        return before, grown, after, _prometheus(service)

    before, grown, after, exported = _boot(
        tmp_path, monkeypatch, during, forge_bin=forge_bin).result
    moved = {name: after[name] - before[name] for name in after
             if isinstance(after.get(name), int) and isinstance(before.get(name), int)}
    assert (moved.get("compiler_resident_memory_bytes", 0) >= GROW_BYTES * 3 // 4
            and moved.get("compiler_thread_count") == GROW_THREADS
            and moved.get("compiler_open_fd_count") == GROW_FDS), (
        f"the compiler process grew by {GROW_BYTES // MIB} MiB, {GROW_THREADS} threads "
        f"and {GROW_FDS} open descriptors, and the metrics moved by {moved}: the "
        f"resource figures measure the Python host, not the compiler where guests "
        f"run (audit F-18)")
    # Exactly the compiler's own count of itself, as totals over one process.
    assert after["compiler_process_count"] == 1
    assert after["compiler_thread_count"] == grown["threads"]
    assert after["compiler_open_fd_count"] == grown["fds"]
    # The host's own figures did not absorb the compiler's growth.
    assert moved["memory_peak_bytes"] < GROW_BYTES * 3 // 4
    # Prometheus carries the same figures, label-free. Resident memory moves
    # by a page or two between two reads, so only its presence is exact.
    for name in ("process_count", "thread_count", "open_fd_count"):
        assert f"\nsigil_pi_compiler_{name} {after['compiler_' + name]}\n" in exported
    assert re.search(r"\nsigil_pi_compiler_resident_memory_bytes \d+\n", exported)


def test_a_dead_compiler_is_not_counted_and_its_replacement_is(tmp_path, monkeypatch):
    forge_bin = _stand_in(tmp_path)

    def during(service):
        runtime = service.agent._mcp
        first = _forge(runtime, "GROW")
        os.kill(first["pid"], signal.SIGKILL)
        # Polled in-process: through the API this loop would spend the
        # operator's request quota, not wait for the kernel.
        dead = _eventually(
            lambda: service.operational_metrics()["resources"],
            lambda resources: resources.get("compiler_process_count") == 0)
        with pytest.raises(runtime_client.RuntimeClientError):
            _forge(runtime, "SELF")       # finds it dead; the supervisor retires it
        second = _forge(runtime, "SELF")  # a new generation
        return first, dead, second, _metrics(service)

    first, dead, second, final = _boot(
        tmp_path, monkeypatch, during, forge_bin=forge_bin).result
    assert dead.get("compiler_process_count") == 0, (
        f"after the compiler was killed the metrics reported {dead}: a dead "
        f"compiler must count as no process")
    assert (dead["compiler_resident_memory_bytes"], dead["compiler_thread_count"],
            dead["compiler_open_fd_count"]) == (0, 0, 0)
    assert second["pid"] != first["pid"]
    resources = final["resources"]
    assert resources["compiler_process_count"] == 1
    assert resources["compiler_thread_count"] == second["threads"]
    assert resources["compiler_open_fd_count"] == second["fds"]
    assert resources["compiler_thread_count"] < first["threads"], (
        "the figures still describe the dead compiler's grown threads")
    assert final["runtime_generation"] == 2
    assert final["runtime_unhealthy_replacements_total"] == 1


def test_the_figures_are_totals_over_every_live_compiler(tmp_path):
    """A5 turns the one compiler into a pool. Every compiler is spawned
    through ``build_runtime``, so registering there counts them all, and the
    metrics stay totals with a process count rather than one process."""
    from product_service import CompilerProcesses

    forge_bin = _stand_in(tmp_path)
    processes = CompilerProcesses()
    auth = AuthRegistry([_operator()])
    with product_main.build_runtime(forge_bin, 30, lambda path: None,
                                    processes=processes) as first, \
            product_main.build_runtime(forge_bin, 30, lambda path: None,
                                       processes=processes) as second:
        agent = SimpleNamespace(manifest={}, memory=None, _mcp=first)
        service = ProductService(agent, auth, log_sink=lambda record: None,
                                 compiler_pids=processes.pids)
        before = _metrics(service)["resources"]
        _forge(first, "GROW")
        after = _metrics(service)["resources"]
        one, two = _forge(first, "SELF"), _forge(second, "SELF")
        live = processes.pids()
    assert sorted(live) == sorted([one["pid"], two["pid"]])
    assert after["compiler_process_count"] == 2
    assert after["compiler_thread_count"] == one["threads"] + two["threads"]
    assert after["compiler_open_fd_count"] == one["fds"] + two["fds"]
    assert (after["compiler_resident_memory_bytes"]
            - before["compiler_resident_memory_bytes"]) >= GROW_BYTES * 3 // 4
    assert processes.pids() == [], "closed compilers must leave the count"


def test_the_count_does_not_keep_a_retired_compiler_alive(tmp_path):
    """Every turn that outlives its budget has its compiler killed and
    replaced; that is the hard deadline working. A registry that held its
    compilers would keep each retired one, and its process handle, for the
    life of the service. It holds them weakly."""
    from product_service import CompilerProcesses

    forge_bin = _stand_in(tmp_path)
    processes = CompilerProcesses()
    with product_main.build_runtime(forge_bin, 30, lambda path: None,
                                    processes=processes) as runtime:
        retired = weakref.ref(runtime._client)
        os.kill(_forge(runtime, "SELF")["pid"], signal.SIGKILL)
        with pytest.raises(runtime_client.RuntimeClientError):
            _forge(runtime, "SELF")
        replacement = _forge(runtime, "SELF")
        gc.collect()
        assert retired() is None, "the compiler count kept a retired compiler alive"
        assert processes.pids() == [replacement["pid"]]


def test_the_pinned_compiler_itself_is_measured(tmp_path, monkeypatch):
    """The stand-ins above are Python. This boots the product on the real
    sigil-mcp and requires its figures to be the real process's."""
    needs_toolchain()

    def during(service):
        return _metrics(service)

    booted = _boot(tmp_path, monkeypatch, during)
    resources = booted.result["resources"]
    assert resources["compiler_process_count"] == 1
    assert resources["compiler_resident_memory_bytes"] >= 1 * MIB
    assert resources["compiler_thread_count"] >= 1
    # its three pipes at least: requests, responses and diagnostics
    assert resources["compiler_open_fd_count"] >= 3


def test_a_host_without_a_compiler_registry_reports_no_compiler_figures():
    """The research host and every scripted double have no registry. The
    exporter omits the series rather than invent a zero that an alert could
    read as a measurement, as it does for runtime generations."""
    service = ProductService(
        SimpleNamespace(manifest={}, memory=None, _mcp=object()),
        AuthRegistry([_operator()]), log_sink=lambda record: None)
    resources = service.operational_metrics()["resources"]
    assert not [name for name in resources if name.startswith("compiler_")]
    assert "effective_settings" not in service.operational_metrics()


# ── measuring one process ───────────────────────────────────────────────


_HOLDER = '''\
import os, sys, threading, time
held = [os.urandom({grow})]
for _ in range({threads}):
    threading.Thread(target=time.sleep, args=(3600,), daemon=True).start()
held.extend(os.open(os.devnull, os.O_RDONLY) for _ in range({fds}))
count = 0
for fd in range(4096):
    try:
        os.fstat(fd)
    except OSError:
        continue
    count += 1
print(threading.active_count(), count, flush=True)
time.sleep(3600)
'''


def test_this_platform_measures_a_real_process_exactly():
    """Linux reads /proc and macOS asks the kernel through libproc. Neither is
    skipped: this runs whichever this machine has, against a real child."""
    from product_service import process_measurer

    measure = process_measurer()
    assert measure is not None, f"{sys.platform} should expose process resources"
    child = subprocess.Popen(
        [sys.executable, "-c", _HOLDER.format(grow=GROW_BYTES, threads=7, fds=20)],
        stdout=subprocess.PIPE, text=True)
    try:
        threads, fds = map(int, child.stdout.readline().split())
        measured = measure(child.pid)
        assert measured["thread_count"] == threads == 8
        assert measured["open_fd_count"] == fds
        assert measured["resident_memory_bytes"] >= GROW_BYTES
        child.kill()
        _eventually(lambda: measure(child.pid), lambda value: value is None)
        assert measure(child.pid) is None, "a zombie has no resources to report"
    finally:
        child.kill()
        child.wait()
    assert measure(child.pid) is None, "a reaped process has no resources to report"


def _status(directory, pid, text, fds=None):
    process = directory / str(pid)
    process.mkdir(parents=True)
    (process / "status").write_text(textwrap.dedent(text))
    if fds is not None:
        (process / "fd").mkdir()
        for fd in range(fds):
            (process / "fd" / str(fd)).write_text("")
    return pid


def test_the_proc_reader_handles_every_shape_a_process_can_be_in(tmp_path):
    """/proc's status file, read the way the kernel writes it, on any host."""
    from product_service import _proc_resources

    proc = tmp_path / "proc"
    running = _status(proc, 101, """\
        Name:\tsigil-mcp
        State:\tS (sleeping)
        VmHWM:\t   90000 kB
        VmRSS:\t   81920 kB
        Threads:\t5
        """, fds=7)
    zombie = _status(proc, 102, "Name:\tsigil-mcp\nState:\tZ (zombie)\nThreads:\t1\n")
    hidden_fds = _status(proc, 103, "State:\tR (running)\nVmRSS:\t4 kB\nThreads:\t2\n")
    garbled = _status(proc, 104, "State:\tS (sleeping)\nVmRSS:\tlots kB\nThreads:\t2\n")
    assert _proc_resources(running, proc_root=proc) == {
        "resident_memory_bytes": 81920 * 1024, "thread_count": 5, "open_fd_count": 7}
    assert _proc_resources(zombie, proc_root=proc) is None
    assert _proc_resources(hidden_fds, proc_root=proc) == {
        "resident_memory_bytes": 4096, "thread_count": 2, "open_fd_count": None}
    assert _proc_resources(garbled, proc_root=proc) is None
    assert _proc_resources(105, proc_root=proc) is None, "a process that has gone"


def test_totals_are_unknown_rather_than_partial(tmp_path):
    from product_service import compiler_resources

    samples = {
        1: {"resident_memory_bytes": 10, "thread_count": 2, "open_fd_count": 3},
        2: {"resident_memory_bytes": 20, "thread_count": 4, "open_fd_count": None},
        3: None,
    }
    assert compiler_resources([1], samples.get) == {
        "compiler_process_count": 1, "compiler_resident_memory_bytes": 10,
        "compiler_thread_count": 2, "compiler_open_fd_count": 3}
    assert compiler_resources([1, 2], samples.get) == {
        "compiler_process_count": 2, "compiler_resident_memory_bytes": 30,
        "compiler_thread_count": 6, "compiler_open_fd_count": None}
    # A live compiler that could not be measured makes every total unknown.
    assert compiler_resources([1, 3], samples.get) == {
        "compiler_process_count": 2, "compiler_resident_memory_bytes": None,
        "compiler_thread_count": None, "compiler_open_fd_count": None}
    # A platform that exposes nothing still says how many compilers there are.
    assert compiler_resources([1, 2], None) == {
        "compiler_process_count": 2, "compiler_resident_memory_bytes": None,
        "compiler_thread_count": None, "compiler_open_fd_count": None}
    assert compiler_resources([], samples.get) == {
        "compiler_process_count": 0, "compiler_resident_memory_bytes": 0,
        "compiler_thread_count": 0, "compiler_open_fd_count": 0}


def test_only_linux_and_macos_are_measured(monkeypatch):
    import product_service

    assert product_service.process_measurer("linux") is product_service._proc_resources
    assert product_service.process_measurer("darwin") is product_service._libproc_resources
    assert product_service.process_measurer("freebsd14") is None
    assert product_service.process_measurer("win32") is None

    def unavailable():
        raise OSError("no libproc here")

    # A macOS without a loadable libproc measures nothing rather than fail
    # the metrics request; the totals then read as unknown.
    monkeypatch.setattr(product_service, "_load_libproc", unavailable)
    assert product_service._libproc_resources(os.getpid()) is None


# ── the settings in force ───────────────────────────────────────────────


# Every setting product_main reads that the metrics report: the variable, the
# key it is reported under, a value that differs from its default (odd, so a
# swap between two settings shows), and what the report must say.
REPORTED = {
    "PI_MODEL": ("model", "claude-p4-test-model", "claude-p4-test-model"),
    "PI_ENDPOINT": ("endpoint_host", "https://provider.p4.test:8443/v1/messages",
                    "provider.p4.test"),
    "PI_MAX_CREDENTIAL_LIFETIME_DAYS": ("max_credential_lifetime_days", "89", 89),
    "PI_SOCKET_TIMEOUT_SECONDS": ("socket_timeout_seconds", "29", 29),
    "PI_DRAIN_TIMEOUT_SECONDS": ("drain_timeout_seconds", "13", 13),
    "PI_TURN_DEADLINE_SECONDS": ("turn_deadline_seconds", "117", 117),
    "PI_TURN_LEASE_SECONDS": ("turn_lease_seconds", "301", 301),
    "PI_MCP_TIMEOUT_SECONDS": ("mcp_timeout_seconds", "89", 89),
    "PI_AUDIT_VERIFY_SECONDS": ("audit_verify_seconds", "61", 61),
    "PI_RETENTION_DAYS": ("retention_days", "91", 91),
    "PI_RETENTION_INTERVAL_SECONDS": ("retention_interval_seconds", "3599", 3599),
    "PI_MAX_HISTORY_BYTES": ("max_history_bytes", "262147", 262147),
    "PI_MAX_TOOL_RESULT_BYTES": ("max_tool_result_bytes", "16387", 16387),
    "PI_MAX_STEPS": ("max_steps", "7", 7),
    "PI_MAX_TOKENS": ("max_tokens", "1001", 1001),
    "PI_THINKING": ("thinking", "disabled", "disabled"),
    "PI_LLM_RETRIES": ("llm_retries", "1", 1),
    "PI_REQUESTS_PER_MINUTE": ("requests_per_minute", "59", 59),
    "PI_MAX_CONCURRENT_TURNS": ("max_concurrent_turns", "3", 3),
    "PI_TOKENS_PER_DAY": ("tokens_per_day", "10000007", 10_000_007),
    "PI_TOKEN_RESERVATION_PER_TURN": ("token_reservation_per_turn", "4000003", 4_000_003),
    "PI_STORAGE_BYTES_PER_TENANT": ("storage_bytes_per_tenant", "1073741827", 1_073_741_827),
    "PI_STORAGE_RESERVATION_PER_TURN": (
        "storage_reservation_per_turn", "16777219", 16_777_219),
    "PI_AUDIT_BYTES_PER_TENANT": ("audit_bytes_per_tenant", "268435459", 268_435_459),
    "PI_AUDIT_RESERVATION_PER_TURN": ("audit_reservation_per_turn", "1048579", 1_048_579),
    "PI_MAX_SCHEDULES_PER_TENANT": ("max_schedules_per_tenant", "31", 31),
    "PI_MAX_EXPORT_BYTES": ("max_export_bytes", "10485763", 10_485_763),
}

# Every other variable product_main reads, and why the report leaves it out.
NOT_REPORTED = {
    "ANTHROPIC_API_KEY": "the provider credential: a secret",
    "PI_AUDIT_KEY": "the audit signing key: a secret",
    "PI_AUTH_FILE": "where the credential digests are, not a limit",
    "PI_STATE": "where state lives, not a limit",
    "PI_HOST": "the listener's own address, which whoever scrapes it already knows",
    "PI_PORT": "the listener's own port, which whoever scrapes it already knows",
    "PI_TLS_CERT": "where key material is",
    "PI_TLS_KEY": "where key material is",
    "PI_NET_ALLOWLIST": "hosts tools may reach: a grant, not a limit, and per tenant (A6)",
    "PI_SYSTEM": "the system prompt: content",
    "PI_SYSTEM_FILE": "where the system prompt is",
    "PI_MEMORY_SIDECAR": "refused at startup: the product runs without memory",
}

_ENV_READERS = {"_positive_env_int", "_env_int", "_required_env", "get", "getenv"}


def _variables_product_main_reads():
    tree = ast.parse((PI_ROOT / "product_main.py").read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args:
            function = node.func
            name = getattr(function, "id", None) or getattr(function, "attr", None)
            first = node.args[0]
            if (name in _ENV_READERS and isinstance(first, ast.Constant)
                    and isinstance(first.value, str)
                    and re.fullmatch(r"[A-Z][A-Z0-9_]*", first.value)):
                names.add(first.value)
        elif (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
              and isinstance(node.slice.value, str)
              and "environ" in ast.unparse(node.value)):
            names.add(node.slice.value)
    return names


def test_every_variable_product_main_reads_is_reported_or_excused():
    """The class guard. A new limit read in product_main without a line in
    the settings report fails here, before a drill or a load run can measure
    a configuration nobody recorded (audit F-18)."""
    read = _variables_product_main_reads()
    assert "PI_TURN_DEADLINE_SECONDS" in read and "ANTHROPIC_API_KEY" in read, (
        "the scan found nothing: it would pass vacuously")
    assert not set(REPORTED) & set(NOT_REPORTED)
    unaccounted = read - set(REPORTED) - set(NOT_REPORTED)
    assert not unaccounted, (
        f"product_main reads {sorted(unaccounted)}, which the metrics neither report "
        f"in effective_settings nor exclude for a stated reason; add each to one")
    stale = (set(REPORTED) | set(NOT_REPORTED)) - read
    assert not stale, f"no longer read by product_main: {sorted(stale)}"


def test_the_metrics_record_the_settings_in_force(tmp_path, monkeypatch):
    env = {name: value for name, (_, value, _) in REPORTED.items()}
    booted = _boot(tmp_path, monkeypatch, _metrics, forge_bin=_stand_in(tmp_path),
                   env=env)
    expected = {key: reported for key, _, reported in REPORTED.values()}
    settings = booted.result.get("effective_settings")
    assert settings == expected, (
        f"the operator set {len(expected)} settings and the metrics report "
        f"{settings!r}: the configuration in force is not recorded (audit F-18)")


@pytest.mark.parametrize("configured", ["defaults", "overrides"])
def test_each_reported_setting_is_the_value_its_component_received(
        tmp_path, monkeypatch, configured):
    env = ({} if configured == "defaults"
           else {name: value for name, (_, value, _) in REPORTED.items()})
    booted = _boot(tmp_path, monkeypatch, _metrics, forge_bin=_stand_in(tmp_path),
                   env=env)
    service, agent = booted.service, booted.agent
    quota = service.quota_store
    received = {
        "model": agent["model"],
        "endpoint_host": urlsplit(agent["endpoint"]).hostname,
        "max_credential_lifetime_days": booted.auth["max_lifetime_s"] // 86400,
        "socket_timeout_seconds": booted.serve["socket_timeout_s"],
        "drain_timeout_seconds": dict(booted.drains)["scheduler"],
        "turn_deadline_seconds": service.turn_deadline_s,
        "turn_lease_seconds": quota.turn_lease_s,
        "mcp_timeout_seconds": booted.spawn_timeouts[0],
        "audit_verify_seconds": service.audit_monitor.interval_s,
        "retention_days": booted.retention["retention_s"] // 86400,
        "retention_interval_seconds": booted.retention["interval_s"],
        "max_history_bytes": agent["max_history_bytes"],
        "max_tool_result_bytes": agent["max_tool_result_bytes"],
        "max_steps": agent["max_steps"],
        "max_tokens": agent["max_tokens"],
        "thinking": agent["thinking"],
        "llm_retries": agent["llm_retries"],
        "requests_per_minute": quota.requests_per_minute,
        "max_concurrent_turns": quota.max_concurrent_turns,
        "tokens_per_day": quota.tokens_per_day,
        "token_reservation_per_turn": quota.token_reservation_per_turn,
        "storage_bytes_per_tenant": quota.storage_bytes_per_tenant,
        "storage_reservation_per_turn": quota.storage_reservation_per_turn,
        "audit_bytes_per_tenant": quota.audit_bytes_per_tenant,
        "audit_reservation_per_turn": quota.audit_reservation_per_turn,
        "max_schedules_per_tenant": service.schedule_store.max_per_tenant,
        "max_export_bytes": service.data_manager.max_export_bytes,
    }
    # The scheduler runs scheduled turns under the same deadline and lease.
    assert booted.scheduler["turn_deadline_s"] == received["turn_deadline_seconds"]
    assert booted.scheduler["lease_s"] == received["turn_lease_seconds"]
    assert {stage: timeout for stage, timeout in booted.drains} == {
        "scheduler": received["drain_timeout_seconds"],
        "retention": received["drain_timeout_seconds"]}
    assert set(received) == {key for key, _, _ in REPORTED.values()}
    assert booted.result.get("effective_settings") == received


def test_no_secret_or_path_reaches_either_metrics_form(tmp_path, monkeypatch):
    canary = "p4canary"
    env = {
        "ANTHROPIC_API_KEY": f"sk-{canary}-provider-key",
        "PI_AUDIT_KEY": f"{canary}-audit-signing-key-0123456789abcdef",
        "PI_SECRET_GITHUB": f"ghp_{canary}_tool_credential",
        "PI_ENDPOINT": (f"https://{canary}-user:{canary}-password@provider.p4.test:8443"
                        f"/{canary}-path/v1/messages?key={canary}-query#{canary}-fragment"),
        "PI_NET_ALLOWLIST": f"{canary}-allowed.example",
        "PI_SYSTEM": f"{canary} system prompt",
        "PI_MODEL": "claude-p4-visible-model",
    }

    def during(service):
        return json.dumps(_metrics(service)), _prometheus(service)

    booted = _boot(tmp_path, monkeypatch, during, forge_bin=_stand_in(tmp_path),
                   env=env, auth_dir=tmp_path / f"{canary}-auth",
                   state_dir=tmp_path / f"{canary}-state")
    document, exported = booted.result
    # The search can fire: what is meant to be reported is found.
    assert "provider.p4.test" in document and "claude-p4-visible-model" in document
    assert booted.agent["endpoint"].count(canary) == 5, "the canaries were planted"
    assert canary not in document, "a secret, credential or path reached /v1/metrics"
    assert canary not in exported, "a secret, credential or path reached Prometheus"
    assert "claude-p4-visible-model" not in exported, (
        "a model name must never be a Prometheus label or value (docs/api.md)")


@pytest.mark.parametrize("name,value", [
    ("PI_MCP_TIMEOUT_SECONDS", "0"),
    ("PI_AUDIT_VERIFY_SECONDS", "-1"),
    ("PI_MAX_SCHEDULES_PER_TENANT", "0"),
    ("PI_MAX_EXPORT_BYTES", "0"),
    ("PI_LLM_RETRIES", "two"),
])
def test_a_setting_is_checked_before_any_state_is_touched(
        tmp_path, monkeypatch, name, value):
    """Reporting a setting means reading it with the others, before the
    service opens anything. These five used to be read after the state
    directory was created and the compiler spawned."""
    with pytest.raises((ConfigError, SystemExit), match=name):
        _boot(tmp_path, monkeypatch, _metrics, forge_bin=_stand_in(tmp_path),
              env={name: value})
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("endpoint,host", [
    ("https://api.anthropic.com/v1/messages", "api.anthropic.com"),
    ("https://user:password@Provider.Example:8443/v1/messages?key=value",
     "provider.example"),
    ("http://[::1]:9/v1/messages", "::1"),
    ("http://[::1/v1/messages", None),        # unparseable: no host, no fragment of it
    ("not a url", None),
])
def test_only_the_providers_host_is_taken_from_the_endpoint(endpoint, host):
    assert product_main._endpoint_host(endpoint) == host


def test_numeric_settings_are_exported_without_labels(tmp_path, monkeypatch):
    def during(service):
        return _metrics(service), _prometheus(service)

    booted = _boot(tmp_path, monkeypatch, during, forge_bin=_stand_in(tmp_path))
    settings, exported = booted.result[0]["effective_settings"], booted.result[1]
    numbers = {key: value for key, value in settings.items()
               if isinstance(value, int) and not isinstance(value, bool)}
    assert len(numbers) == len(settings) - 3      # all but the model, the host and thinking
    for key, value in numbers.items():
        assert f"\n# TYPE sigil_pi_setting_{key} gauge\n" in exported
        assert f"\nsigil_pi_setting_{key} {value}\n" in exported
    label_keys = set(re.findall(r'(\w+)="', exported))
    assert label_keys <= {
        "api_version", "version", "method", "route", "status", "class", "le",
        "dependency"}


@pytest.mark.parametrize("settings", [
    {"Model": "claude"},
    {"turn deadline": 120},
    {"nested": {"a": 1}},
    {"listed": [1, 2]},
    {"": 1},
])
def test_the_service_refuses_settings_it_could_not_export_safely(settings):
    with pytest.raises(ConfigError, match="effective setting"):
        ProductService(SimpleNamespace(manifest={}, memory=None),
                       AuthRegistry([_operator()]), log_sink=lambda record: None,
                       effective_settings=settings)
