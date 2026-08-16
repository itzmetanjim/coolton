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


def build_opt_in_blocks(pending_id: str) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "before using Coolton, you need to opt in to the Coolton policy. you can join the `#coolton` channel or opt in without joining it."}},
        {"type": "actions", "elements": [
            {"type": "button", "action_id": "policy_opt_in_join", "value": pending_id, "style": "primary", "text": {"type": "plain_text", "text": "opt in and join channel"}},
            {"type": "button", "action_id": "policy_opt_in_no_join", "value": pending_id, "text": {"type": "plain_text", "text": "opt in without joining channel"}},
        ]},
    ]
