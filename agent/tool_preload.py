"""Ask Jev which deferred tools this turn will need, and load them up front.

Rarely used tools are deferred (see agent.agent.DEFERRED_TOOLS and the MCP
toolsets in agent.platforms.slack): the model only gets them after calling
search_tools, which costs a whole extra model round trip. Jev (TypeSafe's
"System One" model, via HCAI) answers one yes/no question per tool group
from the user's message in well under a second, and the groups it says yes
to are loaded from the start. search_tools still works for anything Jev
missed.

Never allowed to slow a turn down or break it:
- the request starts in the background when the turn starts (start_preload),
  overlapping the rest of turn setup, and is given a hard JEV_TIMEOUT_SECONDS
  total (collect_preloads) — past that, or on any error, the turn just goes
  ahead with nothing preloaded;
- a failure or timeout marks Jev dead in agent.fallback_cache, and while it's
  dead (or HCAI's whole family is) no request is even attempted, so an outage
  doesn't add the full timeout to every turn. agent.provider_probe re-tests it
  on the background refresh schedule, which marks it alive again.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

JEV_TIMEOUT_SECONDS = 2.0
PRELOAD_THRESHOLD = 0.5
_STATE_CHARS = 4000

# Group -> (the yes/no question Jev answers, the deferred function tools it loads).
# The two MCP groups load a whole MCP toolset instead (see agent.platforms.slack).
TOOL_GROUPS: dict[str, tuple[str, frozenset[str]]] = {
    "email": ("Does this involve email: reading, checking or sending emails, or an email inbox?", frozenset({
        "agentmail_create_inbox", "agentmail_list_inboxes", "agentmail_list_messages",
        "agentmail_read_message", "agentmail_send_email"})),
    "huddlefm": ("Is this about HuddleFM, or playing, queueing or DJing music in a Slack huddle?", frozenset({
        "huddlefm_request_control_tool", "huddlefm_command_tool"})),
    "slack_bot_building": ("Does this ask to create, configure, install or deploy a Slack bot or Slack app?", frozenset({
        "create_slack_bot_tool", "check_bot_install_status_tool", "register_bot_tokens_tool",
        "update_slack_bot_manifest_tool", "wrangler_bot_deploy_tool"})),
    "scheduled_tasks": ("Does this ask for something recurring (every day, every week, a cron job), or to list, pause, resume or delete scheduled tasks?", frozenset({
        "create_scheduled_task_tool", "list_scheduled_tasks_tool", "pause_scheduled_task_tool",
        "resume_scheduled_task_tool", "delete_scheduled_task_tool"})),
    "code_channels": ("Does this ask to create a code channel?", frozenset({"create_code_channel_tool"})),
    "data_analysis": ("Does this involve analyzing data: a CSV or spreadsheet file, a SQL query, statistics or charts?", frozenset({
        "analyze_csv_tool", "run_sql_on_csv_tool", "run_python_data_analysis_tool"})),
    "archives": ("Does this involve extracting a .tar.gz archive?", frozenset({"extract_tar_gz_tool"})),
    "opencode": ("Does this ask to use opencode, the coding agent CLI?", frozenset({"install_opencode_tool", "run_opencode_tool"})),
    "embeds": ("Does this ask for an interactive HTML page, web embed or whiteboard?", frozenset({
        "send_html_embed_tool", "send_whiteboard_embed_tool"})),
    "diagrams": ("Does this ask for a diagram or flowchart (e.g. Mermaid)?", frozenset({"render_mermaid_tool"})),
    "custom_emoji": ("Does this ask to add or upload a custom Slack emoji?", frozenset({"upload_emoji_tool"})),
    "coolton_feedback": ("Is the user reporting a bug in coolton itself, or giving feedback or a suggestion about coolton?", frozenset({"submit_feedback_tool"})),
    "skills": ("Does this ask to create, install, rename, delete or otherwise manage coolton's skills?", frozenset({
        "create_skill", "rename_skill", "delete_skill", "install_skill"})),
    "live_view": ("Does the user want to watch a browser or desktop live, through a live stream or view link?", frozenset({
        "set_sandbox_keepalive_tool", "computer_stream_tool", "agent_browser_stream_tool"})),
    "slack_admin": ("Does this ask to invite coolton's helper account to a channel, remove a reaction, leave a channel, or call the Slack API as the bot?", frozenset({
        "invite_coolton_user_to_channel", "remove_reaction_tool", "leave_channel_tool", "slack_api_call_as_bot_tool"})),
    "slack_mcp": ("Does this involve Slack canvases, Slack lists, message drafts, scheduling a Slack message for later, someone's Slack profile, or a channel's member list?", frozenset()),
    "library_docs": ("Is this a question about how to use a programming library, framework, SDK or API?", frozenset()),
}
SLACK_MCP_GROUP = "slack_mcp"
LIBRARY_DOCS_GROUP = "library_docs"

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="jev-preload")


@dataclass
class PreloadRequest:
    future: Future
    started: float
    name: str


def tools_for(groups: set[str]) -> frozenset[str]:
    return frozenset().union(*(TOOL_GROUPS[g][1] for g in groups if g in TOOL_GROUPS))


def _usable_jev() -> dict | None:
    """The first Jev entry not marked dead (nor its provider's whole family)."""
    from agent.fallback_cache import get_dead_families, get_dead_providers
    from agent.provider_config import build_jev_provider_order

    entries = build_jev_provider_order()
    if not entries:
        return None
    dead, dead_families = get_dead_providers(), get_dead_families()
    return next((e for e in entries if e["name"] not in dead and e["provider"] not in dead_families), None)


def ask_jev(entry: dict, state, questions: dict, timeout: float = JEV_TIMEOUT_SECONDS) -> dict:
    """One Jev /systemone call; returns its `answers`, raising on any failure."""
    response = requests.post(
        entry["url"],
        json={"model": entry["model"], "state": state, "questions": questions},
        headers={"Authorization": f"Bearer {entry['api_key']}", "Content-Type": "application/json"},
        timeout=(timeout, timeout),
    )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
    answers = response.json().get("answers")
    if not isinstance(answers, dict):
        raise RuntimeError(f"no answers in Jev response: {response.text[:300]}")
    return answers


def _decide(entry: dict, state: dict) -> set[str]:
    questions = {group: {"type": "noul", "instructions": spec[0]} for group, spec in TOOL_GROUPS.items()}
    answers = ask_jev(entry, state, questions)
    return {
        group for group, answer in answers.items()
        if group in TOOL_GROUPS and isinstance(answer, dict) and (answer.get("noul") or 0) >= PRELOAD_THRESHOLD
    }


def _reply_text(part) -> str:
    kind = getattr(part, "part_kind", "")
    if kind == "text":
        return part.content or ""
    # A text_only_response call is a whole reply sent as a tool call (agent.agent.OUTPUT_TYPE).
    if kind == "tool-call" and part.tool_name == "text_only_response":
        try:
            return str(part.args_as_dict().get("response") or "")
        except Exception:
            return ""
    return ""


def _last_reply_text(history) -> str:
    for message in reversed(history or []):
        if getattr(message, "kind", "") == "response":
            text = "".join(_reply_text(p) for p in message.parts)
            if text.strip():
                return text
    return ""


def preload_note(groups: set[str]) -> str:
    """Turn-context line telling the model what's already loaded, so it calls
    those tools directly instead of searching for them first."""
    names = sorted(tools_for(groups))
    if SLACK_MCP_GROUP in groups:
        names.append("the Slack MCP tools (canvases, lists, drafts, scheduled messages, profiles, channel members)")
    if LIBRARY_DOCS_GROUP in groups:
        names.append("Context7 (`resolve-library-id`, `query-docs`)")
    if not names:
        return ""
    listed = ", ".join(n if n.startswith(("the ", "Context7")) else f"`{n}`" for n in names)
    return f"[Already loaded for this turn — call directly, no search_tools needed: {listed}]\n\n"


def start_preload(text: str, history=None) -> PreloadRequest | None:
    """Kick off the Jev request in the background; None if Jev isn't
    configured or is currently marked down. Never raises."""
    try:
        entry = _usable_jev()
        if entry is None:
            return None
        state = {"message": (text or "")[:_STATE_CHARS]}
        previous = _last_reply_text(history)
        if previous:
            state["coolton_previous_reply"] = previous[:_STATE_CHARS]
        return PreloadRequest(_executor.submit(_decide, entry, state), time.monotonic(), entry["name"])
    except Exception:
        logger.exception("Jev tool preload: couldn't start")
        return None


def collect_preloads(request: PreloadRequest | None) -> set[str]:
    """The tool groups Jev said this turn needs; empty if there's no request,
    or it failed or ran past JEV_TIMEOUT_SECONDS total (which marks Jev dead so
    later turns skip it until the background refresh sees it working). Never raises."""
    if request is None:
        return set()
    from agent.fallback_cache import mark_alive, mark_dead

    remaining = max(0.0, JEV_TIMEOUT_SECONDS - (time.monotonic() - request.started))
    try:
        groups = request.future.result(timeout=remaining)
    except FutureTimeout:
        logger.warning("Jev tool preload: no answer within %.1fs, skipping (marked down)", JEV_TIMEOUT_SECONDS)
        mark_dead(request.name, f"Jev took over {JEV_TIMEOUT_SECONDS:g}s")
        return set()
    except Exception as e:
        logger.warning("Jev tool preload failed, skipping (marked down): %s", e)
        mark_dead(request.name, f"Jev failed: {e}")
        return set()
    mark_alive(request.name)
    if groups:
        logger.info("Jev tool preload: loading %s", ", ".join(sorted(groups)))
    return groups


def probe_jev() -> None:
    """Background health check (agent.provider_probe): mark each Jev entry
    alive or dead, so a recovered Jev is used again. Never raises."""
    from agent.fallback_cache import mark_alive, mark_dead
    from agent.provider_config import build_jev_provider_order

    for entry in build_jev_provider_order():
        try:
            ask_jev(entry, "the build is broken", {"q": {"type": "noul", "instructions": "Is something broken?"}})
        except Exception as e:
            logger.warning("Provider probe: %s FAILED: %s", entry["name"], e)
            mark_dead(entry["name"], f"background Jev probe failed: {e}")
        else:
            logger.info("Provider probe: %s OK", entry["name"])
            mark_alive(entry["name"])
