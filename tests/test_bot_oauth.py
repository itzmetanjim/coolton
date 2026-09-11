from unittest.mock import patch

import pytest

from web import bot_oauth
from agent.tools import slack_bot_deploy as sbd


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("COOLTON_WEB_SECRET", "test-secret")


def _seed(monkeypatch, app_id="A123", client_id="cid", client_secret="csec", bot_token=None):
    record = {"app_id": app_id, "credentials": {"client_id": client_id, "client_secret": client_secret}}
    if bot_token:
        record["bot_token"] = bot_token
    monkeypatch.setattr(sbd, "_load", lambda: {app_id: record})


def test_rejects_missing_state():
    response = bot_oauth.bot_oauth_callback(code="abc", state="")
    assert response.status_code == 400
    assert "expired or invalid" in response.body.decode()


def test_rejects_garbage_state():
    response = bot_oauth.bot_oauth_callback(code="abc", state="not-a-real-state")
    assert response.status_code == 400


def test_rejects_state_for_unknown_app(monkeypatch):
    monkeypatch.setattr(sbd, "_load", lambda: {})
    state = sbd.sign_install_state("A123")
    response = bot_oauth.bot_oauth_callback(code="abc", state=state)
    assert response.status_code == 404
    assert "Unknown app" in response.body.decode()


def test_rejects_missing_code(monkeypatch):
    _seed(monkeypatch)
    state = sbd.sign_install_state("A123")
    response = bot_oauth.bot_oauth_callback(code="", state=state)
    assert response.status_code == 400
    assert "Missing code" in response.body.decode()


def test_rejects_when_app_has_no_stored_credentials(monkeypatch):
    monkeypatch.setattr(sbd, "_load", lambda: {"A123": {"app_id": "A123", "credentials": {}}})
    state = sbd.sign_install_state("A123")
    response = bot_oauth.bot_oauth_callback(code="abc", state=state)
    assert response.status_code == 500
    assert "OAuth credentials" in response.body.decode()


def test_surfaces_oauth_exchange_failure(monkeypatch):
    _seed(monkeypatch)
    state = sbd.sign_install_state("A123")
    with patch.object(bot_oauth, "_exchange_code", return_value={"ok": False, "error": "invalid_code"}):
        response = bot_oauth.bot_oauth_callback(code="abc", state=state)
    assert response.status_code == 400
    assert "invalid_code" in response.body.decode()


def test_rejects_a_response_with_no_usable_bot_token(monkeypatch):
    _seed(monkeypatch)
    state = sbd.sign_install_state("A123")
    with patch.object(bot_oauth, "_exchange_code", return_value={"ok": True, "access_token": "xoxp-user-token"}):
        response = bot_oauth.bot_oauth_callback(code="abc", state=state)
    assert response.status_code == 400
    assert "no bot token" in response.body.decode() or "didn&#x27;t return" in response.body.decode() or "didn't return" in response.body.decode()


def test_exchange_network_failure_reported_not_swallowed(monkeypatch):
    _seed(monkeypatch)
    state = sbd.sign_install_state("A123")
    with patch.object(bot_oauth, "_exchange_code", side_effect=RuntimeError("boom")):
        response = bot_oauth.bot_oauth_callback(code="abc", state=state)
    assert response.status_code == 502


def test_success_registers_the_captured_bot_token(monkeypatch):
    saved = {}
    _seed(monkeypatch)
    monkeypatch.setattr(sbd, "_save", lambda data: saved.update(data))
    state = sbd.sign_install_state("A123")
    with patch.object(bot_oauth, "_exchange_code", return_value={"ok": True, "access_token": "xoxb-captured"}):
        response = bot_oauth.bot_oauth_callback(code="abc", state=state)
    assert response.status_code == 200
    assert "installed" in response.body.decode().lower()
    assert saved["A123"]["bot_token"] == "xoxb-captured"


def test_exchange_redirect_uri_matches_oauth_callback_url(monkeypatch):
    """Slack rejects a token exchange whose redirect_uri doesn't exactly match
    the one used in the /authorize request — the exchange must reuse
    oauth_callback_url(), not something else."""
    captured = {}

    class _FakeResp:
        def json(self):
            return {"ok": True, "access_token": "xoxb-x"}

    def fake_post(url, data, timeout):
        captured.update(data)
        return _FakeResp()

    monkeypatch.setattr(bot_oauth.requests, "post", fake_post)
    result = bot_oauth._exchange_code("abc", "cid", "csec")
    assert result == {"ok": True, "access_token": "xoxb-x"}
    assert captured["redirect_uri"] == sbd.oauth_callback_url()
