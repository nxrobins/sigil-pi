"""Independent fixture codecs and HTTP client for the actual native/SIGIL service."""
import hashlib
import http.client
import json
import select
import subprocess

from conftest import MCP_BIN, SIGIL_ROOT, needs_toolchain
from scripts.compose_application import compose_application
from turn_support import FUEL, configuration, record
from worker_support import runtime_digest

SCOPES = ["chat", "ops:read", "schedules:read", "schedules:write", "sessions:read", "sessions:delete"]
TOKEN_A = "a-credential-canary-" + "a" * 40
TOKEN_B = "b-credential-canary-" + "b" * 40
BODY = {"session": "same-session", "message": "Read README.md", "submission_key": "same-key"}
LIMITS = {"value_bytes": 2097152, "batch_bytes": 8388608, "batch_items": 64,
          "live_bytes": 134217728, "records": 100000, "database_pages": 262144}


def credential(token=TOKEN_A, *, principal="alice", tenant="tenant-a", prefix="a", scopes=None,
               tools=None, before=100, expires=9000000000, epoch="epoch-1", turn_seconds=120, profile=None):
    policy = profile or record("BP1\n", [configuration(), "8", "160000", "32768", "20000"])
    names = [prefix + suffix for suffix in (".requests", ".operations", ".state", ".intent", ".budget", ".reservation")]
    facts = record("CF2\n", [principal, tenant, epoch, str(before), str(expires),
                            json.dumps(SCOPES if scopes is None else scopes),
                            json.dumps(["read_file"] if tools is None else tools),
                            names[0], names[1], str(turn_seconds), *names[2:], policy])
    return {"sha256": hashlib.sha256(token.encode()).hexdigest(), "facts": facts,
            "grants": dict.fromkeys(names, "read_write")}


def binding(row):
    grants = [record("CG1\n", [ns, access]) for ns, access in row["grants"].items()]
    return record("CB1\n", [row["facts"], json.dumps(grants)])


def envelope(*, method="POST", path="/v1/operations", body=None, row=None, now=150,
             operation="a" * 64, stage="init", observation="", continuation="", purpose=None,
             bundle="b" * 64, functions=None):
    return record("AH3\n", [method, path, json.dumps(BODY) if body is None else body,
                            credential()["facts"] if row is None else row["facts"], str(now),
                            operation, stage, observation, continuation, bundle,
                            json.dumps(["admission", "history"] if functions is None else functions),
                            purpose or ("boot" if stage == "boot" else "request")])


class NativeApi:
    def __init__(self, binary, root, *, credentials=None, mode="init", config=None, source=None, function_source=None):
        needs_toolchain()
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        source = compose_application("api", SIGIL_ROOT).text if source is None else source
        source_path = root / "api.sigil"
        source_path.write_text(source)
        admitted = compose_application("admission", SIGIL_ROOT).text if function_source is None else function_source
        admitted_path = root / "admission.sigil"
        admitted_path.write_text(admitted)
        history = compose_application("history", SIGIL_ROOT).text
        history_path = root / "history.sigil"
        history_path.write_text(history)
        self.config = config or {"version": 3, "state_root": str(root / "records"), "limits": LIMITS,
            "credentials": credentials or [credential()],
            "worker": {"version": 1, "runtime": str(MCP_BIN), "runtime_sha256": runtime_digest(str(MCP_BIN)),
                       "source": str(source_path), "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                       "max_fuel": FUEL, "max_timeout_ms": 15000, "net": [], "fs": [], "secret_env": {}},
            "functions": {"admission": {"version": 1, "runtime": str(MCP_BIN), "runtime_sha256": runtime_digest(str(MCP_BIN)),
                       "source": str(admitted_path), "source_sha256": hashlib.sha256(admitted.encode()).hexdigest(),
                       "max_fuel": FUEL, "max_timeout_ms": 15000, "net": [], "fs": [], "secret_env": {}},
                "history": {"version": 1, "runtime": str(MCP_BIN), "runtime_sha256": runtime_digest(str(MCP_BIN)),
                       "source": str(history_path), "source_sha256": hashlib.sha256(history.encode()).hexdigest(),
                       "max_fuel": FUEL, "max_timeout_ms": 15000, "net": [], "fs": [], "secret_env": {}}}}
        config_path = root / "service.json"
        config_path.write_text(json.dumps(self.config))
        self.proc = subprocess.Popen([str(binary), mode, str(config_path), "0"],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([self.proc.stdout], [], [], 40)
            assert ready, "native API startup timed out"
            line = self.proc.stdout.readline()
            assert line, self.proc.stderr.read()
            self.ready = json.loads(line)
            assert self.ready["status"] == "ready", self.ready
            assert self.ready["protocol"] == f"sigil-application-host/v{self.config['version']}", self.ready
            host, port = self.ready["address"].split(":")
            assert host == "127.0.0.1"
            self.port = int(port)
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def request(self, method="POST", path="/v1/operations", body=None, token=TOKEN_A, raw=None):
        payload = json.dumps(BODY if body is None else body).encode() if raw is None else raw
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            text = response.read().decode()
            assert response.getheader("Cache-Control") == "no-store"
            return response.status, json.loads(text)
        finally:
            conn.close()

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.proc.stdout.close()
        self.proc.stderr.close()
