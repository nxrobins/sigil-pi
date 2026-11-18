"""The scheduler's health verdict agrees with the ages it reports.

``ProductScheduler.snapshot()`` reports ``last_progress_age_seconds`` to the
millisecond, and judged a turn in flight past ``stall_after_seconds`` from its
age to the nanosecond. A turn 1.0103 s old under a bound of 1.01 s made the
scheduler unhealthy while the progress age it reported, 1.0104 s, read 1.01:
no further than the bound. That is how
tests/test_background_loops.py::test_a_scheduler_stuck_past_its_stall_bound_is_not_healthy_until_it_moves
could fail with "assert 1.01 > 1.01" (found in node S2's sweep).

What these tests hold: every age the verdict compares with the bound is the
age as reported, so while every turn in flight began after the loop last made
progress, a live loop is healthy exactly when its reported progress age is
within the bound. A loop that never started is not healthy and reports no age.
"""

from types import SimpleNamespace
from unittest import mock

from hypothesis import example, given, settings
from hypothesis import strategies as st

import product_service
from product_service import ProductScheduler, ProductScheduleStore

NOW = 5000.0
LEASE_S = 1.0
INTERVAL_S = 0.01
BOUND = LEASE_S + INTERVAL_S


def _loop(tmp_path):
    return ProductScheduler(ProductScheduleStore(tmp_path / "schedules.json"), None, None,
                            interval_s=INTERVAL_S, lease_s=LEASE_S)


def _report(loop, *, progress_age, turn_ages, alive=True):
    """The loop's snapshot at NOW, with a live thread, progress made
    `progress_age` seconds earlier and a turn in flight for each age."""
    loop._thread = SimpleNamespace(is_alive=lambda: alive)
    loop._progress_at = NOW - progress_age
    loop._pool.clear()
    for index, age in enumerate(turn_ages):
        turn = product_service._ScheduledTurn(f"key-{index}", "tenant")
        turn.started = NOW - age
        loop._pool.add(turn)
    with mock.patch.object(product_service.time, "monotonic", return_value=NOW):
        return loop.snapshot()


def test_a_turn_just_past_the_bound_does_not_make_an_age_within_it_unhealthy(tmp_path):
    report = _report(_loop(tmp_path), progress_age=1.0104, turn_ages=[1.0103])
    assert report["stall_after_seconds"] == BOUND
    assert report["last_progress_age_seconds"] == 1.01
    assert report["healthy"] is True, (
        f"the scheduler reported itself unhealthy with a progress age of "
        f"{report['last_progress_age_seconds']}, within its bound of {BOUND}: it judged "
        f"the turn in flight by an age it does not report")


def test_a_turn_past_the_bound_as_reported_is_unhealthy(tmp_path):
    loop = _loop(tmp_path)
    report = _report(loop, progress_age=1.0116, turn_ages=[1.0112])
    assert report["healthy"] is False
    assert report["last_progress_age_seconds"] == 1.012 > report["stall_after_seconds"]
    # Another tenant's recorded run keeps the age fresh: the stuck turn alone
    # makes the loop unhealthy, as before.
    report = _report(loop, progress_age=0.2, turn_ages=[1.0112, 0.1])
    assert report["healthy"] is False and report["last_progress_age_seconds"] == 0.2
    assert _report(loop, progress_age=0.2, turn_ages=[0.1])["healthy"] is True


def test_a_loop_that_never_started_or_whose_thread_ended_is_not_healthy(tmp_path):
    loop = _loop(tmp_path)
    with mock.patch.object(product_service.time, "monotonic", return_value=NOW):
        report = loop.snapshot()
    assert report["healthy"] is False and report["thread_alive"] is False
    assert report["last_progress_age_seconds"] is None
    assert report["last_tick_age_seconds"] is None
    assert _report(loop, progress_age=0.0, turn_ages=[], alive=False)["healthy"] is False


def test_the_verdict_agrees_with_the_reported_age_whenever_turns_began_after_progress(
        tmp_path):
    """The class: with every turn in flight begun at or after the last
    progress, a live loop is healthy exactly when the progress age it reports
    is within the bound it reports, however near the bound the ages fall."""
    loop = _loop(tmp_path)
    microseconds = st.integers(min_value=1_005_000, max_value=1_016_000)

    @settings(max_examples=300, deadline=None)
    @given(progress=microseconds, started_after=st.lists(
        st.integers(min_value=0, max_value=1_500), max_size=3))
    @example(progress=1_010_400, started_after=[100])
    def run(progress, started_after):
        progress_age = progress / 1e6
        turn_ages = [max(0, progress - offset) / 1e6 for offset in started_after]
        report = _report(loop, progress_age=progress_age, turn_ages=turn_ages)
        reported = report["last_progress_age_seconds"]
        assert report["healthy"] is (reported <= report["stall_after_seconds"]), (
            f"healthy={report['healthy']} with a reported progress age of {reported} "
            f"and a bound of {report['stall_after_seconds']} (turns {turn_ages})")

    run()
