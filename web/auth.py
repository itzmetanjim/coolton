"""Hack Club Auth (auth.hackclub.com) sign-in for coolton's web UI.

The registered app requests exactly one scope: `slack_id`. That's enough —
coolton already knows how to turn a Slack user id into everything else (display
name, avatar) via the real Slack API, since a web conversation's `user_id` is
the same real Slack user id Slack itself would give it (see agent.platforms.web).

Endpoints (verified against https://auth.hackclub.com/docs/oauth-guide and the
hackclub/omniauth-hack_club strategy source, since the auth-guide page itself
doesn't document a dedicated userinfo endpoint):
  - authorize: GET  https://auth.hackclub.com/oauth/authorize
  - token:     POST https://auth.hackclub.com/oauth/token
  - userinfo:  GET  https://auth.hackclub.com/api/v1/me  (Bearer access_token)

CSRF is handled with a standard double-submit cookie: /oauth/login sets a random
`oauth_state` cookie and puts the same value in the `state` param; /oauth/callback
requires them to match before exchanging the code.

The session itself is a signed, stateless cookie (HMAC-SHA256 over
{slack_id, issued_at} with COOLTON_WEB_SECRET) — no server-side session store,
so it survives a restart and needs no cleanup job. hmac.compare_digest is used
throughout for the same reason coolton_web_helper.py's Bearer check uses it:
timing-safe comparison of anything secret-derived.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time

import requests
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

logger = logging.getLogger(__name__)

router = APIRouter()

AUTHORIZE_URL = "https://auth.hackclub.com/oauth/authorize"
TOKEN_URL = "https://auth.hackclub.com/oauth/token"
USERINFO_URL = "https://auth.hackclub.com/api/v1/me"

SESSION_COOKIE = "coolton_session"
STATE_COOKIE = "oauth_state"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 3600  # 30 days


def _client_id() -> str:
    return os.environ.get("HCA_CLIENT_ID", "")


def _client_secret() -> str:
    return os.environ.get("HCA_CLIENT_SECRET", "")


def _redirect_uri() -> str:
    return os.environ.get("HCA_REDIRECT_URI", "http://localhost:8000/oauth/callback")


def _secure_cookies() -> bool:
    # A browser silently drops a `Secure` cookie sent over plain HTTP, which is
    # exactly the local-dev case (http://localhost:8000) — derive this from the
    # registered redirect URI instead of a separate env var to set.
    return _redirect_uri().startswith("https://")


def _secret() -> bytes:
    key = os.environ.get("COOLTON_WEB_SECRET", "")
    return key.encode()


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _sign(payload: dict) -> str:
    """Encode + HMAC-sign a payload dict into a `<payload>.<sig>` cookie value."""
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def _verify(token: str) -> dict | None:
    """Verify a `_sign`-produced token, returning its payload or None if invalid."""
    if not _secret():
        # Fail closed: no configured signing key means no session is ever valid,
        # not that verification is skipped (same posture as coolton_web_helper's
        # _authorized when its token file is empty/missing).
        return None
    try:
        body, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    expected_sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None
    try:
        return json.loads(_b64decode(body))
    except Exception:
        return None


def create_session_token(slack_id: str) -> str:
    return _sign({"slack_id": slack_id, "issued_at": time.time()})


def get_session(request: Request) -> dict | None:
    """The signed-in user's session payload ({slack_id, issued_at}), or None."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    payload = _verify(token)
    if not payload:
        return None
    if time.time() - payload.get("issued_at", 0) > SESSION_MAX_AGE_SECONDS:
        return None
    if not payload.get("slack_id"):
        return None
    return payload


def require_slack_id(request: Request) -> str | None:
    """The signed-in user's slack_id, or None if not signed in — routes check
    this themselves (rather than raising) so they can return a clean 401 JSON
    body instead of FastAPI's default HTML error page."""
    session = get_session(request)
    return session["slack_id"] if session else None


def _authorize_url(state: str) -> str:
    from urllib.parse import urlencode

    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "slack_id",
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _exchange_code(code: str) -> dict:
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri": _redirect_uri(),
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _fetch_slack_id(access_token: str) -> str | None:
    resp = requests.get(
        USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    logger.info("HCA /api/v1/me raw response: %s", data)
    # The strategy's own raw_info hash nests fields directly; be tolerant of an
    # extra "user"/"data" wrapper, or a differently-cased/named key, since the
    # actual response shape isn't pinned down in the public docs.
    for container in (data, data.get("user") or {}, data.get("data") or {}, data.get("identity") or {}):
        for key in ("slack_id", "slack_uid", "slackId", "SlackId"):
            if container.get(key):
                return container[key]
    return None


@router.get("/oauth/login")
def login():
    state = secrets.token_urlsafe(24)
    response = RedirectResponse(_authorize_url(state), status_code=302)
    response.set_cookie(
        STATE_COOKIE, state, max_age=600, httponly=True, secure=_secure_cookies(), samesite="lax",
    )
    return response


@router.get("/oauth/callback")
def callback(request: Request, code: str = "", state: str = ""):
    expected_state = request.cookies.get(STATE_COOKIE, "")
    if not code or not state or not expected_state or not hmac.compare_digest(state, expected_state):
        logger.warning("HCA callback rejected: missing/mismatched state")
        return RedirectResponse("/?auth_error=state", status_code=302)

    try:
        token_data = _exchange_code(code)
        access_token = token_data["access_token"]
        slack_id = _fetch_slack_id(access_token)
    except Exception:
        logger.exception("HCA sign-in failed")
        return RedirectResponse("/?auth_error=exchange", status_code=302)

    if not slack_id:
        logger.warning("HCA sign-in succeeded but returned no slack_id")
        return RedirectResponse("/?auth_error=no_slack_id", status_code=302)

    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(STATE_COOKIE)
    response.set_cookie(
        SESSION_COOKIE, create_session_token(slack_id),
        max_age=SESSION_MAX_AGE_SECONDS, httponly=True, secure=_secure_cookies(), samesite="lax",
    )
    return response


@router.get("/oauth/logout")
def logout():
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response
