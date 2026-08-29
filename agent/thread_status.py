"""Live "what's coolton doing right now" status via Slack's assistant.threads.setStatus.

Shown in the AI-assistant thread pane's status pill — distinct from the plan/thinking
block (agent.plan_block) and from mid-turn text narration. Kept fresh two ways, mirroring
agent.sandbox_keepalive's per-thread timer pattern:
  - agent.plan_block's before_tool_execute hook calls set_status() with the tool's
    display name on every tool call, so the pill always shows the last tool called.
  - a repeating timer resends whatever the current status is every _REFRESH_SECONDS, so
    a long stretch of pure model "thinking" (no tool call yet, or a single slow tool)
    doesn't leave a stale status on screen.

start()/stop() bracket one turn (listeners.events.turn.run_agent_turn): start() sends the
turn's initial status ("Working") and arms the refresh timer; stop() cancels it so a
finished turn's timer never fires into whatever thread starts next. Never raises — a
flaky status API must not block the actual turn.
"""

import logging
import threading

logger = logging.getLogger(__name__)

# Slack hard-caps assistant.threads.setStatus's status text at 49 characters.
_MAX_STATUS_LEN = 49
_REFRESH_SECONDS = 30.0

_lock = threading.Lock()
_state: dict[tuple[str, str], dict] = {}


def _crop(status: str) -> str:
    return status[:_MAX_STATUS_LEN]


def _send(client, channel_id: str, thread_ts: str, status: str) -> None:
    try:
        client.assistant_threads_setStatus(channel_id=channel_id, thread_ts=thread_ts, status=status)
    except Exception as e:
        logger.warning(f"assistant_threads_setStatus failed for {channel_id}/{thread_ts}: {e}")


def _arm_refresh(key: tuple[str, str]) -> None:
    """Caller must hold _lock and key must already be in _state."""
    t = threading.Timer(_REFRESH_SECONDS, _tick, args=(key[0], key[1]))
    t.daemon = True
    _state[key]["timer"] = t
    t.start()


def _tick(channel_id: str, thread_ts: str) -> None:
    key = (channel_id, thread_ts)
    with _lock:
        entry = _state.get(key)
        if entry is None:
            return
        client, status = entry["client"], entry["status"]
        _arm_refresh(key)
    _send(client, channel_id, thread_ts, status)


def start(client, channel_id: str, thread_ts: str, status: str = "Working") -> None:
    """Begin a turn: send the initial status right away and arm the refresh timer."""
    key = (channel_id, thread_ts)
    cropped = _crop(status)
    with _lock:
        old = _state.pop(key, None)
        if old and old.get("timer"):
            old["timer"].cancel()
        _state[key] = {"client": client, "status": cropped, "timer": None}
        _arm_refresh(key)
    _send(client, channel_id, thread_ts, cropped)


def set_status(channel_id: str, thread_ts: str, status: str) -> None:
    """Update the status shown (e.g. on every tool call), sending it immediately and
    resetting the refresh countdown. No-op if start() was never called for this thread
    (e.g. this hook fires in a context that never armed live status updates)."""
    key = (channel_id, thread_ts)
    cropped = _crop(status)
    with _lock:
        entry = _state.get(key)
        if entry is None:
            return
        if entry.get("timer"):
            entry["timer"].cancel()
        entry["status"] = cropped
        client = entry["client"]
        _arm_refresh(key)
    _send(client, channel_id, thread_ts, cropped)


def stop(channel_id: str, thread_ts: str) -> None:
    """End of turn: cancel the refresh timer and drop state so it never outlives the turn."""
    key = (channel_id, thread_ts)
    with _lock:
        entry = _state.pop(key, None)
        if entry and entry.get("timer"):
            entry["timer"].cancel()
