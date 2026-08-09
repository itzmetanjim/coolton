from unittest.mock import Mock

import pytest

from listeners.events.app_mentioned import handle_app_mentioned
from listeners.events.message import handle_message


@pytest.fixture
def ctx(monkeypatch):
    client = Mock()
    say = Mock()
    say_stream = Mock()
    logger = Mock()
    context = Mock()
    context.channel_id = "C123"
    context.user_id = "U1"
    context.user_token = "xoxp-user"

    monkeypatch.setattr("thread_context.thread_history.build_thread_context", Mock(return_value=["ctx"]))

    return SimpleCtx(client, context, say, say_stream, logger)


class SimpleCtx:
    def __init__(self, client, context, say, say_stream, logger):
        self.client = client
        self.context = context
        self.say = say
        self.say_stream = say_stream
        self.logger = logger


def _msg(ctx, **event):
    defaults = {
        "type": "message",
        "text": "hello",
        "ts": "111.111",
        "channel": "C123",
    }
    defaults.update(event)
    handle_message(
        client=ctx.client,
        context=ctx.context,
        event=defaults,
        logger=ctx.logger,
        say=ctx.say,
        say_stream=ctx.say_stream,
        set_status=None,
    )


def _mention(ctx, **event):
    defaults = {
        "type": "app_mention",
        "text": "<@BOT1> hello",
        "ts": "111.111",
        "channel": "C123",
    }
    defaults.update(event)
    handle_app_mentioned(
        client=ctx.client,
        context=ctx.context,
        event=defaults,
        logger=ctx.logger,
        say=ctx.say,
        say_stream=ctx.say_stream,
        set_status=None,
    )


def test_message_skips_bot_messages(ctx, monkeypatch):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn:
        _msg(ctx, bot_id="B123")
        run_turn.assert_not_called()


def test_message_skips_hardcoded_channel(ctx, monkeypatch):
    ctx.context.channel_id = "C06QV2T1P4G"
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn:
        _msg(ctx)
        run_turn.assert_not_called()


def test_message_mentions_are_handled_by_app_mentioned(monkeypatch):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn, \
         patch("os.environ.get", side_effect=lambda k, d=None: {"COOLTON_BOT_ID": "BOT1"}.get(k, d)):
        client, context, say, say_stream, logger = Mock(), Mock(), Mock(), Mock(), Mock()
        context.channel_id = "C123"
        context.user_id = "U1"
        event = {"type": "message", "text": "hi <@BOT1> there", "ts": "111.111"}
        handle_message(client, context, event, logger, say, say_stream, None)
        run_turn.assert_not_called()


def test_message_ignores_bare_stop_in_channel(monkeypatch):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn, \
         patch("listeners.events.message.request_stop") as request_stop:
        client, context, say, say_stream, logger = Mock(), Mock(), Mock(), Mock(), Mock()
        context.channel_id = "C123"
        context.user_id = "U1"
        event = {"type": "message", "text": "please !stop now", "ts": "111.111"}
        handle_message(client, context, event, logger, say, say_stream, None)
        request_stop.assert_not_called()
        say.assert_not_called()
        run_turn.assert_not_called()


def test_message_stop_requests_halt_in_dm(monkeypatch):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn, \
         patch("listeners.events.message.request_stop") as request_stop:
        client, context, say, say_stream, logger = Mock(), Mock(), Mock(), Mock(), Mock()
        context.channel_id = "D123"
        context.user_id = "U1"
        event = {"type": "message", "text": "please !stop now", "ts": "111.111", "channel_type": "im"}
        handle_message(client, context, event, logger, say, say_stream, None)
        request_stop.assert_called_once_with("D123", "111.111")
        say.assert_called_once()
        run_turn.assert_not_called()


def test_message_ignores_angle_brackets_without_mention(ctx, monkeypatch):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn:
        _msg(ctx, text="<> ignore me")
        run_turn.assert_not_called()


def test_message_ignores_ping_group_mention(ctx, monkeypatch):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn:
        _msg(ctx, text="<@U0> <!channel> check this out")
        run_turn.assert_not_called()


def test_message_skips_top_level_channel_messages(ctx, monkeypatch):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn:
        _msg(ctx, channel_type="channel")  # no thread_ts
        run_turn.assert_not_called()


def test_message_skips_unengaged_thread(ctx, monkeypatch):

    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn, \
         patch("listeners.events.message.is_thread_engaged", return_value=False):
        _msg(ctx, channel_type="channel", thread_ts="1.1")
        run_turn.assert_not_called()


def test_message_engages_in_dm(ctx):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn:
        _msg(ctx, channel_type="im")
        run_turn.assert_called_once()
        kwargs = run_turn.call_args.kwargs
        assert kwargs["channel_id"] == "C123"
        assert kwargs["thread_ts"] == "111.111"
        assert kwargs["user_id"] == "U1"
        assert kwargs["user_token"] == "xoxp-user"
        assert kwargs["text"] == "hello"
        assert kwargs["message_ts"] == "111.111"


def test_message_ignores_hashtag_prefix(ctx):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn:
        _msg(ctx, text="## internal note")
        run_turn.assert_not_called()


# ---------------------------------------------------------------------------
# handle_app_mentioned
# ---------------------------------------------------------------------------


def test_app_mentioned_skips_hardcoded_channel(ctx):
    from unittest.mock import patch

    with patch("listeners.events.app_mentioned.run_agent_turn") as run_turn:
        ctx.context.channel_id = "C06QV2T1P4G"
        _mention(ctx)
        run_turn.assert_not_called()


def test_app_mentioned_ignores_hashtag_prefix(ctx):
    from unittest.mock import patch

    with patch("listeners.events.app_mentioned.run_agent_turn") as run_turn:
        _mention(ctx, text="## agenda")
        run_turn.assert_not_called()


def test_app_mentioned_stop(ctx):

    from unittest.mock import patch

    with patch("listeners.events.app_mentioned.run_agent_turn") as run_turn, \
         patch("listeners.events.app_mentioned.request_stop") as request_stop:
        _mention(ctx, text="!stop")
        request_stop.assert_called_once_with("C123", "111.111")
        ctx.say.assert_called_once()
        run_turn.assert_not_called()


def test_app_mentioned_starter_mention_joins_thread(ctx):

    from unittest.mock import patch

    with patch("listeners.events.app_mentioned.run_agent_turn") as run_turn, \
         patch("listeners.events.app_mentioned.join_thread") as join_thread, \
         patch("listeners.events.app_mentioned.ensure_coolton_user_in_channel"):
        _mention(ctx, text="<@BOT1> hi", ts="1.1", thread_ts="1.1")
        join_thread.assert_called_once_with("C123", "1.1")
        run_turn.assert_called_once()


def test_app_mentioned_mid_thread_mention_does_not_join(ctx):

    from unittest.mock import patch

    with patch("listeners.events.app_mentioned.run_agent_turn") as run_turn, \
         patch("listeners.events.app_mentioned.join_thread") as join_thread:
        _mention(ctx, text="<@BOT1> hi", ts="111.111", thread_ts="1.1")
        join_thread.assert_not_called()
        run_turn.assert_called_once()


def test_app_mentioned_empty_ping_greets(ctx):
    from unittest.mock import patch

    with patch("listeners.events.app_mentioned.run_agent_turn") as run_turn:
        _mention(ctx, text="<@BOT1>")
        run_turn.assert_not_called()
        ctx.say.assert_called_once()
        assert "How can I help you" in ctx.say.call_args.kwargs["text"]


def test_app_mentioned_builds_thread_context_when_history_none(ctx):
    import thread_context.thread_history as th

    from unittest.mock import patch

    store = Mock(get_history=Mock(return_value=None))

    with patch("listeners.events.app_mentioned.run_agent_turn") as run_turn, \
         patch("listeners.events.app_mentioned.conversation_store", store):
        _mention(ctx, text="<@BOT1> hi", ts="111.111", thread_ts="1.1")
        th.build_thread_context.assert_called_once()
        kwargs = run_turn.call_args.kwargs
        assert kwargs["history"] == ["ctx"]


def test_app_mentioned_uses_existing_history(ctx):
    from unittest.mock import patch

    store = Mock(get_history=Mock(return_value=["old-history"]))

    with patch("listeners.events.app_mentioned.run_agent_turn") as run_turn, \
         patch("listeners.events.app_mentioned.conversation_store", store), \
         patch("thread_context.thread_history.build_thread_context") as build_ctx:
        _mention(ctx, text="<@BOT1> hi")
        build_ctx.assert_not_called()
        assert run_turn.call_args.kwargs["history"] == ["old-history"]
