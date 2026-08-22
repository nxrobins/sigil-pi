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
from collections import deque
from pathlib import Path


class RuntimeClientError(RuntimeError):
    pass


class RuntimeClientTimeout(RuntimeClientError):
    pass


class ProductionSigilMCP:
    def __init__(self, proc, stderr_log, reader_thread, timeout_s=90.0):
        self._proc = proc
        self._stderr_log = stderr_log
        self._reader = reader_thread
        self._timeout_s = timeout_s
        self._next_id = 1
        self._closed = False

    @classmethod
    def spawn(cls, binary_path, timeout_s=90.0):
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
            for line in proc.stderr:
                stderr_log.append(line.rstrip())

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
        if timeout <= 0:
            self._proc.kill()
            self._closed = True
            raise RuntimeClientTimeout(f"sigil-mcp deadline elapsed before {method}")
        ready, _, _ = select.select([self._proc.stdout], [], [], timeout)
        if not ready:
            self._proc.kill()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            self._closed = True
            raise RuntimeClientTimeout(
                f"sigil-mcp did not answer {method} within {timeout}s and was killed")
        response_line = self._proc.stdout.readline()
        if not response_line:
            raise RuntimeClientError("sigil-mcp closed its response stream")
        try:
            response = json.loads(response_line)
        except json.JSONDecodeError as e:
            raise RuntimeClientError("sigil-mcp returned malformed JSON") from e
        if response.get("jsonrpc") != "2.0" or response.get("id") != request_id:
            raise RuntimeClientError("sigil-mcp returned a mismatched response")
        if "error" in response:
            raise RuntimeClientError("sigil-mcp returned a protocol error")
        if "result" not in response:
            raise RuntimeClientError("sigil-mcp response has no result")
        return response["result"]

    def _tool(self, name, arguments, timeout_s=None):
        result = self._request(
            "tools/call", {"name": name, "arguments": arguments}, timeout_s=timeout_s)
        try:
            text = result["content"][0]["text"]
            return json.loads(text)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
            raise RuntimeClientError(f"sigil-mcp returned an invalid {name} result") from e

    def initialize(self):
        return self._request("initialize", {})

    def forge(self, source, *, input="", fuel=100_000, grants=None, timeout_s=None):
        arguments = {"source": source, "input": input, "fuel": fuel}
        if grants is not None:
            arguments["grants"] = grants
        return self._tool("sigil_forge", arguments, timeout_s=timeout_s)
