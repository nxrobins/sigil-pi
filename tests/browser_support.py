"""Static test deployment and HTTP byte transport; SIGIL still owns every turn."""
import hashlib
import http.client
import json
from pathlib import Path
import select
import shutil
import subprocess

from api_support import NativeApi
from automatic_support import configuration
from conftest import FIXED_EVALUATOR_BIN
from listing_support import compose_entry, compose_listing
from scripts.public_asset_manifest import manifest
from turn_support import FUEL
from worker_support import runtime_digest


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def configure(root, programs, endpoint, workspace, rows=None):
    config = configuration(root, programs, endpoint, workspace, rows)

    def fixed(name, built):
        path = root / (name + ".sigil")
        path.write_text(built.text)
        return {"version": 1, "runtime": str(FIXED_EVALUATOR_BIN),
            "runtime_sha256": runtime_digest(str(FIXED_EVALUATOR_BIN)),
            "source": str(path), "source_sha256": hashlib.sha256(built.text.encode()).hexdigest(),
            "max_fuel": FUEL, "max_timeout_ms": 15000, "net": [], "fs": [], "secret_env": {}}

    config["version"] = 6
    config["worker"] = fixed("listing-entry", compose_entry())
    config["functions"]["listing"] = fixed("listing", compose_listing())
    config_path = root / "service.json"
    config_path.write_text(json.dumps(config))
    return config, config_path


def assets(root):
    web = root / "public"
    web.mkdir(parents=True)
    for source in (PROJECT_ROOT / "web").iterdir():
        if source.is_file():
            shutil.copy2(source, web / source.name)
    path = root / "public-assets.json"
    path.write_text(json.dumps(manifest(web), sort_keys=True, separators=(",", ":")))
    return path


class BrowserApi(NativeApi):
    def __init__(self, binary, root, programs, endpoint, workspace, *, rows=None, mode="init", asset_path=None):
        self.root = root
        self.config, config_path = configure(root, programs, endpoint, workspace, rows)
        self.asset_path = assets(root) if asset_path is None else asset_path
        self.proc = subprocess.Popen([str(binary), mode, str(config_path), "0", "--public-assets", str(self.asset_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([self.proc.stdout], [], [], 40)
            assert ready, "browser host startup timed out"
            line = self.proc.stdout.readline()
            assert line, self.proc.stderr.read()
            self.ready = json.loads(line)
            assert self.ready["status"] == "ready"
            assert self.ready["protocol"] == "sigil-application-host/v6"
            assert self.ready["public_assets_sha256"] == hashlib.sha256(self.asset_path.read_bytes()).hexdigest()
            host, port = self.ready["address"].split(":")
            assert host == "127.0.0.1"
            self.port = int(port)
        except BaseException:
            self.close()
            raise

    def raw(self, method="GET", path="/", *, payload=b"", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            pairs = [("Host", self.ready["address"])] if headers is None else headers
            for key, value in pairs:
                connection.putheader(key, value)
            connection.putheader("Content-Length", str(len(payload)))
            connection.endheaders(payload)
            response = connection.getresponse()
            return response.status, dict((key.lower(), value) for key, value in response.getheaders()), response.read()
        finally:
            connection.close()
