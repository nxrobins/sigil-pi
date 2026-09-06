"""Fail-closed SIGIL MCP client for the product runtime.

The SIGIL benchmark client deliberately injects ``SIGIL_ALLOW_UNVERIFIED_CERT=1`` so
benchmarks can execute against solver-disabled development builds. The product service must
never inherit that exception. This small client implements only the protocol surface sigil-pi
uses and removes the override even if it is present in the operator environment.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import threading
import time
from collections import deque
from pathlib import Path


class RuntimeClientError(RuntimeError):
    pass


class RuntimeProtocolError(RuntimeClientError):
    """A well-formed protocol response that says no.

    Distinguished from its parent because the response frame was CONSUMED and
    the stream is still in position: the connection is reusable. A desync —
    malformed JSON, a mismatched id, a closed stream — is not, because the next
    caller would read someone else's answer. SupervisedRuntime retires a
    compiler for the latter and keeps it for this.
    """


class RuntimeClientTimeout(RuntimeClientError):
    """The runtime did not answer in time and its process was killed.

    ``budget_expired`` says WHOSE deadline ran out: True when the caller's own
    remaining turn budget won the ``min()`` in ``_request`` — the hard turn
    deadline doing exactly what it promises — and False when the client's base
    watchdog fired, which is a compiler that stopped answering. The product
    host counts only the latter against runtime health; without the
    distinction, readiness either never fails or fails on ordinary traffic.
    """

    def __init__(self, message, *, budget_expired=False):
        super().__init__(message)
        self.budget_expired = budget_expired


# THE HOST DECLARES WHAT IT IS. Since the CSIR v9 verifier (SIGIL, 2026-09-02) a
# host operation's occurrence is Public unless the host declares a profile, and
# a tool that makes a host call inside a branch on an @Internal value — the
# previous call's error code, i.e. every tool here — is then refused as leaking
# Internal control to the host (I013, occurrence detail 40). Not a policy the
# tools violate: the absence of a declaration. sigil-mcp runs every forge under
# its built-in ephemeral executor, whose profile declares each linked operation
# Internal-occurrence (a solver build declares itself a distinct host, since it
# links z3_check). Naming it on every forge is the ONE place this host says
# which host it is. There is deliberately no knob to omit it: omitting it is not
# a looser verdict the tools could pass, it is a verdict they cannot.
HOST_PROFILE = "ephemeral"


class ProductionSigilMCP:
    def __init__(self, proc, stderr_log, reader_thread, timeout_s=90.0):
        self._proc = proc
        self._stderr_log = stderr_log
        self._reader = reader_thread
        self._timeout_s = timeout_s
        self._next_id = 1
        self._closed = False

    @classmethod
    def spawn(cls, binary_path, timeout_s=90.0, generation=0):
        binary_path = Path(binary_path)
        if not binary_path.is_file():
            raise FileNotFoundError(f"sigil-mcp binary not found: {binary_path}")
        if timeout_s <= 0:
            raise ValueError("MCP timeout must be positive")
        child_env = dict(os.environ)
        # The product never permits benchmark/development execution to bypass
        # the forge certificate gate, even if its parent environment does.
        child_env.pop("SIGIL_ALLOW_UNVERIFIED_CERT", None)
        proc = subprocess.Popen(
            [str(binary_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", bufsize=1, env=child_env)
        stderr_log = deque(maxlen=200)

        def drain_stderr():
            # Generation-tagged, because once compilers are replaced a bare
            # stderr tail cannot say which process produced which line — and
            # that is exactly the log an operator reads after a replacement.
            for line in proc.stderr:
                stderr_log.append(f"[gen{generation}] {line.rstrip()}"
                                  if generation else line.rstrip())

        reader = threading.Thread(target=drain_stderr, daemon=True)
        reader.start()
        return cls(proc, stderr_log, reader, timeout_s=timeout_s)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def stderr_log(self):
        return list(self._stderr_log)

    @property
    def is_healthy(self):
        return not self._closed and self._proc.poll() is None

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=2)

    def _request(self, method, params=None, timeout_s=None):
        if self._closed:
            raise RuntimeClientError("MCP client is closed")
        request_id = self._next_id
        self._next_id += 1
        request = {"jsonrpc": "2.0", "id": request_id, "method": method,
                   "params": params if params is not None else {}}
        line = json.dumps(request, separators=(",", ":")) + "\n"
        try:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise RuntimeClientError("failed to write to sigil-mcp") from e

        timeout = self._timeout_s if timeout_s is None else min(self._timeout_s, timeout_s)
        # WHOSE deadline this is. The caller's remaining turn budget winning the
        # min() is the hard turn deadline working as documented; the base
        # watchdog winning it is a compiler that stopped answering. Only the
        # second is evidence about runtime health — see RuntimeClientTimeout.
        budget_expired = timeout_s is not None and timeout_s <= self._timeout_s
        if timeout <= 0:
            self._proc.kill()
            # Reap it. A zombie per expired turn is a slow leak on a host whose
            # whole point is bounded resources.
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            self._closed = True
            raise RuntimeClientTimeout(f"sigil-mcp deadline elapsed before {method}",
                                       budget_expired=budget_expired)
        ready, _, _ = select.select([self._proc.stdout], [], [], timeout)
        if not ready:
            self._proc.kill()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            self._closed = True
            raise RuntimeClientTimeout(
                f"sigil-mcp did not answer {method} within {timeout}s and was killed",
                budget_expired=budget_expired)
        response_line = self._proc.stdout.readline()
        # Below, the split that decides whether this connection is REUSABLE.
        # A desync (no line, unparseable, wrong id) means the stream is not
        # where the next caller expects it; a well-formed refusal consumed its
        # frame and left the connection perfectly usable.
        if not response_line:
            raise RuntimeClientError("sigil-mcp closed its response stream")
        try:
            response = json.loads(response_line)
        except json.JSONDecodeError as e:
            raise RuntimeClientError("sigil-mcp returned malformed JSON") from e
        if response.get("jsonrpc") != "2.0" or response.get("id") != request_id:
            raise RuntimeClientError("sigil-mcp returned a mismatched response")
        if "error" in response:
            raise RuntimeProtocolError("sigil-mcp returned a protocol error")
        if "result" not in response:
            raise RuntimeProtocolError("sigil-mcp response has no result")
        return response["result"]

    def _tool(self, name, arguments, timeout_s=None):
        result = self._request(
            "tools/call", {"name": name, "arguments": arguments}, timeout_s=timeout_s)
        try:
            text = result["content"][0]["text"]
            return json.loads(text)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
            raise RuntimeProtocolError(
                f"sigil-mcp returned an invalid {name} result") from e

    def initialize(self, timeout_s=None):
        # Bounded like any other request: a compiler that accepts a connection
        # and then never completes the handshake must not hold a caller past
        # its turn budget.
        return self._request("initialize", {}, timeout_s=timeout_s)

    def forge(self, source, *, input="", fuel=100_000, grants=None, timeout_s=None):
        arguments = {"source": source, "input": input, "fuel": fuel,
                     "host_profile": HOST_PROFILE}
        if grants is not None:
            arguments["grants"] = grants
        return self._tool("sigil_forge", arguments, timeout_s=timeout_s)


class SupervisedRuntime:
    """One forging endpoint over a succession of disposable compilers.

    WHY THIS EXISTS. The hard whole-turn deadline promises to bound forge
    EXECUTION, and the pinned sigil-mcp protocol has no cancel method, so the
    only way to interrupt a running forge is to kill the child. That is
    deliberate and documented. What was not deliberate: ``ProductionSigilMCP``
    latches ``_closed`` when it kills, there is exactly ONE client per product
    process, and nothing respawned — so one tenant's expired turn ended forging
    for EVERY tenant until someone restarted the process (reproduced 2026-08-23;
    tenant B got 502 ``agent_failure`` forever while ``/v1/ready`` said
    not-ready and nothing exited).

    The fix is ownership, not mechanism. A connection is disposable; the
    ENDPOINT is durable. A kill becomes a generation bump.

    LAZY, and charged to the caller that finds the gap — never to the dying
    turn, so no turn gets less wall clock than before. Respawn measured at
    ~16ms against a 2000ms queue-wait budget (docs/capacity.md), which is why a
    pool is not needed here (issue #17).

    NOT a retry: a caller whose forge failed still sees its exception. The next
    caller gets a working compiler. Retrying inside this object would silently
    double a turn's side effects.
    """

    def __init__(self, spawn, *, handshake_timeout_s=5.0, min_forge_budget_s=0.5,
                 unhealthy_budget=3, unhealthy_window_s=120.0,
                 close_timeout_s=1.0, clock=time.monotonic):
        self._spawn = spawn
        self._handshake_timeout_s = handshake_timeout_s
        self._min_forge_budget_s = min_forge_budget_s
        self._unhealthy_budget = unhealthy_budget
        self._unhealthy_window_s = unhealthy_window_s
        self._close_timeout_s = close_timeout_s
        self._clock = clock
        self._client = None
        self._generation = 0
        self._unhealthy = deque()
        self._replacements = 0
        self._unrecoverable = False
        self._closed = False
        self._lock = threading.Lock()

    # ── lifecycle ───────────────────────────────────────────────────────

    def __enter__(self):
        # Eager, so a host that cannot obtain a compiler at all fails at
        # startup rather than on a customer's first turn.
        with self._lock:
            self._start_generation(None)
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        """Stop serving. Bounded: never blocks behind an in-flight forge.

        ``_closed`` is set WITHOUT the lock on purpose — a forge that is
        mid-``select`` holds the lock for as long as its own deadline, and a
        drain that waited for it would exceed the shutdown budget. If the lock
        cannot be taken in time the in-flight compiler is killed out of band,
        which is what unwinds that ``select``.
        """
        self._closed = True
        acquired = self._lock.acquire(timeout=self._close_timeout_s)
        try:
            client = self._client
            if acquired:
                self._client = None
            if client is None:
                return
            if acquired:
                client.close()
            else:
                self._dispose(client)
        finally:
            if acquired:
                self._lock.release()

    # ── health, read without the lock ───────────────────────────────────

    @property
    def generation(self):
        """How many compilers this endpoint has started. Diagnostics only."""
        return self._generation

    @property
    def unhealthy_replacements(self):
        """Replacements NOT explained by a caller's own expired budget."""
        return self._replacements

    @property
    def is_healthy(self):
        """Whether this endpoint can still be expected to forge.

        Deliberately lock-free: readiness is polled by a supervisor and must
        never queue behind a turn. Both reads are plain bools, so the worst a
        race can do is answer with the value from a moment earlier.

        A retired compiler is NOT unhealthy — replacement is the design. Only
        an exhausted replacement budget is.
        """
        return not self._closed and not self._unrecoverable

    # ── forging ─────────────────────────────────────────────────────────

    def forge(self, source, *, input="", fuel=100_000, grants=None, timeout_s=None):
        with self._lock:
            if self._closed:
                raise RuntimeClientError("MCP client is closed")
            deadline = None if timeout_s is None else self._clock() + timeout_s
            if self._client is None:
                self._start_generation(deadline)
            residue = self._residue(deadline)
            limits = {} if deadline is None else {"timeout_s": residue}
            try:
                result = self._client.forge(source, input=input, fuel=fuel,
                                            grants=grants, **limits)
            except RuntimeProtocolError:
                # The frame was consumed and the stream is in position: the
                # compiler answered, it just said no. Keep it.
                raise
            except Exception as error:
                self._retire(error)
                raise
            return result

    # ── internals (all called under the lock) ───────────────────────────

    def _residue(self, deadline):
        return float("inf") if deadline is None else deadline - self._clock()

    def _start_generation(self, deadline):
        if self._unrecoverable:
            raise RuntimeClientError(
                "sigil-mcp could not be replaced; the runtime is unavailable")
        if self._residue(deadline) < self._min_forge_budget_s:
            # Spawning for a caller who cannot even wait for the handshake is a
            # spawn-kill treadmill: it burns the host and serves nobody.
            raise RuntimeClientTimeout(
                "turn budget too small to start a replacement compiler",
                budget_expired=True)
        client = self._spawn()
        try:
            client.initialize(
                timeout_s=min(self._residue(deadline), self._handshake_timeout_s))
        except Exception:
            self._dispose(client)
            self._mark_unhealthy()
            raise
        # Installed BEFORE the residue check below: a compiler this caller paid
        # for outlives it. Killing it because its sponsor ran out of budget
        # would mean a busy host never manages to keep one.
        self._client = client
        self._generation += 1
        if self._closed:
            # close() flipped while we were spawning. Reap rather than orphan.
            self._client = None
            self._dispose(client)
            raise RuntimeClientError("MCP client is closed")
        if self._residue(deadline) < self._min_forge_budget_s:
            raise RuntimeClientTimeout(
                "replacement compiler started but the turn budget is spent",
                budget_expired=True)

    def _retire(self, error):
        dead, self._client = self._client, None
        # A turn killing its own compiler is the deadline working, not a fault.
        # The test is WHOSE clock ran out and nothing else — deliberately not
        # "and this generation had served something first". That extra
        # condition looks prudent and is a self-inflicted outage: on a busy
        # host a fresh generation's first forge is routinely the one that gets
        # budget-killed, so four expired turns inside the window would latch
        # `_unrecoverable` and fail readiness forever with no compiler at fault
        # — the same shape as the bug this class exists to fix.
        benign = isinstance(error, RuntimeClientTimeout) and error.budget_expired
        if not benign:
            self._mark_unhealthy()
        self._dispose(dead)

    def _mark_unhealthy(self):
        now = self._clock()
        self._unhealthy.append(now)
        while self._unhealthy and now - self._unhealthy[0] > self._unhealthy_window_s:
            self._unhealthy.popleft()
        self._replacements += 1
        if len(self._unhealthy) > self._unhealthy_budget:
            # LATCHED. An alternating good/bad compiler would otherwise keep
            # trimming the window and hide a broken host indefinitely.
            self._unrecoverable = True

    @staticmethod
    def _dispose(client):
        """Best effort, and never graceful: a retired compiler is being thrown
        away, and waiting politely for one that already stopped answering is
        how a drain turns into a stall. Failing to reap must not wedge the
        endpoint a second time, so every error here is swallowed."""
        try:
            client._proc.kill()
        except Exception:
            pass
