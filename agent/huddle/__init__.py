"""Slack huddles: coolton joins one as a voice participant (as cooltonUser, see
agent.huddle.slack_api), listens, talks back, reads and answers the huddle's thread, and
shows its desktop as its video while it's using one.

Each huddle is an agent.huddle.session.HuddleSession. They all run on one asyncio event
loop on its own thread (WebRTC and the Deepgram sockets are asyncio); the functions here
are the thread-safe way in for the rest of coolton (tools, Slack listeners), keyed by
the huddle thread's (channel_id, thread_ts).
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import os
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.huddle.session import HuddleSession

logger = logging.getLogger(__name__)

_JOIN_TIMEOUT = 60
_sessions: dict[tuple[str, str], HuddleSession] = {}
_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None


def _event_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True, name="huddles").start()
        return _loop


def session_for(channel_id: str, thread_ts: str) -> HuddleSession | None:
    with _lock:
        return _sessions.get((channel_id, thread_ts))


def _forget(session) -> None:
    with _lock:
        if _sessions.get((session.channel_id, session.thread_ts)) is session:
            del _sessions[(session.channel_id, session.thread_ts)]


def join(client, *, channel_id: str, thread_ts: str | None, requester_id: str, request_text: str) -> str:
    """Join the live huddle of the conversation coolton was asked in (agent.agent's
    join_huddle_tool). Returns a status line for the model."""
    from agent.ensure_coolton_user import ensure_coolton_user_in_channel
    from agent.huddle import slack_api
    from agent.huddle.session import HuddleSession

    if not os.environ.get("DEEPGRAM_API_KEY"):
        return "Error: huddles aren't set up: DEEPGRAM_API_KEY is missing from the server's .env."
    try:
        slack_api.workspace_url()
        ensure_coolton_user_in_channel(client, channel_id)
        huddle_ts = slack_api.find_live_huddle(channel_id, thread_ts)
        if not huddle_ts:
            return ("Error: there's no huddle going on in this channel right now. Ask the user to start "
                    "one (or to ask you from the huddle's own thread).")
        if session_for(channel_id, huddle_ts):
            return "You're already in that huddle."
        joined = slack_api.join(channel_id)
    except slack_api.HuddleUnavailable as e:
        return f"Error: {e}."
    except Exception as e:
        logger.exception("Couldn't join the huddle in %s", channel_id)
        return f"Error: couldn't join the huddle: {e}"

    session = HuddleSession(joined, client=client, requester_id=requester_id, request_text=request_text,
                            on_closed=_forget)
    loop = session.loop = _event_loop()
    with _lock:
        _sessions[(session.channel_id, session.thread_ts)] = session
    started = asyncio.run_coroutine_threadsafe(session.start(), loop)
    try:
        started.result(timeout=_JOIN_TIMEOUT)
    except Exception as e:
        started.cancel()
        asyncio.run_coroutine_threadsafe(session.stop(f"couldn't connect its audio ({e})"), loop)
        _forget(session)
        logger.exception("Couldn't connect to the huddle in %s", channel_id)
        return f"Error: joined the huddle but couldn't connect its audio: {e}"
    return (f"Joined the huddle (its thread: channel {session.channel_id}, thread_ts {session.thread_ts}). "
            "From now on the huddle's voice agent talks in the call and answers that thread; "
            "just confirm briefly.")


def route_chat(channel_id: str, thread_ts: str, user_id: str, text: str) -> bool:
    """Hand a message in a huddle's thread to the huddle coolton is in, if it's in one
    there. Returns whether it took the message."""
    session = session_for(channel_id, thread_ts)
    if session is None:
        return False
    session.loop.call_soon_threadsafe(session.on_chat, user_id, text)
    return True


def desktop_active(channel_id: str, thread_ts: str) -> bool:
    """A tool is using this thread's desktop. When coolton is in this thread's huddle,
    the desktop becomes its video there (instead of a stream link) and this returns True."""
    session = session_for(channel_id, thread_ts)
    if session is None:
        return False
    session.loop.call_soon_threadsafe(session.desktop_active)
    return True


def sharing_desktop(channel_id: str, thread_ts: str) -> bool:
    session = session_for(channel_id, thread_ts)
    return bool(session is not None and session.video_on)


@atexit.register
def _leave_all() -> None:
    with _lock:
        sessions = list(_sessions.values())
    if _loop is None:
        return
    for session in sessions:
        try:
            asyncio.run_coroutine_threadsafe(session.stop("coolton is restarting"), _loop).result(timeout=5)
        except Exception:
            pass
