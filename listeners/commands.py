"""`!help` and `!connections`: chat commands answered ephemerally, like `!stop`,
without starting a turn."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

COMMANDS = ("!help", "!connections")


def parse_command(text: str, bot_id: str = "") -> str | None:
    """The command if `text` is exactly one (optionally after an @mention of
    the bot), else None — a message merely mentioning "!help" isn't one."""
    stripped = (text or "").strip()
    mention = f"<@{bot_id}>" if bot_id else ""
    if mention and stripped.startswith(mention):
        stripped = stripped[len(mention):].strip()
    return stripped if stripped in COMMANDS else None


def help_text() -> str:
    return "\n".join([
        "*hey, i'm coolton.* mention me in a thread or DM me with anything: questions, code, research, "
        "files, or a hand with a task.",
        "",
        "*commands*",
        "• `!help`: this list",
        "• `!stop`: stop everything i'm running in this thread",
        "• `!connections`: your MCP servers and model endpoints, with their status",
        "",
        "*in a message*",
        "• `[!WITH:tag]`: force a class of model for that turn, e.g. `[!WITH:vision]`",
        "• `[!DEBUG]`: after the reply, get a breakdown of where the time went",
        "• `[!FAST]`: answer quickly instead of researching carefully",
        "• start a message with `##` and i'll ignore it",
        "",
        "custom instructions, your own models (BYOK) and MCP servers are in my *Home* tab.",
    ])


def connections_text(user_id: str) -> str:
    lines = ["*your connections*"]

    try:
        from agent.mcp_health import get_cached_health

        health = get_cached_health()
        if health.get("healthy") is True:
            lines.append("• Slack MCP server: connected")
        elif health.get("healthy") is False:
            lines.append(f"• Slack MCP server: down ({health.get('last_detail') or 'unknown error'})")
        else:
            lines.append("• Slack MCP server: not checked yet")
    except Exception:
        logger.exception("!connections: couldn't read Slack MCP health")
        lines.append("• Slack MCP server: couldn't check right now")

    lines.append("• Context7 (library docs): built in")

    try:
        from agent.mcp_server_store import get_user_servers

        servers = get_user_servers(user_id)
        if servers:
            lines += [f"• MCP server *{s['name']}*: {s['url']}" for s in servers]
        else:
            lines.append("• your MCP servers: none yet")
    except Exception:
        logger.exception("!connections: couldn't list MCP servers for %s", user_id)
        lines.append("• your MCP servers: couldn't read them right now")

    try:
        from agent.byok_store import get_image_endpoint_id, get_text_endpoint_id, get_user_endpoints

        endpoints = get_user_endpoints(user_id)
        text_id, image_id = get_text_endpoint_id(user_id), get_image_endpoint_id(user_id)
        for ep in endpoints:
            uses = [use for use, active in (("text", ep["id"] == text_id), ("images", ep["id"] == image_id)) if active]
            used = f", used for {' and '.join(uses)}" if uses else ", not in use"
            lines.append(f"• your model *{ep['name']}*: `{ep['model']}`{used}")
        if not endpoints:
            lines.append("• your own models (BYOK): none")
    except Exception:
        logger.exception("!connections: couldn't list BYOK endpoints for %s", user_id)
        lines.append("• your own models (BYOK): couldn't read them right now")

    lines += ["", "manage these from my *Home* tab."]
    return "\n".join(lines)


def respond(client, command: str, channel_id: str, user_id: str, thread_ts: str | None) -> None:
    text = help_text() if command == "!help" else connections_text(user_id)
    try:
        client.chat_postEphemeral(channel=channel_id, user=user_id, text=text, thread_ts=thread_ts or None)
    except Exception:
        logger.exception("Failed to post %s reply", command)
