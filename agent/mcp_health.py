"""Background health check for the Slack MCP Server connection.

coolton leans on Slack's hosted MCP server (https://mcp.slack.com/mcp) for a large
slice of its Slack tools (canvas read/write, message drafts, scheduling, reactions,
profile lookups — see the "SLACK MCP SERVER" section of the system prompt in
agent/platforms/slack.py). If that connection goes away, most of coolton's Slack-native
capability silently disappears mid-conversation with no user-visible error
(`toolsets()` just returns `[]`), so this pings it on a schedule and DMs the admin the
moment it stops answering, instead of waiting for someone to notice the bot got dumber.
"""

import asyncio
import json
import logging
import os
import tempfile
import threading
import time

from agent.admin_alerts import notify_admin

logger = logging.getLogger(__name__)

MCP_HEALTH_FILE = os.environ.get("MCP_HEALTH_FILE", "mcp_health.json")
_lock = threading.Lock()

REFRESH_INTERVAL_SECONDS = 600  # 10 minutes
_STILL_DOWN_REMINDER_SECONDS = 2 * 60 * 60  # re-alert every 2h while still down


def _load() -> dict:
    if not os.path.exists(MCP_HEALTH_FILE):
        return {}
    try:
        with open(MCP_HEALTH_FILE, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    directory = os.path.dirname(os.path.abspath(MCP_HEALTH_FILE)) or "."
    fd, temp_file = tempfile.mkstemp(prefix=".mcp-health-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(temp_file, MCP_HEALTH_FILE)
    finally:
        if os.path.exists(temp_file):
            os.unlink(temp_file)


async def _probe() -> tuple[bool, str]:
    token = os.environ.get("SLACK_USER_TOKEN")
    if not token:
        return False, "SLACK_USER_TOKEN not configured"

    from pydantic_ai.mcp import MCPToolset, StreamableHttpTransport

    transport = StreamableHttpTransport(
        "https://mcp.slack.com/mcp",
        headers={"Authorization": f"Bearer {token}"},
    )
    toolset = MCPToolset(transport)
    try:
        async with toolset:
            tools = await toolset.list_tools()
        if not tools:
            return False, "connected but returned zero tools"
        return True, ""
    except Exception as e:
        return False, str(e)


def check_mcp_health() -> tuple[bool, str]:
    """Synchronous wrapper: probes the Slack MCP Server right now. Returns (healthy, detail)."""
    return asyncio.run(_probe())


def refresh_mcp_health() -> None:
    """Probe the Slack MCP Server and DM the admin on a healthy<->down transition, or
    periodically while it stays down so an outage doesn't get forgotten about."""
    healthy, detail = check_mcp_health()
    now = time.time()

    should_alert = False
    message = ""
    with _lock:
        state = _load()
        was_healthy = state.get("healthy")
        last_alerted = state.get("last_alerted", 0.0)

        if healthy and was_healthy is False:
            should_alert = True
            message = (
                "🟢 Slack MCP Server is back up. coolton's full Slack toolset "
                "(canvas, drafts, scheduling, etc) is available again."
            )
        elif not healthy and was_healthy is not False:
            should_alert = True
            message = f"🔴 Slack MCP Server is DOWN — most of coolton's Slack tools just disappeared. Reason: {detail}"
        elif not healthy and was_healthy is False and now - last_alerted > _STILL_DOWN_REMINDER_SECONDS:
            should_alert = True
            message = f"🔴 Slack MCP Server is STILL down. Reason: {detail}"

        state["healthy"] = healthy
        state["last_checked"] = now
        state["last_detail"] = detail
        if should_alert:
            state["last_alerted"] = now
        _save(state)

    if should_alert:
        logger.warning("MCP health transition, notifying admin: %s", message)
        notify_admin(message)
