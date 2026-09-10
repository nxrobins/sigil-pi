"""Actual startup rejection before state creation or any local provider send."""
import hashlib
from pathlib import Path

import pytest

from api_support import TOKEN_B, credential, LIMITS as API_LIMITS
from automatic_audit_support import AutomaticAuditApi
from readiness_host_support import readiness_host_binary as readiness_host_binary
from store_support import LIMITS
from test_automatic_audit import audit_secrets as audit_secrets
from test_turn_execution import programs as programs


@pytest.mark.parametrize("member", ["heads", "records"])
@pytest.mark.parametrize("target", ["own_domain", "peer_domain", "own_claim", "own_delivery",
                                   "peer_claim", "peer_delivery", "peer_audit_head", "peer_audit_records"])
def test_audit_namespaces_are_private_across_the_whole_registry(
        readiness_host_binary, programs, scripted_llm, tmp_path, member, target):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    def patch(c):
        a, b = c["automatic"]["participants"]
        targets = {"own_domain": "a.state", "peer_domain": "b.budget",
            "own_claim": a["claim_namespace"], "own_delivery": a["delivery_namespace"],
            "peer_claim": b["claim_namespace"], "peer_delivery": b["delivery_namespace"],
            "peer_audit_head": b["transaction_audit"]["heads"], "peer_audit_records": b["transaction_audit"]["records"]}
        a["transaction_audit"][member] = targets[target]
    with pytest.raises(AssertionError, match="config"):
        AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace, rows=rows, patch=patch)
    assert not (root / "records").exists()
    assert scripted_llm.requests == []


@pytest.mark.parametrize("target", ["coordinator", "transaction", "effect", "dispatch_policy", "credential"])
def test_audit_capability_is_not_delegated_to_other_execution_roles(
        readiness_host_binary, programs, scripted_llm, tmp_path, target):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    def patch(c):
        p = c["automatic"]["participants"][0]
        namespace = p["transaction_audit"]["records"]
        if target == "coordinator": p["read_grants"][namespace] = "read"
        elif target == "transaction": p["transactions"]["interpret"]["grants"][namespace] = "read"
        elif target == "effect": p["effects"]["reader"]["grants"][namespace] = "read"
        elif target == "dispatch_policy": p["effects"]["reader"]["policy"]["read_grants"][namespace] = "read"
        else: c["credentials"][0]["grants"][namespace] = "read_write"
    with pytest.raises(AssertionError, match="config|application"):
        AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace, patch=patch)
    assert not (root / "records").exists()
    assert scripted_llm.requests == []


@pytest.mark.parametrize("kind", ["null", "wrong_version", "missing_key", "short_key", "invalid_env_name",
    "same_namespaces", "bad_namespace", "bad_chain", "payload_limit", "record_limit", "byte_limit",
    "invalid_store_limits", "network", "filesystem", "secret", "wrong_source_hash", "wrong_runtime_hash",
    "invalid_sigil", "wrong_boot_protocol", "wrong_boot_reply"])
def test_audit_key_limits_artifact_and_sigil_policy_are_admitted_before_state_creation(
        readiness_host_binary, programs, scripted_llm, tmp_path, monkeypatch, kind):
    original_limits = dict(LIMITS)
    original_api_limits = dict(API_LIMITS)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    if kind == "missing_key": monkeypatch.delenv("PI_AUTOMATIC_AUDIT_FIXTURE_KEY_0")
    if kind == "short_key": monkeypatch.setenv("PI_AUTOMATIC_AUDIT_FIXTURE_KEY_0", "too-short")
    def patch(c):
        p = c["automatic"]["participants"][0]
        a = p["transaction_audit"]
        if kind == "null": p["transaction_audit"] = None
        elif kind == "wrong_version": a["version"] = 2
        elif kind == "invalid_env_name": a["key_env"] = "invalid=name"
        elif kind == "same_namespaces": a["heads"] = a["records"]
        elif kind == "bad_namespace": a["heads"] = "audit/path"
        elif kind == "bad_chain": a["chain"] = "not-a-digest"
        elif kind == "payload_limit": a["limits"]["payload_bytes"] = 16385
        elif kind == "record_limit": a["limits"]["records"] = 0
        elif kind == "byte_limit": a["limits"]["bytes"] = 1
        elif kind == "invalid_store_limits": c["limits"]["database_pages"] = 0
        elif kind == "network": a["worker"]["net"] = ["127.0.0.1"]
        elif kind == "filesystem": a["worker"]["fs"] = [str(workspace)]
        elif kind == "secret": a["worker"]["secret_env"] = {"audit": a["key_env"]}
        elif kind == "wrong_source_hash": a["worker"]["source_sha256"] = "0" * 64
        elif kind == "wrong_runtime_hash": a["worker"]["runtime_sha256"] = "0" * 64
        elif kind in {"invalid_sigil", "wrong_boot_protocol", "wrong_boot_reply"}:
            source = Path(a["worker"]["source"])
            body = source.read_text()
            if kind == "invalid_sigil": body = "not SIGIL code"
            elif kind == "wrong_boot_protocol": body = body.replace('"AB1\\n"', '"AB0\\n"')
            else: body = body.replace('"transaction_audit_admitted"', '"not-admitted"')
            source.write_text(body)
            a["worker"]["source_sha256"] = hashlib.sha256(body.encode()).hexdigest()
    with pytest.raises(AssertionError, match="config|application|worker|deadline"):
        AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace, patch=patch)
    assert not (root / "records").exists()
    assert scripted_llm.requests == []
    assert LIMITS == original_limits
    assert API_LIMITS == original_api_limits
