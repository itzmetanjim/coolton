"""Upload files to Bucky (https://bucky.hackclub.com), Hack Club's public file host.

Used for files people download (upload_file_from_sandbox). Bucky serves every
file as binary/octet-stream, so anything that must display in place (images in
Slack, HTML embeds) stays on the coolton web helper (agent.web64_client).
"""
import os

import requests

BUCKY_URL = os.environ.get("BUCKY_URL", "https://bucky.hackclub.com/")


def upload_to_bucky(content: bytes, filename: str, mime: str = "") -> str:
    """Upload bytes as `filename` and return the public URL Bucky gives back."""
    resp = requests.post(
        BUCKY_URL,
        files={"file": (filename, content, mime or "application/octet-stream")},
        timeout=120,
    )
    resp.raise_for_status()
    url = resp.text.strip()
    if not url.startswith("https://"):
        raise RuntimeError(f"Bucky upload failed: {url[:300]}")
    return url
