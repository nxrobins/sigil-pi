"""Compiled SIGIL service-info routes; receipt inputs here are explicit fixtures.

These checks are not actual HTTP/durable-state, logging or release qualification.
The existing Python product is retained as the independent response oracle.
"""

import hashlib
import json

import pytest

from api_support import binding, credential
from conftest import PI_ROOT, SIGIL_ROOT, forge_ok, mcp as mcp
from emergency_support import cause
from http_compat_support import legacy_request_id
from readiness_admission_support import durable_commit, durable_read, envelope, source as baseline
from request_policy_support import compose_request_policy
from scripts.compose_service_info_entry import compose_service_info_entry
from test_api_migration import TOKEN, oracle
from test_request_entry import reply
from turn_support import FUEL, fields, record, refused


PATHS = ["/v1/health", "/v1/version"]
VERSION = "fixture-version"
LABEL = "service-info-request"


def source(limit=2, version=VERSION):
    return compose_service_info_entry(PI_ROOT, SIGIL_ROOT, request_limit=limit, version=version)


@pytest.fixture(scope="module")
def entry():
    return source().text


def step(mcp, entry, **kwargs):
    args = dict(method="GET", body="", hints=(LABEL.encode(),))
    args.update(kwargs)
    return fields(forge_ok(mcp, entry, envelope(**args), fuel=FUEL), "HC7\n", 5)


def admit(mcp, entry, path, *, row=None, body="", observed=None, lifecycle=None, hints=(LABEL.encode(),)):
    args = dict(path=path, row=credential() if row is None else row, body=body, lifecycle=lifecycle, hints=hints)
    first = step(mcp, entry, **args)
    assert first[:4] == ["read_observed", "a.budget", "request-window", "request-rate"]
    second = step(mcp, entry, **args, stage=first[0], continuation=first[3],
                  observation=durable_read() if observed is None else observed)
    assert second[:2] == ["call", "request_policy"]
    proposed = forge_ok(mcp, compose_request_policy().text, second[2], fuel=FUEL)
    third = step(mcp, entry, **args, stage="call", continuation=second[3], observation=proposed)
    if third[0] == "reply":
        return third
    assert third[0] == "commit_observed"
    assert json.loads(third[1])["writes"][0]["key"] == "request-window"
    return step(mcp, entry, **args, stage=third[0], continuation=third[3], observation=durable_commit())


def legacy(path, *, scopes=None, token=TOKEN, body=b"", quota=None, draining=False):
    service, agent, logs = oracle(["ops:read"] if scopes is None else scopes)
    service.quota_store = quota
    service._draining = draining
    headers = {"X-Request-ID": LABEL}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    status, returned, payload = service.dispatch("GET", path, headers, body)
    assert agent.calls == []
    assert len(logs) == 1
    return status, {k.lower(): v for k, v in returned.items() if k != "Cache-Control"}, payload


def test_recipe_is_explicit_bounded_deterministic_and_input_bound():
    built = source()
    assert source() == built
    assert len(built.input_hashes) == 31
    assert built.text.count("pub fn tool_main(") == 1
    assert len(source(9223372036854775807, "v" * 64).text.encode()) <= 65536
    assert built.compiler_input_sha256 != source(version="0.0.0-unset").compiler_input_sha256
    assert built.compiler_input_sha256 != source(3).compiler_input_sha256
    configuration = {"configuration/request-limit": b"2", "configuration/service-version": VERSION.encode()}
    for name, digest in built.input_hashes.items():
        raw = configuration[name] if name in configuration else (PI_ROOT / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest
    assert "// SERVICE_VERSION_LITERAL" not in built.text
    assert "readiness_project(" not in built.text  # This still is not /v1/ready.


@pytest.mark.parametrize("version", [None, True, 3, "", "v" * 65, "x\n", "x\"", "é", " x", "x/y"])
def test_recipe_refuses_missing_unbounded_or_unsafe_build_labels(version):
    with pytest.raises(ValueError):
        source(version=version)


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("body", ["", "{", '{"version":"forged","tenant":"other"}'])
def test_positive_routes_match_legacy_only_after_confirmed_accounting(mcp, entry, path, body):
    actual = reply(admit(mcp, entry, path, body=body))
    assert actual == legacy(path, body=body.encode())
    assert actual[0] == 200
    assert "ready" not in actual[2] and "dependencies" not in actual[2]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("presentation,row,token", [
    ("0", {"facts": ""}, None),
    ("1", {"facts": ""}, "unknown"),
    ("1", credential(before=151), "unknown"),
    ("1", credential(expires=150), "unknown"),
])
def test_authentication_errors_match_legacy_without_accounting(mcp, entry, path, presentation, row, token):
    command = step(mcp, entry, path=path, presentation=presentation, row=row)
    assert command[0] == "reply" and command[4] == ""
    assert reply(command) == legacy(path, token=token)
    assert reply(command)[1]["www-authenticate"] == 'Bearer realm="sigil-pi"'


@pytest.mark.parametrize("path", PATHS)
def test_permission_denial_matches_legacy_after_accounting(mcp, entry, path):
    command = admit(mcp, entry, path, row=credential(scopes=["chat"]))
    assert reply(command) == legacy(path, scopes=["chat"])
    assert fields(command[4], "TG1\n", 2) == ["100", "9000000000"]


@pytest.mark.parametrize("path", PATHS)
def test_rate_limit_does_not_reply_healthy_or_bypass_permissions(mcp, entry, path):
    class Quota:
        def admit_request(self, tenant):
            return False, 30
    observed = durable_read("3", "1", record("RW1\n", ["tenant-a", "120", "2"]))
    command = admit(mcp, entry, path, row=credential(scopes=[]), observed=observed)
    assert reply(command) == legacy(path, scopes=[], quota=Quota())


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("error", ["storage", "corrupt", "commit_uncertain"])
def test_actual_error_codec_never_unlocks_emergency_or_success(mcp, entry, path, error):
    class Quota:
        def admit_request(self, tenant):
            raise OSError("private diagnostic")
    command = step(mcp, entry, path=path, stage="read_observed", continuation="request-rate", observation=cause(error))
    assert command[0] == "reply"
    assert reply(command) == legacy(path, quota=Quota())
    assert "private" not in json.dumps(reply(command))


@pytest.mark.parametrize("path", PATHS)
def test_draining_does_not_turn_liveness_into_readiness(mcp, entry, path):
    command = admit(mcp, entry, path, lifecycle=record("LF1\n", ["ok", "", "1"]))
    assert reply(command) == legacy(path, draining=True)


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("receipt", [durable_commit("0"), durable_commit("01"),
    durable_commit(error="storage"), record("SC1\n", ["ok", "1"])])
def test_forged_or_malformed_receipt_cannot_produce_success(mcp, entry, path, receipt):
    held = record("RP4\n", ["raw", "", "", ""])
    refused(mcp, entry, envelope(method="GET", path=path, body="", hints=(LABEL.encode(),),
        stage="commit_observed", continuation=held, observation=receipt), 400)


@pytest.mark.parametrize("kwargs", [{"path": "/v1/ready"}, {"path": "/v1/health", "method": "POST"},
    {"path": "/v1/version/"}, {"path": "/v1/metrics"}, {"path": "/v1/sessions"}])
def test_other_routes_keep_original_first_transition(mcp, entry, kwargs):
    assert step(mcp, entry, **kwargs) == step(mcp, baseline().text, **kwargs)


def test_original_bootstrap_and_registered_functions_are_retained(mcp, entry):
    body = json.dumps([binding(credential())])
    first = step(mcp, entry, path="", method="", body=body, stage="boot")
    assert first[:2] == ["call", "admission"]
    assert fields(first[2], "AV2\n", 1) == [body]
    second = step(mcp, entry, path="", method="", body=body, stage="call", purpose="boot",
                  observation="registry_validated", continuation=first[3])
    assert reply(second) == (204, {}, None)


def test_largest_build_inputs_really_compile_and_return_the_bound_version(mcp):
    largest = source(9223372036854775807, "v" * 64).text
    code, headers, body = reply(admit(mcp, largest, "/v1/version"))
    assert code == 200 and body == {"api_version": "v1", "version": "v" * 64, "request_id": LABEL}
    assert headers == {"x-request-id": LABEL}


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("hints", [(), (b"short",), (b"valid-label",),
    (b"first-good", b"second-good"), (b"short", b"second-good"),
    (b"a" * 64,), ("é😀label".encode(),), (b"a" * 65,)])
def test_body_and_header_share_the_original_correlation_policy(mcp, entry, path, hints):
    code, headers, body = reply(admit(mcp, entry, path, hints=hints))
    expected = legacy_request_id(hints)
    assert code == 200 and headers == {"x-request-id": expected}
    assert body["request_id"] == expected
    assert set(body) == ({"status", "request_id"} if path == "/v1/health" else {"api_version", "version", "request_id"})
