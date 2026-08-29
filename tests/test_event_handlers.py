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
    monkeypatch.setattr("agent.policy_consent.has_consent", lambda user_id: True)
    monkeypatch.setattr("agent.policy_consent.user_is_in_policy_channel", lambda client, user_id: False)

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


def test_message_steers_into_active_run_instead_of_starting_a_new_one(ctx):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn, \
         patch("listeners.events.message.is_run_active", return_value=True), \
         patch("listeners.events.message.queue_steering_message") as queue_steering:
        _msg(ctx, channel_type="im", text="also check the other thing")

        run_turn.assert_not_called()
        queue_steering.assert_called_once_with("C123", "111.111", "also check the other thing", "U1")
        ctx.client.reactions_add.assert_called_once_with(
            channel="C123", timestamp="111.111", name="white_check_mark"
        )


def test_message_does_not_steer_when_no_run_is_active(ctx):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn, \
         patch("listeners.events.message.is_run_active", return_value=False):
        _msg(ctx, channel_type="im")

        run_turn.assert_called_once()
        ctx.client.reactions_add.assert_not_called()


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


def test_app_mentioned_steers_into_active_run_instead_of_starting_a_new_one(ctx):
    from unittest.mock import patch

    with patch("listeners.events.app_mentioned.run_agent_turn") as run_turn, \
         patch("listeners.events.app_mentioned.ensure_coolton_user_in_channel"), \
         patch("listeners.events.app_mentioned.is_run_active", return_value=True), \
         patch("listeners.events.app_mentioned.queue_steering_message") as queue_steering:
        _mention(ctx, text="<@BOT1> also check the other thing", ts="1.1", thread_ts="1.1")

        run_turn.assert_not_called()
        queue_steering.assert_called_once_with("C123", "1.1", "<@BOT1> also check the other thing", "U1")
        ctx.client.reactions_add.assert_called_once_with(
            channel="C123", timestamp="1.1", name="white_check_mark"
        )


def test_app_mentioned_does_not_steer_when_no_run_is_active(ctx):
    from unittest.mock import patch

    with patch("listeners.events.app_mentioned.run_agent_turn") as run_turn, \
         patch("listeners.events.app_mentioned.ensure_coolton_user_in_channel"), \
         patch("listeners.events.app_mentioned.join_thread"), \
         patch("listeners.events.app_mentioned.is_run_active", return_value=False):
        _mention(ctx, text="<@BOT1> hi", ts="1.1", thread_ts="1.1")

        run_turn.assert_called_once()
        ctx.client.reactions_add.assert_not_called()


def test_app_mentioned_mid_thread_mention_does_not_join(ctx):

    from unittest.mock import patch

    with patch("listeners.events.app_mentioned.run_agent_turn") as run_turn, \
         patch("listeners.events.app_mentioned.join_thread") as join_thread:
        _mention(ctx, text="<@BOT1> hi", ts="111.111", thread_ts="1.1")
        join_thread.assert_not_called()
        run_turn.assert_called_once()


def test_app_mentioned_empty_ping_runs_a_turn_with_thread_context_prefilled(ctx):
    """A bare "@coolton" with no text used to short-circuit to a hardcoded canned
    greeting and never touch history at all. It should run a real turn instead, with
    the thread's existing context prefilled if there is any, so the model can respond
    to what's actually going on rather than a generic string."""
    from unittest.mock import patch

    store = Mock(get_history=Mock(return_value=None))
    with patch("listeners.events.app_mentioned.run_agent_turn") as run_turn, \
         patch("listeners.events.app_mentioned.conversation_store", store):
        _mention(ctx, text="<@BOT1>", ts="111.111", thread_ts="1.1")
        run_turn.assert_called_once()
        assert run_turn.call_args.kwargs["history"] == ["ctx"]  # from build_thread_context, mocked in ctx fixture
    ctx.say.assert_not_called()


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


def test_app_mentioned_skips_bot_mentions(ctx):
    from unittest.mock import patch

    with patch("listeners.events.app_mentioned.run_agent_turn") as run_turn:
        _mention(ctx, bot_id="B123")
    run_turn.assert_not_called()
    ctx.say.assert_not_called()


# ---------------------------------------------------------------------------
# Policy opt-in: only fires for messages coolton would actually answer
# ---------------------------------------------------------------------------


def _no_consent(monkeypatch):
    monkeypatch.setattr("agent.policy_consent.has_consent", lambda user_id: False)
    monkeypatch.setattr("agent.policy_consent.user_is_in_policy_channel", lambda client, user_id: False)


def test_message_non_consenting_user_in_unengaged_thread_gets_no_opt_in(ctx, monkeypatch):
    _no_consent(monkeypatch)
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn, \
         patch("listeners.events.message.is_thread_engaged", return_value=False):
        _msg(ctx, channel_type="channel", thread_ts="1.1")
    ctx.say.assert_not_called()
    run_turn.assert_not_called()


def test_message_non_consenting_user_top_level_channel_gets_no_opt_in(ctx, monkeypatch):
    _no_consent(monkeypatch)
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn:
        _msg(ctx, channel_type="channel")
    ctx.say.assert_not_called()
    run_turn.assert_not_called()


def test_message_non_consenting_user_in_dm_gets_opt_in(ctx, monkeypatch):
    _no_consent(monkeypatch)
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn, \
         patch("agent.policy_consent.save_pending", return_value="pending-1") as save_pending, \
         patch("agent.policy_consent.build_opt_in_blocks", return_value=[]) as build_blocks:
        _msg(ctx, channel_type="im")
    run_turn.assert_not_called()
    ctx.say.assert_called_once()
    text = ctx.say.call_args.kwargs["text"]
    assert "you need to opt in to the Coolton policy" in text
    assert "please opt in" not in text
    save_pending.assert_called_once()
    build_blocks.assert_called_once_with("pending-1")


def test_app_mentioned_non_consenting_user_gets_opt_in(ctx, monkeypatch):
    _no_consent(monkeypatch)
    from unittest.mock import patch

    with patch("listeners.events.app_mentioned.run_agent_turn") as run_turn, \
         patch("agent.policy_consent.save_pending", return_value="pending-1") as save_pending, \
         patch("agent.policy_consent.build_opt_in_blocks", return_value=[]) as build_blocks:
        _mention(ctx, text="<@BOT1> hi")
    run_turn.assert_not_called()
    ctx.say.assert_called_once()
    text = ctx.say.call_args.kwargs["text"]
    assert "you need to opt in to the Coolton policy" in text
    save_pending.assert_called_once()
    build_blocks.assert_called_once_with("pending-1")


def test_app_mentioned_stop_works_without_consent(ctx, monkeypatch):
    _no_consent(monkeypatch)
    from unittest.mock import patch

    with patch("listeners.events.app_mentioned.run_agent_turn") as run_turn, \
         patch("listeners.events.app_mentioned.request_stop") as request_stop:
        _mention(ctx, text="!stop")
    request_stop.assert_called_once_with("C123", "111.111")
    ctx.say.assert_called_once()
    assert "stopping" in ctx.say.call_args.kwargs["text"]
    run_turn.assert_not_called()


# ---------------------------------------------------------------------------
# Rule 1: Double-hash (##) convention - blocks everything including mentions
# ---------------------------------------------------------------------------


def test_message_ignores_double_hash_in_dm(ctx):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn:
        _msg(ctx, text="## internal note", channel_type="im")
        run_turn.assert_not_called()


def test_message_ignores_double_hash_in_engaged_thread(ctx):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn, \
         patch("listeners.events.message.is_thread_engaged", return_value=True):
        _msg(ctx, text="## internal note", channel_type="channel", thread_ts="1.1")
        run_turn.assert_not_called()


def test_app_mentioned_ignores_double_hash_even_with_mention(ctx):
    from unittest.mock import patch

    with patch("listeners.events.app_mentioned.run_agent_turn") as run_turn:
        _mention(ctx, text="## agenda <@BOT1>")
        run_turn.assert_not_called()


def test_message_double_hash_blocks_stop_command_in_dm(ctx, monkeypatch):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn, \
         patch("listeners.events.message.request_stop") as request_stop:
        _msg(ctx, text="## !stop", channel_type="im")
        request_stop.assert_not_called()
        run_turn.assert_not_called()


def test_message_double_hash_blocks_ping_group(ctx):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn:
        _msg(ctx, text="## <!channel> announcement", channel_type="im")
        run_turn.assert_not_called()


# ---------------------------------------------------------------------------
# Rule 2: Thread stop command (@bot !stop)
# ---------------------------------------------------------------------------


def test_app_mentioned_stop_in_thread(ctx):
    from unittest.mock import patch

    with patch("listeners.events.app_mentioned.run_agent_turn") as run_turn, \
         patch("listeners.events.app_mentioned.request_stop") as request_stop:
        _mention(ctx, text="<@BOT1> !stop", ts="111.111", thread_ts="1.1")
        request_stop.assert_called_once_with("C123", "1.1")
        ctx.say.assert_called_once()
        run_turn.assert_not_called()


def test_message_stop_in_dm_only(ctx, monkeypatch):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn, \
         patch("listeners.events.message.request_stop") as request_stop:
        _msg(ctx, text="<@BOT1> !stop", channel_type="im")
        request_stop.assert_called_once_with("C123", "111.111")
        ctx.say.assert_called_once()
        run_turn.assert_not_called()


def test_message_stop_ignored_in_channel_without_mention(ctx, monkeypatch):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn, \
         patch("listeners.events.message.request_stop") as request_stop:
        _msg(ctx, text="please !stop now", channel_type="channel", thread_ts="1.1")
        request_stop.assert_not_called()
        run_turn.assert_not_called()


# ---------------------------------------------------------------------------
# Rule 3: Ping group mentions without direct bot mention
# ---------------------------------------------------------------------------


def test_message_ignores_at_channel_without_bot_mention(ctx):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn:
        _msg(ctx, text="<!channel> hello everyone", channel_type="im")
        run_turn.assert_not_called()


def test_message_ignores_at_here_without_bot_mention(ctx):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn:
        _msg(ctx, text="<!here> quick question", channel_type="im")
        run_turn.assert_not_called()


def test_message_ignores_at_everyone_without_bot_mention(ctx):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn:
        _msg(ctx, text="<!everyone> meeting in 5", channel_type="im")
        run_turn.assert_not_called()


def test_message_ignores_user_group_without_bot_mention(ctx):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn:
        _msg(ctx, text="<!subteam^S123|team> please review", channel_type="im")
        run_turn.assert_not_called()


def test_message_ping_group_allowed_with_bot_mention(ctx):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn, \
         patch("os.environ.get", side_effect=lambda k, d=None: {"COOLTON_BOT_ID": "BOT1"}.get(k, d)):
        _msg(ctx, text="<@BOT1> <!channel> important!", channel_type="im")
        # Mentions are handled by app_mentioned, so message handler returns early
        run_turn.assert_not_called()


def test_app_mentioned_ping_group_allowed(ctx):
    from unittest.mock import patch

    with patch("listeners.events.app_mentioned.run_agent_turn") as run_turn:
        _mention(ctx, text="<@BOT1> <!channel> important!")
        run_turn.assert_called_once()


# ---------------------------------------------------------------------------
# Rule 4: Angle-bracket (<>) convention - blocks unless explicit @-mention
# ---------------------------------------------------------------------------


def test_message_ignores_angle_brackets_in_dm(ctx):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn:
        _msg(ctx, text="<> ignore me", channel_type="im")
        run_turn.assert_not_called()


def test_message_ignores_angle_brackets_in_engaged_thread(ctx):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn, \
         patch("listeners.events.message.is_thread_engaged", return_value=True):
        _msg(ctx, text="<> ignore me", channel_type="channel", thread_ts="1.1")
        run_turn.assert_not_called()


def test_message_ignores_angle_brackets_with_bot_name_not_mention(ctx):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn:
        # "coolton" as plain text, not a user-id mention
        _msg(ctx, text="<> coolton hello", channel_type="im")
        run_turn.assert_not_called()


def test_message_ignores_html_escaped_angle_brackets(ctx):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn:
        # Slack HTML-escapes literal angle brackets: a user-typed "<>" arrives
        # as "&lt;&gt;". Regression test for the production bug.
        _msg(ctx, text="&lt;&gt; if you can see this, reply with: 3ee7d3c6", channel_type="im")
        run_turn.assert_not_called()


def test_message_ignores_html_escaped_angle_brackets_in_engaged_thread(ctx):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn, \
         patch("listeners.events.message.is_thread_engaged", return_value=True):
        _msg(ctx, text="&lt;&gt; if you can see this", channel_type="channel", thread_ts="1.1")
        run_turn.assert_not_called()


def test_message_angle_brackets_allowed_with_explicit_mention(ctx, monkeypatch):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn, \
         patch("os.environ.get", side_effect=lambda k, d=None: {"COOLTON_BOT_ID": "BOT1"}.get(k, d)):
        # Explicit @-mention: message handler defers to app_mentioned
        _msg(ctx, text="<> <@BOT1> hello", channel_type="im")
        run_turn.assert_not_called()


def test_app_mentioned_angle_brackets_allowed_with_mention(ctx):
    from unittest.mock import patch

    with patch("listeners.events.app_mentioned.run_agent_turn") as run_turn:
        _mention(ctx, text="<> <@BOT1> hello")
        run_turn.assert_called_once()


# ---------------------------------------------------------------------------
# Edge cases: whitespace, case, messages that should still process
# ---------------------------------------------------------------------------


def test_message_processes_normal_message_in_dm(ctx):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn:
        _msg(ctx, text="hello there", channel_type="im")
        run_turn.assert_called_once()


def test_message_processes_normal_message_in_engaged_thread(ctx):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn, \
         patch("listeners.events.message.is_thread_engaged", return_value=True):
        _msg(ctx, text="hello there", channel_type="channel", thread_ts="1.1")
        run_turn.assert_called_once()


def test_message_processes_whitespace_before_double_hash(ctx):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn:
        _msg(ctx, text="  ## still blocked", channel_type="im")
        run_turn.assert_not_called()


def test_message_processes_whitespace_before_angle_brackets(ctx):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn:
        _msg(ctx, text="  <> still blocked", channel_type="im")
        run_turn.assert_not_called()


def test_message_case_insensitive_double_hash(ctx):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn:
        _msg(ctx, text="## INTERNAL", channel_type="im")
        run_turn.assert_not_called()


def test_message_case_sensitive_angle_brackets(ctx):
    from unittest.mock import patch

    with patch("listeners.events.message.run_agent_turn") as run_turn:
        _msg(ctx, text="<> valid", channel_type="im")
        run_turn.assert_not_called()
        # Only exact <> prefix is blocked, not case variations
        # <> is literal characters, not case-sensitive
