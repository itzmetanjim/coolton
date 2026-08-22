import time
from unittest.mock import Mock

from agent import admin_alerts


def _wait_for(mock, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mock.called:
            return
        time.sleep(0.02)


def test_notify_admin_sends_dm_to_admin_user(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    post_mock = Mock(return_value=Mock(json=lambda: {"ok": True}))
    monkeypatch.setattr(admin_alerts.requests, "post", post_mock)

    admin_alerts.notify_admin("hello lily")

    _wait_for(post_mock)
    post_mock.assert_called_once()
    _, kwargs = post_mock.call_args
    assert kwargs["json"]["channel"] == admin_alerts.ADMIN_USER_ID
    assert kwargs["json"]["text"] == "hello lily"


def test_notify_admin_without_dedupe_key_always_sends(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    post_mock = Mock(return_value=Mock(json=lambda: {"ok": True}))
    monkeypatch.setattr(admin_alerts.requests, "post", post_mock)

    admin_alerts.notify_admin("one")
    admin_alerts.notify_admin("two")

    _wait_for(post_mock)
    time.sleep(0.1)
    assert post_mock.call_count == 2


def test_notify_admin_dedupes_within_window(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    post_mock = Mock(return_value=Mock(json=lambda: {"ok": True}))
    monkeypatch.setattr(admin_alerts.requests, "post", post_mock)

    admin_alerts.notify_admin("first", dedupe_key="k1", min_interval_seconds=60)
    admin_alerts.notify_admin("second", dedupe_key="k1", min_interval_seconds=60)

    _wait_for(post_mock)
    time.sleep(0.1)
    post_mock.assert_called_once()


def test_notify_admin_missing_bot_token_does_not_raise(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    post_mock = Mock()
    monkeypatch.setattr(admin_alerts.requests, "post", post_mock)

    admin_alerts.notify_admin("no token")  # must not raise
    time.sleep(0.1)
    post_mock.assert_not_called()
