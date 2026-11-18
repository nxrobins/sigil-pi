"""Public HTTP -> real pooled SIGIL -> controlled provider -> durable usage."""
import hashlib
import json
import threading
import time

import pytest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent import AuditLog, PiAgent, SessionStore, verify_audit_dir
from conftest import MCP_BIN, needs_toolchain
from product_main import build_runtime
from product_service import (AuthRegistry, DurableQuotaStore, ProductDataManager,
    ProductScheduleStore, ProductService, ProductScheduler, SessionOperationLocks, serve_product,
    _internal_session)


@contextmanager
def running(tmp_path, *, delay=0, count=40, limit=10000, retries=0, http_ms=6000,
            response_usage=None, drop=False, respond=None):
    needs_toolchain()
    seen = {'active': 0, 'peak': 0, 'calls': 0, 'counts': 0, 'requests': [], 'count_requests': [], 'started': threading.Event()}
    lock = threading.Lock()
    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert body['model'] == 'test-model'
            assert self.headers['x-api-key'] == 'provider-test-key'
            if self.path.endswith('/count_tokens'):
                with lock:
                    seen['counts'] += 1
                    seen['count_requests'].append(body)
                answer = {'input_tokens': count(body) if callable(count) else count}
            else:
                with lock:
                    seen['calls'] += 1
                    sequence = seen['calls']
                    seen['requests'].append(body)
                    seen['active'] += 1
                    seen['peak'] = max(seen['peak'], seen['active'])
                seen['started'].set()
                time.sleep(delay(body) if callable(delay) else delay)
                with lock:
                    seen['active'] -= 1
                if drop:
                    self.close_connection = True
                    return
                answer = {'id': 'test', 'type': 'message', 'role': 'assistant',
                          'content': [{'type': 'text', 'text': 'completed'}],
                          'stop_reason': 'end_turn',
                          'usage': response_usage if response_usage is not None else {'input_tokens': 40, 'output_tokens': 5}}
                if respond is not None:
                    answer = respond(body, sequence)
            raw = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except OSError:
                pass

        def log_message(self, *args):
            pass

    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    audit_key = b'e2e-audit-signing-key-with-at-least-32-bytes'
    try:
        with build_runtime(MCP_BIN, 20, lambda _: None, pool_size=2) as runtime:
            agent = PiAgent(f'http://127.0.0.1:{provider.server_port}/v1/messages', 'provider-test-key',
                store=SessionStore(tmp_path/'sessions'), sandbox_root=tmp_path/'sandboxes',
                mcp=runtime, model='test-model', max_tokens=100, http_timeout_ms=http_ms,
                llm_retries=retries, audit=AuditLog(tmp_path/'audit', key=audit_key, strict=True))
            agent._sleep = lambda _: None
            auth = AuthRegistry([{'sha256': hashlib.sha256((tenant*32).encode()).hexdigest(),
                'principal': tenant, 'tenant': tenant, 'scopes': ['chat', 'schedules:write'], 'tools': ['list_dir']}
                for tenant in ('a', 'b')])
            quota = DurableQuotaStore(tmp_path/'quota.sqlite3', tokens_per_day=20000,
                token_reservation_per_turn=1000, storage_bytes_per_tenant=1000000,
                storage_reservation_per_turn=10000, audit_bytes_per_tenant=1000000,
                audit_reservation_per_turn=10000)
            schedules = ProductScheduleStore(tmp_path/'schedules.json')
            locks = SessionOperationLocks(tmp_path/'locks')
            data = ProductDataManager(agent, schedules, locks, quota_store=quota)
            service = ProductService(agent, auth, quota_store=quota, data_manager=data,
                session_locks=locks, max_tokens_per_turn=limit, turn_deadline_s=20,
                log_sink=lambda _: None)
            server = serve_product(service, port=0)
            seen.update(agent=agent, runtime=runtime, service=service, schedules=schedules,
                        auth=auth, locks=locks, data=data)
            def chat(tenant='a', message='hello'):
                request = Request(f'http://127.0.0.1:{server.server_port}/v1/chat',
                    json.dumps({'session': 'same-external-name', 'message': message}).encode(),
                    headers={'Authorization': 'Bearer '+tenant*32, 'Content-Type': 'application/json'})
                try:
                    response = urlopen(request, timeout=25)
                except HTTPError as error:
                    response = error
                with response:
                    return response.status, json.load(response)
            try:
                yield chat, quota, seen
                assert verify_audit_dir(tmp_path/'audit', key=audit_key)['ok']
            finally:
                server.shutdown()
                server.server_close()
    finally:
        provider.shutdown()
        provider.server_close()


def test_slow_model_calls_overlap_through_real_sigil_and_http(tmp_path):
    with running(tmp_path, delay=3) as (chat, quota, seen):
        with ThreadPoolExecutor(2) as threads:
            results = list(threads.map(chat, ['a', 'b']))
        assert [status for status, _ in results] == [200, 200], results
        assert seen['peak'] == 2
        assert seen['calls'] == seen['counts'] == 2
        assert quota.tenant_usage('a')['tokens_today'] == 45
        assert quota.tenant_usage('b')['tokens_today'] == 45


def test_budget_refuses_before_generation(tmp_path):
    with running(tmp_path, count=5000, limit=2000) as (chat, quota, seen):
        status, body = chat()
        assert (status, body['error']['code']) == (429, 'turn_token_budget_exhausted')
        assert seen['counts'] == 1 and seen['calls'] == 0
        assert quota.tenant_usage('a')['tokens_today'] == 0


def test_timeout_keeps_the_charge_and_next_tenant_can_run(tmp_path):
    with running(tmp_path, delay=1, http_ms=250) as (chat, quota, seen):
        assert chat()[0] == 502
        assert seen['calls'] == 1
        assert quota.tenant_usage('a')['tokens_today'] == 1164
        assert chat('b')[0] == 502  # its own call runs; no broken shared compiler
        assert seen['calls'] == 2


def test_retry_must_reserve_its_own_allowance(tmp_path):
    with running(tmp_path, retries=2, drop=True, limit=2000) as (chat, quota, seen):
        status, body = chat()
        assert (status, body['error']['code']) == (429, 'turn_token_budget_exhausted')
        assert seen['calls'] == 1
        assert quota.tenant_usage('a')['tokens_today'] == 1164


def test_cached_usage_is_not_refunded_as_unused_input(tmp_path):
    with running(tmp_path, response_usage={'input_tokens': 10, 'output_tokens': 5,
            'cache_read_input_tokens': 20, 'cache_creation_input_tokens': 30}) as (chat, quota, seen):
        assert chat()[0] == 200
        assert quota.tenant_usage('a')['tokens_today'] == 65


@pytest.mark.parametrize('usage', [{}, {'input_tokens': -1, 'output_tokens': 5},
    {'input_tokens': 40, 'output_tokens': 5, 'cache_read_input_tokens': -1}])
def test_missing_or_invalid_usage_keeps_full_charge(tmp_path, usage):
    with running(tmp_path, response_usage=usage) as (chat, quota, seen):
        assert chat()[0] == 502
        assert seen['calls'] == 1
        assert quota.tenant_usage('a')['tokens_today'] == 1164


@pytest.mark.parametrize('count', [None, -1, True, 'forty', 2.5])
def test_invalid_token_preflight_never_generates(tmp_path, count):
    with running(tmp_path, count=count) as (chat, quota, seen):
        assert chat()[0] == 502
        assert seen['counts'] == 1 and seen['calls'] == 0
        assert quota.tenant_usage('a')['tokens_today'] == 0


def test_overrun_stops_before_tools_and_reports_actual_usage(tmp_path):
    def answer(body, sequence):
        return {'content': [{'type': 'tool_use', 'id': 'never', 'name': 'list_dir',
                             'input': {'path': '.'}}], 'stop_reason': 'tool_use',
                'usage': {'input_tokens': 1500, 'output_tokens': 5}}
    with running(tmp_path, respond=answer) as (chat, quota, seen):
        status, body = chat()
        assert (status, body['error']['code']) == (502, 'provider_token_budget_exceeded')
        assert seen['calls'] == 1
        assert quota.tenant_usage('a')['tokens_today'] == 1505
        metrics = seen['service'].metrics.snapshot()
        assert (metrics['input_tokens_total'], metrics['output_tokens_total']) == (1500, 5)
        assert metrics['tool_calls_total'] == 0
        events = [json.loads(line) for path in (tmp_path/'audit').glob('*.jsonl')
                  for line in path.read_text().splitlines()]
        assert not any(event.get('kind') == 'tool' for event in events)


def test_exhaustion_preserves_tool_progress_and_followup_can_resume(tmp_path):
    def answer(body, sequence):
        if sequence == 1:
            return {'content': [{'type': 'tool_use', 'id': 'listed', 'name': 'list_dir',
                                 'input': {'path': '.'}}], 'stop_reason': 'tool_use',
                    'usage': {'input_tokens': 40, 'output_tokens': 5}}
        return {'content': [{'type': 'text', 'text': 'resumed'}], 'stop_reason': 'end_turn',
                'usage': {'input_tokens': 40, 'output_tokens': 5}}
    with running(tmp_path, limit=1200, respond=answer) as (chat, quota, seen):
        status, body = chat()
        assert (status, body['error']['code']) == (429, 'turn_token_budget_exhausted')
        assert seen['calls'] == 1
        history = seen['agent'].store.load(_internal_session('a', 'same-external-name'))
        assert any(block.get('tool_use_id') == 'listed'
                   for msg in history if isinstance(msg['content'], list) for block in msg['content'])
        assert chat(message='continue')[0] == 200
        assert seen['calls'] == 2 and quota.tenant_usage('a')['tokens_today'] == 90
        for requested in seen['requests']:
            assert {k: v for k, v in requested.items() if k != 'max_tokens'} in seen['count_requests']


def test_same_session_is_serialized_with_multiple_compilers(tmp_path):
    with running(tmp_path, delay=.2) as (chat, quota, seen):
        with ThreadPoolExecutor(2) as threads:
            assert [status for status, _ in threads.map(chat, ['a', 'a'])] == [200, 200]
        assert seen['peak'] == 1 and seen['calls'] == 2
        assert [len(body['messages']) for body in seen['requests']] == [1, 3]
        assert quota.tenant_usage('a')['tokens_today'] == 90


@pytest.mark.parametrize('limit,status,calls,charged', [(10000, 'ok', 1, 45),
    (1000, 'quota_exceeded', 0, 0)])
def test_scheduled_turn_uses_the_same_budget(tmp_path, limit, status, calls, charged):
    with running(tmp_path, limit=limit) as (_, quota, seen):
        principal = seen['auth'].authenticate('Bearer ' + 'a'*32)
        seen['schedules'].put(principal, name='daily', session='scheduled', message='hello', every_ms=60000)
        scheduler = ProductScheduler(seen['schedules'], seen['agent'], seen['auth'],
            session_locks=seen['locks'], quota_store=quota, data_manager=seen['data'],
            max_tokens_per_turn=limit, log_sink=lambda _: None)
        scheduler.tick()
        assert seen['schedules'].for_tenant('a')[0]['last_status'] == status
        assert seen['calls'] == calls
        assert quota.tenant_usage('a')['tokens_today'] == charged


def test_killed_compiler_does_not_stop_other_tenants_or_future_turns(tmp_path):
    with running(tmp_path, delay=lambda body: 2 if body['messages'][-1]['content'] == 'slow' else 0) as (chat, quota, seen):
        with ThreadPoolExecutor(2) as threads:
            first = threads.submit(chat, 'a', 'slow')
            assert seen['started'].wait(10)
            runtime = seen['runtime']
            with runtime._changed:
                [occupied] = [lane for lane in runtime._lanes if lane not in runtime._idle]
                occupied._client._proc.kill()
            assert chat('b')[0] == 200
            assert first.result(10)[0] == 502
        assert chat('a', 'continue')[0] == 200
        assert runtime.generation == 3 and runtime.is_healthy
        assert seen['calls'] == 3  # the interrupted attempt was never replayed
        assert quota.tenant_usage('a')['tokens_today'] == 1164 + 45


def test_killing_the_whole_compiler_pool_recovers_on_the_next_turn(tmp_path):
    with running(tmp_path) as (chat, quota, seen):
        runtime = seen['runtime']
        for lane in runtime._lanes:
            lane._client._proc.kill()
        assert chat()[0] == 502
        assert seen['calls'] == 0  # the count request failed before generation
        assert chat()[0] == 200
        assert runtime.generation == 4 and runtime.is_healthy
        assert seen['calls'] == 1 and quota.tenant_usage('a')['tokens_today'] == 45
