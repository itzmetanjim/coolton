"""Deferred tools: found with `search_tools`, called through `call_tool`.

Rarely used tools (agent.agent.DEFERRED_TOOLS plus the Slack MCP, Context7
and user MCP servers) aren't in the model's tool list. `search_tools` returns
their names, descriptions and parameter schemas as a normal tool result, and
the model calls them with `call_tool(name, arguments)`.

The tool list therefore never changes: loading a tool only adds messages to
the conversation. On Chat Completions providers (every provider coolton uses)
adding even one tool to `tools` makes the whole cached prompt miss; a tool
result doesn't. The same goes for Jev's preloads (agent.tool_preload), which
are a `search_tools` call run for the model at the start of the turn.

Everything that shows or counts tool calls (the plan block, logs, the web UI,
debug timing, the Slack call budget) goes through `shown_call`, so a
`call_tool` call looks and counts exactly like calling that tool directly.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from pydantic import ValidationError
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.toolsets import WrapperToolset

SEARCH_TOOL = "search_tools"
CALL_TOOL = "call_tool"
# tool_call_id prefix of the search_tools call Jev's preload runs for the model;
# the plan block / web UI don't show it as a step (see is_preload_call).
PRELOAD_CALL_ID_PREFIX = "preload_"

_KEYWORD_MATCHES = 5


class HiddenToolset(WrapperToolset):
    """A toolset whose tools never reach the model's tool list. The agent
    still enters and exits it (so MCP connections work as usual); search_tools
    and call_tool reach its tools through `hidden_tools`."""

    async def get_tools(self, ctx):
        return {}

    async def hidden_tools(self, ctx) -> dict:
        return await self.wrapped.get_tools(ctx)


def shown_call(call, args):
    """(call, args) as they should be shown and counted: a call_tool call
    becomes a call of the tool it runs, with that tool's arguments."""
    if call.tool_name == CALL_TOOL and isinstance(args, dict) and isinstance(args.get("name"), str):
        return replace(call, tool_name=args["name"]), args.get("arguments") or {}
    return call, args


def is_preload_call(call) -> bool:
    return (call.tool_call_id or "").startswith(PRELOAD_CALL_ID_PREFIX)


async def _all_hidden(ctx: RunContext) -> list[tuple[HiddenToolset, dict]]:
    return [(ts, await ts.hidden_tools(ctx)) for ts in (getattr(ctx.deps, "hidden_toolsets", None) or [])]


def _words(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(w) > 1}


def _keyword_matches(query: str, tools: dict) -> list[str]:
    terms = _words(query)
    scored = []
    for name, tool in tools.items():
        name_words = _words(name)
        desc_words = _words(tool.tool_def.description or "")
        score = 3 * len(terms & name_words) + len(terms & desc_words)
        if score:
            scored.append((score, name))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [name for _, name in scored[:_KEYWORD_MATCHES]]


def _describe(tool) -> dict:
    return {
        "name": tool.tool_def.name,
        "description": tool.tool_def.description or "",
        "parameters": tool.tool_def.parameters_json_schema,
    }


async def search_tools(ctx: RunContext, queries: list[str]) -> dict:
    """Find tools that aren't in your tool list (email, HuddleFM, Slack bot building,
    scheduled tasks, data analysis, embeds, Mermaid diagrams, skill management, the Slack
    MCP tools, Context7 library docs, and more). Returns each match's name, description
    and parameters. Found tools are NOT added to your tool list: call them with
    `call_tool(name, arguments)`.

    Args:
        queries: Tool names or short descriptions of what you need, e.g.
            ["render_mermaid_tool"], ["schedule a recurring task"], ["library docs"].
    """
    tools: dict = {}
    for _, found in await _all_hidden(ctx):
        for name, tool in found.items():
            tools.setdefault(name, tool)
    picked: list[str] = []
    for query in queries or []:
        query = (query or "").strip()
        for name in [query] if query in tools else _keyword_matches(query, tools):
            if name not in picked:
                picked.append(name)
    if not picked:
        return {"discovered_tools": [], "message": "No matching tools. Try other words, or a tool's exact name."}
    return {
        "discovered_tools": [_describe(tools[name]) for name in picked],
        "how_to_call": "These are not in your tool list. Call them with call_tool(name=..., arguments={...}).",
    }


async def call_tool(ctx: RunContext, name: str, arguments: dict[str, Any]) -> Any:
    """Call a tool you found with `search_tools` (tools in your normal tool list are
    called directly, not through this). Put ALL of the tool's own arguments inside
    `arguments`, e.g. call_tool(name="render_mermaid_tool", arguments={"diagram_code": "..."}).

    Args:
        name: The tool's exact name, as search_tools returned it.
        arguments: The tool's arguments as an object, matching the parameters search_tools
            returned. Use {} for a tool that takes none.
    """
    for toolset, found in await _all_hidden(ctx):
        tool = found.get(name)
        if tool is None:
            continue
        try:
            args = tool.args_validator.validate_python(arguments or {})
        except ValidationError as e:
            schema = json.dumps(tool.tool_def.parameters_json_schema)
            raise ModelRetry(
                f"Invalid arguments for {name}: {e}\n\nCall it as call_tool(name={name!r}, "
                f"arguments={{...}}), with every argument inside `arguments`. Its parameters: {schema}"
            ) from e
        return await toolset.wrapped.call_tool(name, args, replace(ctx, tool_name=name), tool)
    raise ModelRetry(f"No tool named {name!r}. Use search_tools to find the right name.")
