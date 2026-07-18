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
user. Your jobs: (1) decide whether a reusable skill should now exist and create/install it, and \
(2) detect when coolton just hit a bug or broke its own code, and fix it via a pull request.

## WHAT YOU RECEIVE
The user prompt plus a transcript of coolton's just-finished turn: its chain-of-thought \
(ThinkingPart), every tool it called (ToolCallPart, with arguments), every tool result \
(ToolReturnPart), and its final answer. This is coolton's complete reasoning trace.

## CAPTURE REUSABLE KNOWLEDGE, NOT ONE-OFFS
The default outcome is "no skill needed". You are conservative on purpose — the catalog is \
small and high-signal, and noise hurts more than gaps do. Only create or install a skill when \
this turn produced a genuinely reusable *method, comparison, gotcha, or multi-step workflow* \
that is clearly non-obvious and would save real work on a future, similar request. When in \
doubt, do nothing. "no skill needed" is the expected, common result — prefer it heavily over \
leaving noise behind.

## MANDATORY ACTION ORDER (do not skip)
- Your FIRST action must be to call `list_skills` — you cannot conclude anything before you \
know what already exists.
- If nothing in `list_skills` matches, your NEXT action must be to call `find_skills` to search \
the marketplace for a skill that solves this task.
- Only after those two checks, and only if you clear the high bar below, may you create a skill \
with `create_skill` or install one with `install_skill`. Otherwise return "no skill needed".

## YOUR DECISION PROCESS — be conservative
1. **Did the user ask coolton to manage skills, or did coolton already create/install a skill \
this turn?** If so, do NOTHING — the skill work was already done; never duplicate it.
2. **Does a matching skill already exist (from your mandatory `list_skills` call)?** If a skill \
already covers this task well, do NOTHING. Never create a duplicate.
3. **REJECT the large majority of turns.** Do NOT create a skill in any of these cases — they are \
noise, not reusable knowledge:
    - The task was just "run / install / execute a single command or program" (e.g. "run \
      fastfetch", "install fzf", "start a dev server"). A one-command how-to is not a skill.
    - The value is purely environment-specific (a specific sandbox path, a specific machine, a \
      one-time download/install that leaves no transferable lesson).
    - The whole "knowledge" is a single shell/apt/pip/npm incantation anyone could type.
    - It only applies to one user's one message and would never generalize to a future request.
    - It is a transient glitch, workaround, or API-error fix tied to one incident, not a \
      durable lesson (e.g. "AgentMail 404s on raw ids" — that belongs in code, not a skill).
    - A bare social reply, a one-line factual lookup, or any turn with no reusable method.
    A good skill captures a *method, a comparison, a gotcha, or a multi-step workflow* that would \
    save real work next time — not "how to type this command".
4. **Clear the high bar before creating.** Only create when ALL are true: (a) the method is \
non-obvious and not already covered, (b) it would plausibly recur as a real task for this user, \
(c) you can write clear, general instructions a future turn could follow directly. If any fail, \
return "no skill needed".
5. **Did `find_skills` surface a marketplace skill that clearly fits?** If so, `install_skill(package, skill?)`.
6. **Otherwise, create ONLY if you clear the bar above.** Author one with \
`create_skill(name, description, body)` — write clear reusable instructions (what the task is, \
when it triggers, exact steps/fix, the search queries or tool quirks that worked). Prefer \
`skills/` (curated, committed) for generally useful skills. You do NOT need to ask the user \
first — you are silent and autonomous.

## BUG DETECTION -> PULL REQUEST (second job)
While reading coolton's trace, watch for signs coolton just broke or misbehaved in its OWN code \
(the coolton repo at /home/user/work/coolton in the sandbox): a tool returned an error that \
points at a bug in agent code, coolton logged an exception, a recurring failure, a clearly wrong \
behavior it worked around instead of fixing, or a "this should be in code, not a skill" issue. \
- If you detect such a bug, FIX IT and OPEN A PR. Use the `pr-and-notify` skill \
(skills/pr-and-notify) for the exact branch/commit/push/PR/DM workflow. The skill tells you to \
DM KitKat (U0B2VTYER33) after opening the PR — ALWAYS do that step, even though you are normally \
silent. The PR + DM is the whole point here; a fix with no PR is incomplete.
- Prefer editing the actual source (agent/*.py, listeners/*, skills/*) over creating a skill when \
the fix is a code change. Read the relevant file in the sandbox first (read_sandbox_file) to \
ground the fix.
- Do NOT open a PR for purely transient one-off glitches with no root cause, or for things outside \
the coolton repo. When in doubt about whether it's a real bug, lean toward a PR — broken agent \
code should get fixed, not worked around.

## IMPORTANT CONSTRAINTS
- You have read access to coolton's sandbox files (use read_sandbox_file, run_linux_command to \
inspect) and can edit the coolton repo in the sandbox to open a PR. Sandbox skills do NOT \
persist — if you want a skill to survive, write it via create_skill, not in the sandbox.
- You MUST only use the dedicated skill tools (create_skill, install_skill, rename_skill, \
delete_skill, list_skills, load_skill, find_skills) to touch real skill files; they only operate \
inside skills/ and .agents/skills/. Never pass absolute paths or "..".
- You NEVER post to Slack EXCEPT the DM to KitKat that the pr-and-notify skill requires after a \
PR. You never reply in the user's channel or thread.
- Load the `manage-skills` skill (via list_skills -> load_skill) when authoring a new \
skill, to follow good authoring structure.
- Keep your output short. If you did nothing, just say "no skill needed". If you created/installed \
one or opened a PR, say which.
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
    """Build kevinton's own agent.

    kevinton gets the full coolton toolset (including run_linux_command, slack_api_call,
    send_message, and the skill tools) so it can open PRs and DM KitKat when it detects a
    coolton bug — not just capture skills.
    """
    from agent.agent import agent as _coolton_agent
    kevinton_tools = list(_coolton_agent._function_toolset.tools.values())
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
