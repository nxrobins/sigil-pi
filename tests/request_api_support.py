"""Explicit v8 static configuration and transport. All decisions execute SIGIL."""

import hashlib
import json
import select
import subprocess

from http_service_support import HttpApi, configure as configure_v7
from request_entry_support import compose_request_entry
from request_policy_support import compose_request_policy


def configure(root, programs, endpoint, workspace, rows=None, *, limit):
    entry = compose_request_entry(limit)
    config, path = configure_v7(root, programs, endpoint, workspace, rows, source=entry.text)
    config["version"] = 8
    policy = compose_request_policy()
    policy_path = root / "request-policy.sigil"
    policy_path.write_text(policy.text)
    config["functions"]["request_policy"] = {
        **config["worker"], "source": str(policy_path),
        "source_sha256": hashlib.sha256(policy.text.encode()).hexdigest(),
    }
    path.write_text(json.dumps(config))
    return config, path


class RequestApi(HttpApi):
    def __init__(self, binary, root, programs, endpoint, workspace, *, limit, rows=None, mode="init"):
        self.root = root
        self.config, path = configure(root, programs, endpoint, workspace, rows, limit=limit)
        self.proc = subprocess.Popen([str(binary), mode, str(path), "0"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([self.proc.stdout], [], [], 40)
            assert ready, "v8 request API startup timed out"
            line = self.proc.stdout.readline()
            assert line, self.proc.stderr.read()
            self.ready = json.loads(line)
            assert self.ready["status"] == "ready" and self.ready["protocol"] == "sigil-application-host/v8"
            host, port = self.ready["address"].split(":")
            assert host == "127.0.0.1"
            self.port = int(port)
        except BaseException:
            self.close()
            raise
