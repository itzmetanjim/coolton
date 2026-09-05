"""Conversation CRUD, SSE streaming, and attachments for coolton's web UI."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from web import conversation_log as log
from web.auth import require_slack_id

logger = logging.getLogger(__name__)
router = APIRouter()

ATTACHMENTS_DIR = os.environ.get("WEB_ATTACHMENTS_DIR", "web_attachments")
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
_SSE_KEEPALIVE_SECONDS = 15


def _require_owner(request: Request, conversation_id: str) -> str:
    owner_id = require_slack_id(request)
    if not owner_id:
        raise HTTPException(401, "Not signed in")
    if not log.is_owner(conversation_id, owner_id):
        raise HTTPException(404, "Conversation not found")
    return owner_id


@router.get("/api/me")
def get_me(request: Request):
    owner_id = require_slack_id(request)
    if not owner_id:
        raise HTTPException(401, "Not signed in")
    from agent.agent import _get_user_display_info
    name, _pfp = _get_user_display_info(owner_id)
    return {
        "slack_id": owner_id,
        "display_name": name or owner_id,
        "avatar_url": f"https://cachet.dunkirk.sh/users/{owner_id}/r",
    }


@router.get("/api/conversations")
def list_conversations_route(request: Request):
    owner_id = require_slack_id(request)
    if not owner_id:
        raise HTTPException(401, "Not signed in")
    from agent.active_runs import is_run_active
    from web.runner import WEB_CHANNEL_ID

    rows = log.list_conversations(owner_id)
    for row in rows:
        row["working"] = is_run_active(WEB_CHANNEL_ID, row["id"])
    return rows


@router.post("/api/conversations")
def create_conversation_route(request: Request):
    owner_id = require_slack_id(request)
    if not owner_id:
        raise HTTPException(401, "Not signed in")
    conversation_id = log.create_conversation(owner_id)
    return log.get_conversation_meta(conversation_id)


@router.get("/api/conversations/{conversation_id}")
def get_conversation_route(conversation_id: str, request: Request):
    _require_owner(request, conversation_id)
    return {
        "meta": log.get_conversation_meta(conversation_id),
        "events": log.read_events(conversation_id),
    }


class RenameBody(BaseModel):
    title: str


@router.patch("/api/conversations/{conversation_id}")
def rename_conversation_route(conversation_id: str, body: RenameBody, request: Request):
    _require_owner(request, conversation_id)
    title = " ".join(body.title.split())[:80]
    log.set_title(conversation_id, title)
    return log.get_conversation_meta(conversation_id)


@router.delete("/api/conversations/{conversation_id}")
def delete_conversation_route(conversation_id: str, request: Request):
    _require_owner(request, conversation_id)
    from agent.stop_store import request_stop

    # A conversation being deleted out from under a live turn would keep writing
    # events to a log nobody can reach; stop it on the way out.
    request_stop("web", conversation_id)
    log.delete_conversation(conversation_id)
    return {"ok": True}


class SendMessageBody(BaseModel):
    text: str = ""
    attachment_ids: list[str] = []


@router.post("/api/conversations/{conversation_id}/messages")
def send_message_route(conversation_id: str, body: SendMessageBody, request: Request):
    owner_id = _require_owner(request, conversation_id)
    if not body.text.strip() and not body.attachment_ids:
        raise HTTPException(400, "Empty message")

    attachments = []
    for asset_id in body.attachment_ids:
        meta = _load_attachment_meta(asset_id)
        if meta and meta.get("conversation_id") == conversation_id:
            attachments.append(meta)

    from web.runner import submit_message
    submit_message(conversation_id, owner_id, body.text, attachments)
    return {"ok": True}


@router.post("/api/conversations/{conversation_id}/stop")
def stop_route(conversation_id: str, request: Request):
    _require_owner(request, conversation_id)
    from agent.stop_store import request_stop
    request_stop("web", conversation_id)
    return {"ok": True}


def _sse_line(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


@router.get("/api/conversations/{conversation_id}/events")
async def stream_events(conversation_id: str, request: Request, after: int = 0):
    _require_owner(request, conversation_id)

    async def event_stream():
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def on_event(event: dict) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

        # Subscribe BEFORE replaying, so nothing appended during the replay read
        # can slip through the gap — the last_seq watermark below drops whatever
        # the live queue delivers that replay already covered.
        unsubscribe = log.subscribe(conversation_id, on_event)
        try:
            last_seq = after
            for event in log.read_events(conversation_id, after=after):
                last_seq = max(last_seq, event.get("seq", last_seq))
                yield _sse_line(event)

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_SSE_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if event.get("seq", 0) <= last_seq:
                    continue
                last_seq = event["seq"]
                yield _sse_line(event)
        finally:
            unsubscribe()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


# What upload_attachment_route ever actually generates: a 24-hex-char digest
# plus an optional lowercased extension (at most 12 chars, from
# os.path.splitext) of plain filename characters. attachment_ids reach
# _load_attachment_meta straight from a request body (send_message_route's
# JSON, below) with no other validation, so this is the only thing standing
# between that and a path-traversal read of an arbitrary "*.json" file on
# disk (e.g. "../../../../etc/something") — reject anything that doesn't
# match the real shape before it's ever joined into a path.
_ASSET_ID_RE = re.compile(r"^[0-9a-f]{24}[A-Za-z0-9_.-]{0,12}$")


def _valid_asset_id(asset_id: str) -> bool:
    return bool(asset_id) and bool(_ASSET_ID_RE.match(asset_id))


def _attachment_meta_path(asset_id: str) -> str:
    return os.path.join(ATTACHMENTS_DIR, f"{asset_id}.json")


def _load_attachment_meta(asset_id: str) -> dict | None:
    if not _valid_asset_id(asset_id):
        return None
    try:
        with open(_attachment_meta_path(asset_id)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


@router.post("/api/conversations/{conversation_id}/attachments")
async def upload_attachment_route(conversation_id: str, request: Request, file: UploadFile = File(...)):
    _require_owner(request, conversation_id)

    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(413, "File too large (25MB max)")

    os.makedirs(ATTACHMENTS_DIR, exist_ok=True)
    # Salted with conversation_id so two conversations uploading identical
    # bytes (a common case — the same screenshot, the same sample file) don't
    # collide on the same asset_id and silently steal each other's metadata
    # record (the second upload's conversation_id would otherwise overwrite
    # the first's, transferring ownership and 404-ing the original uploader).
    digest = hashlib.sha256(f"{conversation_id}:".encode() + content).hexdigest()[:24]
    _, ext = os.path.splitext(file.filename or "")
    ext = re.sub(r"[^A-Za-z0-9_.-]", "", ext[:12].lower())
    asset_id = f"{digest}{ext}"

    mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"
    meta = {
        "id": asset_id, "name": file.filename or asset_id, "mime": mime,
        "size": len(content), "conversation_id": conversation_id,
        "media_type": mime,
    }
    with open(os.path.join(ATTACHMENTS_DIR, asset_id), "wb") as f:
        f.write(content)
    with open(_attachment_meta_path(asset_id), "w") as f:
        json.dump(meta, f)

    return {"id": asset_id, "url": f"/api/files/{asset_id}", "name": meta["name"], "mime": mime, "size": meta["size"]}


@router.get("/api/files/{asset_id}")
def get_file_route(asset_id: str, request: Request):
    owner_id = require_slack_id(request)
    if not owner_id:
        raise HTTPException(401, "Not signed in")
    meta = _load_attachment_meta(asset_id)
    if not meta or not log.is_owner(meta["conversation_id"], owner_id):
        raise HTTPException(404, "Not found")
    path = os.path.join(ATTACHMENTS_DIR, asset_id)
    if not os.path.isfile(path):
        raise HTTPException(404, "Not found")
    return FileResponse(path, media_type=meta["mime"], filename=meta["name"])


def download_conversation_attachments(conversation_id: str, sandbox, limit: int = 20) -> str:
    """Download this conversation's attachments into the sandbox — the web
    equivalent of agent.agent.download_slack_attachments. Used by
    agent.surfaces.web.WebSurface.download_attachments."""
    if not os.path.isdir(ATTACHMENTS_DIR):
        return "No attachments found in this conversation."
    sandbox.commands.run("mkdir -p ~/attachments")
    results = []
    for name in sorted(os.listdir(ATTACHMENTS_DIR)):
        if len(results) >= limit:
            break
        if not name.endswith(".json"):
            continue
        asset_id = name[: -len(".json")]
        meta = _load_attachment_meta(asset_id)
        if not meta or meta.get("conversation_id") != conversation_id:
            continue
        path = os.path.join(ATTACHMENTS_DIR, asset_id)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as f:
            content = f.read()
        filename = meta.get("name", asset_id)
        sandbox.files.write(f"/home/user/attachments/{filename}", content)
        results.append(f"✓ {filename} ({len(content)} bytes)")
    if not results:
        return "No attachments found in this conversation."
    return "Downloaded to ~/attachments/:\n" + "\n".join(results)


def summarize_conversation(conversation_id: str) -> str:
    """Summarize this web conversation — the equivalent of
    agent.tools.summarize_thread.summarize_thread for a Slack thread. Used by
    agent.surfaces.web.WebSurface.summarize."""
    events = log.read_events(conversation_id)
    lines = []
    for ev in events:
        if ev.get("type") == "user_message":
            lines.append(f"[user]: {ev.get('text', '')}")
        elif ev.get("type") == "agent_message" and ev.get("variant") == "final":
            lines.append(f"[coolton]: {ev.get('text', '')}")
    conversation_text = "\n".join(lines)
    if not conversation_text.strip():
        return "No messages found in this conversation."

    try:
        from slack_sdk import WebClient

        from agent.deps import AgentDeps
        from agent.subagents import run_subagent

        client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))
        deps = AgentDeps(
            client=client, user_id=os.environ.get("COOLTON_USER_ID", "") or "",
            channel_id="web", thread_ts=conversation_id, message_ts="1.0",
            user_token=os.environ.get("SLACK_USER_TOKEN"),
        )
        task = (
            "Summarize this web conversation clearly and concisely. Preserve decisions, "
            f"open questions, and action items when present.\n\n{conversation_text[:20000]}"
        )
        summary = run_subagent("summarizer", task, deps)
        return summary or "Error: summarizer returned nothing."
    except Exception as e:
        return f"Error summarizing conversation: {e}"
