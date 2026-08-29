from unittest.mock import Mock

from agent.tools.slack_search import _join_channel_as_bot, read_conversation_history


def _info(ok=True, **channel_fields):
    return Mock(json=lambda: {"ok": ok, "channel": channel_fields} if ok else {"ok": False, "error": "channel_not_found"})


def test_join_channel_as_bot_success_for_a_plain_public_channel(monkeypatch):
    monkeypatch.setattr("agent.tools.slack_search.requests.get", lambda url, **k: _info())
    monkeypatch.setattr(
        "agent.tools.slack_search.requests.post",
        lambda url, **k: Mock(json=lambda: {"ok": True}),
    )
    assert _join_channel_as_bot("xoxb-token", "C1") is True


def test_join_channel_as_bot_failure_from_the_join_call(monkeypatch):
    monkeypatch.setattr("agent.tools.slack_search.requests.get", lambda url, **k: _info())
    monkeypatch.setattr(
        "agent.tools.slack_search.requests.post",
        lambda url, **k: Mock(json=lambda: {"ok": False, "error": "is_archived"}),
    )
    assert _join_channel_as_bot("xoxb-token", "C1") is False


def test_join_channel_as_bot_swallows_exceptions(monkeypatch):
    def boom(url, **k):
        raise RuntimeError("network error")

    monkeypatch.setattr("agent.tools.slack_search.requests.get", boom)
    assert _join_channel_as_bot("xoxb-token", "C1") is False


def test_join_channel_as_bot_never_attempts_slack_connect_channels(monkeypatch):
    """Slack Connect channels need a human invite from the other side — never try to
    auto-join one, don't just rely on conversations.join failing cleanly for it."""
    monkeypatch.setattr("agent.tools.slack_search.requests.get", lambda url, **k: _info(is_ext_shared=True))
    joined = []
    monkeypatch.setattr(
        "agent.tools.slack_search.requests.post",
        lambda url, **k: joined.append(1) or Mock(json=lambda: {"ok": True}),
    )
    assert _join_channel_as_bot("xoxb-token", "C1") is False
    assert joined == []


def test_join_channel_as_bot_never_attempts_org_shared_channels(monkeypatch):
    monkeypatch.setattr("agent.tools.slack_search.requests.get", lambda url, **k: _info(is_org_shared=True))
    joined = []
    monkeypatch.setattr(
        "agent.tools.slack_search.requests.post",
        lambda url, **k: joined.append(1) or Mock(json=lambda: {"ok": True}),
    )
    assert _join_channel_as_bot("xoxb-token", "C1") is False
    assert joined == []


def test_join_channel_as_bot_never_attempts_private_channels(monkeypatch):
    monkeypatch.setattr("agent.tools.slack_search.requests.get", lambda url, **k: _info(is_private=True))
    joined = []
    monkeypatch.setattr(
        "agent.tools.slack_search.requests.post",
        lambda url, **k: joined.append(1) or Mock(json=lambda: {"ok": True}),
    )
    assert _join_channel_as_bot("xoxb-token", "C1") is False
    assert joined == []


def test_join_channel_as_bot_fails_closed_when_info_lookup_fails(monkeypatch):
    monkeypatch.setattr("agent.tools.slack_search.requests.get", lambda url, **k: _info(ok=False))
    joined = []
    monkeypatch.setattr(
        "agent.tools.slack_search.requests.post",
        lambda url, **k: joined.append(1) or Mock(json=lambda: {"ok": True}),
    )
    assert _join_channel_as_bot("xoxb-token", "C1") is False
    assert joined == []


def _route_get(history_response, info_response=None):
    """Fake requests.get that routes by endpoint: conversations.info (used inside
    _join_channel_as_bot) vs conversations.history/replies (the actual read)."""
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "conversations.info" in url:
            return info_response or _info()
        return history_response(len([c for c in calls if "conversations.info" not in c]))

    return fake_get, calls


def test_read_conversation_history_self_heals_not_in_channel(monkeypatch):
    """The bot itself (a separate identity from cooltonUser — inviting cooltonUser via
    invite_coolton_user_to_channel does nothing for this) wasn't a member of a public
    channel: join as the bot and retry once instead of surfacing not_in_channel."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-token")
    joined = []

    def history_response(attempt):
        if attempt == 1:
            return Mock(json=lambda: {"ok": False, "error": "not_in_channel"})
        return Mock(json=lambda: {"ok": True, "messages": [{"ts": "1.0", "user": "U1", "text": "hi"}]})

    fake_get, calls = _route_get(history_response)

    def fake_post(url, **kwargs):
        joined.append(kwargs.get("json"))
        return Mock(json=lambda: {"ok": True})

    monkeypatch.setattr("agent.tools.slack_search.requests.get", fake_get)
    monkeypatch.setattr("agent.tools.slack_search.requests.post", fake_post)

    result = read_conversation_history("C1", current_channel_id="C1")

    assert joined == [{"channel": "C1"}]
    assert "hi" in result


def test_read_conversation_history_does_not_retry_other_errors(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-token")
    joined = []

    fake_get, calls = _route_get(lambda attempt: Mock(json=lambda: {"ok": False, "error": "invalid_cursor"}))

    def fake_post(url, **kwargs):
        joined.append(kwargs.get("json"))
        return Mock(json=lambda: {"ok": True})

    monkeypatch.setattr("agent.tools.slack_search.requests.get", fake_get)
    monkeypatch.setattr("agent.tools.slack_search.requests.post", fake_post)

    result = read_conversation_history("C1", current_channel_id="C1")

    assert joined == []  # never even checked conversations.info — wrong error to self-heal
    assert "invalid_cursor" in result


def test_read_conversation_history_reports_the_original_error_when_join_fails(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-token")

    fake_get, calls = _route_get(lambda attempt: Mock(json=lambda: {"ok": False, "error": "not_in_channel"}))
    monkeypatch.setattr("agent.tools.slack_search.requests.get", fake_get)
    monkeypatch.setattr(
        "agent.tools.slack_search.requests.post",
        lambda url, **k: Mock(json=lambda: {"ok": False, "error": "channel_not_found"}),
    )

    result = read_conversation_history("C1", current_channel_id="C1")

    assert "not_in_channel" in result


def test_read_conversation_history_reports_the_original_error_for_slack_connect(monkeypatch):
    """The retry-after-join path must never crash or hang for a Slack Connect channel —
    it should just fall back to the original not_in_channel error cleanly."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-token")

    fake_get, calls = _route_get(
        lambda attempt: Mock(json=lambda: {"ok": False, "error": "not_in_channel"}),
        info_response=_info(is_ext_shared=True),
    )
    monkeypatch.setattr("agent.tools.slack_search.requests.get", fake_get)
    posted = []
    monkeypatch.setattr(
        "agent.tools.slack_search.requests.post",
        lambda url, **k: posted.append(1) or Mock(json=lambda: {"ok": True}),
    )

    result = read_conversation_history("C1", current_channel_id="C1")

    assert posted == []  # never attempted conversations.join for a Connect channel
    assert "not_in_channel" in result
