"""Subagents: focused runs coolton hands a self-contained task to, one or several at once.

delegate_to_subagent runs one; delegate_to_subagents runs several in parallel (each in
its own thread, up to MAX_PARALLEL) and returns every result together. Each subagent
is its own pydantic-ai run through the same provider fallback chain as the turn
(agent.agent._run_with_provider_chain), with:

- tools: "general" gets everything coolton has (rarely used tools and the MCP servers
  through search_tools/call_tool, exactly like the main agent, and skills), minus
  SUBAGENT_EXCLUDED_TOOLS: delegating (no recursion), and what only the turn itself
  does: reacting to and messaging the user, ending or leaving the turn, abuse
  reports. "research" and "explore" get read-only sets; "summarizer" gets none.
- its own copy of the turn's AgentDeps (subagent_deps), so parallel runs don't
  share per-run state; the turn's per-turn limits (the Slack call budget) still count
  on the turn's deps (AgentDeps.parent_deps), and anything that has to outlive the
  subagent (keeping the sandbox warm) is copied back when it finishes.
- the turn's tool hooks (secret redaction, the abuse stop, the Slack budget), plus its
  own: `!stop` stops it at its next tool call, past SUBAGENT_TOOL_CALL_LIMIT every
  tool call is refused so it writes up what it has, and a checkpoint so a provider
  fallback mid-run resumes instead of starting over.

Parallel subagents share the thread's sandbox: agent.sandbox_helpers keeps them from
creating two or pausing it under each other, and code_mode runs one at a time.
"""
from __future__ import annotations

import copy
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.capabilities import Hooks, PrepareTools, ProcessHistory
from pydantic_ai.toolsets import FunctionToolset

from agent.deps import AgentDeps

logger = logging.getLogger(__name__)

MAX_PARALLEL = 6
SUBAGENT_TOOL_CALL_LIMIT = 200
# A result longer than this is cut, so a few subagents can't flood the turn's context.
SUBAGENT_OUTPUT_CHARS = 30_000

_KNOWLEDGE_CUTOFF_NOTE = (
    "You have a training knowledge cutoff: anything past it that you don't recognize (a model "
    "release, a product, an event) is not automatically fake, it's just something you weren't "
    "trained on. Never dismiss a live search result as a hallucination or a future-dated page "
    "just because the name is unfamiliar; trust what the tool actually returned over your own "
    "training data. For fast-moving topics (e.g. \"best AI models\"), search first instead of "
    "answering from memory."
)

_SUBAGENT_BASE = f"""You are a subagent of coolton, an AI assistant in Slack. coolton handed you one \
self-contained task. Do it fully with your tools, then reply with the result. Your reply goes back \
to coolton, not to the user, so make it complete on its own but compact: what you found or did, \
with links, ids, file paths and dates, and anything you couldn't do or aren't sure of. You can't \
talk to the user or ask questions: when something is ambiguous, make the most reasonable \
assumption and say which.

Other subagents may be working in parallel on related tasks, in the same sandbox: don't undo or \
overwrite their work, and use your own directory or git branch when you change files.

Searching: Slack search is keyword search, never natural language (no tool understands a \
question), so search for distinctive words or an exact "quoted phrase" the message would contain, \
and use `search_slack_tool` for messages. Start with the key term on its own and add keywords only \
to narrow it down. A task with several parts may be several unrelated questions, search each \
separately.

Slack access: reading only works for the conversation coolton is in, or public channels (and \
files shared in one); tools refuse anything else, don't try to work around it. Messages you post \
anywhere are automatically credited to the person who asked.

Tools not in your tool list (rarely used ones, and MCP servers like the Slack MCP or library \
docs) are found with `search_tools` and called with `call_tool(name, arguments)`, all arguments \
as one JSON object string. Tools in your list are called directly.

Keep total tool calls under {SUBAGENT_TOOL_CALL_LIMIT}; past that, tools are refused and you \
must write up what you have. Write plainly and never use em dashes. {_KNOWLEDGE_CUTOFF_NOTE}"""

SUBAGENT_PROMPTS = {
    "general": _SUBAGENT_BASE + (
        "\n\nYou have all of coolton's tools: the sandbox, the web, Slack, files, email, and "
        "everything else, so you can do the work, not just look into it."
    ),
    "research": _SUBAGENT_BASE + (
        "\n\nYou are Research: gather facts from Slack, the web, users, channels, threads, "
        "canvases and library docs. Don't change anything: you only have reading tools. Prefer "
        "compact sourced findings over raw dumps."
    ),
    "explore": _SUBAGENT_BASE + (
        "\n\nYou are Explore: inspect the sandbox workspace (read, list, grep, read-only "
        "commands) and gather context, plus Slack and web lookups. Don't modify or delete "
        "files, install anything, or run risky commands. Return findings with file paths."
    ),
    "summarizer": (
        "You summarize Slack conversations. Be clear and concise. Preserve decisions, open "
        "questions, and action items when present. Output only the summary, no preamble."
    ),
}

SUBAGENT_DESCRIPTIONS = {
    "general": "Does the task with all of coolton's tools (sandbox, web, Slack, files, email...) and reports back.",
    "research": "Read-only Slack, web, canvas and docs research; returns compact sourced findings.",
    "explore": "Reads the sandbox workspace (files, grep, read-only commands) to gather context.",
    "summarizer": "Summarizes a conversation transcript, keeping decisions, open questions and action items.",
}

# What no subagent gets: delegating again, and what only the turn itself does.
SUBAGENT_EXCLUDED_TOOLS = frozenset({
    "delegate_to_subagent", "delegate_to_subagents",
    "add_emoji_reaction", "send_message",
    "skip", "wait_tool", "leave_thread_tool", "join_thread_tool",
    "report_abuse_tool",
})

_READ_TOOLS = (
    "search_web_tool", "fetch_url_tool", "search_slack_tool", "read_conversation_history_tool",
    "list_channel_threads_tool", "summarize_thread_tool", "get_user_tool", "get_channel_info_tool",
    "get_slack_file_tool", "analyze_image_tool", "get_datetime",
)
_SANDBOX_READ_TOOLS = (
    "run_linux_command", "read_sandbox_file_tool", "list_sandbox_files_tool",
    "search_sandbox_files_tool", "check_background_command_tool", "see_image_from_sandbox",
)
_ALL = "*"
# Function tools each subagent gets (before SUBAGENT_EXCLUDED_TOOLS); _ALL is every one.
SUBAGENT_TOOLS: dict[str, tuple[str, ...] | str] = {
    "general": _ALL,
    "research": _READ_TOOLS,
    "explore": _READ_TOOLS + _SANDBOX_READ_TOOLS,
    "summarizer": (),
}
# Which MCP tools each subagent reaches through search_tools/call_tool.
_READ_ONLY_MCP_PREFIXES = ("slack_read_", "slack_search_", "slack_list_", "slack_get_")
_DOCS_MCP_TOOLS = frozenset({"resolve-library-id", "query-docs"})


def _mcp_tool_allowed(target: str, name: str) -> bool:
    if target == "general":
        return True
    return name in _DOCS_MCP_TOOLS or name.startswith(_READ_ONLY_MCP_PREFIXES)


def _tool_map() -> dict:
    from agent.agent import agent

    return dict(agent._function_toolset.tools)


def subagent_deps(deps: AgentDeps) -> AgentDeps:
    """A copy of the turn's deps for one subagent run: same conversation, requester and
    credentials, but its own per-run state, with the turn's per-turn limits still
    counted on the turn's deps."""
    sub = copy.copy(deps)
    sub.parent_deps = getattr(deps, "parent_deps", None) or deps
    sub.hidden_toolsets = []
    sub.last_attempt_messages = None
    sub.halted_messages = None
    sub.tool_preload = None
    sub.plan_tasks = {}
    sub.should_skip = False
    sub.excluded_tools = SUBAGENT_EXCLUDED_TOOLS
    return sub


def _merge_back(deps: AgentDeps, sub: AgentDeps) -> None:
    """What a subagent changed that has to outlive it, onto the turn's deps."""
    if sub.keep_sandbox_warm:
        deps.keep_sandbox_warm = True
    deps.sandbox_keepalive_seconds = max(deps.sandbox_keepalive_seconds, sub.sandbox_keepalive_seconds)
    deps.last_screenshot_post_ts = max(deps.last_screenshot_post_ts, sub.last_screenshot_post_ts)


def _subagent_hooks(deps: AgentDeps) -> Hooks:
    """!stop, the tool call cap, and a checkpoint for provider fallback (see the module docstring)."""
    from agent.stop_store import HaltRun, stop_requested_for

    hooks = Hooks()
    calls = {"n": 0}

    @hooks.on.tool_execute
    async def guard(ctx, *, call, tool_def, args, handler):
        if stop_requested_for(deps.channel_id, deps.thread_ts, deps.run_started_at):
            raise HaltRun("!stop requested")
        calls["n"] += 1
        if calls["n"] > SUBAGENT_TOOL_CALL_LIMIT:
            return (f"Refused: you've used your {SUBAGENT_TOOL_CALL_LIMIT} tool calls. Stop calling "
                    "tools and write up what you have now.")
        return await handler(args)

    @hooks.on.model_request
    async def checkpoint(ctx, *, request_context, handler):
        if len(request_context.messages) > 1:
            deps.last_attempt_messages = list(request_context.messages)
        return await handler(request_context)

    return hooks


def _build_tools(target: str, sub: AgentDeps, is_vision: bool) -> tuple[list, list]:
    """(the tools in the subagent's list, its toolsets) for `target`."""
    from agent.agent import DEFERRED_TOOLS
    from agent.deferred_tools import CALL_TOOL, SEARCH_TOOL, HiddenToolset

    wanted = SUBAGENT_TOOLS[target]
    if not wanted:
        return [], []
    tools = _tool_map()
    names = set(tools) if wanted == _ALL else set(wanted)
    names -= SUBAGENT_EXCLUDED_TOOLS | {SEARCH_TOOL, CALL_TOOL}
    if not is_vision:
        names.discard("see_image_from_sandbox")
    missing = [n for n in names if n not in tools]
    if missing:
        logger.warning("subagents: %s tools not found on the main agent: %s", target, ", ".join(sorted(missing)))
    names &= set(tools)

    core = [tools[n].function for n in sorted(names - DEFERRED_TOOLS)]
    deferred = [tools[n].function for n in sorted(names & DEFERRED_TOOLS)]

    from agent.platforms.slack import SlackPlatform

    platform: Any = sub.platform or SlackPlatform(sub.client)
    toolsets = []
    if deferred:
        toolsets.append(HiddenToolset(FunctionToolset(deferred)))
    for toolset in platform.toolsets(sub):
        if isinstance(toolset, HiddenToolset) and target != "general":
            toolset = HiddenToolset(toolset.wrapped.filtered(
                lambda ctx, tool_def: _mcp_tool_allowed(target, tool_def.name)))
        toolsets.append(toolset)
    sub.hidden_toolsets = [t for t in toolsets if isinstance(t, HiddenToolset)]
    if sub.hidden_toolsets:
        core += [tools[SEARCH_TOOL].function, tools[CALL_TOOL]]
    return core, toolsets


def _task_with_context(task: str, deps: AgentDeps) -> str:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"## CONTEXT\n- Requested by Slack user `{deps.user_id}`\n"
        f"- coolton's conversation: channel_id `{deps.channel_id}`, thread_ts `{deps.thread_ts}`\n"
        f"- Current time: {now}\n\n## TASK\n{task}"
    )


def run_subagent(target: str, task: str, deps: AgentDeps) -> str:
    """Run one subagent and return its final reply. Raises if every provider fails;
    see delegate() for the tools' never-raising wrapper."""
    if target not in SUBAGENT_PROMPTS:
        raise ValueError(f"Unknown subagent target: {target}")

    from agent import provider_config
    from agent.agent import _hooks, _resolve_provider_order, _run_with_provider_chain, disable_strict_for_all_tools

    sub = subagent_deps(deps)
    is_vision = False
    if SUBAGENT_TOOLS[target]:
        try:
            first_model = _resolve_provider_order(sub.user_id, tag=sub.provider_tag_filter)[0][1]["model"]
            is_vision = provider_config.is_vision_model(first_model)
        except Exception:
            pass
    tools, toolsets = _build_tools(target, sub, is_vision)

    capabilities = [_hooks, _subagent_hooks(sub), PrepareTools(disable_strict_for_all_tools)]
    if target == "general":
        from agent.agent import build_skills_capability
        from agent.image_cap import cap_images

        capabilities += [build_skills_capability(), ProcessHistory(cap_images)]

    agent_dynamic = Agent(deps_type=AgentDeps, system_prompt=SUBAGENT_PROMPTS[target], tools=tools)
    run_kwargs = dict(
        user_prompt=task if target == "summarizer" else _task_with_context(task, sub),
        deps=sub,
        message_history=None,
        toolsets=toolsets,
        capabilities=capabilities,
        model_settings={
            "anthropic_cache_instructions": True,
            "anthropic_cache_tool_definitions": True,
            "anthropic_cache": True,
            "openai_prompt_cache_key": f"coolton-subagent-{target}",
            "openai_prompt_cache_retention": "24h",
        },
    )

    logger.info("Running subagent: %s", target)
    try:
        result, provider = _run_with_provider_chain(agent_dynamic, run_kwargs, sub, run_label=f"{target} subagent")
    finally:
        _merge_back(deps, sub)
    output = (result.output or "").strip()
    logger.info("Subagent %s done (provider: %s, %d chars)", target, provider, len(output))
    return output


def delegate(target: str, task: str, deps: AgentDeps) -> str:
    """run_subagent for the delegate tools: never raises, and caps the result's length."""
    from agent.redact import redact
    from agent.stop_store import HaltRun

    target = (target or "").strip().lower()
    if target not in SUBAGENT_PROMPTS:
        return f"Error: unknown subagent {target!r}. Use one of: {', '.join(SUBAGENT_PROMPTS)}."
    if not (task or "").strip():
        return "Error: the subagent needs a task."
    try:
        output = run_subagent(target, task, deps)
    except HaltRun:
        return "Stopped: the user sent !stop before this subagent finished."
    except Exception as e:
        logger.exception("Subagent %s failed", target)
        return f"Error: the {target} subagent failed: {redact(str(e), context='subagent error')[:500]}"
    if not output:
        return f"The {target} subagent finished without a reply."
    if len(output) > SUBAGENT_OUTPUT_CHARS:
        output = output[:SUBAGENT_OUTPUT_CHARS] + f"\n\n[cut at {SUBAGENT_OUTPUT_CHARS} characters]"
    return output


def parse_tasks(tasks) -> tuple[list[tuple[str, str]] | None, str | None]:
    """delegate_to_subagents' `tasks`: a JSON array of {"target", "task"} objects (or
    plain task strings, which go to "general"). Returns ([(target, task)], None) or
    (None, error)."""
    example = '\'[{"target": "research", "task": "..."}, {"target": "general", "task": "..."}]\''
    if isinstance(tasks, str):
        try:
            tasks = json.loads(tasks)
        except ValueError as e:
            return None, f"Error: tasks must be a JSON array like {example} ({e})."
    if isinstance(tasks, dict) and isinstance(tasks.get("tasks"), list):
        tasks = tasks["tasks"]
    if not isinstance(tasks, list) or not tasks:
        return None, f"Error: tasks must be a non-empty JSON array like {example}."
    parsed = []
    for item in tasks:
        if isinstance(item, str):
            item = {"target": "general", "task": item}
        if not isinstance(item, dict):
            return None, f"Error: each task must be an object like {example}."
        target = str(item.get("target") or "general").strip().lower()
        task = item.get("task")
        if target not in SUBAGENT_PROMPTS:
            return None, f"Error: unknown subagent {target!r}. Use one of: {', '.join(SUBAGENT_PROMPTS)}."
        if not isinstance(task, str) or not task.strip():
            return None, "Error: every task needs a non-empty \"task\" string."
        parsed.append((target, task))
    if len(parsed) > MAX_PARALLEL:
        return None, f"Error: at most {MAX_PARALLEL} subagents at once; split the rest into another call."
    return parsed, None


def delegate_many(tasks: list[tuple[str, str]], deps: AgentDeps) -> str:
    """Run several subagents at once and return every result, in the order given."""
    if len(tasks) == 1:
        target, task = tasks[0]
        return delegate(target, task, deps)
    with ThreadPoolExecutor(max_workers=len(tasks), thread_name_prefix="subagent") as pool:
        futures = [pool.submit(delegate, target, task, deps) for target, task in tasks]
        results = [f.result() for f in futures]
    return "\n\n".join(
        f"## Subagent {i} ({target})\nTask: {task[:200]}\n\n{result}"
        for i, ((target, task), result) in enumerate(zip(tasks, results), 1)
    )
