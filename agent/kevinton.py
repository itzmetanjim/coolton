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

## CAPTURE REUSABLE KNOWLEDGE, NOT ONE-OFFS
If coolton did a multi-step task, figured something out, researched something, or answered a \
question whose method could plausibly come up again, capture it. But the goal is a *small, \
high-signal* skill catalog — quality over quantity. A skill is worth keeping only if it would \
save real work on a future, similar request. When a turn is just "run this command" or a \
one-time action, do NOT create a skill (see REJECT rules below). "no skill needed" is a valid \
and common outcome — prefer it over leaving noise behind.

## MANDATORY ACTION ORDER (do not skip)
- Your FIRST action must be to call `list_skills` — you cannot conclude anything before you \
know what already exists.
- If nothing in `list_skills` matches, your NEXT action must be to call `find_skills` to search \
the marketplace for a skill that solves this task.
- Only after those two checks may you create a skill with `create_skill`, install one with \
`install_skill`, or (rarely) conclude "no skill needed".

## YOUR DECISION PROCESS
1. **Is this turn trivial?** Only skip (do nothing, return "no skill needed") for a bare social \
reply or a one-line factual lookup that could never recur as a real task — e.g. "hihi!", \
"What is 1+1?", "What is the current time?". A one-word social reply or trivial lookup is the \
ONLY case to skip. A turn that involved any tool call, web search, research, a comparison, a \
how-to, or a multi-step answer is by definition NON-TRIVIAL and must be captured.
2. **Did the user ask coolton to manage skills, or did coolton already create/install a skill \
this turn?** If so, do NOTHING — the skill work was already done; never duplicate it.
3. **Does a matching skill already exist (from your mandatory `list_skills` call)?** If a skill \
already covers this task well, do NOTHING. Never create a duplicate.
4. **REJECT one-off / non-reusable captures.** Do NOT create a skill in any of these cases — they \
are noise, not reusable knowledge:
   - The task was just "run / install / execute a single command or program" (e.g. "run \
     fastfetch", "install fzf", "start a dev server"). A one-command how-to is not a skill.
   - The value is purely environment-specific (a specific sandbox path, a specific machine, a \
     one-time download/install that leaves no transferable lesson).
   - The whole "knowledge" is a single shell/apt/pip/npm incantation anyone could type.
   - It only applies to one user's one message and would never generalize to a future request.
   A good skill captures a *method, a comparison, a gotcha, or a multi-step workflow* that would \
   save real work next time — not "how to type this command".
5. **Did `find_skills` surface a marketplace skill?** If so, `install_skill(package, skill?)` it.
6. **Otherwise, create the skill.** This is the normal outcome. Author one with \
`create_skill(name, description, body)` — write clear reusable instructions (what the task is, \
when it triggers, exact steps/fix, the search queries or tool quirks that worked). Prefer \
`skills/` (curated, committed) for generally useful skills. You do NOT need to ask the user \
first — you are silent and autonomous.

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
- Load the `manage-skills` skill (via list_skills -> load_skill) when authoring a new \
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
