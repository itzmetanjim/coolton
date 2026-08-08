import os
import re
from logging import Logger

from slack_bolt import BoltContext, Say, SayStream
from slack_sdk import WebClient

from agent import AgentDeps, run_agent
from agent.leave_thread_store import is_thread_engaged
from agent.stop_store import request_stop
from thread_context import conversation_store
from listeners.views.feedback_builder import build_feedback_blocks

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

    channel_id = context.channel_id
    thread_ts = event.get("thread_ts") or event["ts"]
    user_id = context.user_id

    # !stop: immediately halt every coolton run in this thread.
    if "!stop" in text:
        request_stop(channel_id, thread_ts)
        say(
            text="⏹️ stopping all your running coolton instances…",
            thread_ts=thread_ts,
        )
        return

    # Messages starting with "<>" are only answered when the bot is explicitly
    # @-mentioned. Without a mention, ignore them even in engaged threads or
    # DMs (messages that DO mention the bot already return above and are owned
    # by handle_app_mentioned).
    if text.strip().startswith("<>"):
        logger.info(f"Ignoring '<>' message without explicit mention: {text[:80]}")
        return

    # Broadcast/user-group pings (@channel, @here, @everyone, named user
    # groups) are not addressed to the bot — a direct mention already returned
    # above. Don't process/respond to them.
    if PING_GROUP_MENTION_RE.search(text):
        logger.info(f"Ignoring ping-group mention without direct bot mention: {text[:80]}")
        return

    is_dm = event.get("channel_type") == "im"

    # Top-level channel messages are handled by app_mentioned.
    if not is_dm and not event.get("thread_ts"):
        return

    # Channel thread replies are handled only while the bot is joined (engaged)
    # in the thread. A mid-thread mention answers once but does not join, so a
    # non-mentioned reply here means we aren't joined and should be ignored.
    if not is_thread_engaged(channel_id, thread_ts, is_dm):
        logger.info(f"Ignoring message in unjoined thread {thread_ts} ({channel_id})")
        return

    try:
        text = event.get("text", "")
        if text.strip().startswith("##"):
            logger.info(f"Ignoring message starting with '##': {text}")
            return

        # Get conversation history
        history = conversation_store.get_history(channel_id, thread_ts)

        # Set assistant thread status with loading messages
        client.assistant_threads_setStatus(
            channel_id=channel_id,
            thread_ts=thread_ts,
            status="Thinking...",
            loading_messages=[
                "Teaching the hamsters to type faster…",
                "Untangling the internet cables…",
                "Consulting the office goldfish…",
                "Polishing up the response just for you…",
                "Convincing the AI to stop overthinking…",
            ],
        )

        # Run the agent
        deps = AgentDeps(
            client=client,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            message_ts=event["ts"],
            user_token=context.user_token,
        )

        from agent.plan_block import send_plan_message, finalize_plan_message, complete_plan_message, delete_plan_message
        plan_ts = send_plan_message(deps)
        deps.plan_ts = plan_ts

        result = run_agent(text, deps, message_history=history)

        if deps.should_skip:
            if plan_ts:
                delete_plan_message(deps)
        else:
            finalize_plan_message(deps, result.output)

            # Stream response in thread with feedback buttons
            streamer = say_stream()
            streamer.append(markdown_text=result.output)
            feedback_blocks = build_feedback_blocks()
            streamer.stop(blocks=feedback_blocks)
            complete_plan_message(deps)

        # Store conversation history
        conversation_store.set_history(channel_id, thread_ts, result.all_messages())

        # kevinton: silent background skill-capture agent (runs after every turn)
        if not deps.should_skip:
            from agent.kevinton import spawn_kevinton

            spawn_kevinton(text, result.all_messages(), channel_id, thread_ts, deps)

    except Exception as e:
        logger.exception(f"Failed to handle message: {e}")
        try:
            from agent.plan_block import set_plan_error
            set_plan_error(deps, str(e))
        except Exception:
            pass
        say(
            text=f":warning: Something went wrong! ({type(e).__name__}: {e})",
            thread_ts=event.get("thread_ts") or event.get("ts"),
        )
