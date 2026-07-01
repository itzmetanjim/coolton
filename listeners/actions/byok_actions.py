import logging

from slack_bolt import Ack, BoltContext
from slack_sdk import WebClient

logger = logging.getLogger(__name__)


def handle_byok_add(ack: Ack, body: dict, client: WebClient, context: BoltContext):
    ack()
    try:
        from listeners.views.byok_views import build_add_endpoint_modal
        client.views_open(trigger_id=body["trigger_id"], view=build_add_endpoint_modal())
    except Exception as e:
        logger.exception("Failed to open Add Endpoint modal: %s", e)


def handle_byok_delete(ack: Ack, body: dict, client: WebClient, context: BoltContext):
    ack()
    try:
        from agent.byok_store import delete_endpoint
        user_id = context.user_id
        action_id = body["actions"][0]["action_id"]
        ep_id = action_id.replace("byok_delete_", "")
        delete_endpoint(user_id, ep_id)
        client.chat_postEphemeral(channel=user_id, user=user_id, text="Endpoint deleted.")
    except Exception as e:
        logger.exception("Failed to delete BYOK endpoint: %s", e)


def handle_byok_edit(ack: Ack, body: dict, client: WebClient, context: BoltContext):
    ack()
    try:
        from agent.byok_store import get_endpoint_decrypted
        from listeners.views.byok_views import build_edit_endpoint_modal
        user_id = context.user_id
        action_id = body["actions"][0]["action_id"]
        ep_id = action_id.replace("byok_edit_", "")
        ep = get_endpoint_decrypted(user_id, ep_id)
        if not ep:
            client.chat_postEphemeral(channel=user_id, user=user_id, text="Endpoint not found.")
            return
        client.views_open(trigger_id=body["trigger_id"], view=build_edit_endpoint_modal(ep))
    except Exception as e:
        logger.exception("Failed to open Edit Endpoint modal: %s", e)


def handle_byok_select_text(ack: Ack, body: dict, client: WebClient, context: BoltContext):
    ack()
    try:
        from agent.byok_store import set_text_endpoint
        user_id = context.user_id
        selected = body["actions"][0]["selected_option"]["value"]
        ep_id = None if selected == "none" else selected
        set_text_endpoint(user_id, ep_id)
        label = "global key (no BYOK)" if ep_id is None else selected
        client.chat_postEphemeral(channel=user_id, user=user_id, text=f"Default text model updated to: {label}")
    except Exception as e:
        logger.exception("Failed to set text endpoint: %s", e)


def handle_byok_select_image(ack: Ack, body: dict, client: WebClient, context: BoltContext):
    ack()
    try:
        from agent.byok_store import set_image_endpoint
        user_id = context.user_id
        selected = body["actions"][0]["selected_option"]["value"]
        ep_id = None if selected == "none" else selected
        set_image_endpoint(user_id, ep_id)
        label = "disabled" if ep_id is None else selected
        client.chat_postEphemeral(channel=user_id, user=user_id, text=f"Default image model updated to: {label}")
    except Exception as e:
        logger.exception("Failed to set image endpoint: %s", e)


def byok_edit_pattern(ack, body, client, context, logger):
    handle_byok_edit(ack, body, client, context)


def byok_delete_pattern(ack, body, client, context, logger):
    handle_byok_delete(ack, body, client, context)
