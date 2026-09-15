"""Isolated static deployment and HTTP transport, not a Python application loop.

The explicit v7 deployment keeps all original native/evaluator prerequisites.
This module configures test artifacts and performs transport, not product policy.
"""
from contextlib import closing
import hashlib
import http.client
import json
from pathlib import Path
import select
import sqlite3
import subprocess

import pytest

from api_support import BODY, TOKEN_A, NativeApi
from browser_support import BrowserApi, assets, configure as configure_v6
from scripts.compose_http_entry import HEADER_NAMES, compose_http_entry
from conftest import API_KEY, PI_ROOT, SIGIL_ROOT
from scripts.compose_application import strip_line_comments


FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def http_fixture_secret(monkeypatch):
    # The fixed local provider's canary, required by original native admission.
    # Never substitute a real provider credential or bypass its admission check.
    monkeypatch.setenv("PI_AUTOMATIC_FIXTURE_SECRET", API_KEY)


def host_probe_source():
    """Test-only owner artifact; never substituted into the product by HTTP input."""
    source = compose_http_entry(PI_ROOT, SIGIL_ROOT).text
    signature = "pub fn tool_main(input_ptr: i64, input_len: i64)"
    assert source.count(signature) == 1
    source = source.replace(signature,
        "fn http_probe_original(input_ptr: i64 @Internal, input_len: i64 @Internal)")
    source += (FIXTURES / "http_host_probe.sigil").read_text()
    source = strip_line_comments(source, compact_indent=True)
    assert len(source.encode()) <= 65536
    assert source.count("pub fn tool_main(") == 1
    return source


@pytest.fixture(scope="session")
def http_service_binary(native_release_service_binary, native_worker_binary, native_store_binary):
    """No shortcuts around the baseline service or fixed-evaluator native gates."""
    assert all(path.is_file() for path in
               (native_release_service_binary, native_worker_binary, native_store_binary))
    return native_release_service_binary


def configure(root, programs, endpoint, workspace, rows=None, *, version=7, source=None):
    config, path = configure_v6(root, programs, endpoint, workspace, rows)
    assert version in {6, 7}
    if version == 7:
        source = compose_http_entry(PI_ROOT, SIGIL_ROOT).text if source is None else source
        assert len(source.encode()) <= 65536, "probe/application cannot widen the source limit"
        source_path = root / "http-entry.sigil"
        source_path.write_text(source)
        config["version"] = 7
        config["http"] = {"response_headers": HEADER_NAMES.copy()}
        # Keep the same fixed evaluator, fuel, timeout and empty capability grants.
        config["worker"].update(source=str(source_path), source_sha256=hashlib.sha256(source.encode()).hexdigest())
    else:
        assert source is None
    path.write_text(json.dumps(config))
    return config, path


class HttpApi(NativeApi):
    def __init__(self, binary, root, programs, endpoint, workspace, *, rows=None,
                 mode="init", version=7, source=None, public_assets=None):
        self.root = root
        self.config, path = configure(root, programs, endpoint, workspace, rows, version=version, source=source)
        command = [str(binary), mode, str(path), "0"]
        if public_assets is not None:
            command += ["--public-assets", str(public_assets)]
        self.proc = subprocess.Popen(command,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([self.proc.stdout], [], [], 40)
            assert ready, "HTTP profile host startup timed out"
            line = self.proc.stdout.readline()
            assert line, self.proc.stderr.read()
            self.ready = json.loads(line)
            assert self.ready["status"] == "ready"
            assert self.ready["protocol"] == f"sigil-application-host/v{version}"
            if public_assets is not None:
                assert self.ready["public_assets_sha256"] == hashlib.sha256(public_assets.read_bytes()).hexdigest()
            host, port = self.ready["address"].split(":")
            assert host == "127.0.0.1"
            self.port = int(port)
        except BaseException:
            self.close()
            raise

    def exchange(self, method="GET", path="/v1/sessions", *, body=None, raw=None,
                 token=TOKEN_A, hints=(), extra_headers=()):
        payload = (b"" if body is None else json.dumps(body).encode()) if raw is None else raw
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            connection.putheader("Host", self.ready["address"])
            connection.putheader("Content-Type", "application/json")
            if token is not None:
                connection.putheader("Authorization", "Bearer " + token)
            for hint in hints:
                connection.putheader("X-Request-ID", hint)
            for name, value in extra_headers:
                connection.putheader(name, value)
            connection.putheader("Content-Length", str(len(payload)))
            connection.endheaders(payload)
            response = connection.getresponse()
            pairs = [(key.lower(), value) for key, value in response.getheaders()]
            return response.status, pairs, response.read()
        finally:
            connection.close()

    def request(self, method="POST", path="/v1/operations", body=None, token=TOKEN_A, raw=None, *, hints=()):
        status, pairs, response = self.exchange(method, path, body=BODY if body is None else body,
            raw=raw, token=token, hints=hints)
        assert dict(pairs)["cache-control"] == "no-store"
        return status, json.loads(response)


class HttpBrowserApi(HttpApi):
    """Same static browser mounts and raw transport, with explicit v7 admission."""
    def __init__(self, binary, root, programs, endpoint, workspace, *, rows=None,
                 mode="init", asset_path=None):
        self.asset_path = assets(root) if asset_path is None else asset_path
        super().__init__(binary, root, programs, endpoint, workspace, rows=rows,
                         mode=mode, version=7, public_assets=self.asset_path)

    # Preserve exact malformed-wire, origin and public-asset fixture transport.
    raw = BrowserApi.raw


def state(root):
    """Read actual durable rows without creating a missing database."""
    with closing(sqlite3.connect(f"file:{root / 'records/records.sqlite'}?mode=ro", uri=True)) as db:
        return (db.execute("SELECT * FROM meta").fetchall(),
                db.execute("SELECT * FROM records ORDER BY namespace,key").fetchall())


def effect_counts(root):
    with closing(sqlite3.connect(f"file:{root / 'records/records.sqlite'}?mode=ro", uri=True)) as db:
        return dict(db.execute("SELECT namespace,COUNT(*) FROM records WHERE namespace IN "
            "('executor0.claim','executor0.delivery','executor1.claim','executor1.delivery') GROUP BY namespace"))
