"""Per-conversation append-only JSONL event log for the web UI.

Each web conversation is a file at `web_conversations/<id>.jsonl`. Every line is
one JSON event with a monotonic `seq`, assigned under a per-conversation lock so
concurrent writers (a running turn, a steering message from a second tab) never
race. A separate `web_conversations/index.json` holds lightweight per-conversation
metadata (owner, title, timestamps) so listing "my conversations" doesn't require
reading every event log in full.

Subscribers (SSE connections) get new events pushed live via a plain synchronous
callback — the caller bridges thread-safety into asyncio itself (see
web.conversations), since append_event can run from a background turn thread.
A reconnecting client replays everything after its last-seen seq straight from
disk (read_events), so closing the tab and coming back never loses anything,
even across a server restart — see repair_orphaned_turns for the restart case
where a turn was killed mid-flight.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Callable

logger = logging.getLogger(__name__)

STORE_DIR = os.environ.get("WEB_CONVERSATIONS_DIR", "web_conversations")

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_last_seq: dict[str, int] = {}
_subscribers: dict[str, list[Callable[[dict], None]]] = {}
_subscribers_guard = threading.Lock()
_index_lock = threading.Lock()


def _lock_for(conversation_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(conversation_id)
        if lock is None:
            lock = threading.Lock()
            _locks[conversation_id] = lock
        return lock


def _path(conversation_id: str) -> str:
    return os.path.join(STORE_DIR, f"{conversation_id}.jsonl")


def _index_path() -> str:
    return os.path.join(STORE_DIR, "index.json")


def _load_index() -> dict:
    try:
        with open(_index_path()) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_index(data: dict) -> None:
    os.makedirs(STORE_DIR, exist_ok=True)
    path = _index_path()
    temp = f"{path}.tmp"
    with open(temp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(temp, path)


# ---------------------------------------------------------------------------
# Conversation metadata (index.json)
# ---------------------------------------------------------------------------


def create_conversation(owner_id: str, title: str = "") -> str:
    os.makedirs(STORE_DIR, exist_ok=True)
    conversation_id = uuid.uuid4().hex[:16]
    open(_path(conversation_id), "a").close()
    now = time.time()
    with _index_lock:
        data = _load_index()
        data[conversation_id] = {
            "owner_id": owner_id, "title": title,
            "created_at": now, "updated_at": now,
        }
        _save_index(data)
    return conversation_id


def list_conversations(owner_id: str) -> list[dict]:
    with _index_lock:
        data = _load_index()
    rows = [
        {"id": cid, **meta} for cid, meta in data.items()
        if meta.get("owner_id") == owner_id
    ]
    rows.sort(key=lambda r: r.get("updated_at", 0), reverse=True)
    return rows


def get_conversation_meta(conversation_id: str) -> dict | None:
    with _index_lock:
        data = _load_index()
    meta = data.get(conversation_id)
    return {"id": conversation_id, **meta} if meta else None


def is_owner(conversation_id: str, owner_id: str) -> bool:
    meta = get_conversation_meta(conversation_id)
    return bool(meta) and meta.get("owner_id") == owner_id


def set_title(conversation_id: str, title: str) -> None:
    with _index_lock:
        data = _load_index()
        if conversation_id in data:
            data[conversation_id]["title"] = title
            data[conversation_id]["updated_at"] = time.time()
            _save_index(data)


def title_from_message(text: str, limit: int = 52) -> str:
    """A conversation's name, derived from the message that opened it.

    First non-empty line, whitespace collapsed, cut on a word boundary. A
    conversation is named after what was asked, which is how the person
    remembers it — nothing here needs the model.
    """
    line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
    line = " ".join(line.split())
    if len(line) <= limit:
        return line
    cut = line[:limit].rsplit(" ", 1)[0] or line[:limit]
    return f"{cut}…"


def delete_conversation(conversation_id: str) -> bool:
    """Remove a conversation's event log and its index entry. Returns whether
    there was anything to remove."""
    with _index_lock:
        data = _load_index()
        existed = data.pop(conversation_id, None) is not None
        if existed:
            _save_index(data)
    try:
        os.remove(_path(conversation_id))
    except OSError:
        pass
    _last_seq.pop(conversation_id, None)
    with _subscribers_guard:
        _subscribers.pop(conversation_id, None)
    return existed


def _touch_updated_at(conversation_id: str) -> None:
    with _index_lock:
        data = _load_index()
        if conversation_id in data:
            data[conversation_id]["updated_at"] = time.time()
            _save_index(data)


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


def _last_seq_for(conversation_id: str) -> int:
    if conversation_id in _last_seq:
        return _last_seq[conversation_id]
    seq = 0
    path = _path(conversation_id)
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    seq = max(seq, json.loads(line).get("seq", 0))
                except Exception:
                    continue
    _last_seq[conversation_id] = seq
    return seq


def append_event(conversation_id: str, event: dict[str, Any]) -> dict[str, Any]:
    """Append one event, assigning it the next seq + a timestamp. Returns the
    full stored event (with seq/ts filled in)."""
    lock = _lock_for(conversation_id)
    with lock:
        seq = _last_seq_for(conversation_id) + 1
        full_event = {"seq": seq, "ts": time.time(), **event}
        os.makedirs(STORE_DIR, exist_ok=True)
        with open(_path(conversation_id), "a") as f:
            f.write(json.dumps(full_event) + "\n")
        _last_seq[conversation_id] = seq
    _touch_updated_at(conversation_id)
    _notify_subscribers(conversation_id, full_event)
    return full_event


def read_events(conversation_id: str, after: int = 0) -> list[dict[str, Any]]:
    path = _path(conversation_id)
    events: list[dict[str, Any]] = []
    if not os.path.exists(path):
        return events
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("seq", 0) > after:
                events.append(ev)
    return events


def subscribe(conversation_id: str, callback: Callable[[dict], None]) -> Callable[[], None]:
    """Register `callback(event)` for every future append_event on this
    conversation. Returns an unsubscribe function."""
    with _subscribers_guard:
        _subscribers.setdefault(conversation_id, []).append(callback)

    def unsubscribe() -> None:
        with _subscribers_guard:
            lst = _subscribers.get(conversation_id)
            if lst and callback in lst:
                lst.remove(callback)

    return unsubscribe


def _notify_subscribers(conversation_id: str, event: dict) -> None:
    with _subscribers_guard:
        callbacks = list(_subscribers.get(conversation_id, []))
    for cb in callbacks:
        try:
            cb(event)
        except Exception:
            logger.exception("Web conversation subscriber callback failed for %s", conversation_id)


def repair_orphaned_turns() -> int:
    """Startup repair: any conversation whose last turn_start has no matching
    turn_end (the server died mid-turn) gets a synthetic error turn_end appended,
    so a restart can never leave a spinner stuck forever. Returns how many
    conversations were repaired."""
    if not os.path.isdir(STORE_DIR):
        return 0
    repaired = 0
    for name in os.listdir(STORE_DIR):
        if not name.endswith(".jsonl"):
            continue
        conversation_id = name[: -len(".jsonl")]
        events = read_events(conversation_id)
        last_start = None
        last_end = None
        for ev in events:
            if ev.get("type") == "turn_start":
                last_start = ev["seq"]
            elif ev.get("type") == "turn_end":
                last_end = ev["seq"]
        if last_start is not None and (last_end is None or last_end < last_start):
            append_event(conversation_id, {
                "type": "turn_end", "state": "error", "reason": "server restarted",
            })
            repaired += 1
    if repaired:
        logger.warning("Repaired %d web conversation(s) with a turn orphaned by a restart", repaired)
    return repaired
