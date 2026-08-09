from unittest.mock import Mock

from agent.tools.slack_info import (
    _resolve_channel_id,
    _resolve_user_id,
    _strip_mention,
    get_channel_info,
    get_user_info,
)


def test_strip_mention_user():
    assert _strip_mention("<@U0123456|bob>") == "U0123456"
    assert _strip_mention("<@U0123456>") == "U0123456"


def test_strip_mention_channel():
    assert _strip_mention("<#C0123456|general>") == "C0123456"
    assert _strip_mention("#general") == "#general"


def test_resolve_user_id_already_an_id():
    assert _resolve_user_id("U0123456") == "U0123456"


def test_resolve_user_id_by_mention(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-token")
    monkeypatch.setenv("SLACK_TEAM_ID", "T123")
    seen = {}

    def fake_get(url, **kwargs):
        seen["params"] = kwargs.get("params")
        return Mock(json=lambda: {
            "ok": True,
            "members": [{"id": "U0123456", "name": "bob", "profile": {"display_name": "Bobby"}}],
        })

    monkeypatch.setattr("agent.tools.slack_info.requests.get", fake_get)
    assert _resolve_user_id("<@bob>") == "U0123456"
    assert seen["params"]["team_id"] == "T123"


def test_resolve_user_id_by_name(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-token")
    monkeypatch.setenv("SLACK_TEAM_ID", "T123")

    def fake_get(url, **kwargs):
        return Mock(json=lambda: {
            "ok": True,
            "members": [{"id": "U99", "name": "bob", "profile": {"email": "bob@example.com"}}],
        })

    monkeypatch.setattr("agent.tools.slack_info.requests.get", fake_get)
    assert _resolve_user_id("bob@example.com") == "U99"


def test_resolve_channel_id_by_name(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-token")

    def fake_get(url, **kwargs):
        return Mock(json=lambda: {
            "ok": True,
            "channels": [{"id": "C99", "name": "general"}],
        })

    monkeypatch.setattr("agent.tools.slack_info.requests.get", fake_get)
    assert _resolve_channel_id("#general") == "C99"
    assert _resolve_channel_id("C99") == "C99"


def test_get_user_info_passes_team_id(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-token")
    monkeypatch.setenv("SLACK_TEAM_ID", "T123")
    seen = {}

    def fake_get(url, **kwargs):
        seen["params"] = kwargs.get("params")
        return Mock(json=lambda: {
            "ok": True,
            "user": {
                "id": "U0123456",
                "name": "bob",
                "profile": {"display_name": "Bobby", "real_name": "Bob Smith", "pronouns": "he/him"},
            },
        })

    monkeypatch.setattr("agent.tools.slack_info.requests.get", fake_get)
    result = get_user_info("<@U0123456|bob>")
    assert seen["params"]["user"] == "U0123456"
    assert seen["params"]["team_id"] == "T123"
    assert "Bobby" in result
    assert "he/him" in result


def test_get_user_info_not_found_guidance(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-token")

    def fake_get(url, **kwargs):
        return Mock(json=lambda: {"ok": False, "error": "user_not_found"})

    monkeypatch.setattr("agent.tools.slack_info.requests.get", fake_get)
    result = get_user_info("U9999999999")
    assert "user_not_found" in result
    assert "don't guess ids" in result


def test_get_channel_info_team_access_guidance(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-token")

    def fake_get(url, **kwargs):
        return Mock(json=lambda: {"ok": False, "error": "team_access_not_granted"})

    monkeypatch.setattr("agent.tools.slack_info.requests.get", fake_get)
    result = get_channel_info("C9999999999")
    assert "team_access_not_granted" in result
    assert "don't guess ids" in result
