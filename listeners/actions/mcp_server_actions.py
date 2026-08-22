import logging
import time

from slack_bolt import Ack, BoltContext
from slack_sdk import WebClient

from listeners.actions.byok_actions import _notify_modal_failure

logger = logging.getLogger(__name__)


def handle_mcp_server_add(ack: Ack, body: dict, client: WebClient, context: BoltContext):
    ack()
    try:
        # Diagnostic: trigger_ids expire ~3s after the click. If this handler is
        # already old by the time it starts, the delay is upstream (event dispatch),
        # not in anything below — narrows down repeated "expired_trigger_id" reports.
        action_ts = (body.get("actions") or [{}])[0].get("action_ts")
        if action_ts:
            age = time.time() - float(action_ts)
            if age > 1.5:
                logger.warning("mcp_server_add: handler started %.2fs after the click (trigger_id expires ~3s)", age)

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
