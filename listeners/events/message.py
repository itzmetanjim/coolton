import html
import logging
import os
import re
from logging import Logger

from slack_bolt import BoltContext, Say, SayStream
from slack_sdk import WebClient

from agent.code_channel_store import CODE_CHANNEL_THREAD_TS, is_code_channel
from agent.leave_thread_store import is_thread_engaged
from agent.stop_store import is_stop_command
from listeners.commands import parse_command, respond
from listeners.events.shared import (
    handled_ban_command,
    is_ignored,
    pass_to_private_hooks,
    report_failure,
    steered_into_active_run,
    stop_thread,
)
from listeners.events.turn import run_agent_turn
from thread_context import conversation_store

_module_logger = logging.getLogger(__name__)

# Slack broadcast / user-group mentions: @channel, @here, @everyone, named
# user groups (e.g. <!subteam^S123|name>).
PING_GROUP_MENTION_RE = re.compile(r"<!(channel|here|everyone)>|<!subteam\^")

# Message subtypes that are still someone writing to the thread: a message with a file
# attached, and a thread reply also sent to the channel. Every other subtype (edits,
# deletes, joins, ...) is skipped.
_ANSWERED_SUBTYPES = {None, "file_share", "thread_broadcast"}

if not os.environ.get("COOLTON_BOT_ID"):
    _module_logger.warning(
        "COOLTON_BOT_ID is not set: the guard against double-answering a mid-thread "
        "@mention (handled once by app_mentioned, then again here) is disabled."
    )


def handle_message(
    client: WebClient,
    context: BoltContext,
    event: dict,
    logger: Logger,
    say: Say,
    say_stream: SayStream,
    set_status,  # SetStatus — unused, we call API directly
):
    """Handle messages sent to the agent via DM or in threads the bot is part of."""

    if event.get("subtype") not in _ANSWERED_SUBTYPES or is_ignored(context, event):
        return

    text = event.get("text", "")
    bot_id = os.environ.get("COOLTON_BOT_ID", "")
    is_dm = event.get("channel_type") == "im"

    # Mentions of the bot in a CHANNEL are owned by handle_app_mentioned (the
    # app_mention event) — without this guard, a mention inside an engaged thread is
    # picked up here too, launching a second coolton alongside the one from
    # app_mention. This must NOT apply to DMs: Slack never emits an app_mention event
    # for a DM (there's no separate "mention" concept in a 1:1 conversation), so a DM
    # containing "<@BOT_ID> ..." would otherwise be dropped by both handlers — owned
    # by neither.
    if not is_dm and bot_id and f"<@{bot_id}>" in text:
        return

    # A mention of the code channels bot is a mention of coolton (agent.mention_ids).
    # The code channels app's own listener gets it too when it's in the channel; the
    # first to claim it answers, so it's answered once whichever arrives first.
    from agent.mention_ids import mentions_code_channel_bot
    if not is_dm and bot_id and mentions_code_channel_bot(text):
        from listeners.events.app_mentioned import handle_app_mentioned
        handle_app_mentioned(client=client, context=context, event=event, logger=logger,
                             say=say, say_stream=say_stream, set_status=set_status)
        return

    # ## double-hash: never process or respond to a message starting with "##",
    # not even commands like !stop. Checked before !stop so "## !stop" is
    # ignored rather than halting runs.
    if text.strip().startswith("##"):
        logger.info(f"Ignoring message starting with '##': {text}")
        return

    channel_id = context.channel_id

    # A code channel's whole-channel conversation (any message NOT inside a
    # thread) is answered as if coolton were mentioned — see
    # agent.code_channel_store. A threaded reply inside a code channel is a
    # normal, separate Slack thread and takes none of this.
    at_channel_level = not is_dm and not event.get("thread_ts") and is_code_channel(channel_id)
    thread_ts = CODE_CHANNEL_THREAD_TS if at_channel_level else (event.get("thread_ts") or event["ts"])
    user_id = context.user_id

    # !stop: immediately halt every coolton run in this thread. Only honored
    # when the bot is explicitly @-mentioned (that path lives in
    # handle_app_mentioned), in a DM, or at a code channel's channel level —
    # in all three, every message is directed at the bot. A bare "!stop" in an
    # ordinary channel thread without a mention is ignored — it must never
    # kill running coolton instances on its own. Must be the message's entire
    # content (see is_stop_command) — a normal prompt that merely contains the
    # word "!stop" must not halt anything.
    if is_stop_command(text, bot_id):
        if not is_dm and not at_channel_level:
            logger.info("Ignoring '!stop' without an @mention")
        else:
            stop_thread(say, channel_id, thread_ts)
        return

    # !help / !connections: same addressing rule as !stop (a DM or a code
    # channel's channel level; mentions go through handle_app_mentioned).
    # Answered ephemerally without starting a turn.
    command = parse_command(text, bot_id)
    if command:
        if is_dm or at_channel_level:
            respond(client, command, channel_id, user_id, None if is_dm else thread_ts)
        return

    # !ban / !unban: only handled in a DM, same reasoning as !stop above — a
    # bare command in a channel thread without a mention is not addressed to
    # the bot at all.
    if is_dm and handled_ban_command(client, logger, text, bot_id, user_id, channel_id, event["ts"]):
        return

    # Messages starting with "<>" are only answered when the bot is explicitly
    # @-mentioned. Without a mention, ignore them even in engaged threads or
    # DMs (messages that DO mention the bot already return above and are owned
    # by handle_app_mentioned). Slack HTML-escapes literal angle brackets in
    # message text, so a user-typed "<>" arrives as "&lt;&gt;" — unescape it
    # before matching.
    stripped = html.unescape(text.strip())
    if stripped.startswith("<>"):
        logger.info(f"Ignoring '<>' message without explicit mention: {text[:80]}")
        return

    # Broadcast/user-group pings (@channel, @here, @everyone, named user
    # groups) are not addressed to the bot — a direct mention already returned
    # above. Don't process/respond to them.
    if PING_GROUP_MENTION_RE.search(text):
        logger.info(f"Ignoring ping-group mention without direct bot mention: {text[:80]}")
        return

    # Top-level channel messages are handled by app_mentioned — except at a
    # code channel's channel level, which IS the conversation and has no
    # "mention required" step to hand off to.
    if not is_dm and not at_channel_level and not event.get("thread_ts"):
        return

    # Channel thread replies are handled only while the bot is joined (engaged)
    # in the thread. A mid-thread mention answers once but does not join, so a
    # non-mentioned reply here means we aren't joined and should be ignored.
    # A code channel at channel level responds to everything by definition —
    # there's no engagement step to gate on.
    if not at_channel_level and not is_thread_engaged(channel_id, thread_ts, is_dm):
        logger.info(f"Ignoring message in unjoined thread {thread_ts} ({channel_id})")
        return

    pass_to_private_hooks(event)

    # The opt-in prompt only fires for messages the bot is actually meant to
    # answer (DMs and engaged threads), so coolton never barges into a channel
    # or thread it wasn't part of just to ask for consent.
    from agent.policy_consent import ensure_consent
    if not ensure_consent(
        client, say, user_id=user_id, channel_id=channel_id, thread_ts=thread_ts,
        message_ts=event["ts"],
    ):
        return

    if steered_into_active_run(client, logger, channel_id, thread_ts, text, user_id, event["ts"]):
        return

    try:
        # Get conversation history
        history = conversation_store.get_history(channel_id, thread_ts)

        from agent.tools.vision import download_attached_images
        images = download_attached_images(client, event.get("files"))

        run_agent_turn(
            client=client,
            say_stream=say_stream,
            say=say,
            logger=logger,
            channel_id=channel_id,
            thread_ts=thread_ts,
            message_ts=event["ts"],
            user_id=user_id,
            user_token=context.user_token,
            text=text,
            history=history,
            images=images,
        )

    except Exception as e:
        report_failure(say, logger, event, e)
