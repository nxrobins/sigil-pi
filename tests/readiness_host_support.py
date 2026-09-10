"""Staged v9 actual HTTP/SIGIL/native conformance, not product readiness."""

import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import subprocess

import pytest

from conftest import PI_ROOT
from http_service_support import HttpApi, configure
from request_host_support import compose_host_probe
from scripts.compose_application import ApplicationSource, strip_line_comments
from scripts.sigil_omit import compact_operator_layout


ROOT = Path(__file__).resolve().parent.parent


def compose_storage_probe():
    baseline = compose_host_probe()
    if baseline.compiler_input_sha256 != "d2f0d9423eff6a959a4e48797913c6b37964c9ec478d678d2bb2bdfe94462a7b":
        raise ValueError("require the exact existing v8 conformance source")
    wrapper = strip_line_comments((PI_ROOT / "tests/fixtures/request_host_probe.sigil").read_text(),
                                  compact_indent=True)
    if not baseline.text.endswith(wrapper):
        raise ValueError("require the exact old whole wrapper suffix")
    body = baseline.text[:-len(wrapper)]

    def replace(old, new, count):
        nonlocal body
        if body.count(old) != count:
            raise ValueError(f"expected {count} explicit v9 probe composition sites: {old}")
        body = body.replace(old, new)

    replace('"AH6\\n", 17', '"AH7\\n", 18', 2)
    replace('"HC6\\n"', '"HC7\\n"', 8)
    old = '["call","commit","metadata","read","read_many","reply"]'
    new = '["call","commit","commit_observed","inspect_execution","inspect_storage","metadata","read","read_many","read_observed","reply","temporary_read","temporary_write"]'
    replace(json.dumps(old), json.dumps(new), 1)
    fragment = ROOT / "tests/fixtures/readiness_storage_probe.sigil"
    raw = fragment.read_bytes()
    text = compact_operator_layout(strip_line_comments(body + raw.decode(), compact_indent=True))
    if len(text.encode()) > 65536 or text.count("pub fn tool_main(") != 1:
        raise ValueError("probe cannot widen entry/source ceilings")
    inputs = {**baseline.input_hashes,
              "tests/fixtures/readiness_storage_probe.sigil": hashlib.sha256(raw).hexdigest(),
              "tests/readiness_host_support.py": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "scripts/sigil_omit.py": hashlib.sha256((ROOT / "scripts/sigil_omit.py").read_bytes()).hexdigest()}
    return ApplicationSource(text, inputs, baseline.stdlib_hash)


@pytest.fixture(scope="session")
def readiness_host_binary(native_fixed_evaluator_binary):
    """Keep all original native gates and the real pinned evaluator prerequisite."""
    assert native_fixed_evaluator_binary.is_file()
    env = {**os.environ, "CARGO_BUILD_JOBS": "2", "CARGO_NET_OFFLINE": "true"}
    env.pop("CARGO_TARGET_DIR", None)
    for crate, binary in [("store", "sigil-store"), ("worker", "sigil-worker"),
                          ("service", "sigil-application-host")]:
        root = ROOT / "native" / crate
        commands = [["cargo", "fmt", "--check"],
                    ["cargo", "clippy", "--locked", "--all-targets", "--", "-D", "warnings"],
                    ["cargo", "test", "--locked", "--quiet"],
                    ["cargo", "build", "--locked", "--bins"]]
        if crate == "service":
            commands.append(["cargo", "build", "--release", "--locked", "--bins"])
        for command in commands:
            result = subprocess.run(command, cwd=root, capture_output=True, text=True,
                                    timeout=240, env=env)
            assert result.returncode == 0, f"{crate} {command}\n{result.stdout}\n{result.stderr}"
        assert (root / "target/debug" / binary).is_file()
    return ROOT / "native/service/target/release/sigil-application-host"


class ReadinessHostApi(HttpApi):
    """Owner-installed mechanism probe. No product policy or fake observations."""
    def __init__(self, binary, root, programs, endpoint, workspace, *, rows=None, mode="init", isolate_effect_runtime=False):
        self.root = root
        source = compose_storage_probe().text
        self.config, path = configure(root, programs, endpoint, workspace, rows, source=source)
        if isolate_effect_runtime:
            # Damage ONLY this private test copy, never the shared pinned runtime.
            self.effect_runtime = root / "isolated-effect-runtime"
            source = self.config["automatic"]["participants"][0]["effects"]["provider"]["worker"]["runtime"]
            shutil.copy2(source, self.effect_runtime)
            for participant in self.config["automatic"]["participants"]:
                for effect in participant["effects"].values():
                    assert effect["worker"]["runtime"] == source
                    effect["worker"]["runtime"] = str(self.effect_runtime)
        self.config["version"] = 9
        self.config["process_facts"] = {
            "namespaces": ["a.state"],
            "per_namespace": {"value_bytes": 1024, "records": 2, "live_bytes": 2048},
        }
        path.write_text(json.dumps(self.config))
        self.proc = subprocess.Popen([str(binary), mode, str(path), "0"],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([self.proc.stdout], [], [], 40)
            assert ready, "v9 host startup timed out"
            line = self.proc.stdout.readline()
            assert line, self.proc.stderr.read()
            self.ready = json.loads(line)
            assert self.ready["status"] == "ready"
            assert self.ready["protocol"] == "sigil-application-host/v9"
            host, port = self.ready["address"].split(":")
            assert host == "127.0.0.1"
            self.port = int(port)
        except BaseException:
            self.close()
            raise
