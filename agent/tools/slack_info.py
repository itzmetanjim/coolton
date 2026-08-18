import os
import re
import time
import requests

SLACK_API = "https://slack.com/api"


def _bot_headers():
    token = os.environ.get("SLACK_BOT_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else None


def _team_id() -> str:
    return os.environ.get("SLACK_TEAM_ID", "")


def _strip_mention(text: str) -> str:
    """Strip a Slack mention wrapper like <@U123|name> / <#C123|name> / <F123|name> to its id."""
    text = text.strip()
    if text.startswith("<") and ">" in text:
        inner = text[1:text.index(">")]
        candidate = inner.split("|")[0]
        if candidate.startswith(("@", "#", "!")):
            candidate = candidate[1:]
        return candidate
    return text


def _resolve_user_id(user_id: str) -> str:
    """Return a Slack user id (U...) given an id, <@mention>, or @username/display name."""
    user_id = _strip_mention(user_id)
    if re.match(r"^U[A-Z0-9]+$", user_id):
        return user_id
    name = user_id.lstrip("@").lower()
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token or not name:
        return user_id
    try:
        params = {"limit": 200}
        team_id = _team_id()
        if team_id:
            params["team_id"] = team_id
        for _ in range(10):
            resp = requests.get(
                f"{SLACK_API}/users.list", params=params, headers=_bot_headers(), timeout=15
            )
            data = resp.json()
            if not data.get("ok"):
                if data.get("error") == "ratelimited":
                    time.sleep(0.5)
                    continue
                return user_id
            for member in data.get("members", []):
                if not member or member.get("deleted"):
                    continue
                profile = member.get("profile", {})
                cands = {
                    member.get("name", ""),
                    profile.get("display_name", ""),
                    profile.get("real_name", ""),
                    (profile.get("email", "") or "").split("@")[0],
                    profile.get("email", "") or "",
                }
                if name in {c.lower() for c in cands if c}:
                    return member["id"]
            next_cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not next_cursor:
                break
            params["cursor"] = next_cursor
            time.sleep(0.15)
    except Exception:
        pass
    return user_id


def _resolve_channel_id(channel_id: str) -> str:
    """Return a Slack channel id (C/D/G...) given an id, <#mention>, or #channel name."""
    channel_id = _strip_mention(channel_id)
    if re.match(r"^[CDG][A-Z0-9]+$", channel_id):
        return channel_id
    name = channel_id.lstrip("#").lower()
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token or not name:
        return channel_id
    try:
        params = {"limit": 200, "types": "public_channel,private_channel,mpim,im"}
        team_id = _team_id()
        if team_id:
            params["team_id"] = team_id
        for _ in range(10):
            resp = requests.get(
                f"{SLACK_API}/conversations.list", params=params, headers=_bot_headers(), timeout=15
            )
            data = resp.json()
            if not data.get("ok"):
                if data.get("error") == "ratelimited":
                    time.sleep(0.5)
                    continue
                return channel_id
            for ch in data.get("channels", []):
                if (ch.get("name") or "").lower() == name or ch.get("id") == channel_id:
                    return ch["id"]
            next_cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not next_cursor:
                break
            params["cursor"] = next_cursor
            time.sleep(0.15)
    except Exception:
        pass
    return channel_id


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
    user_id = _resolve_user_id(user_id)
    if not re.match(r"^U[A-Z0-9]+$", user_id):
        return (
            f"Error: could not resolve '{user_id}' to a Slack user id. Pass the <@U...> mention, "
            "an @username, or the U... id from the message context."
        )
    try:
        params = {"user": user_id}
        team_id = _team_id()
        if team_id:
            params["team_id"] = team_id
        response = requests.get(
            f"{SLACK_API}/users.info",
            params=params,
            headers=_bot_headers(),
            timeout=15,
        )
        res_json = response.json()
        if not res_json.get("ok"):
            err = res_json.get('error', 'unknown')
            if err in ("user_not_found", "team_access_not_granted"):
                return (
                    f"Slack API error: {err} — no user matches '{user_id}'. Use the exact "
                    "user id (U...), an <@U...> mention, or an @username from the message "
                    "context; don't guess ids."
                )
            return f"Slack API error: {res_json}"
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
    channel_id = _resolve_channel_id(channel_id)
    if not re.match(r"^[CDG][A-Z0-9]+$", channel_id):
        return (
            f"Error: could not resolve '{channel_id}' to a Slack channel id. Pass the <#C...|name> "
            "mention, a #channel name, or the C.../D.../G... id from the message context."
        )
    try:
        params = {"channel": channel_id}
        team_id = _team_id()
        if team_id:
            params["team_id"] = team_id
        response = requests.get(
            f"{SLACK_API}/conversations.info",
            params=params,
            headers=_bot_headers(),
            timeout=15,
        )
        res_json = response.json()
        if not res_json.get("ok"):
            err = res_json.get('error', 'unknown')
            if err in ("team_access_not_granted", "channel_not_found", "not_in_channel"):
                return (
                    f"Slack API error: {err} — no channel matches '{channel_id}'. Use the exact "
                    "channel id (C.../D.../G...), an <#C...|name> mention, or a #channel name "
                    "from the message context; don't guess ids."
                )
            return f"Slack API error: {res_json}"
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
    current_channel: str = "", username: str = "", icon_url: str = "",
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
        username: Override display name (set to the prompting user's name).
        icon_url: Override avatar URL (set to the prompting user's pfp).
    """
    if not text or not text.strip():
        return "Error: text is required"
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return "Error: SLACK_BOT_TOKEN not configured"

    if channel_id and channel_id[0] in ("C", "G") and current_channel and channel_id != current_channel:
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
    if username:
        payload["username"] = username
    if icon_url:
        payload["icon_url"] = icon_url
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
        return f"Slack API error: {res_json}"
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
        return f"Slack API error: {res_json}"
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
        return f"Slack API error: {res_json}"
    except Exception as e:
        return f"Error removing reaction: {str(e)}"
