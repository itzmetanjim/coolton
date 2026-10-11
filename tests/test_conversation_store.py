import json
import threading
from datetime import datetime, timezone

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from thread_context.store import ConversationStore


def _store(tmp_path, **kwargs) -> ConversationStore:
    return ConversationStore(directory=str(tmp_path / "conversations"), legacy_file=str(tmp_path / "conversations.json"), **kwargs)


def _history(*texts: str) -> list:
    parts = [UserPromptPart(content=t, timestamp=datetime.now(timezone.utc)) for t in texts]
    return [ModelRequest(parts=parts)]


def test_set_and_get_roundtrip(tmp_path):
    store = _store(tmp_path)
    history = _history("hello", "world")
    store.set_history("C1", "1.1", history)

    loaded = store.get_history("C1", "1.1")
    assert loaded is not None
    assert len(loaded) == 1
    parts = loaded[0].parts
    assert [p.content for p in parts] == ["hello", "world"]


def test_get_unknown_thread_returns_none(tmp_path):
    store = _store(tmp_path)
    assert store.get_history("C1", "1.1") is None


def test_ttl_expiry_removes_history(tmp_path):
    store = _store(tmp_path, ttl_seconds=-1)
    store.set_history("C1", "1.1", _history("hi"))
    assert store.get_history("C1", "1.1") is None


def test_a_turn_writes_only_its_own_conversation(tmp_path):
    """Every turn used to rewrite one file holding every conversation."""
    store = _store(tmp_path)
    store.set_history("C1", "1.1", _history("a"))
    other = tmp_path / "conversations" / "C1%3A1.1.json"
    before = other.stat().st_mtime_ns
    store.set_history("C2", "2.2", _history("b"))
    assert other.stat().st_mtime_ns == before
    assert sorted(p.name for p in (tmp_path / "conversations").iterdir()) == ["C1%3A1.1.json", "C2%3A2.2.json"]


def test_odd_ids_still_make_one_file_in_the_folder(tmp_path):
    store = _store(tmp_path)
    store.set_history("../..", "/etc/passwd", _history("x"))
    (only,) = (tmp_path / "conversations").iterdir()
    assert "/" not in only.name
    assert store.get_history("../..", "/etc/passwd") is not None


def test_persists_and_reloads_from_disk(tmp_path):
    store = _store(tmp_path)
    store.set_history("C1", "1.1", _history("persisted"))

    reloaded = _store(tmp_path)
    loaded = reloaded.get_history("C1", "1.1")
    assert loaded is not None
    assert loaded[0].parts[0].content == "persisted"


def test_the_old_single_file_is_split_into_one_file_per_conversation(tmp_path):
    now = datetime.now().timestamp()
    data = {
        "C1:1.1": {"timestamp": now, "messages": [], "last_seen_ts": "5.0"},
        "C2:2.2": {"nope": True},
        "C3:3.3": {"timestamp": now, "messages": "not-a-list"},
        "C4:4.4": {"timestamp": now - 100000, "messages": []},
        "C5:": {"timestamp": now, "messages": []},  # a code channel's conversation
    }
    tmp_path.joinpath("conversations.json").write_text(json.dumps(data))

    store = _store(tmp_path, ttl_seconds=86400)
    assert store.get_history("C1", "1.1") == []
    assert store.get_last_seen_ts("C1", "1.1") == "5.0"
    assert store.get_history("C2", "2.2") is None
    assert store.get_history("C3", "3.3") is None
    assert store.get_history("C4", "4.4") is None  # expired
    assert store.get_history("C5", "") == []
    assert not tmp_path.joinpath("conversations.json").exists()
    assert tmp_path.joinpath("conversations.json.migrated").exists()


def test_corrupt_json_on_disk_no_crash(tmp_path):
    store = _store(tmp_path)
    store.set_history("C1", "1.1", _history("x"))
    tmp_path.joinpath("conversations", "C1%3A1.1.json").write_text("{total garbage")
    assert store.get_history("C1", "1.1") is None
    # still writable afterwards
    store.set_history("C1", "1.1", _history("recovered"))
    assert store.get_history("C1", "1.1") is not None


def test_expired_conversations_are_deleted_at_startup(tmp_path):
    import os

    store = _store(tmp_path)
    store.set_history("C1", "1.1", _history("old"))
    path = tmp_path / "conversations" / "C1%3A1.1.json"
    os.utime(path, (1, 1))
    _store(tmp_path, ttl_seconds=86400)
    assert not path.exists()


def test_response_messages_roundtrip(tmp_path):
    store = _store(tmp_path)
    response = ModelResponse(
        parts=[TextPart(content="the answer")],
        timestamp=datetime.now(timezone.utc),
    )
    store.set_history("C1", "1.1", [response])
    loaded = store.get_history("C1", "1.1")
    assert loaded is not None
    assert loaded[0].parts[0].content == "the answer"


# ---------------------------------------------------------------------------
# One broken conversation must not stay broken forever — a stored history
# with an unpaired tool call/return gets rejected by every provider on every
# future turn (see agent/observational_memory.py's split-boundary fix for how
# this could happen), so get_history()/_load_from_disk() discard it and let
# the thread start fresh instead of failing identically forever.
# ---------------------------------------------------------------------------


def _paired_tool_round(call_id: str = "call_1") -> list:
    return [
        ModelResponse(parts=[ToolCallPart(tool_name="t", args={}, tool_call_id=call_id)]),
        ModelRequest(parts=[ToolReturnPart(tool_name="t", content="ok", tool_call_id=call_id)]),
    ]


def test_valid_tool_call_return_pair_is_returned_normally(tmp_path):
    store = _store(tmp_path)
    store.set_history("C1", "1.1", _paired_tool_round())
    assert store.get_history("C1", "1.1") is not None


def test_orphaned_tool_return_is_discarded(tmp_path):
    store = _store(tmp_path)
    # A return with no matching call anywhere — exactly what the history
    # compaction bug could produce by summarizing the call away.
    broken = [ModelRequest(parts=[ToolReturnPart(tool_name="t", content="ok", tool_call_id="orphan")])]
    store.set_history("C1", "1.1", broken)

    assert store.get_history("C1", "1.1") is None
    # Discarding persists — it's gone on disk too, not just this one read.
    assert list((tmp_path / "conversations").iterdir()) == []


def test_dangling_tool_call_is_discarded(tmp_path):
    store = _store(tmp_path)
    # A call with no return anywhere — providers reject this just as hard.
    broken = [ModelResponse(parts=[ToolCallPart(tool_name="t", args={}, tool_call_id="never_returned")])]
    store.set_history("C1", "1.1", broken)

    assert store.get_history("C1", "1.1") is None


def test_thread_recovers_after_broken_history_is_discarded(tmp_path):
    """The whole point: one broken conversation degrades to "lost its memory",
    not "permanently wedged" — the thread must be usable again right away."""
    store = _store(tmp_path)
    broken = [ModelRequest(parts=[ToolReturnPart(tool_name="t", content="ok", tool_call_id="orphan")])]
    store.set_history("C1", "1.1", broken)
    assert store.get_history("C1", "1.1") is None

    store.set_history("C1", "1.1", _history("fresh start"))
    assert store.get_history("C1", "1.1") is not None


def test_invalid_history_skipped_on_load_from_disk(tmp_path):
    orphan_return = {
        "parts": [{
            "part_kind": "tool-return",
            "tool_name": "t",
            "content": "ok",
            "tool_call_id": "orphan",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
        "kind": "request",
    }
    data = {"C1:1.1": {"timestamp": datetime.now().timestamp(), "messages": [orphan_return]}}
    tmp_path.joinpath("conversations.json").write_text(json.dumps(data))

    store = _store(tmp_path)
    assert store.get_history("C1", "1.1") is None


def test_concurrent_set_history_across_channels_all_persist(tmp_path):
    """Concurrent writers for DIFFERENT conversations must all land on disk."""
    store = _store(tmp_path)
    n = 20

    def write(i):
        store.set_history(f"C{i}", f"{i}.{i}", _history(f"msg-{i}"))

    threads = [threading.Thread(target=write, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for i in range(n):
        loaded = store.get_history(f"C{i}", f"{i}.{i}")
        assert loaded is not None, f"conversation {i} missing"
        assert loaded[0].parts[0].content == f"msg-{i}"


def test_conversation_trace_contains_thread_metadata_and_all_trace_parts(tmp_path):
    from pydantic_ai.messages import ThinkingPart, ToolCallPart, ToolReturnPart
    from thread_context.training_log import ConversationTraceStore

    now = datetime.now(timezone.utc)
    messages = [
        ModelRequest(parts=[UserPromptPart(content="fix it", timestamp=now)]),
        ModelResponse(parts=[
            ThinkingPart(content="inspect the failing test"),
            ToolCallPart(tool_name="read_file", args={"path": "tests/test_app.py"}, tool_call_id="c1"),
        ]),
        ModelRequest(parts=[
            ToolReturnPart(tool_name="read_file", content="assert False", tool_call_id="c1"),
        ]),
        ModelResponse(parts=[TextPart(content="fixed it")]),
    ]
    path = ConversationTraceStore(str(tmp_path / "logs")).write("C/one", "1.2", messages)
    import json
    document = json.loads(path.read_text())
    assert document["channel_id"] == "C/one"
    assert document["thread_ts"] == "1.2"
    parts = [part for message in document["messages"] for part in message["parts"]]
    assert {part["type"] for part in parts} == {"user", "thinking", "tool_call", "tool_result", "output"}
    assert next(part for part in parts if part["type"] == "thinking")["content"] == "inspect the failing test"
    assert next(part for part in parts if part["type"] == "tool_call")["tool_name"] == "read_file"


def test_a_code_channel_trace_is_the_channels_own_messages_in_order(tmp_path):
    """A code channel's conversation has thread_ts "" (agent.code_channel_store), which
    conversations.replies refuses with thread_not_found."""
    from unittest.mock import Mock

    from thread_context.training_log import ConversationTraceStore

    client = Mock()
    client.conversations_history.return_value = {"messages": [{"ts": "2.0"}, {"ts": "1.0"}]}

    path = ConversationTraceStore(str(tmp_path / "logs")).write_from_slack(client, "CCODE", "", [])

    client.conversations_replies.assert_not_called()
    assert [m["ts"] for m in json.loads(path.read_text())["slack_messages"]] == ["1.0", "2.0"]


def test_a_long_code_channel_is_fetched_only_back_to_its_newest_messages(tmp_path, monkeypatch):
    """The whole channel used to be fetched, page after page, on every turn."""
    from unittest.mock import Mock

    from thread_context import training_log

    monkeypatch.setattr(training_log, "MAX_SLACK_MESSAGES", 3)
    pages = iter([
        {"messages": [{"ts": "5.0"}, {"ts": "4.0"}], "has_more": True, "response_metadata": {"next_cursor": "a"}},
        {"messages": [{"ts": "3.0"}, {"ts": "2.0"}], "has_more": True, "response_metadata": {"next_cursor": "b"}},
    ])
    client = Mock()
    client.conversations_history.side_effect = lambda **kw: next(pages)

    path = training_log.ConversationTraceStore(str(tmp_path / "logs")).write_from_slack(client, "CCODE", "", [])

    assert client.conversations_history.call_count == 2
    assert [m["ts"] for m in json.loads(path.read_text())["slack_messages"]] == ["3.0", "4.0", "5.0"]


def test_a_trace_keeps_what_an_image_was_but_not_its_bytes(tmp_path):
    from pydantic_ai.messages import BinaryContent, ImageUrl

    from thread_context.training_log import ConversationTraceStore

    screenshot = BinaryContent(data=b"\x89PNG" + b"x" * 300_000, media_type="image/png")
    messages = [
        ModelRequest(parts=[UserPromptPart(content=["look", ImageUrl(url="data:image/png;base64," + "A" * 1000)])]),
        ModelResponse(parts=[ToolCallPart(tool_name="computer_use", args={}, tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="computer_use", content=screenshot, tool_call_id="c1")]),
    ]
    path = ConversationTraceStore(str(tmp_path / "logs")).write("C1", "1.1", messages)

    text = path.read_text()
    assert len(text) < 10_000
    assert "image/png, 300004 bytes, not kept" in text
    assert "AAAA" not in text


def test_last_seen_ts_only_moves_forward_and_survives_a_restart(tmp_path):
    store = _store(tmp_path)
    assert store.get_last_seen_ts("C1", "1.1") is None

    store.set_history("C1", "1.1", _history("a"), last_seen_ts="200.000")
    store.set_history("C1", "1.1", _history("b"), last_seen_ts="150.000")  # older ts: ignored
    assert store.get_last_seen_ts("C1", "1.1") == "200.000"
    store.set_history("C1", "1.1", _history("c"))  # none given: kept
    assert store.get_last_seen_ts("C1", "1.1") == "200.000"

    assert _store(tmp_path).get_last_seen_ts("C1", "1.1") == "200.000"
