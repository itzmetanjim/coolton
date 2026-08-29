"""coolton web helper — file host + base64 HTML decoder + agent-browser stream proxy.

Serves uploaded files at /f/<name> and decodes base64url strings to HTML
at /<encoded>. Used by coolton to host sandbox outputs and rendered HTML.

Also reverse-proxies agent-browser's observability dashboard (a Next.js app
running inside the sandbox, reached via its E2B public host — see
agent/agent_browser_helpers.py) onto this same origin, so Slack never needs
another unfurl domain and the raw E2B sandbox host is never exposed to the
browser. See AgentBrowserProxy below.
"""

import asyncio
import base64
import hashlib
import logging
import mimetypes
import os
import re
import secrets
import threading
import time

import httpx
import uvicorn
import websockets
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

logger = logging.getLogger("coolton_web_helper")

app = FastAPI(title="coolton web helper")

BASE_URL = os.environ.get("WEB_HELPER_BASE_URL", "https://2390.proxy.tanjim.org")
FILES_DIR = os.environ.get("WEB_HELPER_FILES_DIR", "/home/tanjim/web64_files")
TOKEN_FILE = os.environ.get("WEB_HELPER_TOKEN_FILE", os.environ.get("WEB64_TOKEN_FILE", "/home/tanjim/web64_token"))


def _api_key() -> str:
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <html>
        <body>
            <h1>coolton web helper</h1>
            <p>Append a base64url-encoded string to the URL path to render it as HTML.</p>
            <p>POST /upload with an <code>Authorization: Bearer &lt;token&gt;</code> header to host files.</p>
        </body>
    </html>
    """


@app.post("/upload")
async def upload(request: Request, filename: str = ""):
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {_api_key()}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    content = await request.body()
    if not content:
        raise HTTPException(status_code=400, detail="Empty body")

    os.makedirs(FILES_DIR, exist_ok=True)

    digest = hashlib.sha256(content).hexdigest()[:16]
    ts = int(time.time())
    _, ext = os.path.splitext(filename)
    if ext and len(ext) <= 12:
        name = f"{digest}_{ts}{ext.lower()}"
    else:
        name = f"{digest}_{ts}"

    path = os.path.join(FILES_DIR, name)
    with open(path, "wb") as f:
        f.write(content)

    logger.info("Uploaded %s (%d bytes)", name, len(content))
    return {"url": f"{BASE_URL}/f/{name}", "path": f"/f/{name}"}


@app.api_route("/f/{name}", methods=["GET", "HEAD"])
async def serve_file(name: str):
    path = os.path.join(FILES_DIR, name)
    if not os.path.isfile(path) or not name or os.path.basename(name) != name:
        raise HTTPException(status_code=404, detail="Not found")
    media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=os.path.basename(name), content_disposition_type="inline")


# --- agent-browser dashboard stream proxy -----------------------------------
#
# The dashboard only binds 127.0.0.1 in the sandbox and uses root-absolute asset
# paths (/_next/..., /favicon.ico) baked into both HTML and inline JS, so a
# path-prefixed reverse proxy would 404 every asset and content-rewriting a
# third-party, versioned JS bundle would be fragile. Instead: a per-sandbox
# session token maps to the sandbox's public dashboard host; visiting /ab/<token>
# sets a cookie and redirects to /; AgentBrowserProxy (an ASGI middleware,
# wrapping everything below) transparently proxies any cookied request for the
# dashboard's known asset shape, and any cookied websocket unconditionally
# (this app has no websocket routes of its own to collide with).

_AB_COOKIE = "ab_session"
_AB_TTL_SECONDS = int(os.environ.get("AB_STREAM_TTL_SECONDS", "7200"))
_AB_UPSTREAM_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?\.e2b\.app$")
_AB_HTTP_PREFIXES = ("/_next/", "/favicon.ico")
_AB_HOP_BY_HOP_REQUEST = {"host", "connection", "content-length"}
_AB_HOP_BY_HOP_RESPONSE = {"connection", "transfer-encoding", "content-encoding", "content-length"}

_ab_lock = threading.Lock()
_ab_sessions: dict[str, tuple[str, float]] = {}
_ab_http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=False)


def _ab_store(token: str, upstream: str) -> None:
    with _ab_lock:
        _ab_sessions[token] = (upstream, time.time() + _AB_TTL_SECONDS)


def _ab_lookup(token: str | None) -> str | None:
    if not token:
        return None
    with _ab_lock:
        entry = _ab_sessions.get(token)
        if not entry:
            return None
        upstream, expires = entry
        if time.time() > expires:
            del _ab_sessions[token]
            return None
        return upstream


@app.post("/ab/register")
async def ab_register(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {_api_key()}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    upstream = str(body.get("upstream", ""))
    if not _AB_UPSTREAM_RE.match(upstream):
        raise HTTPException(status_code=400, detail="upstream must be a *.e2b.app host")

    token = secrets.token_urlsafe(24)
    _ab_store(token, upstream)
    logger.info("Registered agent-browser stream %s -> %s", token[:8], upstream)
    return {"url": f"{BASE_URL}/ab/{token}"}


@app.get("/ab/{token}")
async def ab_entry(token: str):
    upstream = _ab_lookup(token)
    if not upstream:
        raise HTTPException(status_code=404, detail="Not found or expired")
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        _AB_COOKIE, token, max_age=_AB_TTL_SECONDS,
        httponly=True, secure=True, samesite="lax", path="/",
    )
    return response


def _cookie_value(scope, name: str) -> str | None:
    for key, value in scope.get("headers") or []:
        if key == b"cookie":
            for part in value.decode("latin1").split(";"):
                k, _, v = part.strip().partition("=")
                if k == name:
                    return v
    return None


async def _ab_proxy_http(scope, receive, send, upstream: str) -> None:
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body"):
            break

    query = scope.get("query_string", b"").decode("latin1")
    url = f"https://{upstream}{scope['path']}"
    if query:
        url += f"?{query}"

    headers = {
        k.decode("latin1"): v.decode("latin1")
        for k, v in (scope.get("headers") or [])
        if k.decode("latin1").lower() not in _AB_HOP_BY_HOP_REQUEST
    }
    headers["host"] = upstream

    try:
        resp = await _ab_http_client.request(scope["method"], url, headers=headers, content=body or None)
    except httpx.HTTPError as e:
        logger.warning("agent-browser http proxy error: %s", e)
        await send({"type": "http.response.start", "status": 502, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": f"agent-browser proxy error: {e}".encode()})
        return

    resp_headers = [
        (k.encode("latin1"), v.encode("latin1"))
        for k, v in resp.headers.items()
        if k.lower() not in _AB_HOP_BY_HOP_RESPONSE
    ]
    resp_headers.append((b"content-length", str(len(resp.content)).encode("latin1")))
    await send({"type": "http.response.start", "status": resp.status_code, "headers": resp_headers})
    await send({"type": "http.response.body", "body": resp.content})


async def _ab_proxy_ws(scope, receive, send, upstream: str) -> None:
    message = await receive()
    if message["type"] != "websocket.connect":
        return

    query = scope.get("query_string", b"").decode("latin1")
    url = f"wss://{upstream}{scope['path']}"
    if query:
        url += f"?{query}"

    subprotocols = None
    for k, v in scope.get("headers") or []:
        if k == b"sec-websocket-protocol":
            subprotocols = [p.strip() for p in v.decode("latin1").split(",")]

    try:
        async with websockets.connect(url, subprotocols=subprotocols, open_timeout=15) as upstream_ws:
            await send({"type": "websocket.accept", "subprotocol": upstream_ws.subprotocol})

            async def client_to_upstream():
                while True:
                    msg = await receive()
                    if msg["type"] == "websocket.disconnect":
                        await upstream_ws.close()
                        return
                    if msg.get("bytes") is not None:
                        await upstream_ws.send(msg["bytes"])
                    elif msg.get("text") is not None:
                        await upstream_ws.send(msg["text"])

            async def upstream_to_client():
                async for msg in upstream_ws:
                    if isinstance(msg, str):
                        await send({"type": "websocket.send", "text": msg})
                    else:
                        await send({"type": "websocket.send", "bytes": msg})
                await send({"type": "websocket.close"})

            tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
    except Exception as e:
        logger.warning("agent-browser ws proxy error: %s", e)
        try:
            await send({"type": "websocket.close", "code": 1011})
        except Exception:
            pass


class AgentBrowserProxy:
    """ASGI middleware: proxies the agent-browser dashboard onto this origin for
    any request carrying a valid ab_session cookie (set by GET /ab/<token>).

    Runs before FastAPI's router so it can transparently own root-absolute asset
    paths. The http case only intercepts a small, concrete allowlist (the
    dashboard's actual asset shape) so it can never shadow /upload, /f/<name>,
    or a base64 HTML embed even for a browser that still carries a stale cookie.
    The websocket case has no routes of ours to collide with, so any cookied
    websocket is proxied unconditionally.
    """

    def __init__(self, inner_app):
        self.app = inner_app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if scope["type"] == "http" and path != "/" and not path.startswith(_AB_HTTP_PREFIXES):
            await self.app(scope, receive, send)
            return

        upstream = _ab_lookup(_cookie_value(scope, _AB_COOKIE))
        if not upstream:
            await self.app(scope, receive, send)
            return

        if scope["type"] == "http":
            await _ab_proxy_http(scope, receive, send, upstream)
        else:
            await _ab_proxy_ws(scope, receive, send, upstream)


# -----------------------------------------------------------------------------


@app.get("/{encoded_str}", response_class=HTMLResponse)
async def decode_base64_html(encoded_str: str):
    try:
        padding_needed = len(encoded_str) % 4
        if padding_needed:
            encoded_str += "=" * (4 - padding_needed)
        decoded_bytes = base64.urlsafe_b64decode(encoded_str)
        return HTMLResponse(content=decoded_bytes.decode("utf-8", errors="replace"))
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Failed to decode base64url string. Please ensure it is properly encoded.",
        )


asgi_app = AgentBrowserProxy(app)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(asgi_app, host="0.0.0.0", port=2389)
