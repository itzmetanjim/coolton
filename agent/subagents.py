import importlib
import logging

from pydantic_ai import Agent

from agent.deps import AgentDeps

logger = logging.getLogger(__name__)

SUBAGENT_PROMPTS = {
    "research": (
        "You are Research. Gather facts using Slack, web, user, channel, and thread tools. "
        "Prefer compact sourced findings over raw dumps. Include links, thread ids, channel "
        "names, dates, and uncertainty when available. Do not edit files, run commands, upload "
        "files, or post messages. Keep total tool calls under 300, then write up your findings."
    ),
    "explore": (
        "You are Explore. Inspect the sandbox workspace and gather context. You may read files, "
        "list files, grep, and run read-only commands. Do not modify or delete files, do not "
        "upload files, do not post messages, and do not run risky commands. Keep total tool calls "
        "under 300, then write up your findings. Return concise findings with file paths, facts, "
        "and uncertainties."
    ),
    "summarizer": (
        "You summarize Slack conversations. Be clear and concise. Preserve decisions, open "
        "questions, and action items when present. Output only the summary, no preamble."
    ),
}

# Tools each subagent is allowed to call. Names match the @agent.tool registrations in agent.agent.
SUBAGENT_TOOLS = {
    "research": [
        "search_web_tool",
        "fetch_url_tool",
        "search_slack_tool",
        "read_conversation_history_tool",
        "list_channel_threads_tool",
        "get_user_tool",
        "get_channel_info_tool",
    ],
    "explore": [
        "read_sandbox_file_tool",
        "list_sandbox_files_tool",
        "search_sandbox_files_tool",
        "run_linux_command",
        "search_web_tool",
        "fetch_url_tool",
        "search_slack_tool",
        "read_conversation_history_tool",
        "list_channel_threads_tool",
        "get_user_tool",
        "get_channel_info_tool",
    ],
    "summarizer": [],
}

SUBAGENT_DESCRIPTIONS = {
    "research": "Runs focused Slack, web, user, channel, and thread research, then returns compact sourced findings.",
    "explore": "Reads workspace files and gathers implementation context without making changes.",
    "summarizer": "Summarizes a Slack conversation transcript concisely, preserving decisions, open questions, and action items.",
}


def _tool_map():
    mod = importlib.import_module("agent.agent")
    return {name: t.function for name, t in mod.agent._function_toolset.tools.items()}


def run_subagent(target: str, task: str, deps: AgentDeps) -> str:
    """Run a focused subagent (research/explore/summarizer) and return its findings.

    Uses the same provider fallback chain as the main agent (skips dead providers,
    prefers the last-known-good provider). The subagent never sees the delegate tool,
    so there is no recursion.
    """
    if target not in SUBAGENT_PROMPTS:
        raise ValueError(f"Unknown subagent target: {target}")

    from agent.agent import _run_with_provider_chain, _hooks

    funcs = []
    if SUBAGENT_TOOLS.get(target):
        tool_map = _tool_map()
        for name in SUBAGENT_TOOLS[target]:
            func = tool_map.get(name)
            if func is not None:
                funcs.append(func)
            else:
                logger.warning(f"subagents: tool {name} not found on main agent, skipping")

    agent_dynamic = Agent(
        deps_type=AgentDeps,
        system_prompt=SUBAGENT_PROMPTS[target],
        tools=funcs,
    )

    run_kwargs = dict(
        user_prompt=task,
        deps=deps,
        message_history=None,
        toolsets=[],
        capabilities=[_hooks],
    )

    logger.info(f"Running subagent: {target}")
    result, provider = _run_with_provider_chain(agent_dynamic, run_kwargs, deps)
    output = (result.output or "").strip()
    logger.info(f"Subagent {target} done (provider: {provider}, {len(output)} chars)")
    return output
