"""Persists just enough about an in-flight turn to auto-resume it if coolton
is restarted (or crashes) mid-turn.

`systemctl restart` sends SIGTERM, and Python's default disposition for
SIGTERM terminates the process immediately — no `finally` block runs, so a
turn that never reached listeners.events.turn.run_agent_turn's own `finally`
(where record_finished is called, right alongside agent.active_runs'
in-memory mark_run_finished) leaves its record behind. Anything still here at
the next startup was cut off mid-turn and should be picked back up — see
listeners.events.turn.resume_orphaned_runs, called once at process startup.

Each in-flight turn also keeps a checkpoint: its messages so far, saved before
every model request (agent.agent's checkpoint hook), so everything up to the last
completed tool round. A resumed turn continues from it instead of starting over
and redoing work it already did.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

STORE_PATH = Path(os.environ.get("COOLTON_INFLIGHT_RUNS_STORE", "inflight_runs.json"))
_lock = threading.Lock()


def _checkpoint_path(channel_id: str, thread_ts: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in _key(channel_id, thread_ts))
    return STORE_PATH.parent / "inflight_checkpoints" / f"{safe}.json"


def _load() -> dict[str, Any]:
    try:
        data = json.loads(STORE_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict[str, Any]) -> None:
    parent = STORE_PATH.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="coolton-inflight-", dir=str(parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(name, STORE_PATH)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _key(channel_id: str, thread_ts: str) -> str:
    return f"{channel_id}:{thread_ts}"


def record_start(
    channel_id: str, thread_ts: str, *, message_ts: str, user_id: str, text: str, is_slack: bool,
    on_behalf_of: str = "",
) -> None:
    with _lock:
        data = _load()
        data[_key(channel_id, thread_ts)] = {
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "message_ts": message_ts,
            "user_id": user_id,
            "text": text,
            "is_slack": is_slack,
            # who an automated turn acts for (AgentDeps.on_behalf_of), so a resumed
            # one still credits them
            "on_behalf_of": on_behalf_of,
        }
        _save(data)
        # A new turn starts clean: never resume it from an older turn's checkpoint.
        # (A resumed turn loads its checkpoint before calling this.)
        _checkpoint_path(channel_id, thread_ts).unlink(missing_ok=True)


def record_finished(channel_id: str, thread_ts: str) -> None:
    with _lock:
        data = _load()
        if data.pop(_key(channel_id, thread_ts), None) is not None:
            _save(data)
        _checkpoint_path(channel_id, thread_ts).unlink(missing_ok=True)


def save_checkpoint(channel_id: str, thread_ts: str, messages: list) -> None:
    """Save an in-flight turn's messages so far. No-op for a turn that isn't recorded
    as in flight (a subagent, kevinton, a test). Never raises."""
    try:
        from pydantic_ai.messages import ModelMessagesTypeAdapter

        with _lock:
            if _key(channel_id, thread_ts) not in _load():
                return
            path = _checkpoint_path(channel_id, thread_ts)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix="coolton-checkpoint-", dir=str(path.parent))
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(ModelMessagesTypeAdapter.dump_json(messages))
                os.replace(name, path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Couldn't save the checkpoint for %s/%s", channel_id, thread_ts)


def load_checkpoint(channel_id: str, thread_ts: str) -> list | None:
    """The messages an interrupted turn had got through, or None if it never saved any
    (or the file is unreadable, in which case the turn just starts over)."""
    from pydantic_ai.messages import ModelMessagesTypeAdapter

    try:
        return ModelMessagesTypeAdapter.validate_json(_checkpoint_path(channel_id, thread_ts).read_bytes()) or None
    except Exception:
        return None


def pop_all() -> list[dict[str, Any]]:
    """Every record still present. A clean run always clears its own record in
    `finally`, so anything left here was orphaned by a crash or restart.
    Clears the store — each resumed turn re-records itself once it starts."""
    with _lock:
        data = _load()
        if data:
            _save({})
        return list(data.values())
