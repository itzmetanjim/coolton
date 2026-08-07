import json
import time
import logging
import threading

logger = logging.getLogger(__name__)

FALLBACK_CACHE_FILE = "fallback_cache.json"
WORKING_TTL_SECONDS = 1800
DEAD_TTL_SECONDS = 1800

_cache_lock = threading.Lock()


def _load_cache():
    try:
        with open(FALLBACK_CACHE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(cache):
    temp = f"{FALLBACK_CACHE_FILE}.tmp"
    with open(temp, "w") as f:
        json.dump(cache, f)
    import os
    os.replace(temp, FALLBACK_CACHE_FILE)


def get_working_provider() -> str | None:
    """Global last-known-good provider (shared across all users)."""
    with _cache_lock:
        cache = _load_cache()
        entry = cache.get("working")
        if entry and time.time() - entry["timestamp"] < WORKING_TTL_SECONDS:
            return entry["provider"]
    return None


def set_working_provider(provider_name: str):
    """Record a provider that just succeeded. Global + clears any dead mark."""
    with _cache_lock:
        cache = _load_cache()
        cache["working"] = {"provider": provider_name, "timestamp": time.time()}
        cache.setdefault("dead", {}).pop(provider_name, None)
        _save_cache(cache)
    logger.info(f"Fallback cache: working provider -> {provider_name}")


def get_dead_providers() -> dict:
    """Providers currently in cooldown after a hard failure, name -> reason."""
    with _cache_lock:
        cache = _load_cache()
        dead = cache.get("dead", {})
        now = time.time()
        return {
            name: info.get("reason", "failed")
            for name, info in dead.items()
            if now - info.get("since", 0) < DEAD_TTL_SECONDS
        }


def mark_dead(provider_name: str, reason: str):
    """Mark a provider dead for DEAD_TTL_SECONDS so future turns skip it."""
    with _cache_lock:
        cache = _load_cache()
        cache.setdefault("dead", {})[provider_name] = {
            "since": time.time(),
            "reason": reason[:300],
        }
        working = cache.get("working")
        if working and working["provider"] == provider_name:
            cache.pop("working", None)
        _save_cache(cache)
    logger.warning(f"Fallback cache: marked {provider_name} dead ({reason[:120]})")


def unmark_dead(provider_name: str):
    with _cache_lock:
        cache = _load_cache()
        cache.setdefault("dead", {}).pop(provider_name, None)
        _save_cache(cache)


def clear_cache():
    """Clear the whole fallback cache (working provider + dead marks)."""
    with _cache_lock:
        _save_cache({})
    logger.info("Fallback cache cleared (global)")
