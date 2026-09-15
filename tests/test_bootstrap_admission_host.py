"""Actual v9 startup must reject incomplete bootstrap before durable state.

The ordinary product entry runs, except for explicitly labelled hostile-owner
guard probes. Fixed function fixtures remain grantless and hash-admitted.
"""

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from api_support import TOKEN_B, credential
from conftest import SIGIL_ROOT
from http_service_support import http_fixture_secret as http_fixture_secret, state
from readiness_admission_host_support import AdmissionHostApi, configure_admission
from readiness_host_support import readiness_host_binary as readiness_host_binary
from scripts.compose_application import compose_application
from test_admission import profile
from test_turn_execution import programs as programs


pytestmark = pytest.mark.usefixtures("http_fixture_secret")


@pytest.mark.parametrize("change", ["old_admission", "old_result", "extra_grant", "missing_grant",
    "cross_tenant_namespace", "same_tenant_namespace", "same_tenant_profile", "rotated_epoch",
    "invalid_profile", "scope_typo", "numeric_tool", "duplicate_digest", "guest_network",
    "missing_guard", "expired_guard"])
def test_rejected_bootstrap_never_creates_state_or_dispatches_effects(
        readiness_host_binary, programs, scripted_llm, tmp_path, change):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    rows = [credential()]
    if change == "extra_grant": rows[0]["grants"]["b.operations"] = "read_write"
    if change == "missing_grant": rows[0]["grants"].pop("a.reservation")
    if change == "cross_tenant_namespace": rows.append(credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="a"))
    if change == "same_tenant_namespace": rows.append(credential(TOKEN_B, principal="bob", prefix="b"))
    if change == "same_tenant_profile": rows.append(credential(TOKEN_B, principal="bob", profile=profile(turns=0)))
    if change == "rotated_epoch": rows.append(credential(TOKEN_B, epoch="epoch-2"))
    if change == "invalid_profile": rows[0] = credential(profile=profile(per_turn="01"))
    if change == "scope_typo": rows[0] = credential(scopes=["administrator"])
    if change == "numeric_tool": rows[0] = credential(tools=[42])
    if change == "duplicate_digest": rows.append(credential())
    config, path = configure_admission(root, programs, scripted_llm.url, workspace, limit=32, rows=rows)

    def install(worker, source):
        assert len(source.encode()) <= 65536
        Path(worker["source"]).write_text(source)
        worker["source_sha256"] = hashlib.sha256(source.encode()).hexdigest()

    if change == "old_admission":
        install(config["functions"]["admission"], compose_application("admission", SIGIL_ROOT).text)
    if change == "old_result":
        # A distinct, verified owner fixture deliberately supplies the old result
        # even for AV2. The real entry must reject that narrower evidence.
        worker = config["functions"]["admission"]
        original = Path(worker["source"]).read_text()
        assert original.count('text("registry_validated")') == 1
        install(worker, original.replace('text("registry_validated")', 'text("profiles_validated")'))
    if change == "guest_network": config["worker"]["net"] = ["*"]
    if change in {"missing_guard", "expired_guard"}:
        # Host conformance probe, not an application variant. Mutate ONLY the
        # final 204 guard after the actual AV2 function has accepted the registry.
        worker = config["worker"]
        original = Path(worker["source"]).read_text()
        target = 'return time_guard(command("reply",text("204"),text(""),text("")),decimal(now),decimal(active_until));'
        assert original.count(target) == 1
        response = 'command("reply",text("204"),text(""),text(""))'
        if change == "expired_guard":
            response = 'time_guard(' + response + ',text("1"),text("2"))'
        install(worker, original.replace(target, 'return ' + response + ';'))
    path.write_text(json.dumps(config))
    result = subprocess.run([str(readiness_host_binary), "init", str(path), "0"],
                            capture_output=True, text=True, timeout=40)
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert '"status":"ready"' not in result.stdout
    assert not (root / "records").exists()
    assert scripted_llm.requests == []
    if change in {"missing_guard", "expired_guard"}:
        expected = "protocol" if change == "missing_guard" else "time_guard"
        assert '"code":"' + expected + '"' in result.stderr, (result.stdout, result.stderr)


@pytest.mark.parametrize("change", ["old_admission", "cross_tenant_namespace"])
def test_refused_reopen_preserves_existing_durable_state(
        readiness_host_binary, programs, scripted_llm, tmp_path, change):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    with AdmissionHostApi(readiness_host_binary, root, programs, scripted_llm.url, workspace, limit=32) as api:
        # A real authenticated read creates durable request accounting first.
        assert api.exchange("GET", "/v1/sessions")[0] == 200
    retained = state(root)
    assert retained[1]
    rows = [credential()]
    if change == "cross_tenant_namespace":
        rows.append(credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="a"))
    config, path = configure_admission(root, programs, scripted_llm.url, workspace, limit=32, rows=rows)
    if change == "old_admission":
        old = compose_application("admission", SIGIL_ROOT)
        worker = config["functions"]["admission"]
        Path(worker["source"]).write_text(old.text)
        worker["source_sha256"] = old.compiler_input_sha256
    path.write_text(json.dumps(config))
    result = subprocess.run([str(readiness_host_binary), "open", str(path), "0"],
                            capture_output=True, text=True, timeout=40)
    assert result.returncode != 0 and '"status":"ready"' not in result.stdout
    assert state(root) == retained
    assert scripted_llm.requests == []
