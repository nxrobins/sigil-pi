"""Explicit isolated v8 conformance artifact and transport, not product policy."""

import hashlib
import json
from pathlib import Path
import select
import subprocess

import pytest

from conftest import PI_ROOT, SIGIL_ROOT
from http_service_support import HttpApi, configure as configure_v7
from scripts.compose_application import ApplicationSource, strip_line_comments
from scripts.compose_http_entry import compose_http_entry

ROOT = Path(__file__).resolve().parent.parent
BASELINE = "4261e3bcb64f0b7b6f80f5e0226336cc6ff5d3c020457b4af4724bd8f327e289"


def compose_host_probe():
    baseline = compose_http_entry(PI_ROOT, SIGIL_ROOT)
    # Pin the actual source, not just a convenient protocol-marker substitution.
    if baseline.compiler_input_sha256 != BASELINE:
        raise ValueError("request host probe requires the reviewed v7 source fingerprint")
    wrapper = (PI_ROOT / "app/pi/http_api.sigil").read_bytes()
    suffix = strip_line_comments(wrapper.decode(), compact_indent=True)
    if not baseline.text.endswith(suffix):
        raise ValueError("original wrapper must be a complete exact suffix")
    body = baseline.text[:-len(suffix)]

    def replace(old, new, count):
        nonlocal body
        if body.count(old) != count:
            raise ValueError(f"expected {count} explicit probe ABI sites: {old}")
        body = body.replace(old, new)

    replace('"AH5\\n", 15', '"AH6\\n", 17', 2)
    replace('"HC5\\n"', '"HC6\\n"', 8)
    replace(r'[\"call\",\"commit\",\"metadata\",\"read\",\"reply\"]',
            r'[\"call\",\"commit\",\"metadata\",\"read\",\"read_many\",\"reply\"]', 1)
    fragment = ROOT / "tests/fixtures/request_host_probe.sigil"
    raw = fragment.read_bytes()
    body = strip_line_comments(body + raw.decode(), compact_indent=True)
    if len(body.encode()) > 65536 or body.count("pub fn tool_main(") != 1:
        raise ValueError("probe must retain the original source/entry bounds")
    inputs = {**baseline.input_hashes,
              "tests/fixtures/request_host_probe.sigil": hashlib.sha256(raw).hexdigest(),
              "tests/request_host_support.py": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    return ApplicationSource(body, inputs, baseline.stdlib_hash)


@pytest.fixture(scope="session")
def request_host_binary(native_release_service_binary, native_worker_binary, native_store_binary):
    """Integrated v8 host; all original native/evaluator gates remain required."""
    assert all(p.is_file() for p in (native_release_service_binary, native_worker_binary, native_store_binary))
    return native_release_service_binary


class RequestHostApi(HttpApi):
    """Configure an explicitly owner-installed conformance artifact, not a route."""
    def __init__(self, binary, root, programs, endpoint, workspace, *, rows=None, mode="init"):
        self.root = root
        source = compose_host_probe().text
        self.config, path = configure_v7(root, programs, endpoint, workspace, rows, source=source)
        self.config["version"] = 8
        path.write_text(json.dumps(self.config))
        self.proc = subprocess.Popen([str(binary), mode, str(path), "0"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([self.proc.stdout], [], [], 40)
            assert ready, "v8 host startup timed out"
            line = self.proc.stdout.readline()
            assert line, self.proc.stderr.read()
            self.ready = json.loads(line)
            assert self.ready["status"] == "ready"
            assert self.ready["protocol"] == "sigil-application-host/v8"
            host, port = self.ready["address"].split(":")
            assert host == "127.0.0.1"
            self.port = int(port)
        except BaseException:
            self.close()
            raise
