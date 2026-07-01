import os
import requests


def list_channel_threads(channel_id: str, limit: int = 10, user_token: str | None = None) -> str:
    """List recent threads in a Slack channel.

    Fetches the most recent messages and identifies those that are
    thread parents (have replies). Returns thread info with reply counts.

    Args:
        channel_id: Slack channel ID to list threads from.
        limit: Maximum number of threads to return (default 10).
        user_token: Slack user token for API calls.

    Returns:
        Formatted list of threads, or an error message.
    """
    token = user_token or os.environ.get("SLACK_USER_TOKEN")
    if not token:
        return "Error: SLACK_USER_TOKEN not configured."

    try:
        url = "https://slack.com/api/conversations.history"
        headers = {"Authorization": f"Bearer {token}"}

        resp = requests.get(
            url,
            headers=headers,
            params={"channel": channel_id, "limit": 50},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            return f"Slack API error: {data.get('error', 'unknown')}"

        messages = data.get("messages", [])
        thread_parents = [m for m in messages if int(m.get("reply_count", 0)) > 0]

        if not thread_parents:
            return "No threads found in this channel."

        result = []
        for msg in thread_parents[:limit]:
            user = msg.get("user", "unknown")
            text = msg.get("text", "")[:120]
            replies = msg.get("reply_count", 0)
            ts = msg.get("ts", "")
            result.append(f"• <@{user}> ({replies} replies): _{text}_ — `{ts}`")

        return f"*Recent threads in <#{channel_id}>:*\n" + "\n".join(result)

    except Exception as e:
        return f"Error listing threads: {str(e)}"
