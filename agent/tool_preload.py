"""Ask Jev which deferred tools this turn will need, and load them up front.

Rarely used tools are deferred (see agent.agent.DEFERRED_TOOLS and the MCP
toolsets in agent.platforms.slack): the model only gets them after calling
search_tools, which costs a whole extra model round trip. Jev (TypeSafe's
"System One" model, via HCAI) answers one yes/no question per tool group
from the user's message in well under a second, and the groups it says yes
to are loaded from the start. search_tools still works for anything Jev
missed.

"Loaded" means exactly what coolton's own search_tools call does: run_agent
adds a search_tools call and its result for those tools to the turn
(agent.agent._preload_search_exchange). The tool list itself never changes, so
the request prefix stays byte-identical and the shared prompt cache still hits.

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
# The Slack MCP and library-docs groups load MCP tools instead (MCP_GROUP_TOOLS).
# Questions are deliberately broad (a missed preload costs a model round trip, an
# extra one only some prompt tokens), and _question adds the group's tool names.
TOOL_GROUPS: dict[str, tuple[str, frozenset[str]]] = {
    "email": ("Could this involve email in any way: reading, checking, searching, sending or replying to emails, an email address or inbox, or coolton's own inbox?", frozenset({
        "agentmail_create_inbox", "agentmail_list_inboxes", "agentmail_list_messages",
        "agentmail_read_message", "agentmail_send_email"})),
    "huddlefm": ("Is this about HuddleFM, music or a Slack huddle in any way: playing, queueing, skipping or DJing songs, the volume, or what's playing?", frozenset({
        "huddlefm_request_control_tool", "huddlefm_command_tool"})),
    "slack_bot_building": ("Does this involve making or changing a Slack bot or Slack app: creating, configuring, installing or deploying one, or its manifest, tokens or scopes?", frozenset({
        "create_slack_bot_tool", "check_bot_install_status_tool", "register_bot_tokens_tool",
        "update_slack_bot_manifest_tool", "wrangler_bot_deploy_tool"})),
    "scheduled_tasks": ("Does this involve doing something on a schedule: a recurring task (every day, every week, every hour, a cron job), or listing, changing, pausing, resuming or deleting scheduled tasks?", frozenset({
        "create_scheduled_task_tool", "list_scheduled_tasks_tool", "pause_scheduled_task_tool",
        "resume_scheduled_task_tool", "delete_scheduled_task_tool"})),
    "code_channels": ("Does this involve a code channel, such as asking coolton to create one?", frozenset({"create_code_channel_tool"})),
    "data_analysis": ("Does this involve analyzing data: a CSV, Excel or spreadsheet file, a SQL query, statistics, or charts and graphs of numbers?", frozenset({
        "analyze_csv_tool", "run_sql_on_csv_tool", "run_python_data_analysis_tool"})),
    "archives": ("Does this involve an archive file to extract or unpack, like a .tar.gz, .tgz or .tar?", frozenset({"extract_tar_gz_tool"})),
    "opencode": ("Does this mention opencode, or ask coolton to hand coding work to another coding agent?", frozenset({"install_opencode_tool", "run_opencode_tool"})),
    "embeds": ("Does this ask coolton to build an interactive HTML page: a web embed, a mini app or game, a calculator or form, or a whiteboard to draw on?", frozenset({
        "send_html_embed_tool", "send_whiteboard_embed_tool"})),
    "diagrams": ("Does this ask for a diagram of any kind: a flowchart, sequence diagram, mind map, architecture or relationship diagram, or Mermaid?", frozenset({"render_mermaid_tool"})),
    "custom_emoji": ("Does this involve custom Slack emoji: adding, uploading or making a new one?", frozenset({"upload_emoji_tool"})),
    "coolton_feedback": ("Is the user reporting a bug or problem with coolton itself, complaining about it, or giving feedback, ideas or suggestions about coolton?", frozenset({"submit_feedback_tool"})),
    "skills": ("Does this involve changing coolton's skills (its reusable playbooks): creating, installing, renaming, editing or deleting one?", frozenset({
        "create_skill", "rename_skill", "delete_skill", "install_skill"})),
    "live_view": ("Does the user want to watch what coolton is doing live: a live stream or view link of its browser or desktop, or keeping its sandbox running?", frozenset({
        "set_sandbox_keepalive_tool", "computer_stream_tool", "agent_browser_stream_tool"})),
    "slack_admin": ("Does this involve managing Slack channels or acting as coolton's bot: inviting coolton's helper account to a channel, removing a reaction, leaving a channel, or calling the Slack API as the bot (posting as the bot, topics, bookmarks, invites, creating channels)?", frozenset({
        "invite_coolton_user_to_channel", "remove_reaction_tool", "leave_channel_tool", "slack_api_call_as_bot_tool"})),
    # The Slack MCP tools are split small: their definitions are large (~11k tokens for all
    # twelve, ~5.6k for the canvas ones alone).
    "slack_canvases": ("Does this involve a Slack canvas in any way: creating, opening, reading, viewing, summarizing, editing or updating one, or a link to one (slack.com/docs/...)?", frozenset()),
    "slack_lists": ("Does this involve a Slack list (Slack's spreadsheet-like lists with items and columns, not a bullet list) in any way: creating, reading, viewing, editing, or adding or changing its items, or a link to one?", frozenset()),
    "slack_drafts_scheduling": ("Does this ask coolton to draft a Slack message for the user, or to send a message later at a set time?", frozenset()),
    "slack_people": ("Does this involve looking up Slack people: someone's profile, title, timezone, pronouns or status, or who is in a channel?", frozenset()),
    "library_docs": ("Is this about using a programming library, framework, SDK, CLI or API: how to call it, its docs or versions, or code that uses it?", frozenset()),
}
LIBRARY_DOCS_GROUP = "library_docs"

# Abuse checks asked in the same Jev call (agent.abuse_report): a flag tells the turn to
# check and report it, it never stops or reports anything by itself. A higher bar than
# tool preloading, since a false flag is worse than a missed preload.
ABUSE_CHECKS: dict[str, str] = {
    "abuse_nsfw": "Is the user asking coolton for sexual, explicit or NSFW content, or something close to it (sexualized roleplay, explicit descriptions, adult content)?",
    "abuse_spam": "Is the user trying to use coolton to spam: send many or unsolicited messages, DMs or mentions to people or channels, or flood a channel?",
    "abuse_vulnerability": "Does the message describe or try to exploit a security hole in coolton itself, such as getting it to leak secrets or tokens, get around its access rules, or run code it shouldn't?",
}
ABUSE_THRESHOLD = 0.7

# Deferred MCP tools each MCP group loads (names as the servers publish them).
# A name a server doesn't have this turn (e.g. Context7 down) is just ignored.
MCP_GROUP_TOOLS: dict[str, frozenset[str]] = {
    "slack_canvases": frozenset({"slack_create_canvas", "slack_read_canvas", "slack_update_canvas"}),
    "slack_lists": frozenset({
        "slack_create_list", "slack_read_list", "slack_update_list",
        "slack_add_list_record", "slack_update_list_record"}),
    "slack_drafts_scheduling": frozenset({"slack_send_message_draft", "slack_schedule_message"}),
    "slack_people": frozenset({"slack_read_user_profile", "slack_list_channel_members"}),
    LIBRARY_DOCS_GROUP: frozenset({"resolve-library-id", "query-docs"}),
}

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="jev-preload")


@dataclass
class PreloadRequest:
    future: Future
    started: float
    name: str


def tools_for(groups: set[str]) -> frozenset[str]:
    """The deferred function tools (agent.agent.DEFERRED_TOOLS) these groups load."""
    return frozenset().union(*(TOOL_GROUPS[g][1] for g in groups if g in TOOL_GROUPS))


def mcp_tools_for(groups: set[str]) -> frozenset[str]:
    return frozenset().union(*(MCP_GROUP_TOOLS.get(g, frozenset()) for g in groups))


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


def _question(group: str) -> str:
    """A TOOL_GROUPS question, followed by the names of the tools it loads."""
    names = sorted(TOOL_GROUPS[group][1] | MCP_GROUP_TOOLS.get(group, frozenset()))
    return f"{TOOL_GROUPS[group][0]} (Tools: {', '.join(names)}.)"


def _decide(entry: dict, state: dict) -> set[str]:
    """The tool groups to load and the ABUSE_CHECKS keys flagged, from one Jev call."""
    questions = {group: {"type": "noul", "instructions": _question(group)} for group in TOOL_GROUPS}
    questions.update({key: {"type": "noul", "instructions": q} for key, q in ABUSE_CHECKS.items()})
    answers = ask_jev(entry, state, questions)
    picked = set()
    for key, answer in answers.items():
        score = (answer.get("noul") or 0) if isinstance(answer, dict) else 0
        if key in TOOL_GROUPS and score >= PRELOAD_THRESHOLD:
            picked.add(key)
        elif key in ABUSE_CHECKS and score >= ABUSE_THRESHOLD:
            picked.add(key)
    return picked


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
