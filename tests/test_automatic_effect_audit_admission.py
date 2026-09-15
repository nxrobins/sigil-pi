"""Actual effect-audit admission must precede state creation and dispatch."""
import hashlib
from pathlib import Path

import pytest

from api_support import TOKEN_B, credential, LIMITS as API_LIMITS
from automatic_audit_support import AutomaticAuditApi
from readiness_host_support import readiness_host_binary as readiness_host_binary
from store_support import LIMITS
from test_automatic_audit import audit_secrets as audit_secrets
from test_automatic_effect_audit import effect_secrets as effect_secrets
from test_turn_execution import programs as programs


@pytest.mark.parametrize("member", ["heads", "records"])
@pytest.mark.parametrize("target", ["own_domain", "peer_domain", "own_claim", "own_delivery",
    "peer_claim", "peer_delivery", "own_transaction_head", "own_transaction_records",
    "peer_transaction_head", "peer_transaction_records", "peer_effect_head", "peer_effect_records"])
def test_effect_audit_namespaces_are_private_across_all_roles_and_participants(
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
            "own_transaction_head": a["transaction_audit"]["heads"],
            "own_transaction_records": a["transaction_audit"]["records"],
            "peer_transaction_head": b["transaction_audit"]["heads"],
            "peer_transaction_records": b["transaction_audit"]["records"],
            "peer_effect_head": b["effect_audit"]["heads"],
            "peer_effect_records": b["effect_audit"]["records"]}
        a["effect_audit"][member] = targets[target]
    with pytest.raises(AssertionError, match="config"):
        AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace,
                          rows=rows, audit_effects=True, patch=patch)
    assert not (root / "records").exists()
    assert scripted_llm.requests == []


@pytest.mark.parametrize("target", ["coordinator", "transaction", "effect", "dispatch_policy", "credential"])
def test_effect_audit_capability_cannot_be_delegated_to_execution_roles(
        readiness_host_binary, programs, scripted_llm, tmp_path, target):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    def patch(c):
        p = c["automatic"]["participants"][0]
        namespace = p["effect_audit"]["records"]
        if target == "coordinator": p["read_grants"][namespace] = "read"
        elif target == "transaction": p["transactions"]["interpret"]["grants"][namespace] = "read"
        elif target == "effect": p["effects"]["reader"]["grants"][namespace] = "read"
        elif target == "dispatch_policy": p["effects"]["reader"]["policy"]["read_grants"][namespace] = "read"
        else: c["credentials"][0]["grants"][namespace] = "read_write"
    with pytest.raises(AssertionError, match="config|application"):
        AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace,
                          audit_effects=True, patch=patch)
    assert not (root / "records").exists()
    assert scripted_llm.requests == []


@pytest.mark.parametrize("kind", ["null", "wrong_version", "missing_key", "short_key", "invalid_env_name",
    "same_namespaces", "bad_namespace", "bad_chain", "payload_limit", "record_limit", "byte_limit",
    "invalid_store_limits", "network", "filesystem", "secret", "wrong_source_hash", "wrong_runtime_hash",
    "invalid_sigil", "wrong_boot_protocol", "wrong_reply_protocol", "wrong_boot_reply", "empty_context",
    "oversized_context"])
def test_effect_audit_key_limits_artifact_and_boot_protocol_fail_before_state_creation(
        readiness_host_binary, programs, scripted_llm, tmp_path, monkeypatch, kind):
    original_limits, original_api_limits = dict(LIMITS), dict(API_LIMITS)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    if kind == "missing_key": monkeypatch.delenv("PI_AUTOMATIC_EFFECT_AUDIT_FIXTURE_KEY_0")
    if kind == "short_key": monkeypatch.setenv("PI_AUTOMATIC_EFFECT_AUDIT_FIXTURE_KEY_0", "too-short")
    def patch(c):
        p = c["automatic"]["participants"][0]
        a = p["effect_audit"]
        if kind == "null": p["effect_audit"] = None
        elif kind == "wrong_version": a["version"] = 2
        elif kind == "invalid_env_name": a["key_env"] = "invalid=name"
        elif kind == "same_namespaces": a["heads"] = a["records"]
        elif kind == "bad_namespace": a["heads"] = "effects/path"
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
        elif kind in {"invalid_sigil", "wrong_boot_protocol", "wrong_reply_protocol", "wrong_boot_reply",
                      "empty_context", "oversized_context"}:
            source = Path(a["worker"]["source"])
            body = source.read_text()
            if kind == "invalid_sigil": body = "not SIGIL code"
            else:
                replacements = {
                    "wrong_boot_protocol": ('"WB1\\n"', '"WB0\\n"'),
                    "wrong_reply_protocol": ('"WB2\\n"', '"WB0\\n"'),
                    "wrong_boot_reply": ('"effect_audit_admitted"', '"not-admitted"'),
                    "empty_context": ('put(out, 1, bound);', 'put(out, 1, text(""));'),
                    "oversized_context": ('put(out, 1, bound);', 'put(out, 1, text("' + "x" * 1025 + '"));'),
                }
                old, new = replacements[kind]
                assert old in body
                body = body.replace(old, new)
            source.write_text(body)
            a["worker"]["source_sha256"] = hashlib.sha256(body.encode()).hexdigest()
    error = "protocol" if kind == "wrong_reply_protocol" else "config|application|worker|deadline"
    with pytest.raises(AssertionError, match=error):
        AutomaticAuditApi(readiness_host_binary, root, programs, scripted_llm.url, workspace,
                          audit_effects=True, patch=patch)
    assert not (root / "records").exists()
    assert scripted_llm.requests == []
    assert LIMITS == original_limits and API_LIMITS == original_api_limits
