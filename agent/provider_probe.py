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


def _probe_group(entries: list[tuple[str, dict]]) -> list[tuple[str, bool]]:
    """Probe every model belonging to one underlying provider, serially —
    run inside one worker thread by refresh_fallback_cache."""
    results = []
    for provider_name, config in entries:
        ok, display, elapsed, detail = test_provider(provider_name, config)
        results.append((provider_name, ok))
        if ok:
            logger.info("Fallback cache refresh: %s (%s) OK in %.1fs", provider_name, display, elapsed)
        else:
            logger.warning(
                "Fallback cache refresh: %s (%s) FAILED in %.1fs: %s",
                provider_name, display, elapsed, detail[:200],
            )
    return results


def refresh_fallback_cache() -> None:
    """Proactively probe every globally-configured (non-BYOK) provider and
    refresh the fallback cache in one atomic pass.

    Run on a schedule (see agent.scheduler) so the cache's working/dead marks
    stay continuously fresh instead of passively expiring — a live user turn
    then never has to discover a dead or working provider by trial and error,
    as long as this ran within the last refresh cycle.

    Models are grouped by underlying provider (see _group_key) and each group
    is probed serially within its own thread — so as not to hammer one
    upstream's rate limits with concurrent requests — but different providers'
    groups run in parallel, since they're independent APIs. A fully serial
    probe pays every model's latency one after another even across completely
    unrelated services; this only serializes what actually needs it.

    refresh_from_results treats the first ok=True entry in `results` as the
    new cached working provider, so the final list must come back in the same
    fallback-priority order as `order` regardless of which group's thread
    happens to finish first — results are reassembled by provider_name after
    every group completes, not appended in completion order.
    """
    from agent.fallback_cache import refresh_from_results

    order = provider_config.build_provider_order(None)
    if not order:
        logger.info("Fallback cache refresh: no providers configured, skipping")
        return

    groups: dict[str, list[tuple[str, dict]]] = {}
    for provider_name, config in order:
        groups.setdefault(_group_key(provider_name), []).append((provider_name, config))

    ok_by_name: dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=len(groups)) as pool:
        for group_results in pool.map(_probe_group, groups.values()):
            for provider_name, ok in group_results:
                ok_by_name[provider_name] = ok

    results = [(provider_name, ok_by_name[provider_name]) for provider_name, _ in order]
    refresh_from_results(results)
