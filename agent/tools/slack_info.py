import os
import requests

SLACK_API = "https://slack.com/api"


def _bot_headers():
    token = os.environ.get("SLACK_BOT_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else None


def get_user_info(user_id: str) -> str:
    """Look up a Slack user's profile (display name, real name, pronouns,
    timezone, title, status, custom fields, and whether they are a bot).

    Args:
        user_id: Slack user ID (U...). Can also be a member ID.
    """
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return "Error: SLACK_BOT_TOKEN not configured"
    if not user_id:
        return "Error: user_id is required"
    try:
        response = requests.get(
            f"{SLACK_API}/users.info",
            params={"user": user_id},
            headers=_bot_headers(),
            timeout=15,
        )
        res_json = response.json()
        if not res_json.get("ok"):
            return f"Slack API error: {res_json.get('error', 'unknown')}"
        user = res_json.get("user", {})
        profile = user.get("profile", {})
        is_bot = bool(user.get("is_bot") or user.get("is_app_user"))
        lines = [
            f"User: {profile.get('display_name') or user.get('name') or user_id} (id: {user_id})",
        ]
        if profile.get("real_name"):
            lines.append(f"Real name: {profile['real_name']}")
        if profile.get("pronouns"):
            lines.append(f"Pronouns: {profile['pronouns']}")
        if profile.get("title"):
            lines.append(f"Title: {profile['title']}")
        if profile.get("status_text"):
            lines.append(f"Status: {profile['status_text']}")
        if profile.get("status_emoji"):
            lines.append(f"Status emoji: {profile['status_emoji']}")
        tz = user.get("tz_label") or profile.get("tz_label")
        if tz:
            lines.append(f"Timezone: {tz}")
        if is_bot:
            lines.append("Bot: yes")
        custom = profile.get("fields") or {}
        custom_fields = []
        for key, val in custom.items():
            if isinstance(val, dict):
                custom_fields.append(f"{val.get('label', key)}: {val.get('value', '')}")
        if custom_fields:
            lines.append("Custom fields: " + "; ".join(custom_fields))
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching user: {str(e)}"


def get_channel_info(channel_id: str) -> str:
    """Look up a Slack channel's metadata (name, member count, DM status, visibility).

    Args:
        channel_id: Slack channel ID (C..., D..., or G...).
    """
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return "Error: SLACK_BOT_TOKEN not configured"
    if not channel_id:
        return "Error: channel_id is required"
    try:
        response = requests.get(
            f"{SLACK_API}/conversations.info",
            params={"channel": channel_id},
            headers=_bot_headers(),
            timeout=15,
        )
        res_json = response.json()
        if not res_json.get("ok"):
            return f"Slack API error: {res_json.get('error', 'unknown')}"
        channel = res_json.get("channel", {})
        channel_type = channel.get("id", "")[0] if channel.get("id") else ""
        if channel_type == "D":
            kind = "DM"
        elif channel_type == "G":
            kind = "private channel"
        else:
            kind = "public channel"
        lines = [f"Channel: #{channel.get('name', channel_id)} (id: {channel_id})"]
        lines.append(f"Type: {kind}")
        lines.append(f"Members: {channel.get('num_members', 'n/a')}")
        if channel.get("topic", {}).get("value"):
            lines.append(f"Topic: {channel['topic']['value']}")
        if channel.get("purpose", {}).get("value"):
            lines.append(f"Purpose: {channel['purpose']['value']}")
        if channel.get("is_member"):
            lines.append("coolton is a member: yes")
        if channel.get("is_archived"):
            lines.append("Archived: yes")
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching channel: {str(e)}"


def post_message_to_target(
    channel_id: str, text: str, *, thread_ts: str = "", from_user: str = "",
    current_channel: str = "",
) -> str:
    """Post a message to a Slack channel/thread as the coolton bot.

    Safety constraints (mirroring gorkie): you may only post to the channel you
    are currently in, a thread within it, or a DM with the user who asked. Posting
    to arbitrary channels or other users' DMs is refused.

    Args:
        channel_id: Target channel ID.
        text: Message text (Markdown supported).
        thread_ts: Optional thread timestamp to post into.
        from_user: The user who requested the post (for DM validation).
        current_channel: The channel the request came from (must match for non-DM targets).
    """
    if not text or not text.strip():
        return "Error: text is required"
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return "Error: SLACK_BOT_TOKEN not configured"

    if channel_id and channel_id.startswith("C") and current_channel and channel_id != current_channel:
        return (
            "Error: You can only post to the channel you are currently in. "
            "Refusing to post to a different channel."
        )

    # DM channels must belong to the requesting user; others must be the current channel.
    if channel_id and channel_id[0] in ("D", "G"):
        conv = None
        try:
            response = requests.get(
                f"{SLACK_API}/conversations.info",
                params={"channel": channel_id},
                headers=_bot_headers(),
                timeout=15,
            )
            res_json = response.json()
            if res_json.get("ok"):
                conv = res_json.get("channel", {})
        except Exception:
            conv = None
        if conv and channel_id.startswith("D") and from_user:
            users = conv.get("user") or ""
            if users != from_user and from_user not in conv.get("members", []):
                return "Error: You can only post to a DM with the user who asked (refused)."
        elif channel_id.startswith("D") and from_user:
            return "Error: Could not verify this DM belongs to you (refused)."

    payload = {"channel": channel_id, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        response = requests.post(
            f"{SLACK_API}/chat.postMessage",
            json=payload,
            headers={**_bot_headers(), "Content-Type": "application/json; charset=utf-8"},
            timeout=15,
        )
        res_json = response.json()
        if res_json.get("ok"):
            return f"Message posted to {channel_id}"
        return f"Slack API error: {res_json.get('error', 'unknown')}"
    except Exception as e:
        return f"Error posting message: {str(e)}"


def leave_slack_channel(channel_id: str) -> str:
    """Make the coolton bot leave a Slack channel.

    Args:
        channel_id: Slack channel ID to leave.
    """
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return "Error: SLACK_BOT_TOKEN not configured"
    if not channel_id:
        return "Error: channel_id is required"
    if channel_id.startswith("D"):
        return "Error: Cannot leave a DM."
    try:
        response = requests.post(
            f"{SLACK_API}/conversations.leave",
            data={"channel": channel_id},
            headers=_bot_headers(),
            timeout=15,
        )
        res_json = response.json()
        if res_json.get("ok"):
            return f"Left channel {channel_id}"
        return f"Slack API error: {res_json.get('error', 'unknown')}"
    except Exception as e:
        return f"Error leaving channel: {str(e)}"


def remove_emoji_reaction(channel_id: str, timestamp: str, name: str) -> str:
    """Remove an emoji reaction from a message.

    Args:
        channel_id: Channel the message is in.
        timestamp: The message timestamp (ts) to remove the reaction from.
        name: Emoji name without colons (e.g. 'tada').
    """
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return "Error: SLACK_BOT_TOKEN not configured"
    if not name or not timestamp:
        return "Error: name and timestamp are required"
    try:
        response = requests.post(
            f"{SLACK_API}/reactions.remove",
            data={"channel": channel_id, "timestamp": timestamp, "name": name},
            headers=_bot_headers(),
            timeout=15,
        )
        res_json = response.json()
        if res_json.get("ok"):
            return f"Removed :{name}: reaction"
        return f"Slack API error: {res_json.get('error', 'unknown')}"
    except Exception as e:
        return f"Error removing reaction: {str(e)}"
