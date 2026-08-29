import os

import requests

WEB64_UPLOAD_URL = os.environ.get("WEB_HELPER_UPLOAD_URL", "https://tanjim.org:2390/upload")
WEB64_TOKEN_FILE = os.environ.get("WEB_HELPER_TOKEN_FILE", os.environ.get("WEB64_TOKEN_FILE", "/home/tanjim/web64_token"))


def _api_key() -> str:
    with open(WEB64_TOKEN_FILE) as f:
        return f.read().strip()


def upload_bytes(content: bytes, filename: str, mime: str = "") -> str:
    """Host raw bytes on the coolton web helper and return its URL.

    The server stores the file under a hash+timestamp path; this requires the
    shared token at WEB64_TOKEN_FILE, so only coolton can upload.
    """
    headers = {"Authorization": f"Bearer {_api_key()}"}
    if mime:
        headers["Content-Type"] = mime
    resp = requests.post(
        WEB64_UPLOAD_URL,
        headers=headers,
        data=content,
        params={"filename": filename},
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("url"):
        raise RuntimeError(f"web helper upload failed: {data}")
    return data["url"]
