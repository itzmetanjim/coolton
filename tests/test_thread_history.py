from unittest.mock import Mock

from pydantic_ai.messages import UserPromptPart

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
