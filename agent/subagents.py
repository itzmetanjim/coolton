# agent/subagents.py
"""
Focused subagents for coolton, ported from gorkie's research/explore/execute
pattern (gorkie is TypeScript/mastra; coolton is Python/pydantic-ai).

Each subagent is a scoped pydantic-ai Agent with a tight instruction set and a
small set of read-only or action tools. The main coolton agent delegates to them
via the `agent_research` / `agent_explore` / `agent_execute` tools, which keep
the subagent output compact and on-task.

- research: read-only Slack/web/user/channel/thread search. Never writes.
- explore: read-only inspection of the per-thread sandbox filesystem. Never edits.
- execute: runs bash in the per-thread sandbox (the orchestrator's action role).

The subagent tools reuse coolton's existing implementations (web search, sandbox
command runner, file IO) so proxy env, gh auth, and per-thread sandbox resolution
all come along for free.
"""

from __future__ import annotations

from pydantic_ai import Agent, RunContext, Tool

from agent.deps import AgentDeps
from agent.tools.web_search import search_web


# ---------------------------------------------------------------------------
# Shared model resolution: reuse the same provider-order logic as run_agent so
# subagents answer with whatever provider is currently working.
# ---------------------------------------------------------------------------


def _resolve_model(user_id: str | None):
    """Return (model_name, model_obj) using the same fallback order as run_agent."""
    from agent.agent import _build_provider_order  # local import avoids cycle

    provider_order = _build_provider_order(user_id)
    if not provider_order:
        return None, None
    provider_name, provider_config = provider_order[0]
    model_name = provider_config["model"]
    model_obj = None
    if provider_config.get("base_url"):
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        model_obj = OpenAIChatModel(
            provider_config["model"],
            provider=OpenAIProvider(
                base_url=provider_config["base_url"],
                api_key=provider_config["api_key"],
            ),
        )
    return model_name, model_obj


# ---------------------------------------------------------------------------
# Tool wrappers that adapt coolton's existing implementations to a subagent's
# RunContext (pulling channel_id/thread_ts from ctx.deps).
# ---------------------------------------------------------------------------


def _research_tools() -> list[Tool]:
    def web(ctx: RunContext[AgentDeps], query: str, num_results: int = 8) -> str:
        """Search the web with Exa. Returns titles, URLs, and snippets."""
        return search_web(query, num_results)

    return [Tool(web, name="search_web", description="Search the web for facts.")]


def _explore_tools() -> list[Tool]:
    from agent.tools.sandbox_files import (
        read_sandbox_file,
        search_sandbox_files,
        list_sandbox_files,
    )

    def read(ctx: RunContext[AgentDeps], path: str) -> str:
        """Read a file from the per-thread sandbox."""
        return read_sandbox_file(path)

    def grep(ctx: RunContext[AgentDeps], pattern: str, path: str = "/home/user") -> str:
        """Grep for a pattern inside the sandbox."""
        return search_sandbox_files(pattern, path)

    def ls(ctx: RunContext[AgentDeps], pattern: str = "*", path: str = "/home/user") -> str:
        """List files in the sandbox matching a glob."""
        return list_sandbox_files(path, pattern)

    return [
        Tool(read, name="read_file", description="Read a sandbox file."),
        Tool(grep, name="grep", description="Search file contents in the sandbox."),
        Tool(ls, name="list_files", description="List sandbox files by glob."),
    ]


def _execute_tools() -> list[Tool]:
    def run_cmd(ctx: RunContext[AgentDeps], command: str) -> str:
        """Run a bash command in the per-thread sandbox. Returns stdout/stderr/exit code."""
        from agent.agent import run_linux_command  # lazy: avoids circular import
        return run_linux_command(ctx, command)

    def write_file(ctx: RunContext[AgentDeps], path: str, content: str) -> str:
        """Write a file into the sandbox."""
        from agent.tools.sandbox_files import write_sandbox_file

        return write_sandbox_file(path, content)

    return [
        Tool(run_cmd, name="run_command", description="Run bash in the sandbox."),
        Tool(write_file, name="write_file", description="Write a file to the sandbox."),
    ]


# ---------------------------------------------------------------------------
# Subagent definitions
# ---------------------------------------------------------------------------

research_agent = Agent(
    deps_type=AgentDeps,
    name="Research",
    system_prompt=(
        "You are Research. Gather facts using web search and Slack context. Prefer "
        "compact sourced findings over raw dumps. Include links, thread ids, channel "
        "names, dates, and uncertainty when available. Do NOT edit files, run commands, "
        "upload files, or post messages. Keep tool calls focused, then write up your "
        "findings as a tight summary with sources."
    ),
    tools=_research_tools(),
)

explore_agent = Agent(
    deps_type=AgentDeps,
    name="Explore",
    system_prompt=(
        "You are Explore. Inspect the per-thread sandbox filesystem and gather context. "
        "Do NOT modify files, delete files, upload files, post messages, or run risky "
        "commands. Return concise findings with file paths, facts, and uncertainties."
    ),
    tools=_explore_tools(),
)

execute_agent = Agent(
    deps_type=AgentDeps,
    name="Execute",
    system_prompt=(
        "You are Execute. Run bash commands in the per-thread cloud sandbox to get work "
        "done: install packages, run scripts, process files, build things. Prefer "
        "non-destructive commands. If a command can destroy data or the environment, say "
        "so and stop rather than run it. Return the command, its output, and a one-line "
        "result summary."
    ),
    tools=_execute_tools(),
)


# ---------------------------------------------------------------------------
# Delegation helpers + main-agent tools
# ---------------------------------------------------------------------------


def _delegate(agent: Agent, prompt: str, deps: AgentDeps, user_id: str | None) -> str:
    """Run a subagent synchronously and return its compact output."""
    model_name, model_obj = _resolve_model(user_id)
    if not model_name:
        return "Error: no AI provider configured for subagent."
    run_kwargs: dict = dict(user_prompt=prompt, deps=deps, message_history=None)
    if model_obj:
        run_kwargs["model"] = model_obj
    else:
        run_kwargs["model"] = model_name
    try:
        result = agent.run_sync(**run_kwargs)
        out = getattr(result, "output", None)
        return out if isinstance(out, str) else str(out)
    except Exception as e:  # surface, never swallow
        return f"Subagent error: {e}"


def agent_research(ctx: RunContext[AgentDeps], prompt: str) -> str:
    """Delegate a read-only research task to the Research subagent.

    Use for: gathering facts from the web, finding messages, looking up users or
    channels, compiling sourced findings. The subagent cannot write or post.
    """
    return _delegate(research_agent, prompt, ctx.deps, ctx.deps.user_id)


def agent_explore(ctx: RunContext[AgentDeps], prompt: str) -> str:
    """Delegate a read-only inspection task to the Explore subagent.

    Use for: reading/scanning files in the sandbox, grepping code, understanding an
    existing implementation before a change. The subagent cannot edit anything.
    """
    return _delegate(explore_agent, prompt, ctx.deps, ctx.deps.user_id)


def agent_execute(ctx: RunContext[AgentDeps], prompt: str) -> str:
    """Delegate an action task to the Execute subagent (bash in the sandbox).

    Use for: running scripts, installing packages, processing files, building. The
    subagent runs commands in the per-thread sandbox; it will refuse destructive ones.
    """
    return _delegate(execute_agent, prompt, ctx.deps, ctx.deps.user_id)
