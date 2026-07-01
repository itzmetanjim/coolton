import json
import os
import logging
import threading

from slack_bolt import Ack, BoltContext
from slack_sdk import WebClient

from listeners.views.instructions_views import build_instructions_modal

logger = logging.getLogger(__name__)

INSTRUCTIONS_FILE = "custom_instructions.json"
instructions_lock = threading.Lock()


def _load_instructions() -> dict:
    if not os.path.exists(INSTRUCTIONS_FILE):
        return {}
    with open(INSTRUCTIONS_FILE, "r") as f:
        return json.load(f)


def _save_instructions(data: dict):
    temp = f"{INSTRUCTIONS_FILE}.tmp"
    with open(temp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(temp, INSTRUCTIONS_FILE)


def get_user_instructions(user_id: str) -> str:
    with instructions_lock:
        data = _load_instructions()
        return data.get(user_id, "")


def set_user_instructions(user_id: str, instructions: str):
    with instructions_lock:
        data = _load_instructions()
        if instructions.strip():
            data[user_id] = instructions.strip()
        else:
            data.pop(user_id, None)
        _save_instructions(data)


def handle_instructions_open(ack: Ack, body: dict, client: WebClient, context: BoltContext):
    ack()
    try:
        user_id = context.user_id
        current = get_user_instructions(user_id)
        modal = build_instructions_modal(current)
        client.views_open(
            trigger_id=body["trigger_id"],
            view=modal,
        )
    except Exception as e:
        logger.exception("Failed to open instructions modal: %s", e)


def handle_instructions_clear(ack: Ack, body: dict, client: WebClient, context: BoltContext):
    ack()
    try:
        user_id = context.user_id
        set_user_instructions(user_id, "")
        client.chat_postEphemeral(
            channel=context.channel_id or user_id,
            user=user_id,
            text="Cleared your custom instructions.",
        )
    except Exception as e:
        logger.exception("Failed to clear instructions: %s", e)
