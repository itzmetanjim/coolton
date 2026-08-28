"""Cheap single-provider health probe, shared by the interactive "Test
Providers" button (listeners/actions/test_providers.py) and the background
fallback-cache refresh job (see refresh_fallback_cache below).
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

from agent import provider_config
from agent.redact import redact

logger = logging.getLogger(__name__)


def test_provider(provider_name: str, config: dict) -> tuple[bool, str, float, str]:
    """Run one cheap completion against a single provider/model.

    Returns (ok, display_name, elapsed_seconds, detail) — detail is the model's
    reply text on success, or a redacted error string on failure.
    """
    display = config.get("display", provider_name)
    start = time.time()

    try:
        provider_config.apply_provider_env(provider_name, config["api_key"])

        from pydantic_ai import Agent

        if config.get("base_url"):
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider
            model = OpenAIChatModel(
                config["model"],
                provider=OpenAIProvider(
                    base_url=config["base_url"],
                    api_key=config["api_key"],
                ),
            )
        else:
            model = config["model"]

        a = Agent(model)
        result = a.run_sync("Hello! Respond with the single word 'ok' if you receive this.")
        elapsed = time.time() - start
        return True, display, elapsed, result.output
    except Exception as e:
        elapsed = time.time() - start
        return False, display, elapsed, redact(str(e), context="provider_probe")


def _group_key(provider_name: str) -> str:
    """The underlying provider id behind a per-model fallback-chain name like
    "hcai_2" or "kilocode_5" (see provider_config._make_provider_name — same
    "_\\d+$" stripping it uses). Entries sharing this key hit the same upstream
    API and must be probed one at a time; entries in different groups hit
    independent APIs and can safely run concurrently."""
    return re.sub(r"_\d+$", "", provider_name)


def _probe_group(entries: list[tuple[str, dict]]) -> list[tuple[str, bool, str, float, str]]:
    """Probe every model belonging to one underlying provider, serially —
    run inside one worker thread by probe_all. Returns full
    (provider_name, ok, display, elapsed, detail) tuples."""
    results = []
    for provider_name, config in entries:
        ok, display, elapsed, detail = test_provider(provider_name, config)
        results.append((provider_name, ok, display, elapsed, detail))
        if ok:
            logger.info("Provider probe: %s (%s) OK in %.1fs", provider_name, display, elapsed)
        else:
            logger.warning(
                "Provider probe: %s (%s) FAILED in %.1fs: %s",
                provider_name, display, elapsed, detail[:200],
            )
    return results


def probe_all(order: list[tuple[str, dict]]) -> list[tuple[str, bool, str, float, str]]:
    """Probe every (provider_name, config) pair in `order`, parallelized across
    providers. Shared by refresh_fallback_cache (background) and the
    interactive "Test Providers" button (listeners/actions/test_providers.py) —
    both used to probe fully sequentially, which meant the button's total wait
    was roughly the sum of every single configured model's latency (tens of
    models, several seconds each).

    Entries are grouped by underlying provider (see _group_key): models
    sharing one upstream stay serial within their own thread, so as not to
    hammer that provider's rate limits concurrently, but independent
    providers' groups run in parallel.

    Returns (provider_name, ok, display, elapsed, detail) tuples reassembled
    into the same order as `order`, regardless of which group's thread
    happens to finish first — callers that care about fallback priority
    (refresh_fallback_cache picks the first ok=True as the working provider)
    depend on this.
    """
    if not order:
        return []

    groups: dict[str, list[tuple[str, dict]]] = {}
    for provider_name, config in order:
        groups.setdefault(_group_key(provider_name), []).append((provider_name, config))

    by_name: dict[str, tuple[bool, str, float, str]] = {}
    with ThreadPoolExecutor(max_workers=len(groups)) as pool:
        for group_results in pool.map(_probe_group, groups.values()):
            for provider_name, ok, display, elapsed, detail in group_results:
                by_name[provider_name] = (ok, display, elapsed, detail)

    return [(provider_name, *by_name[provider_name]) for provider_name, _ in order]


def refresh_fallback_cache() -> None:
    """Proactively probe every globally-configured (non-BYOK) provider and
    refresh the fallback cache in one atomic pass.

    Run on a schedule (see agent.scheduler) so the cache's working/dead marks
    stay continuously fresh instead of passively expiring — a live user turn
    then never has to discover a dead or working provider by trial and error,
    as long as this ran within the last refresh cycle.
    """
    from agent.fallback_cache import refresh_from_results

    order = provider_config.build_provider_order(None)
    if not order:
        logger.info("Fallback cache refresh: no providers configured, skipping")
        return

    full_results = probe_all(order)
    results = [(provider_name, ok) for provider_name, ok, _, _, _ in full_results]
    refresh_from_results(results)
