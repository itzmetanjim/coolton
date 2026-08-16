import json
import os
import threading
from pathlib import Path

_SECRET_NAME_HINTS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASS",
    "CREDENTIAL",
    "SIGNING",
)

_CANARY_VALUE = "COOLTON-CANARY-c7e6f357-5197-4d5e-8682-9e0758561d8f"

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
_XOXE_TOKENS_PATH = Path(__file__).resolve().parent.parent / "xoxe_tokens.json"

_notifier = None
_secret_values_cache = None
_secret_values_lock = threading.Lock()


def set_notifier(fn) -> None:
    """Register a callback invoked as notifier(keys, context) whenever a real
    secret is stripped out of a message. No-op unless the app wires one up."""
    global _notifier
    _notifier = fn


def invalidate_secret_cache() -> None:
    """Drop the cached secret list so the next redaction picks up current
    values. Called after token rotation swaps the credentials in os.environ."""
    global _secret_values_cache
    with _secret_values_lock:
        _secret_values_cache = None


def secret_values() -> list[tuple[str, str]]:
    """Actual secret values from the environment (e.g. *_API_KEY, *TOKEN, ...)
    and from the .env file, plus the hardcoded canary, as (key, value) pairs.

    Reading .env too means xoxe refresh tokens and any other secret stored in
    the file are redacted even if they never reached os.environ (e.g. when a
    key was already set in the shell and load_dotenv(override=False) skipped
    it).
    """
    global _secret_values_cache
    with _secret_values_lock:
        if _secret_values_cache is None:
            seen = set()
            entries = []

            def _add(key: str, value: str) -> None:
                if value and value not in seen:
                    seen.add(value)
                    entries.append((key, value))

            for key, value in os.environ.items():
                if value and any(hint in key for hint in _SECRET_NAME_HINTS):
                    _add(key, value)
            try:
                with open(_ENV_PATH) as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        key, value = line.split("=", 1)
                        if any(hint in key for hint in _SECRET_NAME_HINTS):
                            _add(key, value)
            except OSError:
                pass
            try:
                xoxe = json.loads(_XOXE_TOKENS_PATH.read_text())
                if isinstance(xoxe, dict):
                    for key in ("access_token", "refresh_token"):
                        value = xoxe.get(key)
                        if isinstance(value, str) and value:
                            _add("XOXE_" + key.upper(), value)
            except (OSError, json.JSONDecodeError):
                pass
            _add("COOLTON_CANARY", _CANARY_VALUE)
            _secret_values_cache = entries
    return _secret_values_cache


def redact(msg: str, context: str = "") -> str:
    """Replace every known secret value found in msg with *** and notify."""
    hits = []
    for key, secret in secret_values():
        if secret and secret in msg:
            msg = msg.replace(secret, "***")
            hits.append(key)
    if hits and _notifier is not None:
        try:
            _notifier(hits, context)
        except Exception:
            pass
    return msg


def strip_secret_keys(obj):
    """Recursively drop token/secret-typed keys from parsed JSON (e.g. Slack
    `api.test` echoes the token back under `args.token`). The value never
    reaches the output at all, so it can't be leaked or misused."""
    if isinstance(obj, dict):
        return {
            k: strip_secret_keys(v)
            for k, v in obj.items()
            if not any(hint in k.upper() for hint in _SECRET_NAME_HINTS)
        }
    if isinstance(obj, list):
        return [strip_secret_keys(item) for item in obj]
    return obj
