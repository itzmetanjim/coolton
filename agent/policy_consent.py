"""Persist policy consent and pending first-use requests."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

POLICY_CHANNEL_ID = "C0BCNM6SQA0"
_STORE_PATH = Path("policy_consents.json")
_LOCK = threading.Lock()


def _load() -> dict:
    if not _STORE_PATH.exists():
        return {"consents": {}, "pending": {}}
    try:
        data = json.loads(_STORE_PATH.read_text())
        if not isinstance(data, dict):
            return {"consents": {}, "pending": {}}
        return {"consents": data.get("consents", {}), "pending": data.get("pending", {})}
    except (OSError, ValueError):
        return {"consents": {}, "pending": {}}


def _save(data: dict) -> None:
    temporary = _STORE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(temporary, _STORE_PATH)


def user_is_in_policy_channel(client, user_id: str) -> bool:
    try:
        response = client.conversations_members(channel=POLICY_CHANNEL_ID, limit=1000)
        if not isinstance(response, dict):
            return False
        return user_id in response.get("members", [])
    except Exception:
        return False


def has_consent(user_id: str) -> bool:
    with _LOCK:
        return user_id in _load()["consents"]


def record_consent(user_id: str, joined_policy_channel: bool = False) -> None:
    with _LOCK:
        data = _load()
        data["consents"][user_id] = {
            "timestamp": time.time(),
            "joined_policy_channel": joined_policy_channel,
        }
        _save(data)


def revoke_consent(user_id: str) -> None:
    with _LOCK:
        data = _load()
        data["consents"].pop(user_id, None)
        _save(data)


def save_pending(payload: dict) -> str:
    pending_id = uuid.uuid4().hex
    with _LOCK:
        data = _load()
        data["pending"][pending_id] = {**payload, "created_at": time.time()}
        _save(data)
    return pending_id


def pop_pending(pending_id: str) -> dict | None:
    with _LOCK:
        data = _load()
        payload = data["pending"].pop(pending_id, None)
        if payload is not None:
            _save(data)
        return payload


def clear_pending_for_user(user_id: str) -> None:
    """Drop every unanswered opt-in prompt for a user once they consent.

    Stale prompts linger in Slack (one per pre-consent message), so clearing
    them here stops a later click on an old button from replaying an old
    message.
    """
    with _LOCK:
        data = _load()
        remaining = {
            pid: payload
            for pid, payload in data["pending"].items()
            if payload.get("user_id") != user_id
        }
        if len(remaining) != len(data["pending"]):
            data["pending"] = remaining
            _save(data)


def ensure_consent(
    client, say, *, user_id: str, channel_id: str, thread_ts: str, message_ts: str,
) -> bool:
    """Check/record policy consent for an incoming message; prompt to opt in if needed.

    Shared by the message and app_mention handlers so the opt-in flow can't drift
    between them. Returns True if the caller should proceed handling the message,
    False if an opt-in prompt was sent instead (the caller must return without
    processing). Opting in does not replay the message that triggered the prompt —
    the user just gets a confirmation and can ask again (see handle_policy_opt_in).
    """
    if user_is_in_policy_channel(client, user_id):
        record_consent(user_id, joined_policy_channel=True)
        return True
    if has_consent(user_id):
        return True
    pending_id = save_pending({
        "user_id": user_id, "channel_id": channel_id, "thread_ts": thread_ts,
        "message_ts": message_ts,
    })
    say(text="you need to opt in to the Coolton policy:",
        blocks=build_opt_in_blocks(pending_id), thread_ts=thread_ts)
    return False


def build_opt_in_blocks(pending_id: str) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "before using Coolton, you need to opt in to the Coolton policy. you can join the `#coolton` channel or opt in without joining it."}},
        {"type": "actions", "elements": [
            {"type": "button", "action_id": "policy_opt_in_join", "value": pending_id, "style": "primary", "text": {"type": "plain_text", "text": "opt in and join channel"}},
            {"type": "button", "action_id": "policy_opt_in_no_join", "value": pending_id, "text": {"type": "plain_text", "text": "opt in without joining channel"}},
        ]},
    ]
