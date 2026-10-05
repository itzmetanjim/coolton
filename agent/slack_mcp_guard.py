"""The Slack MCP toolset (mcp.slack.com, running as cooltonUser), with
coolton's read and attribution rules applied to each call.

The hosted server's tools can read anything cooltonUser can see and post as
cooltonUser, so each call goes through the same rules as coolton's own Slack
tools:

- reads of a channel, thread, file, canvas, or list follow agent.slack_access
  (the current conversation, or public channels/files, only);
- anything that puts a new post in front of other people (scheduled
  messages, file shares, new lists) carries the "(sent from <@user>)"
  footer (agent.attribution). Canvases don't: a canvas is one document
  edited over and over, so a footer per edit just piled up inside it
  (headings included), and Slack already records who edited it;
- slack_send_message (no way to force the footer onto an immediate post —
  chat_postMessage does that instead) and slack_search_public_and_private (its
  whole point is searching private channels and DMs) are hidden entirely.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.toolsets import WrapperToolset

from agent.attribution import attribute_text, attribution_user_id

BLOCKED_SLACK_MCP_TOOLS = {"slack_send_message", "slack_search_public_and_private"}

# Tool -> the arg naming the channel it reads.
_CHANNEL_READ_TOOLS = {
    "slack_read_channel": "channel_id",
    "slack_read_thread": "channel_id",
    "slack_list_channel_members": "channel_id",
    "slack_get_reactions": "channel_id",
}

# Tool -> the arg naming the file (canvases and lists are files too) it reads.
_FILE_READ_TOOLS = {
    "slack_read_file": "file_id",
    "slack_read_canvas": "canvas_id",
    "slack_read_list": "list_id",
}


def guard_args(name: str, args: dict[str, Any], deps: Any) -> tuple[dict[str, Any] | None, str | None]:
    """(args to call the tool with, None), or (None, error to return instead)."""
    from agent.slack_access import channel_read_error, file_read_error

    current = getattr(deps, "channel_id", "") or ""
    if name in BLOCKED_SLACK_MCP_TOOLS:
        return None, f"Error: {name} isn't available to coolton."
    if name in _CHANNEL_READ_TOOLS:
        denied = channel_read_error(str(args.get(_CHANNEL_READ_TOOLS[name]) or ""), current)
        if denied:
            return None, f"Error: {name} refused — {denied}"
    if name in _FILE_READ_TOOLS:
        file_id = str(args.get(_FILE_READ_TOOLS[name]) or "")
        if not file_id:
            return None, f"Error: {name} needs an explicit {_FILE_READ_TOOLS[name]} (looking one up by name isn't allowed)."
        denied = file_read_error(file_id, current, getattr(deps, "user_id", "") or "")
        if denied:
            return None, f"Error: {name} refused — {denied}"

    credited = attribution_user_id(deps)
    args = dict(args)
    if name == "slack_schedule_message":
        args["message"] = attribute_text(str(args.get("message") or ""), credited)
    elif name == "slack_create_list":
        args["description"] = attribute_text(str(args.get("description") or ""), credited).strip()
    elif name == "slack_complete_file_upload" and args.get("channel_id"):
        args["initial_comment"] = attribute_text(str(args.get("initial_comment") or ""), credited).strip()
    elif name == "slack_search_public":
        # By default every result comes with its surrounding messages: ~40k tokens for one
        # search, vs ~2k concise. Detail only when the model explicitly asks for it.
        args.setdefault("response_format", "concise")
    return args, None


@dataclass
class GuardedSlackMCPToolset(WrapperToolset[Any]):
    async def get_tools(self, ctx: RunContext[Any]):
        tools = await super().get_tools(ctx)
        return {name: tool for name, tool in tools.items() if name not in BLOCKED_SLACK_MCP_TOOLS}

    async def call_tool(self, name: str, tool_args: dict[str, Any], ctx: RunContext[Any], tool) -> Any:
        # The read checks make blocking Slack API calls — keep them off the loop.
        args, error = await asyncio.to_thread(guard_args, name, tool_args, ctx.deps)
        if error:
            return error
        return await super().call_tool(name, args, ctx, tool)
