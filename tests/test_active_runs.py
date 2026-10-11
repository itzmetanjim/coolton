import time

from agent.active_runs import is_run_active, mark_run_finished, mark_run_started


def test_marked_run_is_active():
    mark_run_started("AR2", "1.1", time.time())
    assert is_run_active("AR2", "1.1") is True


def test_finished_run_is_no_longer_active():
    started = time.time()
    mark_run_started("AR3", "1.1", started)
    mark_run_finished("AR3", "1.1", started)
    assert is_run_active("AR3", "1.1") is False


def test_active_run_is_per_thread():
    mark_run_started("AR4", "1.1", time.time())
    assert is_run_active("AR4", "2.2") is False


def test_an_hour_long_run_is_still_active():
    mark_run_started("AR5", "1.1", time.time() - 3600)
    assert is_run_active("AR5", "1.1") is True


def test_stale_run_is_treated_as_inactive():
    mark_run_started("AR7", "1.1", time.time() - 7 * 3600)
    assert is_run_active("AR7", "1.1") is False


def test_mark_run_finished_on_untracked_thread_is_a_noop():
    mark_run_finished("AR6", "never-started", time.time())
    assert is_run_active("AR6", "never-started") is False


def test_an_old_run_finishing_leaves_a_newer_runs_mark_alone():
    mark_run_started("AR8", "1.1", 100.0)
    newer = time.time()
    mark_run_started("AR8", "1.1", newer)
    mark_run_finished("AR8", "1.1", 100.0)
    assert is_run_active("AR8", "1.1") is True
    mark_run_finished("AR8", "1.1", newer)
    assert is_run_active("AR8", "1.1") is False
