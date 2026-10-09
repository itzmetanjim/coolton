"""A turn's Slack agent session (agents.sessions.*), as Slack's agent docs describe it.

  - start() puts the thread's session in `processing`: Slack's standard "is working"
    loading UI plus, since coolton subscribes to agent_session_stopped, a stop button
    (see listeners.events.agent_session_stopped). The session is titled after the
    request, which is what the sessions list shows for recall (except a code channel's,
    whose title is the channel's name).
  - stop() sets it `active`. Slack no longer clears the loading UI when a message is
    posted, so a turn that never reached stop() would stay "working" until Slack's
    one-hour timeout.

setStatus takes no custom loading text: what coolton is doing step by step is shown by
its plan message (agent.plan_block), Slack's plan block with one task per tool call.
`processing` times out after an hour; a timer re-sends it every _REFRESH_SECONDS so a
very long turn keeps its loading UI. Never raises: a flaky status API must not block
the actual turn.
"""

import logging
import re
import threading

logger = logging.getLogger(__name__)

_MAX_TITLE_LEN = 200  # agent session titles: 1-200 characters
_REFRESH_SECONDS = 30 * 60.0

_lock = threading.Lock()
_state: dict[tuple[str, str], dict] = {}


def _title(text: str) -> str:
    text = re.sub(r"<@[A-Z0-9]+>", "", text or "")
    text = " ".join(text.split())
    return (text[:_MAX_TITLE_LEN - 1] + "…") if len(text) > _MAX_TITLE_LEN else (text or "coolton")


def _call(client, method: str, channel_id: str, thread_ts: str, **fields) -> None:
    if not thread_ts:
        # thread_ts="" is a code channel's channel-level conversation (see
        # agent.code_channel_store): the whole channel is one session, which belongs to
        # its agent bot, the code channels app (agent.code_channel_api), not coolton's.
        from agent.code_channel_api import call

        response = call(method, channel_id=channel_id, **fields)
        if not response.get("ok") and response.get("error") != "not_configured":
            logger.warning(f"{method} failed for code channel {channel_id}: {response.get('error')}")
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
    """Begin a turn: session `processing`, titled after `request_text` (the user's
    message), and the keep-alive timer armed."""
    key = (channel_id, thread_ts)
    title = _title(request_text)
    with _lock:
        old = _state.pop(key, None)
        if old and old.get("timer"):
            old["timer"].cancel()
        _state[key] = {"client": client, "timer": None}
        _arm_refresh(key)
    if not thread_ts:
        # A code channel's session title is the channel's name, so it's left alone (renaming
        # it is code_channel_rename_tool's job, when someone asks).
        _call(client, "agents.sessions.setStatus", channel_id, thread_ts, status="processing")
        return
    # `title` only applies when this creates the session, so rename too for an existing one.
    _call(client, "agents.sessions.setStatus", channel_id, thread_ts, status="processing", title=title)
    _call(client, "agents.sessions.rename", channel_id, thread_ts, title=title)


def stop(channel_id: str, thread_ts: str) -> None:
    """End of turn: set the session `active` and drop state so the keep-alive timer
    never outlives the turn."""
    with _lock:
        entry = _state.pop((channel_id, thread_ts), None)
        if entry and entry.get("timer"):
            entry["timer"].cancel()
    if entry is None:
        return
    _call(entry["client"], "agents.sessions.setStatus", channel_id, thread_ts, status="active")


def end_session_now(client, channel_id: str, thread_ts: str) -> None:
    """Take the session out of `processing` right away (the stop button). The turn's
    own stop() still runs when the halted run finishes."""
    _call(client, "agents.sessions.setStatus", channel_id, thread_ts, status="active")
