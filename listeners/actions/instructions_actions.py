import logging
import threading

from slack_bolt import Ack, BoltContext
from slack_sdk import WebClient

from listeners.views.instructions_views import build_instructions_modal
from agent.json_file import load_json, save_json

logger = logging.getLogger(__name__)

INSTRUCTIONS_FILE = "custom_instructions.json"
instructions_lock = threading.Lock()


def _load_instructions() -> dict:
    return load_json(INSTRUCTIONS_FILE)


def _save_instructions(data: dict):
    save_json(INSTRUCTIONS_FILE, data)


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
    user_id = context.user_id
    try:
        current = get_user_instructions(user_id)
        modal = build_instructions_modal(current)
        client.views_open(
            trigger_id=body["trigger_id"],
            view=modal,
        )
    except Exception as e:
        logger.exception("Failed to open instructions modal: %s", e)
        if user_id:
            try:
                client.chat_postEphemeral(channel=user_id, user=user_id, text="Couldn't open that — please try again.")
            except Exception:
                pass


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
