"""Channel notices for channel changes coolton makes for someone.

When slack_api_call(_as_bot_tool) changes a channel (its topic, description or
name, a bookmark, a new channel, an invite or removal, archiving it), Slack's own
system message credits coolton. So coolton posts a top-level message in that
channel, as cooltonUser, naming who asked for it, e.g. "The channel topic was
changed by <@U123>."

The notice goes out after the change succeeds, except for archiving: nothing can
be posted in an archived channel, so that notice goes out first and is deleted
again if the archive then fails (NOTICE_FIRST_METHODS).
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
    "conversations.archive": "This channel was archived by {who}.",
}
# Methods whose notice must be posted before the call (see the module docstring).
NOTICE_FIRST_METHODS = {"conversations.archive"}
# Methods whose notice is posted once the call succeeds.
NOTICE_METHODS = (set(_CHANGES) | {"conversations.invite", "conversations.kick"}) - NOTICE_FIRST_METHODS


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
    if method == "conversations.kick":
        user = params.get("user")
        if not isinstance(user, str) or not user.strip():
            return None
        return channel, f"<@{user.strip()}> was removed from this channel by {who}."
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


def post_change_notice(method: str, params: dict, result: dict, requester_id: str) -> dict | None:
    """Post the notice for a call, if it needs one. As cooltonUser, or as the bot
    when cooltonUser isn't in the channel (e.g. one the bot created). Returns what
    delete_notice needs to take it back, or None if nothing was posted."""
    notice = notice_for(method, params, result, requester_id)
    if not notice:
        return None
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
            return {"token_env": env, "channel": channel, "ts": data.get("ts", "")}
        error = data.get("error")
    logger.warning("Couldn't post the %s notice in %s: %s", method, channel, error)
    return None


def delete_notice(posted: dict) -> None:
    """Delete a notice post_change_notice posted (its change then failed), with the
    same token that posted it. Never raises."""
    try:
        resp = requests.post(
            f"{SLACK_API}/chat.delete",
            headers={"Authorization": f"Bearer {os.environ.get(posted['token_env'], '')}"},
            data={"channel": posted["channel"], "ts": posted["ts"]},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            logger.warning("Couldn't delete the notice %s in %s: %s", posted["ts"], posted["channel"], data.get("error"))
    except Exception:
        logger.exception("Couldn't delete the notice %s in %s", posted.get("ts"), posted.get("channel"))
