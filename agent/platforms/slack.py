"""Slack adapter for the platform-independent agent runtime."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from pydantic_ai.mcp import MCPToolset, StreamableHttpTransport

from agent.platform import PlatformAdapter

logger = logging.getLogger(__name__)

# The prompt itself lives in system_prompt.md (next to this file) rather than as an
# inline f-string — kept as one plain markdown document instead of fragmented across
# Python string literals (backslash line-continuations, escaped braces to keep code
# examples literal, etc). Loaded once at import time; the two `${...}` placeholders
# below are its only per-deployment substitution.
#
# NOTE: moving it here fixed a real bug, not just cosmetics — the old inline f-string
# nested a Python `\n` inside an ASCII-art code EXAMPLE meant to be shown literally to
# the model; the outer f-string's own escape processing silently turned that `\n` into
# a real embedded newline, corrupting the example. Plain text has no such double
# interpretation. Otherwise keep this file's content stable turn to turn — an actually
# changed system prompt breaks provider-side prompt caching for every open thread.
_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "system_prompt.md"


def _load_system_prompt() -> str:
    text = _SYSTEM_PROMPT_PATH.read_text()
    text = text.replace("${COOLTON_BOT_ID}", os.environ.get("COOLTON_BOT_ID", ""))
    text = text.replace("${COOLTON_USER_ID}", os.environ.get("COOLTON_USER_ID", ""))
    return text


SYSTEM_PROMPT = _load_system_prompt()


class SlackPlatform(PlatformAdapter):
    name = "slack"

    def __init__(self, client: Any = None):
        self.client = client

    @property
    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def format_user_message(self, text: str, deps: Any) -> str:
        uid = deps.user_id or "unknown"
        name = self.display_name(uid)
        return f"{uid} ({name}):\n{text}"

    def display_name(self, user_id: str) -> str:
        if not user_id or not self.client:
            return user_id or "unknown"
        try:
            resp = self.client.users_info(user=user_id)
            if resp.get("ok"):
                profile = resp["user"].get("profile", {})
                return profile.get("display_name") or profile.get("real_name") or resp["user"].get("name") or user_id
        except Exception:
            pass
        return user_id

    def build_context_prompt(self, deps: Any) -> str:
        # Stable for every turn of this thread — safe inside the cached system
        # prompt. Nothing here may vary turn to turn (see build_turn_context).
        return f"""\n## CURRENT CONTEXT
- You are in channel_id: `{deps.channel_id}` (thread_ts: `{deps.thread_ts}` if in thread, else DM)
- Use this channel_id for operations in the current channel unless user specifies otherwise
- Your user_id (the HUMAN who messaged you): `{deps.user_id}`
- Your own bot user id (this is YOU, not a third party): `{os.environ.get("COOLTON_BOT_ID", "")}`
- Your cooltonUser helper account id (acts on your behalf): `{os.environ.get("COOLTON_USER_ID", "")}`
"""

    def build_turn_context(self, deps: Any, model: str, is_vision: bool) -> str:
        # Changes on every turn (message_ts is unique per message; model/capability
        # can shift turn to turn with the provider fallback chain) — deliberately
        # NOT part of the system prompt, which must stay byte-identical across
        # turns for prompt caching to actually cache anything.
        capability = (
            "VISION-capable: attached images are visible to you, and you can call `see_image_from_sandbox` to view images in your sandbox."
            if is_vision else
            "NOT vision-capable: you cannot see images directly; download them to your sandbox and use `analyze_image`."
        )
        return f"""[Turn context — message timestamp: `{deps.message_ts}`, model: {model or "unknown"} ({capability})]

"""

    def toolsets(self, deps: Any) -> list[Any]:
        toolsets: list[Any] = []

        token = deps.user_token or os.environ.get("SLACK_USER_TOKEN")
        if not token:
            logger.info("Slack MCP Server disabled (no user_token)")
            from agent.admin_alerts import notify_admin
            notify_admin(
                "🔴 Slack MCP Server has no token configured (SLACK_USER_TOKEN unset) — "
                "most of coolton's Slack tools are unavailable this turn.",
                dedupe_key="mcp_no_token", min_interval_seconds=1800,
            )
        else:
            logger.info("Slack MCP Server enabled (user_token present)")
            try:
                transport = StreamableHttpTransport(
                    "https://mcp.slack.com/mcp",
                    headers={"Authorization": f"Bearer {token}"},
                )
                toolsets.append(MCPToolset(transport))
            except Exception as e:
                logger.exception("Failed to create MCP server")
                from agent.admin_alerts import notify_admin
                notify_admin(
                    f"🔴 Slack MCP Server toolset failed to construct: {e} — "
                    "most of coolton's Slack tools are unavailable this turn.",
                    dedupe_key="mcp_construct_error", min_interval_seconds=1800,
                )

        toolsets.extend(self._user_mcp_toolsets(getattr(deps, "user_id", None)))
        return toolsets

    def _user_mcp_toolsets(self, user_id: str | None) -> list[Any]:
        """Any MCP servers this user registered from App Home (see
        agent/mcp_server_store.py). One broken server is dropped, not fatal —
        it never blocks the Slack MCP toolset or another user's servers."""
        if not user_id:
            return []
        try:
            from agent.mcp_server_store import get_server_decrypted, get_user_servers
            servers = get_user_servers(user_id)
        except Exception:
            logger.exception("Failed to load user MCP servers for %s", user_id)
            return []

        result: list[Any] = []
        for meta in servers:
            server = get_server_decrypted(user_id, meta["id"])
            if not server:
                continue
            try:
                headers = {"Authorization": f"Bearer {server['token']}"} if server.get("token") else {}
                transport = StreamableHttpTransport(server["url"], headers=headers)
                result.append(MCPToolset(transport, id=f"user_mcp_{server['id']}"))
            except Exception:
                logger.exception("Failed to build user MCP toolset %s (%s) for %s", server["id"], server["name"], user_id)
        return result
