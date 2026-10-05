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
        inner = args.get("arguments")
        parsed = _parse_arguments(inner)
        return replace(call, tool_name=args["name"]), parsed if parsed is not None else inner
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


def found_in(history) -> set[str]:
    """Tools whose full definitions an earlier search_tools result in this
    conversation already holds, so they needn't be loaded again. Results from
    before call_tool (no `parameters`) don't count: those tools were revealed
    through the tool list, which no longer happens."""
    names: set[str] = set()
    for message in history or []:
        for part in getattr(message, "parts", []):
            if getattr(part, "part_kind", "") != "tool-return" or getattr(part, "tool_name", "") != SEARCH_TOOL:
                continue
            content = part.content if isinstance(part.content, dict) else {}
            names.update(t["name"] for t in content.get("discovered_tools", [])
                         if isinstance(t, dict) and "parameters" in t and t.get("name"))
    return names


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
    # A preload names exact tools; one a server doesn't have this turn (e.g. the Slack MCP
    # is down) is skipped, not turned into a keyword search that loads unrelated tools.
    exact_only = (getattr(ctx, "tool_call_id", None) or "").startswith(PRELOAD_CALL_ID_PREFIX)
    for query in queries or []:
        query = (query or "").strip()
        if exact_only and query not in tools:
            continue
        for name in [query] if query in tools else _keyword_matches(query, tools):
            if name not in picked:
                picked.append(name)
    if not picked:
        return {"discovered_tools": [], "message": "No matching tools. Try other words, or a tool's exact name."}
    return {
        "discovered_tools": [_describe(tools[name]) for name in picked],
        "how_to_call": "These are not in your tool list. Call them with call_tool(name=..., arguments='{...}'), the arguments as one JSON object string.",
    }


def _parse_arguments(arguments) -> dict | None:
    """call_tool's `arguments` JSON string as a dict, or None if it isn't one."""
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments or "{}")
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def call_tool(ctx: RunContext, name: str, arguments: str) -> Any:
    """Call a tool you found with `search_tools` (tools in your normal tool list are
    called directly, not through this). Pass ALL of the tool's own arguments as one JSON
    object string in `arguments`, e.g.
    call_tool(name="render_mermaid_tool", arguments='{"diagram_code": "graph TD; A-->B"}').

    Args:
        name: The tool's exact name, as search_tools returned it.
        arguments: The tool's arguments as a JSON object string, matching the parameters
            search_tools returned. Use "{}" for a tool that takes none.
    """
    for toolset, found in await _all_hidden(ctx):
        tool = found.get(name)
        if tool is None:
            continue
        schema = json.dumps(tool.tool_def.parameters_json_schema)
        how = (f"Call it as call_tool(name={name!r}, arguments='{{...}}'), with every argument in "
               f"one JSON object string. Its parameters: {schema}")
        parsed = _parse_arguments(arguments)
        if parsed is None:
            raise ModelRetry(f"`arguments` for {name} must be a JSON object string. {how}")
        try:
            args = tool.args_validator.validate_python(parsed)
        except ValidationError as e:
            raise ModelRetry(f"Invalid arguments for {name}: {e}\n\n{how}") from e
        return await toolset.wrapped.call_tool(name, args, replace(ctx, tool_name=name), tool)
    raise ModelRetry(f"No tool named {name!r}. Use search_tools to find the right name.")
