from datetime import datetime, timezone

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from thread_context.store import ConversationStore


def _history(*texts: str) -> list:
    parts = [UserPromptPart(content=t, timestamp=datetime.now(timezone.utc)) for t in texts]
    return [ModelRequest(parts=parts)]


def test_set_and_get_roundtrip(tmp_path):
    store = ConversationStore(file_path=str(tmp_path / "conversations.json"))
    history = _history("hello", "world")
    store.set_history("C1", "1.1", history)

    loaded = store.get_history("C1", "1.1")
    assert loaded is not None
    assert len(loaded) == 1
    parts = loaded[0].parts
    assert [p.content for p in parts] == ["hello", "world"]


def test_get_unknown_thread_returns_none(tmp_path):
    store = ConversationStore(file_path=str(tmp_path / "conversations.json"))
    assert store.get_history("C1", "1.1") is None


def test_ttl_expiry_removes_history(tmp_path):
    store = ConversationStore(file_path=str(tmp_path / "conversations.json"), ttl_seconds=-1)
    store.set_history("C1", "1.1", _history("hi"))
    assert store.get_history("C1", "1.1") is None


def test_max_conversations_evicts_oldest(tmp_path):
    store = ConversationStore(file_path=str(tmp_path / "conversations.json"), max_conversations=2)
    store.set_history("C1", "1.1", _history("a"))
    store.set_history("C2", "2.2", _history("b"))
    store.set_history("C3", "3.3", _history("c"))

    assert store.get_history("C1", "1.1") is None
    assert store.get_history("C2", "2.2") is not None
    assert store.get_history("C3", "3.3") is not None


def test_persists_and_reloads_from_disk(tmp_path):
    path = str(tmp_path / "conversations.json")
    store = ConversationStore(file_path=path)
    store.set_history("C1", "1.1", _history("persisted"))

    reloaded = ConversationStore(file_path=path)
    loaded = reloaded.get_history("C1", "1.1")
    assert loaded is not None
    assert loaded[0].parts[0].content == "persisted"


def test_malformed_entries_skipped_on_load(tmp_path):
    path = str(tmp_path / "conversations.json")
    # one valid-looking entry, one missing timestamp, one with bad messages
    data = {
        "C1:1.1": {"timestamp": datetime.now().timestamp(), "messages": []},
        "C2:2.2": {"nope": True},
        "C3:3.3": {"timestamp": datetime.now().timestamp(), "messages": "not-a-list"},
    }
    tmp_path.joinpath("conversations.json").write_text(
        __import__("json").dumps(data)
    )

    store = ConversationStore(file_path=path)
    assert store.get_history("C1", "1.1") == []
    assert store.get_history("C2", "2.2") is None
    assert store.get_history("C3", "3.3") is None


def test_corrupt_json_on_disk_no_crash(tmp_path):
    path = str(tmp_path / "conversations.json")
    tmp_path.joinpath("conversations.json").write_text("{total garbage")
    store = ConversationStore(file_path=path)
    assert store.get_history("C1", "1.1") is None
    # still writable afterwards
    store.set_history("C1", "1.1", _history("recovered"))
    assert store.get_history("C1", "1.1") is not None


def test_expired_entries_skipped_on_load(tmp_path):
    path = str(tmp_path / "conversations.json")
    old_ts = datetime.now().timestamp() - 100000
    data = {"C1:1.1": {"timestamp": old_ts, "messages": []}}
    tmp_path.joinpath("conversations.json").write_text(__import__("json").dumps(data))

    store = ConversationStore(file_path=path, ttl_seconds=86400)
    assert store.get_history("C1", "1.1") is None


def test_response_messages_roundtrip(tmp_path):
    store = ConversationStore(file_path=str(tmp_path / "conversations.json"))
    response = ModelResponse(
        parts=[TextPart(content="the answer")],
        timestamp=datetime.now(timezone.utc),
    )
    store.set_history("C1", "1.1", [response])
    loaded = store.get_history("C1", "1.1")
    assert loaded is not None
    assert loaded[0].parts[0].content == "the answer"
