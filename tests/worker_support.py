"""Test-only bootstrap/stdio transport for the actual native worker bridge."""

import hashlib
import json
import os
import tempfile
from functools import lru_cache
from pathlib import Path
import subprocess
import select

from conftest import MCP_BIN
from turn_support import FUEL


@lru_cache
def runtime_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class NativeWorker:
    def __init__(self, binary, source, *, grants=None, runtime=MCP_BIN, timeout_ms=15000,
                 storage=None):
        self.temp = tempfile.TemporaryDirectory(prefix="sigil-worker-test-")
        root = Path(self.temp.name)
        path = root / "program.sigil"
        path.write_text(source)
        grants = grants or {}
        env = dict(os.environ)
        secret_env = {}
        for i, secret in enumerate(grants.get("secret", [])):
            name, value = secret.split("=", 1)
            variable = f"PI_NATIVE_FIXTURE_SECRET_{i}"
            secret_env[name] = variable
            env[variable] = value
        config = {"version": 1, "runtime": str(runtime), "runtime_sha256": runtime_digest(str(runtime)),
                  "source": str(path), "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                  "max_fuel": FUEL, "max_timeout_ms": timeout_ms, "net": grants.get("net", []),
                  "fs": grants.get("fs", []), "secret_env": secret_env}
        if storage is not None:
            # Test bootstrap may bind a second fixed pure worker to the actual
            # first worker's manifest. Requests cannot alter this configuration.
            extra = storage(config, root) if callable(storage) else storage
            config = {"version": 1, "worker": config, **extra}
        cfg = root / "config.json"
        cfg.write_text(json.dumps(config))
        self.proc = subprocess.Popen([str(binary), str(cfg)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        try:
            self.ready = self._read()
            assert self.ready["status"] == "ready", self.ready
        except BaseException:
            self.close()
            raise
        self.timeout_ms = timeout_ms

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _read(self):
        ready, _, _ = select.select([self.proc.stdout], [], [], 30)
        assert ready, "native worker fixture response timed out"
        line = self.proc.stdout.readline()
        assert line, self.proc.stderr.read()
        return json.loads(line)

    def raw(self, payload):
        self.proc.stdin.write(payload + "\n")
        self.proc.stdin.flush()
        return self._read()

    def request(self, payload):
        return self.raw(json.dumps(payload))

    def prepare(self, payload, *, timeout_ms=None, fuel=FUEL):
        result = self.request({"op": "prepare", "input": payload, "fuel": fuel,
                               "timeout_ms": self.timeout_ms if timeout_ms is None else timeout_ms})
        assert result["status"] == "ok", result
        return result["prepared"]

    def execute(self, prepared):
        result = self.request({"op": "execute", "ticket": prepared["ticket"]})
        assert result["status"] == "ok", result
        return result["observation"]

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if not stream.closed:
                stream.close()
        self.temp.cleanup()
