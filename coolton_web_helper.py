"""coolton web helper — file host, and coolton's anti-bot check for other tools.

Serves files coolton uploads (HTML embeds, screenshots) at /f/<name>. Uploading
needs the Bearer token; there's no way to host anything else here.

POST /antibot/check runs coolton's anti-bot check on one Slack message and answers
"flag", "watch" or "none", with the client categories behind it (like "ios": a kind
of real client known to trip the check), never the reasons. It needs a token from ANTIBOT_API_TOKENS (in
the server's .env, comma-separated so each tool can have its own).

With WEB_HELPER_PRIVATE_HOST set, routes from a deployment's private module, if any, are
served only on that host, and nothing else is served there: pages hosted at /f/ can't
share an origin with them.
"""

import asyncio
import hashlib
import hmac
import logging
import mimetypes
import os
import re
import time

import requests

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
import uvicorn

logger = logging.getLogger("coolton_web_helper")

app = FastAPI(title="coolton web helper")

BASE_URL = os.environ.get("WEB_HELPER_BASE_URL", "https://2390.proxy.tanjim.org")
FILES_DIR = os.environ.get("WEB_HELPER_FILES_DIR", "/home/tanjim/web64_files")
MAX_UPLOAD_BYTES = 10 * 1024**3  # 10 GB; bigger files go to bucky
TOKEN_FILE = os.environ.get("WEB_HELPER_TOKEN_FILE", os.environ.get("WEB64_TOKEN_FILE", "/home/tanjim/web64_token"))


def _api_key() -> str:
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def _authorized(request: Request) -> bool:
    """Constant-time Bearer-token check. If the token file is missing/unreadable/empty,
    _api_key() returns "" — the required header would then just be "Bearer ", which is
    trivially satisfiable by anyone. Fail closed instead: no configured key means no
    request is ever authorized, not that auth is skipped."""
    key = _api_key()
    if not key:
        return False
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    return hmac.compare_digest(auth[len("Bearer "):], key)


def _antibot_authorized(request: Request) -> bool:
    """A Bearer token from ANTIBOT_API_TOKENS. None configured: nobody gets in."""
    tokens = [t.strip() for t in os.environ.get("ANTIBOT_API_TOKENS", "").split(",") if t.strip()]
    auth = request.headers.get("Authorization", "")
    if not tokens or not auth.startswith("Bearer "):
        return False
    given = auth[len("Bearer "):]
    return any(hmac.compare_digest(given, token) for token in tokens)


_PERMALINK_RE = re.compile(r"https://[^/\s]+/archives/([A-Z0-9]+)/p(\d{10})(\d{6})")


def _message_from_permalink(link: str) -> dict:
    """The message a Slack permalink points to, read as cooltonUser."""
    match = _PERMALINK_RE.search(link or "")
    if not match:
        raise HTTPException(status_code=400, detail="Not a Slack message permalink")
    channel, ts = match.group(1), f"{match.group(2)}.{match.group(3)}"
    thread = re.search(r"[?&]thread_ts=(\d+\.\d+)", link)
    token = os.environ.get("SLACK_USER_TOKEN") or os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        raise HTTPException(status_code=503, detail="No Slack token is configured")

    headers = {"Authorization": f"Bearer {token}"}
    try:
        # history is the user-token-compatible way to read ordinary/public-channel
        # messages and parent messages, even when the user/bot is not in the channel.
        history_params = {"channel": channel, "oldest": ts, "latest": ts,
                          "inclusive": "true", "limit": 1}
        data = requests.get("https://slack.com/api/conversations.history", params=history_params,
                            headers=headers, timeout=15).json()
        message = next((m for m in data.get("messages") or [] if m.get("ts") == ts), None)

        # A reply is not normally returned by history. If the permalink names a
        # thread, fetch the thread and find the exact reply timestamp.
        if thread and not message:
            replies_params = {"channel": channel, "ts": thread.group(1),
                              "oldest": ts, "latest": ts, "inclusive": "true", "limit": 1000}
            data = requests.get("https://slack.com/api/conversations.replies", params=replies_params,
                                headers=headers, timeout=15).json()
            message = next((m for m in data.get("messages") or [] if m.get("ts") == ts), None)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail="Couldn't reach Slack") from e
    if not data.get("ok") or not message:
        raise HTTPException(status_code=404, detail="Couldn't read that message")
    return {**message, "channel": channel}


def _antibot_verdict(event: dict) -> dict:
    """{"verdict": "flag" | "watch" | "none", "categories": [...]} from coolton's private
    anti-bot check."""
    try:
        from private_hooks import verdict
    except ImportError as e:
        raise HTTPException(status_code=503, detail="The anti-bot check isn't installed here") from e
    return verdict(event)


def _antibot_event(call: dict) -> dict:
    """Turn one permalink or message call into the event passed to the anti-bot check."""
    if isinstance(call.get("permalink"), str):
        event = _message_from_permalink(call["permalink"])
    elif isinstance(call.get("message"), dict):
        event = dict(call["message"])
        if isinstance(call.get("channel"), str) and not event.get("channel"):
            event["channel"] = call["channel"]
    else:
        raise HTTPException(status_code=400, detail='Send "permalink" or "message"')
    event.setdefault("type", "message")
    return event


def _antibot_response(call: dict) -> dict:
    result = _antibot_verdict(_antibot_event(call))
    return {"verdict": result["verdict"], "categories": list(result.get("categories") or [])}


_ANTIBOT_BATCH_CONCURRENCY = 20


async def _antibot_batch_response(calls: list) -> dict:
    """Resolve/check batch items concurrently, preserving caller-visible order."""
    semaphore = asyncio.Semaphore(_ANTIBOT_BATCH_CONCURRENCY)

    async def run(call: dict) -> dict:
        async with semaphore:
            try:
                return await asyncio.to_thread(_antibot_response, call)
            except HTTPException as e:
                return {"error": {"status": e.status_code, "detail": e.detail}}

    responses = await asyncio.gather(*(run(call) for call in calls))
    return {"responses": responses}


@app.post("/antibot/check")
async def antibot_check(request: Request):
    """Check one call or a batch. A call is a {"permalink": ...} or
    {"message": ..., "channel"?: ...} object. Batch bodies return
    {"responses": [...]} in the same order as {"calls": [...]}."""
    if not _antibot_authorized(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        body = await request.json()
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Body must be JSON") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    if "calls" in body:
        calls = body["calls"]
        if not isinstance(calls, list):
            raise HTTPException(status_code=400, detail='"calls" must be a JSON array')
        if any(not isinstance(call, dict) for call in calls):
            raise HTTPException(status_code=400, detail='Every item in "calls" must be a JSON object')
        return await _antibot_batch_response(calls)

    return _antibot_response(body)


# Routes kept out of the public repo, served when their module is deployed alongside.
try:
    from private_hooks_web import router as _private_router
except ImportError:
    _private_router = None
if _private_router is not None:
    app.include_router(_private_router)

# Off unless set: it only works when that host's requests reach here with their own Host
# (or X-Forwarded-Host), which a relay that rewrites Host doesn't give.
PRIVATE_HOST = os.environ.get("WEB_HELPER_PRIVATE_HOST", "").lower()
_PRIVATE_PATHS = {route.path for route in getattr(_private_router, "routes", [])}


def _host(request: Request) -> str:
    """The host the browser asked for: a proxy's X-Forwarded-Host, else Host."""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return host.split(",")[0].strip().lower().split(":")[0]


@app.middleware("http")
async def _keep_private_routes_on_their_host(request: Request, call_next):
    if not PRIVATE_HOST:
        return await call_next(request)
    on_private_host = _host(request) == PRIVATE_HOST
    if (request.url.path in _PRIVATE_PATHS) != on_private_host:
        if request.url.path in _PRIVATE_PATHS:
            logger.info("Private route %s refused on host %r (Host %r)", request.url.path, _host(request),
                        request.headers.get("host"))
        return PlainTextResponse("Not found", status_code=404)
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <html>
        <body>
            <h1>coolton web helper</h1>
            <p>POST /upload with an <code>Authorization: Bearer &lt;token&gt;</code> header to host files.</p>
            <p>POST /antibot/check with an anti-bot API token and a Slack message (or its permalink)
            to get coolton's anti-bot verdict (flag, watch or none) and the client categories behind it.</p>
        </body>
    </html>
    """


@app.post("/upload")
async def upload(request: Request, filename: str = ""):
    if not _authorized(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large")

    os.makedirs(FILES_DIR, exist_ok=True)
    # Streamed to disk, never held in memory whole.
    partial = os.path.join(FILES_DIR, f".upload-{os.getpid()}-{time.time_ns()}")
    hasher, size = hashlib.sha256(), 0
    try:
        with open(partial, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="File too large")
                hasher.update(chunk)
                f.write(chunk)
        if not size:
            raise HTTPException(status_code=400, detail="Empty body")

        digest = hasher.hexdigest()[:16]
        ts = int(time.time())
        _, ext = os.path.splitext(filename)
        if ext and len(ext) <= 12:
            name = f"{digest}_{ts}{ext.lower()}"
        else:
            name = f"{digest}_{ts}"
        os.replace(partial, os.path.join(FILES_DIR, name))
    finally:
        if os.path.exists(partial):
            os.remove(partial)

    logger.info("Uploaded %s (%d bytes)", name, size)
    return {"url": f"{BASE_URL}/f/{name}", "path": f"/f/{name}"}


@app.api_route("/f/{name}", methods=["GET", "HEAD"])
async def serve_file(name: str):
    path = os.path.join(FILES_DIR, name)
    if not os.path.isfile(path) or not name or os.path.basename(name) != name:
        raise HTTPException(status_code=404, detail="Not found")
    media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=os.path.basename(name), content_disposition_type="inline")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Localhost only: Caddy serves it over TLS (2390.proxy.tanjim.org). Listening on every
    # interface also put it on the internet over plain HTTP, upload token and all.
    uvicorn.run(app, host="127.0.0.1", port=2389)
