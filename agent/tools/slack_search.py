import os
import re
import requests

SLACK_API = "https://slack.com/api"


def _clean_text(text: str) -> str:
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def search_slack_messages(
    query: str, count: int = 5, sort: str = "score", sort_dir: str = "desc"
) -> str:
    """Search Slack messages across the workspace using the user token.

    Args:
        query: The search query (supports Slack search syntax like `in:#channel`, `from:@user`).
        count: Number of results to return (default 5, max 20).
        sort: Sort by 'score' or 'timestamp'.
        sort_dir: Direction 'desc' or 'asc'.
    """
    user_token = os.environ.get("SLACK_USER_TOKEN")
    if not user_token:
        return "Error: SLACK_USER_TOKEN not configured (requires search:read scope)"
    if not query or not query.strip():
        return "Error: query is required"
    try:
        params = {
            "query": query,
            "count": max(1, min(int(count), 20)),
            "sort": sort,
            "sort_dir": sort_dir,
        }
        team_id = os.environ.get("SLACK_TEAM_ID")
        if team_id:
            params["team_id"] = team_id
        response = requests.get(
            f"{SLACK_API}/search.messages",
            params=params,
            headers={"Authorization": f"Bearer {user_token}"},
            timeout=20,
        )
        res_json = response.json()
        if not res_json.get("ok"):
            return f"Slack API error: {res_json}"
        messages = (res_json.get("messages") or {}).get("matches", [])
        if not messages:
            return "No Slack messages found."
        lines = []
        for i, m in enumerate(messages, 1):
            channel = m.get("channel", {})
            channel_name = f"#{channel.get('name')}" if channel.get("name") else channel.get("id", "unknown")
            permalink = m.get("permalink", "")
            user = m.get("username") or m.get("user", "unknown")
            ts = m.get("ts", "")
            text = _clean_text(m.get("text", "") or "")
            if len(text) > 300:
                text = text[:300] + "…"
            lines.append(f"{i}. [{channel_name}] <{permalink}|{user} {ts}>: {text}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error searching Slack: {str(e)}"


def assert_readable_channel(channel_id: str, current_channel_id: str = "") -> str | None:
    """Refuse to read DMs, private channels, or external conversations (mirrors gorkie).

    Reading the current channel is always allowed. Otherwise the channel must be a
    workspace-visible (public) channel. Returns an error string, or None if allowed.

    Fails CLOSED: if the visibility of `channel_id` can't be verified (the API call
    errors, times out, or itself returns ok:false — e.g. a private channel the bot
    can't even see), this denies rather than silently falling through to allow.
    """
    if not channel_id or not current_channel_id:
        return None
    if channel_id == current_channel_id:
        return None
    try:
        response = requests.get(
            f"{SLACK_API}/conversations.info",
            params={"channel": channel_id},
            headers={"Authorization": f"Bearer {os.environ.get('SLACK_BOT_TOKEN', '')}"},
            timeout=10,
        )
        data = response.json() or {}
        if not data.get("ok"):
            return f"Could not verify channel {channel_id} is safe to read ({data.get('error', 'unknown error')}); refusing."
        ch = data.get("channel", {})
        kind = (ch.get("id") or ch.get("type") or "")[:1]
        is_private = (
            ch.get("is_private")
            or ch.get("is_mpim")
            or kind in ("D", "G")
            or ch.get("is_org_shared")
        )
        if is_private:
            return "Reading DMs, private channels, or external conversations is not allowed."
        return None
    except Exception as e:
        return f"Could not verify channel {channel_id} is safe to read ({e}); refusing."


def read_conversation_history(
    channel_id: str,
    limit: int = 20,
    cursor: str = "",
    thread_ts: str = "",
    current_channel_id: str = "",
) -> str:
    """Read recent messages from a Slack channel, or replies within a thread.

    Args:
        channel_id: The channel ID to read.
        limit: Number of messages to read (default 20, max 200).
        cursor: Pagination cursor to read older messages (pass the returned next_cursor).
        thread_ts: If set, read replies in the thread with this timestamp instead.
        current_channel_id: The channel the request came from (only this, or public
            channels, may be read; DMs/private/external are refused).
    """
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        return "Error: SLACK_BOT_TOKEN not configured"
    if not channel_id:
        return "Error: channel_id is required"
    denied = assert_readable_channel(channel_id, current_channel_id)
    if denied:
        return f"Error: {denied}"
    try:
        if thread_ts:
            method = "conversations.replies"
            params = {"channel": channel_id, "ts": thread_ts, "limit": max(1, min(int(limit), 200))}
        else:
            method = "conversations.history"
            params = {"channel": channel_id, "limit": max(1, min(int(limit), 200))}
        if cursor:
            params["cursor"] = cursor
        response = requests.get(
            f"{SLACK_API}/{method}",
            params=params,
            headers={"Authorization": f"Bearer {bot_token}"},
            timeout=20,
        )
        res_json = response.json()
        if not res_json.get("ok"):
            return f"Slack API error: {res_json}"
        messages = res_json.get("messages", [])
        if not messages:
            return "No messages found."
        next_cursor = ""
        metadata = res_json.get("response_metadata") or {}
        if metadata.get("next_cursor"):
            next_cursor = metadata["next_cursor"]
        lines = []
        for m in messages:
            ts = m.get("ts", "")
            user = m.get("user") or m.get("username") or "bot"
            subtype = m.get("subtype", "")
            text = m.get("text", "") or ""
            if subtype == "channel_join":
                continue
            text = text.replace("\n", " ")
            if len(text) > 500:
                text = text[:500] + "…"
            lines.append(f"{ts} <@{user}> {text}")
        if not lines:
            return "No readable messages found."
        lines.append("")
        if next_cursor:
            lines.append(f"(next_cursor: {next_cursor} — call again with it to read older messages)")
        else:
            lines.append("(end of history)")
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading conversation: {str(e)}"
