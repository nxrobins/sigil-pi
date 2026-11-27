"""Native conformance for the actual draft admission path, NOT readiness parity."""

import hashlib
import json
from pathlib import Path
import select
import subprocess

from conftest import PI_ROOT, SIGIL_ROOT
from http_service_support import HttpApi, configure as configure_http
from readiness_admission_support import source as admission_source
from request_policy_support import compose_request_policy
from scripts.compose_application import ApplicationSource, strip_line_comments
from scripts.compose_bootstrap_admission import compose_bootstrap_admission
from scripts.compose_emergency_policy import compose_emergency_policy
from scripts.sigil_omit import compact_operator_layout
from turn_support import fields


ROOT = Path(__file__).resolve().parent.parent


def compose_admission_probe(limit):
    original = admission_source(limit)
    anchor = 'if process_pair(get(x,17))<0{return-400;}'
    assert original.text.count(anchor) == 1
    # Preserve the complete original public wrapper and authentication checks;
    # intercept ONLY the otherwise unfinished final inspection continuation.
    body = original.text.replace(anchor, anchor +
        'let diagnostic:i64@Internal=admission_probe_terminal(x,c);if diagnostic!=0{return diagnostic;}')
    fragment = ROOT / "tests/fixtures/readiness_admission_probe.sigil"
    raw = fragment.read_bytes()
    text = compact_operator_layout(strip_line_comments(body + raw.decode(), compact_indent=True))
    assert len(text.encode()) <= 65536 and text.count("pub fn tool_main(") == 1
    return ApplicationSource(text, {**original.input_hashes,
        "tests/fixtures/readiness_admission_probe.sigil": hashlib.sha256(raw).hexdigest(),
        "tests/readiness_admission_host_support.py": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        original.stdlib_hash)


def configure_admission(root, programs, endpoint, workspace, *, limit, rows=None):
    source = compose_admission_probe(limit)
    config, path = configure_http(root, programs, endpoint, workspace, rows, source=source.text)
    config["version"] = 9
    config["process_facts"] = {
        "namespaces": sorted({fields(row["facts"], "CF2\n", 15)[12] for row in config["credentials"]}),
        "per_namespace": {"value_bytes": 65536, "records": 1, "live_bytes": 65536},
        "lifecycle": {"source": "posix_sigusr1"},
    }
    for name, policy in [("admission", compose_bootstrap_admission(PI_ROOT, SIGIL_ROOT)),
                         ("request_policy", compose_request_policy()),
                         ("emergency_policy", compose_emergency_policy(ROOT, SIGIL_ROOT))]:
        policy_path = root / (name + ".sigil")
        policy_path.write_text(policy.text)
        config["functions"][name] = {**config["worker"], "source": str(policy_path),
                                    "source_sha256": policy.compiler_input_sha256}
    path.write_text(json.dumps(config))
    return config, path


class AdmissionHostApi(HttpApi):
    def __init__(self, binary, root, programs, endpoint, workspace, *, limit, rows=None, mode="init"):
        self.root = root
        self.config, path = configure_admission(root, programs, endpoint, workspace, limit=limit, rows=rows)
        self.proc = subprocess.Popen([str(binary), mode, str(path), "0"],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([self.proc.stdout], [], [], 40)
            assert ready, "draft admission host startup timed out"
            line = self.proc.stdout.readline()
            assert line, self.proc.stderr.read()
            self.ready = json.loads(line)
            assert self.ready["status"] == "ready" and self.ready["protocol"] == "sigil-application-host/v9"
            host, port = self.ready["address"].split(":")
            assert host == "127.0.0.1"
            self.port = int(port)
        except BaseException:
            self.close()
            raise
