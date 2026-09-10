"""agent.hcai_status — the HCAI /up balance check + per-thread warning."""

import time
from unittest.mock import Mock

import pytest

from agent import hcai_status


@pytest.fixture(autouse=True)
def _clear_state():
    hcai_status._last_warned.clear()
    yield
    hcai_status._last_warned.clear()


def _resp(json_body, status_ok=True):
    resp = Mock()
    resp.json.return_value = json_body
    if status_ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = Exception("bad status")
    return resp


def test_fetch_status_returns_parsed_json(monkeypatch):
    monkeypatch.setattr(
        hcai_status.requests, "get",
        lambda url, timeout: _resp({"status": "down", "balanceRemaining": -0.5}),
    )
    result = hcai_status.fetch_status()
    assert result == {"status": "down", "balanceRemaining": -0.5}


def test_fetch_status_returns_none_on_request_exception(monkeypatch):
    def _raise(url, timeout):
        raise ConnectionError("boom")
    monkeypatch.setattr(hcai_status.requests, "get", _raise)
    assert hcai_status.fetch_status() is None


def test_fetch_status_returns_none_on_non_2xx(monkeypatch):
    monkeypatch.setattr(
        hcai_status.requests, "get",
        lambda url, timeout: _resp({"balanceRemaining": 5}, status_ok=False),
    )
    assert hcai_status.fetch_status() is None


def test_fetch_status_returns_none_for_non_dict_body(monkeypatch):
    monkeypatch.setattr(hcai_status.requests, "get", lambda url, timeout: _resp([1, 2, 3]))
    assert hcai_status.fetch_status() is None


def test_warn_low_balance_posts_when_balance_below_threshold(monkeypatch):
    monkeypatch.setattr(
        hcai_status, "fetch_status", lambda: {"status": "down", "balanceRemaining": -0.27},
    )
    client = Mock()
    hcai_status._warn_low_balance(client, "C1", "1.1")
    client.chat_postMessage.assert_called_once_with(
        channel="C1", thread_ts="1.1", text=hcai_status.WARNING_TEXT,
    )


def test_warn_low_balance_coerces_empty_thread_ts_to_none(monkeypatch):
    """thread_ts="" is a code channel's channel-level conversation (see
    agent.code_channel_store) — must post at channel level, not thread under ""."""
    monkeypatch.setattr(
        hcai_status, "fetch_status", lambda: {"balanceRemaining": 0.0},
    )
    client = Mock()
    hcai_status._warn_low_balance(client, "C1", "")
    client.chat_postMessage.assert_called_once_with(
        channel="C1", thread_ts=None, text=hcai_status.WARNING_TEXT,
    )


def test_warn_low_balance_does_nothing_when_balance_is_healthy(monkeypatch):
    monkeypatch.setattr(hcai_status, "fetch_status", lambda: {"balanceRemaining": 5.0})
    client = Mock()
    hcai_status._warn_low_balance(client, "C1", "1.1")
    client.chat_postMessage.assert_not_called()


def test_warn_low_balance_does_nothing_when_status_check_fails(monkeypatch):
    monkeypatch.setattr(hcai_status, "fetch_status", lambda: None)
    client = Mock()
    hcai_status._warn_low_balance(client, "C1", "1.1")
    client.chat_postMessage.assert_not_called()


def test_warn_low_balance_does_nothing_when_balance_field_missing_or_wrong_type(monkeypatch):
    client = Mock()
    monkeypatch.setattr(hcai_status, "fetch_status", lambda: {"status": "down"})
    hcai_status._warn_low_balance(client, "C1", "1.1")
    monkeypatch.setattr(hcai_status, "fetch_status", lambda: {"balanceRemaining": "not a number"})
    hcai_status._warn_low_balance(client, "C1", "1.1")
    client.chat_postMessage.assert_not_called()


def test_warn_low_balance_does_not_repost_within_the_cooldown(monkeypatch):
    monkeypatch.setattr(hcai_status, "fetch_status", lambda: {"balanceRemaining": -1})
    client = Mock()
    hcai_status._warn_low_balance(client, "C1", "1.1")
    hcai_status._warn_low_balance(client, "C1", "1.1")
    assert client.chat_postMessage.call_count == 1


def test_warn_low_balance_reposts_once_the_cooldown_has_elapsed(monkeypatch):
    monkeypatch.setattr(hcai_status, "fetch_status", lambda: {"balanceRemaining": -1})
    client = Mock()
    hcai_status._warn_low_balance(client, "C1", "1.1")
    hcai_status._last_warned[("C1", "1.1")] = time.time() - hcai_status._REWARN_AFTER_SECONDS - 1
    hcai_status._warn_low_balance(client, "C1", "1.1")
    assert client.chat_postMessage.call_count == 2


def test_warn_low_balance_cooldown_is_per_thread():
    hcai_status._last_warned[("C1", "1.1")] = time.time()
    assert hcai_status._should_warn("C1", "2.2") is True


def test_warn_low_balance_swallows_post_failure(monkeypatch):
    monkeypatch.setattr(hcai_status, "fetch_status", lambda: {"balanceRemaining": -1})
    client = Mock()
    client.chat_postMessage.side_effect = Exception("slack down too")
    # Must not raise.
    hcai_status._warn_low_balance(client, "C1", "1.1")


def test_check_and_warn_async_runs_on_a_background_thread(monkeypatch):
    calls = []
    monkeypatch.setattr(
        hcai_status, "_warn_low_balance",
        lambda client, channel_id, thread_ts: calls.append((client, channel_id, thread_ts)),
    )
    client = Mock()
    hcai_status.check_and_warn_async(client, "C1", "1.1")
    # Give the daemon thread a moment to run.
    for _ in range(50):
        if calls:
            break
        time.sleep(0.02)
    assert calls == [(client, "C1", "1.1")]
