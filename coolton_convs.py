#!/usr/bin/env python3
"""Browse coolton's saved conversations in the terminal.

    python coolton_convs.py                    conversations under the current directory
    python coolton_convs.py DIR                conversations under DIR
    python coolton_convs.py FILE               one conversation file
    python coolton_convs.py ssh HOST [PATH]    the same on HOST over ssh. Without PATH it uses
                                               the directory named in HOST's ~/.coolton_convs

Reads every format coolton saves: Slack traces (conversation_logs/), conversation history
(conversations/ and the old conversations.json), and web UI logs (web_conversations/).

Standalone on purpose: it imports nothing from coolton. It needs Python 3.9+ and Textual,
which it installs into ~/.cache/coolton-convs on first run if it's missing. Over ssh the
server only needs python3: this file is piped to it to read the conversations there, and
only the one you open is sent back.

Press ? in the viewer for every key.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

TEXTUAL_REQUIREMENT = "textual>=8.2,<9"
VENV_DIR = Path(os.environ.get("COOLTON_CONVS_VENV", "~/.cache/coolton-convs/venv")).expanduser()
CONFIG_FILE = "~/.coolton_convs"

# ─────────────────────────────── reading conversations (stdlib only) ───────────────────────────────
# Everything in this section also runs on the remote side of `ssh HOST`, so it must not
# import Textual or anything outside the standard library.

SUFFIXES = (".json", ".jsonl", ".json.migrated")
SKIP_DIRS = {"node_modules", "__pycache__", "site-packages", ".git", ".venv", "venv", "web_attachments",
             "skills", "dist", "build"}
MAX_DEPTH = 3
# A file is only parsed if its start mentions one of these (state files like providers.json don't).
MARKERS = ('"messages"', '"slack_messages"', '"schema_version"', '"parts"', '"user_message"', '"part_kind"')
OUTPUT_TOOLS = {"text_only_response", "final_result"}

# "U0123ABCD (Lily):" — a display name can hold parentheses itself ("Lily ^-^ (KitKat)").
_SENDER = re.compile(r"^(?P<id>[UWB][A-Z0-9]{6,})(?: \((?P<name>[^\n]*)\))?:[ \t]*$")
_OTHER_BOT = re.compile(r"^\[other bot\] (?P<name>[^:\n]+):[ \t]*")
_MODEL_IN_CONTEXT = re.compile(r"\bmodel: ([^\s,()\]]+)")
_WHERE = re.compile(r"This conversation is (?:a |an )?(?P<what>[^\n]+)")
_CHANNEL_NAME = re.compile(r"#([\w.-]+)")
_SELF_ID = re.compile(r"Your own bot user id[^`\n]*`([A-Z0-9]+)`")
_MENTION = re.compile(r"<([@#!])([^>|]*)(?:\|([^>]*))?>")
_LINK = re.compile(r"<(https?://[^>|]+)(?:\|([^>]*))?>")
_LEADING_MENTIONS = re.compile(r"^(?:\s*(?:<[@!][^>]*>|@\S+)[,:]?)+\s*")
_BASE64ISH = re.compile(r"^[A-Za-z0-9+/_=\-\s]+$")


def _read(path: str):
    """The parsed file: a JSON value, or for .jsonl a list of its records."""
    with open(path, encoding="utf-8") as f:
        if path.endswith(".jsonl"):
            records = []
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        pass
            return records
        return json.load(f)


def _looks_like_conversation_file(path: str) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(65536)
    except OSError:
        return True  # let the real read report why it can't be opened
    return any(marker in head for marker in MARKERS)


def _files(root: str) -> list[str]:
    if os.path.isfile(root):
        return [root]
    found = []
    base_depth = root.rstrip(os.sep).count(os.sep)
    for directory, subdirs, names in os.walk(root):
        depth = directory.rstrip(os.sep).count(os.sep) - base_depth
        subdirs[:] = sorted(d for d in subdirs if not d.startswith(".") and d not in SKIP_DIRS and depth < MAX_DEPTH)
        for name in sorted(names):
            if name.endswith(SUFFIXES) and not name.startswith("."):
                found.append(os.path.join(directory, name))
    return found


def _conversations_in(path: str, data) -> list:
    """(subkey, document, format) for every conversation the parsed file holds."""
    if path.endswith(".jsonl"):
        if any(isinstance(r, dict) and r.get("type") in ("user_message", "turn_start", "step") for r in data):
            return [(None, data, "web")]
        return []
    if isinstance(data, list):
        if data and all(isinstance(m, dict) and "parts" in m for m in data[:5]):
            return [(None, {"messages": data}, "history")]
        return []
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("messages"), list):
        return [(None, data, "trace" if "slack_messages" in data or "schema_version" in data else "history")]
    held = [(key, value, "history") for key, value in data.items()
            if isinstance(value, dict) and isinstance(value.get("messages"), list)]
    return held if held and len(held) >= len(data) / 2 else []


def _when(value) -> float | None:
    """A POSIX time from a number, a Slack ts, or an ISO 8601 string."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _shrink(value):
    """`value` with embedded file bytes (images, screenshots) replaced by a note of their size."""
    if isinstance(value, dict):
        data = value.get("data")
        if isinstance(data, str) and len(data) > 300:
            return {**{k: _shrink(v) for k, v in value.items() if k != "data"},
                    "data": f"<{value.get('media_type') or 'file'}, {len(data) * 3 // 4:,} bytes>"}
        url = value.get("url")
        if isinstance(url, str) and url.startswith("data:") and len(url) > 300:
            return {**{k: _shrink(v) for k, v in value.items() if k != "url"}, "url": f"<data: URL, {len(url):,} chars>"}
        return {k: _shrink(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shrink(v) for v in value]
    if isinstance(value, str) and len(value) > 20000 and " " not in value[:2000] and _BASE64ISH.match(value[:2000]):
        return f"<{len(value):,} characters of encoded data>"
    return value


def _content_text(content) -> str:
    """A part's content as text: strings joined, attached files as a short note."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(_content_text(c) for c in content if c is not None)
    if isinstance(content, dict):
        if "data" in content or "url" in content or content.get("kind") in ("binary", "image-url", "document-url"):
            kind = content.get("media_type") or content.get("kind") or "file"
            detail = content.get("data") if isinstance(content.get("data"), str) and content["data"].startswith("<") else ""
            return f"[attached {kind}{' ' + detail if detail else ''}]"
        return json.dumps(content, ensure_ascii=False, indent=2)
    return str(content)


def _args(value):
    """Tool call arguments as a dict when they're JSON, else as given."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed
        except ValueError:
            return value
    return value


def _part_type(part: dict) -> str:
    kind = part.get("type") or part.get("part_kind") or ""
    return {
        "user-prompt": "user", "text": "output",
        "tool-call": "tool_call", "builtin-tool-call": "tool_call", "tool-search-call": "tool_call",
        "tool-return": "tool_result", "builtin-tool-return": "tool_result", "tool-search-return": "tool_result",
    }.get(kind, kind)


def _parse_prompt(text: str) -> dict:
    """Split a user prompt coolton built into the per-turn context it added and the
    message itself: {"context", "who_id", "who", "text", "model", "where", "self_id"}."""
    out = {"context": "", "who_id": None, "who": None, "text": text, "model": None, "where": None, "self_id": None}
    context = []
    body = text.lstrip("\n")
    while True:
        if body.startswith("## USER'S CUSTOM INSTRUCTIONS"):
            # Custom instructions can hold blank lines; they end where the turn context starts.
            end = body.find("\n\n[Turn context")
            block, body = (body[:end], body[end + 2:]) if end >= 0 else body.partition("\n\n")[::2]
            context.append(block)
            continue
        if body.startswith("## CURRENT CONTEXT"):
            block, _, body = body.partition("\n\n")
            context.append(block)
            where = _WHERE.search(block)
            if where:
                out["where"] = where["what"].strip()
            me = _SELF_ID.search(block)
            if me:
                out["self_id"] = me[1]
            continue
        if body.startswith("[") and not body.startswith("[other bot]"):
            block, sep, rest = body.partition("\n\n")
            if block.rstrip().endswith("]") and "\n" not in block.strip()[:-1] or block.startswith("[Turn context"):
                context.append(block)
                model = _MODEL_IN_CONTEXT.search(block)
                if model and block.startswith("[Turn context"):
                    out["model"] = model[1]
                body = rest
                if sep:
                    continue
        break
    body = body.lstrip("\n")
    first, _, rest = body.partition("\n")
    sender = _SENDER.match(first)
    if sender:
        out["who_id"], out["who"], body = sender["id"], sender["name"], rest
    else:
        bot = _OTHER_BOT.match(first)
        if bot:
            out["who"] = bot["name"].strip()
            body = body[bot.end():]
    out["context"] = "\n\n".join(context)
    out["text"] = body.strip("\n")
    return out


def _channel_name(description: str | None) -> str | None:
    """"#name" or "#name (private)" from the prompt's "This conversation is …" line."""
    name = _CHANNEL_NAME.search(description or "")
    if not name:
        return None
    return "#" + name[1] + (" (private)" if "private" in (description or "") else "")


def _where(channel_name: str | None, channel_id: str | None, thread_ts: str | None) -> str:
    if channel_id and channel_id.startswith("web"):
        return "web"
    if channel_id and channel_id.startswith("D"):
        return "DM"
    label = channel_name or channel_id or "?"
    # A code channel's conversation is the whole channel, so it has no thread.
    return label if thread_ts or not channel_id else label + " · code channel"


def _entries_from_messages(messages: list, users: dict) -> tuple[list, dict]:
    entries: list[dict] = []
    calls: dict[str, dict] = {}
    info = {"model": None, "where": None, "self_id": None, "usage": {"input": 0, "output": 0, "cache_read": 0}}
    turn = 0
    instructions = None
    for message in messages:
        if not isinstance(message, dict):
            continue
        kind = message.get("kind") or message.get("role")
        model_name = message.get("model_name")
        stamp = _when(message.get("timestamp"))
        usage = message.get("usage") if isinstance(message.get("usage"), dict) else None
        if usage:
            info["usage"]["input"] += usage.get("input_tokens") or usage.get("request_tokens") or 0
            info["usage"]["output"] += usage.get("output_tokens") or usage.get("response_tokens") or 0
            info["usage"]["cache_read"] += usage.get("cache_read_tokens") or 0
        if kind == "request" and message.get("instructions") and message["instructions"] != instructions:
            instructions = message["instructions"]
            entries.append({"kind": "system", "title": "instructions", "text": instructions, "ts": stamp,
                            "raw": {"instructions": instructions}})
        for part in message.get("parts") or []:
            if not isinstance(part, dict):
                continue
            ptype = _part_type(part)
            ts = _when(part.get("timestamp")) or stamp
            raw = _shrink(part)
            if ptype == "system-prompt":
                entries.append({"kind": "system", "title": "system prompt", "text": _content_text(part.get("content")),
                                "ts": ts, "raw": raw})
            elif ptype == "user":
                content = _content_text(part.get("content"))
                prompt = _parse_prompt(content)
                first, _, rest = content.strip().partition("\n")
                if not prompt["who"] and not prompt["context"] and first.startswith("[") and first.endswith("]"):
                    # Something coolton added, not a person's message: a compaction summary, its
                    # observational memory, or a note about the messages that follow.
                    if rest.strip():
                        entries.append({"kind": "system", "title": one_line(first[1:-1], 90), "text": rest.strip(),
                                        "ts": ts, "raw": raw})
                    else:
                        entries.append({"kind": "note", "text": first[1:-1], "ts": ts, "raw": raw})
                    continue
                for key in ("model", "where", "self_id"):
                    if prompt[key]:
                        info[key] = prompt[key]
                if prompt["who_id"] and prompt["who"]:
                    users.setdefault(prompt["who_id"], prompt["who"])
                turn += 1
                entries.append({"kind": "user", "turn": turn, "who": prompt["who"], "who_id": prompt["who_id"],
                                "text": prompt["text"], "context": prompt["context"], "model": prompt["model"],
                                "ts": ts, "raw": raw})
            elif ptype == "thinking":
                text = _content_text(part.get("content")).strip()
                if text:
                    entries.append({"kind": "thinking", "text": text, "ts": ts, "raw": raw})
            elif ptype == "tool_call":
                name = part.get("tool_name") or "?"
                args = _shrink(_args(part.get("args")))
                call_id = part.get("tool_call_id") or f"#{len(entries)}"
                if name in OUTPUT_TOOLS:
                    text = args.get("response") if isinstance(args, dict) else None
                    entry = {"kind": "output", "text": text if isinstance(text, str) else _content_text(args),
                             "via": name, "emoji": args.get("emoji_name") if isinstance(args, dict) else None,
                             "model": model_name or info["model"], "ts": ts, "raw": raw}
                else:
                    entry = {"kind": "tool", "title": name, "args": args, "result": None, "status": "pending",
                             "model": model_name or info["model"], "ts": ts, "raw": {"call": raw}}
                calls[call_id] = entry
                entries.append(entry)
            elif ptype == "tool_result":
                call_id = part.get("tool_call_id")
                entry = calls.pop(call_id, None) if call_id else None
                if entry is not None and entry["kind"] == "output":
                    continue  # an output tool's "Final result processed."
                content = _shrink(part.get("content"))
                failed = part.get("outcome") in ("failed", "error") or (
                    isinstance(content, str) and content[:40].lower().startswith(("error", "failed")))
                if entry is None:
                    entry = {"kind": "tool", "title": part.get("tool_name") or "?", "args": None, "ts": ts,
                             "raw": {}}
                    entries.append(entry)
                entry.update(result=content, status="error" if failed else "ok", done_ts=ts)
                entry["raw"]["return"] = raw
            elif ptype == "retry-prompt":
                content = _content_text(part.get("content"))
                entry = calls.get(part.get("tool_call_id") or "")
                if entry is not None and entry["kind"] == "tool":
                    entry.update(result=content, status="retry", done_ts=ts)
                    entry["raw"]["retry"] = raw
                else:
                    entries.append({"kind": "retry", "title": part.get("tool_name") or "", "text": content, "ts": ts,
                                    "raw": raw})
            elif ptype == "output":
                text = _content_text(part.get("content"))
                if text.strip():
                    entries.append({"kind": "output", "text": text, "model": model_name or info["model"],
                                    "ts": ts, "raw": raw})
            else:
                entries.append({"kind": "note", "text": f"{ptype or 'part'}: {_content_text(part.get('content'))[:300]}",
                                "ts": ts, "raw": raw})
    return entries, info


def _entries_from_web(events: list) -> tuple[list, dict]:
    entries: list[dict] = []
    steps: dict[str, dict] = {}
    by_seq: dict = {}
    info = {"model": None, "where": "web", "self_id": None, "usage": None, "owner": None}
    turn = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        kind, ts = event.get("type"), _when(event.get("ts"))
        if kind == "user_message":
            turn += 1
            text = event.get("text") or ""
            files = [a.get("name") or a.get("filename") or "file" for a in event.get("attachments") or [] if isinstance(a, dict)]
            if files:
                text += "\n\n" + "\n".join(f"[attached {name}]" for name in files)
            entry = {"kind": "user", "turn": turn, "who": None, "who_id": event.get("user_id"), "text": text,
                     "context": "", "model": None, "ts": ts, "raw": event}
            entries.append(entry)
            info["owner"] = info["owner"] or event.get("user_id")
        elif kind == "step":
            step = event.get("kind")
            if step == "model":
                info["model"] = event.get("text")
            elif step == "reasoning" and (event.get("text") or "").strip():
                entries.append({"kind": "thinking", "text": event["text"].strip(), "ts": ts, "raw": event})
            elif step == "tool":
                step_id = event.get("step_id") or f"#{len(entries)}"
                entry = steps.get(step_id)
                if entry is None:
                    entry = {"kind": "tool", "title": event.get("tool_name") or "?", "args": _args(event.get("args")),
                             "result": None, "status": "pending", "display": event.get("display"),
                             "model": info["model"], "ts": ts, "raw": {"start": event}}
                    steps[step_id] = entry
                    entries.append(entry)
                if event.get("status") in ("complete", "error"):
                    entry.update(result=event.get("result"), done_ts=ts,
                                 status="error" if event.get("status") == "error" else "ok")
                    entry["raw"]["end"] = event
        elif kind == "agent_message":
            text = event.get("text") or ""
            if event.get("filename") or event.get("url"):
                text += f"\n\n[file: {event.get('title') or event.get('filename') or ''} {event.get('url') or ''}]".rstrip()
                if event.get("comment"):
                    text = event["comment"] + "\n\n" + text
            entries.append({"kind": "output", "text": text, "model": info["model"], "variant": event.get("variant"),
                            "ts": ts, "raw": event})
        elif kind == "reaction":
            verb = "reacted" if event.get("op", "add") == "add" else "removed reaction"
            entries.append({"kind": "note", "text": f"{verb} :{event.get('emoji')}:", "ts": ts, "raw": event})
        elif kind == "turn_end" and event.get("state") not in (None, "complete", "completed", "done"):
            reason = f" ({event['reason']})" if event.get("reason") else ""
            entries.append({"kind": "note", "text": f"turn ended: {event.get('state')}{reason}", "ts": ts, "raw": event})
        if "seq" in event:
            by_seq[event["seq"]] = event
    return entries, info


def _blocks_text(blocks) -> str:
    """Readable text from Slack Block Kit (for messages whose `text` is empty)."""
    pieces: list[str] = []

    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get("text"), str):
                pieces.append(value["text"])
            elif isinstance(value.get("text"), dict):
                walk(value["text"])
            for key in ("elements", "fields", "blocks", "rows", "cells"):
                if isinstance(value.get(key), list):
                    for item in value[key]:
                        walk(item)
            if value.get("type") == "rich_text_section":
                pieces.append("\n")
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(blocks)
    return "".join(pieces).strip()


def _slack_messages(raw_messages: list, users: dict) -> list[dict]:
    out = []
    for message in raw_messages or []:
        if not isinstance(message, dict):
            continue
        who_id = message.get("user") or message.get("bot_id")
        profile = message.get("user_profile") or {}
        bot = message.get("bot_profile") or {}
        name = (profile.get("display_name") or profile.get("real_name") or bot.get("name")
                or message.get("username") or users.get(who_id or "") or who_id or "?")
        if who_id and name != who_id:
            users.setdefault(who_id, name)
        text = message.get("text") or ""
        if not text.strip() and message.get("blocks"):
            text = _blocks_text(message["blocks"])
        files = [f.get("title") or f.get("name") or "file" for f in message.get("files") or [] if isinstance(f, dict)]
        reactions = [(r.get("name"), r.get("count", 1)) for r in message.get("reactions") or [] if isinstance(r, dict)]
        out.append({"kind": "slack", "who": name, "who_id": who_id, "bot": bool(message.get("bot_id")),
                    "text": text, "files": files, "reactions": reactions, "ts": _when(message.get("ts")),
                    "edited": bool(message.get("edited")), "subtype": message.get("subtype"),
                    "raw": _shrink({k: v for k, v in message.items() if k != "blocks"})})
    return out


def _title_from(text: str) -> str:
    text = _LEADING_MENTIONS.sub("", text or "")
    text = _MENTION.sub(lambda m: (m[1] if m[1] != "!" else "@") + (m[3] or m[2]), text)
    text = _LINK.sub(lambda m: m[2] or m[1], text)
    line = " ".join(text.split())
    return line[:200]


def load_conversation(path: str, subkey: str | None = None) -> dict:
    """One conversation in the viewer's own shape (also what a remote load sends back)."""
    data = _read(path)
    found = _conversations_in(path, data)
    match = next((c for c in found if c[0] == subkey), None) if subkey is not None else (found[0] if found else None)
    if match is None:
        raise ValueError(f"{path} holds no conversation{' ' + repr(subkey) if subkey else ''}")
    return _normalize(path, *match)


def _web_index(path: str) -> dict:
    try:
        index = _read(os.path.join(os.path.dirname(path), "index.json"))
    except (OSError, ValueError):
        return {}
    name = os.path.basename(path).rsplit(".", 1)[0]
    entry = index.get(name) if isinstance(index, dict) else None
    return entry if isinstance(entry, dict) else {}


def _normalize(path: str, subkey: str | None, doc, fmt: str) -> dict:
    users: dict[str, str] = {}
    slack: list[dict] = []
    channel_id = thread_ts = None
    updated = None
    title = ""
    if fmt == "web":
        entries, info = _entries_from_web(doc)
        meta = _web_index(path)
        title = meta.get("title") or ""
        updated = _when(meta.get("updated_at")) or (entries[-1]["ts"] if entries else None)
        channel_id = "web:" + os.path.basename(path).rsplit(".", 1)[0]
        info["owner"] = meta.get("owner_id") or info.get("owner")
    else:
        if subkey and ":" in subkey:
            channel_id, _, thread_ts = subkey.partition(":")
        channel_id = doc.get("channel_id", channel_id)
        thread_ts = doc.get("thread_ts", thread_ts)
        entries, info = _entries_from_messages(doc.get("messages") or [], users)
        slack = _slack_messages(doc.get("slack_messages"), users)
        updated = _when(doc.get("updated_at") or doc.get("timestamp"))
    if info.get("self_id"):
        users.setdefault(info["self_id"], "coolton")
    first_user = next((e for e in entries if e["kind"] == "user" and e["text"].strip() and (e.get("who") or e.get("who_id"))),
                      None) or next((e for e in entries if e["kind"] == "user" and e["text"].strip()), None)
    if not title:
        title = _title_from(first_user["text"]) if first_user else ""
    if not title and slack:
        title = _title_from(slack[0]["text"])
    if not title:
        title = os.path.basename(path) + (f" › {subkey}" if subkey else "")
    try:
        stat = os.stat(path)
        size, mtime = stat.st_size, stat.st_mtime
    except OSError:
        size, mtime = 0, None
    times = [e["ts"] for e in entries if e.get("ts")] + [m["ts"] for m in slack if m.get("ts")]
    people = []
    for e in entries:
        if e["kind"] == "user":
            name = e.get("who") or users.get(e.get("who_id") or "") or e.get("who_id")
            if name and name not in people and e.get("who_id") != info.get("self_id") and not re.match(r"^B[A-Z0-9]{6,}$", name):
                people.append(name)
    if fmt == "web" and not people and info.get("owner"):
        people.append(info["owner"])
    return {
        "key": path + (f"#{subkey}" if subkey is not None else ""),
        "path": path, "subkey": subkey, "format": fmt, "title": title,
        "channel_id": channel_id, "thread_ts": thread_ts,
        "channel_name": _channel_name(info.get("where")),
        "where": "web" if fmt == "web" else _where(_channel_name(info.get("where")), channel_id, thread_ts),
        "updated": updated or (max(times) if times else None) or mtime,
        "started": min(times) if times else None,
        "turns": sum(1 for e in entries if e["kind"] == "user"),
        "tools": sum(1 for e in entries if e["kind"] == "tool"),
        "people": people, "size": size, "model": info.get("model"),
        "usage": info.get("usage") if info.get("usage") and any(info["usage"].values()) else None,
        "users": users, "self_id": info.get("self_id"),
        "entries": entries, "slack": slack,
    }


SUMMARY_FIELDS = ("key", "path", "subkey", "format", "title", "channel_id", "channel_name", "thread_ts", "where",
                  "updated",
                  "started", "turns", "tools", "people", "size", "model")


def scan(root: str) -> dict:
    """{"root", "summaries", "errors"} for every conversation under `root` (a dir or a file)."""
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.exists(root):
        return {"root": root, "summaries": [], "errors": [f"{root} doesn't exist"], "users": {}}
    summaries, errors, users = [], [], {}
    for path in _files(root):
        if not _looks_like_conversation_file(path):
            continue
        try:
            data = _read(path)
        except PermissionError:
            errors.append(f"{path}: permission denied")
            continue
        except (OSError, ValueError) as e:
            errors.append(f"{path}: {e}")
            continue
        for subkey, doc, fmt in _conversations_in(path, data):
            try:
                conv = _normalize(path, subkey, doc, fmt)
            except Exception as e:  # one odd file shouldn't hide the rest
                errors.append(f"{path}{'#' + subkey if subkey else ''}: {type(e).__name__}: {e}")
                continue
            summaries.append({k: conv[k] for k in SUMMARY_FIELDS})
            for user_id, name in conv["users"].items():
                users.setdefault(user_id, name)
    # Older conversations don't say their channel's name; another one in the same channel might.
    names = {s["channel_id"]: s["channel_name"] for s in summaries if s.get("channel_name")}
    for channel_id, name in names.items():
        users.setdefault(channel_id, name.split(" ")[0])
    for s in summaries:
        if not s.get("channel_name") and s.get("channel_id") in names:
            s["channel_name"] = names[s["channel_id"]]
            s["where"] = _where(s["channel_name"], s["channel_id"], s["thread_ts"])
    return {"root": root, "summaries": summaries, "errors": errors, "users": users}


def searchable_text(conv: dict) -> str:
    """Everything a person or coolton said or did in a conversation (not the system prompts)."""
    parts = []
    for e in conv["entries"]:
        if e["kind"] == "system" and e.get("title") in ("system prompt", "instructions"):
            continue
        for key in ("who", "title", "text", "context"):
            if isinstance(e.get(key), str):
                parts.append(e[key])
        for key in ("args", "result"):
            value = e.get(key)
            if value is not None:
                parts.append(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    for m in conv["slack"]:
        parts.append(f"{m['who']} {m['text']}")
    return "\n".join(parts)


def search(root: str, query: str) -> dict:
    """{key: number of matches} for every conversation under `root` that contains `query`."""
    needle = query.lower()
    hits = {}
    for path in _files(os.path.abspath(os.path.expanduser(root))):
        if not _looks_like_conversation_file(path):
            continue
        try:
            data = _read(path)
        except (OSError, ValueError):
            continue
        for subkey, doc, fmt in _conversations_in(path, data):
            try:
                conv = _normalize(path, subkey, doc, fmt)
            except Exception:
                continue
            count = searchable_text(conv).lower().count(needle)
            if count:
                hits[conv["key"]] = count
    return hits


def configured_root() -> str | None:
    """The directory ~/.coolton_convs names, if that file exists."""
    config = Path(CONFIG_FILE).expanduser()
    if config.is_file():
        value = config.read_text(encoding="utf-8").strip().splitlines()
        if value and value[0].strip():
            return os.path.expanduser(value[0].strip())
    return None


REMOTE_MARK = "@@coolton-convs@@"


def remote_main(argv: list[str]) -> int:
    """What the remote side of `ssh HOST` runs: one command, one JSON line back."""
    command, args = argv[0], argv[1:]
    try:
        if command == "--remote-scan":
            root = args[0] if args and args[0] else configured_root()
            if not root:
                result = {"error": f"no path given and there's no {CONFIG_FILE} on this server"}
            else:
                result = scan(root)
        elif command == "--remote-load":
            result = load_conversation(args[0], args[1] if len(args) > 1 and args[1] != "" else None)
        elif command == "--remote-search":
            result = search(args[0], args[1])
        else:
            result = {"error": f"unknown command {command}"}
    except PermissionError as e:
        result = {"error": f"permission denied: {e.filename}"}
    except Exception as e:
        result = {"error": f"{type(e).__name__}: {e}"}
    sys.stdout.write(REMOTE_MARK + json.dumps(result, ensure_ascii=False) + "\n")
    return 0


# ─────────────────────────────── where conversations come from ───────────────────────────────


class SourceError(Exception):
    pass


class LocalSource:
    def __init__(self, root: str):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.label = _home_relative(self.root)

    def scan(self) -> dict:
        return scan(self.root)

    def load(self, path: str, subkey: str | None) -> dict:
        try:
            return load_conversation(path, subkey)
        except PermissionError as e:
            raise SourceError(f"permission denied: {e.filename}") from e
        except (OSError, ValueError) as e:
            raise SourceError(str(e)) from e

    def search(self, query: str) -> dict:
        return search(self.root, query)

    def where_for_export(self) -> str:
        return os.getcwd()


class SshSource:
    """Runs this same file on HOST with python3, so only the conversation you open travels."""

    def __init__(self, host: str, root: str):
        self.host = host
        self.root = root  # "" means: the path in HOST's ~/.coolton_convs
        self.label = f"{host}:{root or CONFIG_FILE}"
        control_dir = Path("~/.ssh").expanduser()
        control_dir = control_dir if control_dir.is_dir() else Path("/tmp")
        self._ssh = ["ssh", "-C", "-o", "ControlMaster=auto", "-o", f"ControlPath={control_dir}/coolton-convs-%C",
                     "-o", "ControlPersist=900"]
        self._interactive = True  # the first call may need a password or a host key answer

    def _source(self) -> str:
        if not SCRIPT_PATH:
            raise SourceError("can't find this script's own file to send to the server")
        return Path(SCRIPT_PATH).read_text(encoding="utf-8")

    def _call(self, *args: str, timeout: float = 180):
        ssh = self._ssh + ([] if self._interactive else ["-o", "BatchMode=yes"])
        remote = "python3 - " + " ".join(shlex.quote(a) for a in args)
        try:
            done = subprocess.run([*ssh, self.host, remote], input=self._source(), capture_output=True, text=True,
                                  timeout=timeout)
        except FileNotFoundError as e:
            raise SourceError("ssh isn't installed here") from e
        except subprocess.TimeoutExpired as e:
            raise SourceError(f"{self.host} didn't answer within {int(timeout)}s") from e
        self._interactive = False
        line = next((l for l in reversed(done.stdout.splitlines()) if l.startswith(REMOTE_MARK)), None)
        if line is None:
            detail = (done.stderr or done.stdout).strip().splitlines()
            raise SourceError(f"ssh {self.host} failed: {detail[-1] if detail else f'exit code {done.returncode}'}")
        result = json.loads(line[len(REMOTE_MARK):])
        if isinstance(result, dict) and result.get("error") and len(result) == 1:
            raise SourceError(f"{self.host}: {result['error']}")
        return result

    def scan(self) -> dict:
        result = self._call("--remote-scan", self.root)
        if not self.root:
            self.root = result["root"]
            self.label = f"{self.host}:{_home_relative(self.root, remote=True)}"
        return result

    def load(self, path: str, subkey: str | None) -> dict:
        return self._call("--remote-load", path, "" if subkey is None else subkey)

    def search(self, query: str) -> dict:
        return self._call("--remote-search", self.root, query, timeout=600)

    def where_for_export(self) -> str:
        return os.getcwd()


def _home_relative(path: str, remote: bool = False) -> str:
    home = os.path.expanduser("~")
    if not remote and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home):]
    match = re.match(r"^/(?:home|Users)/[^/]+(/.*)?$", path) if remote else None
    return "~" + (match[1] or "") if match else path


# ─────────────────────────────── formatting helpers ───────────────────────────────


def fmt_when(ts: float | None, seconds: bool = False) -> str:
    if not ts:
        return "—"
    moment = datetime.fromtimestamp(ts)
    now = datetime.now()
    clock = moment.strftime("%H:%M:%S" if seconds else "%H:%M")
    if moment.date() == now.date():
        return f"today {clock}"
    if moment.year == now.year:
        return moment.strftime("%b %d ") + clock
    return moment.strftime("%Y-%m-%d ") + clock


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def slack_text(text: str, users: dict) -> str:
    """Slack's <@U…>, <#C…|name> and <url|label> markup as plain readable text."""
    def mention(m):
        sigil, target, label = m[1], m[2], m[3]
        if sigil == "@":
            return "@" + (label or users.get(target) or target)
        if sigil == "#":
            return "#" + (label or users.get(target, target)).lstrip("#")
        return "@" + (label or target.split("^")[0])
    text = _MENTION.sub(mention, text or "")
    text = _LINK.sub(lambda m: f"{m[2]} ({m[1]})" if m[2] and m[2] != m[1] else m[1], text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def value_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def one_line(text: str, limit: int = 160) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def args_summary(args) -> str:
    if isinstance(args, dict):
        return ", ".join(f"{k}={one_line(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False), 48)}"
                         for k, v in args.items())
    return one_line(value_text(args), 120)


def entry_plain(entry: dict, users: dict) -> str:
    """The whole entry as plain text (copying, the full view, exporting)."""
    kind = entry["kind"]
    if kind == "tool":
        out = f"{entry['title']}\n\nargs:\n{value_text(entry.get('args'))}"
        if entry.get("result") is not None:
            out += f"\n\nresult ({entry.get('status')}):\n{value_text(entry['result'])}"
        return out
    if kind == "user":
        return slack_text(entry.get("text", ""), users)
    if kind == "slack":
        extra = "".join(f"\n[file: {f}]" for f in entry.get("files") or [])
        return slack_text(entry.get("text", ""), users) + extra
    return entry.get("text") or ""


def export_markdown(conv: dict) -> str:
    users = conv.get("users") or {}
    lines = [f"# {conv['title']}", "",
             f"- where: {conv['where']}", f"- file: {conv['path']}{' #' + conv['subkey'] if conv.get('subkey') else ''}",
             f"- updated: {fmt_when(conv.get('updated'), True)}", ""]
    for e in conv["entries"]:
        kind = e["kind"]
        if kind == "user":
            who = e.get("who") or users.get(e.get("who_id") or "") or e.get("who_id") or "user"
            lines += [f"## {e.get('turn')}. {who} · {fmt_when(e.get('ts'), True)}", "", slack_text(e["text"], users), ""]
        elif kind == "output":
            lines += [f"**coolton**{' (' + e['model'] + ')' if e.get('model') else ''}:", "", e["text"], ""]
        elif kind == "thinking":
            lines += ["<details><summary>thinking</summary>", "", e["text"], "", "</details>", ""]
        elif kind == "tool":
            lines += [f"<details><summary>tool: {e['title']} ({e.get('status')})</summary>", "",
                      "```json", value_text(e.get("args")), "```", "", "```", value_text(e.get("result"))[:20000], "```",
                      "", "</details>", ""]
        elif kind == "system" and e.get("title") in ("system prompt", "instructions"):
            lines += [f"_({e['title']} left out, {len(e.get('text') or ''):,} characters)_", ""]
        elif kind == "system":
            lines += [f"<details><summary>{e.get('title', 'system')}</summary>", "", e["text"], "", "</details>", ""]
        else:
            lines += [f"_{e.get('text', '')}_", ""]
    return "\n".join(lines)


# ─────────────────────────────── the viewer (Textual) ───────────────────────────────


def ensure_textual() -> None:
    """Re-run under a private virtualenv with Textual in it, if this Python lacks it."""
    try:
        import textual
        if int(textual.__version__.split(".")[0]) >= 8:
            return
    except (ImportError, ValueError):
        pass
    if os.environ.get("COOLTON_CONVS_BOOTSTRAPPED"):
        sys.exit("coolton_convs: Textual still isn't importable after installing it. "
                 f"Try: {sys.executable} -m pip install '{TEXTUAL_REQUIREMENT}'")
    python = VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    ready = python.exists() and subprocess.run(
        [str(python), "-c", "import textual,sys; sys.exit(int(textual.__version__.split('.')[0]) < 8)"],
        capture_output=True).returncode == 0
    if not ready:
        print(f"coolton_convs: installing Textual into {_home_relative(str(VENV_DIR))} (one time)…", file=sys.stderr)
        try:
            if not python.exists():
                subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)
            subprocess.run([str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check",
                            TEXTUAL_REQUIREMENT], check=True)
        except (subprocess.CalledProcessError, OSError) as e:
            sys.exit(f"coolton_convs: couldn't install Textual ({e}). Install it yourself with "
                     f"`pip install '{TEXTUAL_REQUIREMENT}'` and run this again.")
    os.environ["COOLTON_CONVS_BOOTSTRAPPED"] = "1"
    os.execv(str(python), [str(python), os.path.abspath(SCRIPT_PATH), *sys.argv[1:]])


def build_app():
    """The Textual app class (Textual is only imported once we know we're showing a UI)."""
    import asyncio

    from rich.console import Group
    from rich.markdown import Markdown
    from rich.text import Text
    from textual import on, work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical, VerticalScroll
    from textual.screen import ModalScreen, Screen
    from textual.widgets import (
        ContentSwitcher,
        Footer,
        Input,
        LoadingIndicator,
        OptionList,
        Static,
        TextArea,
    )
    from textual.widgets.option_list import Option

    BODY_LIMIT = 6000  # characters shown inline; `o` shows everything
    SORTS = [("newest", lambda s: -(s.get("updated") or 0)), ("oldest", lambda s: s.get("updated") or 0),
             ("most turns", lambda s: -(s.get("turns") or 0)), ("most tool calls", lambda s: -(s.get("tools") or 0)),
             ("biggest", lambda s: -(s.get("size") or 0)), ("title", lambda s: (s.get("title") or "").lower())]
    COLLAPSIBLE = {"system", "thinking", "tool"}
    HIDEABLE = {"tool": "tools", "thinking": "thinking", "system": "system prompts"}

    def highlight(text: Text, query: str | None) -> Text:
        if query:
            text.highlight_words([query], style="black on yellow", case_sensitive=False)
        return text

    def clipped(text: str) -> tuple[str, int]:
        return (text, 0) if len(text) <= BODY_LIMIT else (text[:BODY_LIMIT], len(text) - BODY_LIMIT)

    def user_body(text: str, users: dict, query: str | None) -> Text:
        body = Text()
        shown, more = clipped(slack_text(text, users))
        for i, line in enumerate(shown.split("\n")):
            if i:
                body.append("\n")
            if _SENDER.match(line) or _OTHER_BOT.match(line):
                body.append(line, style="bold")
            else:
                body.append(line)
        if more:
            body.append(f"\n… {more:,} more characters (o shows everything)", style="dim italic")
        return highlight(body, query)

    def matches(entry: dict, users: dict, query: str | None) -> bool:
        if not query:
            return False
        needle = query.lower()
        text = entry_plain(entry, users) + "\n" + (entry.get("context") or "")
        return needle in text.lower() or needle in (entry.get("who") or "").lower()

    class EntryView(Static, can_focus=True):
        """One step of a conversation: a message, a thought, a tool call and its result."""

        def __init__(self, entry: dict, users: dict, coolton_name: str = "coolton"):
            super().__init__(classes=f"entry {entry['kind']}")
            self.entry = entry
            self.users = users
            self.coolton_name = coolton_name
            self.expanded = entry["kind"] not in COLLAPSIBLE
            self.show_context = False  # a person's message: the context coolton added to it
            self.query: str | None = None
            if entry["kind"] == "tool" and entry.get("status") in ("error", "retry"):
                self.add_class("failed")

        def on_mount(self) -> None:
            self.redraw()

        def set_query(self, query: str | None) -> bool:
            hit = matches(self.entry, self.users, query)
            changed = (self.query is not None) != hit or (hit and self.query != query)
            self.query = query if hit else None
            if changed:
                self.redraw()
            return hit

        def toggle(self, expanded: bool | None = None) -> None:
            if self.entry["kind"] == "user" and self.entry.get("context"):
                if expanded is None:
                    self.show_context = not self.show_context
                    self.redraw()
                return
            if self.entry["kind"] not in COLLAPSIBLE:
                return
            new = (not self.expanded) if expanded is None else expanded
            if new != self.expanded:
                self.expanded = new
                self.redraw()

        def on_click(self) -> None:
            self.focus()
            self.toggle()

        def redraw(self) -> None:
            self.set_class(self.entry["kind"] in COLLAPSIBLE and not self.expanded, "oneline")
            self.update(self.render_entry())

        def render_entry(self):
            e, kind, q = self.entry, self.entry["kind"], self.query
            when = fmt_when(e.get("ts"), seconds=True) if e.get("ts") else ""
            arrow = ("▾ " if self.expanded else "▸ ") if kind in COLLAPSIBLE else ""
            if kind == "user":
                who = e.get("who") or self.users.get(e.get("who_id") or "") or e.get("who_id") or "someone"
                head = Text.assemble((who, "bold"), (f"  #{e.get('turn')}", "dim"), (f" · {when}", "dim"))
                if e.get("who_id") and e.get("who") is not None:
                    head.append(f" · {e['who_id']}", style="dim")
                if e.get("model"):
                    head.append(f" · {e['model']}", style="dim")
                if self.show_context and e.get("context"):
                    context = Text(e["context"], style="dim")
                    return Group(head, highlight(context, q), Text(""), user_body(e["text"], self.users, q))
                return Group(head, user_body(e["text"] or "(empty)", self.users, q))
            if kind == "output":
                head = Text.assemble((self.coolton_name, "bold green"), (f" · {when}" if when else "", "dim"))
                if e.get("model"):
                    head.append(f" · {e['model']}", style="dim")
                if e.get("via"):
                    head.append(f" · via {e['via']}" + (f" :{e['emoji']}:" if e.get("emoji") else ""), style="dim")
                if e.get("variant") and e["variant"] != "final":
                    head.append(f" · {e['variant']}", style="dim")
                text, more = clipped(e.get("text") or "")
                if q:
                    body = highlight(Text(text), q)
                else:
                    try:
                        body = Markdown(text, code_theme="monokai", hyperlinks=True)
                    except Exception:
                        body = Text(text)
                parts = [head, body]
                if more:
                    parts.append(Text(f"… {more:,} more characters (o shows everything)", style="dim italic"))
                return Group(*parts)
            if kind == "thinking":
                if not self.expanded:
                    return highlight(Text.assemble((arrow + "thinking  ", "italic"), (one_line(e["text"], 400), "dim italic")), q)
                text, more = clipped(e["text"])
                body = highlight(Text(text, style="italic"), q)
                if more:
                    body.append(f"\n… {more:,} more characters (o shows everything)", style="dim")
                return Group(Text(arrow + "thinking", style="italic dim"), body)
            if kind == "tool":
                status = e.get("status")
                mark = {"ok": ("✓", "green"), "error": ("✗", "red"), "retry": ("↻", "yellow")}.get(status, ("…", "yellow"))
                head = Text.assemble((arrow, ""), (mark[0] + " ", mark[1]), (e["title"], "bold cyan"))
                if not self.expanded:
                    head.append("  " + args_summary(e.get("args")), style="dim")
                    if e.get("result") is not None:
                        head.append("  → " + one_line(value_text(e["result"]), 200), style="")
                    return highlight(head, q)
                if when:
                    head.append(f"  {when}", style="dim")
                if e.get("done_ts") and e.get("ts") and e["done_ts"] >= e["ts"]:
                    head.append(f" · {e['done_ts'] - e['ts']:.1f}s", style="dim")
                if e.get("model"):
                    head.append(f" · {e['model']}", style="dim")
                parts = [head, Text("args", style="bold dim")]
                args = e.get("args")
                if isinstance(args, dict) and args:
                    for key, value in args.items():
                        if isinstance(value, str) and ("\n" in value or len(value) > 100):
                            shown, more = clipped(value)
                            parts.append(highlight(Text.assemble((f"{key}:", "cyan"), "\n", shown), q))
                            if more:
                                parts.append(Text(f"… {more:,} more characters", style="dim italic"))
                        else:
                            shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                            parts.append(highlight(Text.assemble((f"{key}: ", "cyan"), shown), q))
                else:
                    parts.append(highlight(Text(value_text(args) or "(none)"), q))
                label = {"ok": "result", "error": "result (failed)", "retry": "retried: the model was told",
                         "pending": "no result recorded"}.get(status, "result")
                parts.append(Text(label, style="bold dim" if status == "ok" else "bold red"))
                if e.get("result") is not None:
                    shown, more = clipped(value_text(e["result"]))
                    parts.append(highlight(Text(shown), q))
                    if more:
                        parts.append(Text(f"… {more:,} more characters (o shows everything)", style="dim italic"))
                return Group(*parts)
            if kind == "system":
                size = len(e.get("text") or "")
                head = Text.assemble((arrow + e.get("title", "system prompt"), "magenta"), (f"  {size:,} characters", "dim"))
                if not self.expanded:
                    return head
                shown, more = clipped(e["text"])
                body = highlight(Text(shown, style="dim"), q)
                if more:
                    body.append(f"\n… {more:,} more characters (o shows everything)", style="dim italic")
                return Group(head, body)
            if kind == "retry":
                return highlight(Text.assemble(("↻ retry ", "yellow bold"), (e.get("title") or "", "yellow"), "\n",
                                               e.get("text") or ""), q)
            if kind == "slack":
                who = e["who"] if e["who"] != e.get("who_id") else self.users.get(e.get("who_id") or "", e["who"])
                head = Text.assemble((who, "bold green" if e.get("bot") else "bold"), (f" · {when}", "dim"))
                if e.get("edited"):
                    head.append(" · edited", style="dim")
                if e.get("subtype"):
                    head.append(f" · {e['subtype']}", style="dim")
                body = user_body(e.get("text") or "", self.users, q)
                parts = [head, body]
                for name in e.get("files") or []:
                    parts.append(Text(f"📎 {name}", style="cyan"))
                if e.get("reactions"):
                    parts.append(Text("  ".join(f":{n}: {c}" for n, c in e["reactions"]), style="dim"))
                return Group(*parts)
            return highlight(Text.assemble((f"{when}  " if when else "", "dim"), (e.get("text") or "", "dim italic")), q)

    class FullView(ModalScreen):
        """Everything in one entry, selectable, with its raw JSON a key away."""

        BINDINGS = [Binding("escape,q,o", "dismiss", "Close"), Binding("r", "raw", "Raw JSON / text"),
                    Binding("y", "copy", "Copy all")]

        def __init__(self, title: str, text: str, raw):
            super().__init__()
            self.title_text, self.text, self.raw, self.showing_raw = title, text, raw, False

        def compose(self) -> ComposeResult:
            with Vertical(id="full"):
                yield Static(id="full-head")
                yield TextArea(self.text, read_only=True, soft_wrap=True, show_line_numbers=False, id="full-text")
                yield Footer()

        def on_mount(self) -> None:
            self._head()
            self.query_one(TextArea).focus()

        def _head(self) -> None:
            mode = "raw JSON" if self.showing_raw else "text"
            self.query_one("#full-head", Static).update(Text.assemble((self.title_text, "bold"), (f"  · {mode}", "dim")))

        def action_raw(self) -> None:
            self.showing_raw = not self.showing_raw
            area = self.query_one(TextArea)
            area.load_text(json.dumps(self.raw, ensure_ascii=False, indent=2) if self.showing_raw else self.text)
            self._head()

        def action_copy(self) -> None:
            area = self.query_one(TextArea)
            self.app.copy_to_clipboard(area.selected_text or area.text)
            self.app.notify("Copied" + (" the selection" if area.selected_text else ""))

    class Ask(ModalScreen):
        """A one-line question (searching every conversation's text)."""

        BINDINGS = [Binding("escape", "dismiss", "Cancel")]

        def __init__(self, prompt: str, value: str = ""):
            super().__init__()
            self.prompt, self.value = prompt, value

        def compose(self) -> ComposeResult:
            with Vertical(id="ask"):
                yield Static(self.prompt, id="ask-prompt")
                yield Input(self.value, id="ask-input")

        @on(Input.Submitted)
        def submitted(self, event: Input.Submitted) -> None:
            self.dismiss(event.value.strip())

    class Help(ModalScreen):
        BINDINGS = [Binding("escape,q,question_mark", "dismiss", "Close")]
        TEXT = """[b]Conversation list[/b]
  enter         open the highlighted conversation
  ↑ ↓  j k      move
  /             filter by title, channel, person or id
  f             search the text of every conversation
  s             change the sort order
  esc           clear the filter or the text search
  R             reload
  q             quit

[b]A conversation[/b]
  j k  tab      next / previous step
  ] [           next / previous message from a person
  ↑ ↓ pgup pgdn scroll
  g G           top / bottom
  enter, click  open or close a step (on a message: the context coolton added)
  e c           open all / close all
  t h s         show or hide tool calls / thinking / system prompts
  / n N         find in this conversation, next / previous match
  o             the whole step, selectable (r inside: its raw JSON)
  y             copy the step
  x             export the conversation as Markdown
  1 2           coolton's trace / the Slack thread as people saw it
  R             reload
  q esc         back to the list
"""

        def compose(self) -> ComposeResult:
            with VerticalScroll(id="help"):
                yield Static(self.TEXT)

    class ConversationScreen(Screen):
        BINDINGS = [
            Binding("escape", "back", "Back"), Binding("q", "back", "Back", show=False),
            Binding("j,tab", "move(1)", "Next", show=False), Binding("k,shift+tab", "move(-1)", "Prev", show=False),
            Binding("right_square_bracket", "turn(1)", "Next msg"), Binding("left_square_bracket", "turn(-1)", "Prev msg"),
            Binding("enter", "toggle", "Open/close"), Binding("e", "expand_all(True)", "Open all", show=False),
            Binding("c", "expand_all(False)", "Close all", show=False),
            Binding("t", "hide('tool')", "Tools"), Binding("h", "hide('thinking')", "Thinking"),
            Binding("s", "hide('system')", "System", show=False),
            Binding("slash", "find", "Find"), Binding("n", "next_match(1)", "Next match", show=False),
            Binding("N", "next_match(-1)", "Prev match", show=False),
            Binding("o", "full", "Full"), Binding("y", "copy", "Copy", show=False),
            Binding("x", "export", "Export"), Binding("1", "view('trace')", "Trace", show=False),
            Binding("2", "view('slack')", "Slack", show=False), Binding("g,home", "edge(0)", "Top", show=False),
            Binding("G,end", "edge(1)", "Bottom", show=False), Binding("R", "reload", "Reload", show=False),
            Binding("question_mark", "app.help", "Keys"),
        ]

        def __init__(self, source, summary: dict, query: str | None = None):
            super().__init__()
            self.source, self.summary, self.conv = source, summary, None
            self.query_text = query
            self.hidden: set[str] = set()
            self.matches: list = []
            self.before_find = None

        def compose(self) -> ComposeResult:
            yield Static(id="conv-head")
            with ContentSwitcher(initial="loading", id="views"):
                yield LoadingIndicator(id="loading")
                yield VerticalScroll(id="trace")
                yield VerticalScroll(id="slack")
            yield Input(placeholder="find in this conversation (enter to search, esc to close)", id="find",
                        compact=True)
            yield Footer()

        def on_mount(self) -> None:
            self.query_one("#find").display = False
            self._head()
            self.load()

        @property
        def view(self) -> VerticalScroll:
            current = self.query_one("#views", ContentSwitcher).current
            return self.query_one(f"#{current if current in ('trace', 'slack') else 'trace'}", VerticalScroll)

        def entries(self, visible_only: bool = True) -> list:
            return [w for w in self.view.query(EntryView) if not visible_only or w.entry["kind"] not in self.hidden]

        @work(exclusive=True)
        async def load(self, keep_position: bool = False) -> None:
            old_index = self._focused_index() if keep_position else None
            try:
                conv = await asyncio.to_thread(self.source.load, self.summary["path"], self.summary.get("subkey"))
            except Exception as e:
                trace = self.query_one("#trace", VerticalScroll)
                await trace.remove_children()
                await trace.mount(Static(Text(f"Couldn't open this conversation: {e}", style="bold red")))
                self.query_one("#views", ContentSwitcher).current = "trace"
                return
            known = getattr(self.app, "users", {})
            conv["users"] = {**known, **{k: v for k, v in (conv.get("users") or {}).items() if v != k}}
            if not conv.get("channel_name") and self.summary.get("channel_name"):
                conv["channel_name"], conv["where"] = self.summary["channel_name"], self.summary["where"]
            self.conv = conv
            users = conv.get("users") or {}
            name = users.get(conv.get("self_id") or "", "coolton")
            trace = self.query_one("#trace", VerticalScroll)
            slack = self.query_one("#slack", VerticalScroll)
            await trace.remove_children()
            await slack.remove_children()
            views = [EntryView(e, users, name) for e in conv["entries"]]
            await trace.mount_all(views or [Static(Text("This conversation has no messages.", style="dim"))])
            await slack.mount_all([EntryView(m, users, name) for m in conv["slack"]]
                                  or [Static(Text("No Slack thread is saved with this conversation.", style="dim"))])
            self._apply_hidden()
            self.query_one("#views", ContentSwitcher).current = "trace"
            self._head()
            if self.query_text:
                self._search(self.query_text, jump=True)
            elif old_index is not None and views:
                self._focus(views[min(old_index, len(views) - 1)], top=True)
            elif views:
                views[0].focus(scroll_visible=False)

        def _focused_index(self) -> int | None:
            items = self.entries(visible_only=False)
            return items.index(self.focused) if self.focused in items else None

        def _head(self) -> None:
            s = self.conv or self.summary
            head = Text(one_line(s.get("title") or "", 300), style="bold")
            meta = [s.get("where") or "", plural(s.get("turns", 0), "message"), plural(s.get("tools", 0), "tool call")]
            if s.get("started") and s.get("updated"):
                meta.append(f"{fmt_when(s['started'])} → {fmt_when(s['updated'])}")
            usage = (self.conv or {}).get("usage")
            if usage:
                meta.append(f"{fmt_tokens(usage['input'])} in / {fmt_tokens(usage['output'])} out tokens")
            meta.append(s.get("format", ""))
            head.append("\n" + "  ·  ".join(m for m in meta if m), style="dim")
            location = s["path"] + (f" #{s['subkey']}" if s.get("subkey") else "")
            head.append("\n" + _home_relative(location, remote=isinstance(self.source, SshSource)), style="dim")
            if self.conv:
                current = self.query_one("#views", ContentSwitcher).current
                tabs = Text("\n")
                views = [("trace", "1 trace", len(self.conv["entries"]))]
                if self.conv["slack"]:
                    views.append(("slack", "2 slack thread", len(self.conv["slack"])))
                for key, label, count in views:
                    tabs.append(f" {label} ({count}) ", style="reverse bold" if current == key else "dim")
                    tabs.append("  ")
                if self.hidden:
                    tabs.append("hiding " + ", ".join(HIDEABLE[k] for k in sorted(self.hidden)), style="yellow")
                if self.query_text:
                    tabs.append(f"  find “{self.query_text}”: {len(self.matches)} steps", style="black on yellow")
                head.append_text(tabs)
            self.query_one("#conv-head", Static).update(head)

        def _focus(self, widget, top: bool = False) -> None:
            widget.focus(scroll_visible=False)
            self.view.scroll_to_widget(widget, animate=False, top=top)

        def _step(self, delta: int, predicate=lambda w: True) -> None:
            items = [w for w in self.entries() if predicate(w)]
            if not items:
                return
            everything = self.entries()
            if self.focused in everything:
                start = everything.index(self.focused)
                ahead = [w for w in everything[start + 1:] if predicate(w)] if delta > 0 else \
                        [w for w in reversed(everything[:start]) if predicate(w)]
                target = ahead[0] if ahead else None
            else:
                target = items[0] if delta > 0 else items[-1]
            if target is not None:
                self._focus(target, top=delta > 0 and target.outer_size.height > self.view.size.height // 2)

        def action_move(self, delta: int) -> None:
            self._step(delta)

        def action_turn(self, delta: int) -> None:
            self._step(delta, lambda w: w.entry["kind"] in ("user", "slack") and not w.entry.get("bot"))

        def action_toggle(self) -> None:
            if isinstance(self.focused, EntryView):
                self.focused.toggle()
                self.view.scroll_to_widget(self.focused, animate=False)

        def action_expand_all(self, expanded: bool) -> None:
            for w in self.entries():
                # Opening everything leaves system prompts shut: they're tens of thousands of characters.
                if w.entry["kind"] in COLLAPSIBLE and (w.entry["kind"] != "system" or not expanded):
                    w.toggle(expanded)
            if isinstance(self.focused, EntryView):
                self.call_after_refresh(self.view.scroll_to_widget, self.focused, animate=False, top=True)

        def _apply_hidden(self) -> None:
            trace = self.query_one("#trace", VerticalScroll)
            for kind in HIDEABLE:
                trace.set_class(kind in self.hidden, f"hide-{kind}")

        def action_hide(self, kind: str) -> None:
            self.hidden ^= {kind}
            self._apply_hidden()
            self._head()
            if isinstance(self.focused, EntryView) and self.focused.entry["kind"] in self.hidden:
                self._step(1)
            self.app.notify(f"{'Hiding' if kind in self.hidden else 'Showing'} {HIDEABLE[kind]}", timeout=2)

        def action_find(self) -> None:
            self.before_find = self.focused if isinstance(self.focused, EntryView) else None
            find = self.query_one("#find", Input)
            find.display = True
            find.value = self.query_text or ""
            find.focus()

        @on(Input.Submitted, "#find")
        def find_submitted(self, event: Input.Submitted) -> None:
            event.input.display = False
            self._search(event.value.strip() or None, jump=True)

        def _search(self, query: str | None, jump: bool) -> None:
            self.query_text = query
            self.matches = [w for w in self.entries(visible_only=False) if w.set_query(query)]
            self._head()
            if query and not self.matches:
                self.app.notify(f"Nothing in this view matches “{query}”", severity="warning")
            elif jump and self.matches:
                self.action_next_match(1, wrap_from_top=True)

        def action_next_match(self, delta: int, wrap_from_top: bool = False) -> None:
            if not self.matches:
                if not self.query_text:
                    self.app.notify("Press / to find something first", timeout=2)
                return
            everything = self.entries(visible_only=False)
            here = -1 if wrap_from_top or self.focused not in everything else everything.index(self.focused)
            ordered = sorted(self.matches, key=everything.index)
            after = [w for w in ordered if everything.index(w) > here] if delta > 0 else \
                    [w for w in reversed(ordered) if everything.index(w) < here]
            target = (after or (ordered if delta > 0 else list(reversed(ordered))))[0]
            if target.entry["kind"] in self.hidden:
                self.hidden.discard(target.entry["kind"])
                self._apply_hidden()
                self._head()
            target.toggle(True)
            position = ordered.index(target) + 1
            self.call_after_refresh(self._focus, target, True)
            self.app.notify(f"match {position} of {len(ordered)}", timeout=1.5)

        def action_back(self) -> None:
            find = self.query_one("#find", Input)
            if find.display:
                find.display = False
                if isinstance(self.focused, Input):
                    if self.before_find is not None and self.before_find.is_mounted:
                        self.before_find.focus(scroll_visible=False)
                    else:
                        self._step(1)
                return
            if self.query_text:
                self._search(None, jump=False)
                return
            self.app.pop_screen()

        def action_full(self) -> None:
            if isinstance(self.focused, EntryView):
                e = self.focused.entry
                title = {"tool": f"tool call: {e.get('title')}", "user": "message", "output": "coolton's reply",
                         "slack": f"Slack message from {e.get('who')}"}.get(e["kind"], e["kind"])
                text = entry_plain(e, self.focused.users)
                if e["kind"] == "user" and e.get("context"):
                    text = e["context"] + "\n\n" + text
                self.app.push_screen(FullView(title, text, e.get("raw")))

        def action_copy(self) -> None:
            if isinstance(self.focused, EntryView):
                text = entry_plain(self.focused.entry, self.focused.users)
                self.app.copy_to_clipboard(text)
                self.app.notify(f"Copied {len(text):,} characters")

        def action_export(self) -> None:
            if not self.conv:
                return
            slug = re.sub(r"[^a-z0-9]+", "-", self.conv["title"].lower()).strip("-")[:50] or "conversation"
            path = Path(self.source.where_for_export()) / f"coolton-{slug}.md"
            path.write_text(export_markdown(self.conv), encoding="utf-8")
            self.app.notify(f"Exported to {_home_relative(str(path))}")

        def action_view(self, which: str) -> None:
            if not self.conv:
                return
            if which == "slack" and not self.conv["slack"]:
                self.app.notify("No Slack thread is saved with this conversation", timeout=2)
                return
            self.query_one("#views", ContentSwitcher).current = which
            items = self.entries()
            if items:
                items[0].focus(scroll_visible=False)
            if self.query_text:
                self._search(self.query_text, jump=False)
            self._head()

        def action_edge(self, end: int) -> None:
            items = self.entries()
            if items:
                self._focus(items[-1 if end else 0], top=not end)
                if end:
                    self.view.scroll_end(animate=False)

        def action_reload(self) -> None:
            self.load(keep_position=True)
            self.app.notify("Reloaded", timeout=1.5)

    class ListScreen(Screen):
        BINDINGS = [
            Binding("slash", "filter", "Filter"), Binding("f", "search", "Search text"),
            Binding("s", "sort", "Sort"), Binding("R", "reload", "Reload"),
            Binding("escape", "clear", "Clear", show=False), Binding("j", "cursor(1)", "Down", show=False),
            Binding("k", "cursor(-1)", "Up", show=False), Binding("question_mark", "app.help", "Keys"),
            Binding("q", "app.quit", "Quit"),
        ]

        def __init__(self, source, scanned: dict):
            super().__init__()
            self.source = source
            self.summaries = scanned["summaries"]
            self.errors = scanned["errors"]
            self.sort_index = 0
            self.hits: dict | None = None
            self.hit_query = ""

        def compose(self) -> ComposeResult:
            yield Static(id="list-head")
            yield Input(placeholder="/ to filter by title, channel, person or id", id="filter", compact=True)
            yield OptionList(id="convs")
            yield Footer()

        def on_mount(self) -> None:
            self.call_after_refresh(self.refill)
            self.query_one(OptionList).focus()

        def on_resize(self) -> None:
            self.call_after_refresh(self.refill)
            if self.errors:
                denied = sum("permission denied" in e for e in self.errors)
                detail = f"{denied} couldn't be read (permission denied)" if denied == len(self.errors) else \
                    f"{len(self.errors)} files couldn't be read, e.g. {self.errors[0]}"
                self.app.notify(detail, severity="warning", timeout=8)

        def visible(self) -> list[dict]:
            needle = self.query_one("#filter", Input).value.strip().lower()
            items = self.summaries
            if self.hits is not None:
                items = [s for s in items if s["key"] in self.hits]
            if needle:
                words = needle.split()
                items = [s for s in items if all(w in self._haystack(s) for w in words)]
            if self.hits is not None:
                return sorted(items, key=lambda s: (-self.hits[s["key"]], -(s.get("updated") or 0)))
            return sorted(items, key=SORTS[self.sort_index][1])

        @staticmethod
        def _haystack(s: dict) -> str:
            return " ".join(str(x) for x in (s.get("title"), s.get("where"), " ".join(s.get("people") or []),
                                             s.get("channel_id"), s.get("thread_ts"), s.get("path"), s.get("subkey"),
                                             s.get("format"), s.get("model"))).lower()

        def refill(self, to_top: bool = False) -> None:
            items = self.visible()
            options = self.query_one(OptionList)
            keep = None if to_top or not options.highlighted_option else options.highlighted_option.id
            rows = []
            width = max(20, (options.size.width or self.app.size.width) - 4)
            folders = {os.path.dirname(s["path"]) for s in self.summaries}
            root = os.path.commonpath(list(folders)) if len(folders) > 1 else None
            for s in items:
                title = Text(one_line(s.get("title") or "(untitled)", 400), style="bold")
                title.truncate(width, overflow="ellipsis")
                meta = [fmt_when(s.get("updated")), s.get("where") or ""]
                if s.get("people"):
                    people = [self.app.users.get(p, p) for p in s["people"]]
                    meta.append(", ".join(people[:3]) + (f" +{len(people) - 3}" if len(people) > 3 else ""))
                meta.append(f"{plural(s.get('turns', 0), 'message')} · {plural(s.get('tools', 0), 'tool call')} · "
                            f"{fmt_size(s.get('size') or 0)}")
                if s.get("format") != "trace":
                    meta.append(s.get("format"))
                if root is not None:  # several folders: say which one
                    meta.append(os.path.relpath(os.path.dirname(s["path"]), root).replace(os.sep, "/") + "/")
                if self.hits is not None:
                    meta.append(f"{self.hits[s['key']]} matches")
                details = Text("  ·  ".join(m for m in meta if m), style="dim")
                details.truncate(width, overflow="ellipsis")
                rows.append(Option(Text("\n").join([title, details, Text("")]), id=s["key"]))
            options.set_options(rows)
            if rows:
                ids = [r.id for r in rows]
                options.highlighted = ids.index(keep) if keep in ids else 0
            head = Text.assemble(("coolton conversations", "bold"), ("  ·  " + self.source.label, "dim"))
            shown = f"{len(items)} of {len(self.summaries)}" if len(items) != len(self.summaries) else str(len(items))
            head.append(f"\n{shown} conversations", style="dim")
            if self.hits is not None:
                head.append(f"  ·  text search “{self.hit_query}” (esc clears)", style="black on yellow")
            else:
                head.append(f"  ·  sorted by {SORTS[self.sort_index][0]} (s)", style="dim")
            self.query_one("#list-head", Static).update(head)

        @on(Input.Changed, "#filter")
        def filter_changed(self) -> None:
            self.refill()

        @on(Input.Submitted, "#filter")
        def filter_submitted(self) -> None:
            self.query_one(OptionList).focus()

        @on(OptionList.OptionSelected)
        def open_selected(self, event: OptionList.OptionSelected) -> None:
            summary = next(s for s in self.summaries if s["key"] == event.option.id)
            self.app.push_screen(ConversationScreen(self.source, summary, self.hit_query if self.hits else None))

        def action_cursor(self, delta: int) -> None:
            self.query_one(OptionList).action_cursor_down() if delta > 0 else self.query_one(OptionList).action_cursor_up()

        def action_filter(self) -> None:
            self.query_one("#filter", Input).focus()

        def action_clear(self) -> None:
            box = self.query_one("#filter", Input)
            if self.focused is box:
                self.query_one(OptionList).focus()
            elif box.value:
                box.value = ""
            elif self.hits is not None:
                self.hits, self.hit_query = None, ""
                self.refill()

        def action_sort(self) -> None:
            self.sort_index = (self.sort_index + 1) % len(SORTS)
            self.refill(to_top=True)

        def action_search(self) -> None:
            def run(query: str | None) -> None:
                if query:
                    self.do_search(query)
            self.app.push_screen(Ask("Search the text of every conversation (messages, replies, tool calls):",
                                     self.hit_query), run)

        @work(exclusive=True, group="search")
        async def do_search(self, query: str) -> None:
            self.app.notify(f"Searching for “{query}”…", timeout=2)
            try:
                hits = await asyncio.to_thread(self.source.search, query)
            except Exception as e:
                self.app.notify(f"Search failed: {e}", severity="error")
                return
            if not hits:
                self.app.notify(f"No conversation mentions “{query}”", severity="warning")
                return
            self.hits, self.hit_query = hits, query
            self.refill(to_top=True)

        @work(exclusive=True, group="reload")
        async def action_reload(self) -> None:
            try:
                scanned = await asyncio.to_thread(self.source.scan)
            except Exception as e:
                self.app.notify(f"Reload failed: {e}", severity="error")
                return
            self.summaries, self.errors = scanned["summaries"], scanned["errors"]
            self.app.users.update(scanned.get("users") or {})
            self.refill()
            self.app.notify(f"{len(self.summaries)} conversations", timeout=2)

    class ConvsApp(App):
        TITLE = "coolton conversations"
        CSS = """
        Screen { background: $background; }
        #list-head, #conv-head { padding: 0 1; background: $panel; color: $text; height: auto;
                                 text-wrap: nowrap; text-overflow: ellipsis; }
        #filter { margin: 0 1; }
        #convs { height: 1fr; border: none; padding: 0 1; }
        #views { height: 1fr; }
        #trace, #slack { height: 1fr; padding: 0 1; scrollbar-gutter: stable; }
        #find { dock: bottom; margin: 0 1 1 1; border: tall $accent; height: 3; }
        .entry { padding: 0 1; margin: 0 0 1 0; border-left: tall $panel-lighten-2; height: auto; }
        .entry:focus { background: $boost; border-left: tall $accent; }
        .entry.user, .entry.slack { border-left: tall $primary; }
        .entry.output { border-left: tall $success; }
        .entry.tool, .entry.thinking, .entry.note, .entry.system { margin: 0; border-left: tall $surface-lighten-1; }
        .entry.tool.failed, .entry.retry { border-left: tall $error; }
        .entry.note { color: $text-muted; }
        .entry.oneline { text-wrap: nowrap; text-overflow: ellipsis; }
        .hide-tool .entry.tool, .hide-thinking .entry.thinking, .hide-system .entry.system { display: none; }
        .entry.user:focus, .entry.slack:focus, .entry.output:focus, .entry.tool:focus, .entry.thinking:focus,
        .entry.system:focus { border-left: tall $accent; }
        FullView, Ask, Help { align: center middle; }
        #full { width: 95%; height: 95%; background: $surface; border: round $accent; }
        #full-head { padding: 0 1; height: auto; }
        #full-text { height: 1fr; border: none; }
        #ask { width: 80%; height: auto; background: $surface; border: round $accent; padding: 1 2; }
        #help { width: 84; max-width: 95%; height: auto; max-height: 95%; background: $surface;
                border: round $accent; padding: 1 2; }
        """
        BINDINGS = [Binding("ctrl+c", "quit", "Quit", show=False, priority=True)]

        def __init__(self, source, scanned: dict, open_first: bool):
            super().__init__()
            self.source, self.scanned, self.open_first = source, scanned, open_first
            self.users = scanned.get("users") or {}  # names seen anywhere, for ids a conversation doesn't name

        def on_mount(self) -> None:
            self.push_screen(ListScreen(self.source, self.scanned))
            if self.open_first and self.scanned["summaries"]:
                self.push_screen(ConversationScreen(self.source, self.scanned["summaries"][0]))

        def action_help(self) -> None:
            self.push_screen(Help())

    return ConvsApp


# ─────────────────────────────── command line ───────────────────────────────

SCRIPT_PATH = globals().get("__file__")


def main(argv: list[str]) -> int:
    if argv and argv[0].startswith("--remote-"):
        return remote_main(argv)
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if argv and argv[0] == "ssh":
        if len(argv) < 2:
            print("usage: python coolton_convs.py ssh HOST [PATH]", file=sys.stderr)
            return 2
        source = SshSource(argv[1], argv[2] if len(argv) > 2 else "")
        target_is_file = len(argv) > 2 and argv[2].endswith(SUFFIXES)
    else:
        if len(argv) > 1:
            print("usage: python coolton_convs.py [DIR | FILE]  or  python coolton_convs.py ssh HOST [PATH]",
                  file=sys.stderr)
            return 2
        target = argv[0] if argv else os.getcwd()
        if not os.path.exists(os.path.expanduser(target)):
            print(f"coolton_convs: {target} doesn't exist", file=sys.stderr)
            return 1
        source = LocalSource(target)
        target_is_file = os.path.isfile(source.root)

    ensure_textual()
    print(f"Reading conversations from {source.label}…", file=sys.stderr)
    started = time.time()
    try:
        scanned = source.scan()
    except SourceError as e:
        print(f"coolton_convs: {e}", file=sys.stderr)
        if isinstance(source, SshSource) and not source.root and CONFIG_FILE in str(e):
            print(f"Give a path, e.g.  python coolton_convs.py ssh {source.host} ~/coolton-private/conversation_logs\n"
                  f"or put that path in {CONFIG_FILE} on {source.host}.", file=sys.stderr)
        return 1
    if not scanned["summaries"] and not argv and isinstance(source, LocalSource):
        fallback = configured_root()
        if fallback and os.path.isdir(fallback):
            print(f"Nothing here; using {CONFIG_FILE} ({fallback}).", file=sys.stderr)
            source = LocalSource(fallback)
            scanned = source.scan()
    if not scanned["summaries"]:
        print(f"coolton_convs: no coolton conversations in {source.label}", file=sys.stderr)
        for error in scanned["errors"][:5]:
            print(f"  {error}", file=sys.stderr)
        return 1
    if time.time() - started > 3:
        print(f"Read {len(scanned['summaries'])} conversations in {time.time() - started:.1f}s.", file=sys.stderr)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(f"coolton_convs: found {len(scanned['summaries'])} conversations, but showing them needs an "
              "interactive terminal (over ssh, use ssh -t).", file=sys.stderr)
        return 1
    app = build_app()(source, scanned, open_first=target_is_file and len(scanned["summaries"]) == 1)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
