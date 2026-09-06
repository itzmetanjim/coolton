from unittest.mock import Mock

from pydantic_ai.messages import ModelResponse, UserPromptPart

from thread_context.thread_history import build_thread_context


def _client_with_messages(messages):
    client = Mock()
    client.conversations_replies.return_value = {"ok": True, "messages": messages, "response_metadata": {}}
    client.users_info.return_value = {"ok": True, "user": {"profile": {"display_name": "Alice"}}}
    return client


def test_build_thread_context_excludes_double_hash_messages():
    """"##"-prefixed messages are never processed or responded to (see
    listeners/events/message.py / app_mentioned.py) — a private aside dropped into a
    thread must not leak into the model's context the first time a real message pulls
    this thread's history in."""
    client = _client_with_messages([
        {"ts": "1.0", "user": "U1", "text": "hello there"},
        {"ts": "2.0", "user": "U1", "text": "## just a private note, ignore this"},
        {"ts": "3.0", "user": "U1", "text": "  ##also private (leading whitespace)"},
        {"ts": "4.0", "user": "U1", "text": "actually useful message"},
    ])

    history = build_thread_context(client, "C1", "1.0", exclude_ts=None)

    assert history is not None
    texts = [m.parts[0].content for m in history if isinstance(m.parts[0], UserPromptPart)]
    assert not any("private note" in t or "also private" in t for t in texts)
    assert any("hello there" in t for t in texts)
    assert any("actually useful message" in t for t in texts)
    assert len(texts) == 2


def test_build_thread_context_returns_none_when_everything_is_double_hash():
    client = _client_with_messages([
        {"ts": "1.0", "user": "U1", "text": "## note one"},
        {"ts": "2.0", "user": "U1", "text": "## note two"},
    ])

    assert build_thread_context(client, "C1", "1.0", exclude_ts=None) is None


def test_build_thread_context_still_excludes_the_triggering_message():
    client = _client_with_messages([
        {"ts": "1.0", "user": "U1", "text": "earlier message"},
        {"ts": "2.0", "user": "U1", "text": "the message that triggered this turn"},
    ])

    history = build_thread_context(client, "C1", "1.0", exclude_ts="2.0")

    texts = [m.parts[0].content for m in history if isinstance(m.parts[0], UserPromptPart)]
    assert len(texts) == 1
    assert "earlier message" in texts[0]


def test_build_thread_context_does_not_attribute_other_bots_to_coolton(monkeypatch):
    """A different Slack app's messages must never become a ModelResponse — that
    role tells the model "you said this," and doing so for a third-party bot made
    coolton treat that bot's outputs as its own and mimic/continue them the first
    time it was mentioned in a thread it hadn't seen before."""
    monkeypatch.setenv("COOLTON_BOT_ID", "UCOOLTON")
    client = _client_with_messages([
        {"ts": "1.0", "user": "U1", "text": "hey can someone deploy this"},
        {
            "ts": "2.0",
            "user": "UOTHERBOT",
            "bot_id": "BOTHER",
            "username": "Deploybot",
            "text": "Deployment started for build #42",
        },
    ])

    history = build_thread_context(client, "C1", "1.0", exclude_ts=None)

    assert history is not None
    assert not any(isinstance(m, ModelResponse) for m in history)
    texts = [m.parts[0].content for m in history if isinstance(m.parts[0], UserPromptPart)]
    assert any("Deploybot" in t and "Deployment started" in t for t in texts)


def test_build_thread_context_attributes_coolton_own_messages_as_response(monkeypatch):
    monkeypatch.setenv("COOLTON_BOT_ID", "UCOOLTON")
    client = _client_with_messages([
        {
            "ts": "1.0",
            "user": "UCOOLTON",
            "bot_id": "BCOOLTON",
            "text": "sure, on it",
        },
    ])

    history = build_thread_context(client, "C1", "1.0", exclude_ts=None)

    assert history is not None
    assert len(history) == 1
    assert isinstance(history[0], ModelResponse)
    assert history[0].parts[0].content == "sure, on it"
