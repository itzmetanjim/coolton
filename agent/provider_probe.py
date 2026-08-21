"""Cheap single-provider health probe, shared by the interactive "Test
Providers" button (listeners/actions/test_providers.py) and the background
fallback-cache refresh job (see refresh_fallback_cache below).
"""

import logging
import time

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

    results: list[tuple[str, bool]] = []
    for provider_name, config in order:
        ok, display, elapsed, detail = test_provider(provider_name, config)
        results.append((provider_name, ok))
        if ok:
            logger.info("Fallback cache refresh: %s (%s) OK in %.1fs", provider_name, display, elapsed)
        else:
            logger.warning(
                "Fallback cache refresh: %s (%s) FAILED in %.1fs: %s",
                provider_name, display, elapsed, detail[:200],
            )

    refresh_from_results(results)
