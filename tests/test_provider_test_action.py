from listeners.actions.test_providers import _test_single


def test_test_single_redacts_secret_in_error_message(monkeypatch):
    """This is the one place testing provider credentials directly, and the
    result gets posted straight to the user's Slack DM — an SDK/HTTP error can
    echo the key back, so it must go through the same redaction every other
    secret-adjacent path uses."""
    monkeypatch.setenv("JAMS_API_KEY", "sk-super-secret-123")

    def boom(provider_name, api_key):
        raise RuntimeError(f"auth failed with key sk-super-secret-123 for {provider_name}")

    monkeypatch.setattr("listeners.actions.test_providers._set_env", boom)
    from agent.redact import invalidate_secret_cache
    invalidate_secret_cache()

    ok, display, elapsed, detail = _test_single("jams", {"model": "m", "api_key": "sk-super-secret-123"})

    assert ok is False
    assert "sk-super-secret-123" not in detail
    assert "***" in detail
