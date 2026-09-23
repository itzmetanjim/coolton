import os
from logging import Logger

from slack_bolt import BoltContext, Say, SayStream
from slack_sdk import WebClient

from agent.active_runs import is_run_active
from agent.ban_store import apply_ban_command, is_authorized, parse_ban_command
from agent.code_channel_store import CODE_CHANNEL_THREAD_TS, is_code_channel
from agent.ensure_coolton_user import ensure_coolton_user_in_channel
from agent.leave_thread_store import join_thread
from agent.steering_store import queue_steering_message
from agent.stop_store import is_stop_command, request_stop
from agent.stopped_threads_store import is_thread_stopped, mark_thread_stopped
from thread_context import conversation_store
from listeners.events.message import PING_GROUP_MENTION_RE
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

        # Banned users get nothing at all — not even !stop or a steering
        # fold-in into someone else's active run (both live further down this
        # function). This must run before either of those, since neither path
        # reaches run_agent_turn (listeners.events.turn), which does its own
        # is_banned() check but only guards a real turn, not a steer.
        from agent.ban_store import is_banned
        if is_banned(context.user_id):
            return

        text = event.get("text", "")
        if text.strip().startswith("##"):
            logger.info(f"Ignoring message starting with '##': {text}")
            return

        # A mention at a code channel's channel level (not inside a thread)
        # is part of that channel's single whole-channel conversation — see
        # agent.code_channel_store. A mention inside a thread within a code
        # channel is a normal, separate thread conversation and takes neither
        # branch here.
        at_channel_level = (
            event.get("channel_type") != "im"
            and not event.get("thread_ts")
            and is_code_channel(channel_id)
        )
        thread_ts = CODE_CHANNEL_THREAD_TS if at_channel_level else (event.get("thread_ts") or event["ts"])
        user_id = context.user_id
        bot_id = os.environ.get("COOLTON_BOT_ID", "")

        # RFC i rule 2: a thread stopped with `@coolton !stop` is ignored
        # entirely — every later message here is dropped without a reply,
        # mentioned or not, for the lifetime of the process (see
        # agent.stopped_threads_store). Checked before command parsing so a
        # second !stop can't re-trigger the ack either.
        if is_thread_stopped(channel_id, event.get("thread_ts")):
            logger.info(f"Ignoring message in stopped thread {thread_ts} ({channel_id})")
            return

        # RFC i rule 3 (defense in depth): a ping-group / broadcast mention
        # without the bot's own direct mention is not addressed to us. The
        # app_mention event is only supposed to fire on a direct mention, so
        # this guards odd API edge cases (workflows, replayed payloads) rather
        # than normal traffic.
        if bot_id and PING_GROUP_MENTION_RE.search(text) and f"<@{bot_id}>" not in text:
            logger.info(f"Ignoring ping-group mention without direct bot mention: {text[:80]}")
            return

        # !stop: immediately halt every coolton run in this thread. Must be the
        # message's entire content aside from the mention (see is_stop_command) —
        # a normal prompt that merely contains the word "!stop" must not halt
        # anything.
        if is_stop_command(text, bot_id):
            request_stop(channel_id, thread_ts)
            # RFC i rule 2: beyond halting in-flight runs, the thread itself is
            # now stopped — ALL later messages in it are ignored (see above).
            mark_thread_stopped(channel_id, event.get("thread_ts"))
            say(
                text="⏹️ stopping — coolton will ignore this thread from here on.",
                thread_ts=thread_ts,
            )
            return

        # !ban / !unban: silently ignored (not even a "not authorized" reply)
        # for anyone but ban_store.BAN_ADMIN_USER_ID, so the mechanism isn't
        # exposed to random users typing the syntax.
        ban_command = parse_ban_command(text, bot_id)
        if ban_command:
            if not is_authorized(user_id):
                return
            action, target_user_id, reason = ban_command
            apply_ban_command(client, action, target_user_id, reason)
            try:
                client.reactions_add(channel=channel_id, timestamp=event["ts"], name="white_check_mark")
            except Exception:
                logger.exception("Failed to react to ban/unban command")
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
            logger.info(f"Steering: queuing mention into active run {channel_id}/{thread_ts}: {text[:200]}")
            queue_steering_message(channel_id, thread_ts, text, user_id, event["ts"])
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
        # but does NOT join — we only respond again when mentioned again. A
        # code channel's channel level has no thread to join — it already
        # responds to everything by definition.
        if not at_channel_level and event["ts"] == thread_ts:
            join_thread(channel_id, thread_ts)

        # The bot mention stays in the text verbatim — the model is taught to read
        # <@BOTID> as "@coolton". A bare, contentless mention ("@coolton" with nothing
        # else) still runs a real turn rather than a hardcoded canned reply — with
        # thread history prefilled (below) the model can respond to what's actually
        # going on instead of a generic greeting, and without any history it still
        # generates a natural one itself from the system prompt/personality.

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
