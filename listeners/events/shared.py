"""What coolton's message and app_mention handlers both do before a turn."""

from agent.active_runs import is_run_active
from agent.ban_store import apply_ban_command, is_authorized, parse_ban_command
from agent.code_channel_store import is_expected_origin_context
from agent.steering_store import queue_steering_message
from agent.stop_store import request_stop

# Hardcoded: never respond in this channel, no matter what. The agent doesn't even start.
IGNORED_CHANNEL_ID = "C06QV2T1P4G"


def is_ignored(context, event: dict) -> bool:
    """Messages nothing in them can make coolton answer: in the ignored channel; from a bot
    or from an app posting with a user token (an app_id but no bot_id; cooltonUser's own
    posts too); Slack's "Context from" opener in a code channel coolton made, which quotes
    the request the handoff turn already covers (agent.code_channel_store); or from a
    banned user.

    A banned user gets nothing at all: not !stop and not a steer into someone else's
    active run, neither of which reaches run_agent_turn's own is_banned() check."""
    from agent.ban_store import is_banned

    return (
        context.channel_id == IGNORED_CHANNEL_ID
        or bool(event.get("bot_id") or event.get("app_id"))
        or is_expected_origin_context(event.get("text", ""))
        or is_banned(context.user_id)
    )


def _react_done(client, logger, channel_id: str, message_ts: str) -> None:
    try:
        client.reactions_add(channel=channel_id, timestamp=message_ts, name="white_check_mark")
    except Exception:
        logger.exception("Failed to react with a check mark")


def stop_thread(say, channel_id: str, thread_ts: str) -> None:
    """!stop: immediately halt every coolton run in this thread."""
    request_stop(channel_id, thread_ts)
    say(text="⏹️ stopping all your running coolton instances…", thread_ts=thread_ts)


def handled_ban_command(client, logger, text: str, bot_id: str, user_id: str, channel_id: str, message_ts: str) -> bool:
    """Apply a !ban / !unban, and say whether `text` was one. Silently ignored (not even a
    "not authorized" reply) for anyone but admin_alerts.ADMIN_USER_ID, so the mechanism
    isn't exposed to random users typing the syntax."""
    ban_command = parse_ban_command(text, bot_id)
    if not ban_command:
        return False
    if is_authorized(user_id):
        action, target_user_id, reason = ban_command
        apply_ban_command(client, action, target_user_id, reason)
        _react_done(client, logger, channel_id, message_ts)
    return True


def steered_into_active_run(client, logger, channel_id: str, thread_ts: str, text: str, user_id: str, message_ts: str) -> bool:
    """coolton is already working in this thread: steer the run in flight instead of racing
    a second one alongside it (agent/steering_store.py, plan_block.after_tool)."""
    if not is_run_active(channel_id, thread_ts):
        return False
    logger.info(f"Steering: queuing message into active run {channel_id}/{thread_ts}: {text[:200]}")
    queue_steering_message(channel_id, thread_ts, text, user_id, message_ts)
    _react_done(client, logger, channel_id, message_ts)
    return True


def report_failure(say, logger, event: dict, e: Exception) -> None:
    """run_agent_turn reports its own errors in its plan block (turn.py); this covers a
    failure before or around that call, when no plan block was ever sent."""
    from agent.redact import redact

    logger.exception(f"Failed to handle message: {e}")
    say(
        text=f":warning: Something went wrong! ({type(e).__name__}: {redact(str(e))})",
        thread_ts=event.get("thread_ts") or event["ts"],
    )
