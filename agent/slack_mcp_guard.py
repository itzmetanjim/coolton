"""The Slack MCP toolset (mcp.slack.com, running as cooltonUser), with
coolton's read and attribution rules applied to each call.

The hosted server's tools can read anything cooltonUser can see and post as
cooltonUser, so each call goes through the same rules as coolton's own Slack
tools:

- reads of a channel, thread, file, canvas, or list follow agent.slack_access
  (the current conversation, or public channels/files, only), and so do edits
  of an existing canvas or list;
- listing or searching channels only ever covers public ones (cooltonUser's own
  private channels and DMs aren't anyone else's to know about);
- creating a channel goes through slack_api_call's conversations.create instead,
  which posts who asked for it in the new channel (agent.change_notices);
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

# slack_search_public is the same keyword search as coolton's own search_slack_tool (no
# semantic search on this workspace), so it's off: one message search, the cheaper one.
BLOCKED_SLACK_MCP_TOOLS = {"slack_send_message", "slack_search_public_and_private", "slack_search_public"}

# Tool -> the arg naming the channel it reads.
_CHANNEL_READ_TOOLS = {
    "slack_read_channel": "channel_id",
    "slack_read_thread": "channel_id",
    "slack_list_channel_members": "channel_id",
    "slack_get_reactions": "channel_id",
}

# Tool -> the arg naming the file (canvases and lists are files too) it reads or edits.
_FILE_READ_TOOLS = {
    "slack_read_file": "file_id",
    "slack_read_canvas": "canvas_id",
    "slack_read_list": "list_id",
    "slack_update_canvas": "canvas_id",
    "slack_update_list": "list_id",
    "slack_add_list_record": "list_id",
    "slack_update_list_record": "list_id",
}

# Tool -> the arg naming the channel types it lists, and what it lists when that's left out.
_CHANNEL_LISTING_TOOLS = {
    "slack_list_user_channels": ("types", "public_channel,private_channel"),
    "slack_search_channels": ("channel_types", "public_channel"),
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
    if name in _CHANNEL_LISTING_TOOLS:
        param, default = _CHANNEL_LISTING_TOOLS[name]
        types = {t.strip() for t in str(args.get(param) or default).split(",") if t.strip()}
        if types != {"public_channel"}:
            return None, f"Error: {name} only lists public channels: pass {param}=\"public_channel\"."
    if name == "slack_create_conversation" and args.get("channel_name"):
        return None, ("Error: create channels with slack_api_call(method=\"conversations.create\"), which "
                      "says in the new channel who asked for it. This tool is only for opening DMs.")
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
