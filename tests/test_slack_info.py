from unittest.mock import Mock

from agent.tools.slack_info import (
    _resolve_channel_id,
    _resolve_user_id,
    _strip_mention,
    get_channel_info,
    get_user_info,
    post_message_to_target,
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


# ---------------------------------------------------------------------------
# post_message_to_target — channel/DM ACL (only current channel, or the
# requester's own DM; never an arbitrary channel or someone else's DM)
# ---------------------------------------------------------------------------


def test_post_message_refuses_other_channel(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-token")
    posted = []
    monkeypatch.setattr(
        "agent.tools.slack_info.requests.post",
        lambda *a, **k: posted.append(1) or Mock(json=lambda: {"ok": True}),
    )
    result = post_message_to_target(
        channel_id="C_OTHER", text="hi", current_channel="C_CURRENT", from_user="U1",
    )
    assert "only post to the channel you are currently in" in result
    assert not posted


def test_post_message_allows_current_channel(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-token")
    posted = []

    def fake_post(url, **kwargs):
        posted.append(kwargs.get("json"))
        return Mock(json=lambda: {"ok": True})

    monkeypatch.setattr("agent.tools.slack_info.requests.post", fake_post)
    result = post_message_to_target(
        channel_id="C_CURRENT", text="hi", current_channel="C_CURRENT", from_user="U1",
    )
    assert "Message posted" in result
    assert posted[0]["channel"] == "C_CURRENT"


def test_post_message_refuses_dm_not_belonging_to_requester(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-token")
    monkeypatch.setattr(
        "agent.tools.slack_info.requests.get",
        lambda *a, **k: Mock(json=lambda: {"ok": True, "channel": {"user": "U_OTHER"}}),
    )
    posted = []
    monkeypatch.setattr(
        "agent.tools.slack_info.requests.post",
        lambda *a, **k: posted.append(1) or Mock(json=lambda: {"ok": True}),
    )
    result = post_message_to_target(
        channel_id="D123", text="hi", current_channel="C_CURRENT", from_user="U1",
    )
    assert "only post to a DM with the user who asked" in result
    assert not posted


def test_post_message_allows_own_dm(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-token")
    monkeypatch.setattr(
        "agent.tools.slack_info.requests.get",
        lambda *a, **k: Mock(json=lambda: {"ok": True, "channel": {"user": "U1"}}),
    )
    posted = []

    def fake_post(url, **kwargs):
        posted.append(kwargs.get("json"))
        return Mock(json=lambda: {"ok": True})

    monkeypatch.setattr("agent.tools.slack_info.requests.post", fake_post)
    result = post_message_to_target(
        channel_id="D123", text="hi", current_channel="C_CURRENT", from_user="U1",
    )
    assert "Message posted" in result
    assert posted[0]["channel"] == "D123"


def test_post_message_refuses_dm_when_lookup_fails(monkeypatch):
    """conversations.info failing must fail closed, not silently allow the DM."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-token")
    monkeypatch.setattr(
        "agent.tools.slack_info.requests.get",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network error")),
    )
    posted = []
    monkeypatch.setattr(
        "agent.tools.slack_info.requests.post",
        lambda *a, **k: posted.append(1) or Mock(json=lambda: {"ok": True}),
    )
    result = post_message_to_target(
        channel_id="D123", text="hi", current_channel="C_CURRENT", from_user="U1",
    )
    assert "Could not verify this DM belongs to you" in result
    assert not posted
