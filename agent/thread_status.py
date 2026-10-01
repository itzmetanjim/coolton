"""Live "what's coolton doing right now" status, via Slack agent sessions.

A turn puts the thread's agent session in `processing` (agents.sessions.setStatus),
which shows Slack's loading UI and, since coolton subscribes to agent_session_stopped,
a stop button (see listeners.events.agent_session_stopped). setStatus takes no custom
text, so the text lives in the session's title (agents.sessions.rename):
  - start() creates/updates the session as `processing`, titled "working".
  - set_status() renames it on every tool call (agent.plan_block) and every
    set_activity (agent.surfaces.slack), e.g. "calling tool: Search web".
  - stop() renames it to the request that started the turn, so the sessions list
    reads as a list of requests rather than whatever tool ran last, and sets it
    `active`. Slack no longer clears the loading UI when a message is posted, so a
    turn that never reached stop() would stay "working" until Slack's one-hour timeout.

`processing` times out after an hour; a timer re-sends it every _REFRESH_SECONDS so a
very long turn keeps its loading UI. Never raises: a flaky status API must not block
the actual turn.
"""

import logging
import re
import threading

logger = logging.getLogger(__name__)

_MAX_TITLE_LEN = 200  # agents.sessions.rename accepts 1-200 characters
_REFRESH_SECONDS = 30 * 60.0
_START_TITLE = "working"

_lock = threading.Lock()
_state: dict[tuple[str, str], dict] = {}


def _title(text: str) -> str:
    text = re.sub(r"<@[A-Z0-9]+>", "", text or "")
    text = " ".join(text.split())
    return (text[:_MAX_TITLE_LEN - 1] + "…") if len(text) > _MAX_TITLE_LEN else (text or "coolton")


def _call(client, method: str, channel_id: str, thread_ts: str, **fields) -> None:
    if not thread_ts:
        # thread_ts="" is a code channel's channel-level conversation (see
        # agent.code_channel_store): there's no real Slack thread for a session.
        return
    try:
        client.api_call(method, json={"channel_id": channel_id, "thread_ts": thread_ts, **fields})
    except Exception as e:
        logger.warning(f"{method} failed for {channel_id}/{thread_ts}: {e}")


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
        client = entry["client"]
        _arm_refresh(key)
    _call(client, "agents.sessions.setStatus", channel_id, thread_ts, status="processing")


def start(client, channel_id: str, thread_ts: str, request_text: str = "") -> None:
    """Begin a turn: put the session in `processing` and arm the keep-alive timer.
    `request_text` (the user's message) becomes the session's title when the turn ends."""
    key = (channel_id, thread_ts)
    with _lock:
        old = _state.pop(key, None)
        if old and old.get("timer"):
            old["timer"].cancel()
        _state[key] = {"client": client, "final_title": _title(request_text), "timer": None}
        _arm_refresh(key)
    # `title` only applies when this creates the session, so rename too for an existing one.
    _call(client, "agents.sessions.setStatus", channel_id, thread_ts, status="processing", title=_START_TITLE)
    _call(client, "agents.sessions.rename", channel_id, thread_ts, title=_START_TITLE)


def set_status(channel_id: str, thread_ts: str, status: str) -> None:
    """Show what coolton is doing right now (e.g. "calling tool: Search web") as the
    session title. No-op if start() was never called for this thread."""
    with _lock:
        entry = _state.get((channel_id, thread_ts))
        if entry is None:
            return
        client = entry["client"]
    _call(client, "agents.sessions.rename", channel_id, thread_ts, title=_title(status))


def stop(channel_id: str, thread_ts: str) -> None:
    """End of turn: title the session after the request, set it `active`, and drop
    state so the keep-alive timer never outlives the turn."""
    with _lock:
        entry = _state.pop((channel_id, thread_ts), None)
        if entry and entry.get("timer"):
            entry["timer"].cancel()
    if entry is None:
        return
    client = entry["client"]
    _call(client, "agents.sessions.rename", channel_id, thread_ts, title=entry["final_title"])
    _call(client, "agents.sessions.setStatus", channel_id, thread_ts, status="active")


def end_session_now(client, channel_id: str, thread_ts: str) -> None:
    """Take the session out of `processing` right away (the stop button). The turn's
    own stop() still runs when the halted run finishes."""
    _call(client, "agents.sessions.setStatus", channel_id, thread_ts, status="active")
