"""Automatic OAuth capture for Slack bots coolton builds for other people
(agent.tools.slack_bot_deploy.create_slack_bot) — NOT coolton's own OAuth,
which is app_oauth.py/oauth_server.py, a separate install flow for coolton
itself.

create_slack_bot bakes this route's URL into the new app's
oauth_config.redirect_urls and signs a `state` token binding the install
attempt to that app (agent.tools.slack_bot_deploy.sign_install_state). When
the workspace admin finishes Slack's install dialog, Slack redirects here
with a `code` — exchanged via oauth.v2.access using that app's own
client_id/client_secret (already persisted at creation time), and the
resulting bot token is registered automatically. Nobody has to dig a token
out of the Slack UI and hand it back to coolton.
"""

from __future__ import annotations

import logging

import requests
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from agent.tools.slack_bot_deploy import get_bot_record, oauth_callback_url, register_bot_tokens, verify_install_state

logger = logging.getLogger(__name__)
router = APIRouter()

TOKEN_URL = "https://slack.com/api/oauth.v2.access"


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(f"<h3>{title}</h3><p>{body}</p>", status_code=status_code)


def _exchange_code(code: str, client_id: str, client_secret: str) -> dict:
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            # Must match exactly what create_slack_bot put in the /authorize
            # request — Slack rejects a mismatch.
            "redirect_uri": oauth_callback_url(),
        },
        timeout=30,
    )
    return resp.json()


@router.get("/bot-oauth/callback")
def bot_oauth_callback(code: str = "", state: str = ""):
    app_id = verify_install_state(state)
    if not app_id:
        return _page("Install link expired or invalid", "Ask coolton to generate a fresh install link.", 400)

    record = get_bot_record(app_id)
    if not record:
        return _page("Unknown app", f"No bot was created with id {app_id}.", 404)

    if not code:
        return _page("Missing code", "Slack didn't send an authorization code.", 400)

    creds = record.get("credentials", {})
    client_id = creds.get("client_id", "")
    client_secret = creds.get("client_secret", "")
    if not client_id or not client_secret:
        logger.error("Bot %s has no stored client credentials to exchange the OAuth code", app_id)
        return _page("Install failed", "coolton is missing this app's OAuth credentials.", 500)

    try:
        result = _exchange_code(code, client_id, client_secret)
    except Exception:
        logger.exception("oauth.v2.access request failed for bot %s", app_id)
        return _page("Install failed", "Couldn't reach Slack to exchange the code.", 502)

    if not result.get("ok"):
        logger.error("oauth.v2.access failed for bot %s: %s", app_id, result.get("error"))
        return _page("Install failed", f"Slack error: {result.get('error')}", 400)

    bot_token = result.get("access_token", "")
    if not bot_token.startswith("xoxb-"):
        logger.error("oauth.v2.access for bot %s returned no usable bot token", app_id)
        return _page("Install failed", "Slack didn't return a bot token.", 400)

    outcome = register_bot_tokens(app_id, bot_token)
    if outcome.startswith("Error"):
        logger.error("Failed to register captured token for bot %s: %s", app_id, outcome)
        return _page("Install failed", outcome, 500)

    logger.info("Auto-registered bot token for app %s via OAuth callback", app_id)
    return _page("App installed", "coolton picked this up automatically — you can go back to the conversation.")
