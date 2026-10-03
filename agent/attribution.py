"""Enforced "(sent from <@user>)" attribution on messages coolton posts via tools.

Applied in code by every tool that can post or edit a Slack message on the
model's say-so (post_message_tool, chat_postMessage, slack_api_call,
slack_api_call_as_bot_tool, including file shares, and the Slack MCP posting tools — see
agent.slack_mcp_guard) — NOT left to the system prompt, where a user can talk
the model out of it ("don't mention me", "stay anonymous"). The only exempt
tool is send_message: it posts in-thread status updates, where the person who
asked is already right there.

An automated turn (a background job or `wait` waking the conversation up) has
no live sender, so it is credited to the person who started that job/wait
(AgentDeps.on_behalf_of) — see attribution_user_id. Anything with nobody at all
behind it still gets a footer saying so, never none.
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

# Shares a file into a channel, with `initial_comment` as the visible message.
FILE_SHARE_METHOD = "files.completeuploadexternal"

_METHOD_RE = re.compile(r"^[A-Za-z]+(\.[A-Za-z]+)+$")
_SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]+$")


AUTOMATED_FOOTER = "(sent automatically by coolton)"


def _attributable(user_id: str) -> bool:
    return bool(user_id and _SLACK_USER_ID_RE.match(user_id))


def footer_line(user_id: str) -> str:
    return f"(sent from <@{user_id}>)" if _attributable(user_id) else AUTOMATED_FOOTER


def attribution_user_id(deps) -> str:
    """The Slack user a tool-posted message is credited to: the person who
    sent this turn's message, or — for an automated wake-up turn, whose
    user_id is a synthetic "AUTOMATED" — whoever started the job/wait."""
    user_id = getattr(deps, "user_id", "") or ""
    if _attributable(user_id):
        return user_id
    return getattr(deps, "on_behalf_of", "") or ""


def attribute_text(text: str, user_id: str) -> str:
    """`text` with the footer appended on its own line after a blank one.

    Idempotent: text that already ends with this exact footer (the model
    added it itself, or an edit re-sends an attributed message) isn't
    footed twice.
    """
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
    the visible message unattributed). A message carrying none of those
    (e.g. only legacy `attachments`) gets the footer as its `text`, which
    Slack shows above the attachments. A file shared into a channel
    (files.completeUploadExternal with a channel_id or channels) gets the
    footer on its initial_comment, or as its initial_comment when it has none.
    Other methods pass through unchanged.
    """
    if method.lower() == FILE_SHARE_METHOD:
        if not (params.get("channel_id") or params.get("channels")):
            return params
        comment = params.get("initial_comment")
        comment = comment if isinstance(comment, str) else ""
        return {**params, "initial_comment": attribute_text(comment, user_id).strip()}
    if method.lower() not in MESSAGE_METHODS:
        return params
    params = dict(params)
    footed = False
    for key in ("text", "markdown_text"):
        if isinstance(params.get(key), str) and params[key]:
            params[key] = attribute_text(params[key], user_id)
            footed = True

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
        footed = True
    if not footed:
        params["text"] = footer_line(user_id)
    return params
