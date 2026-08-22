import logging

from slack_bolt import Ack, BoltContext
from slack_sdk import WebClient

from listeners.actions.byok_actions import _notify_modal_failure

logger = logging.getLogger(__name__)


def handle_mcp_server_add(ack: Ack, body: dict, client: WebClient, context: BoltContext):
    ack()
    try:
        from listeners.views.mcp_server_views import build_add_mcp_server_modal
        client.views_open(trigger_id=body["trigger_id"], view=build_add_mcp_server_modal())
    except Exception as e:
        logger.exception("Failed to open Add MCP Server modal: %s", e)
        _notify_modal_failure(client, context.user_id)


def handle_mcp_server_delete(ack: Ack, body: dict, client: WebClient, context: BoltContext):
    ack()
    try:
        from agent.mcp_server_store import delete_server
        user_id = context.user_id
        action_id = body["actions"][0]["action_id"]
        server_id = action_id.replace("mcp_server_delete_", "")
        delete_server(user_id, server_id)
        client.chat_postEphemeral(channel=user_id, user=user_id, text="MCP server removed.")
    except Exception as e:
        logger.exception("Failed to delete MCP server: %s", e)


def mcp_server_delete_pattern(ack, body, client, context, logger):
    handle_mcp_server_delete(ack, body, client, context)
