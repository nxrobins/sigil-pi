"""Actual HTTP/native/SIGIL v8 mechanism and request-admission evidence."""

import hashlib
import json
import re
import time

import pytest

from api_support import TOKEN_A, TOKEN_B, credential
from conftest import API_KEY
from http_service_support import http_fixture_secret as http_fixture_secret, state
from request_host_support import ROOT, RequestHostApi, compose_host_probe, request_host_binary as request_host_binary
from test_http_host_boundary import (response_command,
    test_actual_host_refuses_bad_response_metadata_and_preserves_time_guards,
    test_actual_transport_keeps_native_facts_across_scoped_read_and_separates_entropy)
from test_turn_execution import programs as programs
from turn_support import fields, record
from request_api_checks import (
    test_v8_configuration_preserves_native_and_effect_limits_and_adds_only_a_grantless_policy,
    test_real_requests_commit_accounting_before_route_errors_and_stop_at_the_limit,
    test_request_quota_survives_restart_and_credential_rotation_without_cross_tenant_reset,
    test_real_rate_admitted_tool_turn_followup_replay_and_restart_fit_the_unchanged_host,
    test_all_route_scope_denials_count_requests_without_parsing_bodies_or_changing_domain_state,
    test_rate_admitted_cancellation_after_delivery_preserves_uncertainty_across_restart,
)
from request_token_checks import (
    request_lexer_oracle as request_lexer_oracle,
    test_full_layout_has_identical_real_compiler_tokens_and_literal_values,
    test_real_token_oracle_detects_a_changed_literal,
)
from request_compat_checks import (
    test_actual_frozen_v7_rejects_request_admission_before_state_creation,
    test_unchanged_v7_profile_retains_tool_turn_and_new_followup_across_both_host_directions,
)

# These imports register the complete test functions. Root conftest supplies
# their original native/compiler prerequisites; do not re-export its fixtures
# here, which would register duplicate module-local session instances.
__all__ = [
    "test_actual_host_refuses_bad_response_metadata_and_preserves_time_guards",
    "test_actual_transport_keeps_native_facts_across_scoped_read_and_separates_entropy",
    "test_v8_configuration_preserves_native_and_effect_limits_and_adds_only_a_grantless_policy",
    "test_real_requests_commit_accounting_before_route_errors_and_stop_at_the_limit",
    "test_request_quota_survives_restart_and_credential_rotation_without_cross_tenant_reset",
    "test_real_rate_admitted_tool_turn_followup_replay_and_restart_fit_the_unchanged_host",
    "test_all_route_scope_denials_count_requests_without_parsing_bodies_or_changing_domain_state",
    "test_rate_admitted_cancellation_after_delivery_preserves_uncertainty_across_restart",
    "test_full_layout_has_identical_real_compiler_tokens_and_literal_values",
    "test_real_token_oracle_detects_a_changed_literal",
    "test_actual_frozen_v7_rejects_request_admission_before_state_creation",
    "test_unchanged_v7_profile_retains_tool_turn_and_new_followup_across_both_host_directions",
]

pytestmark = pytest.mark.usefixtures("http_fixture_secret")


@pytest.fixture
def http_service_binary(request_host_binary):
    # Both imported v7 tests retain their original source, ABI, fixtures and
    # assertions. Only the executable is retargeted to test the additive host.
    return request_host_binary


def test_probe_recipe_retains_bootstrap_and_original_execution_bounds():
    source = compose_host_probe()
    assert source == compose_host_probe()
    assert len(source.text.encode()) == 64182 <= 65536
    assert source.compiler_input_sha256 == "d2f0d9423eff6a959a4e48797913c6b37964c9ec478d678d2bb2bdfe94462a7b"
    assert source.text.count("pub fn tool_main(") == 1
    assert 'read_record(api_response_core(input_ptr, input_len), "HC6\\n", 5)' in source.text
    assert '"profiles_validated"' in source.text
    assert '"AH5\\n"' not in source.text and '"HC5\\n"' not in source.text
    assert len(source.input_hashes) == 16
    for relative in ("tests/fixtures/request_host_probe.sigil", "tests/request_host_support.py"):
        assert source.input_hashes[relative] == hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()


def test_actual_v8_facts_are_native_bound_nonsecret_and_survive_the_scoped_read(
        request_host_binary, programs, scripted_llm, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    # The request body cannot replace CF2, clock, presentation, scope or IDs.
    forged = record("FP3\n", ["1", "0", "1", rows[0]["facts"], "forged-hint", "0" * 64, "f" * 64])
    cases = [(None, "0", ""), ("Bearer", "0", ""),
             ("bearer " + TOKEN_A, "0", ""), ("Bearer\t" + TOKEN_A, "0", ""),
             ("Basic " + TOKEN_A, "0", ""), ("Bearer unknown-credential", "1", ""),
             ("Bearer " + TOKEN_A, "1", rows[0]["facts"]),
             ("Bearer " + TOKEN_B, "1", rows[1]["facts"])]
    identities, labels = set(), set()
    with RequestHostApi(request_host_binary, root, programs, scripted_llm.url, workspace, rows=rows) as api:
        before = state(root)
        started = time.time_ns() // 1_000_000_000
        for authorization, expected_prefix, expected_facts in cases:
            extra = [("Cookie", "omitted-cookie-canary")]
            if authorization is not None:
                extra.append(("Authorization", authorization))
            status, headers, raw = api.exchange("POST", "/host-facts", raw=forged.encode(), token=None,
                hints=["native-label"], extra_headers=extra)
            assert status == 200 and dict(headers)["content-type"] == "text/plain; charset=utf-8"
            held, current, observation = fields(raw.decode(), "FP2\n", 3)
            facts = fields(current, "FP3\n", 7)
            assert started <= int(facts[0]) <= time.time_ns() // 1_000_000_000
            assert facts[1] in {"0", "1"} and facts[2] == expected_prefix
            assert facts[3] == expected_facts
            fresh, count, hint = fields(facts[4], "RF1\n", 3)
            assert re.fullmatch("[0-9a-f]{32}", fresh)
            assert count == "1" and bytes.fromhex(hint) == b"native-label"
            assert re.fullmatch("[0-9a-f]{64}", facts[5])
            assert re.fullmatch("[0-9a-f]{64}", facts[6])
            assert fresh not in {facts[5][:32], facts[5][32:]}
            assert fresh not in labels and facts[5] not in identities
            labels.add(fresh)
            identities.add(facts[5])
            if expected_facts:
                initial = fields(held, "FP3\n", 7)
                assert started <= int(initial[0]) <= int(facts[0])
                assert initial[1] in {"0", "1"}
                assert initial[2:] == facts[2:]
                outer = fields(observation, "RM1\n", 3)
                if expected_facts == rows[0]["facts"]:
                    assert outer[:2] == ["ok", "2"]
                    found = fields(outer[2], "RB1\n", 3)
                    assert fields(found[0], "RR1\n", 5) == ["a.operations", "one", "0", "0", ""]
                    assert fields(found[1], "RR1\n", 5) == ["a.state", "two", "0", "0", ""]
                    assert found[2] == ""
                else:
                    assert outer == ["error", "0", ""]
            else:
                assert held == observation == ""
            for forbidden in (TOKEN_A, TOKEN_B, API_KEY, "omitted-cookie-canary", "forged-hint", "unknown-credential"):
                assert forbidden.encode() not in raw
                assert forbidden.encode().hex().encode() not in raw
        assert state(root) == before
    assert scripted_llm.requests == []


def test_actual_v8_preserves_response_metadata_guards_and_refuses_old_commands(
        request_host_binary, programs, scripted_llm, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    with RequestHostApi(request_host_binary, root, programs, scripted_llm.url, workspace) as api:
        before = state(root)
        valid = response_command(marker="HC6\n", headers=[("x-request-id", "owned-label"), ("retry-after", "7")])
        status, headers, raw = api.exchange("POST", "/probe-command", raw=valid.encode())
        assert status == 200 and json.loads(raw) == {"fixture": "observed"}
        assert dict(headers)["x-request-id"] == "owned-label" and dict(headers)["retry-after"] == "7"
        invalid = [response_command(), valid + "trailing",
                   response_command(marker="HC6\n", guard=record("TG1\n", ["1", "2"])),
                   response_command(marker="HC6\n", headers=[("set-cookie", "a=b")]),
                   response_command(marker="HC6\n", mime="text/html"),
                   record("HC6\n", ["read_many", "[]", "", "held", record("TG1\n", ["100", "9000000000"])])]
        for command in invalid:
            status, headers, raw = api.exchange("POST", "/probe-command", raw=command.encode())
            assert status == 503 and json.loads(raw) == {"error": {"code": "host_refused"}}
            assert "x-request-id" not in dict(headers)
            assert dict(headers)["cache-control"] == "no-store"
        assert state(root) == before
    assert scripted_llm.requests == []


def test_real_v7_binary_refuses_v8_before_creating_application_state(
        legacy_request_service_binary, programs, scripted_llm, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    with pytest.raises(AssertionError, match="config"):
        RequestHostApi(legacy_request_service_binary, root, programs, scripted_llm.url, workspace)
    assert not (root / "records").exists()
    assert scripted_llm.requests == []
