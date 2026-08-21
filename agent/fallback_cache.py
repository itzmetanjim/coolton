import json
import time
import logging
import threading

logger = logging.getLogger(__name__)

FALLBACK_CACHE_FILE = "fallback_cache.json"

# agent.scheduler runs refresh_fallback_cache() (agent/provider_probe.py) every
# REFRESH_INTERVAL_SECONDS, proactively re-testing every provider and rewriting
# working/dead in one pass — see refresh_from_results(). Under normal operation
# an entry's timestamp is therefore never more than one refresh cycle old.
# WORKING_TTL_SECONDS/DEAD_TTL_SECONDS are a safety net, not the primary
# expiry mechanism: they're wider than the refresh interval so a refresh
# cycle that's still running (up to ~a minute for a full provider list) never
# makes the previous cycle's still-valid data look expired mid-refresh. If
# the background job stops running entirely (e.g. APScheduler not started),
# entries still eventually age out here rather than being trusted forever.
REFRESH_INTERVAL_SECONDS = 1800
_TTL_GRACE_SECONDS = 300
WORKING_TTL_SECONDS = REFRESH_INTERVAL_SECONDS + _TTL_GRACE_SECONDS
DEAD_TTL_SECONDS = REFRESH_INTERVAL_SECONDS + _TTL_GRACE_SECONDS

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


def refresh_from_results(results: list[tuple[str, bool]]) -> None:
    """Atomically rewrite working/dead from a full-chain background probe.

    `results` is (provider_name, ok) pairs in fallback-priority order, as
    produced by agent.provider_probe.refresh_fallback_cache. The first ok=True
    entry becomes the cached `working` provider; every ok=False entry gets a
    fresh `dead` mark. A provider not present in `results` (e.g. BYOK, or one
    with no key configured) is left untouched — this function only updates
    entries it actually just tested.
    """
    now = time.time()
    with _cache_lock:
        cache = _load_cache()
        working_provider = next((name for name, ok in results if ok), None)
        if working_provider:
            cache["working"] = {"provider": working_provider, "timestamp": now}
        else:
            cache.pop("working", None)

        dead = cache.setdefault("dead", {})
        for name, ok in results:
            if ok:
                dead.pop(name, None)
            else:
                dead[name] = {"since": now, "reason": "background refresh probe failed"}

        cache["last_refreshed_at"] = now
        _save_cache(cache)
    logger.info(
        f"Fallback cache: background refresh complete ({len(results)} provider(s) tested, "
        f"working={working_provider})"
    )


def clear_cache():
    """Clear the whole fallback cache (working provider + dead marks)."""
    with _cache_lock:
        _save_cache({})
    logger.info("Fallback cache cleared (global)")
