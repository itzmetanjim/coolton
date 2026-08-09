import os
import threading

_SECRET_NAME_HINTS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASS",
    "CREDENTIAL",
    "SIGNING",
)

_notifier = None
_secret_values_cache = None
_secret_values_lock = threading.Lock()


def set_notifier(fn) -> None:
    """Register a callback invoked as notifier(keys, context) whenever a real
    secret is stripped out of a message. No-op unless the app wires one up."""
    global _notifier
    _notifier = fn


def secret_values() -> list[str]:
    """Actual secret values from the environment (e.g. *_API_KEY, *TOKEN, ...)."""
    global _secret_values_cache
    with _secret_values_lock:
        if _secret_values_cache is None:
            _secret_values_cache = [
                v
                for k, v in os.environ.items()
                if v and any(hint in k for hint in _SECRET_NAME_HINTS)
            ]
    return _secret_values_cache


def redact(msg: str, context: str = "") -> str:
    """Replace every known secret value found in msg with *** and notify."""
    hits = []
    for secret in secret_values():
        if secret and secret in msg:
            msg = msg.replace(secret, "***")
            hits.append(secret)
    if hits and _notifier is not None:
        try:
            _notifier(hits, context)
        except Exception:
            pass
    return msg
