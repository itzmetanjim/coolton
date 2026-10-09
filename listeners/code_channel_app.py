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

As the agent, this app also gets what happens in its code channels: Slack's stop button
(agent_session_stopped, halting coolton's run there), its per-channel slash commands
(agent.tools.code_channel_tools.set_commands) and the Block Kit tabs' buttons and selects
(block_actions). Each command or interaction is posted in the channel by coolton, saying
who did what, and handed to coolton as that person's message.
"""
from __future__ import annotations

import logging
import os
import re
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


def handle_session_stopped(event: dict, coolton: WebClient) -> None:
    """Slack's stop button in a code channel: halt coolton's run there like !stop. Slack
    updates the session status itself."""
    from agent.active_runs import is_run_active
    from agent.code_channel_store import CODE_CHANNEL_THREAD_TS
    from agent.stop_store import request_stop

    channel_id = event.get("channel", "")
    thread_ts = event.get("thread_ts") or CODE_CHANNEL_THREAD_TS
    if not channel_id or not is_run_active(channel_id, thread_ts):
        return
    request_stop(channel_id, thread_ts)
    try:
        coolton.chat_postMessage(channel=channel_id, thread_ts=thread_ts or None, text="⏹️ stopping…")
    except Exception:
        logger.exception("Code channels app: couldn't confirm the stop in %s", channel_id)


def _hand_to_coolton(coolton: WebClient, app_client: WebClient, channel_id: str, user_id: str,
                     notice: str, text: str, team_id: str | None = None) -> None:
    """Post `notice` in the channel as coolton, then answer `text` as `user_id`'s message there."""
    try:
        ts = coolton.chat_postMessage(channel=channel_id, text=notice)["ts"]
    except Exception:
        logger.exception("Code channels app: couldn't post in %s", channel_id)
        return
    handle_code_channel_app_mention(
        {"channel": channel_id, "user": user_id, "ts": ts, "text": text, "team": team_id},
        app_client, coolton,
    )


def handle_command(body: dict, app_client: WebClient, coolton: WebClient) -> None:
    """Someone ran one of coolton's slash commands in a code channel."""
    command, text = body.get("command", ""), (body.get("text") or "").strip()
    channel_id, user_id = body.get("channel_id", ""), body.get("user_id", "")
    if not channel_id or not user_id:
        return
    invocation = f"{command} {text}".strip()
    _hand_to_coolton(coolton, app_client, channel_id, user_id, f"<@{user_id}> ran `{invocation}`",
                     invocation, body.get("team_id"))


def _describe_action(action: dict) -> str:
    what = action.get("type", "action")
    label = ((action.get("text") or {}).get("text")) or action.get("action_id", "")
    value = (action.get("value") or (action.get("selected_option") or {}).get("value")
             or ", ".join(o.get("value", "") for o in action.get("selected_options") or [])
             or action.get("selected_date") or action.get("selected_user") or "")
    return f"{what} \"{label}\"" + (f" (value: {value})" if value else "") + f" [action_id: {action.get('action_id')}]"


def handle_block_action(body: dict, app_client: WebClient, coolton: WebClient) -> None:
    """Someone used a button or select in one of coolton's Block Kit tabs."""
    channel_id = ((body.get("channel") or {}).get("id") or (body.get("container") or {}).get("channel_id") or "")
    user_id = (body.get("user") or {}).get("id", "")
    actions = body.get("actions") or []
    if not channel_id or not user_id or not actions:
        logger.info("Code channels app: block action without a channel or user: %s", list(body))
        return
    described = "; ".join(_describe_action(a) for a in actions)
    _hand_to_coolton(coolton, app_client, channel_id, user_id, f"<@{user_id}> used {described} in a tab",
                     f"[used {described} in one of your Block Kit tabs]", (body.get("team") or {}).get("id"))


def handle_context_bar_action(event: dict, app_client: WebClient, coolton: WebClient) -> None:
    """Someone clicked an "action" item in a code channel's context bar. Slack documents
    this event but its manifest validator doesn't accept a subscription to it yet, so it
    may never arrive; handled in case Slack delivers it anyway."""
    channel_id = event.get("channel") or event.get("channel_id") or ""
    user_id = event.get("user") or event.get("user_id") or ""
    key = event.get("key") or (event.get("item") or {}).get("key") or "?"
    if channel_id and user_id:
        _hand_to_coolton(coolton, app_client, channel_id, user_id,
                         f"<@{user_id}> clicked `{key}` in the context bar",
                         f"[clicked the context bar item {key}]", event.get("team"))


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

    @app.event("agent_session_stopped")
    def _on_stop(event):
        handle_session_stopped(event, coolton)

    @app.event("code_channel_action")
    def _on_context_bar_action(event, client):
        handle_context_bar_action(event, client, coolton)

    @app.command(re.compile(r".*"))
    def _on_command(ack, body, client):
        ack()
        threading.Thread(target=handle_command, args=(body, client, coolton), daemon=True).start()

    @app.action(re.compile(r".*"))
    def _on_action(ack, body, client):
        ack()
        threading.Thread(target=handle_block_action, args=(body, client, coolton), daemon=True).start()

    def _run():
        try:
            SocketModeHandler(app, app_token).start()
        except Exception:
            logger.exception("Code channels app: Socket Mode connection failed")

    threading.Thread(target=_run, daemon=True, name="code-channel-app").start()
    logger.info("Code channels app: listening for mentions")
    return True
