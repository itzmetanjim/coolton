import threading
import time
from unittest.mock import Mock

from listeners.actions.fallback_cache_actions import handle_fallback_cache_clear


def test_clear_triggers_immediate_background_refresh(monkeypatch):
    """Clearing the cache must not leave the user waiting up to
    REFRESH_INTERVAL_SECONDS for the next scheduled cycle — it should kick
    off an immediate re-probe in the background."""
    clear_mock = Mock()
    monkeypatch.setattr("agent.fallback_cache.clear_cache", clear_mock)
    refresh_mock = Mock()
    monkeypatch.setattr("agent.provider_probe.refresh_fallback_cache", refresh_mock)

    ack = Mock()
    client = Mock()
    context = Mock()
    context.user_id = "U1"

    handle_fallback_cache_clear(ack, client, context)

    ack.assert_called_once()
    clear_mock.assert_called_once()
    client.chat_postEphemeral.assert_called_once()

    # The refresh runs in a background thread — give it a moment to complete.
    for _ in range(50):
        if refresh_mock.called:
            break
        time.sleep(0.02)
    refresh_mock.assert_called_once()


def test_clear_does_not_block_on_the_refresh(monkeypatch):
    """The refresh must run on its own thread, not inline in the handler —
    otherwise the ack()/ephemeral response would be delayed by a probe that
    can take up to ~a minute across every configured provider."""
    monkeypatch.setattr("agent.fallback_cache.clear_cache", Mock())
    started = threading.Event()

    def slow_refresh():
        started.wait(timeout=2)

    monkeypatch.setattr("agent.provider_probe.refresh_fallback_cache", slow_refresh)

    ack = Mock()
    client = Mock()
    context = Mock()
    context.user_id = "U1"

    start = time.time()
    handle_fallback_cache_clear(ack, client, context)
    elapsed = time.time() - start

    started.set()  # let the background thread finish so it doesn't leak past the test
    assert elapsed < 1.0, "handler must return immediately, not wait for the refresh"


def test_refresh_failure_after_clear_is_caught_and_logged(monkeypatch):
    monkeypatch.setattr("agent.fallback_cache.clear_cache", Mock())
    monkeypatch.setattr(
        "agent.provider_probe.refresh_fallback_cache",
        Mock(side_effect=RuntimeError("boom")),
    )

    ack = Mock()
    client = Mock()
    context = Mock()
    context.user_id = "U1"

    handle_fallback_cache_clear(ack, client, context)  # must not raise
    time.sleep(0.1)  # let the background thread run and swallow the exception
