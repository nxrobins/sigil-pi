"""Real copied native/SIGIL/browser deployment, not clean-Linux pilot qualification."""

import hashlib
import json
import os
from pathlib import Path
import select
import subprocess

import pytest

import test_browser_flow as original_flow
from api_support import TOKEN_A, TOKEN_B, credential
from automatic_audit_support import configure, events
from browser_support import assets
from conftest import PI_ROOT, SIGIL_ROOT
from http_service_support import HttpApi, state, http_fixture_secret as http_fixture_secret
from readiness_host_support import readiness_host_binary as readiness_host_binary
from scripts.compose_service_info_entry import compose_service_info_entry
from scripts.stage_native_deployment import stage_deployment, worker_sites
from test_automatic_audit import audit_secrets as audit_secrets
from test_automatic_effect_audit import effect_secrets as effect_secrets
from test_evaluation_audit_publication import enable
from test_turn_execution import programs as programs


pytestmark = pytest.mark.usefixtures("audit_secrets", "effect_secrets", "http_fixture_secret")


def without_checkout_environment():
    env = os.environ.copy()
    for name in ("SIGIL_ROOT", "PI_FORGE_BIN", "PI_TOOLCHAIN_DIR", "PI_STDLIB_DIR",
                 "PYTHONPATH", "VIRTUAL_ENV", "CARGO_TARGET_DIR"):
        env.pop(name, None)
    return env


def prepare(binary, root, programs, endpoint, workspace, rows=None):
    source_root = root / "input"

    def select_actual_entry(config):
        source = compose_service_info_entry(PI_ROOT, SIGIL_ROOT, request_limit=10000,
                                           version="native-deployment-fixture")
        path = source_root / "service-info-entry.sigil"
        path.write_text(source.text)
        config["worker"].update(source=str(path), source_sha256=source.compiler_input_sha256)
        for participant in config["automatic"]["participants"]:
            for role in ("transaction_audit", "effect_audit"):
                enable(participant[role], source_root)

    original, config_path = configure(source_root, programs, endpoint, workspace,
        rows=rows, patch=select_actual_entry, audit_effects=True)
    retained = root / "retained"
    retained.mkdir(mode=0o700)
    output = root / "deployment"
    evidence = stage_deployment(config_path=config_path, host_path=binary,
        host_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(), assets_path=assets(source_root),
        output=output, state_root=retained / "records")
    deployed = json.loads((output / "service.json").read_text())
    old_rows, new_rows = worker_sites(original), worker_sites(deployed)
    old_sites, new_sites = dict(old_rows), dict(new_rows)
    assert len(old_rows) == len(old_sites) and len(new_rows) == len(new_sites)
    assert old_sites.keys() == new_sites.keys()
    # JSON object order is not worker identity; the native registries also use
    # named maps. Retain exact bytes and every non-path field for EACH role.
    for role, old in old_sites.items():
        new = new_sites[role]
        for kind in ("runtime", "source"):
            assert Path(new[kind]).is_relative_to(output)
            assert Path(new[kind]).read_bytes() == Path(old[kind]).read_bytes()
        assert {k: v for k, v in old.items() if k not in {"runtime", "source"}} == {
            k: v for k, v in new.items() if k not in {"runtime", "source"}}
    source_root.rename(root / "input-unavailable-at-original-path")
    assert not source_root.exists()
    empty = root / "empty-working-directory"
    empty.mkdir()
    assert not list(empty.iterdir())
    return evidence, deployed, retained, empty


class InstalledApi(HttpApi):
    def __init__(self, binary, root, programs, endpoint, workspace, *, rows=None):
        self.evidence, self.config, self.root, self.empty = prepare(binary, root, programs, endpoint, workspace, rows)
        self.start("init")

    def start(self, mode):
        self.proc = subprocess.Popen(self.evidence[mode], cwd=self.empty, env=without_checkout_environment(),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([self.proc.stdout], [], [], 40)
            assert ready, "copied native service startup timed out"
            line = self.proc.stdout.readline()
            assert line, self.proc.stderr.read()
            self.ready = json.loads(line)
            assert self.ready["status"] == "ready" and self.ready["protocol"] == "sigil-application-host/v9"
            host, port = self.ready["address"].split(":")
            assert host == "127.0.0.1"
            self.port = int(port)
            assert self.ready["public_assets_sha256"] == hashlib.sha256(
                Path(self.evidence[mode][-1]).read_bytes()).hexdigest()
        except BaseException:
            self.close()
            raise


@pytest.mark.parametrize("mode", ["basic", "lost_ack"])
def test_copied_deployment_runs_original_browser_api_story_and_restarts_without_source_checkout(
        readiness_host_binary, browser_runtime, programs, scripted_llm, tmp_path, monkeypatch, mode):
    assert browser_runtime["playwright"]
    created = []

    def installed(*args, **kwargs):
        api = InstalledApi(*args, **kwargs)
        created.append(api)
        return api

    monkeypatch.setattr(original_flow, "BrowserApi", installed)
    # Keep every original assertion, browser driver, fixture and timeout. This
    # replaces only static build/launch, not service replies or agent decisions.
    original_flow.test_real_browser_shared_service_flow(readiness_host_binary, programs,
        scripted_llm, tmp_path, monkeypatch, mode)
    assert len(created) == 1
    api = created[0]
    retained = state(api.root)
    audits = [events(api.root, api.config, index, effects=True) for index in range(2)]
    assert [len(items) for items in audits] == ([10, 2] if mode == "basic" else [6, 0])
    count = len(scripted_llm.requests)
    api.start("open")
    try:
        assert state(api.root) == retained
        assert [events(api.root, api.config, index, effects=True) for index in range(2)] == audits
        status, history = api.request("GET", "/v1/sessions/same-name/messages", raw=b"", token=TOKEN_A)
        assert status == 200 and "ALICE Ada canary." in json.dumps(history)
        assert "BOB private canary." not in json.dumps(history)
        status, other = api.request("GET", "/v1/sessions/same-name/messages", raw=b"", token=TOKEN_B)
        assert status == (200 if mode == "basic" else 404)
        assert "ALICE" not in json.dumps(other)
        assert len(scripted_llm.requests) == count
    finally:
        api.close()
    assert not list(api.empty.iterdir())


@pytest.mark.parametrize("kind", ["entry", "runtime", "asset"])
def test_copied_artifact_tampering_fails_native_admission_before_state_creation(
        readiness_host_binary, programs, scripted_llm, tmp_path, kind):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    evidence, config, retained, empty = prepare(readiness_host_binary, tmp_path / "service",
                                               programs, scripted_llm.url, workspace, [credential()])
    if kind == "asset":
        changed = Path(json.loads(Path(evidence["init"][-1]).read_text())["assets"][0]["path"])
    else:
        changed = Path(config["worker"]["source" if kind == "entry" else "runtime"])
    changed.write_bytes(changed.read_bytes() + b"\n")
    result = subprocess.run(evidence["init"], cwd=empty, env=without_checkout_environment(),
                            capture_output=True, text=True, timeout=40)
    assert result.returncode != 0 and not result.stdout
    assert not (retained / "records").exists()
    assert scripted_llm.requests == []
