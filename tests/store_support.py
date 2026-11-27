"""Test-only client for a real scoped native store process; no product decisions."""

import json
import select
import subprocess


LIMITS = {"value_bytes": 2 * 1024 * 1024, "batch_bytes": 8 * 1024 * 1024,
          "batch_items": 64, "live_bytes": 128 * 1024 * 1024,
          "records": 100_000, "database_pages": 262_144}


def mutation(namespace, key, value, revision=0):
    return {"namespace": namespace, "key": key, "revision": revision, "value": value}


def check(namespace, key, revision):
    return {"namespace": namespace, "key": key, "revision": revision}


class NativeStore:
    def __init__(self, binary, root, grants, *, initialize=False, config=None):
        if initialize:
            root.mkdir(mode=0o700)
        config = config or {"version": 1, "limits": LIMITS,
                            "grants": [{"namespace": name, "access": access}
                                       for name, access in grants.items()]}
        config_path = root.parent / f"{root.name}-fixture-scope.json"
        config_path.write_text(json.dumps(config))
        self.proc = subprocess.Popen(
            [str(binary), "init" if initialize else "open", str(root), str(config_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8")
        try:
            self.ready = self._read()
            assert self.ready["status"] == "ready"
            assert self.ready["protocol"] == "sigil-store/v1"
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _read(self):
        ready, _, _ = select.select([self.proc.stdout], [], [], 10)
        assert ready, "native store response timeout"
        line = self.proc.stdout.readline()
        assert line, f"native store closed: {self.proc.stderr.read()}"
        return json.loads(line)

    def raw(self, line):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        return self._read()

    def request(self, payload):
        return self.raw(json.dumps(payload))

    def get(self, namespace, key):
        response = self.request({"op": "get", "namespace": namespace, "key": key})
        assert response["status"] == "ok", response
        return response["record"]

    def commit(self, writes, checks=()):
        response = self.request({"op": "commit", "writes": writes, "checks": list(checks)})
        assert response["status"] == "ok", response
        return response["receipt"]

    def kill(self):
        self.proc.kill()
        self.proc.wait(timeout=5)

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.kill()
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if not stream.closed:
                stream.close()
