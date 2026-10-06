"""A per-turn cap on Slack tool calls.

A turn that loops over Slack (code_mode iterating channels, a model stuck
re-reading history) can make hundreds of calls, burning Slack's rate limits
for every other conversation. Past SLACK_CALL_BUDGET calls in one turn, Slack
tools refuse with a message telling the model to narrow down and answer from
what it has. Counted on AgentDeps.slack_calls, from both tool paths: the
pydantic-ai hook in agent.agent (normal tool calls, Slack MCP included) and
agent.tool_proxy (code_mode, which bypasses those hooks). Subagents' calls count
toward the turn that started them.
"""
from __future__ import annotations

import threading

SLACK_CALL_BUDGET = 200

SLACK_TOOLS = frozenset({
    "search_slack_tool", "read_conversation_history_tool", "summarize_thread_tool",
    "list_channel_threads_tool", "get_user_tool", "get_channel_info_tool", "get_slack_file_tool",
    "download_attachments_to_sandbox", "upload_file_from_sandbox", "post_message_tool",
    "chat_postMessage", "slack_api_call", "slack_api_call_as_bot_tool", "add_emoji_reaction",
    "remove_reaction_tool", "invite_coolton_user_to_channel", "leave_channel_tool", "upload_emoji_tool",
})

OVER_BUDGET_ERROR = (
    f"Error: this turn has made over {SLACK_CALL_BUDGET} Slack calls, so further Slack calls are "
    "blocked. Narrow the channels or time range, and answer from what you already have."
)

_lock = threading.Lock()


# Tools whose arguments can end up in a Slack message without being a "Slack tool" above.
_OTHER_POSTING_TOOLS = frozenset({"send_message", "text_only_response", "upload_file_from_sandbox",
                                  "send_html_embed_tool", "send_whiteboard_embed_tool", "generate_image_tool",
                                  "render_mermaid_tool"})


def posts_to_slack(name: str) -> bool:
    """Whether `name`'s arguments can end up in a Slack message (agent.mentions)."""
    return is_slack_tool(name) or name in _OTHER_POSTING_TOOLS


def is_slack_tool(name: str) -> bool:
    # Slack MCP server tools are all slack_*-prefixed.
    return name in SLACK_TOOLS or name.startswith("slack_")


def spend(deps, tool_name: str) -> str | None:
    """Count one call if `tool_name` is a Slack tool; returns OVER_BUDGET_ERROR
    once this turn is past the budget, else None."""
    if deps is None or not is_slack_tool(tool_name):
        return None
    # A subagent's calls count toward its turn's budget (agent.subagents).
    deps = getattr(deps, "parent_deps", None) or deps
    with _lock:
        count = (getattr(deps, "slack_calls", 0) or 0) + 1
        deps.slack_calls = count
    return OVER_BUDGET_ERROR if count > SLACK_CALL_BUDGET else None
