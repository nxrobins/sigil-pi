"""Legacy contract evidence only; these tests do not execute SIGIL or new HTTP."""
import json

import pytest

from http_compat_support import FRESH, INVALID, VALID, legacy_request_id, record, request_facts
from test_api_migration import TOKEN, oracle


@pytest.mark.parametrize("hint", VALID)
def test_legacy_valid_hint_is_an_exact_echo(hint):
    assert legacy_request_id([hint]) == hint.decode("ascii")


@pytest.mark.parametrize("hint", INVALID)
def test_legacy_invalid_hint_selects_independent_native_identity(hint):
    assert legacy_request_id([hint]) == FRESH


@pytest.mark.parametrize("hints,expected", [
    ([], FRESH), ([b"", b"later-valid-id"], FRESH),
    ([b"first-valid-id", b"second-valid-id"], "first-valid-id"),
    ([b"first-invalid/", b"later-valid-id"], FRESH),
])
def test_legacy_missing_and_duplicate_hint_semantics(hints, expected):
    assert legacy_request_id(hints) == expected


def test_request_fact_oracle_preserves_first_raw_bytes_without_claiming_authority():
    assert request_facts([b"a\xff", b"second-value"]) == record("RF1\n", [FRESH, "2", "61ff"])
    assert request_facts([]) == record("RF1\n", [FRESH, "0", ""])
    assert request_facts([b""]) == record("RF1\n", [FRESH, "1", ""])


def test_legacy_response_id_matches_body_and_header_on_success_and_refusal():
    for scopes, status in [(["ops:read"], 200), ([], 403)]:
        service, agent, records = oracle(scopes)
        result, headers, payload = service.dispatch("GET", "/v1/health", {
            "Authorization": "Bearer " + TOKEN, "X-Request-ID": "trace-id-123"})
        assert result == status
        assert headers["X-Request-ID"] == payload["request_id"] == "trace-id-123"
        assert records[0]["request_id"] == "trace-id-123"
        assert not agent.calls


def test_legacy_request_id_is_not_an_idempotency_key():
    service, agent, records = oracle(["chat"])
    for _ in range(2):
        status, headers, body = service.dispatch("POST", "/v1/chat", {
            "Authorization": "Bearer " + TOKEN, "X-Request-ID": "same-trace-id"},
            json.dumps({"session": "same-session", "message": "fixture message"}).encode())
        assert status == 200
        assert headers["X-Request-ID"] == body["request_id"] == "same-trace-id"
    assert len(agent.calls) == len(records) == 2
