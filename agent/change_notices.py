"""Channel notices for channel changes coolton makes for someone.

When slack_api_call(_as_bot_tool) changes a channel (its topic, description or
name, a bookmark, a new channel, an invite), Slack's own system message credits
coolton. So after the change succeeds coolton posts a top-level message in that
channel, as cooltonUser, naming who asked for it, e.g. "The channel topic was
changed by <@U123>."
"""
from __future__ import annotations

import logging
import os

import requests

from agent.attribution import _attributable

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"

_CHANGES = {
    "conversations.setTopic": "The channel topic was changed by {who}.",
    "conversations.setPurpose": "The channel description was changed by {who}.",
    "conversations.rename": "This channel was renamed by {who}.",
    "bookmarks.add": "A bookmark was added by {who}.",
    "bookmarks.edit": "A bookmark was edited by {who}.",
    "conversations.create": "This channel was created by {who}.",
}
NOTICE_METHODS = set(_CHANGES) | {"conversations.invite"}


def _who(requester_id: str) -> str:
    return f"<@{requester_id}>" if _attributable(requester_id) else "coolton automatically"


def _mentions(user_ids: list[str]) -> str:
    mentions = [f"<@{u}>" for u in user_ids]
    if len(mentions) == 1:
        return mentions[0]
    return f"{', '.join(mentions[:-1])} and {mentions[-1]}"


def notice_for(method: str, params: dict, result: dict, requester_id: str) -> tuple[str, str] | None:
    """(channel id, text) of the notice for a successful call, or None if it needs none."""
    who = _who(requester_id)
    if method == "conversations.create":
        channel = (result.get("channel") or {}).get("id") or ""
    else:
        channel = params.get("channel") or params.get("channel_id") or ""
    if not isinstance(channel, str) or not channel:
        return None
    if method == "conversations.invite":
        users = params.get("users") or ""
        if isinstance(users, str):
            users = users.split(",")
        users = [u.strip() for u in users if isinstance(u, str) and u.strip()]
        if not users:
            return None
        verb = "was" if len(users) == 1 else "were"
        return channel, f"{_mentions(users)} {verb} invited here by {who}."
    template = _CHANGES.get(method)
    return (channel, template.format(who=who)) if template else None


def _post(token: str, channel: str, text: str) -> dict:
    resp = requests.post(
        f"{SLACK_API}/chat.postMessage",
        headers={"Authorization": f"Bearer {token}"},
        data={"channel": channel, "text": text},
        timeout=10,
    )
    return resp.json()


def post_change_notice(method: str, params: dict, result: dict, requester_id: str) -> None:
    """Post the notice for a successful call, if it needs one. As cooltonUser, or
    as the bot when cooltonUser isn't in the channel (e.g. one the bot created)."""
    notice = notice_for(method, params, result, requester_id)
    if not notice:
        return
    channel, text = notice
    error = None
    for env in ("SLACK_USER_TOKEN", "SLACK_BOT_TOKEN"):
        token = os.environ.get(env)
        if not token:
            continue
        try:
            data = _post(token, channel, text)
        except Exception as e:
            error = str(e)
            continue
        if data.get("ok"):
            return
        error = data.get("error")
    logger.warning("Couldn't post the %s notice in %s: %s", method, channel, error)
