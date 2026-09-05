"""coolton's ban list.

`is_banned` gates every turn (listeners.events.turn.run_agent_turn) — a banned
user's messages never start a run, on Slack or the web UI, since both funnel
through that one pipeline. The `!ban`/`!unban` command syntax that manages
this list lives here too: parsing and enforcement are two views of the same
data, and only agent.ban_store.BAN_ADMIN_USER_ID may issue either command
(listeners.events.message / app_mentioned check that before calling ban_user/
unban_user — this module doesn't enforce it itself, so it stays a pure store).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

BAN_STORE_FILE = "ban_store.json"
_lock = threading.Lock()

# Hardcoded, not configurable at runtime — only this Slack user may ban/unban.
BAN_ADMIN_USER_ID = "U0B2VTYER33"
# Every ban/unban is announced here regardless of which channel or DM the
# command was issued from.
ANNOUNCE_CHANNEL_ID = "C0BCNM6SQA0"

# Optionally preceded by an @-mention of the bot (with or without a space
# before "!ban" — Slack renders a mention as its own token, and users type
# both "<@BOT> !ban ..." and "<@BOT>!ban ..."). The target mention's id may
# carry a "|label" suffix ("<@U123|display name>") — Slack includes one on
# some client/message paths — so that's matched and discarded too, not just
# the bare "<@U123>" form. Reason is everything after the target mention,
# trimmed; DOTALL so a multi-line reason is captured whole.
_BAN_COMMAND_RE = re.compile(r"^!(ban|unban)\s+<@([A-Z0-9]+)(?:\|[^>]*)?>\s*(.*)$", re.IGNORECASE | re.DOTALL)


def _load() -> dict:
    try:
        with open(BAN_STORE_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    # A non-dict top-level value (corrupt/hand-edited file) must fall back to
    # "no bans" rather than raise AttributeError the next time something
    # calls .get() on it (e.g. is_banned) — mirrors the same defensive check
    # agent.scheduler / agent.policy_consent already make on their own stores.
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    temp = f"{BAN_STORE_FILE}.tmp"
    with open(temp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(temp, BAN_STORE_FILE)


def is_authorized(user_id: str) -> bool:
    return user_id == BAN_ADMIN_USER_ID


def ban_user(target_user_id: str, reason: str = "") -> None:
    with _lock:
        data = _load()
        data[target_user_id] = {"banned": True, "reason": reason, "updated_at": time.time()}
        _save(data)


def unban_user(target_user_id: str, reason: str = "") -> None:
    with _lock:
        data = _load()
        data[target_user_id] = {"banned": False, "reason": reason, "updated_at": time.time()}
        _save(data)


def is_banned(user_id: str) -> bool:
    with _lock:
        entry = _load().get(user_id)
    if not isinstance(entry, dict):
        return False
    return bool(entry.get("banned"))


def parse_ban_command(text: str, bot_id: str = "") -> tuple[str, str, str] | None:
    """Parse a `!ban <@U...> [reason]` / `!unban <@U...> [reason]` command,
    optionally preceded by an @-mention of the bot. Returns
    (action, target_user_id, reason) — action is "ban" or "unban", reason is
    "" if none was given — or None if `text` isn't one of these commands at
    all (a normal prompt that happens to mention "!ban" partway through must
    never match; the whole message, aside from a leading mention, must be it).
    """
    stripped = text.strip()
    if bot_id:
        mention = f"<@{bot_id}>"
        if stripped.startswith(mention):
            stripped = stripped[len(mention):].strip()
    match = _BAN_COMMAND_RE.match(stripped)
    if not match:
        return None
    action, target_user_id, reason = match.groups()
    return action.lower(), target_user_id, reason.strip()


def format_announcement(action: str, target_user_id: str, reason: str) -> str:
    verb = "banned" if action == "ban" else "unbanned"
    lines = [f"*A user has been {verb} from Coolton.*", "", f"*User:* <@{target_user_id}>"]
    if reason:
        lines.append(f"*Reason:* {reason}")
    return "\n".join(lines)


def apply_ban_command(client, action: str, target_user_id: str, reason: str) -> None:
    """Apply an already-parsed, already-authorized !ban/!unban and announce it
    in ANNOUNCE_CHANNEL_ID — the one place both listeners.events.message and
    listeners.events.app_mentioned land after checking is_authorized."""
    if action == "ban":
        ban_user(target_user_id, reason)
    else:
        unban_user(target_user_id, reason)
    client.chat_postMessage(
        channel=ANNOUNCE_CHANNEL_ID,
        text=format_announcement(action, target_user_id, reason),
    )
