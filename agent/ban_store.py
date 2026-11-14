"""coolton's ban list.

`is_banned` gates every turn (listeners.events.turn.run_agent_turn) — a banned
user's messages never start a run, on Slack or the web UI, since both funnel
through that one pipeline. The `!ban`/`!unban` command syntax that manages
this list lives here too: parsing and enforcement are two views of the same
data, and only an admin (agent.admin_alerts.ADMIN_USER_IDS) may issue either command
(listeners.events.message / app_mentioned check that before calling ban_user/
unban_user — this module doesn't enforce it itself, so it stays a pure store).
"""

from __future__ import annotations

import re
import threading
import time

from agent.admin_alerts import is_admin
from agent.json_file import load_json, save_json

BAN_STORE_FILE = "ban_store.json"
_lock = threading.Lock()

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

# A ban's length, at the end of its reason: `!duration 7d` (s, m, h, d or w), with or
# without the [brackets] the help shows it in.
_DURATION_RE = re.compile(r"\s*\[?\s*!duration\s+(\d+(?:\.\d+)?)\s*([smhdw])\s*\]?\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_UNIT_NAMES = {"s": "second", "m": "minute", "h": "hour", "d": "day", "w": "week"}


def split_duration(reason: str) -> tuple[str, float | None, str]:
    """(reason without the `!duration`, its length in seconds or None, a readable length
    like "7 days" or "")."""
    match = _DURATION_RE.search(reason or "")
    if not match:
        return (reason or "").strip(), None, ""
    amount, unit = float(match.group(1)), match.group(2).lower()
    shown = f"{amount:g} {_UNIT_NAMES[unit]}{'' if amount == 1 else 's'}"
    return reason[:match.start()].strip(), amount * _UNIT_SECONDS[unit], shown


def _load() -> dict:
    return load_json(BAN_STORE_FILE)


def _save(data: dict) -> None:
    save_json(BAN_STORE_FILE, data)


def is_authorized(user_id: str) -> bool:
    return is_admin(user_id)


def ban_user(target_user_id: str, reason: str = "", until: float | None = None) -> None:
    """Ban `target_user_id`, until the `until` timestamp when one is given, else for good."""
    with _lock:
        data = _load()
        entry = {"banned": True, "reason": reason, "updated_at": time.time()}
        if until is not None:
            entry["until"] = until
        data[target_user_id] = entry
        _save(data)


def unban_user(target_user_id: str, reason: str = "") -> None:
    with _lock:
        data = _load()
        data[target_user_id] = {"banned": False, "reason": reason, "updated_at": time.time()}
        _save(data)


def is_banned(user_id: str) -> bool:
    with _lock:
        entry = _load().get(user_id)
    if not isinstance(entry, dict) or not entry.get("banned"):
        return False
    until = entry.get("until")
    return not isinstance(until, (int, float)) or until > time.time()


def parse_ban_command(text: str, bot_id: str = "") -> tuple[str, str, str] | None:
    """Parse a `!ban <@U...> [reason] [!duration 7d]` / `!unban <@U...> [reason]` command,
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


def format_announcement(action: str, target_user_id: str, reason: str,
                        until: float | None = None, length: str = "") -> str:
    verb = "banned" if action == "ban" else "unbanned"
    lines = [f"*A user has been {verb} from Coolton.*", "", f"*User:* <@{target_user_id}>"]
    if reason:
        lines.append(f"*Reason:* {reason}")
    if action == "ban":
        if until is None:
            lines.append("*Duration:* permanent")
        else:
            ends = int(until)
            lines.append(f"*Duration:* {length}, until <!date^{ends}^{{date_short_pretty}} at {{time}}|{length} from now>")
    return "\n".join(lines)


def apply_ban_command(client, action: str, target_user_id: str, reason: str) -> None:
    """Apply an already-parsed, already-authorized !ban/!unban and announce it
    in ANNOUNCE_CHANNEL_ID — the one place both listeners.events.message and
    listeners.events.app_mentioned land after checking is_authorized."""
    reason, seconds, length = split_duration(reason)
    until = time.time() + seconds if action == "ban" and seconds else None
    if action == "ban":
        ban_user(target_user_id, reason, until)
    else:
        unban_user(target_user_id, reason)
    client.chat_postMessage(
        channel=ANNOUNCE_CHANNEL_ID,
        text=format_announcement(action, target_user_id, reason, until, length),
    )
