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
    yield
    ts._state.clear()


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


def test_titles_are_cropped_to_slacks_200_character_limit():
    client = Mock()
    ts.start(client, "C1", "1.1", request_text="x" * 300)
    assert len(calls(client)[0][1]["title"]) == 200


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
