"""Provider grant limits and preflight failure close the generation path."""
import time

import pytest

from agent import PiAgent, SessionStore, redact_grants


def agent(tmp_path, **kwargs):
    return PiAgent('http://localhost:9999/v1/messages', 'private-key',
        store=SessionStore(tmp_path/'sessions'), sandbox_root=tmp_path/'sandbox', mcp=None, **kwargs)


@pytest.mark.parametrize('invalid', [0, -1, 60001, True, 1.5])
def test_http_budget_validation(tmp_path, invalid):
    with pytest.raises(ValueError, match='http_timeout_ms'):
        agent(tmp_path, http_timeout_ms=invalid)


def test_http_grant_spends_remaining_turn_time(tmp_path):
    a = agent(tmp_path, http_timeout_ms=60000)
    a._turn_context.deadline = time.monotonic() + 1
    assert 0 < a._provider_grants()['http_timeout_ms'] <= 1000
    a._turn_context.deadline = time.monotonic() - 1
    with pytest.raises(TimeoutError, match='before provider'):
        a._provider_grants()


def test_scalar_http_grant_redaction_never_exports_arbitrary_text():
    assert redact_grants({'http_timeout_ms': 60000}) == {'http_timeout_ms': 60000}
    assert redact_grants({'http_timeout_ms': 'secret-text'}) == {'http_timeout_ms': 'invalid'}


def test_failed_token_count_never_reaches_generation(tmp_path):
    a = agent(tmp_path, http_timeout_ms=1000)
    a._llm_src = 'count guest'
    a._forge = lambda *args, **kwargs: (None, 'HTTP failed')
    with pytest.raises(RuntimeError, match='count unavailable'):
        a._count_input_tokens({'model': 'test', 'max_tokens': 100, 'messages': []}, 'session')
