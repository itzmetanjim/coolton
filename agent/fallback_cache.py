import json
import time
import logging
import threading

logger = logging.getLogger(__name__)

FALLBACK_CACHE_FILE = "fallback_cache.json"
TTL_SECONDS = 1800

_cache_lock = threading.Lock()


def _load_cache():
    try:
        with open(FALLBACK_CACHE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(cache):
    with open(FALLBACK_CACHE_FILE, "w") as f:
        json.dump(cache, f)


def get_working_provider(user_id: str) -> str | None:
    with _cache_lock:
        cache = _load_cache()
        entry = cache.get(user_id)
        if entry and time.time() - entry["timestamp"] < TTL_SECONDS:
            return entry["provider"]
    return None


def set_working_provider(user_id: str, provider_name: str):
    with _cache_lock:
        cache = _load_cache()
        cache[user_id] = {"provider": provider_name, "timestamp": time.time()}
        _save_cache(cache)
    logger.info(f"Fallback cache: {user_id} -> {provider_name}")


def clear_cache(user_id: str):
    with _cache_lock:
        cache = _load_cache()
        cache.pop(user_id, None)
        _save_cache(cache)
    logger.info(f"Fallback cache cleared for {user_id}")
