"""A button on one of coolton's cards (agent.tools.json_render_card) was clicked: coolton
posts in the thread who clicked what, then answers the button's label, marked as a button
click, as that person's mention,
through her own mention handler, the way the code channels app hands over clicks in its
tabs (listeners.code_channel_app). The card's other inputs (selects, checkboxes...) never
ask anything themselves; their state goes along with the button's prompt.

What's asked is the label the person saw and clicked, never a hidden prompt: a card can be
made at anyone's request, and a button that says one thing and asks another would run that
other thing as whoever clicks it.
"""

from __future__ import annotations

import logging
import os
import threading

from slack_bolt import BoltContext, Say, SayStream

logger = logging.getLogger(__name__)


def _thread_of(body: dict) -> tuple[str, str]:
    """(channel, thread_ts) of the clicked card; thread_ts is "" for a top-level card."""
    message = body.get("message") or {}
    container = body.get("container") or {}
    channel_id = (body.get("channel") or {}).get("id") or container.get("channel_id") or ""
    return channel_id, message.get("thread_ts") or container.get("thread_ts") or ""


def handle_card_prompt(ack, body: dict, client) -> None:
    ack()
    threading.Thread(target=hand_click_to_coolton, args=(body, client), daemon=True, name="card-prompt").start()


def button_click_text(label: str) -> str:
    """What coolton is asked when someone clicks a card button labelled `label`."""
    return (f"{label}\n[from a button click: they clicked the \"{label}\" button on your card. "
            "This is the button's label, not a message they typed: do what the button offers.]")


def hand_click_to_coolton(body: dict, client) -> None:
    from agent.ban_store import is_banned
    from agent.block_state import with_state
    from agent.mentions import defuse_mass_mentions
    from listeners.events.app_mentioned import handle_app_mentioned

    action = (body.get("actions") or [{}])[0]
    label = ((action.get("text") or {}).get("text") or "").strip()
    user_id = (body.get("user") or {}).get("id", "")
    channel_id, thread_ts = _thread_of(body)
    if not label or not user_id or not channel_id or is_banned(user_id):
        return

    from agent.block_state import describe_state

    inputs = describe_state(body)
    text = with_state(button_click_text(label), body, "The card's")
    notice = f"<@{user_id}> clicked *{label}*" + "".join(f"\n> • {line}" for line in inputs)
    try:
        posted = client.chat_postMessage(channel=channel_id, thread_ts=thread_ts or None,
                                         text=defuse_mass_mentions(notice))
    except Exception:
        logger.exception("Couldn't post the card button notice in %s", channel_id)
        return

    # No "type": an event coolton builds herself, not one Slack delivered.
    event = {"channel": channel_id, "user": user_id, "ts": posted["ts"], "text": text,
             "team": (body.get("team") or {}).get("id")}
    if thread_ts:
        event["thread_ts"] = thread_ts
    if channel_id.startswith("D"):
        event["channel_type"] = "im"
    reply_ts = thread_ts or posted["ts"]
    context = BoltContext({"channel_id": channel_id, "user_id": user_id, "client": client,
                           "user_token": os.environ.get("SLACK_USER_TOKEN")})
    handle_app_mentioned(
        client=client, context=context, event=event, logger=logger,
        say=Say(client=client, channel=channel_id, thread_ts=reply_ts),
        say_stream=SayStream(client=client, channel=channel_id, thread_ts=reply_ts,
                             recipient_team_id=event.get("team"), recipient_user_id=user_id),
        set_status=None,
    )
