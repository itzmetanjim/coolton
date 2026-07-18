import logging
import os
import threading

from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import PrepareTools
from dataclasses import replace

from agent.deps import AgentDeps
from agent.agent import (
    create_skill,
    install_skill,
    rename_skill,
    delete_skill,
    get_thread_sandbox_id,
)
from agent.tools.sandbox_files import read_sandbox_file

logger = logging.getLogger(__name__)


KEVINTON_SYSTEM_PROMPT = """\
You are kevinton, a silent background agent that makes coolton (your sibling Slack assistant) \
self-improving. You run AFTER coolton has already answered the user — you are invisible to the \
user and you never post to Slack. Your only job is to decide whether a reusable skill should now \
exist, and if so, create or install it.

## WHAT YOU RECEIVE
The user prompt plus a transcript of coolton's just-finished turn: its chain-of-thought \
(ThinkingPart), every tool it called (ToolCallPart, with arguments), every tool result \
(ToolReturnPart), and its final answer. This is coolton's complete reasoning trace.

## YOUR DECISION PROCESS
1. **Was this turn trivial?** A bare social reply ("hi", "thanks", "yo", "lol"), a one-word \
acknowledgement, or a trivial factual lookup ("what is 1+1") — do NOTHING, return immediately. \
You are the one who decides this; the caller runs you after every turn including trivial ones.
2. **Did the user ask coolton to manage skills?** If the user's request was about creating, \
installing, renaming, deleting, or listing skills — or if coolton itself already called \
create_skill / install_skill this turn — do NOTHING. The skill work was already done; never \
duplicate or second-guess it.
3. **Does a matching skill already exist?** Call `list_skills`. If a skill already covers this \
task, do NOTHING. Never create a duplicate.
4. **Otherwise, capture it.** Decide: is there an existing skill on the skills.sh marketplace \
that solves this? If so, `install_skill(package, skill?)` it. If not, author one with \
`create_skill(name, description, body)` — write clear reusable instructions (what the task is, \
when it triggers, exact steps/fix). Prefer `skills/` (curated, committed) for generally useful \
skills. You do NOT need to ask the user first — you are silent and autonomous.

## IMPORTANT CONSTRAINTS
- You have **read-only** access to Slack and to coolton's sandbox files. You can read what \
coolton produced in its sandbox (use read_sandbox_file) to ground a skill in real artifacts, but \
**sandbox skills do not persist** — if you want a skill to survive, write it via create_skill, \
not in the sandbox.
- You MUST only use the dedicated skill tools (create_skill, install_skill, rename_skill, \
delete_skill, list_skills, load_skill, find_skills). They are the only things that touch real \
skill files and they only operate inside skills/ and .agents/skills/. Never pass absolute paths \
or "..".
- You NEVER post to Slack, never edit agent code, never run destructive commands. If a task \
looks like it needs code changes outside skills/, do NOTHING.
- Load the `fusion-skill-authoring` skill (via list_skills -> load_skill) when authoring a new \
skill, to follow good authoring structure.
- Keep your output short. If you did nothing, just say "no skill needed". If you created/installed \
one, say which.
"""


def _render_trace(all_messages) -> str:
    """Render coolton's full message trace (thinking + tool calls + returns + answer) to text."""
    lines = []
    for msg in all_messages:
        parts = getattr(msg, "parts", []) or []
        for part in parts:
            kind = getattr(part, "part_kind", None)
            if kind == "user-prompt":
                lines.append(f"[USER] {getattr(part, 'content', '')}")
            elif kind == "tool-call":
                name = getattr(part, "tool_name", "?")
                args = getattr(part, "args", "")
                lines.append(f"[TOOL CALL] {name}({args})")
            elif kind == "tool-return":
                name = getattr(part, "tool_name", "?")
                content = getattr(part, "content", "")
                if isinstance(content, str) and len(content) > 1500:
                    content = content[:1500] + "...<truncated>"
                lines.append(f"[TOOL RESULT] {name}: {content}")
            elif kind == "thinking":
                content = getattr(part, "content", "")
                if content:
                    lines.append(f"[THINKING] {content}")
            elif kind == "text":
                content = getattr(part, "content", "")
                if content:
                    lines.append(f"[ANSWER] {content}")
    return "\n".join(lines)


def _disable_strict(ctx, tool_defs):
    return [replace(t, strict=False) for t in tool_defs]


def build_kevinton_agent() -> Agent:
    """Build kevinton's own agent with a narrow, read-only-except-skills tool set."""
    kevinton_tools = [
        create_skill,
        install_skill,
        rename_skill,
        delete_skill,
        read_sandbox_file,
    ]
    agent = Agent(
        deps_type=AgentDeps,
        system_prompt=KEVINTON_SYSTEM_PROMPT,
        tools=kevinton_tools,
    )
    capabilities = [PrepareTools(_disable_strict)]
    try:
        from pydantic_ai_skills import SkillsCapability

        capabilities.append(
            SkillsCapability(directories=["skills", ".agents/skills"], auto_reload=True)
        )
    except Exception as e:  # pragma: no cover - skills lib optional
        logger.warning(f"kevinton: SkillsCapability unavailable: {e}")
    return agent, capabilities


def run_kevinton(
    user_text: str,
    all_messages,
    channel_id: str,
    thread_ts: str,
    deps: AgentDeps,
) -> str:
    """Run kevinton synchronously (called from a daemon thread by the listeners).

    Returns a short status string. Never raises into the caller's thread.
    """
    try:
        transcript = _render_trace(all_messages)
        sandbox_id = get_thread_sandbox_id(channel_id, thread_ts)
        prompt = (
            f"## USER REQUEST\n{user_text}\n\n"
            f"## COOLTON'S TRACE THIS TURN\n{transcript}\n\n"
            f"## CONTEXT\n"
            f"channel_id: {channel_id}\nsandbox_id: {sandbox_id or 'none'}\n\n"
            f"Decide whether a reusable skill should exist. If yes, create/install it. "
            f"If no, return 'no skill needed'."
        )
        agent, capabilities = build_kevinton_agent()
        model = _kevinton_model(deps)
        result = agent.run_sync(
            user_prompt=prompt,
            deps=deps,
            model=model,
            capabilities=capabilities,
        )
        status = result.output
        logger.info(f"kevinton: {status}")
        return status
    except Exception as e:
        logger.exception(f"kevinton failed: {e}")
        return f"kevinton error: {e}"


def _kevinton_model(deps: AgentDeps) -> str:
    """Use the same provider/model selection as coolton, and set its env key."""
    from agent.agent import get_runtime_model

    return get_runtime_model(deps.user_id)


def spawn_kevinton(
    user_text: str,
    all_messages,
    channel_id: str,
    thread_ts: str,
    deps: AgentDeps,
) -> None:
    """Fire-and-forget: run kevinton in a daemon thread so the user never waits."""
    t = threading.Thread(
        target=run_kevinton,
        args=(user_text, all_messages, channel_id, thread_ts, deps),
        name="kevinton",
        daemon=True,
    )
    t.start()
