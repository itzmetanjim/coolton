"""Who gets a canvas coolton just created. A new canvas is shared with nobody, so without
this it can't be opened by the people it's for, or by coolton from anywhere else.

Coolton (bot and cooltonUser) and the conversation it was made in can edit it. Unless it's
private, the channels in CANVAS_SHARE_CHANNEL_IDS can read it too.
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

_API = "https://slack.com/api"


def _call(method: str, token: str, payload: dict) -> dict:
    try:
        response = requests.post(
            f"{_API}/{method}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
            json=payload,
            timeout=15,
        )
        return response.json()
    except Exception as e:
        return {"ok": False, "error": f"request_failed: {type(e).__name__}"}


def _bot_user_id() -> str:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    return (_call("auth.test", token, {}).get("user_id") or "") if token else ""


def _grant(token: str, canvas_id: str, level: str, ignored_failures: set[str], **target) -> list[str]:
    """Grant access to channel_ids or user_ids (Slack takes one kind per call). Returns problems."""
    result = _call("canvases.access.set", token, {"canvas_id": canvas_id, "access_level": level, **target})
    if not result.get("ok"):
        return [f"{', '.join(next(iter(target.values())))}: {result.get('error', 'unknown')}"]
    # Slack lists the canvas's owner here when asked to change the owner's own access.
    failed = [
        item
        for key in ("failed_to_update_channel_ids", "failed_to_update_user_ids")
        for item in result.get(key) or []
        if item not in ignored_failures
    ]
    return [f"{item}: not updated" for item in failed]


def share_new_canvas(canvas_id: str, channel_id: str, requester_id: str, private: bool = False) -> list[str]:
    """Share a new canvas. Returns what couldn't be shared; never raises."""
    token = os.environ.get("SLACK_USER_TOKEN", "")
    if not token:
        return ["SLACK_USER_TOKEN isn't configured"]

    coolton_user = os.environ.get("COOLTON_USER_ID", "")
    users = {coolton_user, _bot_user_id()}
    problems: list[str] = []

    if channel_id[:1] in ("C", "G"):
        problems += _grant(token, canvas_id, "write", set(), channel_ids=[channel_id])
    else:  # A DM (or the web UI), where the person is who it's shared with.
        users.add(requester_id)

    user_ids = sorted(u for u in users if u)
    if user_ids:
        problems += _grant(token, canvas_id, "write", {coolton_user}, user_ids=user_ids)

    if not private:
        extra = [c.strip() for c in os.environ.get("CANVAS_SHARE_CHANNEL_IDS", "").split(",") if c.strip()]
        # Not the current channel: it already has write access, and this would lower it.
        extra = [c for c in extra if c != channel_id]
        if extra:
            problems += _grant(token, canvas_id, "read", set(), channel_ids=extra)

    if problems:
        logger.warning("Canvas %s wasn't fully shared: %s", canvas_id, "; ".join(problems))
    return problems
