import html
import os
import re
from logging import Logger

from slack_bolt import BoltContext, Say, SayStream
from slack_sdk import WebClient

from agent.leave_thread_store import is_thread_engaged
from agent.stop_store import request_stop
from thread_context import conversation_store
from listeners.events.turn import run_agent_turn

# Slack broadcast / user-group mentions: @channel, @here, @everyone, named
# user groups (e.g. <!subteam^S123|name>).
PING_GROUP_MENTION_RE = re.compile(r"<!(channel|here|everyone)>|<!subteam\^")


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

    # Skip message subtypes (edits, deletes, etc.) and bot messages.
    if event.get("subtype"):
        return
    if event.get("bot_id"):
        return

    # Hardcoded: never respond in this channel, no matter what. The agent
    # doesn't even start.
    if context.channel_id == "C06QV2T1P4G":
        return

    # Mentions of the bot are owned by handle_app_mentioned (the app_mention event).
    # Without this guard, a mention inside an engaged thread is picked up here too,
    # launching a second coolton alongside the one from app_mention.
    text = event.get("text", "")
    bot_id = os.environ.get("COOLTON_BOT_ID", "")
    if bot_id and f"<@{bot_id}>" in text:
        return

    # ## double-hash: never process or respond to a message starting with "##",
    # not even commands like !stop. Checked before !stop so "## !stop" is
    # ignored rather than halting runs.
    if text.strip().startswith("##"):
        logger.info(f"Ignoring message starting with '##': {text}")
        return

    channel_id = context.channel_id
    thread_ts = event.get("thread_ts") or event["ts"]
    user_id = context.user_id
    is_dm = event.get("channel_type") == "im"

    # !stop: immediately halt every coolton run in this thread. Only honored
    # when the bot is explicitly @-mentioned (that path lives in
    # handle_app_mentioned) or in a DM, where every message is directed at the
    # bot. A bare "!stop" in a channel thread without a mention is ignored —
    # it must never kill running coolton instances on its own.
    if "!stop" in text:
        if not is_dm:
            logger.info("Ignoring '!stop' without an @mention")
        else:
            request_stop(channel_id, thread_ts)
            say(
                text="⏹️ stopping all your running coolton instances…",
                thread_ts=thread_ts,
            )
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

    # Top-level channel messages are handled by app_mentioned.
    if not is_dm and not event.get("thread_ts"):
        return

    # Channel thread replies are handled only while the bot is joined (engaged)
    # in the thread. A mid-thread mention answers once but does not join, so a
    # non-mentioned reply here means we aren't joined and should be ignored.
    if not is_thread_engaged(channel_id, thread_ts, is_dm):
        logger.info(f"Ignoring message in unjoined thread {thread_ts} ({channel_id})")
        return

    # The opt-in prompt only fires for messages the bot is actually meant to
    # answer (DMs and engaged threads), so coolton never barges into a channel
    # or thread it wasn't part of just to ask for consent.
    from agent.policy_consent import (
        build_opt_in_blocks, has_consent, record_consent, save_pending,
        user_is_in_policy_channel,
    )
    if user_is_in_policy_channel(client, user_id):
        record_consent(user_id, joined_policy_channel=True)
    elif not has_consent(user_id):
        pending_id = save_pending({
            "user_id": user_id, "channel_id": channel_id, "thread_ts": thread_ts,
            "message_ts": event["ts"], "text": text,
            "user_token": context.user_token if isinstance(context.user_token, str) else None, "files": event.get("files"),
        })
        say(text="you need to opt in to the Coolton policy:",
            blocks=build_opt_in_blocks(pending_id), thread_ts=thread_ts)
        return

    try:
        text = event.get("text", "")

        # Get conversation history
        history = conversation_store.get_history(channel_id, thread_ts)

        from agent.tools.vision import download_attached_images
        images = download_attached_images(client, event.get("files"))

        from agent import AgentDeps
        deps = AgentDeps(
            client=client,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            message_ts=event["ts"],
            user_token=context.user_token,
        )

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
        logger.exception(f"Failed to handle message: {e}")
        from agent.redact import redact as _redact
        from agent.plan_block import set_plan_error
        try:
            if 'deps' in locals():
                set_plan_error(deps, _redact(str(e)))
        except Exception:
            pass
        say(
            text=f":warning: Something went wrong! ({type(e).__name__}: {_redact(str(e))})",
            thread_ts=event.get("thread_ts") or event.get("ts"),
        )
