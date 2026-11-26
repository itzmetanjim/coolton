"""agent.thread_status: a turn's live status through Slack agent sessions."""
from unittest.mock import Mock

import pytest

from agent import thread_status as ts


class _FakeTimer:
    def __init__(self, interval, function, args=()):
        self.interval, self.function, self.args = interval, function, args
        self.started = self.canceled = False

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
    ts._pinned.clear()
    yield
    ts._state.clear()
    ts._pinned.clear()


def calls(client):
    return [(c.args[0], c.kwargs["json"]) for c in client.api_call.call_args_list]


def test_a_turn_is_a_processing_session_titled_after_the_request_then_active():
    client = Mock()
    session = {"channel_id": "C1", "thread_ts": "1.1"}
    ts.start(client, "C1", "1.1", request_text="<@U0BOT>  can you check the   deploy logs?")
    timer = ts._state[("C1", "1.1")]["timer"]
    ts.stop("C1", "1.1")

    title = "can you check the deploy logs?"
    assert calls(client) == [
        ("agents.sessions.setStatus", {**session, "status": "processing", "title": title}),
        ("agents.sessions.rename", {**session, "title": title}),
        ("agents.sessions.setStatus", {**session, "status": "active"}),
    ]
    assert timer.canceled and ("C1", "1.1") not in ts._state


def test_a_long_turn_keeps_processing_alive_past_slacks_one_hour_timeout():
    client = Mock()
    ts.start(client, "C1", "1.1")
    timer = ts._state[("C1", "1.1")]["timer"]
    assert timer.interval < 60 * 60
    client.api_call.reset_mock()

    timer.fire()

    assert calls(client) == [("agents.sessions.setStatus", {"channel_id": "C1", "thread_ts": "1.1", "status": "processing"})]
    assert ts._state[("C1", "1.1")]["timer"] is not timer


def test_titles_are_only_what_slack_accepts():
    """Slack refuses a session title over 80 characters or with most punctuation, Slack
    markup included (invalid_name, checked live), and the session then keeps a stale title."""
    client = Mock()
    ts.start(client, "C1", "1.1", request_text="x" * 300)
    assert len(calls(client)[0][1]["title"]) == 80
    assert ts._title("go to <https://ctf2026.hitcon.org/|ctf2026.hitcon.org> &lt;&gt; 50% off: now/later") == \
        "go to ctf2026.hitcon.org 50 off now later"
    assert ts._title("&lt;&gt; look at <#C0CA5LKDDRS|coolton-auto-test> 😅") == "look at coolton-auto-test 😅"


def test_a_code_channel_turn_is_never_titled_after_the_request(monkeypatch):
    """A code channel's session title is the channel's name: titling it after each
    message renamed the channel to the latest prompt."""
    sent = []
    monkeypatch.setattr("agent.code_channel_api.call", lambda method, **p: sent.append((method, p)) or {"ok": True})
    client = Mock()
    ts.stop("C1", "1.1")  # never started
    ts.start(client, "C1", "", request_text="hi")  # channel-level code channel: no thread
    ts.stop("C1", "")
    assert client.api_call.call_count == 0  # its session is the code channels app's
    assert sent == [("agents.sessions.setStatus", {"channel_id": "C1", "status": "processing"}),
                    ("agents.sessions.setStatus", {"channel_id": "C1", "status": "active"})]


def test_a_failing_status_api_never_breaks_the_turn():
    client = Mock()
    client.api_call.side_effect = Exception("status api down")
    ts.start(client, "C1", "1.1")
    ts.stop("C1", "1.1")


def test_an_explicit_suspended_status_survives_the_turns_end():
    """An agent that has posted a question is suspended, not active: the turn's own stop()
    must not flip it back (agent.thread_status.set_status pins it)."""
    client = Mock()
    ts.start(client, "C1", "1.1")
    client.api_call.reset_mock()
    ts.set_status(client, "C1", "1.1", "suspended")
    ts.stop("C1", "1.1")
    # set_status sets it, and the turn's stop() re-asserts it instead of flipping to active.
    assert calls(client) == [("agents.sessions.setStatus", {"channel_id": "C1", "thread_ts": "1.1", "status": "suspended"})] * 2


def test_a_pinned_status_is_unpinned_by_the_next_turn_and_by_active():
    client = Mock()
    ts.start(client, "C1", "1.1")
    ts.set_status(client, "C1", "1.1", "suspended")
    ts.stop("C1", "1.1")
    client.api_call.reset_mock()
    ts.start(client, "C1", "1.1", request_text="hi")
    assert calls(client)[0] == ("agents.sessions.setStatus", {"channel_id": "C1", "thread_ts": "1.1", "status": "processing", "title": "hi"})

    client.api_call.reset_mock()
    ts.set_status(client, "C1", "1.1", "active")
    ts.stop("C1", "1.1")
    assert calls(client) == [("agents.sessions.setStatus", {"channel_id": "C1", "thread_ts": "1.1", "status": "active"})] * 2


@pytest.mark.parametrize("running", [True, False])
def test_the_stop_button_halts_the_run_and_takes_the_session_out_of_processing(monkeypatch, running):
    from listeners.events import agent_session_stopped as handler

    stopped = []
    monkeypatch.setattr(handler, "is_run_active", lambda c, t: running)
    monkeypatch.setattr(handler, "request_stop", lambda c, t: stopped.append((c, t)))
    client = Mock()

    handler.handle_agent_session_stopped(
        client, {"type": "agent_session_stopped", "channel": "C1", "thread_ts": "1.1", "user": "U1"}, Mock())

    assert stopped == ([("C1", "1.1")] if running else [])
    assert client.chat_postMessage.called == running
    assert calls(client) == [("agents.sessions.setStatus", {"channel_id": "C1", "thread_ts": "1.1", "status": "active"})]
