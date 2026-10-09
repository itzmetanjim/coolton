"""Calls to Slack's code channel API (agents.conversations.*, agents.sessions.*) as the
"coolton code channels" app.

Slack only lets a code channel's agent bot manage it, and that's the code channels app's
bot (its token in SLACK_CODE_CHANNEL_BOT_TOKEN; manifests/code_channel_app.json), not
coolton's own: coolton's app can't get the code_channels:manage scope. So everything
here (creating channels, their tabs, context bar, canvas, slash commands, session status,
archiving) goes through that app, while coolton's own bot does the talking.
"""
from __future__ import annotations

import logging
import os

from slack_sdk import WebClient

logger = logging.getLogger(__name__)


def app_client() -> WebClient | None:
    """The code channels app's bot client, or None if its token isn't configured."""
    token = os.environ.get("SLACK_CODE_CHANNEL_BOT_TOKEN")
    return WebClient(token=token) if token else None


def call(method: str, **params) -> dict:
    """Call `method` as the code channels app. Never raises: returns Slack's response,
    or {"ok": False, "error": ...}."""
    client = app_client()
    if client is None:
        return {"ok": False, "error": "not_configured"}
    try:
        data = client.api_call(method, json=params).data
        return data if isinstance(data, dict) else {"ok": False, "error": "unexpected_response"}
    except Exception as e:
        data = getattr(getattr(e, "response", None), "data", None)
        if isinstance(data, dict):
            return data
        logger.warning("%s failed: %s", method, e)
        return {"ok": False, "error": str(e)}


def error_text(response: dict) -> str:
    """A refused call as a message for the model (Slack's error and any warnings)."""
    error = response.get("error") or "unknown_error"
    if error == "not_configured":
        return "Error: code channels aren't set up (no SLACK_CODE_CHANNEL_BOT_TOKEN configured)."
    detail = response.get("response_metadata", {}).get("messages") or []
    return f"Error: Slack refused it ({error})" + (f": {'; '.join(detail)}" if detail else "")
