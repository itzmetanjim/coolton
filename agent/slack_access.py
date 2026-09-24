"""Who may read what, and which Slack Web API methods coolton will call for someone.

Every Slack read coolton does on a user's behalf runs as cooltonUser (or the
bot), which can see far more than the person asking — private channels it was
invited to, its own DMs, files shared anywhere it can see. The rule, applied
the same way by every tool that reads Slack content (read_conversation_history,
summarize_thread, list_channel_threads, search_slack, get_slack_file, the
generic slack_api_call tools, and the Slack MCP read tools):

- the conversation the request came from is always readable;
- otherwise only public, workspace-visible channels (and files shared in one,
  or uploaded by the requester) are.

The generic slack_api_call / slack_api_call_as_bot_tool additionally only call
methods on ALLOWED_API_METHODS — reads, posting (always footed, see
agent.attribution), reactions, pins and joining/leaving — never admin,
deleting, or token-management methods.
"""

from __future__ import annotations

import os
import re

import requests

from agent.tools.slack_search import assert_readable_channel

SLACK_API = "https://slack.com/api"

# Methods that read a channel's content — the channel must be readable (above).
CHANNEL_READ_METHODS = {
    "conversations.history",
    "conversations.replies",
    "conversations.info",
    "conversations.members",
    "reactions.get",
    "pins.list",
    "bookmarks.list",
}

# No delete methods: coolton doesn't delete Slack messages (see the system
# prompt's rules), and a deletion can't carry an attribution footer anyway.
ALLOWED_API_METHODS = CHANNEL_READ_METHODS | {
    # reads that don't expose channel content
    "auth.test",
    "bots.info",
    "chat.getPermalink",
    "conversations.list",
    "dnd.info",
    "emoji.list",
    "team.info",
    "usergroups.list",
    "usergroups.users.list",
    "users.getPresence",
    "users.info",
    "users.list",
    "users.lookupByEmail",
    "users.profile.get",
    # posting (footed by agent.attribution)
    "chat.meMessage",
    "chat.postEphemeral",
    "chat.postMessage",
    "chat.scheduleMessage",
    "chat.update",
    # lightweight channel actions
    "conversations.join",
    "conversations.leave",
    "conversations.open",
    "pins.add",
    "pins.remove",
    "reactions.add",
    "reactions.remove",
}

_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]+$")


def _channel_param(params: dict) -> str:
    value = params.get("channel") or params.get("channel_id") or ""
    return value if isinstance(value, str) else ""


def channel_read_error(channel_id: str, current_channel_id: str) -> str | None:
    """None if `channel_id` may be read for a request made in
    `current_channel_id`, else the reason it can't. A user id (reading
    cooltonUser's DM with that person) is never readable."""
    if not channel_id:
        return "a channel id is required."
    if channel_id == current_channel_id:
        return None
    if _USER_ID_RE.match(channel_id):
        return "Reading DMs, private channels, or external conversations is not allowed."
    # assert_readable_channel treats a missing current channel as "no
    # restriction"; there is always a current conversation here, so pass a
    # placeholder that can never equal a real channel id.
    return assert_readable_channel(channel_id, current_channel_id or "-")


def check_api_call(method: str, params: dict, current_channel_id: str) -> str | None:
    """None if slack_api_call(_as_bot_tool) may make this call, else an
    "Error: ..." string to hand back to the model."""
    if method not in ALLOWED_API_METHODS:
        return (
            f"Error: {method} is not on coolton's Slack API allowlist. Allowed methods: "
            f"{', '.join(sorted(ALLOWED_API_METHODS))}."
        )
    if "token" in params:
        return "Error: api_parameters must not include a token."
    channel = _channel_param(params)
    if method in CHANNEL_READ_METHODS:
        denied = channel_read_error(channel, current_channel_id)
        if denied:
            return f"Error: {method} refused — {denied}"
    if method == "conversations.list":
        types = {t.strip() for t in str(params.get("types") or "public_channel").split(",") if t.strip()}
        if types != {"public_channel"}:
            return "Error: conversations.list may only list public channels (types=public_channel)."
    return None


def file_info_read_error(file_info: dict, current_channel_id: str, requester_id: str) -> str | None:
    """None if a file (Slack file, canvas, or list — all "files" to the API)
    may be read for this request: the requester uploaded it, it's shared in
    the current conversation, or it's shared in a public channel."""
    if requester_id and file_info.get("user") == requester_id:
        return None
    shares = file_info.get("shares") or {}
    public_shares = shares.get("public") or {}
    private_shares = shares.get("private") or {}
    shared_in = set(public_shares) | set(private_shares)
    for key in ("channels", "groups", "ims"):
        shared_in.update(file_info.get(key) or [])
    if current_channel_id and current_channel_id in shared_in:
        return None
    if public_shares or file_info.get("channels") or file_info.get("is_public"):
        return None
    return "that file isn't shared in this conversation or in a public channel, and you didn't upload it."


def fetch_file_info(file_id: str, token: str) -> tuple[dict | None, str | None]:
    """(file_info, None) from files.info, or (None, slack_error_code)."""
    try:
        resp = requests.get(
            f"{SLACK_API}/files.info",
            headers={"Authorization": f"Bearer {token}"},
            params={"file": file_id},
            timeout=10,
        )
        data = resp.json()
    except Exception as e:
        return None, f"request_failed: {e}"
    if not data.get("ok"):
        return None, data.get("error", "unknown")
    return data.get("file") or {}, None


def file_read_error(file_id: str, current_channel_id: str, requester_id: str) -> str | None:
    """Look the file up (as cooltonUser, the identity that would read it) and
    apply file_info_read_error. Fails closed when the lookup fails."""
    token = os.environ.get("SLACK_USER_TOKEN") or os.environ.get("SLACK_BOT_TOKEN", "")
    info, error = fetch_file_info(file_id, token)
    if info is None:
        return f"couldn't verify file {file_id} is shared with you ({error}); refusing."
    return file_info_read_error(info, current_channel_id, requester_id)
