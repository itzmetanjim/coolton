import logging
from datetime import datetime, timezone

from pydantic_ai.messages import ModelRequest, ModelResponse, UserPromptPart, TextPart

logger = logging.getLogger(__name__)

MAX_TOTAL = 500
MAX_CONTEXT = 40


def _display_name(client, user_id: str, cache: dict) -> str:
    if user_id in cache:
        return cache[user_id]
    name = user_id
    try:
        resp = client.users_info(user=user_id)
        if resp.get("ok"):
            profile = (resp.get("user") or {}).get("profile") or {}
            name = profile.get("display_name") or profile.get("real_name") or user_id
    except Exception:
        pass
    cache[user_id] = name
    return name


def build_thread_context(
    client,
    channel_id: str,
    thread_ts: str,
    exclude_ts: str | None = None,
) -> list | None:
    """Fetch a Slack thread's earlier messages and return them as model history.

    Returns None when there's nothing useful to add (empty prior thread, fetch
    error, or the mention isn't inside a real thread) so callers can fall back
    to the current no-context behavior.
    """
    fetched = []
    cursor = None
    while True:
        try:
            kwargs = {"channel": channel_id, "ts": thread_ts, "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = client.conversations_replies(**kwargs)
            if not resp.get("ok"):
                logger.warning("conversations.replies failed: %s", resp.get("error"))
                return None
        except Exception as e:
            logger.warning("Failed to fetch thread history: %s", e)
            return None
        fetched.extend(resp.get("messages") or [])
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor or len(fetched) >= MAX_TOTAL:
            break

    prior = [m for m in fetched if m.get("ts") != exclude_ts and m.get("text")]
    if not prior:
        return None
    prior = prior[-MAX_CONTEXT:]

    name_cache = {}
    model_messages = []
    for msg in prior:
        text = msg.get("text", "")
        try:
            if msg.get("bot_id") or msg.get("subtype") == "bot_message":
                model_messages.append(ModelResponse(parts=[TextPart(content=text)]))
            else:
                user = msg.get("user") or "unknown"
                name = _display_name(client, user, name_cache)
                try:
                    timestamp = datetime.fromtimestamp(float(msg.get("ts")), tz=timezone.utc)
                except (TypeError, ValueError):
                    timestamp = None
                model_messages.append(
                    ModelRequest(
                        parts=[
                            UserPromptPart(
                                content=f"{user} ({name}):\n{text}",
                                timestamp=timestamp,
                            )
                        ]
                    )
                )
        except Exception:
            logger.exception(
                "Failed to build model message for thread message %s", msg.get("ts")
            )
    return model_messages or None
