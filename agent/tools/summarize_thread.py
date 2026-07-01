import os
import requests


def summarize_thread(channel_id: str, thread_ts: str, user_token: str | None = None) -> str:
    """Summarize a Slack thread by fetching its messages and condensing them.

    Args:
        channel_id: Slack channel ID.
        thread_ts: Thread timestamp to summarize.
        user_token: Slack user token for API calls.

    Returns:
        A summary of the thread, or an error message.
    """
    token = user_token or os.environ.get("SLACK_USER_TOKEN")
    if not token:
        return "Error: SLACK_USER_TOKEN not configured."

    try:
        messages = _fetch_thread_messages(channel_id, thread_ts, token)
        if not messages:
            return "No messages found in this thread."

        conversation_text = _format_messages(messages)
        summary = _call_summary_model(conversation_text)
        return summary

    except Exception as e:
        return f"Error summarizing thread: {str(e)}"


def _fetch_thread_messages(channel_id: str, thread_ts: str, token: str) -> list[dict]:
    url = "https://slack.com/api/conversations.replies"
    headers = {"Authorization": f"Bearer {token}"}
    all_messages = []
    cursor = None

    while True:
        params = {"channel": channel_id, "ts": thread_ts, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        data = resp.json()
        if not data.get("ok"):
            raise Exception(f"Slack API error: {data.get('error', 'unknown')}")
        all_messages.extend(data.get("messages", []))
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    return all_messages


def _format_messages(messages: list[dict]) -> str:
    lines = []
    for msg in messages:
        user = msg.get("user", "unknown")
        text = msg.get("text", "")
        ts = msg.get("ts", "")
        lines.append(f"[{ts}] <{user}>: {text}")
    return "\n".join(lines)


def _call_summary_model(conversation_text: str) -> str:
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    prompt = f"Summarize the following Slack conversation concisely, highlighting key decisions, questions, and action items:\n\n{conversation_text[:15000]}"

    if anthropic_key:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 2048, "messages": [{"role": "user", "content": prompt}]},
            headers={"x-api-key": anthropic_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            timeout=30,
        )
        data = resp.json()
        if "content" in data:
            return "".join(b["text"] for b in data["content"] if b.get("type") == "text")
        return f"Error: {data.get('error', {}).get('message', 'unknown')}"

    if openai_key:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            json={"model": "gpt-4.1-mini", "max_tokens": 2048, "messages": [{"role": "user", "content": prompt}]},
            headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
            timeout=30,
        )
        data = resp.json()
        if "choices" in data:
            return data["choices"][0]["message"]["content"]
        return f"Error: {data.get('error', {}).get('message', 'unknown')}"

    return "Error: No AI provider configured for summarization."
