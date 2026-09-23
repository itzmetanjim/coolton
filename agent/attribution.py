"""Enforced "(sent from <@user>)" attribution on messages coolton posts via tools.

Applied in code by every tool that can post or edit a Slack message on the
model's say-so (post_message_tool, chat_postMessage, slack_api_call,
slack_api_call_as_bot_tool) — NOT left to the system prompt, where a user can
talk the model out of it ("don't mention me", "stay anonymous"). The only
exempt tool is send_message: it posts in-thread status updates, where the
person who asked is already right there.
"""
from __future__ import annotations

import json
import re

# Slack API methods whose text/blocks end up as a visible message.
MESSAGE_METHODS = {
    "chat.postmessage",
    "chat.update",
    "chat.schedulemessage",
    "chat.postephemeral",
    "chat.memessage",
}

_METHOD_RE = re.compile(r"^[A-Za-z]+(\.[A-Za-z]+)+$")
_SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]+$")


def footer_line(user_id: str) -> str:
    return f"(sent from <@{user_id}>)"


def _attributable(user_id: str) -> bool:
    # Automated turns (background-job wakes etc.) run as a synthetic id with
    # no real Slack user behind it — there's nobody to credit.
    return bool(user_id and _SLACK_USER_ID_RE.match(user_id))


def attribute_text(text: str, user_id: str) -> str:
    """`text` with the footer appended on its own line after a blank one.

    Idempotent: text that already ends with this exact footer (the model
    added it itself, or an edit re-sends an attributed message) isn't
    footed twice.
    """
    if not _attributable(user_id):
        return text
    footer = footer_line(user_id)
    if (text or "").rstrip().endswith(footer):
        return text
    return f"{(text or '').rstrip()}\n\n{footer}"


def is_valid_method(method: str) -> bool:
    """Slack method names are plain dotted words. Anything else (a query
    string, a path, a fragment) is refused outright rather than normalized:
    `chat.postMessage?text=...` would otherwise reach slack.com with message
    params the footer logic never saw."""
    return bool(_METHOD_RE.match(method or ""))


def attribute_api_params(method: str, params: dict, user_id: str) -> dict:
    """Footer every visible text field of a message-posting/editing call.

    Covers `text`, `markdown_text`, and `blocks` (when blocks are present
    Slack renders them INSTEAD of `text`, so footing only `text` would leave
    the visible message unattributed). Non-message methods pass through
    unchanged.
    """
    if method.lower() not in MESSAGE_METHODS or not _attributable(user_id):
        return params
    params = dict(params)
    for key in ("text", "markdown_text"):
        if isinstance(params.get(key), str) and params[key]:
            params[key] = attribute_text(params[key], user_id)

    blocks = params.get("blocks")
    was_string = isinstance(blocks, str)
    if was_string:
        try:
            blocks = json.loads(blocks)
        except ValueError:
            blocks = None
    if isinstance(blocks, list) and blocks:
        blocks = [*blocks, {"type": "context", "elements": [{"type": "mrkdwn", "text": footer_line(user_id)}]}]
        params["blocks"] = json.dumps(blocks) if was_string else blocks
    return params
