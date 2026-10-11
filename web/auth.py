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

The session itself is a signed, stateless cookie ({slack_id, issued_at}, signed
by agent.signing) — no server-side session store, so it survives a restart and
needs no cleanup job.

Over HTTPS both cookies carry the `__Host-` prefix, which browsers only accept when
the site's own host set it (Secure, Path=/, no Domain). Other *.tanjim.org hosts
serve pages anyone can write (HTML embeds), and without the prefix such a page could
set a Domain=tanjim.org cookie of the same name: logging a visitor into someone
else's session, or fixing the OAuth state.
"""

from __future__ import annotations

import hmac
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


def _redirect_uris() -> list[str]:
    """Every callback URL registered with Hack Club Auth for this client, as a
    comma-separated HCA_REDIRECT_URI (e.g. coolton is reachable at both
    coolton.tanjim.org and coolton.lily.hackclub.app, each independently
    registered) — see _redirect_uri, which picks the one matching the current
    request. A single value (or the unset default) still works exactly as
    before this supported more than one.
    """
    raw = os.environ.get("HCA_REDIRECT_URI", "http://localhost:8000/oauth/callback")
    return [u.strip() for u in raw.split(",") if u.strip()]


def _request_host(request: Request | None) -> str:
    """The hostname the browser is actually on.

    coolton is reachable through a chain (a discovery-based relay proxy on
    another box, then Caddy) that resolves the ultimate upstream by its OWN
    hostname (e.g. "tanjim.org") and forwards THAT as Host. The relay also
    sets X-Forwarded-Host to the true original external hostname (e.g.
    coolton.tanjim.org vs coolton.lily.hackclub.app) - but Caddy's own
    reverse_proxy recomputes and overwrites X-Forwarded-Host on its next hop
    rather than trusting one from an upstream it has no reason to trust, so
    that value never survives to here either. X-Lily-Forwarded-Host is the
    relay's own custom header carrying the same information under a name
    Caddy doesn't recognize as one of its "forwarded" headers, so it passes
    through untouched - prefer it, then fall back to the (Caddy-rewritten,
    only reliable for a direct, non-relayed hit) X-Forwarded-Host, then Host.
    """
    if request is None:
        return ""
    host = (
        request.headers.get("x-lily-forwarded-host")
        or request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or ""
    )
    return host.split(":")[0].lower()


def _redirect_uri(request: Request | None = None) -> str:
    """The registered HCA callback URL to use for this request — whichever
    configured one (see _redirect_uris) matches the host the browser is
    actually on, so /oauth/login and /oauth/callback always round-trip
    through the SAME hostname (HCA rejects a token exchange whose redirect_uri
    doesn't match the one the code was issued for). Falls back to the first
    configured URL when there's no request or its host matches none of them.
    """
    from urllib.parse import urlparse

    uris = _redirect_uris()
    host = _request_host(request)
    if host:
        for uri in uris:
            if urlparse(uri).hostname == host:
                return uri
    return uris[0]


def _secure_cookies(redirect_uri: str) -> bool:
    # A browser silently drops a `Secure` cookie sent over plain HTTP, which is
    # exactly the local-dev case (http://localhost:8000) — derive this from the
    # registered redirect URI instead of a separate env var to set.
    return redirect_uri.startswith("https://")


def _cookie_name(base: str, secure: bool) -> str:
    """`base` with the `__Host-` prefix over HTTPS (see the module docstring); plain
    HTTP (local dev) can't set a prefixed cookie, so it keeps the bare name."""
    return f"__Host-{base}" if secure else base


def _request_cookie_name(base: str, request: Request) -> str:
    return _cookie_name(base, _secure_cookies(_redirect_uri(request)))


def _sign(payload: dict) -> str:
    """A signed session token (agent.signing) for `payload`."""
    from agent.signing import sign

    return sign("web-session", payload)


def _verify(token: str) -> dict | None:
    """The payload of a session token `_sign` made, or None if it isn't one."""
    from agent.signing import verify

    return verify("web-session", token)


def create_session_token(slack_id: str) -> str:
    return _sign({"slack_id": slack_id, "issued_at": time.time()})


def get_session(request: Request) -> dict | None:
    """The signed-in user's session payload ({slack_id, issued_at}), or None."""
    token = request.cookies.get(_request_cookie_name(SESSION_COOKIE, request))
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


def _authorize_url(state: str, redirect_uri: str) -> str:
    from urllib.parse import urlencode

    params = {
        "client_id": _client_id(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "slack_id",
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _exchange_code(code: str, redirect_uri: str) -> dict:
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri": redirect_uri,
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
    # Just its shape: the values are the person's profile.
    logger.info("HCA /api/v1/me response keys: %s", sorted(data) if isinstance(data, dict) else type(data).__name__)
    # The strategy's own raw_info hash nests fields directly; be tolerant of an
    # extra "user"/"data" wrapper, or a differently-cased/named key, since the
    # actual response shape isn't pinned down in the public docs.
    for container in (data, data.get("user") or {}, data.get("data") or {}, data.get("identity") or {}):
        for key in ("slack_id", "slack_uid", "slackId", "SlackId"):
            if container.get(key):
                return container[key]
    return None


@router.get("/oauth/login")
def login(request: Request):
    redirect_uri = _redirect_uri(request)
    state = secrets.token_urlsafe(24)
    response = RedirectResponse(_authorize_url(state, redirect_uri), status_code=302)
    secure = _secure_cookies(redirect_uri)
    response.set_cookie(
        _cookie_name(STATE_COOKIE, secure), state, max_age=600, path="/", httponly=True, secure=secure,
        samesite="lax",
    )
    return response


@router.get("/oauth/callback")
def callback(request: Request, code: str = "", state: str = ""):
    expected_state = request.cookies.get(_request_cookie_name(STATE_COOKIE, request), "")
    if not code or not state or not expected_state or not hmac.compare_digest(state, expected_state):
        logger.warning("HCA callback rejected: missing/mismatched state")
        return RedirectResponse("/?auth_error=state", status_code=302)

    redirect_uri = _redirect_uri(request)
    try:
        token_data = _exchange_code(code, redirect_uri)
        access_token = token_data["access_token"]
        slack_id = _fetch_slack_id(access_token)
    except Exception:
        logger.exception("HCA sign-in failed")
        return RedirectResponse("/?auth_error=exchange", status_code=302)

    if not slack_id:
        logger.warning("HCA sign-in succeeded but returned no slack_id")
        return RedirectResponse("/?auth_error=no_slack_id", status_code=302)

    response = RedirectResponse("/", status_code=302)
    secure = _secure_cookies(redirect_uri)
    response.delete_cookie(_cookie_name(STATE_COOKIE, secure), path="/", secure=secure, httponly=True)
    response.set_cookie(
        _cookie_name(SESSION_COOKIE, secure), create_session_token(slack_id),
        max_age=SESSION_MAX_AGE_SECONDS, path="/", httponly=True, secure=secure, samesite="lax",
    )
    return response


@router.get("/oauth/logout")
def logout(request: Request):
    # Not "/" — the frontend calls /api/me on load and bounces any 401 straight
    # to /oauth/login, and Hack Club Auth itself stays signed in across that
    # round trip (it's a separate SSO session coolton has no way to end), so
    # landing on "/" re-authorizes silently and undoes the sign-out before the
    # user ever sees it. "/?signed_out=1" tells the frontend to show a plain
    # "signed out" screen instead of calling any authenticated endpoint.
    response = RedirectResponse("/?signed_out=1", status_code=302)
    secure = _secure_cookies(_redirect_uri(request))
    response.delete_cookie(_cookie_name(SESSION_COOKIE, secure), path="/", secure=secure, httponly=True)
    return response
