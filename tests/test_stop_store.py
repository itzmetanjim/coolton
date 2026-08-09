import time

from agent.stop_store import (
    HaltRun,
    request_stop,
    stop_requested_for,
)


def test_halt_run_is_exception():
    assert issubclass(HaltRun, Exception)


def test_no_stop_never_halted():
    assert stop_requested_for("C1", "1.1", run_started_at=0.0) is False


def test_stop_halted_for_runs_that_started_before_request():
    run_started_at = time.time() - 1
    request_stop("C1", "1.1")
    assert stop_requested_for("C1", "1.1", run_started_at) is True


def test_stop_does_not_affect_runs_that_started_after_request():
    request_stop("C1", "1.1")
    run_started_at = time.time() + 1000
    assert stop_requested_for("C1", "1.1", run_started_at) is False


def test_stop_is_per_thread():
    request_stop("C1", "1.1")
    assert stop_requested_for("C2", "2.2", run_started_at=0.0) is False


def test_later_stop_overrides():
    request_stop("C1", "1.1")
    time.sleep(0.01)
    request_stop("C1", "1.1")
    assert stop_requested_for("C1", "1.1", run_started_at=0.0) is True
