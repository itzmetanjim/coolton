import re
from logging import Logger

from slack_bolt import BoltContext, Say, SayStream
from slack_sdk import WebClient

from agent.active_runs import is_run_active
from agent.ensure_coolton_user import ensure_coolton_user_in_channel
from agent.leave_thread_store import join_thread
from agent.steering_store import queue_steering_message
from agent.stop_store import request_stop
from thread_context import conversation_store
from listeners.events.turn import run_agent_turn


def handle_app_mentioned(
    client: WebClient,
    context: BoltContext,
    event: dict,
    logger: Logger,
    say: Say,
    say_stream: SayStream,
    set_status,  # SetStatus — unused, we call API directly
):
    """Handle @mentions in channels."""
    try:
        channel_id = context.channel_id

        # Hardcoded: never respond in this channel, no matter what. The agent
        # doesn't even start.
        if channel_id == "C06QV2T1P4G":
            return

        # Never reply to bot messages, not even @mentions from other bots.
        if event.get("bot_id"):
            return

        text = event.get("text", "")
        if text.strip().startswith("##"):
            logger.info(f"Ignoring message starting with '##': {text}")
            return
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

        from agent.policy_consent import ensure_consent
        if not ensure_consent(
            client, say, user_id=user_id, channel_id=channel_id, thread_ts=thread_ts,
            message_ts=event["ts"],
        ):
            return

        # coolton is already working in this thread — steer the run already in
        # flight instead of racing a second one alongside it. See
        # agent/steering_store.py + plan_block.after_tool for how the running
        # turn picks this up.
        if is_run_active(channel_id, thread_ts):
            queue_steering_message(channel_id, thread_ts, text, user_id)
            try:
                client.reactions_add(channel=channel_id, timestamp=event["ts"], name="white_check_mark")
            except Exception:
                logger.exception("Failed to react to steering message")
            return

        # Silently make sure cooltonUser is a member of this channel (not in DMs).
        if event.get("channel_type") != "im":
            ensure_coolton_user_in_channel(client, channel_id)

        # A mention on the thread's starter message auto-joins the thread so we
        # respond to every subsequent message. A mid-thread mention answers once
        # but does NOT join — we only respond again when mentioned again.
        if event["ts"] == thread_ts:
            join_thread(channel_id, thread_ts)

        # The bot mention stays in the text verbatim — the model is taught to read
        # <@BOTID> as "@coolton". Only use a stripped copy to test for empty pings.
        has_content = re.sub(r"<@[A-Z0-9]+>", "", text).strip()

        if not has_content:
            say(
                text="Hey there! How can I help you? Ask me anything and I'll do my best.",
                thread_ts=thread_ts,
            )
            return

        # Get conversation history
        history = conversation_store.get_history(channel_id, thread_ts)

        # Mentioned in a thread we've never been part of: pull in the earlier
        # Slack messages so the model has the conversation's context.
        if history is None and event.get("thread_ts"):
            from thread_context.thread_history import build_thread_context

            history = build_thread_context(
                client, channel_id, thread_ts, exclude_ts=event["ts"]
            )

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
        # Note: run_agent_turn handles its own plan-block error reporting with its
        # real deps (see turn.py); an exception only lands here if it happened
        # before/around that call, when no plan block was ever sent.
        logger.exception(f"Failed to handle app mention: {e}")
        from agent.redact import redact as _redact
        say(
            text=f":warning: Something went wrong! ({type(e).__name__}: {_redact(str(e))})",
            thread_ts=event.get("thread_ts") or event["ts"],
        )
