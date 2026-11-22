"""Durable, incremental model-call budgets survive crashes and concurrent turns."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from product_service import DurableQuotaStore, ProductError


def store(tmp_path, **kw):
    return DurableQuotaStore(tmp_path / 'quota.sqlite3', tokens_per_day=1000,
        token_reservation_per_turn=100, storage_bytes_per_tenant=10000,
        storage_reservation_per_turn=100, audit_bytes_per_tenant=10000,
        audit_reservation_per_turn=100, **kw)


def test_reservations_grow_only_when_needed_and_reconcile(tmp_path):
    q = store(tmp_path)
    lease = q.acquire_turn('a', incremental_tokens=True)
    budget = lease.token_budget(700)
    assert q.tenant_usage('a')['reserved_tokens'] == 0
    first = budget.reserve(250)
    assert q.tenant_usage('a')['tokens_today'] == 250
    first.settle(100)
    second = budget.reserve(500)
    second.settle(400)
    lease.release(usage={'input_tokens': 500})
    assert q.tenant_usage('a')['tokens_today'] == 500


def test_unknown_attempt_stays_charged_after_expiry_and_reopen(tmp_path):
    now = [100]
    q = store(tmp_path, clock=lambda: now[0], turn_lease_s=5)
    lease = q.acquire_turn('a', incremental_tokens=True)
    lease.token_budget(700).reserve(600)
    now[0] += 6
    reopened = store(tmp_path, clock=lambda: now[0])
    other = reopened.acquire_turn('a', incremental_tokens=True).token_budget(700)
    with pytest.raises(ProductError, match='daily'):
        other.reserve(401)
    assert reopened.tenant_usage('a')['tokens_today'] == 600


def test_concurrent_turns_cannot_spend_the_same_daily_capacity(tmp_path):
    q = store(tmp_path)
    budgets = [q.acquire_turn('a', incremental_tokens=True).token_budget(800) for _ in range(2)]
    def spend(budget):
        try:
            budget.reserve(600)
            return True
        except ProductError:
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(spend, budgets)) == [False, True]
    assert q.tenant_usage('a')['tokens_today'] == 600


def test_turn_ceiling_is_independent_of_daily_capacity(tmp_path):
    q = store(tmp_path)
    budget = q.acquire_turn('a', incremental_tokens=True).token_budget(500)
    budget.reserve(400).settle(350)
    with pytest.raises(ProductError) as caught:
        budget.reserve(151)
    assert caught.value.code == 'turn_token_budget_exhausted'
    budget.reserve(150).settle(150)
    assert q.tenant_usage('a')['tokens_today'] == 500


def test_lease_has_one_ceiling_and_invalid_inputs_do_not_charge(tmp_path):
    q = store(tmp_path)
    lease = q.acquire_turn('a', incremental_tokens=True)
    for invalid in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            lease.token_budget(invalid)
    budget = lease.token_budget(700)
    assert lease.token_budget(700) is budget
    with pytest.raises(ValueError, match='cannot change'):
        lease.token_budget(701)
    for invalid in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            budget.reserve(invalid)
    attempt = budget.reserve(100)
    for invalid in (-1, True, 1.5):
        with pytest.raises(ValueError):
            attempt.settle(invalid)
    attempt.settle(40)
    attempt.settle(40)
    assert budget.spent == 40
    lease.release()
    with pytest.raises(ValueError):
        lease.token_budget(700)
    with pytest.raises(ProductError, match='expired'):
        budget.reserve(1)
    fixed = q.acquire_turn('b')
    with pytest.raises(ValueError):
        fixed.token_budget(1)
    fixed.release()


def test_expired_lease_cannot_dispatch_or_refund(tmp_path):
    now = [10]
    q = store(tmp_path, clock=lambda: now[0], turn_lease_s=5)
    lease = q.acquire_turn('a', incremental_tokens=True)
    budget = lease.token_budget(700)
    attempt = budget.reserve(500)
    now[0] = 16
    with pytest.raises(ProductError, match='expired'):
        budget.reserve(1)
    with pytest.raises(ProductError, match='expired'):
        attempt.settle(40)
    assert q.tenant_usage('a')['tokens_today'] == 500


def test_prior_day_receipt_never_credits_today(tmp_path):
    now = [86399]
    q = store(tmp_path, clock=lambda: now[0])
    budget = q.acquire_turn('a', incremental_tokens=True).token_budget(800)
    old = budget.reserve(300)
    now[0] += 2
    budget.reserve(300)
    old.settle(40)
    assert budget.spent == 340
    assert q.tenant_usage('a')['tokens_today'] == 300


def test_provider_overrun_is_recorded_and_stops_the_turn(tmp_path):
    q = store(tmp_path)
    budget = q.acquire_turn('a', incremental_tokens=True).token_budget(500)
    with pytest.raises(ProductError) as caught:
        budget.reserve(400).settle(1200)
    assert caught.value.code == 'provider_token_budget_exceeded'
    assert budget.spent == q.tenant_usage('a')['tokens_today'] == 1200
    with pytest.raises(ProductError):
        budget.reserve(1)


def test_new_debits_respect_older_fixed_reservations(tmp_path):
    q = store(tmp_path)
    fixed = q.acquire_turn('a')
    budget = q.acquire_turn('a', incremental_tokens=True).token_budget(1000)
    with pytest.raises(ProductError, match='daily'):
        budget.reserve(901)
    fixed.release()
    budget.reserve(901)
    assert q.tenant_usage('a')['tokens_today'] == 901


def test_incremental_budget_requires_enabled_resource_quotas(tmp_path):
    from product_service import ConfigError
    q = DurableQuotaStore(tmp_path/'disabled.sqlite3')
    with pytest.raises(ConfigError, match='resource quotas'):
        q.acquire_turn('a', incremental_tokens=True)


@pytest.mark.parametrize('scheduled', [False, True])
@pytest.mark.parametrize('limit,enabled', [(0, True), (True, True), (1.5, True),
    (100, False), (100, None)])
def test_service_configuration_requires_a_valid_budget_store(tmp_path, scheduled, limit, enabled):
    from types import SimpleNamespace
    from product_service import ConfigError, ProductService, ProductScheduler
    q = store(tmp_path) if enabled else (DurableQuotaStore(tmp_path/'disabled.sqlite3') if enabled is False else None)
    agent = SimpleNamespace(manifest={}, memory=None)
    auth = SimpleNamespace(principals=[])
    with pytest.raises(ConfigError):
        if scheduled:
            ProductScheduler(None, agent, auth, quota_store=q, data_manager=object(), max_tokens_per_turn=limit)
        else:
            ProductService(agent, auth, quota_store=q, data_manager=object(), max_tokens_per_turn=limit)
