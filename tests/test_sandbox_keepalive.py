import pytest

from agent import sandbox_keepalive as ka


class _FakeTimer:
    """Records what would have been scheduled instead of actually waiting — arm()
    should behave correctly (reset, cancel-old-before-new) without real sleeps."""

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
        """Simulate the timer actually elapsing."""
        self.function(*self.args)


@pytest.fixture(autouse=True)
def _fake_timer(monkeypatch):
    monkeypatch.setattr(ka.threading, "Timer", _FakeTimer)
    ka._timers.clear()
    yield
    ka._timers.clear()


def test_arm_starts_a_timer_with_the_given_seconds():
    ka.arm("C1", "1.1", 120)
    timer = ka._timers[("C1", "1.1")]
    assert isinstance(timer, _FakeTimer)
    assert timer.interval == 120
    assert timer.started is True


def test_arm_cancels_the_previous_timer_before_starting_a_new_one():
    ka.arm("C1", "1.1", 120)
    first = ka._timers[("C1", "1.1")]
    ka.arm("C1", "1.1", 120)  # e.g. another action resetting the countdown
    second = ka._timers[("C1", "1.1")]
    assert first.canceled is True
    assert second is not first
    assert second.started is True


def test_arm_with_zero_seconds_only_cancels_without_starting_a_new_timer():
    ka.arm("C1", "1.1", 120)
    first = ka._timers[("C1", "1.1")]
    ka.arm("C1", "1.1", 0)
    assert first.canceled is True
    assert ("C1", "1.1") not in ka._timers


def test_cancel_stops_a_pending_timer():
    ka.arm("C1", "1.1", 120)
    timer = ka._timers[("C1", "1.1")]
    ka.cancel("C1", "1.1")
    assert timer.canceled is True
    assert ("C1", "1.1") not in ka._timers


def test_cancel_is_a_noop_when_nothing_is_pending():
    ka.cancel("C1", "1.1")  # must not raise


def test_separate_threads_get_independent_timers():
    ka.arm("C1", "1.1", 120)
    ka.arm("C1", "2.2", 60)
    assert ka._timers[("C1", "1.1")].interval == 120
    assert ka._timers[("C1", "2.2")].interval == 60
    ka.cancel("C1", "1.1")
    assert ("C1", "1.1") not in ka._timers
    assert ("C1", "2.2") in ka._timers


def test_firing_the_timer_pauses_the_sandbox_and_clears_the_entry(monkeypatch):
    paused = []

    class _FakeSandbox:
        def pause(self):
            paused.append(True)

    class _FakeSandboxAPI:
        @staticmethod
        def connect(sandbox_id):
            assert sandbox_id == "sbx-1"
            return _FakeSandbox()

    monkeypatch.setattr(ka, "get_thread_sandbox_id", lambda c, t: "sbx-1")
    monkeypatch.setattr(ka, "Sandbox", _FakeSandboxAPI)

    ka.arm("C1", "1.1", 120)
    timer = ka._timers[("C1", "1.1")]
    timer.fire()

    assert paused == [True]
    assert ("C1", "1.1") not in ka._timers


def test_firing_the_timer_is_a_noop_when_no_sandbox_is_stored(monkeypatch):
    connected = []
    monkeypatch.setattr(ka, "get_thread_sandbox_id", lambda c, t: None)
    monkeypatch.setattr(ka, "Sandbox", type("S", (), {"connect": staticmethod(lambda sid: connected.append(sid))}))

    ka.arm("C1", "1.1", 120)
    ka._timers[("C1", "1.1")].fire()

    assert connected == []


def test_firing_the_timer_swallows_errors(monkeypatch):
    def boom(sandbox_id):
        raise RuntimeError("connect failed")

    monkeypatch.setattr(ka, "get_thread_sandbox_id", lambda c, t: "sbx-1")
    monkeypatch.setattr(ka, "Sandbox", type("S", (), {"connect": staticmethod(boom)}))

    ka.arm("C1", "1.1", 120)
    ka._timers[("C1", "1.1")].fire()  # must not raise
