"""coolton web helper — file host.

Serves files coolton uploads (HTML embeds, screenshots) at /f/<name>. Uploading
needs the Bearer token; there's no way to host anything else here.
"""

import hashlib
import hmac
import logging
import mimetypes
import os
import time

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


@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <html>
        <body>
            <h1>coolton web helper</h1>
            <p>POST /upload with an <code>Authorization: Bearer &lt;token&gt;</code> header to host files.</p>
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
    uvicorn.run(app, host="0.0.0.0", port=2389)
