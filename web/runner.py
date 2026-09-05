"""Runs web UI turns on a background thread pool, decoupled from any HTTP
connection — the point of coolton being able to keep working after someone
closes the tab. Reuses the exact same turn pipeline Slack uses
(listeners.events.turn.run_agent_turn) and the exact same active-run/steering
mechanism (agent.active_runs / agent.steering_store), just triggered by a REST
POST instead of a Slack event — see listeners/events/message.py for the Slack
side of this same logic.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor

from slack_sdk import WebClient

from agent.active_runs import is_run_active
from agent.steering_store import queue_steering_message
from agent.surfaces.web import WebSurface
from thread_context import conversation_store

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="web-turn")
_bot_client: WebClient | None = None

# Web conversations share the Slack-shaped thread stores (active_runs,
# steering_store, conversation_store, sandboxes...) under this fixed
# channel_id — Slack channel ids always start with C/D/G, so "web" can never
# collide with a real one.
WEB_CHANNEL_ID = "web"


def _client() -> WebClient:
    global _bot_client
    if _bot_client is None:
        _bot_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))
    return _bot_client


def submit_message(conversation_id: str, user_id: str, text: str, attachments: list[dict] | None = None) -> None:
    """Record the message and either fold it into the run already in flight
    (steering — same rule Slack follows: don't race a second turn alongside a
    live one) or start a fresh turn on the executor."""
    from agent.ban_store import is_banned
    from web import conversation_log as log

    attachments = attachments or []
    user_event = log.append_event(conversation_id, {
        "type": "user_message", "text": text, "user_id": user_id, "attachments": attachments,
    })

    # A banned user's message is recorded (so it isn't silently swallowed —
    # the person can see they sent it) but never starts or steers a turn, on
    # Slack or here. Checked here, before the steering branch below, because
    # listeners.events.turn.run_agent_turn's own is_banned() check (the other
    # half of this fix) only guards a fresh turn — a message folded into an
    # already-running one via queue_steering_message never reaches it.
    if is_banned(user_id):
        log.append_event(conversation_id, {
            "type": "turn_end", "state": "error", "reason": "you're banned from using coolton.",
        })
        return

    # Name the conversation after the message that opened it, so the sidebar
    # isn't a column of identical placeholders. Only ever fills a blank name —
    # a title the user set by hand, or one already derived, is never overwritten.
    meta = log.get_conversation_meta(conversation_id) or {}
    if not (meta.get("title") or "").strip():
        title = log.title_from_message(text)
        if title:
            log.set_title(conversation_id, title)

    if is_run_active(WEB_CHANNEL_ID, conversation_id):
        logger.info("Steering: queuing message into active web run %s", conversation_id)
        queue_steering_message(WEB_CHANNEL_ID, conversation_id, text, user_id, str(user_event["seq"]))
        log.append_event(conversation_id, {
            "type": "reaction", "op": "add", "emoji": "white_check_mark", "target_seq": user_event["seq"],
        })
        return

    _executor.submit(_run_turn, conversation_id, user_id, text, user_event["seq"], attachments)


def _run_turn(conversation_id: str, user_id: str, text: str, message_seq: int, attachments: list[dict]) -> None:
    from listeners.events.turn import run_agent_turn
    from web import conversation_log as log

    surface = WebSurface(conversation_id, user_id)
    surface.set_target_message(message_seq)
    log.append_event(conversation_id, {"type": "turn_start"})

    images = None
    if attachments:
        image_attachments = [a for a in attachments if (a.get("media_type") or "").startswith("image/")]
        if image_attachments:
            images = []
            for a in image_attachments:
                try:
                    path = os.path.join("web_attachments", a["id"])
                    with open(path, "rb") as f:
                        images.append({"data": f.read(), "media_type": a["media_type"], "name": a.get("name", a["id"])})
                except OSError:
                    logger.warning("Attachment %s missing on disk for conversation %s", a.get("id"), conversation_id)

    try:
        run_agent_turn(
            client=_client(),
            surface=surface,
            logger=logger,
            channel_id=WEB_CHANNEL_ID,
            thread_ts=conversation_id,
            message_ts=str(message_seq),
            user_id=user_id,
            user_token=os.environ.get("SLACK_USER_TOKEN"),
            text=text,
            history=conversation_store.get_history(WEB_CHANNEL_ID, conversation_id),
            images=images,
        )
    except Exception:
        # run_agent_turn already catches and reports its own errors (via
        # surface.post_error, which ends the turn) — this is a last-resort
        # backstop for something going wrong outside that, so a crash here can
        # never leave the conversation's turn_status stuck on "Working" forever.
        logger.exception("Unhandled error running web turn for conversation %s", conversation_id)
        try:
            surface.post_error("Internal error running this turn.")
        except Exception:
            logger.exception("Failed to report the internal error itself to conversation %s", conversation_id)
