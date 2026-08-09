from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import listeners.events.turn as turn


@pytest.fixture
def mocks(monkeypatch):
    client = Mock()
    say = Mock()
    say_stream = Mock()
    logger = Mock()

    monkeypatch.setattr(turn, "conversation_store", Mock())

    result = SimpleNamespace(
        output="Here is the answer.",
        all_messages=lambda: ["msg1"],
    )

    monkeypatch.setattr(turn, "run_agent", Mock(return_value=result))
    monkeypatch.setattr(turn, "AgentDeps", Mock(side_effect=lambda **kw: SimpleNamespace(
        client=kw["client"],
        user_id=kw["user_id"],
        channel_id=kw["channel_id"],
        thread_ts=kw["thread_ts"],
        message_ts=kw["message_ts"],
        user_token=kw["user_token"],
        plan_ts=None,
        plan_tasks={},
        should_skip=False,
        halt_reason="",
        model_used="",
        run_started_at=0.0,
    )))

    import agent.plan_block as pb
    monkeypatch.setattr(pb, "send_plan_message", Mock(return_value="100.200"))
    monkeypatch.setattr(pb, "finalize_plan_message", Mock())
    monkeypatch.setattr(pb, "complete_plan_message", Mock())
    monkeypatch.setattr(pb, "delete_plan_message", Mock())
    monkeypatch.setattr(pb, "set_plan_error", Mock())

    import agent.kevinton as kev
    monkeypatch.setattr(kev, "spawn_kevinton", Mock())

    return SimpleNamespace(
        client=client, say=say, say_stream=say_stream, logger=logger,
    )


def _run_turn(mocks, text="hello"):
    turn.run_agent_turn(
        client=mocks.client,
        say_stream=mocks.say_stream,
        say=mocks.say,
        logger=mocks.logger,
        channel_id="C1",
        thread_ts="1.1",
        message_ts="111.111",
        user_id="U1",
        user_token="xoxp-user",
        text=text,
        history=None,
    )


def test_happy_path(mocks):
    import agent.plan_block as pb
    _run_turn(mocks)

    mocks.client.assistant_threads_setStatus.assert_called_once()
    status_call = mocks.client.assistant_threads_setStatus.call_args.kwargs
    assert status_call["status"] == "Thinking..."

    pb.send_plan_message.assert_called_once()
    pb.finalize_plan_message.assert_called_once()
    pb.complete_plan_message.assert_called_once()
    pb.delete_plan_message.assert_not_called()

    # streamed the model output
    streamer = mocks.say_stream.return_value
    streamer.append.assert_called_once_with(markdown_text="Here is the answer.")
    streamer.stop.assert_called_once()

    # history persisted
    turn.conversation_store.set_history.assert_called_once_with("C1", "1.1", ["msg1"])

    import agent.kevinton as kev
    kev.spawn_kevinton.assert_called_once()


def test_skip_path_deletes_plan_and_does_not_stream(mocks):
    import agent.plan_block as pb

    def fake_run_agent(text, deps, message_history=None):
        deps.should_skip = True
        return SimpleNamespace(output="", all_messages=lambda: [])

    turn.run_agent.side_effect = fake_run_agent
    _run_turn(mocks, text="skip this")

    pb.delete_plan_message.assert_called_once()
    pb.finalize_plan_message.assert_not_called()
    pb.complete_plan_message.assert_not_called()
    mocks.say_stream.assert_not_called()
    turn.conversation_store.set_history.assert_called_once()

    import agent.kevinton as kev
    kev.spawn_kevinton.assert_not_called()


def test_stop_path_keeps_plan_and_sets_error(mocks):
    import agent.plan_block as pb

    def fake_run_agent(text, deps, message_history=None):
        deps.should_skip = True
        deps.halt_reason = "!stop requested"
        return SimpleNamespace(output="", all_messages=lambda: [])

    turn.run_agent.side_effect = fake_run_agent
    _run_turn(mocks, text="stop this")

    pb.delete_plan_message.assert_not_called()
    pb.set_plan_error.assert_called_once()
    args, kwargs = pb.set_plan_error.call_args
    assert "manually stopped" in args[1]
    pb.finalize_plan_message.assert_not_called()
    pb.complete_plan_message.assert_not_called()
    mocks.say_stream.assert_not_called()

    import agent.kevinton as kev
    kev.spawn_kevinton.assert_not_called()


def test_error_path_reports_and_sets_plan_error(mocks):
    import agent.plan_block as pb

    turn.run_agent.side_effect = RuntimeError("boom")
    _run_turn(mocks)

    pb.set_plan_error.assert_called_once()
    args, kwargs = pb.set_plan_error.call_args
    assert "boom" in args[1]

    mocks.say.assert_called_once()
    say_text = mocks.say.call_args.kwargs["text"]
    assert "Something went wrong" in say_text
    assert "RuntimeError" in say_text
    assert mocks.say.call_args.kwargs["thread_ts"] == "1.1"


def test_status_failure_still_reports_error(mocks):
    import agent.plan_block as pb

    mocks.client.assistant_threads_setStatus.side_effect = Exception("status api down")
    turn.run_agent.side_effect = RuntimeError("boom")
    _run_turn(mocks)

    # no deps were created yet, so no plan to mark as errored
    pb.set_plan_error.assert_not_called()
    mocks.say.assert_called_once()
