from unittest.mock import Mock

import pytest

from agent import mcp_health as mh


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(mh, "MCP_HEALTH_FILE", str(tmp_path / "mcp_health.json"))
    notify_mock = Mock()
    monkeypatch.setattr(mh, "notify_admin", notify_mock)
    return notify_mock


def test_check_mcp_health_no_token(monkeypatch, isolated):
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
    healthy, detail = mh.check_mcp_health()
    assert healthy is False
    assert "SLACK_USER_TOKEN" in detail


def test_first_check_down_alerts(monkeypatch, isolated):
    monkeypatch.setattr(mh, "check_mcp_health", lambda: (False, "boom"))
    mh.refresh_mcp_health()
    isolated.assert_called_once()
    assert "DOWN" in isolated.call_args[0][0]


def test_first_check_healthy_does_not_alert(monkeypatch, isolated):
    monkeypatch.setattr(mh, "check_mcp_health", lambda: (True, ""))
    mh.refresh_mcp_health()
    isolated.assert_not_called()


def test_transition_to_down_alerts_once(monkeypatch, isolated):
    monkeypatch.setattr(mh, "check_mcp_health", lambda: (True, ""))
    mh.refresh_mcp_health()
    isolated.assert_not_called()

    monkeypatch.setattr(mh, "check_mcp_health", lambda: (False, "connection refused"))
    mh.refresh_mcp_health()
    isolated.assert_called_once()
    assert "DOWN" in isolated.call_args[0][0]

    # staying down within the reminder window should not alert again
    isolated.reset_mock()
    mh.refresh_mcp_health()
    isolated.assert_not_called()


def test_recovery_alerts(monkeypatch, isolated):
    monkeypatch.setattr(mh, "check_mcp_health", lambda: (False, "down"))
    mh.refresh_mcp_health()
    isolated.reset_mock()

    monkeypatch.setattr(mh, "check_mcp_health", lambda: (True, ""))
    mh.refresh_mcp_health()
    isolated.assert_called_once()
    assert "back up" in isolated.call_args[0][0]


def test_still_down_reminder_fires_after_window(monkeypatch, isolated):
    monkeypatch.setattr(mh, "check_mcp_health", lambda: (False, "down"))
    mh.refresh_mcp_health()
    isolated.reset_mock()

    # simulate the reminder window having already elapsed by rewriting last_alerted
    state = mh._load()
    state["last_alerted"] = state["last_alerted"] - mh._STILL_DOWN_REMINDER_SECONDS - 1
    mh._save(state)

    mh.refresh_mcp_health()
    isolated.assert_called_once()
    assert "STILL down" in isolated.call_args[0][0]
