"""Automatic rotation for Slack CLI (xoxe) tokens, kept in a gitignored JSON file.

Slack CLI-issued xoxe access tokens expire every ~12 hours and are refreshed
with a single-use refresh token via `tooling.tokens.rotate` (no client
credentials). Each rotation returns a fresh access token, a fresh refresh
token, and the expiry epoch. We persist the pair atomically to
`xoxe_tokens.json` (gitignored, so it can be edited programmatically) and
expose it to the rest of the app through getters. The redactor scans the same
file so the values are always redacted.

This only manages the xoxe pair. The env-based xoxb/xoxp tokens are untouched.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

TOKENS_FILE = Path(__file__).resolve().parent.parent / "xoxe_tokens.json"

ROTATE_URL = "https://slack.com/api/tooling.tokens.rotate"

# Rotate when the access token has this much (or less) life remaining.
ROTATE_BEFORE_EXPIRY_SECONDS = 4 * 60 * 60

# xoxe tokens live 12 hours when the response omits exp/iat.
DEFAULT_LIFETIME_SECONDS = 12 * 60 * 60

_lock = threading.Lock()
_rotating = False
_cache: dict | None = None


def _load() -> dict:
    global _cache
    with _lock:
        if _cache is None:
            try:
                data = json.loads(TOKENS_FILE.read_text())
                _cache = data if isinstance(data, dict) else {}
            except OSError:
                _cache = {}
            except json.JSONDecodeError:
                logger.exception("%s is corrupt; treating it as empty", TOKENS_FILE)
                _cache = {}
        return _cache


def _persist(data: dict) -> None:
    tmp = TOKENS_FILE.with_name("xoxe_tokens.json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, TOKENS_FILE)
    global _cache
    _cache = data


def get_access_token() -> str | None:
    value = _load().get("access_token")
    return value if isinstance(value, str) and value else None


def get_refresh_token() -> str | None:
    value = _load().get("refresh_token")
    return value if isinstance(value, str) and value else None


def get_expires_at() -> float | None:
    value = _load().get("expires_at")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _call_rotate(refresh_token: str) -> dict | None:
    try:
        resp = requests.post(ROTATE_URL, data={"refresh_token": refresh_token}, timeout=30)
        return resp.json()
    except Exception:
        logger.exception("Token rotation: request to %s failed", ROTATE_URL)
        return None


def rotate_token() -> bool:
    """Refresh the xoxe pair via tooling.tokens.rotate and persist the new one."""
    global _rotating
    with _lock:
        if _rotating:
            return False
        _rotating = True
    try:
        refresh_token = get_refresh_token()
        if not refresh_token:
            logger.info("Token rotation: no refresh token in %s", TOKENS_FILE)
            return False

        payload = _call_rotate(refresh_token)
        if payload is None:
            return False
        if not payload.get("ok"):
            logger.error("Token rotation failed: %s", payload.get("error", "unknown"))
            return False

        access = payload.get("token")
        refresh = payload.get("refresh_token")
        if not access or not refresh:
            logger.error("Token rotation: response missing token/refresh_token")
            return False

        exp = payload.get("exp")
        if not isinstance(exp, int):
            iat = payload.get("iat")
            if isinstance(iat, int):
                exp = iat + DEFAULT_LIFETIME_SECONDS
            else:
                exp = int(time.time()) + DEFAULT_LIFETIME_SECONDS

        _persist({"access_token": access, "refresh_token": refresh, "expires_at": exp})

        try:
            from agent.redact import invalidate_secret_cache

            invalidate_secret_cache()
        except Exception:
            pass

        logger.info("Token rotation: rotated xoxe token, valid until epoch %s", exp)
        return True
    finally:
        with _lock:
            _rotating = False


def check_and_rotate() -> None:
    """Rotate the xoxe token if it is due (no known expiry, or expiring soon)."""
    data = _load()
    if not data.get("refresh_token"):
        return
    expires_at = data.get("expires_at")
    if expires_at is not None:
        try:
            if float(expires_at) - time.time() > ROTATE_BEFORE_EXPIRY_SECONDS:
                return
        except (TypeError, ValueError):
            pass
    rotate_token()


def start_token_rotation() -> None:
    """Run an initial background rotation check so expiry is established quickly."""

    def _initial_check():
        try:
            check_and_rotate()
        except Exception:
            logger.exception("Initial token rotation check failed")

    threading.Thread(target=_initial_check, daemon=True, name="token-rotation-initial").start()
