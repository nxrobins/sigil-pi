"""Static v8/browser test setup and bounded client coordination, not API policy."""

import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import time

from http_service_support import HttpApi
from request_api_support import configure
from scripts.public_asset_manifest import manifest

ROOT = Path(__file__).resolve().parent.parent


class RequestBrowserApi(HttpApi):
    def __init__(self, binary, root, programs, endpoint, workspace, *, limit, rows=None,
                 mode="init", asset_path=None):
        self.root = root
        self.config, path = configure(root, programs, endpoint, workspace, rows, limit=limit)
        if asset_path is None:
            public = root / "public"
            public.mkdir()
            names = {p.name for p in (ROOT / "web").iterdir()}
            assert names == {"index.html", "app.mjs", "api.mjs", "styles.css"}
            for name in sorted(names):
                source = ROOT / "web" / name
                assert source.is_file() and not source.is_symlink()
                shutil.copy2(source, public / name)
            self.asset_path = root / "public-assets.json"
            self.asset_path.write_text(json.dumps(manifest(public), sort_keys=True, separators=(",", ":")))
        else:
            # Reopen the exact installed manifest and bytes. Native admission
            # validates them; never regenerate an asset bundle during recovery.
            self.asset_path = Path(asset_path)
        self.proc = subprocess.Popen([str(binary), mode, str(path), "0", "--public-assets", str(self.asset_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([self.proc.stdout], [], [], 40)
            assert ready, "v8 browser startup exceeded the original observation bound"
            line = self.proc.stdout.readline()
            assert line, self.proc.stderr.read()
            self.ready = json.loads(line)
            assert self.ready["status"] == "ready" and self.ready["protocol"] == "sigil-application-host/v8"
            assert self.ready["public_assets_sha256"] == hashlib.sha256(self.asset_path.read_bytes()).hexdigest()
            host, port = self.ready["address"].split(":")
            assert host == "127.0.0.1"
            self.port = int(port)
        except BaseException:
            self.close()
            raise


class RequestRegressionBrowserApi(RequestBrowserApi):
    """Explicit high test allowance for unchanged pre-v8 browser scenarios."""

    def __init__(self, binary, root, programs, endpoint, workspace, *, rows=None,
                 mode="init", asset_path=None):
        # Isolate lifecycle/tenant/unknown-delivery behavior without dropping
        # request accounting. This is not a production/pilot allowance.
        super().__init__(binary, root, programs, endpoint, workspace, limit=10000,
                         rows=rows, mode=mode, asset_path=asset_path)


def browser(config, on_connected=None):
    driver = ROOT / "tests/browser_request_admission.mjs"
    proc = subprocess.Popen(["node", str(driver)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0, env=os.environ.copy())
    events = []
    try:
        proc.stdin.write((json.dumps(config) + "\n").encode())
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], max(0, deadline - time.monotonic()))
            assert ready, "browser exceeded the original observation window"
            line = proc.stdout.readline()
            if not line:
                break
            event = json.loads(line)
            events.append(event)
            print(json.dumps(event))
            if event.get("event") == "credential_connected":
                assert on_connected is not None
                on_connected()
                proc.stdin.write(b'{"event":"credential_expired"}\n')
            if event.get("result") == "passed":
                break
        proc.stdin.close()
        code = proc.wait(timeout=15)
        errors = proc.stderr.read().decode()
        assert code == 0, errors
        assert events and events[-1].get("result") == "passed", (events, errors)
        return events[-1]
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        for stream in [proc.stdin, proc.stdout, proc.stderr]:
            stream.close()
