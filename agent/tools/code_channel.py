"""Create a Slack "code channel" and turn it into its own single coolton
conversation. See agent.code_channel_store for the (channel_id, thread_ts="")
conversation-identity trick this relies on.

Code channels are created with Slack's documented agents.conversations.create
(bot token, code_channels:manage scope): coolton's bot becomes the channel's agent
and is added on creation. Linking the message the request came from (an "origin")
also gets its author added by Slack, gives the channel the origin's privacy, and
records the link on the channel. Slack refuses an origin in a DM, a group DM or a
Slack Connect channel, so there the channel is created without one (its workspace
given explicitly, since coolton is an org-wide install) and the requester is
invited by coolton instead.
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_ACTIVATE_DELAY_SECONDS = 3
_MAX_WAIT_FOR_ORIGIN_TURN_SECONDS = 10 * 60

# Errors meaning the origin message can't be linked; the channel is created without it.
_NO_ORIGIN_ERRORS = {"origin_channel_externally_shared", "invalid_origin_link", "origin_channel_is_file_channel"}
_ERROR_HELP = {
    "missing_scope": "coolton's Slack app doesn't have the code_channels:manage scope yet; its maintainer needs "
                     "to reinstall it with the updated manifest.",
    "feature_disabled": "Slack hasn't enabled code channels for coolton's app on this workspace.",
    "user_not_enabled": "you don't have Slack's code channels feature enabled.",
    "restricted_action": "you're not allowed to create this kind of channel in this workspace.",
}


def _team_id(client, channel_id: str) -> str | None:
    """The workspace `channel_id` belongs to (needed without an origin, as coolton's
    token is org-wide)."""
    try:
        channel = client.conversations_info(channel=channel_id).get("channel") or {}
    except Exception:
        logger.exception("Couldn't look up the workspace of %s", channel_id)
        return None
    return channel.get("context_team_id") or (channel.get("shared_team_ids") or [None])[0]


def _create(client, params: dict) -> dict:
    try:
        return client.api_call("agents.conversations.create", json=params).data
    except Exception as e:
        data = getattr(getattr(e, "response", None), "data", None)
        return data if isinstance(data, dict) else {"ok": False, "error": str(e)}


def create_code_channel(
    client, name: str, task: str, owner_id: str,
    source_channel_id: str, source_thread_ts: str, source_message_ts: str = "",
) -> str:
    """Create a code channel and, on success, schedule coolton picking up `task` there
    as its own conversation. Returns a message for the model to relay."""
    params = {"name": name}
    if source_message_ts:
        # The request's own message: retries of it return the same channel.
        params["session_id"] = f"coolton:{source_channel_id}:{source_message_ts}"
    linked = bool(source_channel_id and source_message_ts)
    response = _create(client, {**params, "origin_channel_id": source_channel_id,
                                "origin_message_ts": source_message_ts}) if linked else None
    if response is None or (not response.get("ok") and response.get("error") in _NO_ORIGIN_ERRORS):
        linked = False
        team_id = _team_id(client, source_channel_id)
        response = _create(client, {**params, **({"team_id": team_id} if team_id else {})})

    if not response.get("ok"):
        error = response.get("error") or "unknown_error"
        return f"Could not create the code channel ({error}): {_ERROR_HELP.get(error, 'Slack refused it.')}"
    channel_id = response.get("channel_id") or ""
    if not channel_id:
        return "Error: Slack reported success but returned no channel id."
    if not linked:
        try:
            client.conversations_invite(channel=channel_id, users=owner_id)
        except Exception as e:
            logger.info("Inviting %s to code channel %s: %s", owner_id, channel_id, e)

    from agent.code_channel_store import register_code_channel
    register_code_channel(channel_id, name, owner_id, source_channel_id, source_thread_ts)

    t = threading.Thread(
        target=_activate_code_channel,
        args=(client, channel_id, name, task, owner_id, source_channel_id, source_thread_ts),
        daemon=True,
    )
    t.start()

    return (
        f"Created code channel <#{channel_id}>. I'm picking up the work there in a few seconds, "
        f"so this thread doesn't need to continue it."
    )


def _activate_code_channel(
    client, channel_id: str, name: str, task: str, owner_id: str,
    source_channel_id: str, source_thread_ts: str,
) -> None:
    time.sleep(_ACTIVATE_DELAY_SECONDS)

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
