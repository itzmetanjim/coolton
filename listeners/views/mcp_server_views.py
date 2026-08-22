import logging

logger = logging.getLogger(__name__)


def _pt(text: str) -> dict:
    return {"type": "plain_text", "text": text}


def build_add_mcp_server_modal() -> dict:
    return {
        "type": "modal",
        "callback_id": "mcp_server_add_submit",
        "title": _pt("Add MCP Server"),
        "submit": _pt("Add"),
        "close": _pt("Cancel"),
        "blocks": [
            {"type": "header", "text": _pt("Add an MCP Server")},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "coolton will connect to this server's tools every time you message it. "
                    "Must be a Streamable HTTP MCP server (SSE/stdio not supported).",
                },
            },
            {"type": "divider"},
            {
                "type": "input",
                "block_id": "server_name",
                "label": _pt("Name"),
                "element": {"type": "plain_text_input", "action_id": "value", "placeholder": _pt("e.g. My Notion, My Linear")},
            },
            {
                "type": "input",
                "block_id": "server_url",
                "label": _pt("Server URL"),
                "element": {"type": "plain_text_input", "action_id": "value", "placeholder": _pt("https://mcp.example.com/mcp")},
            },
            {
                "type": "input",
                "block_id": "server_token",
                "label": _pt("Bearer token (optional)"),
                "element": {"type": "plain_text_input", "action_id": "value", "placeholder": _pt("Leave blank if the server needs no auth")},
                "optional": True,
            },
        ],
    }


def handle_mcp_server_add_submit(ack, body, client, context, logger):
    ack()
    user_id = context.user_id
    try:
        from agent.mcp_server_store import add_server, probe_server

        values = body["view"]["state"]["values"]
        name = values["server_name"]["value"]["value"]
        url = values["server_url"]["value"]["value"]
        token = values["server_token"]["value"].get("value") or ""

        ok, detail = probe_server(url, token)
        if not ok:
            client.chat_postEphemeral(
                channel=user_id, user=user_id,
                text=f"Couldn't connect to that MCP server, not saved: {detail}",
            )
            return

        add_server(user_id, name, url, token)
        client.chat_postEphemeral(channel=user_id, user=user_id, text=f"Added MCP server: {name} ({detail}).")
    except Exception as e:
        logger.exception("Failed to add MCP server: %s", e)
        client.chat_postEphemeral(channel=user_id, user=user_id, text=f"Error adding MCP server: {str(e)}")
