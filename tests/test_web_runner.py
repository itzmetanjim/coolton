"""web.runner.submit_message — the same active-run/steering rule Slack follows
(listeners/events/message.py): don't race a second turn alongside one already
in flight, fold the message into it instead."""

import time
from unittest.mock import Mock

import pytest

from web import conversation_log as log


@pytest.fixture(autouse=True)
def tmp_store(monkeypatch, tmp_path):
    monkeypatch.setattr(log, "STORE_DIR", str(tmp_path / "web_conversations"))
    log._locks.clear()
    log._last_seq.clear()
    with log._subscribers_guard:
        log._subscribers.clear()
    yield
    from agent.active_runs import mark_run_finished
    from agent.steering_store import clear_steering_messages
    mark_run_finished("web", "convo-1")
    clear_steering_messages("web", "convo-1")


@pytest.fixture
def conversation_id():
    return log.create_conversation("U1")


def test_submit_message_records_a_user_message_event(conversation_id, monkeypatch):
    from web import runner

    submitted = []
    monkeypatch.setattr(runner._executor, "submit", lambda fn, *a: submitted.append((fn, a)))
    runner.submit_message(conversation_id, "U1", "hello")

    events = log.read_events(conversation_id)
    assert events[0]["type"] == "user_message"
    assert events[0]["text"] == "hello"
    assert events[0]["user_id"] == "U1"
    assert len(submitted) == 1


def test_submit_message_starts_a_fresh_turn_when_nothing_is_active(conversation_id, monkeypatch):
    from web import runner

    calls = []
    monkeypatch.setattr(runner._executor, "submit", lambda fn, *a: calls.append((fn, a)))
    runner.submit_message(conversation_id, "U1", "hello")

    assert len(calls) == 1
    fn, args = calls[0]
    assert fn is runner._run_turn
    assert args[0] == conversation_id
    assert args[1] == "U1"
    assert args[2] == "hello"


def test_submit_message_steers_into_an_active_run_instead_of_starting_a_new_one(conversation_id, monkeypatch):
    from agent.active_runs import mark_run_started
    from web import runner

    mark_run_started("web", conversation_id, time.time())
    calls = []
    monkeypatch.setattr(runner._executor, "submit", lambda fn, *a: calls.append((fn, a)))

    runner.submit_message(conversation_id, "U1", "keep going")

    assert calls == []  # no new turn started
    from agent.steering_store import peek_steering_messages
    queued = peek_steering_messages("web", conversation_id)
    assert len(queued) == 1
    assert queued[0]["text"] == "keep going"

    from agent.active_runs import mark_run_finished
    mark_run_finished("web", conversation_id)
    from agent.steering_store import clear_steering_messages
    clear_steering_messages("web", conversation_id)


def test_submit_message_reacts_to_the_steered_message_with_a_checkmark(conversation_id, monkeypatch):
    from agent.active_runs import mark_run_finished, mark_run_started
    from web import runner

    mark_run_started("web", conversation_id, time.time())
    monkeypatch.setattr(runner._executor, "submit", lambda fn, *a: None)

    runner.submit_message(conversation_id, "U1", "keep going")

    events = log.read_events(conversation_id)
    reaction_events = [e for e in events if e["type"] == "reaction"]
    assert len(reaction_events) == 1
    assert reaction_events[0]["emoji"] == "white_check_mark"

    mark_run_finished("web", conversation_id)
    from agent.steering_store import clear_steering_messages
    clear_steering_messages("web", conversation_id)


def test_run_turn_calls_run_agent_turn_with_the_web_channel_and_surface(conversation_id, monkeypatch):
    from web import runner

    captured = {}

    def fake_run_agent_turn(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("listeners.events.turn.run_agent_turn", fake_run_agent_turn)
    monkeypatch.setattr(runner, "_client", lambda: Mock())

    runner._run_turn(conversation_id, "U1", "hi", message_seq=1, attachments=[])

    assert captured["channel_id"] == "web"
    assert captured["thread_ts"] == conversation_id
    assert captured["user_id"] == "U1"
    assert captured["text"] == "hi"
    assert captured["surface"].name == "web"
    assert captured["surface"].target_seq == 1

    events = log.read_events(conversation_id)
    assert events[0]["type"] == "turn_start"


def test_run_turn_reports_an_error_if_run_agent_turn_itself_raises(conversation_id, monkeypatch):
    from web import runner

    def boom(**kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr("listeners.events.turn.run_agent_turn", boom)
    monkeypatch.setattr(runner, "_client", lambda: Mock())

    runner._run_turn(conversation_id, "U1", "hi", message_seq=1, attachments=[])

    events = log.read_events(conversation_id)
    assert events[-1]["type"] == "turn_end"
    assert events[-1]["state"] == "error"


def test_submit_message_names_an_unnamed_conversation_after_the_first_message(monkeypatch):
    from web import runner

    monkeypatch.setattr(runner._executor, "submit", lambda fn, *a: None)
    cid = log.create_conversation("U1")
    assert log.get_conversation_meta(cid)["title"] == ""

    runner.submit_message(cid, "U1", "fix the caddy config on the box")
    assert log.get_conversation_meta(cid)["title"] == "fix the caddy config on the box"


def test_submit_message_never_renames_a_conversation_that_already_has_a_title(monkeypatch):
    from web import runner

    monkeypatch.setattr(runner._executor, "submit", lambda fn, *a: None)
    cid = log.create_conversation("U1", title="Named by hand")

    runner.submit_message(cid, "U1", "something else entirely")
    assert log.get_conversation_meta(cid)["title"] == "Named by hand"
