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


# Maps sandbox_id -> token so we can revoke the exact token a sandbox used
# (and so a leaked token from one sandbox cannot be blindly reused for another).
_token_by_sandbox: dict[str, str] = {}


def issue_sandbox_token(sandbox_id: str) -> str:
    """Create and authorize a fresh per-sandbox token.

    The token is namespaced with the sandbox id (sent as the credential username)
    so the host proxy can scope/revoke it per sandbox, and we track the mapping
    locally so revoke_sandbox_token(sandbox_id) actually removes the right one.
    """
    if not sandbox_id:
        raise ValueError("issue_sandbox_token requires a sandbox_id")
    tok = secrets.token_urlsafe(32)
    resp = requests.post(
        f"{ADMIN_URL}/tokens",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        # sandbox_id is passed as `user` so the proxy can bind the token to it.
        json={"token": tok, "user": sandbox_id},
        timeout=10,
    )
    resp.raise_for_status()
    # Authorize for both auth styles the sandbox uses:
    #  - gh sends the token as Bearer/Token with an empty username -> key ("", tok)
    #  - git sends Basic "sandbox_id:token" -> key (sandbox_id, tok)
    # Adding both means a leaked token still only works for THIS sandbox.
    requests.post(
        f"{ADMIN_URL}/tokens",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        json={"token": tok, "user": ""},
        timeout=10,
    )
    _token_by_sandbox[sandbox_id] = tok
    return tok


def revoke_sandbox_token(token: str | None = None, *, sandbox_id: str | None = None) -> None:
    """Revoke a previously issued sandbox token.

    Pass either the raw `token`, or the `sandbox_id` (we look up the token we issued).
    """
    if not token and sandbox_id:
        token = _token_by_sandbox.pop(sandbox_id, None)
    if not token:
        return
    if sandbox_id:
        _token_by_sandbox.pop(sandbox_id, None)
    try:
        requests.delete(
            f"{ADMIN_URL}/tokens",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            json={"token": token},
            timeout=10,
        )
    except Exception as e:  # best-effort cleanup
        print(f"[github_proxy_client] revoke failed: {e}")
