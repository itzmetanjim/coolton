"""coolton web helper — file host, and coolton's anti-bot check for other tools.

Serves files coolton uploads (HTML embeds, screenshots) at /f/<name>. Uploading
needs the Bearer token; there's no way to host anything else here.

POST /antibot/check runs coolton's anti-bot check on one Slack message and answers
"flag", "watch" or "none", never why. It needs a token from ANTIBOT_API_TOKENS (in
the server's .env, comma-separated so each tool can have its own).
"""

import hashlib
import hmac
import logging
import mimetypes
import os
import re
import time

import requests

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
import uvicorn

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
    params = {"channel": channel, "ts": thread.group(1) if thread else ts,
              "oldest": ts, "latest": ts, "inclusive": "true", "limit": 1}
    try:
        data = requests.get("https://slack.com/api/conversations.replies", params=params,
                            headers={"Authorization": f"Bearer {token}"}, timeout=15).json()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail="Couldn't reach Slack") from e
    message = next((m for m in data.get("messages") or [] if m.get("ts") == ts), None)
    if not data.get("ok") or not message:
        raise HTTPException(status_code=404, detail="Couldn't read that message")
    return {**message, "channel": channel}


def _antibot_verdict(event: dict) -> str:
    """"flag", "watch" or "none" from coolton's private anti-bot check."""
    try:
        from private_hooks import verdict
    except ImportError as e:
        raise HTTPException(status_code=503, detail="The anti-bot check isn't installed here") from e
    return verdict(event)


@app.post("/antibot/check")
async def antibot_check(request: Request):
    """Body: {"permalink": "<Slack message link>"} or {"message": {<Slack message>}, "channel"?:
    "<its channel id>"}. Answers {"verdict": "flag" | "watch" | "none"}."""
    if not _antibot_authorized(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        body = await request.json()
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Body must be JSON") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    if isinstance(body.get("permalink"), str):
        event = _message_from_permalink(body["permalink"])
    elif isinstance(body.get("message"), dict):
        event = dict(body["message"])
        if isinstance(body.get("channel"), str) and not event.get("channel"):
            event["channel"] = body["channel"]
    else:
        raise HTTPException(status_code=400, detail='Send "permalink" or "message"')
    event.setdefault("type", "message")
    return {"verdict": _antibot_verdict(event)}


@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <html>
        <body>
            <h1>coolton web helper</h1>
            <p>POST /upload with an <code>Authorization: Bearer &lt;token&gt;</code> header to host files.</p>
            <p>POST /antibot/check with an anti-bot API token and a Slack message (or its permalink)
            to get coolton's anti-bot verdict: flag, watch or none.</p>
        </body>
    </html>
    """


@app.post("/upload")
async def upload(request: Request, filename: str = ""):
    if not _authorized(request):
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Localhost only: Caddy serves it over TLS (2390.proxy.tanjim.org). Listening on every
    # interface also put it on the internet over plain HTTP, upload token and all.
    uvicorn.run(app, host="127.0.0.1", port=2389)
