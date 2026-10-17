"""Finding and joining a Slack huddle as cooltonUser.

Bots can't be in a huddle, so coolton joins as its helper account (COOLTON_USER_ID), the
way the Slack client does: with that account's browser session, the `xoxc-` token the web
client uses (COOLTON_USER_XOXC) and its `d` cookie (COOLTON_USER_XOXD). These are Slack's
own client endpoints, not its public API. `rooms.join` hands back the huddle's Amazon
Chime meeting and attendee, which agent.huddle.chime connects to.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 15
# Where Chime puts the media for a huddle rooms.join has to start; an existing huddle
# keeps its own region.
_MEDIA_REGION = "us-east-1"
# How far back in a channel to look for a huddle that's still going.
_HISTORY_LIMIT = 50


class HuddleUnavailable(RuntimeError):
    """Joining can't work: missing credentials, or no live huddle to join."""


@dataclass
class JoinedHuddle:
    call_id: str
    channel_id: str
    # The huddle's thread: its chat, and the conversation coolton's tools work in.
    thread_ts: str
    participant_ids: list[str]
    meeting: dict
    attendee: dict


def _session() -> tuple[str, str]:
    xoxc = os.environ.get("COOLTON_USER_XOXC", "")
    xoxd = os.environ.get("COOLTON_USER_XOXD", "")
    if not (xoxc and xoxd):
        raise HuddleUnavailable(
            "huddles aren't set up: COOLTON_USER_XOXC and COOLTON_USER_XOXD (cooltonUser's "
            "browser session) are missing from the server's .env"
        )
    return xoxc, xoxd


def _post(base_url: str, method: str, fields: dict) -> dict:
    xoxc, xoxd = _session()
    response = requests.post(
        f"{base_url.rstrip('/')}/api/{method}",
        data={"token": xoxc, **fields},
        headers={"Cookie": f"d={xoxd}"},
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"{method}: unexpected response")
    return data


_workspace_url: str | None = None


def workspace_url() -> str:
    """The workspace's own URL, which the client endpoints live under. Also checks the
    browser session really is cooltonUser's, so coolton never joins as someone else."""
    global _workspace_url
    if _workspace_url:
        return _workspace_url
    auth = _post("https://slack.com", "auth.test", {})
    if not auth.get("ok"):
        raise HuddleUnavailable(f"cooltonUser's browser session was refused: {auth.get('error')}")
    expected = os.environ.get("COOLTON_USER_ID", "")
    if expected and auth.get("user_id") != expected:
        raise HuddleUnavailable("COOLTON_USER_XOXC belongs to another account than COOLTON_USER_ID")
    _workspace_url = auth.get("url") or "https://slack.com"
    return _workspace_url


def _call(method: str, fields: dict) -> dict:
    return _post(workspace_url(), method, fields)


def live_call_id(message: dict) -> str | None:
    """The huddle a huddle thread's root message belongs to, while it's still going."""
    if message.get("subtype") != "huddle_thread":
        return None
    room = message.get("room")
    if not isinstance(room, dict) or room.get("has_ended") or float(room.get("date_end") or 0) > 0:
        return None
    return room.get("id") or None


def find_live_huddle(channel_id: str, thread_ts: str | None) -> str | None:
    """The thread_ts of the live huddle for a request made in `thread_ts`: that thread
    when it's a huddle's own thread, otherwise the channel's latest live huddle."""
    if thread_ts:
        replies = _call("conversations.replies", {
            "channel": channel_id, "ts": thread_ts, "limit": "1", "inclusive": "true",
        })
        root = next((m for m in replies.get("messages") or [] if m.get("ts") == thread_ts), None)
        if root and live_call_id(root):
            return thread_ts
    history = _call("conversations.history", {"channel": channel_id, "limit": str(_HISTORY_LIMIT)})
    for message in history.get("messages") or []:
        if live_call_id(message):
            return message["ts"]
    return None


def _participant_ids(value) -> list[str]:
    ids = []
    for entry in value or []:
        uid = entry if isinstance(entry, str) else (entry or {}).get("user_id")
        if isinstance(uid, str):
            ids.append(uid)
    return ids


def parse_join_response(data: dict) -> JoinedHuddle:
    if not data.get("ok"):
        raise RuntimeError(f"rooms.join failed: {data.get('error', 'unknown error')}")
    call = data.get("call") or {}
    free_willy = call.get("free_willy") or {}
    canvas = data.get("canvas") or {}
    huddle = data.get("huddle") or {}
    meeting = dict(free_willy.get("meeting") or {})
    if meeting.get("MeetingFeatures") is None:
        meeting.pop("MeetingFeatures", None)
    joined = JoinedHuddle(
        call_id=call.get("call_id") or "",
        channel_id=canvas.get("thread_channel_id") or "",
        thread_ts=canvas.get("root_thread_ts") or "",
        participant_ids=_participant_ids(huddle.get("participants")),
        meeting=meeting,
        attendee=dict(free_willy.get("attendee") or {}),
    )
    media = joined.meeting.get("MediaPlacement") or {}
    if not (joined.call_id and joined.channel_id and joined.thread_ts and media.get("SignalingUrl")
            and joined.attendee.get("JoinToken")):
        raise RuntimeError("rooms.join answered without a huddle to connect to")
    return joined


def join(channel_id: str) -> JoinedHuddle:
    """Join the channel's huddle as cooltonUser. Starts one if none is going, so check
    find_live_huddle first."""
    data = _call("rooms.join", {
        "channel_id": channel_id, "regions": _MEDIA_REGION, "multidevice": "true",
    })
    return parse_join_response(data)
