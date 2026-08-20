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
        return _call_summary_model(conversation_text, channel_id, thread_ts)

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
            raise Exception(f"Slack API error: {data}")
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


def _call_summary_model(conversation_text: str, channel_id: str = "", thread_ts: str = "") -> str:
    """Summarize via the summarizer subagent (reuses the main provider fallback chain).

    Falls back to a single direct model call — still going through the centralized
    providers.json fallback chain (agent.provider_config), never a hardcoded
    provider/model — if the subagent path is unavailable.
    """
    try:
        from agent.deps import AgentDeps
        from agent.subagents import run_subagent

        from slack_sdk import WebClient

        client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))
        deps = AgentDeps(
            client=client,
            user_id=os.environ.get("COOLTON_USER_ID", "") or "",
            channel_id=channel_id,
            thread_ts=thread_ts or "",
            message_ts="1.0",
            user_token=os.environ.get("SLACK_USER_TOKEN"),
        )
        task = (
            "Summarize this Slack conversation clearly and concisely. Preserve decisions, "
            f"open questions, and action items when present.\n\n{conversation_text[:20000]}"
        )
        summary = run_subagent("summarizer", task, deps)
        if summary:
            return summary
    except Exception:
        pass

    prompt = f"Summarize the following Slack conversation concisely, highlighting key decisions, questions, and action items:\n\n{conversation_text[:15000]}"
    try:
        from pydantic_ai.direct import model_request_sync
        from pydantic_ai.messages import ModelRequest, UserPromptPart

        from agent.provider_config import get_model_from_config

        response = model_request_sync(
            get_model_from_config(),
            [ModelRequest(parts=[UserPromptPart(content=prompt)])],
        )
        text = "".join(p.content for p in response.parts if hasattr(p, "content"))
        return text or "Error: model returned no text."
    except Exception as e:
        return f"Error: No AI provider available for summarization ({e})."
