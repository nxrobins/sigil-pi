"""Build-time artifact copying; inert files here are not runtime admission evidence."""

import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from scripts.stage_native_deployment import DeploymentError, _safe_mode, stage_deployment, worker_sites


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def deployment(tmp_path):
    inputs = tmp_path / "input"
    inputs.mkdir()
    source = inputs / "program.sigil"
    source.write_bytes(b"module inert_unit_fixture;\n")
    runtime = inputs / "runtime"
    runtime.write_bytes(b"inert unit-test executable, never run\n")
    runtime.chmod(0o700)
    worker = {"version": 1, "runtime": str(runtime), "runtime_sha256": sha(runtime.read_bytes()),
        "source": str(source), "source_sha256": sha(source.read_bytes()), "max_fuel": 900,
        "max_timeout_ms": 1200, "net": [], "fs": [], "secret_env": {}}
    effect = copy.deepcopy(worker)
    effect.update(net=["explicit.example.invalid"], fs=["/explicit/workspace"],
                  secret_env={"provider": "EXPLICIT_SECRET_NAME"})
    config = {"version": 9, "worker": worker, "functions": {"helper": copy.deepcopy(worker)},
        "state_root": str(tmp_path / "original-state"), "limits": {"value_bytes": 2097152},
        "credentials": [{"facts": "opaque owner facts", "sha256": "a" * 64, "grants": {"a.state": "read_write"}}],
        "http": {"response_headers": ["x-request-id"]},
        "process_facts": {"namespaces": ["a.state"]},
        "automatic": {"participants": [{"worker": copy.deepcopy(worker), "credential_sha256": "a" * 64,
            "binding": "unchanged opaque binding", "effects": {"effect": {"worker": effect,
                "policy": {"worker": copy.deepcopy(worker), "binding": "unchanged dispatch context"},
                "recorder": {"worker": copy.deepcopy(worker)}}},
            "transactions": {"finish": {"worker": copy.deepcopy(worker), "grants": {"a.state": "read_write"}}},
            "transaction_audit": {"worker": copy.deepcopy(worker), "evaluation": copy.deepcopy(worker),
                "key_env": "AUDIT_SECRET_NAME"},
            "effect_audit": {"worker": copy.deepcopy(worker), "evaluation": copy.deepcopy(worker),
                "key_env": "EFFECT_AUDIT_SECRET_NAME"}}]}}
    config_path = inputs / "service.json"
    config_path.write_text(json.dumps(config))
    asset = inputs / "index.html"
    asset.write_bytes(b"<!doctype html><title>inert unit fixture</title>")
    assets = {"version": 1, "assets": [{"route": "/", "path": str(asset),
        "sha256": sha(asset.read_bytes()), "content_type": "text/html"}]}
    assets_path = inputs / "assets.json"
    assets_path.write_text(json.dumps(assets))
    return {"config_path": config_path, "host_path": runtime, "host_sha256": sha(runtime.read_bytes()),
            "assets_path": assets_path, "output": tmp_path / "installed", "state_root": tmp_path / "state"}


def test_exact_artifacts_complete_role_inventory_private_output_and_unchanged_policy(deployment, monkeypatch):
    original = json.loads(deployment["config_path"].read_text())
    before = {path: path.read_bytes() for path in deployment["config_path"].parent.iterdir()}
    monkeypatch.setenv("EXPLICIT_SECRET_NAME", "not-copied-secret-canary")
    result = stage_deployment(**deployment)
    output = deployment["output"]
    assert result["status"] == "development-unqualified"
    assert len(result["workers"]) == 11
    assert {row["role"] for row in result["workers"]} == {
        "entry", "function/helper", "participant/0", "participant/0/effect/effect",
        "participant/0/effect/effect/policy", "participant/0/effect/effect/recorder",
        "participant/0/transaction/finish", "participant/0/transaction_audit",
        "participant/0/transaction_audit/evaluation", "participant/0/effect_audit",
        "participant/0/effect_audit/evaluation"}
    deployed = json.loads((output / "service.json").read_text())
    for (_, old), (_, new) in zip(worker_sites(original), worker_sites(deployed), strict=True):
        for kind in ("source", "runtime"):
            assert Path(new[kind]).is_relative_to(output)
            assert Path(new[kind]).read_bytes() == Path(old[kind]).read_bytes()
            new[kind] = old[kind]
    assert deployed["state_root"] == str(deployment["state_root"])
    deployed["state_root"] = original["state_root"]
    assert deployed == original
    assert not deployment["state_root"].exists()
    assert {path: path.read_bytes() for path in before} == before
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    members = {str(path.relative_to(output)) for path in output.rglob("*") if path.is_file()}
    assert members == {row["path"] for row in result["files"]} | {"DEPLOYMENT.json"}
    assert len([row for row in result["files"] if row["path"].startswith("runtime/")]) == 1
    assert len([row for row in result["files"] if row["path"].startswith("source/")]) == 1
    for row in result["files"]:
        path = output / row["path"]
        assert sha(path.read_bytes()) == row["sha256"] and path.stat().st_size == row["bytes"]
        assert stat.S_IMODE(path.stat().st_mode) == row["mode"]
        assert b"not-copied-secret-canary" not in path.read_bytes()
    assert result["init"] == [str(output / "bin/sigil-application-host"), "init", str(output / "service.json"),
                              "0", "--public-assets", str(output / "public-assets.json")]
    assert result["open"] == [result["init"][0], "open", *result["init"][2:]]
    assert json.loads((output / "DEPLOYMENT.json").read_text()) == result


@pytest.mark.parametrize("kind", ["host", "source", "runtime", "public"])
def test_each_copied_artifact_requires_the_actual_supplied_digest(deployment, kind):
    if kind == "host":
        deployment["host_sha256"] = "0" * 64
    elif kind == "public":
        assets = json.loads(deployment["assets_path"].read_text())
        assets["assets"][0]["sha256"] = "0" * 64
        deployment["assets_path"].write_text(json.dumps(assets))
    else:
        config = json.loads(deployment["config_path"].read_text())
        config["worker"][kind + "_sha256"] = "0" * 64
        deployment["config_path"].write_text(json.dumps(config))
    with pytest.raises(DeploymentError, match="digest does not match"):
        stage_deployment(**deployment)
    assert not (deployment["output"] / "DEPLOYMENT.json").exists()
    assert not deployment["state_root"].exists()


@pytest.mark.parametrize("kind", ["host", "source", "public", "config", "assets"])
def test_final_component_symlinks_are_not_followed(deployment, kind):
    if kind in {"host", "config", "assets"}:
        key = kind + "_path"
        source = deployment[key]
    elif kind == "source":
        config = json.loads(deployment["config_path"].read_text())
        source = Path(config["worker"]["source"])
    else:
        assets = json.loads(deployment["assets_path"].read_text())
        source = Path(assets["assets"][0]["path"])
    link = source.with_name("link-" + source.name)
    link.symlink_to(source)
    if kind in {"host", "config", "assets"}: deployment[key] = link
    elif kind == "source":
        config["worker"]["source"] = str(link)
        deployment["config_path"].write_text(json.dumps(config))
    else:
        assets["assets"][0]["path"] = str(link)
        deployment["assets_path"].write_text(json.dumps(assets))
    with pytest.raises(DeploymentError, match="regular file"):
        stage_deployment(**deployment)
    assert source.is_file() and not deployment["state_root"].exists()


@pytest.mark.parametrize("mode", [0o600, 0o722])
def test_host_requires_safe_executable_mode(deployment, mode):
    deployment["host_path"].chmod(mode)
    with pytest.raises(DeploymentError, match="type, mode or size"):
        stage_deployment(**deployment)


@pytest.mark.parametrize("mode,executable,allowed", [
    (0o4700, True, False), (0o2700, True, False), (0o4600, False, False),
    (0o600, True, False), (0o600, False, True), (0o644, False, True),
    (0o700, True, True), (0o755, True, True), (0o775, True, False), (0o707, True, False),
])
def test_mode_bit_contract_including_bits_the_sandbox_cannot_set(mode, executable, allowed):
    # This filesystem clears set-ID bits at chmod. Explicit bit-level evidence
    # tests the same production predicate; it is not an actual set-ID file claim.
    assert _safe_mode(stat.S_IFREG | mode, executable) is allowed


@pytest.mark.parametrize("target", ["output", "state_root"])
def test_existing_target_and_contents_are_untouched(deployment, target):
    deployment[target].mkdir()
    sentinel = deployment[target] / "preserve"
    sentinel.write_bytes(b"existing user data")
    with pytest.raises(DeploymentError, match="already exists"):
        stage_deployment(**deployment)
    assert sentinel.read_bytes() == b"existing user data"


@pytest.mark.parametrize("change", ["same", "state_inside", "output_inside", "relative", "parent"])
def test_paths_cannot_overlap_or_implicitly_resolve_parent_traversal(deployment, change):
    if change == "same": deployment["state_root"] = deployment["output"]
    elif change == "state_inside": deployment["state_root"] = deployment["output"] / "state"
    elif change == "output_inside": deployment["output"] = deployment["state_root"] / "install"
    elif change == "relative": deployment["output"] = Path("relative")
    else: deployment["output"] = deployment["output"] / ".." / "elsewhere"
    with pytest.raises(DeploymentError):
        stage_deployment(**deployment)


@pytest.mark.parametrize("raw", [b'{"version":9,"version":9}', b'{"version":NaN}', b'\xff', b'{'])
def test_noncanonical_or_invalid_json_is_not_silently_normalized(deployment, raw):
    deployment["config_path"].write_bytes(raw)
    with pytest.raises(DeploymentError):
        stage_deployment(**deployment)
    assert not deployment["output"].exists()


@pytest.mark.parametrize("change", ["version", "boolean_version", "unknown_top", "unknown_worker", "missing_worker",
                                    "empty_participants", "missing_effect_worker", "null_evaluation"])
def test_unknown_or_incomplete_worker_inventory_is_not_packaged(deployment, change):
    config = json.loads(deployment["config_path"].read_text())
    if change == "version": config["version"] = 8
    elif change == "boolean_version": config["version"] = True
    elif change == "unknown_top": config["hidden_worker"] = config["worker"]
    elif change == "unknown_worker": config["worker"]["extra"] = "not silently stripped"
    elif change == "missing_worker": del config["worker"]["source"]
    elif change == "empty_participants": config["automatic"]["participants"] = []
    elif change == "missing_effect_worker": del config["automatic"]["participants"][0]["effects"]["effect"]["worker"]
    else: config["automatic"]["participants"][0]["effect_audit"]["evaluation"] = None
    deployment["config_path"].write_text(json.dumps(config))
    with pytest.raises(DeploymentError):
        stage_deployment(**deployment)
    assert not deployment["output"].exists()


@pytest.mark.parametrize("size", [65536, 65537])
def test_original_source_ceiling_is_preserved(deployment, size):
    config = json.loads(deployment["config_path"].read_text())
    raw = b"x" * size
    source = Path(config["worker"]["source"])
    source.write_bytes(raw)
    for _, worker in worker_sites(config): worker["source_sha256"] = sha(raw)
    deployment["config_path"].write_text(json.dumps(config))
    if size == 65536:
        result = stage_deployment(**deployment)
        assert any(row["bytes"] == size for row in result["files"])
    else:
        with pytest.raises(DeploymentError, match="type, mode or size"):
            stage_deployment(**deployment)


def test_fifo_cannot_block_artifact_copy(deployment):
    fifo = deployment["host_path"].with_name("fifo")
    os.mkfifo(fifo, 0o700)
    deployment["host_path"] = fifo
    with pytest.raises(DeploymentError, match="type, mode or size"):
        stage_deployment(**deployment)


def test_artifact_and_state_aliases_cannot_name_the_same_physical_directory(deployment):
    output = deployment["output"]
    alias = output.parent / "parent-alias"
    alias.symlink_to(output.parent, target_is_directory=True)
    deployment["state_root"] = alias / output.name
    assert deployment["state_root"].resolve() == output.resolve()
    with pytest.raises(DeploymentError, match="disjoint"):
        stage_deployment(**deployment)
    assert not output.exists()


@pytest.mark.parametrize("kind", ["missing", "regular_file"])
def test_state_parent_must_be_an_existing_directory(deployment, kind):
    parent = deployment["state_root"].parent / "bad-parent"
    if kind == "regular_file": parent.write_bytes(b"preserve existing file")
    deployment["state_root"] = parent / "records"
    with pytest.raises(DeploymentError, match="parent"):
        stage_deployment(**deployment)
    assert not deployment["output"].exists()
    if kind == "regular_file": assert parent.read_bytes() == b"preserve existing file"


def test_disjoint_symlinked_parents_preserve_the_explicit_operator_paths(deployment):
    alias = deployment["state_root"].parent / "allowed-parent-alias"
    alias.symlink_to(deployment["state_root"].parent, target_is_directory=True)
    deployment["state_root"] = alias / "separate-state"
    result = stage_deployment(**deployment)
    config = json.loads(Path(result["init"][2]).read_text())
    assert config["state_root"] == str(deployment["state_root"])
    assert not deployment["state_root"].exists()


def test_filesystem_case_equivalence_cannot_make_state_equal_artifacts(deployment):
    output = deployment["output"]
    probe = output.parent / "Case-Probe"
    probe.write_bytes(b"detect actual filesystem name equivalence")
    aliases = probe.with_name("case-probe").exists()
    deployment["state_root"] = output.with_name(output.name.upper())
    if aliases:
        with pytest.raises(DeploymentError, match="disjoint"):
            stage_deployment(**deployment)
        assert not (output / "DEPLOYMENT.json").exists()
    else:
        stage_deployment(**deployment)
        assert not deployment["state_root"].exists()


@pytest.mark.parametrize("spaces", [False, True])
def test_documented_cli_materializes_the_inventory_without_running_an_inert_host(deployment, spaces):
    if spaces:
        deployment["output"] = deployment["output"].with_name("deployment with spaces")
    script = Path(__file__).resolve().parents[1] / "scripts/stage_native_deployment.py"
    command = [sys.executable, str(script)]
    for flag, key in [("config", "config_path"), ("host", "host_path"), ("host-sha256", "host_sha256"),
                      ("assets", "assets_path"), ("output", "output"), ("state-root", "state_root")]:
        command += ["--" + flag, str(deployment[key])]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0 and result.stderr == ""
    manifest = json.loads((deployment["output"] / "DEPLOYMENT.json").read_text())
    assert json.loads(result.stdout) == manifest
    assert Path(manifest["init"][0]).read_bytes() == deployment["host_path"].read_bytes()
    assert not deployment["state_root"].exists()
    assert not list(deployment["output"].rglob("*.py"))


def test_cli_requires_all_explicit_configuration_choices(deployment):
    script = Path(__file__).resolve().parents[1] / "scripts/stage_native_deployment.py"
    result = subprocess.run([sys.executable, str(script), "--output", str(deployment["output"])],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2 and result.stdout == ""
    for flag in ("--config", "--host", "--host-sha256", "--assets", "--state-root"):
        assert flag in result.stderr
    assert not deployment["output"].exists() and not deployment["state_root"].exists()
