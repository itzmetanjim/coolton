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
        provider_tag_filter=kw.get("provider_tag_filter"),
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

    def fake_run_agent(text, deps, message_history=None, images=None):
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

    def fake_run_agent(text, deps, message_history=None, images=None):
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


def test_streaming_failure_falls_back_to_chat_post_message(mocks):
    import agent.plan_block as pb

    mocks.say_stream.return_value.append.side_effect = Exception(
        "SlackApiError: msg_too_long"
    )
    _run_turn(mocks)

    # no "something went wrong" warning; the response was delivered instead
    mocks.say.assert_not_called()
    pb.complete_plan_message.assert_called_once()

    posts = mocks.client.chat_postMessage.call_args_list
    assert posts
    texts = [c.kwargs["markdown_text"] for c in posts if "markdown_text" in c.kwargs]
    assert texts == ["Here is the answer."]
    assert any("blocks" in c.kwargs for c in posts)


def test_streaming_stop_failure_falls_back_to_chat_post_message(mocks):
    import agent.plan_block as pb

    mocks.say_stream.return_value.stop.side_effect = Exception("stream down")
    _run_turn(mocks)

    mocks.say.assert_not_called()
    pb.complete_plan_message.assert_called_once()
    texts = [c.kwargs["markdown_text"] for c in mocks.client.chat_postMessage.call_args_list if "markdown_text" in c.kwargs]
    assert texts == ["Here is the answer."]


def test_chunk_text_splits_on_line_boundaries():
    text = "\n".join(f"line {i} " * 30 for i in range(50))
    chunks = turn._chunk_text(text, limit=1000)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(chunks) == text


def test_chunk_text_short_text_unchanged():
    assert turn._chunk_text("hi there", limit=1000) == ["hi there"]


def test_chunk_text_hard_splits_overlong_line():
    line = "x" * 2500
    chunks = turn._chunk_text(line, limit=1000)
    assert chunks == ["x" * 1000, "x" * 1000, "x" * 500]


def test_chunk_text_respects_default_limit():
    long = "a" * (turn._MAX_MESSAGE_CHARS + 100)
    chunks = turn._chunk_text(long)
    assert all(len(c) <= turn._MAX_MESSAGE_CHARS for c in chunks)
    assert "".join(chunks) == long


# ---------------------------------------------------------------------------
# [!WITH:tag] provider tag directive
# ---------------------------------------------------------------------------


def test_valid_tag_directive_is_stripped_and_forces_provider(mocks):
    _run_turn(mocks, text="hi [!WITH:luna] there")

    call_text = turn.run_agent.call_args.args[0]
    assert "[!WITH:" not in call_text
    assert "hi" in call_text and "there" in call_text

    deps_used = turn.run_agent.call_args.args[1]
    assert deps_used.provider_tag_filter == "luna"

    # a live directive is not an error — the turn proceeds normally
    turn.conversation_store.set_history.assert_called_once()


def test_invalid_tag_directive_short_circuits_before_any_work(mocks):
    _run_turn(mocks, text="hi [!WITH:bogus] there")

    turn.run_agent.assert_not_called()
    mocks.client.assistant_threads_setStatus.assert_not_called()
    mocks.say.assert_called_once()
    error_text = mocks.say.call_args.kwargs["text"]
    assert "bogus" in error_text
    assert "luna" in error_text

    import agent.plan_block as pb
    pb.send_plan_message.assert_not_called()
    turn.conversation_store.set_history.assert_not_called()


def test_escaped_tag_directive_is_left_literal_with_backslash_stripped(mocks):
    _run_turn(mocks, text=r"hi \[!WITH:luna] there")

    call_text = turn.run_agent.call_args.args[0]
    assert call_text == "hi [!WITH:luna] there"

    deps_used = turn.run_agent.call_args.args[1]
    assert deps_used.provider_tag_filter is None


def test_no_tag_directive_leaves_provider_tag_filter_none(mocks):
    _run_turn(mocks, text="just a normal message")

    deps_used = turn.run_agent.call_args.args[1]
    assert deps_used.provider_tag_filter is None
