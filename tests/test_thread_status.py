from unittest.mock import Mock

import pytest

from agent import thread_status as ts


class _FakeTimer:
    """Records what would have been scheduled instead of actually waiting 30s — start()/
    set_status()/stop() should behave correctly (reset, cancel-old-before-new) without
    real sleeps, and fire() lets a test simulate the periodic refresh firing."""

    def __init__(self, interval, function, args=()):
        self.interval = interval
        self.function = function
        self.args = args
        self.started = False
        self.canceled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.canceled = True

    def fire(self):
        self.function(*self.args)


@pytest.fixture(autouse=True)
def _fake_timer(monkeypatch):
    monkeypatch.setattr(ts.threading, "Timer", _FakeTimer)
    ts._state.clear()
    yield
    ts._state.clear()


def test_start_sends_the_initial_status_and_arms_a_refresh_timer():
    client = Mock()
    ts.start(client, "C1", "1.1")

    client.assistant_threads_setStatus.assert_called_once_with(
        channel_id="C1", thread_ts="1.1", status="Working"
    )
    timer = ts._state[("C1", "1.1")]["timer"]
    assert isinstance(timer, _FakeTimer)
    assert timer.interval == ts._REFRESH_SECONDS
    assert timer.started is True


def test_start_accepts_a_custom_initial_status():
    client = Mock()
    ts.start(client, "C1", "1.1", status="Getting started")

    client.assistant_threads_setStatus.assert_called_once_with(
        channel_id="C1", thread_ts="1.1", status="Getting started"
    )


def test_status_is_cropped_to_49_chars():
    client = Mock()
    long_status = "x" * 80
    ts.start(client, "C1", "1.1", status=long_status)

    sent = client.assistant_threads_setStatus.call_args.kwargs["status"]
    assert sent == "x" * 49


def test_set_status_sends_immediately_and_cancels_the_old_timer():
    client = Mock()
    ts.start(client, "C1", "1.1")
    first_timer = ts._state[("C1", "1.1")]["timer"]

    ts.set_status("C1", "1.1", "calling tool: Reacting to message")

    assert first_timer.canceled is True
    assert client.assistant_threads_setStatus.call_args.kwargs["status"] == "calling tool: Reacting to message"
    new_timer = ts._state[("C1", "1.1")]["timer"]
    assert new_timer is not first_timer
    assert new_timer.started is True


def test_set_status_crops_a_long_tool_name_to_49_chars():
    client = Mock()
    ts.start(client, "C1", "1.1")

    ts.set_status("C1", "1.1", "calling tool: " + "y" * 60)

    sent = client.assistant_threads_setStatus.call_args.kwargs["status"]
    assert len(sent) == 49


def test_set_status_is_a_noop_when_start_was_never_called():
    # No exception, no call — nothing to update for a thread with no live status armed.
    ts.set_status("NEVER-STARTED", "1.1", "calling tool: X")


def test_refresh_timer_resends_the_current_status_and_rearms():
    client = Mock()
    ts.start(client, "C1", "1.1")
    ts.set_status("C1", "1.1", "calling tool: Searching the web")
    client.assistant_threads_setStatus.reset_mock()

    timer = ts._state[("C1", "1.1")]["timer"]
    timer.fire()

    client.assistant_threads_setStatus.assert_called_once_with(
        channel_id="C1", thread_ts="1.1", status="calling tool: Searching the web"
    )
    new_timer = ts._state[("C1", "1.1")]["timer"]
    assert new_timer is not timer
    assert new_timer.started is True


def test_stop_cancels_the_timer_and_clears_state():
    client = Mock()
    ts.start(client, "C1", "1.1")
    timer = ts._state[("C1", "1.1")]["timer"]

    ts.stop("C1", "1.1")

    assert timer.canceled is True
    assert ("C1", "1.1") not in ts._state


def test_stop_is_a_noop_when_nothing_was_started():
    ts.stop("NEVER-STARTED", "1.1")


def test_set_status_after_stop_is_a_noop():
    client = Mock()
    ts.start(client, "C1", "1.1")
    ts.stop("C1", "1.1")
    client.assistant_threads_setStatus.reset_mock()

    ts.set_status("C1", "1.1", "calling tool: X")

    client.assistant_threads_setStatus.assert_not_called()


def test_start_again_cancels_a_previous_unstopped_timer():
    """A new turn's start() must not leave the previous turn's timer running (belt and
    suspenders alongside listeners.events.turn's own stop() in its finally block)."""
    client = Mock()
    ts.start(client, "C1", "1.1")
    first_timer = ts._state[("C1", "1.1")]["timer"]

    ts.start(client, "C1", "1.1")

    assert first_timer.canceled is True


def test_send_failure_is_logged_and_swallowed_not_raised():
    client = Mock()
    client.assistant_threads_setStatus.side_effect = Exception("status api down")

    ts.start(client, "C1", "1.1")  # must not raise
    ts.set_status("C1", "1.1", "calling tool: X")  # must not raise either
