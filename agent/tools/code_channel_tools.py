"""coolton's tools for working in a code channel (agent.code_channel_api): its tabs
(HTML pages, the diff, Block Kit, a plan canvas, a pull request), the context bar, the
plan canvas's comments, per-channel slash commands, renaming and archiving.

Every tool acts on the conversation's own channel, and only when it's a code channel.
"""
from __future__ import annotations

import json
import re
import threading

from agent.code_channel_api import call, error_text
from agent.code_channel_store import (
    canvas_views, forget_canvas_view, forget_view, is_code_channel, remember_canvas_view, remember_view,
    views as stored_views,
)

VIEW_TYPES = ("html", "diff", "block_kit", "canvas", "pull_request")
CONTEXT_BAR_ICONS = ("branch", "folder", "hierarchy", "life-ring", "link", "globe", "terminal", "code", "search", "lock")
# One setView at a time per channel: Slack fails tab creations that race each other in
# the same channel with view_creation_failed (seen live when coolton made several tabs
# with parallel tool calls).
_view_locks: dict[str, threading.Lock] = {}
_view_locks_guard = threading.Lock()


def _view_lock(channel_id: str) -> threading.Lock:
    with _view_locks_guard:
        return _view_locks.setdefault(channel_id, threading.Lock())


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


def _canvas_id(channel_id: str, view_key: str) -> str | None:
    """The canvas behind a canvas tab coolton added (Slack itself keeps no view_key for
    canvas tabs; agent.code_channel_store.remember_canvas_view)."""
    return (canvas_views(channel_id).get(view_key) or {}).get("canvas_id")


def set_view(channel_id: str, view_type: str, view_key: str = "", name: str = "", content: str = "",
             blocks: str = "", markdown: str = "", pr_url: str = "", base_branch: str = "",
             head_branch: str = "", access_level: str = "comment", resource_domains: str = "") -> str:
    """Create a tab, or update one by its view_key (see agent.agent.code_channel_create_view_tool)."""
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
    with _view_lock(channel_id):
        response = call("agents.conversations.setView", **params)
    if response.get("error") == "view_creation_failed":
        return (error_text(response) + ". Slack couldn't add it as a tab; if it keeps failing for this "
                "view_key, try a new view_key.")
    if not response.get("ok"):
        return error_text(response)
    if view_type == "canvas":
        remember_canvas_view(channel_id, view_key, params["canvas_id"], response.get("view_id", ""), name)
    remember_view(channel_id, view_key or view_type, view_type, response.get("view_id", ""),
                  response.get("file_id") or params.get("canvas_id", ""), name)
    return (f"Tab {'updated' if (response.get('content_version') or 1) > 1 else 'added'}: view_key "
            f"{view_key or view_type}, view_id {response.get('view_id')} (link to it in a message with a "
            f"rich_text channel element whose tab_id is that view_id).")


def _channel_tabs(client, channel_id: str) -> list[dict] | None:
    """Every tab Slack shows in the channel, from conversations.info's properties.tabs:
    {"id" (the view_id), "label", "type" ("agent_view" for HTML and Block Kit, "canvas"),
    "data": {"file_id"}}. The only complete list: listViews leaves out Block Kit and canvas
    tabs. The diff isn't in it (it lives in the built-in Code tab). None if it can't be read."""
    try:
        channel = client.conversations_info(channel=channel_id)["channel"]
    except Exception:
        return None
    return (channel.get("properties") or {}).get("tabs") or []


def _tabs(client, channel_id: str) -> list[dict]:
    """The channel's tabs, each put together from properties.tabs, listViews and what
    coolton remembers: {"view_id", "view_key" (if known), "type", "name", "file_id"}."""
    remembered = {**{k: {**c, "type": "canvas", "file_id": c.get("canvas_id")} for k, c in canvas_views(channel_id).items()},
                  **stored_views(channel_id)}
    listed = call("agents.conversations.listViews", channel_id=channel_id).get("views") or []
    by_id: dict[str, dict] = {}
    for key, v in remembered.items():
        by_id[v.get("view_id") or key] = dict(view_id=v.get("view_id"), view_key=key, type=v.get("type"),
                                             name=v.get("name") or key, file_id=v.get("file_id"))
    for v in listed:
        tab = by_id.setdefault(v.get("view_id"), dict(view_id=v.get("view_id"), type="diff" if v.get("view_id") == "code" else "html"))
        tab.update({k: val for k, val in (("view_key", v.get("view_key")), ("file_id", v.get("file_id")),
                                          ("name", v.get("label") or v.get("name"))) if val})
    shown = _channel_tabs(client, channel_id)
    if shown is None:  # can't see the channel's tabs: fall back to what's known
        return list(by_id.values())
    tabs = []
    for t in shown:
        tab = by_id.get(t.get("id")) or dict(view_id=t.get("id"))
        tab.setdefault("type", "canvas" if t.get("type") == "canvas" else "html or block_kit")
        tab["name"] = t.get("label") or tab.get("name")
        tab["file_id"] = (t.get("data") or {}).get("file_id") or tab.get("file_id")
        tabs.append(tab)
    diff = by_id.get("code") or next((v for v in by_id.values() if v.get("type") == "diff"), None)
    if diff:
        tabs.append(diff)
    return tabs


def _find_tab(client, channel_id: str, view_key: str, view_id: str) -> dict | None:
    return next((t for t in _tabs(client, channel_id)
                 if (view_id and t.get("view_id") == view_id) or (view_key and t.get("view_key") == view_key)), None)


def list_views(client, channel_id: str) -> str:
    """Every tab in this code channel, with what's needed to read or delete it."""
    denied = not_a_code_channel(channel_id)
    if denied:
        return denied
    lines = []
    for t in _tabs(client, channel_id):
        key = f"view_key={t['view_key']}" if t.get("view_key") else "view_key unknown (use the view_id)"
        name = "the diff, in the Code tab" if t.get("type") == "diff" else t.get("name") or "(unnamed)"
        lines.append(f"- {name} ({t.get('type')}): {key}, view_id={t.get('view_id')}")
    if not lines:
        return "This code channel has no tabs yet."
    return "\n".join(lines) + "\n(Up to 5 tabs, at most one diff.)"


_READ_LIMIT = 40_000


def _download(client, file_id: str) -> str:
    """A Slack file's text, fetched with coolton's own bot (`client`)."""
    import requests

    info = client.files_info(file=file_id)["file"]
    url = info.get("url_private_download") or info.get("url_private")
    response = requests.get(url, headers={"Authorization": f"Bearer {client.token}"}, timeout=30)
    response.raise_for_status()
    return response.text


def read_view(client, channel_id: str, view_key: str = "", view_id: str = "") -> str:
    """A tab's content (HTML, diff or Block Kit JSON) from the Slack file behind it;
    canvas tabs go through read_canvas, which also returns their comments."""
    denied = not_a_code_channel(channel_id)
    if denied:
        return denied
    if bool(view_key) == bool(view_id):
        return "Error: give exactly one of view_key or view_id."
    tab = _find_tab(client, channel_id, view_key, view_id)
    if not tab:
        return "Error: there's no tab with that id here (code_channel_list_views_tool lists them all)."
    if tab.get("type") == "canvas":
        return read_canvas(channel_id, tab.get("view_key") or "", canvas_id=tab.get("file_id"))
    if not tab.get("file_id"):
        return "Error: Slack didn't say which file holds that tab's content, so it can't be read."
    try:
        content = _download(client, tab["file_id"])
    except Exception as e:
        return f"Error: couldn't read that tab's content ({e})."
    if len(content) > _READ_LIMIT:
        content = content[:_READ_LIMIT] + f"\n[... {len(content) - _READ_LIMIT} more characters cut]"
    return content


def remove_view(client, channel_id: str, view_key: str = "", view_id: str = "") -> str:
    """Delete a tab. A canvas tab is deleted by deleting its canvas (Slack's removeView
    can't remove canvas tabs, and deleting the canvas takes its tab with it)."""
    denied = not_a_code_channel(channel_id)
    if denied:
        return denied
    if bool(view_key) == bool(view_id):
        return "Error: give exactly one of view_key or view_id."
    tab = _find_tab(client, channel_id, view_key, view_id)
    if not tab:
        return ("Error: there's no tab with that id here. A view_key isn't the tab's name, and a creation that "
                "failed (e.g. too_many_views) never made a tab: code_channel_list_views_tool lists every tab.")
    if tab.get("type") == "canvas":
        if not tab.get("file_id"):
            return "Error: Slack didn't say which canvas that tab shows, so it can't be deleted."
        response = call("canvases.delete", canvas_id=tab["file_id"])
        if not response.get("ok"):
            return error_text(response)
    else:
        response = call("agents.conversations.removeView", channel_id=channel_id, view_id=tab["view_id"])
        if not response.get("ok"):
            return error_text(response)
    if tab.get("view_key"):
        forget_view(channel_id, tab["view_key"])
        forget_canvas_view(channel_id, tab["view_key"])
    return "Tab deleted." + (" Its canvas was deleted with it." if tab.get("type") == "canvas" else "")


def read_canvas(channel_id: str, view_key: str, include_resolved: bool = False, canvas_id: str | None = None) -> str:
    denied = not_a_code_channel(channel_id)
    if denied:
        return denied
    canvas_id = canvas_id or _canvas_id(channel_id, view_key)
    if not canvas_id:
        known = ", ".join(canvas_views(channel_id)) or "none"
        return f"Error: no canvas tab with view_key {view_key!r} here (canvas tabs: {known})."
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
