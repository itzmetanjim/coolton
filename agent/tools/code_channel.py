"""Create a Slack "code channel" and turn it into its own single coolton
conversation. See agent.code_channel_store for the (channel_id, thread_ts="")
conversation-identity trick this relies on, and ./codechannel.sh for the
actual channel-creation call (deliberately kept in bash — do NOT reimplement
its logic here, and NEVER read or commit ./codechannelinternal.sh, which
holds tokens and is gitignored for exactly that reason).

This whole feature is explicitly cursed/buggy per the person who asked for
it — the tool wrapping this (create_code_channel_tool in agent.agent) is
documented to only ever fire on an explicit user request for a code channel.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time

from slack_sdk import WebClient

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SCRIPT_PATH = os.path.join(_REPO_ROOT, "codechannel.sh")

_ACTIVATE_DELAY_SECONDS = 3
_MAX_WAIT_FOR_ORIGIN_TURN_SECONDS = 10 * 60


def create_code_channel(
    client, name: str, task: str, owner_id: str,
    source_channel_id: str, source_thread_ts: str,
) -> str:
    """Create a code channel via ./codechannel.sh and, on success, schedule
    coolton joining it and picking up `task` there as its own conversation.

    Returns a message for the model to relay, or the script's own error text
    forwarded verbatim.
    """
    try:
        result = subprocess.run(
            [_SCRIPT_PATH, "a", name],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        return "Error: codechannel.sh not found."
    except subprocess.TimeoutExpired:
        return "Error: codechannel.sh timed out."
    except Exception as e:
        return f"Error running codechannel.sh: {e}"

    stdout = result.stdout or ""
    lines = stdout.split("\n", 1)
    status = lines[0].strip() if lines else ""
    rest = lines[1] if len(lines) > 1 else ""

    if status == "error":
        return f"Could not create the code channel: {rest.strip()}"

    if status != "ok":
        # Neither "ok" nor "error" on the first line — something unexpected
        # (missing internal script, non-zero exit, empty output). Surface the
        # raw output rather than silently swallowing it.
        detail = stdout.strip() or (result.stderr or "").strip() or f"exit code {result.returncode}"
        return f"Error: codechannel.sh gave an unexpected response: {detail}"

    channel_id = rest.strip().splitlines()[0].strip() if rest.strip() else ""
    if not channel_id:
        return "Error: codechannel.sh reported success but returned no channel id."

    from agent.code_channel_store import register_code_channel
    register_code_channel(channel_id, name, owner_id, source_channel_id, source_thread_ts)

    t = threading.Thread(
        target=_activate_code_channel,
        args=(client, channel_id, name, task, owner_id, source_channel_id, source_thread_ts),
        daemon=True,
    )
    t.start()

    return (
        f"Created code channel <#{channel_id}>. I'm joining it in a few seconds "
        f"and will pick up the work there — this thread doesn't need to continue it."
    )


def _delete_cooltonuser_auto_message(channel_id: str) -> None:
    """codechannelinternal.sh creates the channel using cooltonUser's own
    Slack user token, and Slack auto-posts a message "as cooltonUser" the
    moment a code channel is created (a Slack quirk, not anything
    codechannelinternal.sh itself asks for). Find it and delete it.

    Uses cooltonUser's own token (SLACK_USER_TOKEN) for both the lookup and
    the delete — chat.delete can only remove a message on behalf of the user
    who posted it (or a workspace admin), and the bot client this module
    otherwise uses is neither.
    """
    user_token = os.environ.get("SLACK_USER_TOKEN")
    coolton_user_id = os.environ.get("COOLTON_USER_ID", "")
    if not user_token or not coolton_user_id:
        return

    user_client = WebClient(token=user_token)
    try:
        resp = user_client.conversations_history(channel=channel_id, limit=200)
        messages = resp.get("messages", []) if resp.get("ok") else []
    except Exception:
        logger.exception("Failed to fetch history to find cooltonUser's auto message in %s", channel_id)
        return

    candidates = [m for m in messages if m.get("user") == coolton_user_id and m.get("ts")]
    if not candidates:
        return
    oldest = min(candidates, key=lambda m: float(m["ts"]))
    try:
        user_client.chat_delete(channel=channel_id, ts=oldest["ts"])
        logger.info("Deleted cooltonUser's auto-generated message in code channel %s (ts %s)", channel_id, oldest["ts"])
    except Exception:
        logger.exception("Failed to delete cooltonUser's auto message (ts %s) in %s", oldest["ts"], channel_id)


def _activate_code_channel(
    client, channel_id: str, name: str, task: str, owner_id: str,
    source_channel_id: str, source_thread_ts: str,
) -> None:
    time.sleep(_ACTIVATE_DELAY_SECONDS)

    try:
        client.conversations_join(channel=channel_id)
    except Exception as e:
        logger.info("conversations_join for code channel %s: %s", channel_id, e)

    _delete_cooltonuser_auto_message(channel_id)

    try:
        from agent.ensure_coolton_user import ensure_coolton_user_in_channel
        ensure_coolton_user_in_channel(client, channel_id)
    except Exception:
        logger.exception("ensure_coolton_user_in_channel failed for code channel %s", channel_id)

    from agent.ban_store import is_banned
    if is_banned(owner_id):
        logger.info("Code channel %s: owner %s is banned; not activating", channel_id, owner_id)
        return

    try:
        response = client.chat_postMessage(
            channel=channel_id,
            text=f":robot_face: coolton is in this code channel now — picking up: {task}" if task
            else ":robot_face: coolton is in this code channel now.",
        )
        message_ts = str(response["ts"])
    except Exception:
        logger.exception("Failed to post join banner in code channel %s", channel_id)
        return

    from agent.active_runs import is_run_active
    waited = 0.0
    while is_run_active(source_channel_id, source_thread_ts) and waited < _MAX_WAIT_FOR_ORIGIN_TURN_SECONDS:
        time.sleep(1)
        waited += 1

    from thread_context import conversation_store
    history = conversation_store.get_history(source_channel_id, source_thread_ts)

    prompt = (
        f'[SYSTEM: you just created the code channel "{name}" and you\'re in it now. '
        "Nobody sent this message — it's the handoff. The history above is the thread "
        "this came from. This whole channel is ONE conversation: reply at channel level, "
        f"not in a thread. Continue with: {task}]" if task else
        f'[SYSTEM: you just created the code channel "{name}" and you\'re in it now. '
        "Nobody sent this message — it's the handoff. The history above is the thread "
        "this came from. This whole channel is ONE conversation: reply at channel level, "
        "not in a thread.]"
    )

    try:
        from slack_bolt import Say, SayStream

        from agent.code_channel_store import CODE_CHANNEL_THREAD_TS
        from listeners.events.turn import run_agent_turn
        run_agent_turn(
            client=client,
            say=Say(client=client, channel=channel_id, thread_ts=None),
            say_stream=SayStream(client=client, channel=channel_id, thread_ts=None),
            logger=logger,
            channel_id=channel_id,
            thread_ts=CODE_CHANNEL_THREAD_TS,
            message_ts=message_ts,
            user_id=owner_id,
            user_token=os.environ.get("SLACK_USER_TOKEN"),
            text=prompt,
            history=history,
        )
    except Exception:
        logger.exception("Code channel activation turn failed for %s", channel_id)
