"""coolton's tools for working in a code channel (agent.code_channel_api): its tabs
(HTML pages, the diff, Block Kit, a plan canvas, a pull request), the context bar, the
plan canvas's comments, per-channel slash commands, renaming and archiving.

Every tool acts on the conversation's own channel, and only when it's a code channel.
"""
from __future__ import annotations

import json
import re

from agent.code_channel_api import call, error_text
from agent.code_channel_store import is_code_channel

VIEW_TYPES = ("html", "diff", "block_kit", "canvas", "pull_request")
CONTEXT_BAR_ICONS = ("branch", "folder", "hierarchy", "life-ring", "link", "globe", "terminal", "code", "search", "lock")
_COMMAND_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")


def not_a_code_channel(channel_id: str) -> str | None:
    if not is_code_channel(channel_id):
        return "Error: this isn't a code channel, so it has no tabs, context bar or commands."
    return None


def _json(value, what: str):
    if isinstance(value, (list, dict)):
        return value, None
    try:
        return json.loads(value or ""), None
    except ValueError as e:
        return None, f"Error: {what} must be JSON ({e})."


def _views(channel_id: str) -> list[dict]:
    return call("agents.conversations.listViews", channel_id=channel_id).get("views") or []


def _canvas_id(channel_id: str, view_key: str) -> str | None:
    view = next((v for v in _views(channel_id) if v.get("view_key") == view_key), None)
    return (view or {}).get("file_id")


def set_view(channel_id: str, view_type: str, view_key: str = "", name: str = "", content: str = "",
             blocks: str = "", markdown: str = "", pr_url: str = "", base_branch: str = "",
             head_branch: str = "", access_level: str = "comment", resource_domains: str = "") -> str:
    """Create or update a tab (see agent.agent.code_channel_view_tool)."""
    denied = not_a_code_channel(channel_id)
    if denied:
        return denied
    view_type = (view_type or "html").strip().lower()
    if view_type not in VIEW_TYPES:
        return f"Error: view_type must be one of {', '.join(VIEW_TYPES)}."
    params: dict = {"channel_id": channel_id, "type": view_type}
    if name:
        params["name"] = name
    if view_type in ("html", "block_kit", "canvas"):
        if not view_key:
            return f"Error: a {view_type} tab needs a view_key (its stable id; reuse it to update the tab)."
        params["view_key"] = view_key
    if view_type == "html":
        if not content:
            return "Error: an html tab needs content: a full HTML document."
        params["content"] = content
        domains = [d.strip() for d in (resource_domains or "").split(",") if d.strip()]
        if domains:
            params["csp"] = {"resource_domains": domains}
    elif view_type == "diff":
        if not content:
            return "Error: a diff tab needs content: unified diff text (git diff output)."
        params.update(content=content, **{k: v for k, v in (("base_branch", base_branch), ("head_branch", head_branch)) if v})
    elif view_type == "block_kit":
        parsed, error = _json(blocks, "blocks")
        if error:
            return error
        params["blocks"] = parsed
    elif view_type == "pull_request":
        if not pr_url:
            return "Error: a pull_request tab needs pr_url."
        params["pr_url"] = pr_url
    elif view_type == "canvas":
        if not markdown:
            return "Error: a canvas tab needs markdown content."
        canvas_id = _canvas_id(channel_id, view_key)
        if canvas_id:  # update in place, keeping comments on unchanged sections
            response = call("agents.conversations.setCanvasContent", channel=channel_id, canvas_id=canvas_id, content=markdown)
            if not response.get("ok"):
                return error_text(response)
            return f"Updated the {view_key} canvas ({response.get('sections_changed_count', 0)} sections changed)."
        created = call("canvases.create", title=name or view_key,
                       document_content={"type": "markdown", "markdown": markdown})
        if not created.get("ok"):
            return error_text(created)
        params.update(canvas_id=created["canvas_id"], access_level=access_level or "comment")
    response = call("agents.conversations.setView", **params)
    if not response.get("ok"):
        return error_text(response)
    return (f"Tab {'updated' if (response.get('content_version') or 1) > 1 else 'added'}: view_id "
            f"{response.get('view_id')} (link to it in a message with a rich_text channel element whose "
            f"tab_id is that view_id).")


def list_views(channel_id: str) -> str:
    denied = not_a_code_channel(channel_id)
    if denied:
        return denied
    response = call("agents.conversations.listViews", channel_id=channel_id)
    if not response.get("ok"):
        return error_text(response)
    views = response.get("views") or []
    if not views:
        return "This code channel has no tabs yet."
    return "\n".join(
        f"- {v.get('label') or v.get('name') or '(unnamed)'}: view_key={v.get('view_key', '(diff)')}, "
        f"view_id={v.get('view_id')}, version {v.get('content_version')}" for v in views)


def remove_view(channel_id: str, view_key: str = "", view_id: str = "") -> str:
    denied = not_a_code_channel(channel_id)
    if denied:
        return denied
    if bool(view_key) == bool(view_id):
        return "Error: give exactly one of view_key or view_id."
    response = call("agents.conversations.removeView", channel_id=channel_id,
                    **({"view_key": view_key} if view_key else {"view_id": view_id}))
    return "Tab removed." if response.get("ok") else error_text(response)


def read_canvas(channel_id: str, view_key: str, include_resolved: bool = False) -> str:
    denied = not_a_code_channel(channel_id)
    if denied:
        return denied
    canvas_id = _canvas_id(channel_id, view_key)
    if not canvas_id:
        return f"Error: no canvas tab with view_key {view_key!r} in this channel."
    response = call("agents.conversations.getCanvas", channel=channel_id, canvas_id=canvas_id,
                    include_resolved=include_resolved)
    if not response.get("ok"):
        return error_text(response)
    lines = [f"# Canvas: {response.get('title') or view_key}", "", response.get("content") or "", "", "## Comments"]
    comments = response.get("comments") or []
    for c in comments:
        lines.append(f"- <@{c.get('user_id')}> on \"{c.get('quoted_text') or ''}\""
                     f"{' (resolved)' if c.get('is_resolved') else ''}: {c.get('text')}")
        lines += [f"  - reply from <@{r.get('user_id')}>: {r.get('text')}" for r in c.get("replies") or []]
    if not comments:
        lines.append("(none)")
    return "\n".join(lines)


def set_context_bar(channel_id: str, items: str) -> str:
    denied = not_a_code_channel(channel_id)
    if denied:
        return denied
    parsed, error = _json(items, "items")
    if error:
        return error
    if not isinstance(parsed, list) or len(parsed) > 5:
        return "Error: items must be a JSON array of at most 5 items."
    for item in parsed:
        if not isinstance(item, dict) or not item.get("key") or not item.get("label"):
            return "Error: every item needs a key and a label."
        item.pop("item_type", None)  # action items can't reach coolton yet (listeners.code_channel_app)
        if item.get("icon") and item["icon"] not in CONTEXT_BAR_ICONS:
            return f"Error: icon must be one of {', '.join(CONTEXT_BAR_ICONS)}."
    response = call("agents.conversations.setProperties", channel_id=channel_id,
                    code_channel={"context_bar_items": parsed})
    return "Context bar updated." if response.get("ok") else error_text(response)


def set_commands(channel_id: str, commands: str) -> str:
    denied = not_a_code_channel(channel_id)
    if denied:
        return denied
    parsed, error = _json(commands, "commands")
    if error:
        return error
    if not isinstance(parsed, list) or len(parsed) > 10:
        return "Error: commands must be a JSON array of at most 10 commands."
    for command in parsed:
        if not isinstance(command, dict) or not command.get("description"):
            return "Error: every command needs a name and a description."
        command["name"] = str(command.get("name") or "").lstrip("/")
        if not _COMMAND_NAME_RE.match(command["name"]):
            return f"Error: {command['name']!r} isn't a valid command name (1-31 lowercase letters, digits, - or _)."
    response = call("agents.conversations.setCommands", channel_id=channel_id, commands=parsed)
    if not response.get("ok"):
        return error_text(response)
    return f"Registered {response.get('command_count', len(parsed))} slash commands in this channel."


def rename(channel_id: str, title: str) -> str:
    denied = not_a_code_channel(channel_id)
    if denied:
        return denied
    response = call("agents.sessions.rename", channel_id=channel_id, title=title[:200])
    return "Renamed." if response.get("ok") else error_text(response)


def archive(client, channel_id: str, summary: str) -> str:
    """Post `summary` in the channel (as coolton), then archive it with that message
    as its summary (also shared on the request's original message, when it has one)."""
    denied = not_a_code_channel(channel_id)
    if denied:
        return denied
    summary_ts = None
    if summary:
        try:
            summary_ts = client.chat_postMessage(channel=channel_id, markdown_text=summary)["ts"]
        except Exception as e:
            return f"Error: couldn't post the summary: {e}"
    response = call("agents.conversations.archive", channel_id=channel_id,
                    **({"summary_message_ts": summary_ts} if summary_ts else {}))
    if not response.get("ok") and response.get("error") == "no_origin_link" and summary_ts:
        response = call("agents.conversations.archive", channel_id=channel_id)
    if not response.get("ok"):
        return error_text(response)
    from agent.code_channel_store import unregister_code_channel
    unregister_code_channel(channel_id)
    return "Archived the code channel."
