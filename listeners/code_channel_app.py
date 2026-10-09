"""The "coolton code channels" app's own event listener.

That app (manifests/code_channel_app.json) is the agent of every code channel: Slack
makes it the agent of channels coolton creates (agent.tools.code_channel) and of ones
people start from Slack's own code channel UI, whose first message is a mention of it.
coolton's bot isn't in those UI-made channels, so this app listens for its own
app_mention over Socket Mode (SLACK_CODE_CHANNEL_APP_TOKEN), and for each one:
adds coolton's bot and cooltonUser to the channel, registers a code channel coolton
doesn't know yet as one (agent.code_channel_store), and hands the event to coolton's
own mention handler, text unchanged (the prompt tells coolton this bot is hers),
answered once (agent.mention_ids).
"""
from __future__ import annotations

import logging
import os
import threading

from slack_bolt import App, BoltContext, Say, SayStream
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

logger = logging.getLogger(__name__)


def _add_coolton(app_client: WebClient, channel_id: str) -> None:
    users = [u for u in (os.environ.get("COOLTON_BOT_ID"), os.environ.get("COOLTON_USER_ID")) if u]
    try:
        app_client.conversations_invite(channel=channel_id, users=",".join(users), force=True)
    except Exception as e:
        data = getattr(getattr(e, "response", None), "data", None) or {}
        if data.get("error") != "already_in_channel":
            logger.warning("Code channels app: couldn't add coolton to %s: %s", channel_id, e)


def _register_if_code_channel(coolton: WebClient, channel_id: str, owner_id: str) -> None:
    from agent.code_channel_store import is_code_channel, register_code_channel

    if is_code_channel(channel_id):
        return
    try:
        channel = coolton.conversations_info(channel=channel_id).get("channel") or {}
    except Exception:
        logger.exception("Code channels app: couldn't look up %s", channel_id)
        return
    record = ((channel.get("properties") or {}).get("record_channel") or {}).get("record_type")
    if record == "agent_channel":
        register_code_channel(channel_id, channel.get("name") or "", owner_id, channel_id, "")


def handle_code_channel_app_mention(event: dict, app_client: WebClient, coolton: WebClient) -> None:
    """A mention of the code channels bot, answered by coolton as a mention of her."""
    if event.get("bot_id") or event.get("app_id"):
        return
    channel_id, user_id = event.get("channel", ""), event.get("user", "")
    if not channel_id or not user_id:
        return
    _add_coolton(app_client, channel_id)
    _register_if_code_channel(coolton, channel_id, user_id)

    from listeners.events.app_mentioned import handle_app_mentioned

    thread_ts = event.get("thread_ts") or event.get("ts")
    context = BoltContext({"channel_id": channel_id, "user_id": user_id, "client": coolton,
                           "user_token": os.environ.get("SLACK_USER_TOKEN")})
    handle_app_mentioned(
        client=coolton, context=context, event=event, logger=logger,
        say=Say(client=coolton, channel=channel_id, thread_ts=thread_ts),
        say_stream=SayStream(client=coolton, channel=channel_id, thread_ts=thread_ts,
                             recipient_team_id=event.get("team"), recipient_user_id=user_id),
        set_status=None,
    )


def start_code_channel_app(coolton: WebClient) -> bool:
    """Connect the code channels app over Socket Mode in the background. Returns
    whether it started (it needs both of its tokens)."""
    bot_token = os.environ.get("SLACK_CODE_CHANNEL_BOT_TOKEN")
    app_token = os.environ.get("SLACK_CODE_CHANNEL_APP_TOKEN")
    if not bot_token or not app_token:
        logger.info("Code channels app: not listening (SLACK_CODE_CHANNEL_BOT_TOKEN/APP_TOKEN not set)")
        return False
    app = App(token=bot_token)

    @app.event("app_mention")
    def _on_mention(event, client):
        handle_code_channel_app_mention(event, client, coolton)

    @app.event("message")
    def _ignore_messages():
        pass

    def _run():
        try:
            SocketModeHandler(app, app_token).start()
        except Exception:
            logger.exception("Code channels app: Socket Mode connection failed")

    threading.Thread(target=_run, daemon=True, name="code-channel-app").start()
    logger.info("Code channels app: listening for mentions")
    return True
