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


async def _capture_raw_response(holder: dict, response) -> None:
    """httpx response event hook: eagerly buffer the raw body so it's captured even
    when the OpenAI SDK's own (permissive) parsing produces a ChatCompletion with
    every field None instead of raising on a malformed body — that's the shape
    pydantic_ai's stricter re-validation then fails on, but by then the raw bytes are
    gone. Reading via .aread() here just buffers the content on the Response object;
    the SDK reading it again afterward gets the same cached bytes, not a second
    network read, so this doesn't change what run_sync actually sees."""
    try:
        body = await response.aread()
        holder["status"] = response.status_code
        holder["body"] = body.decode("utf-8", errors="replace")
    except Exception:
        pass


def test_provider(provider_name: str, config: dict) -> tuple[bool, str, float, str]:
    """Run one cheap completion against a single provider/model.

    Returns (ok, display_name, elapsed_seconds, detail) — detail is the model's
    reply text on success, or a redacted error string on failure. On failure for a
    custom-base_url provider (e.g. HCAI), detail is prefixed with the actual raw HTTP
    response body when one was captured — a validation error alone (e.g. "4
    validation errors for ChatCompletion ... input_value=None") only says the SDK
    couldn't parse a ChatCompletion out of it, not what the endpoint actually sent
    back (an empty body, an HTML error page, a truncated stream, etc.).
    """
    display = config.get("display", provider_name)
    start = time.time()
    raw: dict = {}

    try:
        provider_config.apply_provider_env(provider_name, config["api_key"])

        from pydantic_ai import Agent

        if config.get("base_url"):
            import httpx
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider
            # No pooled keep-alive connections: this client is never explicitly closed
            # (run_sync tears down its own event loop before we could safely await
            # aclose() in it — closing from a different loop afterward is an httpx/anyio
            # footgun), so nothing here should hold an open OS connection past this one
            # request. The Python object itself is just normal garbage after this call.
            http_client = httpx.AsyncClient(
                event_hooks={"response": [lambda r: _capture_raw_response(raw, r)]},
                limits=httpx.Limits(max_keepalive_connections=0),
            )
            model = OpenAIChatModel(
                config["model"],
                provider=OpenAIProvider(
                    base_url=config["base_url"],
                    api_key=config["api_key"],
                    http_client=http_client,
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
        detail = redact(str(e), context="provider_probe")
        if raw.get("body") is not None:
            raw_body = redact(raw["body"], context="provider_probe raw response")[:500]
            detail = f"raw HTTP {raw.get('status', '?')} body: {raw_body!r} | {detail}"
        return False, display, elapsed, detail


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
                provider_name, display, elapsed, detail[:700],
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
