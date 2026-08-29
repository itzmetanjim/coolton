"""coolton web helper — file host + base64 HTML decoder.

Serves uploaded files at /f/<name> and decodes base64url strings to HTML
at /<encoded>. Used by coolton to host sandbox outputs and rendered HTML.
"""

import base64
import hashlib
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=2389)
