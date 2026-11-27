"""Actual host conformance with an explicit test artifact, not product policy."""
import json
import re

import pytest

from api_support import TOKEN_A, TOKEN_B, credential
from conftest import API_KEY
from http_service_support import (HttpApi, host_probe_source,
                                  http_fixture_secret as http_fixture_secret,
                                  http_service_binary as http_service_binary, state)
from test_turn_execution import programs as programs
from turn_support import fields, record


pytestmark = pytest.mark.usefixtures("http_fixture_secret")


def response_command(*, mime="application/json", headers=(), body='{"fixture":"observed"}',
                     guard=None, marker="HC5\n", status="200"):
    headers = record("HH1\n", [str(len(headers)), *[part for pair in headers for part in pair]])
    payload = record("HR1\n", [mime, headers, body])
    guard = record("TG1\n", ["100", "9000000000"]) if guard is None else guard
    return record(marker, ["reply", status, payload, "", guard])


def test_actual_transport_keeps_native_facts_across_scoped_read_and_separates_entropy(
        http_service_binary, programs, scripted_llm, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    rows = [credential(), credential(TOKEN_B, principal="bob", tenant="tenant-b", prefix="b")]
    forged = record("RF1\n", ["0" * 32, "1", b"forged-body-hint".hex()])
    labels, identities = [], []
    with HttpApi(http_service_binary, root, programs, scripted_llm.url, workspace,
                 rows=rows, source=host_probe_source()) as api:
        before = state(root)
        bundle = None
        for hints, token, observation in [([], TOKEN_A, ["ok", "0", ""]),
                ([b"\xff", b"later-valid-id"], TOKEN_A, ["ok", "0", ""]),
                ([b"same-visible-label"], TOKEN_A, ["ok", "0", ""]),
                ([b"same-visible-label"], TOKEN_B, ["error", "0", ""])]:
            # The test artifact asks only for A's absent record. B's actual scope
            # must deny that read even though it uses the same HTTP trace label.
            status, pairs, raw = api.exchange("POST", "/host-facts", raw=forged.encode(), token=token,
                hints=hints, extra_headers=[("Cookie", "unrelated-cookie-canary"),
                                           ("X-Unrelated", "unrelated-header-canary")])
            assert status == 200
            assert dict(pairs)["content-type"] == "text/plain; charset=utf-8"
            current, continued, operation, bound_bundle, seen = fields(raw.decode(), "FP1\n", 5)
            assert current == continued and current != forged
            fresh, count, first = fields(current, "RF1\n", 3)
            assert re.fullmatch("[0-9a-f]{32}", fresh)
            assert count == str(len(hints)) and first == (hints[0].hex() if hints else "")
            assert re.fullmatch("[0-9a-f]{64}", operation)
            assert fresh not in {operation[:32], operation[32:]}
            assert re.fullmatch("[0-9a-f]{64}", bound_bundle)
            bundle = bound_bundle if bundle is None else bundle
            assert bundle == bound_bundle
            assert fields(seen, "SR1\n", 3) == observation
            for omitted in [TOKEN_A, TOKEN_B, API_KEY, "unrelated-cookie-canary", "unrelated-header-canary", "forged-body-hint"]:
                assert omitted.encode() not in raw and omitted.encode().hex().encode() not in raw
            labels.append(fresh)
            identities.append(operation)
        assert len(set(labels)) == len(labels) and len(set(identities)) == len(identities)
        assert state(root) == before
    assert scripted_llm.requests == []


def test_actual_host_refuses_bad_response_metadata_and_preserves_time_guards(
        http_service_binary, programs, scripted_llm, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "service"
    valid = response_command(headers=[("x-request-id", "owned-probe-id"),
        ("retry-after", "7"), ("www-authenticate", 'Bearer realm="sigil"'),
        ("server-timing", "tool;dur=1"), ("x-sigil-retries", "0"), ("x-sigil-tool-calls", "1")])
    malformed = [response_command(mime="text/html"),
        response_command(headers=[("x-request-id", "bad\r\nheader: value")]),
        response_command(headers=[("x-request-id", "one"), ("x-request-id", "two")]),
        response_command(headers=[("x-not-admitted", "value")]),
        response_command(headers=[("x-frame-options", "ALLOWALL")]),
        response_command(headers=[("set-cookie", "a=b")]),
        response_command(headers=[("location", "https://example.invalid/")]),
        response_command(headers=[("content-length", "0")]),
        response_command(headers=[("x-request-id", "x" * 1025)]),
        response_command(guard=record("TG1\n", ["1", "2"])),
        response_command(guard=record("TG1\n", ["9000000000", "9000000001"])),
        response_command(guard=record("TG1\n", ["0100", "9000000000"])),
        response_command(marker="HC4\n"), valid + "trailing",
        record("HC5\n", ["reply", "200", "unframed-json", "", ""])]
    with HttpApi(http_service_binary, root, programs, scripted_llm.url, workspace,
                 source=host_probe_source()) as api:
        before = state(root)
        status, pairs, raw = api.exchange("POST", "/probe-command", raw=valid.encode())
        headers = dict(pairs)
        assert status == 200 and json.loads(raw) == {"fixture": "observed"}
        assert headers["x-request-id"] == "owned-probe-id" and headers["retry-after"] == "7"
        assert headers["www-authenticate"] == 'Bearer realm="sigil"'
        assert headers["server-timing"] == "tool;dur=1"
        assert headers["x-sigil-retries"] == "0" and headers["x-sigil-tool-calls"] == "1"
        for mime in ["text/plain; charset=utf-8", "text/plain; version=0.0.4; charset=utf-8"]:
            command = response_command(mime=mime, body="fixture_metric 1\n")
            status, pairs, raw = api.exchange("POST", "/probe-command", raw=command.encode())
            assert status == 200 and raw == b"fixture_metric 1\n"
            assert dict(pairs)["content-type"] == mime
        for command in malformed:
            status, pairs, raw = api.exchange("POST", "/probe-command", raw=command.encode())
            assert status == 503, command[:120]
            assert json.loads(raw) == {"error": {"code": "host_refused"}}
            assert "x-request-id" not in dict(pairs)
            assert "fixture" not in raw.decode()
            assert dict(pairs)["cache-control"] == "no-store"
            assert dict(pairs)["x-content-type-options"] == "nosniff"
        assert state(root) == before
    assert scripted_llm.requests == []
