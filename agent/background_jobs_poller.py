"""Notice a run_background_command job finishing and let the agent react to
it — coolton's equivalent of Claude Code surfacing a background task's
completion instead of making you poll BashOutput yourself.

Runs as a periodic APScheduler job (see agent.scheduler.start_scheduler).
Each tick, for every job agent.background_jobs_store still knows about:
  - still running: nothing to do (checking it is itself sandbox activity,
    which re-arms the keepalive — see sandbox_background.get_job_status).
  - finished: notify. If a turn is already active on that thread, fold it in
    as a steering message (agent.steering_store) exactly the way a real
    mid-run message from a person does. If nothing is active, start a fresh
    turn — the Slack "scheduled task fires" pattern (agent.scheduler) for a
    Slack thread, or web.runner.wake_conversation for a web conversation.
"""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

from slack_sdk import WebClient

logger = logging.getLogger(__name__)

_POLL_TAIL_LINES = 100

# A background job nobody has checked on this long is presumed abandoned —
# stop paying to keep its sandbox warm and stop polling it. Long enough for
# a real build/install to finish, short enough that a forgotten `sleep`
# doesn't bill a sandbox forever.
_MAX_JOB_AGE_SECONDS = 2 * 60 * 60

_wake_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bg-job-wake")


def poll_background_jobs() -> None:
    from agent.background_jobs_store import list_jobs

    for job in list_jobs():
        try:
            _poll_one(job)
        except Exception:
            logger.exception("Failed polling background job %s", job.get("job_id"))


def _poll_one(job: dict) -> None:
    from agent.background_jobs_store import unregister_job

    job_id = job["job_id"]
    channel_id = job["channel_id"]
    thread_ts = job["thread_ts"]

    if time.time() - job.get("started_at", 0) > _MAX_JOB_AGE_SECONDS:
        logger.warning("Background job %s abandoned after %ds; no longer tracking it", job_id, _MAX_JOB_AGE_SECONDS)
        unregister_job(job_id)
        return

    from agent.tools.sandbox_background import get_job_status
    result = get_job_status(channel_id, thread_ts, job_id, tail_lines=_POLL_TAIL_LINES)
    if isinstance(result, str):
        # Sandbox connect error, or the job id vanished (UNKNOWN, already
        # unregistered by get_job_status itself) — nothing more to do here.
        logger.info("Background job %s: %s", job_id, result)
        return

    status, output = result
    if status != "RUNNING":
        _notify_finished(job, output)


def _notify_finished(job: dict, output: str) -> None:
    from agent.active_runs import is_run_active
    from agent.ban_store import is_banned
    from agent.steering_store import queue_steering_message

    channel_id = job["channel_id"]
    thread_ts = job["thread_ts"]
    job_id = job["job_id"]
    command = job.get("command", "")
    user_id = job.get("user_id", "")

    # Same rule the ban-bypass fix applies to every other steering/fresh-turn
    # path (listeners.events.message, .app_mentioned, web.runner,
    # agent.scheduler._fire_task): a banned user's job finishing must not
    # steer or start a turn under their identity.
    if user_id and is_banned(user_id):
        logger.info("Background job %s: owner %s is banned; not notifying", job_id, user_id)
        return

    text = (
        f"[SYSTEM: background job `{job_id}` (`{command}`) finished. Last output:]\n\n{output}"
    )

    if is_run_active(channel_id, thread_ts):
        # Same mechanism a real mid-run message from a person uses — the live
        # run picks this up and factors it in on its next tool call (see
        # agent.plan_block._steering_note). user_id is deliberately blank:
        # this isn't a message "from" whoever started the job, and
        # _steering_note would otherwise flag it as being from a different
        # person than the one running the turn.
        queue_steering_message(channel_id, thread_ts, text, user_id="", message_ts="")
        return

    _wake_executor.submit(_wake, channel_id, thread_ts, user_id, job_id, command, output)


def _wake(channel_id: str, thread_ts: str, user_id: str, job_id: str, command: str, output: str) -> None:
    banner = f":gear: *Background job finished:* `{command}`"
    prompt = (
        f"[SYSTEM: your background job `{job_id}` (`{command}`) finished while you "
        f"weren't running a turn. Last output:]\n\n{output}"
    )

    from web.runner import WEB_CHANNEL_ID
    if channel_id == WEB_CHANNEL_ID:
        _wake_web(thread_ts, user_id, banner, prompt)
    else:
        _wake_slack(channel_id, thread_ts, user_id, banner, prompt)


def _wake_web(conversation_id: str, user_id: str, banner: str, prompt: str) -> None:
    from web.runner import wake_conversation
    try:
        wake_conversation(conversation_id, user_id, banner, prompt)
    except Exception:
        logger.exception("Failed to wake web conversation %s for a finished background job", conversation_id)


def _wake_slack(channel_id: str, thread_ts: str, user_id: str, banner: str, prompt: str) -> None:
    from slack_bolt import Say, SayStream

    from listeners.events.turn import run_agent_turn
    from thread_context import conversation_store

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        logger.error("Background job wake-up: SLACK_BOT_TOKEN not set")
        return
    client = WebClient(token=bot_token)
    try:
        response = client.chat_postMessage(channel=channel_id, text=banner, thread_ts=thread_ts or None)
    except Exception:
        logger.exception("Background job wake-up: failed to post banner to %s", channel_id)
        return

    message_ts: str = str(response["ts"])
    turn_thread_ts: str = thread_ts or message_ts
    try:
        run_agent_turn(
            client=client, say=Say(client=client, channel=channel_id, thread_ts=turn_thread_ts),
            say_stream=SayStream(client=client, channel=channel_id, thread_ts=turn_thread_ts),
            logger=logger, channel_id=channel_id, thread_ts=turn_thread_ts, message_ts=message_ts,
            user_id=user_id, user_token=os.environ.get("SLACK_USER_TOKEN"),
            text=prompt, history=conversation_store.get_history(channel_id, turn_thread_ts),
        )
    except Exception:
        logger.exception("Background job wake-up turn failed for %s/%s", channel_id, thread_ts)
