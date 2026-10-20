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
import re
import threading
import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.huddle.session import HuddleSession

logger = logging.getLogger(__name__)

_JOIN_TIMEOUT = 120
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


# Huddles coolton is connecting to right now (joining takes seconds), by (channel, huddle
# thread), mapped to whether it was kicked meanwhile. Without this, every request or approval
# during that window started another coolton in the same call, each one replacing the last
# in _sessions so kicks and restarts could only reach the newest.
_joining: dict[tuple[str, str], bool] = {}
_APPROVAL_TTL = 15 * 60
# Join requests waiting for someone in the huddle to approve them, by id (the buttons' value).
_pending: dict[str, dict] = {}
KICK_RE = re.compile(r"^\s*(?:<@[A-Z0-9]+>\s*)?!kick-coolton\s*$", re.IGNORECASE)


def request_join(client, *, channel_id: str, thread_ts: str | None, requester_id: str, request_text: str,
                 may_start: bool = True) -> str:
    """Ask a channel's live huddle whether coolton may join (agent.agent's join_huddle_tool):
    the huddle opts in to coolton's policy as a whole, so coolton joins only once someone
    there approves. With no huddle going, start one, unless `may_start` is False (a channel
    other than the one coolton was asked in). Returns a status line for the model."""
    from agent.huddle import slack_api
    from agent.policy_consent import POLICY_URL

    if not os.environ.get("DEEPGRAM_API_KEY"):
        return "Error: huddles aren't set up: DEEPGRAM_API_KEY is missing from the server's .env."
    try:
        slack_api.workspace_url()
        huddle_ts = slack_api.find_live_huddle(channel_id, thread_ts)
        if not huddle_ts:
            if not may_start:
                return (f"Error: there's no huddle going on in <#{channel_id}> right now. You only start huddles "
                        "in the conversation you were asked in, so tell the user.")
            return _start(client, channel_id=channel_id, requester_id=requester_id, request_text=request_text)
        with _lock:
            busy = (channel_id, huddle_ts) in _sessions or (channel_id, huddle_ts) in _joining
        if busy:
            return "You're already in (or joining) that huddle."
        _expire_pending()
        if any(p["channel_id"] == channel_id and p["huddle_ts"] == huddle_ts for p in _pending.values()):
            return "You already asked that huddle; you join once someone there approves."
        pending_id = uuid.uuid4().hex
        text = (f"<@{requester_id}> asked coolton to join this huddle. Everything said, typed or shown in it "
                f"while coolton is there is processed under the <{POLICY_URL}|coolton policy>. Anyone can type "
                "`!kick-coolton` in this thread to make coolton leave right away, and it has to ask again "
                "before coming back. Let coolton join?")
        posted = client.chat_postMessage(channel=channel_id, thread_ts=huddle_ts, text=text, blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {"type": "actions", "elements": [
                {"type": "button", "action_id": "huddle_join_approve", "value": pending_id, "style": "primary",
                 "text": {"type": "plain_text", "text": "Let coolton join"}},
                {"type": "button", "action_id": "huddle_join_decline", "value": pending_id,
                 "text": {"type": "plain_text", "text": "Not now"}},
            ]},
        ])
    except slack_api.HuddleUnavailable as e:
        return f"Error: {e}."
    except Exception as e:
        logger.exception("Couldn't ask to join the huddle in %s", channel_id)
        return f"Error: couldn't ask to join the huddle: {e}"
    with _lock:
        _pending[pending_id] = {
            "channel_id": channel_id, "huddle_ts": huddle_ts, "requester_id": requester_id,
            "request_text": request_text, "message_ts": posted.get("ts"), "asked_at": time.time(),
        }
    return ("Asked the huddle (in its thread) to approve you joining; you join as soon as someone there "
            "approves. Tell the user that briefly.")


def _start(client, *, channel_id: str, requester_id: str, request_text: str) -> str:
    """No huddle is going in the channel: start one and join it. Nobody is in a call yet to
    approve coolton, so it starts right away; its first message in the huddle's thread tells
    whoever joins that the policy covers the huddle and how to kick it."""
    if channel_id.startswith("D"):
        return ("Error: you can't start a huddle in a DM (cooltonUser isn't in it). Ask the user to ask "
                "you in a channel, or to start the huddle themselves.")
    error = _join(client, {"channel_id": channel_id, "huddle_ts": None, "requester_id": requester_id,
                           "request_text": request_text}, requester_id, start=True)
    if error:
        return f"Error: {error}"
    return ("There was no huddle going in this channel, so you started one and joined it. From now on the "
            "huddle's voice agent talks in the call and answers the huddle's thread; tell the user briefly.")


def leave(channel_id: str, thread_ts: str | None, user_id: str) -> str:
    """Leave this conversation's huddle (agent.agent's leave_huddle_tool): the one this
    thread is, or else the one coolton is in in this channel. Also drops join requests
    still waiting in the channel."""
    with _lock:
        sessions = [s for (channel, _), s in _sessions.items() if channel == channel_id]
        dropped = [i for i, p in _pending.items() if p["channel_id"] == channel_id]
        for pending_id in dropped:
            del _pending[pending_id]
        joining = [key for key in _joining if key[0] == channel_id]
        for key in joining:
            _joining[key] = True  # _connect leaves once it's connected
    session = next((s for s in sessions if s.thread_ts == thread_ts), None) or (sessions[0] if sessions else None)
    if session is None:
        if joining:
            return "Left the huddle (you were still connecting to it)."
        if dropped:
            return "You weren't in a huddle here yet; cancelled your request to join."
        return "You're not in a huddle in this channel."
    asyncio.run_coroutine_threadsafe(session.stop(f"<@{user_id}> asked it to leave"), session.loop)
    return "Left the huddle."


def _expire_pending() -> None:
    with _lock:
        for pending_id in [i for i, p in _pending.items() if time.time() - p["asked_at"] > _APPROVAL_TTL]:
            del _pending[pending_id]


def _take_pending(pending_id: str) -> dict | None:
    _expire_pending()
    with _lock:
        return _pending.pop(pending_id, None)


def _settle(client, pending: dict, text: str) -> None:
    """Replace the approval prompt (and its buttons) with how it ended."""
    try:
        client.chat_update(channel=pending["channel_id"], ts=pending["message_ts"], text=text, blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        ])
    except Exception as e:
        logger.warning("Couldn't update the huddle approval prompt: %s", e)


def approve(client, pending_id: str, approver_id: str) -> None:
    """Someone pressed "Let coolton join" (listeners.actions.huddle_actions)."""
    from agent.ban_store import is_banned

    if is_banned(approver_id):
        return
    pending = _take_pending(pending_id)
    if pending is None:
        return
    with _lock:  # one approval answers every request for this huddle
        others = [_pending.pop(i) for i, p in list(_pending.items())
                  if p["channel_id"] == pending["channel_id"] and p["huddle_ts"] == pending["huddle_ts"]]
    for other in others:
        _settle(client, other, f"<@{approver_id}> already let coolton join this huddle.")
    _settle(client, pending, f"<@{approver_id}> let coolton join this huddle. Type `!kick-coolton` here "
                             "to make it leave.")
    error = _join(client, pending, approver_id)
    if error:
        client.chat_postMessage(channel=pending["channel_id"], thread_ts=pending["huddle_ts"], text=error)


def decline(client, pending_id: str, user_id: str) -> None:
    pending = _take_pending(pending_id)
    if pending is not None:
        _settle(client, pending, f"<@{user_id}> didn't let coolton join this huddle.")


def _join(client, pending: dict, approver_id: str, *, start: bool = False) -> str | None:
    """Join an approved huddle, or (`start`) start one in a channel that has none. Returns
    what went wrong, if anything. Only one join per huddle runs at a time (_joining)."""
    key = (pending["channel_id"], pending["huddle_ts"] or "starting")
    with _lock:
        if key in _sessions or key in _joining:
            return None  # already in it, or already on the way
        _joining[key] = False
    try:
        return _connect(client, pending, key, start)
    finally:
        with _lock:
            _joining.pop(key, None)


def _connect(client, pending: dict, key: tuple[str, str], start: bool) -> str | None:
    from agent.ensure_coolton_user import ensure_coolton_user_in_channel
    from agent.huddle import slack_api
    from agent.huddle.session import HuddleSession

    channel_id = pending["channel_id"]
    try:
        live = slack_api.find_live_huddle(channel_id, pending["huddle_ts"])
        if start and live:
            return "someone started a huddle here just now; ask again to join it."
        if not start and live != pending["huddle_ts"]:
            return "That huddle has ended, so coolton didn't join."
        ensure_coolton_user_in_channel(client, channel_id)
        joined = slack_api.join(channel_id)
    except slack_api.HuddleUnavailable as e:
        return f"coolton couldn't join: {e}."
    except Exception as e:
        logger.exception("Couldn't join the huddle in %s", channel_id)
        return f"coolton couldn't join the huddle: {e}"

    session = HuddleSession(joined, client=client, requester_id=pending["requester_id"],
                            request_text=pending["request_text"], on_closed=_forget, started=start)
    loop = session.loop = _event_loop()
    with _lock:
        if (session.channel_id, session.thread_ts) in _sessions:
            return None  # another join got there first: don't put a second coolton in the call
        _sessions[(session.channel_id, session.thread_ts)] = session
    started = asyncio.run_coroutine_threadsafe(session.start(), loop)
    try:
        started.result(timeout=_JOIN_TIMEOUT)
    except Exception as e:
        started.cancel()
        reason = str(e) or type(e).__name__
        asyncio.run_coroutine_threadsafe(session.stop(f"couldn't connect its audio ({reason})"), loop)
        _forget(session)
        logger.exception("Couldn't connect to the huddle in %s", channel_id)
        return f"coolton joined the huddle but couldn't connect its audio: {reason}"
    with _lock:
        kicked = _joining.get(key) or _joining.get((session.channel_id, session.thread_ts))
    if kicked:  # someone kicked coolton while it was still connecting
        asyncio.run_coroutine_threadsafe(session.stop("kicked while it was joining"), loop)
    return None


def kick(client, channel_id: str, thread_ts: str, user_id: str, text: str) -> bool:
    """`!kick-coolton` in a huddle's thread: leave that huddle right away, or drop a join
    request still waiting there. Returns whether the message was one that did either."""
    if not KICK_RE.match(text or ""):
        return False
    with _lock:
        dropped = [_pending.pop(i) for i, p in list(_pending.items())
                   if p["channel_id"] == channel_id and p["huddle_ts"] == thread_ts]
        joining = (channel_id, thread_ts) in _joining
        if joining:
            _joining[(channel_id, thread_ts)] = True  # _connect stops it once it's connected
    for pending in dropped:
        _settle(client, pending, f"<@{user_id}> kicked coolton, so it won't join this huddle.")
    session = session_for(channel_id, thread_ts)
    if session is not None:
        asyncio.run_coroutine_threadsafe(session.stop(f"<@{user_id}> kicked it"), session.loop)
    return bool(dropped) or joining or session is not None


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
    """Leave every huddle properly (posting why, saving the transcript): at exit, and
    before a restart's SIGTERM ends coolton (agent.shutdown)."""
    with _lock:
        sessions = list(_sessions.values())
    if _loop is None:
        return
    for session in sessions:
        try:
            asyncio.run_coroutine_threadsafe(session.stop("coolton is restarting"), _loop).result(timeout=10)
        except Exception:
            pass


from agent import shutdown  # noqa: E402

shutdown.on_sigterm(_leave_all)
