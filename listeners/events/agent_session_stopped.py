from logging import Logger

from slack_sdk import WebClient

from agent.active_runs import is_run_active
from agent.ban_store import is_banned
from agent.stop_store import request_stop
import agent.thread_status as thread_status


def handle_agent_session_stopped(client: WebClient, event: dict, logger: Logger):
    """Someone clicked Slack's stop button on a coolton agent session (shown while
    agent.thread_status has it in `processing`). Halts the thread's run exactly like
    `!stop` (the run stops before its next tool call), confirms in the thread, and takes
    the session out of `processing`, which Slack leaves to the app."""
    channel_id, thread_ts = event.get("channel", ""), event.get("thread_ts", "")
    if not channel_id or not thread_ts or is_banned(event.get("user", "")):
        return
    try:
        if is_run_active(channel_id, thread_ts):
            request_stop(channel_id, thread_ts)
            client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text="⏹️ stopping…")
    except Exception:
        logger.exception("agent_session_stopped: couldn't stop the run")
    thread_status.end_session_now(client, channel_id, thread_ts)
