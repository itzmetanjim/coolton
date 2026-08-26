import time

from agent.active_runs import is_run_active, mark_run_finished, mark_run_started


def test_no_run_never_active():
    assert is_run_active("AR1", "1.1") is False


def test_marked_run_is_active():
    mark_run_started("AR2", "1.1", time.time())
    assert is_run_active("AR2", "1.1") is True


def test_finished_run_is_no_longer_active():
    mark_run_started("AR3", "1.1", time.time())
    mark_run_finished("AR3", "1.1")
    assert is_run_active("AR3", "1.1") is False


def test_active_run_is_per_thread():
    mark_run_started("AR4", "1.1", time.time())
    assert is_run_active("AR4", "2.2") is False


def test_stale_run_is_treated_as_inactive():
    mark_run_started("AR5", "1.1", time.time() - 3600)
    assert is_run_active("AR5", "1.1") is False


def test_mark_run_finished_on_untracked_thread_is_a_noop():
    mark_run_finished("AR6", "never-started")
    assert is_run_active("AR6", "never-started") is False
