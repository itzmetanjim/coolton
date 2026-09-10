"""HuddleFM bot API — control a HuddleFM listening session (add/skip/pause/
volume/etc) by DMing JSON commands to the HuddleFM Slack user and reading its
threaded JSON reply. Spec: https://github.com/ingoau/huddlefm/blob/main/docs/bot-api.md

coolton is allowlisted on HuddleFM's side (INTEGRATION_USER_IDS) under its own
bot user id, so every request here is sent from the bot's own identity
(deps.client / SLACK_BOT_TOKEN) — no user token needed.

Two request shapes, both single top-level DMs whose reply comes back
threaded under the command's own ts:
  - request_control: no immediate success reply — the host approves/declines
    from an ephemeral Slack prompt, and that can take up to 5 minutes. We
    still poll briefly to surface an immediate error (e.g. session_not_found)
    but do NOT block for the full grant window; the caller has to try a real
    command again once the host has responded.
  - every other command type: replies promptly once a grant already covers
    the required permission, so a short poll is enough.
"""

from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

_REQUEST_CONTROL_POLL_SECONDS = 8.0
_COMMAND_POLL_SECONDS = 12.0
_POLL_INTERVAL_SECONDS = 1.5


def _huddlefm_user_id() -> str | None:
    return os.environ.get("HUDDLEFM_USER_ID") or None


def _dm_channel(client) -> tuple[str | None, str | None]:
    """Open (or reuse) the DM with the HuddleFM user. Returns (channel_id, error)."""
    huddlefm_user_id = _huddlefm_user_id()
    if not huddlefm_user_id:
        return None, "Error: HUDDLEFM_USER_ID not configured."
    try:
        resp = client.conversations_open(users=huddlefm_user_id)
    except Exception as e:
        return None, f"Error opening a DM with HuddleFM: {e}"
    if not resp.get("ok"):
        return None, f"Error opening a DM with HuddleFM: {resp.get('error')}"
    channel_id = (resp.get("channel") or {}).get("id")
    if not channel_id:
        return None, "Error: HuddleFM DM channel had no id."
    return channel_id, None


def _send(client, payload: dict) -> tuple[str | None, str | None, str | None]:
    """Post `payload` as JSON text into the HuddleFM DM. Returns
    (dm_channel_id, sent_ts, error)."""
    dm_channel_id, error = _dm_channel(client)
    if error:
        return None, None, error
    try:
        resp = client.chat_postMessage(channel=dm_channel_id, text=json.dumps(payload))
    except Exception as e:
        return None, None, f"Error sending command to HuddleFM: {e}"
    if not resp.get("ok"):
        return None, None, f"Error sending command to HuddleFM: {resp.get('error')}"
    return dm_channel_id, str(resp.get("ts")), None


def _poll_reply(client, dm_channel_id: str, sent_ts: str, timeout: float) -> dict | None:
    """Poll the DM thread under `sent_ts` for HuddleFM's JSON reply. Returns
    the parsed reply, or None if nothing arrived within `timeout`."""
    huddlefm_user_id = _huddlefm_user_id()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = client.conversations_replies(channel=dm_channel_id, ts=sent_ts)
        except Exception:
            logger.exception("Failed polling HuddleFM DM %s for a reply to %s", dm_channel_id, sent_ts)
            return None
        if resp.get("ok"):
            for msg in resp.get("messages", [])[1:]:  # [0] is our own sent message
                if huddlefm_user_id and msg.get("user") not in (huddlefm_user_id, None):
                    continue
                try:
                    parsed = json.loads(msg.get("text", ""))
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(parsed, dict):
                    return parsed
        time.sleep(_POLL_INTERVAL_SECONDS)
    return None


def _format_reply(reply: dict | None, pending_note: str) -> str:
    if reply is None:
        return pending_note
    return json.dumps(reply)


def request_control(client, channel: str, permissions: str, events: str = "") -> str:
    """Send a request_control DM. `permissions`/`events` are comma-separated
    lists (readable, not JSON — this is the model-facing entry point)."""
    if not channel:
        return "Error: channel is required (the huddle source channel, controls channel, or companion channel)."
    perm_list = [p.strip() for p in permissions.split(",") if p.strip()]
    if not perm_list:
        return "Error: at least one permission is required."
    event_list = [e.strip() for e in events.split(",") if e.strip()] if events else []

    payload = {
        "v": 1, "type": "request_control", "channel": channel,
        "permissions": perm_list, "events": event_list,
    }
    dm_channel_id, sent_ts, error = _send(client, payload)
    if error or dm_channel_id is None or sent_ts is None:
        return error or "Error: HuddleFM DM send failed with no error message."

    reply = _poll_reply(client, dm_channel_id, sent_ts, _REQUEST_CONTROL_POLL_SECONDS)
    if reply is not None:
        # Only an immediate error looks like this — a real grant_accepted/
        # declined/expired only arrives after the host acts, well past this
        # short poll window.
        return _format_reply(reply, "")
    return (
        f"Requested control of {channel} ({', '.join(perm_list)}). No immediate error — "
        "the host now has an approval prompt in Slack. This can take up to 5 minutes "
        "and there's no way to wait for it here; ask again once the host has approved "
        "(if a command is tried before that, it will fail with not_granted)."
    )


def send_command(client, command_type: str, channel: str = "", fields: dict | None = None) -> str:
    """Send any HuddleFM command DM and return its threaded JSON reply."""
    if not command_type:
        return "Error: command_type is required."
    payload: dict = {"v": 1, "type": command_type}
    if channel:
        payload["channel"] = channel
    if fields:
        payload.update(fields)

    dm_channel_id, sent_ts, error = _send(client, payload)
    if error or dm_channel_id is None or sent_ts is None:
        return error or "Error: HuddleFM DM send failed with no error message."

    reply = _poll_reply(client, dm_channel_id, sent_ts, _COMMAND_POLL_SECONDS)
    return _format_reply(
        reply,
        f"No reply from HuddleFM within {_COMMAND_POLL_SECONDS:.0f}s for `{command_type}` "
        "— it may still be processing, or the DM/grant may be missing.",
    )
