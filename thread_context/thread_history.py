import logging
import os
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


def _fetch_replies(client, channel_id: str, thread_ts: str, oldest: str | None = None) -> list | None:
    """Every message in a thread (up to MAX_TOTAL), or None on a fetch error."""
    fetched = []
    cursor = None
    while True:
        try:
            kwargs = {"channel": channel_id, "ts": thread_ts, "limit": 200}
            if oldest:
                kwargs["oldest"] = oldest
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
            return fetched


def _timestamp(msg: dict):
    try:
        return datetime.fromtimestamp(float(msg.get("ts")), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _to_model_message(client, msg: dict, name_cache: dict, coolton_bot_id: str):
    """One Slack thread message as model history (None if it can't be built)."""
    text = msg.get("text", "")
    try:
        is_bot_message = bool(msg.get("bot_id")) or msg.get("subtype") == "bot_message"
        if is_bot_message and coolton_bot_id and msg.get("user") == coolton_bot_id:
            return ModelResponse(parts=[TextPart(content=text)])
        if is_bot_message:
            bot_name = msg.get("username") or msg.get("bot_id") or "unknown bot"
            return ModelRequest(parts=[UserPromptPart(content=f"[other bot] {bot_name}:\n{text}", timestamp=_timestamp(msg))])
        user = msg.get("user") or "unknown"
        name = _display_name(client, user, name_cache)
        return ModelRequest(parts=[UserPromptPart(content=f"{user} ({name}):\n{text}", timestamp=_timestamp(msg))])
    except Exception:
        logger.exception("Failed to build model message for thread message %s", msg.get("ts"))
        return None


_UNSEEN_HEADER = (
    "[Messages posted in this thread since your last turn here. You weren't mentioned in them, "
    "so you haven't seen them until now — read them as context for the message you're answering.]"
)


def _history_text(history: list) -> str:
    """Every piece of text already in a stored history, to skip re-adding a
    message the model already saw some other way (e.g. a steering message
    folded into a running turn)."""
    chunks = []
    for message in history:
        for part in getattr(message, "parts", []):
            content = getattr(part, "content", None)
            if isinstance(content, str):
                chunks.append(content)
    return "\n".join(chunks)


def build_unseen_messages(
    client,
    channel_id: str,
    thread_ts: str,
    after_ts: str,
    exclude_ts: str | None,
    history: list,
) -> list | None:
    """Thread messages posted after `after_ts` that the stored `history`
    doesn't already cover, as model history to append to it — for a mention
    in a thread coolton already has context in, where people kept talking
    without mentioning it. None if there are none (or the fetch failed).

    Skips the message being answered, "##" asides, coolton's own messages
    (the bot and cooltonUser — its replies are already in history as its own
    turns), and anything whose text is already in history.
    """
    fetched = _fetch_replies(client, channel_id, thread_ts, oldest=after_ts)
    if not fetched:
        return None
    try:
        after = float(after_ts)
    except (TypeError, ValueError):
        return None

    own_ids = {i for i in (os.environ.get("COOLTON_BOT_ID", ""), os.environ.get("COOLTON_USER_ID", "")) if i}
    seen_text = _history_text(history)
    unseen = []
    for msg in fetched:
        text = msg.get("text") or ""
        try:
            ts = float(msg.get("ts"))
        except (TypeError, ValueError):
            continue
        if ts <= after or msg.get("ts") == exclude_ts:
            continue
        if not text.strip() or text.strip().startswith("##") or msg.get("user") in own_ids:
            continue
        if text.strip() in seen_text:
            continue
        unseen.append(msg)
    if not unseen:
        return None
    unseen = unseen[-MAX_CONTEXT:]

    name_cache = {}
    converted = [m for m in (_to_model_message(client, msg, name_cache, "") for msg in unseen) if m is not None]
    if not converted:
        return None
    return [ModelRequest(parts=[UserPromptPart(content=_UNSEEN_HEADER)]), *converted]


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
    fetched = _fetch_replies(client, channel_id, thread_ts)
    if fetched is None:
        return None

    # "##"-prefixed messages are never processed or responded to (see
    # listeners/events/message.py / app_mentioned.py) — they must stay out of the
    # model's context too, not just skip triggering a response, or a private
    # aside dropped into a thread leaks in the moment a real message pulls this
    # thread's history for the first time.
    prior = [
        m for m in fetched
        if m.get("ts") != exclude_ts and m.get("text") and not m["text"].strip().startswith("##")
    ]
    if not prior:
        return None
    prior = prior[-MAX_CONTEXT:]

    # Only messages coolton itself posted (its own bot user id) belong in the
    # model's history as ModelResponse — that role tells the model "you said
    # this," so it MUST NOT be used for other Slack apps' bot messages. A
    # thread this mention is new to may already have other bots talking in
    # it; attributing their text to the assistant turn made the model treat
    # a different bot's outputs as its own prior utterances and continue/
    # mimic them instead of speaking as itself.
    coolton_bot_id = os.environ.get("COOLTON_BOT_ID", "")

    name_cache = {}
    model_messages = [
        m for m in (_to_model_message(client, msg, name_cache, coolton_bot_id) for msg in prior) if m is not None
    ]
    return model_messages or None
