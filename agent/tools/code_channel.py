"""Create a Slack "code channel" and turn it into its own single coolton
conversation. See agent.code_channel_store for the (channel_id, thread_ts="")
conversation-identity trick this relies on.

Code channels are created with Slack's documented agents.conversations.create by a
separate Slack app (its bot token in SLACK_CODE_CHANNEL_BOT_TOKEN; manifest in
manifests/code_channel_app.json), since coolton's own app can't get the
code_channels:manage scope. That app's bot becomes the channel's agent, and it invites
coolton's bot and cooltonUser so coolton can work there.

Linking the message the request came from (an "origin") makes Slack add its author
to the channel, give the channel the origin's privacy (or private, when asked), and
put a join card ("Started a session with ... in #channel") on that message, which
the origin channel's members can join a private channel from. Slack also posts a
"Context from #origin" message in the new channel. Slack only accepts an origin the
code channel app's bot can see, so the first time coolton links one in a channel,
her bot invites that bot in. Slack refuses an origin in a DM, a group DM or a Slack
Connect channel, and bots can't post the join card themselves, so there the channel
is created without one (its workspace given explicitly, since coolton is an org-wide
install, and private when the conversation is), and the requester is invited too.
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
    "missing_scope": "the code channel app is missing a scope (code_channels:manage, or the invite scopes); its "
                     "maintainer needs to fix that app's install.",
    "feature_disabled": "Slack hasn't enabled code channels for the code channel app on this workspace.",
    "user_not_enabled": "you don't have Slack's code channels feature enabled.",
    "restricted_action": "you're not allowed to create this kind of channel in this workspace.",
}


def _conversation(client, channel_id: str) -> dict:
    try:
        return client.conversations_info(channel=channel_id).get("channel") or {}
    except Exception:
        logger.exception("Couldn't look up %s", channel_id)
        return {}


def _team_id(channel: dict) -> str | None:
    """The workspace a conversation belongs to (needed without an origin, as coolton's
    token is org-wide)."""
    return channel.get("context_team_id") or (channel.get("shared_team_ids") or [None])[0]


def _add_code_channel_bot(client, channel_id: str) -> bool:
    """Invite the code channel app's bot to `channel_id` so Slack accepts an origin
    there. False if it can't be (a DM, or invites are restricted)."""
    from agent.mention_ids import CODE_CHANNEL_BOT_ID

    try:
        client.conversations_invite(channel=channel_id, users=CODE_CHANNEL_BOT_ID)
    except Exception as e:
        data = getattr(getattr(e, "response", None), "data", None) or {}
        if data.get("error") != "already_in_channel":
            logger.info("Couldn't add the code channel bot to %s: %s", channel_id, data.get("error") or e)
            return False
    return True


def _code_channel_app():
    from agent.code_channel_api import app_client

    return app_client()


def _create(app, params: dict) -> dict:
    try:
        return app.api_call("agents.conversations.create", json=params).data
    except Exception as e:
        data = getattr(getattr(e, "response", None), "data", None)
        return data if isinstance(data, dict) else {"ok": False, "error": str(e)}


def create_code_channel(
    client, name: str, task: str, owner_id: str,
    source_channel_id: str, source_thread_ts: str, source_message_ts: str = "",
    private: bool = False,
) -> str:
    """Create a code channel and, on success, schedule coolton picking up `task` there
    as its own conversation. Returns a message for the model to relay. `client` is
    coolton's own bot client; the channel is created by the code channel app.
    `private` makes it private; otherwise it has the origin's privacy."""
    app = _code_channel_app()
    if app is None:
        return "Error: code channels aren't set up (no SLACK_CODE_CHANNEL_BOT_TOKEN configured)."
    from agent.code_channel_store import expect_origin_context

    # The origin's privacy, made explicit so coolton can say what the channel is; if it
    # can't be looked up, Slack still matches a linked origin's privacy itself.
    source = _conversation(client, source_channel_id)
    source_private = any(source.get(k) for k in ("is_im", "is_mpim", "is_private")) if source else None
    params = {"name": name}
    if private or source_private is not None:
        params["is_private"] = bool(private or source_private)
    if source_message_ts:
        # The request's own message: retries of it return the same channel.
        params["session_id"] = f"coolton:{source_channel_id}:{source_message_ts}"
    linked = bool(source_channel_id and source_message_ts)
    response = None
    if linked:
        expect_origin_context(source_channel_id, source_message_ts)
        origin = {**params, "origin_channel_id": source_channel_id, "origin_message_ts": source_message_ts}
        response = _create(app, origin)
        if response.get("error") == "invalid_origin_link" and _add_code_channel_bot(client, source_channel_id):
            response = _create(app, origin)
    if response is None or (not response.get("ok") and response.get("error") in _NO_ORIGIN_ERRORS):
        linked = False
        params.setdefault("is_private", True)  # privacy unknown: don't expose it
        team_id = _team_id(source)
        response = _create(app, {**params, **({"team_id": team_id} if team_id else {})})

    if not response.get("ok"):
        error = response.get("error") or "unknown_error"
        return f"Could not create the code channel ({error}): {_ERROR_HELP.get(error, 'Slack refused it.')}"
    channel_id = response.get("channel_id") or ""
    if not channel_id:
        return "Error: Slack reported success but returned no channel id."
    # The code channel app's bot is the agent; coolton's bot and cooltonUser do the
    # work, and the requester is only added by Slack when the origin is linked.
    members = [os.environ.get("COOLTON_BOT_ID", ""), os.environ.get("COOLTON_USER_ID", "")]
    members = [u for u in members + ([] if linked else [owner_id]) if u]
    try:
        # force: invite everyone who can be, even if one of them can't.
        app.conversations_invite(channel=channel_id, users=",".join(members), force=True)
    except Exception as e:
        data = getattr(getattr(e, "response", None), "data", None) or {}
        if data.get("error") != "already_in_channel":
            logger.warning("Inviting %s to code channel %s: %s", members, channel_id, e)
            return (f"Created code channel <#{channel_id}>, but couldn't add coolton to it "
                    f"({data.get('error') or e}), so I can't work there.")

    from agent.code_channel_store import register_code_channel
    register_code_channel(channel_id, name, owner_id, source_channel_id, source_thread_ts)

    t = threading.Thread(
        target=_activate_code_channel,
        args=(client, channel_id, name, task, owner_id, source_channel_id, source_thread_ts),
        daemon=True,
    )
    t.start()

    privacy = {True: "private", False: "public"}.get(params.get("is_private"), "with the same privacy as this channel")
    joining = (" Slack put a join card on the request's message; people in this channel can join from it."
               if linked else f" <@{owner_id}> was added to it (there's no join card outside a channel).")
    return (
        f"Created code channel <#{channel_id}> ({privacy}).{joining} I'm picking up the work there in a "
        f"few seconds, so this thread doesn't need to continue it."
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

    # The handoff is addressed to her, so it normally ends in a reply (or skip(preserve=True)),
    # not skip(preserve=False), which would erase the turn as if it weren't.
    next_step = (
        f"Continue with: {task}" if task else
        "Usually that means carrying on with what the person asked for in it. If all they asked "
        "for was the channel, just say briefly that it's set up (text_only_response is fine) or "
        "skip(preserve=True)"
    )
    prompt = (
        f'[SYSTEM: you just created the code channel "{name}" and you\'re in it now. '
        "Nobody sent this message — it's the handoff. The history above is the thread "
        "this came from. This whole channel is ONE conversation: reply at channel level, "
        f"not in a thread. {next_step}. This handoff is addressed to you, so it shouldn't end "
        "in skip(preserve=False).]"
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
