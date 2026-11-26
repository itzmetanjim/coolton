from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from web import auth, settings
from web.server import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("COOLTON_WEB_SECRET", "test-secret")
    browser = TestClient(app)
    browser.cookies.set(auth.SESSION_COOKIE, auth.create_session_token("U1"))
    return browser


@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/settings", None),
    ("post", "/api/settings/policy/opt-in", {"join": False}),
    ("post", "/api/settings/policy/opt-out", None),
    ("post", "/api/settings/byok", {"name": "x", "base_url": "https://example.com"}),
    ("patch", "/api/settings/byok/ep_1", {}),
    ("delete", "/api/settings/byok/ep_1", None),
    ("post", "/api/settings/byok/text-default", {"endpoint_id": None}),
    ("post", "/api/settings/byok/image-default", {"endpoint_id": None}),
    ("put", "/api/settings/instructions", {"text": ""}),
    ("post", "/api/settings/fallback-cache/clear", None),
    ("post", "/api/settings/providers/test", None),
    ("post", "/api/settings/mcp", {"name": "x", "url": "https://example.com"}),
    ("delete", "/api/settings/mcp/mcp_1", None),
])
def test_settings_routes_require_a_signed_in_user(method, path, body):
    browser = TestClient(app)
    response = getattr(browser, method)(path, json=body) if body is not None else getattr(browser, method)(path)
    assert response.status_code == 401


def test_get_settings_returns_only_the_signed_in_users_reminders(client, monkeypatch):
    from importlib import import_module
    from agent import admin_alerts, byok_store, mcp_server_store, policy_consent, scheduler
    from listeners.actions import instructions_actions

    agent_module = import_module("agent.agent")
    monkeypatch.setattr(agent_module, "_get_user_display_info", lambda uid: ("Alice", None))
    monkeypatch.setattr(admin_alerts, "is_admin", lambda uid: False)
    monkeypatch.setattr(byok_store, "get_user_endpoints", lambda uid: [{"id": "ep_1", "name": "mine"}])
    monkeypatch.setattr(byok_store, "get_text_endpoint_id", lambda uid: "ep_1")
    monkeypatch.setattr(byok_store, "get_image_endpoint_id", lambda uid: None)
    monkeypatch.setattr(mcp_server_store, "get_user_servers", lambda uid: [{"id": "mcp_1"}])
    monkeypatch.setattr(policy_consent, "has_consent", lambda uid: True)
    monkeypatch.setattr(scheduler, "_load_reminders", lambda: {"reminders": [
        {"user_id": "U1", "id": 1}, {"user_id": "U2", "id": 2},
    ]})
    monkeypatch.setattr(instructions_actions, "get_user_instructions", lambda uid: "be concise")

    response = client.get("/api/settings")

    assert response.status_code == 200
    data = response.json()
    assert data["display_name"] == "Alice"
    assert data["policy"]["consent"] is True
    assert data["instructions"] == "be concise"
    assert data["reminders"] == [{"user_id": "U1", "id": 1}]
    assert data["endpoints"] == [{"id": "ep_1", "name": "mine"}]


def test_policy_opt_in_records_consent_even_if_channel_invite_fails(client, monkeypatch):
    recorded = []

    class Slack:
        def conversations_invite(self, **kwargs):
            raise RuntimeError("invite failed")

    monkeypatch.setattr(settings, "_bot_client", lambda: Slack())
    monkeypatch.setattr("agent.policy_consent.record_consent",
                        lambda user_id, joined_policy_channel: recorded.append((user_id, joined_policy_channel)))

    response = client.post("/api/settings/policy/opt-in", json={"join": True})

    assert response.status_code == 200
    assert recorded == [("U1", True)]


def test_provider_tests_are_admin_only(client, monkeypatch):
    from agent import admin_alerts, provider_config

    monkeypatch.setattr(admin_alerts, "is_admin", lambda uid: False)
    denied = client.post("/api/settings/providers/test")
    assert denied.status_code == 403

    monkeypatch.setattr(admin_alerts, "is_admin", lambda uid: uid == "U1")
    monkeypatch.setattr(provider_config, "build_provider_order", lambda uid: [])
    allowed = client.post("/api/settings/providers/test")
    assert allowed.status_code == 200
    assert allowed.json() == {"results": []}


def test_byok_patch_cannot_update_another_users_endpoint(client, monkeypatch):
    from agent import byok_store
    looked_up = []
    monkeypatch.setattr(byok_store, "get_endpoint_decrypted",
                        lambda user_id, ep_id: looked_up.append((user_id, ep_id)) or None)

    response = client.patch("/api/settings/byok/ep_other", json={"name": "mine"})

    assert response.status_code == 404
    assert looked_up == [("U1", "ep_other")]


def test_policy_opt_out_revokes_only_the_signed_in_users_consent(client, monkeypatch):
    revoked = []
    monkeypatch.setattr("agent.policy_consent.revoke_consent", revoked.append)

    response = client.post("/api/settings/policy/opt-out")

    assert response.status_code == 200
    assert revoked == ["U1"]


def test_byok_api_passes_user_identity_and_never_returns_the_key(client, monkeypatch):
    from agent import byok_store
    captured = {}
    monkeypatch.setattr(byok_store, "add_endpoint",
                        lambda *args: captured.update(args=args) or "ep_1")

    response = client.post("/api/settings/byok", json={
        "name": "Personal", "base_url": "https://api.example.com/v1",
        "api_key": "secret-key", "model": "model-x",
    })

    assert response.status_code == 200
    assert response.json() == {"id": "ep_1"}
    assert captured["args"] == ("U1", "Personal", "https://api.example.com/v1", "secret-key", "model-x")
    assert "secret-key" not in response.text


def test_mcp_api_probes_and_saves_for_the_signed_in_user(client, monkeypatch):
    from agent import mcp_server_store
    captured = {}
    monkeypatch.setattr(mcp_server_store, "parse_headers", lambda text: {"X-Key": "secret"})
    monkeypatch.setattr(mcp_server_store, "probe_server",
                        lambda *args: (True, "connected", "http"))
    monkeypatch.setattr(mcp_server_store, "add_server",
                        lambda *args: captured.update(args=args) or "mcp_1")

    response = client.post("/api/settings/mcp", json={
        "name": "My server", "url": "https://mcp.example.com", "token": "secret-token",
        "transport": "auto", "headers": "X-Key: secret",
    })

    assert response.status_code == 200
    assert response.json() == {"id": "mcp_1", "detail": "connected"}
    assert captured["args"] == ("U1", "My server", "https://mcp.example.com", "secret-token", "http", {"X-Key": "secret"})


def test_fallback_cache_clear_starts_a_background_refresh(client, monkeypatch):
    from agent import fallback_cache, provider_probe

    calls = []
    thread = {}

    class FakeThread:
        def __init__(self, target, name, daemon):
            thread.update(target=target, name=name, daemon=daemon)

        def start(self):
            calls.append("thread started")

    monkeypatch.setattr(settings.threading, "Thread", FakeThread)
    monkeypatch.setattr(fallback_cache, "clear_cache", lambda: calls.append("cache cleared"))
    monkeypatch.setattr(provider_probe, "refresh_fallback_cache", lambda: calls.append("refreshed"))

    response = client.post("/api/settings/fallback-cache/clear")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert calls == ["cache cleared", "thread started"]
    assert thread["name"] == "web-fallback-cache-refresh"
    assert thread["daemon"] is True
    thread["target"]()
    assert calls[-1] == "refreshed"
