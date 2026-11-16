"""Each compiler has one owner; queued expiry cannot kill another turn."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import runtime_client as runtime


class Client:
    def initialize(self, **kw):
        pass

    def close(self):
        pass

    def forge(self, source, **kw):
        return source()


def test_two_forges_overlap_and_queued_deadline_leaves_them_alive():
    both = threading.Barrier(3)
    release = threading.Event()
    def slow():
        both.wait(2)
        release.wait(2)
        return 'ok'
    with runtime.RuntimePool(Client, size=2) as pool, ThreadPoolExecutor(2) as threads:
        pending = [threads.submit(pool.forge, slow) for _ in range(2)]
        both.wait(2)
        start = time.monotonic()
        with pytest.raises(runtime.RuntimeClientTimeout) as caught:
            pool.forge(lambda: 'never', timeout_s=.03)
        assert caught.value.budget_expired
        assert time.monotonic() - start < .5
        release.set()
        assert [f.result(2) for f in pending] == ['ok', 'ok']
        assert pool.forge(lambda: 'next') == 'next'


@pytest.mark.parametrize('size', [0, -1, 33, True, 1.5])
def test_invalid_pool_size(size):
    with pytest.raises(ValueError):
        runtime.RuntimePool(Client, size=size)


def test_failed_startup_closes_already_started_compilers():
    closed = []
    clients = iter([Client(), None])
    first = next(clients)
    first.close = lambda: closed.append(True)
    calls = [first]
    def spawn():
        if calls:
            return calls.pop()
        raise OSError('spawn failed')
    pool = runtime.RuntimePool(spawn, size=2)
    with pytest.raises(OSError, match='spawn failed'):
        pool.__enter__()
    assert closed == [True]
    assert not pool.is_healthy
    pool.close()
    with pytest.raises(runtime.RuntimeClientError, match='closed'):
        pool.forge(lambda: None)


def test_failed_lane_is_replaced_without_replay_and_other_lane_survives():
    with runtime.RuntimePool(Client, size=2) as pool:
        assert pool.is_healthy and pool.generation == 2
        def broken():
            raise runtime.RuntimeClientTimeout('own deadline', budget_expired=True)
        with pytest.raises(runtime.RuntimeClientTimeout):
            pool.forge(broken)
        assert pool.unhealthy_replacements == 0
        assert pool.forge(lambda: 'other') == 'other'
        assert pool.forge(lambda: 'replacement') == 'replacement'
        assert pool.generation == 3
        assert pool.is_healthy


def test_exhausted_lanes_fail_readiness_and_are_never_selected():
    with runtime.RuntimePool(Client, size=2) as pool:
        # Exercise the real supervisor's failure budget, not a pool flag.
        def broken():
            raise OSError('compiler lost')
        for _ in range(8):
            with pytest.raises(OSError):
                pool.forge(broken)
        assert pool.unhealthy_replacements == 8
        assert not pool.is_healthy
        with pytest.raises(runtime.RuntimeClientError, match='no healthy'):
            pool.forge(lambda: None)


def test_waiter_wakes_when_a_lane_returns():
    started, release = threading.Event(), threading.Event()
    def slow():
        started.set()
        assert release.wait(2)
        return 'first'
    with runtime.RuntimePool(Client, size=1) as pool, ThreadPoolExecutor(2) as threads:
        first = threads.submit(pool.forge, slow)
        assert started.wait(2)
        second = threads.submit(pool.forge, lambda: 'second')
        release.set()
        assert first.result(2) == 'first' and second.result(2) == 'second'


def test_a_host_fault_retires_all_dead_idle_peers_before_the_next_request():
    from types import SimpleNamespace
    clients = []
    class Killable(Client):
        def __init__(self):
            self.dead = False
            self._proc = SimpleNamespace(poll=lambda: 1 if self.dead else None,
                                         kill=self.kill)
            clients.append(self)
        def kill(self):
            self.dead = True
        def forge(self, source, **kwargs):
            if self.dead:
                raise runtime.RuntimeClientError('dead process')
            return source()
    with runtime.RuntimePool(Killable, size=4) as pool:
        for client in clients:
            client.kill()
        with pytest.raises(runtime.RuntimeClientError, match='dead process'):
            pool.forge(lambda: 'must not replay')
        assert pool.unhealthy_replacements == 4
        for _ in range(4):
            assert pool.forge(lambda: 'new request') == 'new request'
        assert pool.generation == 8 and pool.is_healthy
        # Observing idle exits must not wait for or retire an occupied lane.
        lane = pool._lanes[0]
        with lane._lock:
            lane.retire_if_exited()
        assert pool.generation == 8


def test_pool_warms_compilers_together_before_serving():
    initialized = threading.Barrier(4)
    class ColdClient(Client):
        def initialize(self, **kwargs):
            initialized.wait(2)
    with runtime.RuntimePool(ColdClient, size=4) as pool:
        assert pool.generation == 4 and pool.is_healthy
        assert pool.forge(lambda: 'ready') == 'ready'
