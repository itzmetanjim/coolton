import logging

logger = logging.getLogger(__name__)


def _pt(text: str) -> dict:
    return {"type": "plain_text", "text": text}


_TRANSPORT_OPTIONS = [
    {"text": _pt("Auto-detect"), "value": "auto"},
    {"text": _pt("Streamable HTTP"), "value": "http"},
    {"text": _pt("SSE"), "value": "sse"},
]


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
                    "text": "coolton will connect to this server's tools every time you message it. Any "
                    "remote MCP server works, over Streamable HTTP or SSE. A local (stdio) server has to "
                    "be put behind an HTTP bridge first (e.g. `npx supergateway` or `mcp-proxy`).",
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
                "block_id": "server_transport",
                "label": _pt("Transport"),
                "element": {
                    "type": "static_select", "action_id": "value",
                    "initial_option": _TRANSPORT_OPTIONS[0],
                    "options": _TRANSPORT_OPTIONS,
                },
                "hint": _pt("Auto-detect tries both and keeps whichever the server answers."),
            },
            {
                "type": "input",
                "block_id": "server_token",
                "label": _pt("Bearer token (optional)"),
                "element": {"type": "plain_text_input", "action_id": "value", "placeholder": _pt("Leave blank if the server needs no auth")},
                "optional": True,
            },
            {
                "type": "input",
                "block_id": "server_headers",
                "label": _pt("Extra headers (optional)"),
                "element": {"type": "plain_text_input", "action_id": "value", "multiline": True,
                            "placeholder": _pt("One per line, e.g. X-API-Key: abc123")},
                "hint": _pt("For servers that take an API key in a header instead of a bearer token. Stored encrypted."),
                "optional": True,
            },
        ],
    }


def handle_mcp_server_add_submit(ack, body, client, context, logger):
    ack()
    user_id = context.user_id
    try:
        from agent.mcp_server_store import add_server, parse_headers, probe_server

        values = body["view"]["state"]["values"]
        name = values["server_name"]["value"]["value"]
        url = values["server_url"]["value"]["value"]
        token = values["server_token"]["value"].get("value") or ""
        transport = ((values.get("server_transport", {}).get("value", {}).get("selected_option") or {})
                     .get("value") or "auto")
        headers = parse_headers(values.get("server_headers", {}).get("value", {}).get("value") or "")

        ok, detail, transport = probe_server(url, token, transport, headers)
        if not ok:
            client.chat_postEphemeral(
                channel=user_id, user=user_id,
                text=f"Couldn't connect to that MCP server, not saved: {detail}",
            )
            return

        add_server(user_id, name, url, token, transport, headers)
        client.chat_postEphemeral(channel=user_id, user=user_id, text=f"Added MCP server: {name} ({detail}).")
    except Exception as e:
        logger.exception("Failed to add MCP server: %s", e)
        client.chat_postEphemeral(channel=user_id, user=user_id, text=f"Error adding MCP server: {str(e)}")
