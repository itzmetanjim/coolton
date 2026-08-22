from types import SimpleNamespace
from unittest.mock import Mock

from agent.platform import PlatformAdapter
from agent.platforms.slack import SlackPlatform


def test_slack_adapter_preserves_user_message_shape():
    deps = SimpleNamespace(user_id="U123", channel_id="C123", thread_ts="1.2", message_ts="1.3")
    platform = SlackPlatform()
    assert isinstance(platform, PlatformAdapter)
    assert platform.format_user_message("hello", deps) == "U123 (U123):\nhello"


def test_slack_adapter_context_contains_legacy_fields():
    deps = SimpleNamespace(user_id="U123", channel_id="C123", thread_ts="1.2", message_ts="1.3", user_token=None)
    context = SlackPlatform().build_context_prompt(deps, "model", False)
    assert "channel_id: `C123`" in context
    assert "thread_ts: `1.2`" in context
    assert "Your user_id (the HUMAN who messaged you): `U123`" in context


def test_toolsets_includes_users_registered_mcp_servers(monkeypatch):
    deps = SimpleNamespace(user_id="U123", user_token="xoxp-test")
    monkeypatch.setattr(
        "agent.mcp_server_store.get_user_servers",
        lambda user_id: [{"id": "mcp_abc", "name": "My Notion", "url": "https://mcp.example.com/mcp"}],
    )
    monkeypatch.setattr(
        "agent.mcp_server_store.get_server_decrypted",
        lambda user_id, server_id: {"id": "mcp_abc", "name": "My Notion", "url": "https://mcp.example.com/mcp", "token": "tok"},
    )
    monkeypatch.setattr("agent.platforms.slack.MCPToolset", lambda transport, **kwargs: SimpleNamespace(kwargs=kwargs))

    toolsets = SlackPlatform().toolsets(deps)

    # one for the official Slack MCP server, one for the user's registered server
    assert len(toolsets) == 2
    assert toolsets[1].kwargs["id"] == "user_mcp_mcp_abc"


def test_toolsets_skips_user_mcp_when_none_registered(monkeypatch):
    deps = SimpleNamespace(user_id="U123", user_token="xoxp-test")
    monkeypatch.setattr("agent.mcp_server_store.get_user_servers", lambda user_id: [])
    monkeypatch.setattr("agent.platforms.slack.MCPToolset", lambda transport, **kwargs: SimpleNamespace(kwargs=kwargs))

    toolsets = SlackPlatform().toolsets(deps)
    assert len(toolsets) == 1


def test_toolsets_one_broken_user_server_does_not_break_others(monkeypatch):
    deps = SimpleNamespace(user_id="U123", user_token="xoxp-test")
    monkeypatch.setattr(
        "agent.mcp_server_store.get_user_servers",
        lambda user_id: [
            {"id": "mcp_bad", "name": "Bad", "url": "https://bad.example.com/mcp"},
            {"id": "mcp_good", "name": "Good", "url": "https://good.example.com/mcp"},
        ],
    )
    monkeypatch.setattr(
        "agent.mcp_server_store.get_server_decrypted",
        lambda user_id, server_id: {"id": server_id, "name": server_id, "url": "https://x/mcp", "token": None},
    )

    def fake_mcp_toolset(transport, **kwargs):
        if kwargs.get("id") == "user_mcp_mcp_bad":
            raise RuntimeError("boom")
        return SimpleNamespace(kwargs=kwargs)

    monkeypatch.setattr("agent.platforms.slack.MCPToolset", fake_mcp_toolset)

    toolsets = SlackPlatform().toolsets(deps)
    # official Slack MCP toolset + the one good user server, bad one dropped
    assert len(toolsets) == 2
    assert toolsets[1].kwargs["id"] == "user_mcp_mcp_good"


def test_toolsets_no_user_id_skips_user_mcp_lookup(monkeypatch):
    deps = SimpleNamespace(user_id=None, user_token="xoxp-test")
    lookup_mock = Mock()
    monkeypatch.setattr("agent.mcp_server_store.get_user_servers", lookup_mock)
    monkeypatch.setattr("agent.platforms.slack.MCPToolset", lambda transport, **kwargs: SimpleNamespace(kwargs=kwargs))

    SlackPlatform().toolsets(deps)
    lookup_mock.assert_not_called()
