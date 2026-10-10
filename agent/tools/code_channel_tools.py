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
    canvas_views, forget_view, is_code_channel, remember_canvas_view, remember_view, views as stored_views,
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
    listed = response.get("views") or []
    lines = [f"- {v.get('label') or v.get('name') or '(unnamed)'}: view_key={v.get('view_key', '(diff)')}, "
             f"view_id={v.get('view_id')}, version {v.get('content_version')}" for v in listed]
    # Slack leaves Block Kit and canvas tabs out of listViews; add the ones coolton made.
    seen = {v.get("view_id") for v in listed}
    remembered = {**{k: {**c, "type": "canvas"} for k, c in canvas_views(channel_id).items()}, **stored_views(channel_id)}
    lines += [f"- {v.get('name') or key} ({v.get('type')}): view_key={key}, view_id={v.get('view_id')}"
              for key, v in remembered.items() if v.get("view_id") not in seen]
    return "\n".join(lines) if lines else "This code channel has no tabs yet."


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
    remembered = stored_views(channel_id)
    if view_id:
        view_key = next((k for k, v in remembered.items() if v.get("view_id") == view_id), "")
    tab = remembered.get(view_key) if view_key else None
    if view_key in canvas_views(channel_id) or (tab or {}).get("type") == "canvas":
        return read_canvas(channel_id, view_key)
    file_id = (tab or {}).get("file_id")
    if not file_id:
        response = call("agents.conversations.listViews", channel_id=channel_id)
        match = next((v for v in response.get("views") or []
                      if (view_key and v.get("view_key") == view_key) or (view_id and v.get("view_id") == view_id)), None)
        file_id = (match or {}).get("file_id")
    if not file_id:
        return ("Error: no tab I can read with that id here (list them with code_channel_list_views_tool). A Block "
                "Kit tab only shows up there, and can only be read, if you made it.")
    try:
        content = _download(client, file_id)
    except Exception as e:
        return f"Error: couldn't read that tab's content ({e})."
    if len(content) > _READ_LIMIT:
        content = content[:_READ_LIMIT] + f"\n[... {len(content) - _READ_LIMIT} more characters cut]"
    return content


def remove_view(channel_id: str, view_key: str = "", view_id: str = "") -> str:
    denied = not_a_code_channel(channel_id)
    if denied:
        return denied
    if bool(view_key) == bool(view_id):
        return "Error: give exactly one of view_key or view_id."
    if view_key in canvas_views(channel_id):
        return ("Error: Slack can't remove canvas tabs through its API yet. Ask someone to remove it from the "
                "channel's tabs in Slack, or keep it and update it instead.")
    response = call("agents.conversations.removeView", channel_id=channel_id,
                    **({"view_key": view_key} if view_key else {"view_id": view_id}))
    if not response.get("ok"):
        return error_text(response)
    key = view_key or next((k for k, v in stored_views(channel_id).items() if v.get("view_id") == view_id), "")
    if key:
        forget_view(channel_id, key)
    return "Tab removed."


def read_canvas(channel_id: str, view_key: str, include_resolved: bool = False) -> str:
    denied = not_a_code_channel(channel_id)
    if denied:
        return denied
    canvas_id = _canvas_id(channel_id, view_key)
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
