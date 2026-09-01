import threading
import time
from unittest.mock import Mock

import agent.provider_probe as provider_probe


def test_refresh_fallback_cache_tests_every_configured_provider_and_writes_results(monkeypatch):
    order = [
        ("anthropic", {"model": "m1", "api_key": "k1"}),
        ("openai", {"model": "m2", "api_key": "k2"}),
    ]
    monkeypatch.setattr(provider_probe.provider_config, "build_provider_order", lambda user_id: order)

    def fake_test_provider(name, config):
        return (name == "openai"), name, 0.1, "ok" if name == "openai" else "boom"

    monkeypatch.setattr(provider_probe, "test_provider", fake_test_provider)
    refresh_mock = Mock()
    monkeypatch.setattr("agent.fallback_cache.refresh_from_results", refresh_mock)

    provider_probe.refresh_fallback_cache()

    refresh_mock.assert_called_once_with([("anthropic", False), ("openai", True)])


def test_refresh_fallback_cache_uses_global_order_not_any_user(monkeypatch):
    """The background refresh is a global cache — must never pull in a
    specific user's BYOK endpoint."""
    seen = {}

    def fake_build_order(user_id):
        seen["user_id"] = user_id
        return []

    monkeypatch.setattr(provider_probe.provider_config, "build_provider_order", fake_build_order)
    provider_probe.refresh_fallback_cache()
    assert seen["user_id"] is None


def test_refresh_fallback_cache_noop_when_nothing_configured(monkeypatch):
    monkeypatch.setattr(provider_probe.provider_config, "build_provider_order", lambda user_id: [])
    refresh_mock = Mock()
    monkeypatch.setattr("agent.fallback_cache.refresh_from_results", refresh_mock)

    provider_probe.refresh_fallback_cache()

    refresh_mock.assert_not_called()


def test_group_key_strips_the_per_model_index_suffix():
    # Matches provider_config._make_provider_name's "<provider_id>_<index>" shape.
    assert provider_probe._group_key("hcai_2") == "hcai"
    assert provider_probe._group_key("kilocode_16") == "kilocode"
    assert provider_probe._group_key("mistral") == "mistral"  # single-model provider, no suffix


def test_refresh_fallback_cache_preserves_priority_order_despite_parallel_groups(monkeypatch):
    """refresh_from_results picks the first ok=True entry as the new working
    provider — that's only correct if the final results list comes back in
    the same order as `order`, even though different providers' groups race
    each other in separate threads and may finish in any order."""
    order = [
        ("hcai_0", {"model": "m1", "api_key": "k"}),
        ("hcai_1", {"model": "m2", "api_key": "k"}),
        ("anthropic", {"model": "m3", "api_key": "k"}),
        ("openai", {"model": "m4", "api_key": "k"}),
    ]
    monkeypatch.setattr(provider_probe.provider_config, "build_provider_order", lambda user_id: order)

    # openai (last in `order`) finishes fastest; hcai_0 (first) finishes slowest —
    # if results were appended in completion order instead of reassembled, the
    # first ok=True in the output would be "openai", not "hcai_0".
    delays = {"hcai_0": 0.08, "hcai_1": 0.0, "anthropic": 0.04, "openai": 0.0}

    def fake_test_provider(name, config):
        time.sleep(delays[name])
        return True, name, delays[name], "ok"

    monkeypatch.setattr(provider_probe, "test_provider", fake_test_provider)
    refresh_mock = Mock()
    monkeypatch.setattr("agent.fallback_cache.refresh_from_results", refresh_mock)

    provider_probe.refresh_fallback_cache()

    refresh_mock.assert_called_once_with(
        [("hcai_0", True), ("hcai_1", True), ("anthropic", True), ("openai", True)]
    )


def test_refresh_fallback_cache_probes_different_providers_concurrently(monkeypatch):
    """Three providers each 'take' 0.2s. If they ran fully serially (the old
    behavior) this pass would take >=0.6s; running as parallel groups it
    should finish in well under that."""
    order = [
        ("hcai_0", {"model": "m1", "api_key": "k"}),
        ("anthropic", {"model": "m2", "api_key": "k"}),
        ("openai", {"model": "m3", "api_key": "k"}),
    ]
    monkeypatch.setattr(provider_probe.provider_config, "build_provider_order", lambda user_id: order)

    def fake_test_provider(name, config):
        time.sleep(0.2)
        return True, name, 0.2, "ok"

    monkeypatch.setattr(provider_probe, "test_provider", fake_test_provider)
    monkeypatch.setattr("agent.fallback_cache.refresh_from_results", Mock())

    start = time.time()
    provider_probe.refresh_fallback_cache()
    elapsed = time.time() - start

    assert elapsed < 0.5  # comfortably under the 0.6s a serial run would take


def test_refresh_fallback_cache_keeps_same_provider_models_serial(monkeypatch):
    """Multiple models under one provider (e.g. hcai_0, hcai_1, hcai_2) share
    rate limits with that one upstream — they must never run concurrently with
    each other, only with a *different* provider's group."""
    order = [
        ("hcai_0", {"model": "m1", "api_key": "k"}),
        ("hcai_1", {"model": "m2", "api_key": "k"}),
        ("hcai_2", {"model": "m3", "api_key": "k"}),
    ]
    monkeypatch.setattr(provider_probe.provider_config, "build_provider_order", lambda user_id: order)

    lock = threading.Lock()
    concurrent_count = 0
    max_concurrent = 0

    def fake_test_provider(name, config):
        nonlocal concurrent_count, max_concurrent
        with lock:
            concurrent_count += 1
            max_concurrent = max(max_concurrent, concurrent_count)
        time.sleep(0.05)
        with lock:
            concurrent_count -= 1
        return True, name, 0.05, "ok"

    monkeypatch.setattr(provider_probe, "test_provider", fake_test_provider)
    monkeypatch.setattr("agent.fallback_cache.refresh_from_results", Mock())

    provider_probe.refresh_fallback_cache()

    assert max_concurrent == 1


def test_provider_redacts_secret_in_error_message(monkeypatch):
    """This is used both by the interactive "Test Providers" button and the
    background fallback-cache refresh — both post/log results, so a leaked
    key in an SDK/HTTP error must go through redaction like every other
    secret-adjacent path in the codebase."""
    monkeypatch.setenv("JAMS_API_KEY", "sk-super-secret-123")

    def boom(provider_name, api_key):
        raise RuntimeError(f"auth failed with key sk-super-secret-123 for {provider_name}")

    monkeypatch.setattr("agent.provider_probe.provider_config.apply_provider_env", boom)
    from agent.redact import invalidate_secret_cache
    invalidate_secret_cache()

    ok, display, elapsed, detail = provider_probe.test_provider(
        "jams", {"model": "m", "api_key": "sk-super-secret-123"}
    )

    assert ok is False
    assert "sk-super-secret-123" not in detail
    assert "***" in detail


def test_provider_includes_raw_http_body_on_failure(monkeypatch):
    """A ChatCompletion validation error alone (e.g. "4 validation errors ...
    input_value=None") only says the SDK couldn't parse a response — not what the
    endpoint actually sent. Observed live against HCAI: a 200 status carrying an
    error payload instead of a real ChatCompletion (HCAI/OpenRouter disguising a 429
    rate limit as HTTP 200), which this test reproduces with a mock transport."""
    import httpx

    def handler(request):
        return httpx.Response(
            200,
            json={
                "id": "gen-1788234631-fake",
                "error": {"message": "some-model is temporarily rate-limited upstream", "code": 429},
            },
        )

    # Patch AsyncClient's __init__ (not the class itself, which openai's SDK checks
    # against via isinstance()) to force every real construction onto a mock
    # transport, so the actual request never leaves the process.
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    ok, display, elapsed, detail = provider_probe.test_provider(
        "hcai_0",
        {
            "model": "openai/some-model",
            "base_url": "https://fake.example/v1",
            "api_key": "k",
            "display": "Fake Model",
        },
    )

    assert ok is False
    assert "raw HTTP 200 body" in detail
    assert "temporarily rate-limited upstream" in detail
    assert "validation errors for ChatCompletion" in detail


def test_provider_no_raw_body_prefix_for_non_base_url_providers(monkeypatch):
    """Providers without a custom base_url (anthropic, openai, google, ...) go
    through pydantic_ai's own model resolution, not the http_client this codebase
    constructs — there's no raw body to capture there, and detail must stay exactly
    the plain redacted error, not gain a stray 'raw HTTP' prefix."""
    def boom(provider_name, api_key):
        raise RuntimeError("boom")

    monkeypatch.setattr("agent.provider_probe.provider_config.apply_provider_env", boom)

    ok, display, elapsed, detail = provider_probe.test_provider(
        "anthropic", {"model": "m", "api_key": "k"}
    )

    assert ok is False
    assert "raw HTTP" not in detail
    assert detail == "boom"
