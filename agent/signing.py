"""Short signed tokens coolton hands out and later trusts: web UI sessions
(web.auth) and Slack bot install links (agent.tools.slack_bot_deploy).

All are HMAC-SHA256 signed with COOLTON_WEB_SECRET, each under its own `purpose`
(mixed into the signed bytes), so a token issued for one purpose never verifies
as another, whatever its payload happens to look like. With no secret configured
nothing verifies (fail closed).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os


def _secret() -> bytes:
    return os.environ.get("COOLTON_WEB_SECRET", "").encode()


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _signature(purpose: str, body: str) -> str:
    return hmac.new(_secret(), f"{purpose}.{body}".encode(), hashlib.sha256).hexdigest()


def sign(purpose: str, payload: dict) -> str:
    """`payload` as a `<body>.<signature>` token for `purpose`."""
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_signature(purpose, body)}"


def verify(purpose: str, token: str) -> dict | None:
    """The payload of a token `sign` made for `purpose`, or None if it isn't one."""
    if not _secret() or not token:
        return None
    try:
        body, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _signature(purpose, body)):
        return None
    try:
        payload = json.loads(_b64decode(body))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None
