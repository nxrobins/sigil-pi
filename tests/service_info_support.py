"""Static service-info test deployment on the actual audited v9 host.

Only source/configuration assembly and HTTP transport live here. The production
SIGIL entry, not a diagnostic wrapper or Python request driver, decides replies.
"""

import hashlib
import json
from pathlib import Path
import select
import subprocess

from automatic_audit_support import configure as audited_configuration
from browser_support import BrowserApi, assets
from conftest import PI_ROOT, SIGIL_ROOT
from http_service_support import HttpApi
from scripts.compose_service_info_entry import compose_service_info_entry
from test_evaluation_audit_publication import enable


class ServiceInfoApi(HttpApi):
    def __init__(self, binary, root, programs, endpoint, workspace, *, rows=None,
                 mode="init", limit=1024, version="fixture-version", public_assets=None):
        def configure(config):
            built = compose_service_info_entry(PI_ROOT, SIGIL_ROOT,
                                                request_limit=limit, version=version)
            path = root / "service-info-entry.sigil"
            path.write_text(built.text)
            config["worker"].update(source=str(path), source_sha256=built.compiler_input_sha256)
            for participant in config["automatic"]["participants"]:
                for role in ("transaction_audit", "effect_audit"):
                    enable(participant[role], root)
        self.root = root
        self.config, path = audited_configuration(root, programs, endpoint, workspace,
            rows=rows, patch=configure, audit_effects=True)
        command = [str(binary), mode, str(path), "0"]
        if public_assets is not None:
            command += ["--public-assets", str(public_assets)]
        self.proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
        try:
            readable, _, _ = select.select([self.proc.stdout], [], [], 40)
            assert readable, "audited service-info startup timed out"
            line = self.proc.stdout.readline()
            assert line, self.proc.stderr.read()
            self.ready = json.loads(line)
            assert self.ready["status"] == "ready" and self.ready["protocol"] == "sigil-application-host/v9"
            if public_assets is not None:
                assert self.ready["public_assets_sha256"] == hashlib.sha256(public_assets.read_bytes()).hexdigest()
            host, port = self.ready["address"].split(":")
            assert host == "127.0.0.1"
            self.port = int(port)
        except BaseException:
            self.close()
            raise


class ServiceInfoBrowserApi(ServiceInfoApi):
    """Identical SIGIL entry, with the original admitted browser asset bundle."""

    def __init__(self, binary, root, programs, endpoint, workspace, *, rows=None,
                 mode="init", asset_path=None, limit=1024, version="fixture-version"):
        self.asset_path = assets(root) if asset_path is None else Path(asset_path)
        # Reopen admits the exact existing bytes/manifest, never a regenerated
        # bundle. Neither browser assets nor Python implement product decisions.
        super().__init__(binary, root, programs, endpoint, workspace, rows=rows,
                         mode=mode, limit=limit, version=version, public_assets=self.asset_path)

    raw = BrowserApi.raw


class ServiceInfoRegressionBrowserApi(ServiceInfoBrowserApi):
    """Same explicit allowance as the preexisting v8 lifecycle regression."""

    def __init__(self, binary, root, programs, endpoint, workspace, *, rows=None,
                 mode="init", asset_path=None):
        # Keep real request admission, while isolating the original lifecycle,
        # uncertainty and isolation assertions. This is not a pilot allowance.
        super().__init__(binary, root, programs, endpoint, workspace, rows=rows,
                         mode=mode, asset_path=asset_path, limit=10000)
