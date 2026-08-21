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
