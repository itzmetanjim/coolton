"""Client for coolton's host-side GitHub proxy (github_proxy.py).

The proxy runs as its own systemd service and is exposed publicly (via Caddy TLS) as
https://ghproxy.tanjim.org for sandbox GitHub traffic, plus a localhost-only admin API
on 127.0.0.1:29055 for issuing/revoking per-sandbox tokens.

This module is what agent.py imports to wire a sandbox's GitHub access: each sandbox gets
its own short-lived token, authorized on sandbox start and revoked on sandbox stop. The
real GitHub PAT never leaves the host.
"""

import json
import os
import secrets

import requests

ADMIN_URL = os.environ.get("GH_PROXY_ADMIN_URL", "http://127.0.0.1:29055")
ADMIN_TOKEN = os.environ.get("GH_PROXY_ADMIN_TOKEN", "") or os.environ.get("COOLTON_GH_TOKEN", "")

# Public endpoint the sandbox uses for GitHub traffic (TLS terminated by Caddy).
PUBLIC_PROXY_HOST = os.environ.get("GH_PROXY_HOST", "ghproxy.tanjim.org")


def issue_sandbox_token(sandbox_id: str) -> str:
    """Create and authorize a fresh per-sandbox token. Returns the token string."""
    tok = secrets.token_urlsafe(32)
    resp = requests.post(
        f"{ADMIN_URL}/tokens",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        json={"token": tok},
        timeout=10,
    )
    resp.raise_for_status()
    return tok


def revoke_sandbox_token(token: str) -> None:
    """Revoke a previously issued sandbox token."""
    if not token:
        return
    try:
        requests.delete(
            f"{ADMIN_URL}/tokens",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            json={"token": token},
            timeout=10,
        )
    except Exception as e:  # best-effort cleanup
        print(f"[github_proxy_client] revoke failed: {e}")
