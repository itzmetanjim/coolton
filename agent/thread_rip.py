"""`!threadrip [link]`: delete every message coolton posted in a thread (as her bot, as
cooltonUser, or as the code channels app). Admin only (agent.ban_store.is_authorized);
anyone else's `!threadrip` is ignored without a word, like !ban. With no link it rips
the thread it was sent in."""

from __future__ import annotations

import logging
import os
import re
import time

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

_COMMAND_RE = re.compile(r"^!threadrip(?:\s+<?(\S+?)>?)?$", re.IGNORECASE)
_LINK_RE = re.compile(r"https://[^/\s]+/archives/([A-Z0-9]+)/p(\d{10})(\d{6})(?:\?[^\s>|]*)?")


def parse_threadrip(text: str, bot_id: str = "") -> tuple[bool, str | None]:
    """(is a !threadrip, its link or None). The whole message (after an optional mention of
    the bot) must be the command, so a prompt that merely mentions it never matches."""
    stripped = (text or "").strip()
    mention = f"<@{bot_id}>" if bot_id else ""
    if mention and stripped.startswith(mention):
        stripped = stripped[len(mention):].strip()
    match = _COMMAND_RE.match(stripped)
    if not match:
        return False, None
    return True, match.group(1)


def thread_from_link(link: str) -> tuple[str, str] | None:
    """(channel, ts) of a Slack message link: the thread it's in when the link says so
    (`?thread_ts=`), else the linked message itself."""
    match = _LINK_RE.search(link or "")
    if not match:
        return None
    channel, ts = match.group(1), f"{match.group(2)}.{match.group(3)}"
    thread = re.search(r"[?&]thread_ts=(\d+\.\d+)", link)
    return channel, thread.group(1) if thread else ts


def _identities() -> dict[str, WebClient]:
    """coolton's own users -> a client that can delete their messages."""
    pairs = (
        (os.environ.get("COOLTON_BOT_ID"), os.environ.get("SLACK_BOT_TOKEN")),
        (os.environ.get("COOLTON_USER_ID"), os.environ.get("SLACK_USER_TOKEN")),
        (os.environ.get("COOLTON_CODE_CHANNEL_BOT_ID", "U0C848QS7GU"), os.environ.get("SLACK_CODE_CHANNEL_BOT_TOKEN")),
    )
    return {user: WebClient(token=token) for user, token in pairs if user and token}


def _replies(reader: WebClient, channel: str, ts: str) -> list[dict]:
    messages, cursor = [], None
    while True:
        resp = reader.conversations_replies(channel=channel, ts=ts, limit=200, cursor=cursor)
        messages += resp.get("messages") or []
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return messages


def _delete(client: WebClient, channel: str, ts: str) -> str | None:
    """None once deleted (or already gone), else the Slack error."""
    for _ in range(5):
        try:
            client.chat_delete(channel=channel, ts=ts)
            return None
        except SlackApiError as e:
            error = e.response.get("error", "unknown")
            if error == "ratelimited":
                time.sleep(int(e.response.headers.get("Retry-After", "5")))
                continue
            return None if error == "message_not_found" else error
    return "ratelimited"


def rip_thread(channel: str, thread_ts: str) -> tuple[int, list[str]]:
    """Delete coolton's messages in the thread. Returns (deleted, problems)."""
    from agent.active_runs import is_run_active
    from agent.stop_store import request_stop

    # A run still going would keep posting into the thread being emptied.
    if is_run_active(channel, thread_ts):
        request_stop(channel, thread_ts)
    clients = _identities()
    reader = WebClient(token=os.environ.get("SLACK_USER_TOKEN") or os.environ.get("SLACK_BOT_TOKEN", ""))
    try:
        messages = _replies(reader, channel, thread_ts)
    except SlackApiError as e:
        return 0, [f"couldn't read the thread: {e.response.get('error', 'unknown')}"]

    deleted, problems = 0, []
    # Replies first and the parent last, so a coolton-started thread empties fully.
    for message in sorted(messages, key=lambda m: m.get("ts") == m.get("thread_ts", m.get("ts"))):
        client = clients.get(message.get("user") or "")
        if not client:
            continue
        error = _delete(client, channel, message["ts"])
        if error:
            problems.append(f"{message['ts']}: {error}")
        else:
            deleted += 1
        time.sleep(0.3)  # chat.delete allows about 50 a minute
    return deleted, problems


def handle_threadrip(client: WebClient, link: str | None, user_id: str, channel_id: str,
                     thread_ts: str, reply_ts: str | None) -> None:
    """Run a !threadrip and tell the admin (only them) how it went."""
    def tell(text: str) -> None:
        try:
            client.chat_postEphemeral(channel=channel_id, user=user_id, text=text, thread_ts=reply_ts or None)
        except Exception:
            logger.exception("!threadrip: couldn't report back")

    if link:
        target = thread_from_link(link)
        if not target:
            tell("`!threadrip` takes a Slack message link, like `https://….slack.com/archives/C…/p…`.")
            return
        channel, ts = target
    else:
        channel, ts = channel_id, thread_ts
        if not ts:
            tell("this isn't a thread. send `!threadrip` inside the thread, or give it a link to one.")
            return
    deleted, problems = rip_thread(channel, ts)
    text = f"deleted {deleted} of my messages in that thread."
    if problems:
        text += f" couldn't delete {len(problems)}: " + "; ".join(problems[:5])
    tell(text)
